import time
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

import numpy as np
import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.config import settings
from app.db import Base
from app.models import Camera, Event, Person
from app.pipeline.debounce import EventDebouncer
from app.pipeline.tracker import DetectionObservation, IoUTracker
from app.pipeline.worker import EventRecord, EventWriter, PipelineWorker
from app.vision.detector import Face
from app.vision.matcher import Gallery


def _unit(index: int) -> np.ndarray:
    vector = np.zeros(128, dtype=np.float32)
    vector[index] = 1.0
    return vector


def _face(box: tuple[float, float, float, float] = (10, 10, 60, 60)) -> Face:
    return Face(
        box=box,
        score=0.99,
        landmarks5=np.asarray([[20, 25], [55, 25], [37, 40], [24, 55], [51, 55]], dtype=np.float32),
    )


class EventSink:
    def __init__(self) -> None:
        self.events = []

    def submit(self, event) -> None:
        self.events.append(event)


class FakeDetector:
    def __init__(self, faces: list[Face] | None = None, error: bool = False) -> None:
        self.faces = faces if faces is not None else [_face()]
        self.error = error
        self.calls = 0

    def detect(self, _frame: np.ndarray) -> list[Face]:
        self.calls += 1
        if self.error:
            raise RuntimeError("synthetic detector failure")
        return self.faces


class FakeEmbedder:
    def __init__(self, selector: Callable[[np.ndarray], int] | None = None) -> None:
        self.selector = selector or (lambda _frame: 0)

    def align_and_embed(self, frame: np.ndarray, _face: Face) -> np.ndarray:
        return _unit(self.selector(frame))


class FakeQuality:
    def __init__(self, accepted: bool = True) -> None:
        self.accepted = accepted

    def check(self, _frame: np.ndarray, _face: Face) -> tuple[bool, list[str]]:
        return self.accepted, [] if self.accepted else ["blurred"]


def _worker(
    *,
    person_gallery: Gallery | None = None,
    writer: EventSink | None = None,
    detector: FakeDetector | None = None,
    embedder: FakeEmbedder | None = None,
    quality: FakeQuality | None = None,
    **kwargs,
) -> PipelineWorker:
    return PipelineWorker(
        camera_id=11,
        gate="North Gate",
        direction="OUT",
        gallery=person_gallery or Gallery([], np.empty((0, 128), dtype=np.float32)),
        event_writer=writer or EventSink(),
        detector=detector or FakeDetector(),
        embedder=embedder or FakeEmbedder(),
        quality=quality or FakeQuality(),
        fps=5,
        consensus_n=3,
        consensus_window=5,
        snapshot_dir=kwargs.pop("snapshot_dir", None),
        **kwargs,
    )


