import asyncio
import json
import threading
from datetime import UTC, datetime

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.camera.reader import ReaderState
from app.db import Base
from app.models import AuditLog, Camera, Event, Incident
from app.notify import sources
from app.notify.bus import NotificationBus
from app.pipeline.worker import EventRecord, EventWriter
from test_camera import web_client  # noqa: F401  (shared fixture)


@pytest.fixture
def fresh_bus(monkeypatch: pytest.MonkeyPatch) -> NotificationBus:
    bus = NotificationBus(history=5)
    monkeypatch.setattr("app.notify.bus.bus", bus)
    monkeypatch.setattr("app.web.routes.notify.bus", bus)
    return bus


def test_bus_translates_keeps_history_and_ids(fresh_bus: NotificationBus) -> None:
    first = fresh_bus.publish(
        "rule", "critical", "notify_rule_title", "notify_rule_body", rule="Store", camera="Gate 1",
        image="/incidents/7/snapshot.jpg", ack={"type": "incident", "id": 7},
    )
    assert first.title == {"en": "Rule broken: Store", "bn": "নিয়ম ভেঙেছে: Store"}
    assert json.loads(first.to_json())["ack"] == {"type": "incident", "id": 7}

    for _ in range(6):
        fresh_bus.publish("camera_online", "info", "notify_camera_online_title", "notify_camera_online_body", camera="C")
    assert fresh_bus.last_id == 7
    assert [item.id for item in fresh_bus.since(0)] == [3, 4, 5, 6, 7]  # history is bounded
    assert fresh_bus.since(7) == []


def test_unknown_face_text_uses_translated_direction(fresh_bus: NotificationBus) -> None:
    sources.notify_unknown_face(12, "Main", "IN")
    (item,) = fresh_bus.since(0)
    from app.i18n import TRANSLATIONS

    assert item.body == {
        "en": f"Main gate, {TRANSLATIONS['en']['in']}",
        "bn": f"Main গেট, {TRANSLATIONS['bn']['in']}",
    }
    assert TRANSLATIONS["bn"]["in"] != "IN"  # really translated, not the raw code
    assert item.ack == {"type": "event", "id": 12}
    assert item.image == "/events/12/snapshot"


def test_info_rules_do_not_notify(fresh_bus: NotificationBus, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sources, "camera_name", lambda _id: "Gate")
    sources.notify_rule_hit(1, 1, "Record only", "info")
    sources.notify_rule_hit(2, 1, "Store room", "warning")
    assert [item.ack["id"] for item in fresh_bus.since(0)] == [2]


def test_stream_replays_missed_and_delivers_from_other_threads(fresh_bus: NotificationBus) -> None:
    from app.web.routes.notify import _stream

    fresh_bus.publish("camera_online", "info", "notify_camera_online_title", "notify_camera_online_body", camera="A")
    fresh_bus.publish("camera_online", "info", "notify_camera_online_title", "notify_camera_online_body", camera="B")

    class FakeRequest:
        def __init__(self) -> None:
            self.calls = 0

        async def is_disconnected(self) -> bool:
            self.calls += 1
            return self.calls > 2

    async def collect() -> list[bytes]:
        frames = []
        stream = _stream(FakeRequest(), last_id=1)
        frames.append(await stream.__anext__())  # retry hint
        frames.append(await stream.__anext__())  # replay of id 2 (id 1 already seen)
        threading.Thread(
            target=fresh_bus.publish,
            args=("camera_offline", "warning", "notify_camera_offline_title", "notify_camera_offline_body"),
            kwargs={"camera": "C"},
        ).start()
        frames.append(await asyncio.wait_for(stream.__anext__(), timeout=5))
        await stream.aclose()
        return frames

    frames = asyncio.run(collect())

    assert frames[0].startswith(b"retry:")
    assert frames[1].startswith(b"id: 2\nevent: notify\n")
    assert frames[2].startswith(b"id: 3\n")
    assert '"camera_offline"' in frames[2].decode()
    assert fresh_bus.subscriber_count == 0  # unsubscribed when the page went away


