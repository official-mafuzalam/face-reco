from datetime import UTC, datetime
from pathlib import Path

import cv2
import numpy as np
import pytest
from cryptography.fernet import Fernet
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.config import settings
from app.db import Base
from app.models import AuditLog, Camera, Embedding, Event, Person
from app.notify.bus import NotificationBus
from app.pipeline.worker import EventRecord, EventWriter
from app.services import enroll
from app.services import face_search
from app.vision.detector import Face
from test_camera import web_client  # noqa: F401  (shared fixture)

_fernet = Fernet(settings.fernet_key.encode())


def _unit(index: int, other: int | None = None, mix: float = 0.0) -> np.ndarray:
    vector = np.zeros(128, dtype=np.float32)
    vector[index] = 1.0
    if other is not None:
        vector[index] = np.sqrt(1 - mix**2)
        vector[other] = mix
    return vector


def _face(x: float) -> Face:
    return Face(
        box=(x, 20, 100, 100),
        score=0.99,
        landmarks5=np.asarray([[x + 20, 45], [x + 80, 45], [x + 50, 65], [x + 28, 90], [x + 72, 90]], dtype=np.float32),
    )


@pytest.fixture
def db(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    monkeypatch.setattr(enroll, "_FACES_DIR", tmp_path / "faces")
    enroll.clear_gallery_cache()
    with factory() as session:
        session.add(Camera(name="Gate", gate="Main", direction="IN", rtsp_url_encrypted="x"))
        session.commit()
        yield session
    enroll.clear_gallery_cache()
    engine.dispose()


def _unknown(db: Session, vector: np.ndarray, snapshot: str | None = None, minute: int = 0) -> Event:
    event = Event(
        camera_id=1, gate="Main", direction="IN", status="unknown",
        at=datetime(2026, 11, 16, 10, minute, tzinfo=UTC), snapshot_path=snapshot,
        embedding_encrypted=_fernet.encrypt(vector.astype(np.float32).tobytes()),
    )
    db.add(event)
    db.commit()
    return event


def test_similar_events_and_people_rank_by_score(db: Session) -> None:
    query = _unit(0)
    close = _unknown(db, _unit(0, 1, 0.3), minute=1)
    _unknown(db, _unit(5), minute=2)  # a different face
    exact = _unknown(db, _unit(0), minute=3)
    person = Person(code="E1", name="Mafuz")
    db.add(person)
    db.flush()
    db.add(Embedding(person_id=person.id, created_by=1, vector_encrypted=_fernet.encrypt(_unit(0, 2, 0.5).tobytes())))
    db.commit()

    found = face_search.similar_events(db, query, exclude_id=exact.id)
    assert [item.event.id for item in found] == [close.id]
    assert found[0].score == pytest.approx(np.sqrt(1 - 0.09), abs=1e-4)
    (match,) = face_search.similar_people(db, query)
    assert match.person.code == "E1"
    assert face_search.decrypt_vector(b"garbage") is None


def test_enroll_from_event_picks_the_matching_face_in_a_crowded_snapshot(db: Session, tmp_path: Path) -> None:
    snapshot = tmp_path / "data" / "snapshots" / "s.jpg"
    snapshot.parent.mkdir(parents=True)
    cv2.imwrite(str(snapshot), np.full((200, 400, 3), 120, dtype=np.uint8))
    event = _unknown(db, _unit(3), snapshot="data/snapshots/s.jpg")
    person = Person(code="V1", name="Visitor")
    db.add(person)
    db.commit()

    class TwoFaces:
        def detect(self, _image):
            return [_face(10), _face(250)]

    class ByPosition:
        def align_and_embed(self, _image, face):
            return _unit(7) if face.box[0] < 100 else _unit(3, 4, 0.1)  # the second face is our person

    result = face_search.enroll_from_event(db, event, person, 1, detector=TwoFaces(), embedder=ByPosition(), root=tmp_path)

    assert result.accepted
    sample = db.query(Embedding).filter_by(person_id=person.id).one()
    stored = np.frombuffer(_fernet.decrypt(sample.vector_encrypted), dtype=np.float32)
    assert float(stored @ _unit(3)) == pytest.approx(1.0)  # the gate embedding, not the re-detected one
    assert (tmp_path / "faces" / str(person.id)).is_dir()

    class Stranger:
        def align_and_embed(self, _image, _face):
            return _unit(9)

    other = _unknown(db, _unit(3), snapshot="data/snapshots/s.jpg", minute=5)
    refused = face_search.enroll_from_event(db, other, Person(code="X", name="X"), 1, detector=TwoFaces(), embedder=Stranger(), root=tmp_path)
    assert refused.reasons == ("face_source_missing",)


def test_snapshot_paths_cannot_escape_the_snapshot_folder(tmp_path: Path) -> None:
    (tmp_path / "secret.jpg").write_bytes(b"x")
    event = Event(camera_id=1, gate="G", direction="IN", status="unknown", snapshot_path="data/snapshots/../../secret.jpg")
    assert face_search.snapshot_image(event, tmp_path) is None


def test_writer_keeps_unknown_embeddings_and_alarms_for_blacklist(db: Session, monkeypatch: pytest.MonkeyPatch) -> None:
    bus = NotificationBus()
    monkeypatch.setattr("app.notify.bus.bus", bus)
    engine = db.get_bind()
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    db.add_all([Person(code="B1", name="Thief", category="blacklist"), Person(code="V1", name="Guest", category="vip"),
                Person(code="E1", name="Worker")])
    db.commit()
    writer = EventWriter(factory)
    writer.start()
    now = datetime.now(UTC)
    writer.submit(EventRecord(None, 1, "Main", "IN", now, 0.2, "unknown", "data/snapshots/u.jpg", embedding=_unit(4)))
    for person_id in (1, 2, 3):
        writer.submit(EventRecord(person_id, 1, "Main", "IN", now, 0.9, "matched", None))
    writer.stop()

    unknown = db.query(Event).filter_by(status="unknown").one()
    assert face_search.decrypt_vector(unknown.embedding_encrypted) @ _unit(4) == pytest.approx(1.0)
    assert db.query(Event).filter(Event.status == "matched", Event.embedding_encrypted.is_not(None)).count() == 0
    kinds = [(item.kind, item.severity, item.image) for item in bus.since(0)]
    assert kinds == [
        ("unknown_face", "warning", f"/events/{unknown.id}/snapshot"),
        ("watchlist_blacklist", "critical", None),  # no snapshot saved -> no broken image
        ("watchlist_vip", "info", None),
    ]
    assert bus.since(0)[1].title["en"] == "BLACKLISTED person at the gate: Thief"


def test_category_and_face_pages(web_client, monkeypatch: pytest.MonkeyPatch) -> None:  # noqa: F811
    client, test_session = web_client
    with test_session() as session:
        session.add(Person(code="E9", name="Nine"))
        session.add(
            Event(camera_id=1, gate="North", direction="IN", status="unknown",
                  embedding_encrypted=_fernet.encrypt(_unit(0).tobytes()))
        )
        session.commit()

    client.post("/login", data={"username": "security-camera", "password": "test-password-123"})
    assert client.post("/people/1/category", data={"category": "vip"}).status_code == 403
    page = client.get("/faces/similar/1?lang=bn")
    assert page.status_code == 200
    assert "/faces/enroll/1" not in page.text  # guards may search but not enrol
    assert client.get("/faces/similar/999").status_code == 404
    assert client.get("/faces/search").status_code == 200
    client.post("/logout")

    client.post("/login", data={"username": "admin-camera", "password": "test-password-123"})
    assert client.post("/people/1/category", data={"category": "nope"}).status_code == 422
    assert client.post("/people/1/category", data={"category": "blacklist"}, follow_redirects=False).status_code == 303
    created = client.post("/people/new", data={"code": "V2", "name": "Visitor", "category": "visitor"}, follow_redirects=False)
    assert created.status_code == 303
    with test_session() as session:
        assert session.get(Person, 1).category == "blacklist"
        assert session.query(Person).filter_by(code="V2").one().category == "visitor"
        assert session.query(AuditLog).filter_by(action="person.category").count() == 1
    people_page = client.get("/people?lang=bn")
    assert "tag-category-blacklist" in people_page.text

    admin_page = client.get("/faces/similar/1")
    assert "/faces/enroll/1" in admin_page.text
    # The snapshot is missing in this test DB, so enrolment must refuse cleanly and change nothing.
    refused = client.post("/faces/enroll/1", data={"mode": "new", "code": "N1", "name": "New"})
    assert refused.status_code == 200
    with test_session() as session:
        assert session.query(Person).filter_by(code="N1").count() == 0
