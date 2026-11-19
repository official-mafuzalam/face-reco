"""Per-camera surveillance: motion gate -> object detection -> tracking -> incidents + clips."""

import json
import logging
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol

import cv2
import numpy as np
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from app.agent.verify import queue_incident_for_verification
from app.camera.manager import manager as camera_manager
from app.config import settings
from app.db import SessionLocal
from app.models import Camera, Incident
from app.loop_stats import LoopSnapshot, LoopStats
from app.notify.sources import notify_rule_hit
from app.pipeline.overlay import ObjectOverlay, face_overlays, object_overlays
from app.surveillance.clips import ClipRecorder
from app.surveillance.detector import ObjectDetector, effective_surveillance_fps
from app.surveillance.motion import MotionGate
from app.surveillance.rules import RuleEvaluator, RuleHit, RuleProvider, local_time_now
from app.surveillance.tracker import ObjectTrack, ObjectTracker, attach_identities


logger = logging.getLogger(__name__)
PROJECT_ROOT = Path(__file__).resolve().parents[2]
MEDIA_ROOT = PROJECT_ROOT / "data" / "incidents"
MODEL_PATH = PROJECT_ROOT / "models" / "object_detection_yolox_2022nov.onnx"
INCIDENT_CATEGORIES = ("person", "vehicle")
_BOX_COLOURS = {"person": (48, 178, 242), "vehicle": (79, 127, 30), "bag": (200, 200, 200)}


class IncidentStore(Protocol):
    def open(self, **fields: object) -> int: ...

    def update(self, incident_id: int, **fields: object) -> None: ...


class DbIncidentStore:
    def __init__(self, session_factory: sessionmaker[Session] = SessionLocal) -> None:
        self._session_factory = session_factory

    def open(self, **fields: object) -> int:
        with self._session_factory() as db:
            incident = Incident(**fields)
            db.add(incident)
            db.commit()
            return incident.id

    def update(self, incident_id: int, **fields: object) -> None:
        with self._session_factory() as db:
            incident = db.get(Incident, incident_id)
            if incident is None:
                return
            for key, value in fields.items():
                setattr(incident, key, value)
            db.commit()


@dataclass
class OpenIncident:
    incident_id: int
    kind: str
    opened_at: float
    people: int
    vehicles: int
    labels: dict[str, int] = field(default_factory=dict)


@dataclass
class RecordingSession:
    """One clip. Every incident raised while it runs points at the same clip file."""

    opened_at: float
    clip_path: str | None
    incident_ids: list[int] = field(default_factory=list)
    activity: OpenIncident | None = None
    # rule id -> (incident id, people, vehicles, directions seen)
    rule_incidents: dict[int, tuple[int, int, int, set[str]]] = field(default_factory=dict)


def _media_paths(camera_id: int, media_root: Path) -> tuple[Path, str]:
    now = datetime.now(UTC)
    day = Path(now.strftime("%Y")) / now.strftime("%m") / now.strftime("%d")
    stem = f"camera-{camera_id}-{time.time_ns()}"
    return media_root / day / stem, (Path("data") / "incidents" / day / stem).as_posix()


def draw_tracks(frame: np.ndarray, tracks: list[ObjectTrack]) -> np.ndarray:
    marked = frame.copy()
    for track in tracks:
        x, y, w, h = (int(v) for v in track.box)
        colour = _BOX_COLOURS.get(track.category, (255, 255, 255))
        cv2.rectangle(marked, (x, y), (x + w, y + h), colour, 2)
        cv2.putText(marked, track.label, (x, max(12, y - 6)), cv2.FONT_HERSHEY_SIMPLEX, 0.5, colour, 1, cv2.LINE_AA)
    return marked


def draw_rule_hit(frame: np.ndarray, hit: RuleHit) -> np.ndarray:
    marked = frame.copy()
    height, width = marked.shape[:2]
    points = np.array([(int(x * width), int(y * height)) for x, y in hit.rule.points], np.int32)
    cv2.polylines(marked, [points], hit.rule.kind != "line", (48, 178, 242), 2, cv2.LINE_AA)
    if hit.track is not None:
        x, y, w, h = (int(v) for v in hit.track.box)
        cv2.rectangle(marked, (x, y), (x + w, y + h), (30, 38, 179), 3)
    else:
        x, y = int(points[:, 0].min()), int(points[:, 1].min())
    cv2.putText(marked, hit.rule.name, (x, max(14, y - 8)), cv2.FONT_HERSHEY_SIMPLEX, 0.6, (30, 38, 179), 2, cv2.LINE_AA)
    return marked