def test_matched_event_waits_for_consensus_and_uses_camera_direction(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    monkeypatch.setattr(settings, "save_match_snapshots", False)
    sink = EventSink()
    gallery = Gallery([42], _unit(0)[None, :])
    worker = _worker(person_gallery=gallery, writer=sink, snapshot_dir=tmp_path)
    frame = np.zeros((100, 100, 3), dtype=np.uint8)

    worker.process_frame(frame, now=1)
    worker.process_frame(frame, now=2)
    assert sink.events == []

    worker.process_frame(frame, now=3)

    assert len(sink.events) == 1
    event = sink.events[0]
    assert event.person_id == 42
    assert event.status == "matched"
    assert event.gate == "North Gate"
    assert event.direction == "OUT"
    assert event.score == pytest.approx(1.0)
    assert event.snapshot_path is None


def test_matched_event_debounce_uses_person_gate_and_direction() -> None:
    debounce = EventDebouncer(debounce_s=45, unknown_debounce_s=60)

    assert debounce.allow_matched(1, "Gate A", "IN", 10)
    assert not debounce.allow_matched(1, "Gate A", "IN", 20)
    assert debounce.allow_matched(1, "Gate A", "OUT", 20)
    assert debounce.allow_matched(1, "Gate B", "IN", 20)
    assert debounce.allow_matched(1, "Gate A", "IN", 55)


def test_unknown_faces_are_grouped_and_debounced(tmp_path) -> None:
    sink = EventSink()
    worker = _worker(writer=sink, snapshot_dir=tmp_path)
    frame = np.zeros((100, 100, 3), dtype=np.uint8)

    worker.process_frame(frame, now=10)
    worker.process_frame(frame, now=11)
    worker.process_frame(frame, now=12)

    assert len(sink.events) == 1
    assert sink.events[0].status == "unknown"
    assert sink.events[0].person_id is None
    assert sink.events[0].snapshot_path is not None
    assert list(tmp_path.rglob("*.jpg"))


def test_unknown_debouncer_separates_dissimilar_groups() -> None:
    debounce = EventDebouncer(unknown_debounce_s=60, unknown_similarity=0.75)
    assert debounce.allow_unknown(_unit(0), now=1)
    assert not debounce.allow_unknown(_unit(0), now=2)
    assert debounce.allow_unknown(_unit(1), now=2)


def test_low_quality_event_is_reported_once_per_track() -> None:
    sink = EventSink()
    worker = _worker(writer=sink, quality=FakeQuality(False))
    frame = np.zeros((100, 100, 3), dtype=np.uint8)

    worker.process_frame(frame, now=1)
    worker.process_frame(frame, now=2)

    assert len(sink.events) == 1
    assert sink.events[0].status == "low_quality"


def test_failure_in_one_worker_does_not_stop_another(tmp_path: Path) -> None:
    sink = EventSink()
    gallery = Gallery([7], _unit(0)[None, :])
    failing = PipelineWorker(
        camera_id=1,
        gate="A",
        direction="IN",
        gallery=gallery,
        event_writer=sink,
        detector=FakeDetector(error=True),
        embedder=FakeEmbedder(),
        quality=FakeQuality(),
        frame_provider=lambda _camera_id: np.zeros((100, 100, 3), dtype=np.uint8),
        fps=50,
    )
    good_detector = FakeDetector()
    working = PipelineWorker(
        camera_id=2,
        gate="B",
        direction="OUT",
        gallery=gallery,
        event_writer=sink,
        detector=good_detector,
        embedder=FakeEmbedder(),
        quality=FakeQuality(),
        frame_provider=lambda _camera_id: np.zeros((100, 100, 3), dtype=np.uint8),
        fps=50,
        snapshot_dir=tmp_path,
    )
    failing.start()
    working.start()
    try:
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline and not any(
            event.camera_id == 2 for event in sink.events
        ):
            time.sleep(0.01)
        assert failing._thread.is_alive()
        assert working._thread.is_alive()
        assert good_detector.calls >= 3
        assert any(event.camera_id == 2 and event.direction == "OUT" for event in sink.events)
    finally:
        failing.stop()
        working.stop()


def test_event_writer_persists_through_one_writer_thread() -> None:
    test_engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(test_engine)
    session_factory = sessionmaker(bind=test_engine, expire_on_commit=False)
    with session_factory() as db:
        db.add(
            Camera(
                id=11,
                name="Front",
                gate="North Gate",
                direction="OUT",
                rtsp_url_encrypted="encrypted",
            )
        )
        db.add(Person(id=42, code="P-42", name="Test person"))
        db.commit()
    writer = EventWriter(session_factory)
    writer.start()
    writer.submit(
        EventRecord(
            person_id=42,
            camera_id=11,
            gate="North Gate",
            direction="OUT",
            at=datetime.now(UTC),
            score=0.9,
            status="matched",
            snapshot_path=None,
        )
    )
    writer._queue.join()
    writer.stop()
    with session_factory() as db:
        saved = db.scalar(select(Event))
        assert saved is not None
        assert saved.person_id == 42
        assert saved.direction == "OUT"
        assert saved.status == "matched"
    test_engine.dispose()
