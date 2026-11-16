from datetime import UTC, datetime
from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.auth import authenticate, hash_password
from app.db import Base
from app.models import (
    AttendanceAdjustment, AuditLog, Camera, Embedding, Event, Incident, Person, SurveillanceRule, User,
)
from app.services import deletion, enroll

import numpy as np
from cryptography.fernet import Fernet
from app.config import settings

VECTOR = Fernet(settings.fernet_key.encode()).encrypt(np.eye(128, dtype=np.float32)[0].tobytes())
from app.web.routes.events import safe_back
from test_camera import web_client  # noqa: F401  (shared fixture)


@pytest.fixture
def db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    monkeypatch.setattr(enroll, "_FACES_DIR", tmp_path / "data" / "faces")
    enroll.clear_gallery_cache()
    with sessionmaker(bind=engine, expire_on_commit=False)() as session:
        session.add(Camera(name="Gate", gate="Main", direction="IN", rtsp_url_encrypted="x"))
        session.add(User(username="boss", password_hash=hash_password("long-password-123"), role="admin"))
        session.commit()
        yield session
    enroll.clear_gallery_cache()
    engine.dispose()


def _file(root: Path, relative: str) -> Path:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"x")
    return path


def test_delete_person_keeps_gate_log_without_a_name(db, tmp_path: Path) -> None:
    person = Person(code="E1", name="Rahim")
    db.add(person)
    db.flush()
    thumb = _file(tmp_path, "data/faces/1/1.jpg")
    db.add(Embedding(person_id=person.id, vector_encrypted=VECTOR, thumb_path="faces/1/1.jpg", created_by=1))
    db.add(Event(person_id=person.id, camera_id=1, gate="Main", direction="IN", status="matched"))
    db.add(AttendanceAdjustment(person_id=person.id, day=datetime(2026, 11, 16).date(), note="x"))
    db.commit()

    deletion.delete_person(db, person, 1)

    assert db.query(Person).count() == 0 and db.query(Embedding).count() == 0
    assert db.query(AttendanceAdjustment).count() == 0
    event = db.query(Event).one()
    assert (event.person_id, event.reason) == (None, "person_deleted")
    assert not thumb.exists()
    assert db.query(AuditLog).filter_by(action="person.delete").one().detail.endswith("E1 Rahim")


def test_delete_one_sample_only_touches_that_person(db, tmp_path: Path) -> None:
    db.add_all([Person(code="A", name="A"), Person(code="B", name="B")])
    db.flush()
    db.add(Embedding(person_id=1, vector_encrypted=VECTOR, thumb_path="faces/1/1.jpg", created_by=1))
    db.add(Embedding(person_id=1, vector_encrypted=VECTOR, thumb_path="faces/1/2.jpg", created_by=1))
    db.commit()
    first = _file(tmp_path, "data/faces/1/1.jpg")
    with pytest.raises(deletion.DeleteRefused):
        deletion.delete_sample(db, db.get(Person, 2), 1, 1)  # someone else's sample
    deletion.delete_sample(db, db.get(Person, 1), 1, 1)
    assert [e.id for e in db.query(Embedding)] == [2] and not first.exists()


def test_media_is_only_deleted_inside_its_folder(db, tmp_path: Path) -> None:
    inside = _file(tmp_path, "data/snapshots/2026/a.jpg")
    outside = _file(tmp_path, "important.txt")
    db.add_all([
        Event(camera_id=1, gate="Main", direction="IN", status="unknown", snapshot_path="data/snapshots/2026/a.jpg"),
        Event(camera_id=1, gate="Main", direction="IN", status="unknown", snapshot_path="data/snapshots/../../important.txt"),
    ])
    db.commit()

    assert deletion.delete_events(db, [1, 2, 2], 1, root=tmp_path) == 2

    assert not inside.exists() and outside.exists()
    assert db.query(Event).count() == 0
    with pytest.raises(deletion.DeleteRefused):
        deletion.delete_events(db, [], 1, root=tmp_path)


def test_shared_clips_and_running_incidents(db, tmp_path: Path) -> None:
    clip = _file(tmp_path, "data/incidents/c.mp4")
    ended = datetime(2026, 11, 16, tzinfo=UTC)
    db.add_all([
        Incident(camera_id=1, kind="person", ended_at=ended, clip_path="data/incidents/c.mp4"),
        Incident(camera_id=1, kind="intrusion", ended_at=ended, clip_path="data/incidents/c.mp4"),
        Incident(camera_id=1, kind="person", ended_at=None),
    ])
    db.commit()

    first = deletion.delete_incidents(db, [1, 3], 1, root=tmp_path)
    assert (first.deleted, first.skipped_running) == (1, 1)
    assert clip.exists()  # incident 2 still uses it
    deletion.delete_incidents(db, [2], 1, root=tmp_path)
    assert not clip.exists()
    assert [i.id for i in db.query(Incident)] == [3]