class IncidentEngine:
    """Turns activity and rule hits on one camera into incidents that share a recorded clip."""

    def __init__(
        self,
        camera_id: int,
        store: IncidentStore,
        recorder: ClipRecorder,
        *,
        media_root: Path = MEDIA_ROOT,
        idle_seconds: float | None = None,
        max_seconds: float | None = None,
        notify: bool | None = None,
    ) -> None:
        self.camera_id = camera_id
        self.store = store
        # Live notifications only for incidents that really reach the database.
        self.notify = isinstance(store, DbIncidentStore) if notify is None else notify
        self.recorder = recorder
        self.media_root = media_root
        self.idle_seconds = idle_seconds if idle_seconds is not None else settings.incident_idle_seconds
        self.max_seconds = max_seconds if max_seconds is not None else settings.clip_max_seconds
        self.session: RecordingSession | None = None
        self._last_activity = 0.0

    @property
    def current(self) -> OpenIncident | None:
        return self.session.activity if self.session is not None else None

    def update(
        self,
        frame: np.ndarray,
        tracks: list[ObjectTrack],
        now: float,
        hits: list[RuleHit] | None = None,
    ) -> None:
        hits = hits or []
        # Only objects seen in this frame and not parked/standing still count as activity.
        active = [
            track
            for track in tracks
            if track.category in INCIDENT_CATEGORIES
            and track.last_seen == now
            and not track.is_stationary(now)
        ]
        if active or hits:
            self._last_activity = now
            if self.session is not None and now - self.session.opened_at >= self.max_seconds:
                self.close()
        if active:
            if self.current is None:
                self._open_activity(frame, active, now)
            else:
                self._refresh(active)
        for hit in hits:
            self._record_hit(frame, hit, now)
        if not active and not hits and self.session is not None:
            if now - self._last_activity >= self.idle_seconds:
                self.close()

    def _ensure_session(self, now: float) -> tuple[RecordingSession, Path, str]:
        base, relative = _media_paths(self.camera_id, self.media_root)
        base.parent.mkdir(parents=True, exist_ok=True)
        if self.session is None:
            clip_file = self.recorder.start(base)
            clip = relative + clip_file.suffix if clip_file is not None else None
            self.session = RecordingSession(now, clip)
        return self.session, base, relative

    @staticmethod
    def _snapshot(image: np.ndarray, base: Path, relative: str) -> str | None:
        return relative + ".jpg" if cv2.imwrite(str(base.with_suffix(".jpg")), image) else None

    @staticmethod
    def _counts(active: list[ObjectTrack]) -> tuple[int, int, dict[str, int]]:
        labels: dict[str, int] = {}
        for track in active:
            labels[track.label] = labels.get(track.label, 0) + 1
        people = sum(track.category == "person" for track in active)
        return people, len(active) - people, labels

    def _open_activity(self, frame: np.ndarray, active: list[ObjectTrack], now: float) -> None:
        session, base, relative = self._ensure_session(now)
        people, vehicles, labels = self._counts(active)
        kind = "person" if people else "vehicle"
        incident_id = self.store.open(
            camera_id=self.camera_id,
            kind=kind,
            severity="info",
            started_at=datetime.now(UTC),
            person_count=people,
            vehicle_count=vehicles,
            details=json.dumps({"labels": labels}),
            snapshot_path=self._snapshot(draw_tracks(frame, active), base, relative),
            clip_path=session.clip_path,
        )
        session.incident_ids.append(incident_id)
        session.activity = OpenIncident(incident_id, kind, now, people, vehicles, labels)

    def _record_hit(self, frame: np.ndarray, hit: RuleHit, now: float) -> None:
        session, base, relative = self._ensure_session(now)
        category = hit.track.category if hit.track is not None else ""
        is_person = category == "person"
        is_vehicle = category == "vehicle"
        existing = session.rule_incidents.get(hit.rule.id)
        if existing is not None:
            # Same rule again within this clip: count it on the existing incident instead of a new one.
            incident_id, people, vehicles, directions = existing
            people, vehicles = people + is_person, vehicles + is_vehicle
            if hit.detail in ("ab", "ba"):
                directions.add(hit.detail)
            session.rule_incidents[hit.rule.id] = (incident_id, people, vehicles, directions)
            details = {
                "rule": hit.rule.name,
                "rule_id": hit.rule.id,
                "hits": people + vehicles,
                "detail": ("any" if len(directions) == 2 else next(iter(directions))) if directions else hit.detail,
                "labels": {k: v for k, v in (("person", people), ("vehicle", vehicles)) if v},
            }
            self.store.update(incident_id, person_count=people, vehicle_count=vehicles,
                              details=json.dumps(details))
            return
        incident_id = self.store.open(
            camera_id=self.camera_id,
            kind=hit.rule.kind,
            severity=hit.rule.severity,
            started_at=datetime.now(UTC),
            person_count=int(is_person),
            vehicle_count=int(is_vehicle),
            details=json.dumps(
                {
                    "rule": hit.rule.name,
                    "rule_id": hit.rule.id,
                    "label": hit.track.label if hit.track is not None else "",
                    "detail": hit.detail,
                    "labels": {hit.track.label: 1} if hit.track is not None else {},
                }
            ),
            snapshot_path=self._snapshot(draw_rule_hit(frame, hit), base, relative),
            clip_path=session.clip_path,
        )
        session.incident_ids.append(incident_id)
        directions = {hit.detail} if hit.detail in ("ab", "ba") else set()
        session.rule_incidents[hit.rule.id] = (incident_id, int(is_person), int(is_vehicle), directions)
        if self.notify:
            notify_rule_hit(incident_id, self.camera_id, hit.rule.name, hit.rule.severity)

    def _refresh(self, active: list[ObjectTrack]) -> None:
        current = self.current
        people, vehicles, labels = self._counts(active)
        changes: dict[str, object] = {}
        if people > current.people:
            current.people = changes["person_count"] = people
        if vehicles > current.vehicles:
            current.vehicles = changes["vehicle_count"] = vehicles
        if current.kind == "vehicle" and people:
            current.kind = changes["kind"] = "person"
        merged = {label: max(count, current.labels.get(label, 0)) for label, count in labels.items()}
        if any(current.labels.get(label, 0) < count for label, count in merged.items()):
            current.labels.update(merged)
            changes["details"] = json.dumps({"labels": current.labels})
        if changes:
            self.store.update(current.incident_id, **changes)

    def close(self) -> None:
        if self.session is None:
            return
        session, self.session = self.session, None
        self.recorder.stop()
        ended = datetime.now(UTC)
        for incident_id in session.incident_ids:
            self.store.update(incident_id, ended_at=ended)
            queue_incident_for_verification(incident_id)


