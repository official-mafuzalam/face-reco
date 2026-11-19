from collections.abc import AsyncIterator, Generator
from contextlib import asynccontextmanager
from unittest.mock import MagicMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.auth import hash_password
from app.config import settings
from app.db import Base, get_db
from app.main import app
from app.models import PushSubscription, User
from app.notify.push import (
    is_in_quiet_hours,
    send_push_notification,
    subscribe,
    unsubscribe,
)

PASSWORD = "test-push-password"


@pytest.fixture
def push_env(monkeypatch: pytest.MonkeyPatch):
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)

    def override_get_db() -> Generator[Session, None, None]:
        with factory() as db:
            yield db

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        yield

    monkeypatch.setattr(app.router, "lifespan_context", lifespan)
    monkeypatch.setattr(settings, "push_enabled", True)
    monkeypatch.setattr(settings, "vapid_private_key", "test-fake-vapid-key")
    monkeypatch.setattr(settings, "vapid_public_key", "test-fake-public-key")

    app.dependency_overrides[get_db] = override_get_db
    with factory() as db:
        user = User(username="admin", password_hash=hash_password(PASSWORD), role="admin")
        db.add(user)
        db.commit()
    with TestClient(app) as client:
        yield client, factory
    app.dependency_overrides.clear()
    engine.dispose()


def test_is_in_quiet_hours():
    # Daytime range: 13:00 to 15:00
    assert is_in_quiet_hours("13:00", "15:00", "14:00") is True
    assert is_in_quiet_hours("13:00", "15:00", "12:59") is False
    assert is_in_quiet_hours("13:00", "15:00", "15:00") is False

    # Overnight range: 22:00 to 06:00
    assert is_in_quiet_hours("22:00", "06:00", "22:00") is True
    assert is_in_quiet_hours("22:00", "06:00", "23:59") is True
    assert is_in_quiet_hours("22:00", "06:00", "00:00") is True
    assert is_in_quiet_hours("22:00", "06:00", "05:59") is True
    assert is_in_quiet_hours("22:00", "06:00", "06:00") is False
    assert is_in_quiet_hours("22:00", "06:00", "12:00") is False

    # Empty / invalid
    assert is_in_quiet_hours("", "", "12:00") is False
    assert is_in_quiet_hours("10:00", "10:00", "10:00") is False


def test_subscribe_and_unsubscribe_helpers(push_env):
    _, factory = push_env
    endpoint = "https://fcm.googleapis.com/fcm/send/test-sub-1"

    with factory() as db:
        user = db.query(User).filter(User.username == "admin").one()
        sub = subscribe(db, user.id, endpoint, "p256dh-key", "auth-token")
        assert sub.id is not None
        assert sub.endpoint == endpoint
        assert sub.quiet_start == "22:00"

        # Update existing
        sub2 = subscribe(db, user.id, endpoint, "new-p256dh", "new-auth", quiet_start="23:00")
        assert sub2.id == sub.id
        assert sub2.quiet_start == "23:00"

        # Unsubscribe
        ok = unsubscribe(db, endpoint)
        assert ok is True
        assert db.scalar(select(PushSubscription).where(PushSubscription.endpoint == endpoint)) is None


def test_push_routes(push_env):
    client, factory = push_env
    # Login
    client.post("/login", data={"username": "admin", "password": PASSWORD})

    # VAPID public key endpoint
    res = client.get("/push/vapid-public-key")
    assert res.status_code == 200
    assert res.json()["public_key"] == "test-fake-public-key"
    assert res.json()["enabled"] is True

    # Subscribe endpoint
    sub_payload = {
        "endpoint": "https://push.example.com/device1",
        "keys": {"p256dh": "key1", "auth": "auth1"},
        "quiet_start": "23:00",
        "quiet_end": "07:00",
        "min_severity": "warning",
    }
    sub_res = client.post("/push/subscribe", json=sub_payload)
    assert sub_res.status_code == 200
    assert sub_res.json() == {"status": "ok"}

    # Update preferences
    pref_res = client.post("/push/preferences", json={"quiet_start": "21:00", "quiet_end": "08:00", "min_severity": "critical"})
    assert pref_res.status_code == 200

    with factory() as db:
        sub = db.scalar(select(PushSubscription).where(PushSubscription.endpoint == "https://push.example.com/device1"))
        assert sub.min_severity == "critical"
        assert sub.quiet_start == "21:00"

    # Test push trigger
    with patch("app.notify.push.webpush") as mock_webpush:
        mock_webpush.return_value = MagicMock(status_code=201)
        test_res = client.post("/push/test")
        assert test_res.status_code == 200
        assert test_res.json()["delivered_targets"] == 1

    # Unsubscribe endpoint
    unsub_res = client.post("/push/unsubscribe", json={"endpoint": "https://push.example.com/device1"})
    assert unsub_res.status_code == 200


def test_send_push_notification_filtering(push_env, monkeypatch: pytest.MonkeyPatch):
    _, factory = push_env
    with factory() as db:
        user = db.query(User).filter(User.username == "admin").one()
        # sub1: warning min severity, 22:00-06:00 quiet hours
        subscribe(db, user.id, "https://push.example.com/sub1", "k1", "a1", quiet_start="22:00", quiet_end="06:00", min_severity="warning")
        # sub2: critical min severity, no quiet hours
        subscribe(db, user.id, "https://push.example.com/sub2", "k2", "a2", quiet_start="00:00", quiet_end="00:00", min_severity="critical")

    # In daytime: 14:00
    monkeypatch.setattr(settings, "tz", "UTC")
    with patch("app.notify.push.webpush") as mock_webpush:
        # Info alert -> none match (sub1 wants warning+, sub2 wants critical)
        with factory() as db:
            targets = send_push_notification(db, "Info", "Info msg", severity="info")
            assert targets == 0

        # Warning alert -> sub1 matches
        with factory() as db:
            targets = send_push_notification(db, "Warning", "Warning msg", severity="warning")
            assert targets == 1

        # Critical alert -> both match
        with factory() as db:
            targets = send_push_notification(db, "Critical", "Critical msg", severity="critical")
            assert targets == 2
