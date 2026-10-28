from datetime import UTC, datetime, timedelta
import os
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.config import settings
from app.db import Base
from app.jobs.retention import run_retention
from app.models import AuditLog, Camera, Event
from app.main import health


@pytest.fixture
def retention_db():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    with factory() as db:
        db.add(
            Camera(
                id=1,
                name="Test",
                gate="Test gate",
                direction="IN",
                rtsp_url_encrypted="encrypted",
            )
        )
        db.commit()
    try:
        yield factory
    finally:
        engine.dispose()


def _write_old_file(path: Path, modified_at: datetime) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"synthetic snapshot")
    timestamp = modified_at.timestamp()
    os.utime(path, (timestamp, timestamp))


def test_retention_deletes_only_items_older_than_configured_cutoffs(
    retention_db: sessionmaker[Session],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "snapshot_retention_days", 90)
    monkeypatch.setattr(settings, "event_retention_days", 365)
    now = datetime(2026, 10, 28, tzinfo=UTC)
    old_snapshot = tmp_path / "old.jpg"
    recent_snapshot = tmp_path / "recent.jpg"
    _write_old_file(old_snapshot, now - timedelta(days=91))
    _write_old_file(recent_snapshot, now - timedelta(days=89))

    with retention_db() as db:
        db.add_all(
            [
                Event(
                    camera_id=1,
                    gate="Gate",
                    direction="IN",
                    at=now - timedelta(days=366),
                    status="unknown",
                    snapshot_path="data/snapshots/old.jpg",
                ),
                Event(
                    camera_id=1,
                    gate="Gate",
                    direction="OUT",
                    at=now - timedelta(days=364),
                    status="matched",
                ),
            ]
        )
        db.commit()

        counts = run_retention(db, now=now, snapshot_dir=tmp_path)

        assert counts == {"snapshots": 1, "events": 1}
        assert not old_snapshot.exists()
        assert recent_snapshot.exists()
        events = list(db.scalars(select(Event)))
        assert len(events) == 1
        assert events[0].direction == "OUT"
        audit = db.scalar(select(AuditLog).where(AuditLog.action == "retention.run"))
        assert audit is not None
        assert "snapshot_candidates=1" in audit.detail
        assert "event_candidates=1" in audit.detail


def test_retention_dry_run_keeps_files_and_event_rows(
    retention_db: sessionmaker[Session],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "snapshot_retention_days", 30)
    monkeypatch.setattr(settings, "event_retention_days", 30)
    now = datetime(2026, 10, 28, tzinfo=UTC)
    stale_snapshot = tmp_path / "stale.jpg"
    _write_old_file(stale_snapshot, now - timedelta(days=31))

    with retention_db() as db:
        event = Event(
            camera_id=1,
            gate="Gate",
            direction="IN",
            at=now - timedelta(days=31),
            status="unknown",
        )
        db.add(event)
        db.commit()
        event_id = event.id

        counts = run_retention(db, now=now, dry_run=True, snapshot_dir=tmp_path)

        assert counts == {"snapshots": 1, "events": 1}
        assert stale_snapshot.exists()
        assert db.get(Event, event_id) is not None
        audit = db.scalar(select(AuditLog).where(AuditLog.action == "retention.run"))
        assert audit is not None
        assert "dry_run=true" in audit.detail


def test_health_reports_only_online_and_offline_camera_counts(
    retention_db: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.main import manager

    monkeypatch.setattr(manager, "get_state", lambda _camera_id: SimpleNamespace(status="online"))
    with retention_db() as db:
        result = health(db)

    assert result == {"status": "ok", "cameras_online": 1, "cameras_offline": 0}
    assert not {"name", "gate", "ip", "url"}.intersection(result)
