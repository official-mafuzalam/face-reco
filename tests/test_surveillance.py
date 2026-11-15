from contextlib import asynccontextmanager
from collections.abc import AsyncIterator, Generator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import cv2
import numpy as np
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.auth import hash_password
from app.db import Base, get_db
from app.main import app
from app.models import Camera, Incident, User
from app.surveillance import detector as detector_module
from app.surveillance.clips import ClipRecorder
from app.surveillance.detector import ObjectDetection, _grids, decode
from app.surveillance.tracker import ObjectTracker
from app.surveillance.worker import DbIncidentStore, IncidentEngine, SurveillanceWorker
from app.web.routes import incidents as incidents_routes


def _person(x: float = 100, category: str = "person", label: str = "person") -> ObjectDetection:
    return ObjectDetection((x, 50, 40, 100), label, category, 0.9)


def _frame(value: int = 60) -> np.ndarray:
    frame = np.full((240, 320, 3), value, np.uint8)
    cv2.rectangle(frame, (value % 200, 40), (value % 200 + 40, 140), (255, 255, 255), -1)
    return frame


def test_decode_maps_grid_prediction_to_frame_box() -> None:
    grids, strides = _grids(640)
    raw = np.zeros((8400, 85), np.float32)
    raw[:, 4] = 0.0
    # cell (10, 20) at stride 8 -> centre (84, 164); size exp(log(8)) * 8 = 64 px
    index = 20 * 80 + 10
    raw[index, :4] = [0.5, 0.5, np.log(8), np.log(8)]
    raw[index, 4] = 1.0
    raw[index, 5 + 0] = 0.95  # person
    detections = decode(raw, 0.5, (480, 640, 3), grids, strides, 0.45, 0.5)
    assert len(detections) == 1
    det = detections[0]
    assert det.label == "person" and det.category == "person"
    x, y, w, h = det.box
    assert (round(x), round(y), round(w), round(h)) == (104, 264, 128, 128)


def test_decode_ignores_uninteresting_classes() -> None:
    grids, strides = _grids(640)
    raw = np.zeros((8400, 85), np.float32)
    raw[0, 4] = 1.0
    raw[0, 5 + 41] = 0.99  # cup
    assert decode(raw, 1.0, (640, 640, 3), grids, strides, 0.45, 0.5) == []


def test_tracker_confirms_after_two_hits_and_drops_lost_tracks() -> None:
    tracker = ObjectTracker(lost_after_s=2.0, min_hits=2)
    assert tracker.update([_person()], 0.0) == []
    confirmed = tracker.update([_person(104)], 0.5)
    assert len(confirmed) == 1
    track_id = confirmed[0].track_id
    assert tracker.update([_person(108)], 1.0)[0].track_id == track_id
    # Same place but a different category starts a new track.
    tracker.update([_person(108, "vehicle", "car")], 1.5)
    assert len(tracker.tracks) == 2
    tracker.update([], 5.0)
    assert tracker.tracks == {}


class FakeStore:
    def __init__(self) -> None:
        self.rows: dict[int, dict[str, object]] = {}

    def open(self, **fields: object) -> int:
        incident_id = len(self.rows) + 1
        self.rows[incident_id] = dict(fields)
        return incident_id

    def update(self, incident_id: int, **fields: object) -> None:
        self.rows[incident_id].update(fields)


def _tracks(tracker: ObjectTracker, dets: list[ObjectDetection], now: float):
    tracker.update(dets, now - 0.1)
    return tracker.update(dets, now)