def _camera_confidence(camera_id: int) -> float | None:
    with SessionLocal() as db:
        return db.scalar(select(Camera.object_confidence).where(Camera.id == camera_id))


class SurveillanceWorker:
    def __init__(
        self,
        camera_id: int,
        detector: ObjectDetector,
        *,
        store: IncidentStore | None = None,
        frame_provider: Callable[[int], np.ndarray | None] = camera_manager.get_latest_frame,
        media_root: Path = MEDIA_ROOT,
        rule_provider: Callable[[], list] | None = None,
        confidence_provider: Callable[[], float | None] | None = None,
    ) -> None:
        self.camera_id = camera_id
        self.detector = detector
        self._confidence_provider = confidence_provider
        self.frame_provider = frame_provider
        self.stats = LoopStats()
        self.motion = MotionGate(settings.motion_threshold)
        self.tracker = ObjectTracker()
        self.recorder = ClipRecorder(settings.clip_fps, settings.clip_pre_seconds, settings.clip_max_width)
        self.engine = IncidentEngine(camera_id, store or DbIncidentStore(), self.recorder, media_root=media_root)
        self.rules = rule_provider or RuleProvider(camera_id)
        self.evaluator = RuleEvaluator()
        self._confidence_at = -1e9
        self._confidence_value = settings.object_confidence
        self._stop = threading.Event()
        self._threads = [
            threading.Thread(target=self._analyse_loop, name=f"surveillance-{camera_id}", daemon=True),
            threading.Thread(target=self._record_loop, name=f"surveillance-rec-{camera_id}", daemon=True),
        ]

    def start(self) -> None:
        for thread in self._threads:
            thread.start()

    def stop(self, timeout: float = 10.0) -> None:
        self._stop.set()
        for thread in self._threads:
            if thread.is_alive() and thread is not threading.current_thread():
                thread.join(timeout=timeout)
        object_overlays.clear(self.camera_id)
        try:
            self.engine.close()
        except Exception:
            logger.exception("Unable to close incident for camera %s.", self.camera_id)

    def _confidence(self, now: float) -> float:
        """The camera's own detection confidence (set on the tuning page), else the global one."""
        if self._confidence_provider is not None and now - self._confidence_at >= 10.0:
            self._confidence_at = now
            try:
                self._confidence_value = self._confidence_provider() or settings.object_confidence
            except Exception:
                logger.exception("Unable to load the detection confidence for camera %s.", self.camera_id)
        return self._confidence_value

    def process(self, frame: np.ndarray, now: float) -> None:
        moved = self.motion.moved(frame)
        detections = []
        if moved or self.tracker.tracks:
            detections = self.detector.detect(frame, self._confidence(now))
        tracks = self.tracker.update(detections, now)
        attach_identities(tracks, face_overlays.latest(self.camera_id))
        rules = self.rules()
        hits = []
        if rules:
            seen = [track for track in tracks if track.last_seen == now]
            hits = self.evaluator.evaluate(
                rules, seen, frame.shape, now, local_time_now(), set(self.tracker.tracks), frame
            )
        self.engine.update(frame, tracks, now, hits)
        object_overlays.publish(
            self.camera_id,
            [
                ObjectOverlay(track.box, track.category, track.label, track.track_id)
                for track in tracks
                if track.last_seen == now
            ],
            {hit.rule.id for hit in hits},
        )

    def _analyse_loop(self) -> None:
        interval = 1.0 / effective_surveillance_fps()
        while not self._stop.is_set():
            started = time.monotonic()
            try:
                frame = self.frame_provider(self.camera_id)
                if frame is not None:
                    self.process(frame, started)
                    self.stats.record(time.monotonic() - started)
            except Exception:
                logger.exception("Surveillance failed on camera %s.", self.camera_id)
            self._stop.wait(max(0.0, interval - (time.monotonic() - started)))

    def _record_loop(self) -> None:
        interval = 1.0 / settings.clip_fps
        while not self._stop.is_set():
            started = time.monotonic()
            try:
                frame = self.frame_provider(self.camera_id)
                if frame is not None:
                    self.recorder.push(frame)
            except Exception:
                logger.exception("Clip recording failed on camera %s.", self.camera_id)
            self._stop.wait(max(0.0, interval - (time.monotonic() - started)))