def test_camera_watcher_reports_long_outage_once_and_recovery(fresh_bus: NotificationBus, monkeypatch: pytest.MonkeyPatch) -> None:
    states = {}
    monkeypatch.setattr(sources.manager, "get_state", lambda camera_id: states[camera_id])
    monkeypatch.setattr(sources, "camera_name", lambda _id: "Gate cam")
    watcher = sources.CameraWatcher(lambda: [1], offline_after=30)
    now = datetime.now(UTC)

    states[1] = ReaderState("offline", 0.0, now, None, 3, frame_age_seconds=12.0)
    watcher.check()  # a short reconnect is normal
    assert fresh_bus.since(0) == []

    states[1] = ReaderState("offline", 0.0, now, None, 4, frame_age_seconds=45.0)
    watcher.check()
    watcher.check()
    states[1] = ReaderState("online", 12.0, now, None, 5, frame_age_seconds=0.1)
    watcher.check()
    watcher.check()

    assert [(item.kind, item.body["en"]) for item in fresh_bus.since(0)] == [
        ("camera_offline", '"Gate cam" has stopped sending video.'),
        ("camera_online", '"Gate cam" is sending video again.'),
    ]


def test_event_writer_notifies_unknown_faces_after_saving(fresh_bus: NotificationBus) -> None:
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    with factory() as db:
        db.add(Camera(name="C", gate="Main", direction="IN", rtsp_url_encrypted="x"))
        db.commit()
    writer = EventWriter(factory)
    writer.start()
    for status in ("unknown", "matched", "low_quality"):
        writer.submit(EventRecord(None, 1, "Main", "IN", datetime.now(UTC), 0.2, status, None))
    writer.stop()

    (item,) = fresh_bus.since(0)
    with factory() as db:
        unknown = db.query(Event).filter_by(status="unknown").one()
    assert item.ack == {"type": "event", "id": unknown.id}
    engine.dispose()


def test_ack_marks_items_reviewed_and_is_logged(web_client) -> None:  # noqa: F811
    client, test_session = web_client
    with test_session() as db:
        db.add(Incident(camera_id=1, kind="intrusion", severity="critical"))
        db.add(Event(camera_id=1, gate="North", direction="IN", status="unknown"))
        db.add(Event(camera_id=1, gate="North", direction="IN", status="matched"))
        db.commit()

    assert client.post("/notify/ack", json={"type": "incident", "id": 1}).status_code == 401
    assert client.get("/notify/stream").status_code == 401
    client.post("/login", data={"username": "security-camera", "password": "test-password-123"})

    page = client.get("/?lang=bn")
    assert 'id="notify-root"' in page.text and "/static/notify.js" in page.text
    # The browser reads this attribute with JSON.parse; a quoting mistake would silently disable alerts.
    import html
    import re

    raw = re.search(r"data-text='([^']*)'", page.text).group(1)
    texts = json.loads(html.unescape(raw))
    assert texts["ack"] == "দেখেছি" and set(texts) >= {"open", "close", "more", "enableSound"}

    assert client.post("/notify/ack", json={"type": "incident", "id": 1}).json() == {"ok": True}
    assert client.post("/notify/ack", json={"type": "incident", "id": 1}).status_code == 200  # twice is harmless
    assert client.post("/notify/ack", json={"type": "event", "id": 1}).status_code == 200
    assert client.post("/notify/ack", json={"type": "event", "id": 2}).status_code == 404  # not an unknown face
    assert client.post("/notify/ack", json={"type": "other", "id": 1}).status_code == 422

    with test_session() as db:
        assert db.get(Incident, 1).reviewed_at is not None
        assert db.get(Event, 1).reviewed_at is not None
        assert db.query(AuditLog).filter(AuditLog.detail.contains("via=live alarm")).count() == 2


def test_login_page_has_no_notification_script(web_client) -> None:  # noqa: F811
    client, _ = web_client
    assert "notify.js" not in client.get("/login").text
