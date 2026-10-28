import logging
import queue
import threading
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Callable

import cv2
import numpy as np
from cryptography.fernet import Fernet
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from app.camera.manager import manager as camera_manager
from app.camera.reader import mask_credentials
from app.config import settings
from app.db import SessionLocal
from app.models import Camera, Embedding, Event, Person
from app.pipeline.debounce import EventDebouncer
from app.pipeline.tracker import DetectionObservation, IoUTracker, TrackedObservation
from app.vision.detector import Face, FaceDetector
from app.vision.embedder import FaceEmbedder
from app.vision.matcher import Gallery
from app.vision.quality import FaceQuality


logger = logging.getLogger(__name__)
_fernet = Fernet(settings.fernet_key.encode())
_PROJECT_ROOT = Path(__file__).resolve().parents[2]


@dataclass(frozen=True)
class EventRecord:
    person_id: int | None
    camera_id: int
    gate: str
    direction: str
    at: datetime
    score: float | None
    status: str
    snapshot_path: str | None


class EventWriter:
    def __init__(self, session_factory: sessionmaker[Session] = SessionLocal) -> None:
        self._session_factory = session_factory
        self._queue: queue.Queue[EventRecord | None] = queue.Queue()
        self._thread = threading.Thread(target=self._run, name="event-db-writer", daemon=True)
        self._started = False

    def start(self) -> None:
        if not self._started:
            self._started = True
            self._thread.start()

    def submit(self, event: EventRecord) -> None:
        if not self._started:
            raise RuntimeError("EventWriter must be started before submitting events.")
        self._queue.put(event)

    def stop(self, timeout: float = 10.0) -> None:
        if not self._started:
            return
        self._queue.put(None)
        self._thread.join(timeout=timeout)
        if self._thread.is_alive():
            raise RuntimeError("Event writer did not stop before timeout.")
        self._started = False

    def _run(self) -> None:
        while True:
            record = self._queue.get()
            try:
                if record is None:
                    return
                with self._session_factory() as db:
                    db.add(
                        Event(
                            person_id=record.person_id,
                            camera_id=record.camera_id,
                            gate=record.gate,
                            direction=record.direction,
                            at=record.at,
                            score=record.score,
                            status=record.status,
                            snapshot_path=record.snapshot_path,
                        )
                    )
                    db.commit()
            except Exception:
                logger.exception("Unable to persist pipeline event.")
            finally:
                self._queue.task_done()


class GalleryProvider:
    def __init__(
        self,
        initial: Gallery,
        session_factory: sessionmaker[Session] = SessionLocal,
        refresh_seconds: float = 2.0,
    ) -> None:
        self._gallery = initial
        self._session_factory = session_factory
        self._refresh_seconds = refresh_seconds
        self._last_refresh = time.monotonic()
        self._lock = threading.Lock()

    def __call__(self) -> Gallery:
        now = time.monotonic()
        if now - self._last_refresh < self._refresh_seconds:
            return self._gallery
        with self._lock:
            now = time.monotonic()
            if now - self._last_refresh >= self._refresh_seconds:
                try:
                    with self._session_factory() as db:
                        self._gallery = load_active_gallery(db)
                except Exception:
                    logger.exception("Unable to refresh recognition gallery.")
                finally:
                    self._last_refresh = now
            return self._gallery


