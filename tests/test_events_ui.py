from collections.abc import Generator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from typing import AsyncIterator

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.auth import hash_password
from app.db import Base, get_db
from app.main import app
from app.models import AuditLog, Camera, Event, Person, User
from app.web.routes.events import _local_zone


@pytest.fixture
def ui_client(monkeypatch: pytest.MonkeyPatch) -> Generator[tuple[TestClient, sessionmaker], None, None]:
    test_engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(test_engine)
    test_session = sessionmaker(bind=test_engine, autoflush=False, expire_on_commit=False)

    def override_get_db() -> Generator[Session, None, None]:
        with test_session() as db:
            yield db

    @asynccontextmanager
    async def test_lifespan(_app: FastAPI) -> AsyncIterator[None]:
        yield

    monkeypatch.setattr(app.router, "lifespan_context", test_lifespan)
    app.dependency_overrides[get_db] = override_get_db
    with test_session() as db:
        db.add_all(
            [
                User(username="admin", password_hash=hash_password("admin-password"), role="admin"),
                User(username="security", password_hash=hash_password("security-password"), role="security"),
            ]
        )
        db.commit()
        camera = Camera(
            name="Main",
            gate="Gate A",
            direction="IN",
            rtsp_url_encrypted="encrypted",
            enabled=False,
        )
        person = Person(code="P-1", name="Test Person")
        db.add_all([camera, person])
        db.flush()
        now = datetime.now(UTC)
        for index in range(55):
            db.add(
                Event(
                    camera_id=camera.id,
                    person_id=None if index < 2 else person.id,
                    gate="Gate B" if index == 0 else "Gate A",
                    direction="IN" if index % 2 == 0 else "OUT",
                    at=now - timedelta(minutes=index),
                    score=0.9 if index >= 2 else None,
                    status="unknown" if index < 2 else "matched",
                )
            )
        db.commit()

    with TestClient(app) as client:
        yield client, test_session

    app.dependency_overrides.clear()
    test_engine.dispose()


def _login(client: TestClient, username: str = "admin", password: str = "admin-password") -> None:
    response = client.post(
        "/login",
        data={"username": username, "password": password},
        follow_redirects=False,
    )
    assert response.status_code == 303


def test_role_access_for_events_and_admin_pages(ui_client) -> None:
    client, _ = ui_client
    assert client.get("/events").status_code == 401

    _login(client, "security", "security-password")
    dashboard = client.get("/")
    assert dashboard.status_code == 200
    assert "dashboard-data" in dashboard.text
    assert "Today&#39;s IN" in dashboard.text
    assert "Last 20 events" in dashboard.text
    assert client.get("/events").status_code == 200
    assert client.get("/alerts").status_code == 200
    assert client.get("/people").status_code == 403
    client.cookies.clear()
    assert client.get("/", follow_redirects=False).status_code == 303


def test_event_filters_and_fifty_row_pagination(ui_client) -> None:
    client, _ = ui_client
    _login(client)
    today = datetime.now(_local_zone()).date().isoformat()

    filtered = client.get(
        "/events",
        params={"start": today, "end": today, "gate": "Gate B", "status": "unknown"},
    )
    assert filtered.status_code == 200
    assert "<td>Gate B</td>" in filtered.text
    assert "<td>Gate A</td>" not in filtered.text

    first_page = client.get("/events", params={"start": today, "end": today})
    second_page = client.get("/events", params={"start": today, "end": today, "page": 2})
    assert first_page.status_code == 200
    assert first_page.text.count("<tr>") == 51
    assert second_page.status_code == 200
    assert second_page.text.count("<tr>") == 6


def test_csv_is_bom_encoded_and_uses_same_filters(ui_client) -> None:
    client, _ = ui_client
    _login(client)
    today = datetime.now(_local_zone()).date().isoformat()
    response = client.get(
        "/events/export.csv",
        params={"start": today, "end": today, "status": "unknown", "gate": "Gate B"},
    )

    assert response.status_code == 200
    assert response.content.startswith(b"\xef\xbb\xbf")
    assert "Gate B".encode() in response.content
    assert "Gate A".encode() not in response.content


def test_event_time_is_displayed_in_dhaka_timezone(ui_client) -> None:
    client, test_session = ui_client
    _login(client)
    with test_session() as db:
        camera = db.scalar(select(Camera.id))
        db.add(
            Event(
                camera_id=camera,
                gate="Dhaka Time Gate",
                direction="IN",
                at=datetime(2024, 1, 15, 2, 15, tzinfo=UTC),
                status="unknown",
            )
        )
        db.commit()

    response = client.get("/events", params={"start": "2024-01-15", "end": "2024-01-15"})
    assert response.status_code == 200
    assert "2024-01-15 08:15:00" in response.text

    bengali = client.get(
        "/events",
        params={"start": "2024-01-15", "end": "2024-01-15", "bn_digits": "1"},
    )
    assert "২০২৪-০১-১৫ ০৮:১৫:০০" in bengali.text


def test_review_updates_event_and_writes_audit_log(ui_client) -> None:
    client, test_session = ui_client
    _login(client, "security", "security-password")
    with test_session() as db:
        event_id = db.scalar(select(Event.id).where(Event.status == "unknown").order_by(Event.id))

    response = client.post(
        f"/alerts/{event_id}/review?reviewed=unreviewed",
        data={"note": "Checked at the gate"},
        follow_redirects=False,
    )
    assert response.status_code == 303
    with test_session() as db:
        event = db.get(Event, event_id)
        audit = db.scalar(select(AuditLog).where(AuditLog.action == "event.review"))
        assert event is not None
        assert event.reviewed_at is not None
        assert event.reviewed_by is not None
        assert audit is not None
        assert f"event_id={event_id}" in audit.detail
        assert "Checked at the gate" in audit.detail


def test_event_snapshot_requires_authentication(ui_client) -> None:
    client, _ = ui_client
    assert client.get("/events/1/snapshot").status_code == 401