def test_delete_user_rules(db) -> None:
    boss = db.get(User, 1)
    guard = User(username="guard", password_hash=hash_password("long-password-123"), role="security")
    db.add(guard)
    db.commit()

    with pytest.raises(deletion.DeleteRefused, match="user_cannot_delete_self"):
        deletion.delete_user(db, boss, boss)
    with pytest.raises(deletion.DeleteRefused, match="user_last_admin"):
        deletion.delete_user(db, boss, guard)

    deletion.delete_user(db, guard, boss)
    assert guard.username == "guard~deleted2" and not guard.active and guard.deleted_at is not None
    assert authenticate(db, "guard", "long-password-123") is None
    db.add(User(username="guard", password_hash=hash_password("x" * 12), role="security"))
    db.commit()  # the name can be reused


@pytest.mark.parametrize("value, ok", [
    ("/events?page=2&lang=bn", True), ("/incidents", True), ("//evil.example/x", False),
    ("https://evil.example", False), ("/events\\..\\x", False), ("/system", False),
])
def test_safe_back_never_leaves_the_app(value: str, ok: bool) -> None:
    assert (safe_back(value, "/default") == value) is ok


def test_delete_routes(web_client, monkeypatch: pytest.MonkeyPatch) -> None:  # noqa: F811
    client, test_session = web_client
    with test_session() as session:
        session.add(Person(code="E1", name="Rahim"))
        session.add(SurveillanceRule(camera_id=1, name="Zone", kind="intrusion", points="[[0,0],[1,0],[1,1]]"))
        session.add(Event(camera_id=1, gate="North", direction="IN", status="unknown"))
        session.add(Incident(camera_id=1, kind="person", ended_at=datetime.now(UTC)))
        session.commit()

    client.post("/login", data={"username": "security-camera", "password": "test-password-123"})
    assert client.post("/events/delete", data={"ids": ["1"]}).status_code == 403
    assert client.post("/incidents/delete", data={"ids": ["1"]}).status_code == 403
    assert "bulk-delete" not in client.get("/events").text
    client.post("/logout")

    client.post("/login", data={"username": "admin-camera", "password": "test-password-123"})
    assert "bulk-delete" in client.get("/events").text
    wrong = client.post("/people/1/delete", data={"confirm_code": "E2"}, follow_redirects=False)
    assert "error=delete_confirm_code" in wrong.headers["location"]
    client.post("/people/1/delete", data={"confirm_code": "E1"})
    moved = client.post("/events/delete", data={"ids": ["1"], "back": "https://evil.example"}, follow_redirects=False)
    assert moved.headers["location"].startswith("/events")
    client.post("/incidents/delete", data={"ids": ["1"], "back": "/incidents?page=1"})

    # The confirmation must survive HTML quoting, or the browser would delete without asking.
    import html
    import json
    import re

    page = client.get("/cameras").text
    handler = re.search(r"onsubmit='return confirm\(([^']*)\)'", page)
    assert handler is not None
    assert json.loads(html.unescape(handler.group(1))).startswith('Delete camera "Front"?')

    monkeypatch.setattr("app.web.routes.cameras._sync_camera", lambda _camera: None)
    client.post("/cameras/1/delete")
    assert "Front" not in client.get("/cameras").text
    assert client.get("/cameras/1/edit").status_code == 404
    assert client.post("/cameras/1/delete").status_code == 404

    users = client.get("/users").text
    refused = client.post("/users/1/delete")  # admin-camera is user 1: cannot delete self
    assert refused.status_code == 400
    client.post("/users/2/delete")
    assert "security-camera" not in client.get("/users").text and "security-camera" in users

    with test_session() as session:
        assert session.query(Person).count() == 0
        assert session.query(Event).count() == 0 and session.query(Incident).count() == 0
        assert session.query(SurveillanceRule).count() == 0
        camera = session.get(Camera, 1)
        assert camera.deleted_at is not None and not camera.enabled
        actions = {log.action for log in session.query(AuditLog)}
        assert {"person.delete", "event.delete", "incident.delete", "camera.delete", "user.delete"} <= actions