class PipelineWorker:
    def __init__(
        self,
        camera_id: int,
        gate: str,
        direction: str,
        gallery: Gallery,
        event_writer: EventWriter,
        *,
        detector: object | None = None,
        embedder: object | None = None,
        quality: object | None = None,
        gallery_provider: Callable[[], Gallery] | None = None,
        frame_provider: Callable[[int], np.ndarray | None] = camera_manager.get_latest_frame,
        fps: float | None = None,
        consensus_n: int | None = None,
        consensus_window: int | None = None,
        debounce: EventDebouncer | None = None,
        snapshot_dir: Path | None = None,
    ) -> None:
        self.camera_id = camera_id
        self.gate = gate
        self.direction = direction
        self.gallery = gallery
        self.gallery_provider = gallery_provider
        self.event_writer = event_writer
        self.detector = detector
        self.embedder = embedder
        self.quality = quality
        self.frame_provider = frame_provider
        self.interval = 1.0 / (fps if fps is not None else settings.pipeline_fps)
        if self.interval <= 0:
            raise ValueError("pipeline_fps must be positive.")
        self.tracker = IoUTracker(
            consensus_n=consensus_n if consensus_n is not None else settings.consensus_n,
            consensus_window=(
                consensus_window if consensus_window is not None else settings.consensus_window
            ),
        )
        self.debounce = debounce or EventDebouncer(
            debounce_s=settings.debounce_s,
            unknown_debounce_s=settings.unknown_debounce_s,
            unknown_similarity=settings.unknown_group_similarity,
        )
        self.snapshot_dir = snapshot_dir or _PROJECT_ROOT / "data" / "snapshots"
        self._stop_event = threading.Event()
        self._thread = threading.Thread(
            target=self._run,
            name=f"pipeline-camera-{camera_id}",
            daemon=True,
        )

    def start(self) -> None:
        self._thread.start()

    def stop(self, timeout: float = 10.0) -> None:
        self._stop_event.set()
        if self._thread.is_alive() and self._thread is not threading.current_thread():
            self._thread.join(timeout=timeout)
            if self._thread.is_alive():
                raise RuntimeError(f"Pipeline worker {self.camera_id} did not stop.")

    def _ensure_vision(self) -> None:
        model_dir = _PROJECT_ROOT / "models"
        if self.detector is None:
            self.detector = FaceDetector(model_dir / "face_detection_yunet_2023mar.onnx")
        if self.embedder is None:
            self.embedder = FaceEmbedder(model_dir / "face_recognition_sface_2021dec.onnx")
        if self.quality is None:
            self.quality = FaceQuality(min_face_width=settings.min_face_px)

    def _run(self) -> None:
        while not self._stop_event.is_set():
            started = time.monotonic()
            try:
                self._ensure_vision()
                frame = self.frame_provider(self.camera_id)
                if frame is not None:
                    self.process_frame(frame)
            except Exception as exc:
                logger.warning(
                    "Pipeline camera %s failed: %s",
                    self.camera_id,
                    mask_credentials(str(exc)),
                )
            delay = max(0.0, self.interval - (time.monotonic() - started))
            self._stop_event.wait(delay)

    def _embed(self, frame: np.ndarray, face: Face) -> np.ndarray:
        embedding = np.asarray(self.embedder.align_and_embed(frame, face), dtype=np.float32).reshape(-1)
        if embedding.shape != (128,) or not np.isfinite(embedding).all():
            raise ValueError("Embedder returned invalid vector.")
        norm = float(np.linalg.norm(embedding))
        if norm <= 0:
            raise ValueError("Embedder returned zero vector.")
        return (embedding / norm).astype(np.float32)

    def process_frame(self, frame: np.ndarray, now: float | None = None) -> None:
        if self.gallery_provider is not None:
            self.gallery = self.gallery_provider()
        observations: list[DetectionObservation] = []
        for face in self.detector.detect(frame):
            quality_ok, _reasons = self.quality.check(frame, face)
            if not quality_ok:
                observations.append(
                    DetectionObservation(face.box, None, face.score, low_quality=True)
                )
                continue
            embedding = self._embed(frame, face)
            person_id, score, _second_best = self.gallery.match(embedding)
            observations.append(
                DetectionObservation(face.box, person_id, score, embedding=embedding)
            )

        current_time = time.monotonic() if now is None else now
        for tracked in self.tracker.update(observations):
            self._handle_observation(frame, tracked, current_time)

    def _handle_observation(
        self,
        frame: np.ndarray,
        tracked: TrackedObservation,
        now: float,
    ) -> None:
        observation = tracked.observation
        track = tracked.track
        if observation.low_quality:
            if not track.low_quality_logged:
                track.low_quality_logged = True
                self._submit_event(None, None, "low_quality", frame, observation.box)
            return

        person_id = track.consensus_person(self.tracker.consensus_n)
        if person_id is not None and track.matched_logged != person_id:
            if self.debounce.allow_matched(person_id, self.gate, self.direction, now):
                self._submit_event(
                    person_id,
                    track.consensus_score(person_id),
                    "matched",
                    frame,
                    observation.box,
                )
                track.matched_logged = person_id
            return

        if observation.person_id is None and observation.embedding is not None:
            if self.debounce.allow_unknown(observation.embedding, now):
                self._submit_event(None, observation.score, "unknown", frame, observation.box)

    def _submit_event(
        self,
        person_id: int | None,
        score: float | None,
        status: str,
        frame: np.ndarray,
        box: tuple[float, float, float, float],
    ) -> None:
        save_snapshot = status == "unknown" or (
            status == "matched" and settings.save_match_snapshots
        )
        snapshot_path = self._snapshot(frame, box) if save_snapshot else None
        self.event_writer.submit(
            EventRecord(
                person_id=person_id,
                camera_id=self.camera_id,
                gate=self.gate,
                direction=self.direction,
                at=datetime.now(UTC),
                score=score,
                status=status,
                snapshot_path=snapshot_path,
            )
        )

    def _snapshot(self, frame: np.ndarray, box: tuple[float, float, float, float]) -> str:
        now = datetime.now(UTC)
        relative = Path("data") / "snapshots" / now.strftime("%Y") / now.strftime("%m") / now.strftime("%d")
        directory = self.snapshot_dir / now.strftime("%Y") / now.strftime("%m") / now.strftime("%d")
        directory.mkdir(parents=True, exist_ok=True)
        x, y, width, height = box
        left, top = max(0, int(x)), max(0, int(y))
        right = min(frame.shape[1], int(x + width))
        bottom = min(frame.shape[0], int(y + height))
        marked = frame.copy()
        cv2.rectangle(marked, (left, top), (right, bottom), (0, 255, 0), 2)
        filename = f"camera-{self.camera_id}-{time.time_ns()}.jpg"
        path = directory / filename
        if not cv2.imwrite(str(path), marked):
            raise OSError("Unable to save event snapshot.")
        return (relative / filename).as_posix()