def test_engine_opens_upgrades_and_closes_incident_with_playable_clip(tmp_path: Path) -> None:
    store = FakeStore()
    recorder = ClipRecorder(fps=5, pre_seconds=1, max_width=320)
    engine = IncidentEngine(1, store, recorder, media_root=tmp_path, idle_seconds=2, max_seconds=60)
    tracker = ObjectTracker()
    for i in range(5):
        recorder.push(_frame(i * 10))

    engine.update(_frame(), _tracks(tracker, [_person(100, "vehicle", "car")], 1.0), 1.0)
    assert engine.current is not None
    row = store.rows[1]
    assert row["kind"] == "vehicle" and row["vehicle_count"] == 1
    assert (tmp_path.parent / row["snapshot_path"]).name.endswith(".jpg")

    for i in range(6):
        recorder.push(_frame(100 + i * 10))
    tracks = tracker.update([_person(100, "vehicle", "car"), _person(200)], 1.5)
    tracks = tracker.update([_person(100, "vehicle", "car"), _person(200)], 2.0)
    engine.update(_frame(), tracks, 2.0)
    assert store.rows[1]["kind"] == "person"
    assert store.rows[1]["person_count"] == 1

    engine.update(_frame(), [], 3.0)
    assert engine.current is not None  # still within idle window
    engine.update(_frame(), [], 4.5)
    assert engine.current is None
    assert isinstance(store.rows[1]["ended_at"], datetime)

    clips = list(tmp_path.rglob("*.mp4")) + list(tmp_path.rglob("*.webm"))
    assert len(clips) == 1
    capture = cv2.VideoCapture(str(clips[0]))
    frames = 0
    while capture.read()[0]:
        frames += 1
    capture.release()
    assert frames >= 10  # pre-roll plus frames recorded while open


def test_engine_splits_long_activity(tmp_path: Path) -> None:
    store = FakeStore()
    recorder = ClipRecorder(fps=5, pre_seconds=1, max_width=320)
    recorder.push(_frame())
    engine = IncidentEngine(1, store, recorder, media_root=tmp_path, idle_seconds=5, max_seconds=10)
    tracker = ObjectTracker()
    now = 0.9
    while now <= 11.5:  # a person walking continuously for longer than max_seconds
        engine.update(_frame(), tracker.update([_person(50 + now * 20)], now), now)
        now = round(now + 0.5, 1)
    assert len(store.rows) == 2
    assert "ended_at" in store.rows[1] and "ended_at" not in store.rows[2]


class FakeDetector:
    def __init__(self) -> None:
        self.calls = 0

    def detect(self, frame: np.ndarray, confidence: float = 0.45) -> list[ObjectDetection]:
        self.calls += 1
        return [_person()]


def test_worker_skips_detection_without_motion(tmp_path: Path) -> None:
    detector = FakeDetector()
    store = FakeStore()
    worker = SurveillanceWorker(
        1, detector, store=store, frame_provider=lambda _id: None, media_root=tmp_path, rule_provider=list
    )
    still = _frame(80)
    worker.recorder.push(still)
    worker.process(still, 0.0)  # first frame always counts as motion
    assert detector.calls == 1
    worker.tracker.tracks.clear()
    worker.process(still, 0.5)
    assert detector.calls == 1  # nothing moved and nothing tracked
    worker.process(_frame(150), 1.0)
    worker.process(_frame(160), 1.5)
    assert detector.calls == 3
    assert len(store.rows) == 1
    worker.engine.close()


# ---------------- web ----------------

PASSWORD = "admin-password-123"


