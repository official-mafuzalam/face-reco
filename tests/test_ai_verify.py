from collections.abc import AsyncIterator, Generator
from contextlib import asynccontextmanager
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.agent.verify import (
    check_ollama_status,
    extract_incident_frames,
    query_ollama_vlm,
    verify_incident,
)
from app.auth import hash_password
from app.config import settings
from app.db import Base, get_db
from app.main import app
from app.models import Camera, Incident, User


@pytest.fixture
def ai_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
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
    monkeypatch.setattr("app.agent.verify.SessionLocal", factory)
    monkeypatch.setattr(settings, "ai_verify_enabled", True)

    app.dependency_overrides[get_db] = override_get_db
    with factory() as db:
        user = User(username="admin", password_hash=hash_password("adminpass1234"), role="admin")
        camera = Camera(name="Gate Cam", gate="Main Gate", direction="IN", rtsp_url_encrypted="x", enabled=True)
        db.add_all([user, camera])
        db.commit()
    with TestClient(app) as client:
        yield client, factory, tmp_path
    app.dependency_overrides.clear()
    engine.dispose()


def test_check_ollama_status_offline():
    status = check_ollama_status()
    assert "status" in status
    assert status["target_model"] == settings.ollama_model


def test_extract_incident_frames(tmp_path: Path):
    snap = tmp_path / "snap.jpg"
    snap.write_bytes(b"\xff\xd8\xff\xe0\x00\x10JFIF\x00\x01\x01\x00\x00\x01\x00\x01\x00\x00\xff\xdb")
    frames = extract_incident_frames(str(snap), None)
    assert len(frames) == 1
    assert isinstance(frames[0], str)


def test_query_ollama_vlm_mocked():
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = {
        "message": {
            "content": '{"verdict": "confirmed", "confidence": 0.92, "summary_bn": "গেট পার হওয়া ব্যক্তি শনাক্ত হয়েছে", "summary_en": "Person crossing detected"}'
        }
    }
    with patch("httpx.Client.post", return_value=mock_resp):
        res = query_ollama_vlm(["dGVzdA=="], "intrusion", "person inside")
        assert res is not None
        assert res["verdict"] == "confirmed"
        assert res["confidence"] == 0.92


def test_verify_incident_end_to_end(ai_env):
    client, factory, tmp_path = ai_env

    snap = tmp_path / "snap.jpg"
    snap.write_bytes(b"\xff\xd8\xff\xe0test")

    with factory() as db:
        inc = Incident(
            camera_id=1,
            kind="intrusion",
            severity="warning",
            snapshot_path=str(snap),
            details="person detected in finance room",
        )
        db.add(inc)
        db.commit()
        inc_id = inc.id

    # Run verification with mock VLM
    mock_res = {
        "verdict": "confirmed",
        "confidence": 0.95,
        "summary_bn": "ফাইন্যান্স রুমে অবৈধ প্রবেশ",
        "summary_en": "Unauthorized entry in finance room",
    }
    with patch("app.agent.verify.query_ollama_vlm", return_value=mock_res):
        verify_incident(inc_id)

    with factory() as db:
        updated = db.get(Incident, inc_id)
        assert updated.ai_verdict == "confirmed"
        assert updated.ai_confidence == 0.95
        assert updated.ai_summary_bn == "ফাইন্যান্স রুমে অবৈধ প্রবেশ"
        assert updated.ai_checked_at is not None


def test_health_endpoint_reports_ai_verifier(ai_env):
    client, factory, _ = ai_env
    res = client.get("/health")
    assert res.status_code == 200
    data = res.json()
    assert "ai_verifier" in data