def load_active_gallery(db: Session) -> Gallery:
    rows = db.execute(
        select(Embedding.person_id, Embedding.vector_encrypted)
        .join(Person, Person.id == Embedding.person_id)
        .where(Person.active.is_(True))
        .order_by(Embedding.id)
    ).all()
    vectors: list[np.ndarray] = []
    person_ids: list[int] = []
    for person_id, encrypted in rows:
        vector = np.frombuffer(_fernet.decrypt(encrypted), dtype=np.float32)
        if vector.shape != (128,) or not np.isfinite(vector).all():
            logger.warning("Skipping malformed enrollment embedding %s.", person_id)
            continue
        if np.linalg.norm(vector) == 0:
            logger.warning("Skipping zero enrollment embedding %s.", person_id)
            continue
        person_ids.append(person_id)
        vectors.append(vector)
    matrix = np.stack(vectors) if vectors else np.empty((0, 128), dtype=np.float32)
    return Gallery(
        person_ids,
        matrix,
        threshold=settings.match_threshold,
        margin=settings.match_margin,
    )


class PipelineManager:
    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._workers: dict[int, PipelineWorker] = {}
        self._writer: EventWriter | None = None

    def start_enabled(
        self,
        cameras: list[tuple[int, str, str]],
        gallery: Gallery,
        *,
        worker_factory: Callable[..., PipelineWorker] = PipelineWorker,
        writer: EventWriter | None = None,
    ) -> None:
        self.stop_all()
        self._writer = writer or EventWriter()
        self._writer.start()
        gallery_provider = GalleryProvider(gallery)
        shared_debounce = EventDebouncer(
            settings.debounce_s,
            settings.unknown_debounce_s,
            settings.unknown_group_similarity,
        )
        try:
            for camera_id, gate, direction in cameras:
                worker = worker_factory(
                    camera_id,
                    gate,
                    direction,
                    gallery,
                    self._writer,
                    gallery_provider=gallery_provider,
                    debounce=shared_debounce,
                )
                with self._lock:
                    self._workers[camera_id] = worker
                worker.start()
        except Exception:
            self.stop_all()
            raise

    def stop_all(self) -> None:
        with self._lock:
            workers = list(self._workers.values())
            self._workers.clear()
            writer = self._writer
            self._writer = None
        worker_errors: list[Exception] = []
        for worker in workers:
            try:
                worker.stop()
            except Exception as exc:
                worker_errors.append(exc)
        if writer is not None:
            writer.stop()
        if worker_errors:
            raise RuntimeError("One or more pipeline workers failed to stop.") from worker_errors[0]


pipeline_manager = PipelineManager()
