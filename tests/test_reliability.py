import shutil
import time
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.camera.manager import manager
from app.db import Base, get_db
from app.jobs.retention import guard_disk_space
from app.jobs.watchdog import SystemWatchdog, start_watchdog, stop_watchdog
from app.main import app
from app.models import AuditLog, Camera, User


@pytest.fixture
def rel_db(tmp_path: Path) -> sessionmaker[Session]:
    db_file = tmp_path / "reliability_test.db"
    engine = create_engine(f"sqlite:///{db_file.as_posix()}", connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


def test_guard_disk_space_prunes_oldest_when_threshold_breached(tmp_path: Path) -> None:
    target_dir = tmp_path / "data"
    media_dir = target_dir / "incidents"
    snap_dir = target_dir / "snapshots"
    media_dir.mkdir(parents=True, exist_ok=True)
    snap_dir.mkdir(parents=True, exist_ok=True)

    # Create dummy files with different mtimes
    file_old = media_dir / "old_clip.mp4"
    file_old.write_bytes(b"A" * 1024 * 1024)  # 1MB
    time.sleep(0.01)

    file_new = snap_dir / "new_snap.jpg"
    file_new.write_bytes(b"B" * 512 * 1024)  # 512KB

    # Call with high min_free_gb to force triggering prune
    # Mock shutil.disk_usage to return low free space first, then restored free space
    class MockDiskUsage:
        def __init__(self, free_gb: float) -> None:
            self.total = int(100 * (1024 ** 3))
            self.free = int(free_gb * (1024 ** 3))

    calls = [MockDiskUsage(1.0), MockDiskUsage(1.0), MockDiskUsage(10.0)]

    def fake_disk_usage(_path):
        return calls.pop(0) if calls else MockDiskUsage(10.0)

    import shutil
    orig_usage = shutil.disk_usage
    shutil.disk_usage = fake_disk_usage
    try:
        res = guard_disk_space(
            target_dir=target_dir,
            min_free_gb=5.0,
            snapshot_dir=snap_dir,
            incident_media_dir=media_dir,
        )
        assert res["triggered"] is True
        assert res["pruned_files"] >= 1
        assert not file_old.exists()  # Oldest file must be pruned first
    finally:
        shutil.disk_usage = orig_usage


def test_watchdog_detects_dead_camera_and_restarts(
    rel_db: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("app.jobs.watchdog.SessionLocal", rel_db)

    with rel_db() as db:
        cam = Camera(
            name="Main Gate Cam",
            gate="Main",
            direction="IN",
            rtsp_url_encrypted=app.state._fernet.encrypt(b"webcam:0").decode()
            if hasattr(app.state, "_fernet")
            else "MDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDA=",
            enabled=True,
            face_enabled=False,
            surveillance_enabled=False,
        )
        # Ensure Fernet encrypts with test fernet key
        from app.config import settings
        from cryptography.fernet import Fernet
        f = Fernet(settings.fernet_key.encode())
        cam.rtsp_url_encrypted = f.encrypt(b"webcam:0").decode()

        db.add(cam)
        db.commit()
        db.refresh(cam)
        cam_id = cam.id

    # Track restart calls
    restarted = []
    monkeypatch.setattr(manager, "restart", lambda cid, src: restarted.append((cid, src)))

    watchdog = SystemWatchdog()
    stats = watchdog.check_once()

    assert cam_id in stats["restarted_cameras"]
    assert stats["total_restarts"] == 1
    assert len(restarted) == 1
    assert restarted[0][0] == cam_id


def test_watchdog_lifecycle() -> None:
    stop_event, thread = start_watchdog(interval_seconds=0.1)
    assert thread.is_alive()
    stop_watchdog(stop_event, thread)
    assert not thread.is_alive()


def test_health_reports_watchdog_status() -> None:
    # An in-memory database: the real one may not have been migrated to the current schema yet.
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine)

    def override_get_db():
        with factory() as db:
            yield db

    app.dependency_overrides[get_db] = override_get_db
    try:
        res = TestClient(app).get("/health")
    finally:
        app.dependency_overrides.pop(get_db, None)
    assert res.status_code == 200
    assert "watchdog" in res.json()
    assert res.json()["watchdog"] in ("running", "disabled")


def test_watchdog_restarts_a_stopped_face_pipeline(rel_db: sessionmaker[Session], monkeypatch: pytest.MonkeyPatch) -> None:
    # A face-enabled camera made the whole check crash (PipelineManager had no running()),
    # which also skipped the disk guard and the daily summary.
    from cryptography.fernet import Fernet

    from app.config import settings
    from app.pipeline.worker import pipeline_manager

    monkeypatch.setattr("app.jobs.watchdog.SessionLocal", rel_db)
    with rel_db() as db:
        cam = Camera(
            name="Gate", gate="Main", direction="IN", enabled=True, face_enabled=True, surveillance_enabled=False,
            rtsp_url_encrypted=Fernet(settings.fernet_key.encode()).encrypt(b"webcam:0").decode(),
        )
        db.add(cam)
        db.commit()
        cam_id = cam.id
    started = []
    monkeypatch.setattr(manager, "restart", lambda cid, src: None)
    monkeypatch.setattr(pipeline_manager, "start_camera", lambda cid, gate, direction: started.append((cid, gate, direction)))
    monkeypatch.setattr("app.jobs.watchdog.guard_disk_space", lambda db: {"triggered": False, "marker": "ran"})

    stats = SystemWatchdog().check_once()

    assert started == [(cam_id, "Main", "IN")]
    assert stats["disk"].get("marker") == "ran"  # the check ran to the end


def test_pipeline_manager_running_follows_the_worker_thread() -> None:
    import threading

    from app.pipeline.worker import PipelineManager

    class FakeWorker:
        def __init__(self, thread):
            self._thread = thread

    done = threading.Thread(target=lambda: None)
    done.start()
    done.join()
    live_stop = threading.Event()
    live = threading.Thread(target=live_stop.wait)
    live.start()
    manager_ = PipelineManager()
    try:
        manager_._workers = {1: FakeWorker(live), 2: FakeWorker(done)}
        assert manager_.running(1) is True
        assert manager_.running(2) is False  # thread ended: the watchdog must restart it
        assert manager_.running(3) is False
    finally:
        live_stop.set()
        live.join()
