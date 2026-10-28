import time
from collections.abc import Generator
from contextlib import asynccontextmanager
from pathlib import Path
import sys
from typing import AsyncIterator

import numpy as np
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.camera import reader as camera_reader
from app.camera.reader import CameraReader, mask_credentials
import cv2
from app.auth import hash_password
from app.db import Base, get_db
from app.main import app, encrypt_rtsp_url
from app.models import Camera, User


def _write_video(path: Path) -> None:
    writer = cv2.VideoWriter(
        str(path),
        cv2.VideoWriter_fourcc(*"mp4v"),
        10.0,
        (32, 24),
    )
    assert writer.isOpened()
    for index in range(10):
        frame = np.zeros((24, 32, 3), dtype=np.uint8)
        frame[:, :, 0] = index * 20
        writer.write(frame)
    writer.release()


def test_reader_opens_generated_video_and_keeps_latest_frame(tmp_path: Path) -> None:
    video_path = tmp_path / "reader.mp4"
    _write_video(video_path)
    reader = CameraReader(1, f"file:{video_path}")
    reader.start()
    try:
        deadline = time.monotonic() + 5
        frame = None
        while time.monotonic() < deadline:
            frame = reader.latest_frame()
            if frame is not None and reader.state().status == "online":
                break
            time.sleep(0.05)
        assert frame is not None
        assert frame.shape == (24, 32, 3)
        assert reader.state().status == "online"
    finally:
        reader.stop()


def test_probe_reports_video_details_and_saves_frame(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys) -> None:
    video_path = tmp_path / "probe.mp4"
    output_path = tmp_path / "frame.jpg"
    _write_video(video_path)
    monkeypatch.setattr(
        sys,
        "argv",
        ["probe", f"file:{video_path}", "--out", str(output_path)],
    )

    from app.camera.probe import main as probe_main

    assert probe_main() == 0
    output = capsys.readouterr().out
    assert "Resolution: 32x24" in output
    assert "FPS: 10.00" in output
    assert "Codec:" in output
    assert output_path.is_file()


def test_bad_source_goes_offline_and_retries(monkeypatch: pytest.MonkeyPatch) -> None:
    attempts = 0

    class BadCapture:
        def isOpened(self) -> bool:
            return False

        def release(self) -> None:
            return None

    def bad_capture(_source: str, *_args: int) -> BadCapture:
        nonlocal attempts
        attempts += 1
        return BadCapture()

    monkeypatch.setattr(camera_reader.cv2, "VideoCapture", bad_capture)
    reader = CameraReader(2, "rtsp://user:secret@example.invalid/bad")
    reader.start()
    try:
        deadline = time.monotonic() + 4
        while time.monotonic() < deadline and attempts < 2:
            time.sleep(0.05)
        state = reader.state()
        assert state.status == "offline"
        assert state.attempts >= 2
        assert "secret" not in (state.last_error or "")
    finally:
        reader.stop()


def test_credentials_are_masked_in_errors_and_logs(caplog: pytest.LogCaptureFixture) -> None:
    reader = CameraReader(3, "rtsp://user:secret@example.invalid/live")
    reader._set_failure("Unable to read rtsp://user:secret@example.invalid/live")

    assert "secret" not in (reader.state().last_error or "")
    assert "user:secret" not in caplog.text
    assert mask_credentials("rtsp://user:secret@example.invalid/live") == (
        "rtsp://***:***@example.invalid/live"
    )


@pytest.fixture
def web_client(monkeypatch: pytest.MonkeyPatch) -> Generator[tuple[TestClient, sessionmaker[Session]], None, None]:
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
                User(username="admin-camera", password_hash=hash_password("test-password-123"), role="admin"),
                User(username="security-camera", password_hash=hash_password("test-password-123"), role="security"),
            ]
        )
        db.add(
            Camera(
                name="Front",
                gate="North",
                direction="IN",
                rtsp_url_encrypted=encrypt_rtsp_url(
                    "rtsp://camera-user:camera-secret@example.invalid/live"
                ),
                enabled=False,
            )
        )
        db.commit()
    with TestClient(app) as client:
        yield client, test_session
    app.dependency_overrides.clear()
    test_engine.dispose()


def test_snapshot_requires_login_and_camera_pages_are_admin_only(
    web_client: tuple[TestClient, sessionmaker[Session]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, test_session = web_client
    assert client.get("/cameras/1/snapshot.jpg").status_code == 401

    response = client.post(
        "/login",
        data={"username": "security-camera", "password": "test-password-123"},
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert client.get("/cameras").status_code == 403

    client.post("/logout")
    client.post(
        "/login",
        data={"username": "admin-camera", "password": "test-password-123"},
        follow_redirects=False,
    )
    camera_page = client.get("/cameras")
    assert camera_page.status_code == 200
    assert "every 3s" in camera_page.text
    assert client.get("/cameras/status?lang=bn").status_code == 200

    invalid_form = client.post(
        "/cameras/new",
        data={
            "name": "Bad",
            "gate": "North",
            "direction": "SIDEWAYS",
            "source": "rtsp://camera-user:camera-secret@example.invalid/live",
        },
    )
    assert invalid_form.status_code == 200
    assert "camera-secret" not in invalid_form.text

    create_result = client.post(
        "/cameras/new",
        data={
            "name": "Side",
            "gate": "West",
            "direction": "OUT",
            "source": "rtsp://new-user:new-secret@example.invalid/side",
        },
        follow_redirects=False,
    )
    assert create_result.status_code == 303
    with test_session() as db:
        added_camera = db.query(Camera).filter_by(name="Side").one()
        from app.web.routes.cameras import _decrypt

        assert _decrypt(added_camera) == "rtsp://new-user:new-secret@example.invalid/side"

    edit_page = client.get("/cameras/1/edit")
    assert edit_page.status_code == 200
    assert "camera-secret" not in edit_page.text
    assert "rtsp://***:***@example.invalid/live" in edit_page.text

    monkeypatch.setattr("app.web.routes.cameras.manager.restart", lambda _camera_id, _source: None)
    edit_result = client.post(
        "/cameras/1/edit",
        data={
            "name": "Front",
            "gate": "North",
            "direction": "IN",
            "source": "rtsp://***:***@example.invalid/live",
            "password": "",
            "enabled": "on",
        },
        follow_redirects=False,
    )
    assert edit_result.status_code == 303
    with test_session() as db:
        camera = db.get(Camera, 1)
        assert camera is not None
        assert _decrypt(camera) == "rtsp://camera-user:camera-secret@example.invalid/live"

    monkeypatch.setattr(
        "app.web.routes.cameras.manager.get_latest_frame",
        lambda _camera_id: np.zeros((8, 8, 3), dtype=np.uint8),
    )
    snapshot = client.get("/cameras/1/snapshot.jpg")
    assert snapshot.status_code == 200
    assert snapshot.headers["content-type"] == "image/jpeg"