class SurveillanceManager:
    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._workers: dict[int, SurveillanceWorker] = {}
        self._detector: ObjectDetector | None = None
        self.unavailable_reason: str | None = None

    def _get_detector(self) -> ObjectDetector | None:
        if self._detector is None and self.unavailable_reason is None:
            try:
                self._detector = ObjectDetector(
                    MODEL_PATH,
                    backend=settings.detector_backend,
                    preferred_provider=settings.hardware_profile,
                )
            except Exception as exc:
                self.unavailable_reason = str(exc)
                logger.error("Surveillance disabled: %s", exc)
        return self._detector

    def start(self, camera_id: int) -> None:
        if not settings.surveillance_enabled:
            return
        self.stop(camera_id)
        detector = self._get_detector()
        if detector is None:
            return
        worker = SurveillanceWorker(camera_id, detector, confidence_provider=lambda: _camera_confidence(camera_id))
        with self._lock:
            self._workers[camera_id] = worker
        worker.start()

    def start_enabled(self, camera_ids: list[int]) -> None:
        for camera_id in camera_ids:
            self.start(camera_id)

    def stop(self, camera_id: int) -> None:
        with self._lock:
            worker = self._workers.pop(camera_id, None)
        if worker is not None:
            worker.stop()

    def stop_all(self) -> None:
        with self._lock:
            camera_ids = list(self._workers)
        for camera_id in camera_ids:
            self.stop(camera_id)

    def running(self, camera_id: int) -> bool:
        with self._lock:
            return camera_id in self._workers

    def stats(self, camera_id: int) -> LoopSnapshot | None:
        with self._lock:
            worker = self._workers.get(camera_id)
        return worker.stats.snapshot() if worker is not None else None


surveillance_manager = SurveillanceManager()