@pytest.fixture
def web(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)

    def override_get_db() -> Generator[Session, None, None]:
        with factory() as db:
            yield db

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        yield

    media = tmp_path / "data" / "incidents"
    media.mkdir(parents=True)
    monkeypatch.setattr(incidents_routes, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(incidents_routes, "MEDIA_ROOT", media.resolve())
    monkeypatch.setattr(app.router, "lifespan_context", lifespan)
    app.dependency_overrides[get_db] = override_get_db
    with factory() as db:
        db.add(User(username="guard", password_hash=hash_password(PASSWORD), role="security"))
        db.add(Camera(name="Yard", gate="Back", direction="IN", rtsp_url_encrypted="x"))
        db.commit()
    with TestClient(app) as client:
        yield client, factory, media
    app.dependency_overrides.clear()
    engine.dispose()


def test_incident_pages_require_login_and_serve_media(web) -> None:
    client, factory, media = web
    (media / "a.jpg").write_bytes(b"\xff\xd8jpeg")
    (media / "a.webm").write_bytes(b"webm")
    store = DbIncidentStore(factory)
    started = datetime.now(UTC) - timedelta(minutes=1)
    incident_id = store.open(
        camera_id=1, kind="person", severity="info", started_at=started, person_count=2,
        vehicle_count=0, details='{"labels": {"person": 2}}',
        snapshot_path="data/incidents/a.jpg", clip_path="data/incidents/a.webm",
    )
    assert client.get("/incidents").status_code == 401

    client.post("/login", data={"username": "guard", "password": PASSWORD})
    page = client.get("/incidents")
    assert page.status_code == 200 and f"/incidents/{incident_id}" in page.text
    assert client.get(f"/incidents/{incident_id}/snapshot.jpg").status_code == 200
    # Clip is only served once the incident has ended (file is still being written before that).
    assert client.get(f"/incidents/{incident_id}/clip").status_code == 404
    store.update(incident_id, ended_at=started + timedelta(seconds=42))
    clip = client.get(f"/incidents/{incident_id}/clip")
    assert clip.status_code == 200 and clip.headers["content-type"] == "video/webm"
    detail = client.get(f"/incidents/{incident_id}")
    assert "0:42" in detail.text and "<video" in detail.text

    assert client.get("/incidents?kind=vehicle").text.count(f'href="/incidents/{incident_id}?') == 0


def test_incident_media_paths_cannot_escape_media_root(web) -> None:
    client, factory, media = web
    secret = media.parent.parent / "secret.jpg"
    secret.write_bytes(b"secret")
    incident_id = DbIncidentStore(factory).open(
        camera_id=1, kind="person", severity="info", started_at=datetime.now(UTC),
        snapshot_path="data/incidents/../../secret.jpg",
    )
    client.post("/login", data={"username": "guard", "password": PASSWORD})
    assert client.get(f"/incidents/{incident_id}/snapshot.jpg").status_code == 404


def test_review_incident_records_reviewer(web) -> None:
    client, factory, _media = web
    incident_id = DbIncidentStore(factory).open(
        camera_id=1, kind="vehicle", severity="info", started_at=datetime.now(UTC)
    )
    client.post("/login", data={"username": "guard", "password": PASSWORD})
    response = client.post(f"/incidents/{incident_id}/review", data={"note": "delivery van"}, follow_redirects=False)
    assert response.status_code == 303
    with factory() as db:
        incident = db.get(Incident, incident_id)
        assert incident.reviewed_at is not None and incident.review_note == "delivery van"


@pytest.mark.models
def test_real_detector_loads_when_model_present() -> None:
    model = Path(__file__).resolve().parents[1] / "models" / "object_detection_yolox_2022nov.onnx"
    if not model.exists():
        pytest.skip("object model not downloaded")
    detector = detector_module.ObjectDetector(model)
    assert detector.detect(np.zeros((360, 640, 3), np.uint8)) == []


def test_parked_vehicle_does_not_keep_incident_open(tmp_path: Path) -> None:
    store = FakeStore()
    recorder = ClipRecorder(fps=5, pre_seconds=1, max_width=320)
    recorder.push(_frame())
    engine = IncidentEngine(1, store, recorder, media_root=tmp_path, idle_seconds=3, max_seconds=600)
    tracker = ObjectTracker(lost_after_s=3.0)
    parked = _person(100, "vehicle", "car")
    now = 0.0
    while now <= 30.0:
        engine.update(_frame(), tracker.update([parked], now), now)
        now += 0.5
    assert len(store.rows) == 1
    assert "ended_at" in store.rows[1]  # closed once the car was judged stationary
    assert engine.current is None


def test_fast_walker_keeps_one_track_at_low_fps() -> None:
    tracker = ObjectTracker()
    for step in range(6):
        tracker.update([_person(100 + step * 30)], step * 0.5)  # 30 px per step, box 40 px wide
    assert len(tracker.tracks) == 1


@pytest.mark.parametrize("use_ffmpeg", [True, False])
def test_clip_recorder_writes_readable_video(tmp_path: Path, use_ffmpeg: bool) -> None:
    recorder = ClipRecorder(fps=4, pre_seconds=1, max_width=320, use_ffmpeg=use_ffmpeg)
    if use_ffmpeg and recorder.extension != ".mp4":
        pytest.skip("imageio-ffmpeg not installed")
    for i in range(3):
        recorder.push(_frame(i * 30))
    path = recorder.start(tmp_path / "clip")
    assert path is not None and path.suffix == (".mp4" if use_ffmpeg else ".webm")
    for i in range(8):
        recorder.push(_frame(100 + i * 10))
    recorder.stop()
    capture = cv2.VideoCapture(str(path))
    frames = 0
    while capture.read()[0]:
        frames += 1
    capture.release()
    assert frames == 11  # 3 pre-roll + 8 live
