import os
from datetime import UTC, datetime, timedelta
from pathlib import Path

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.db import Base
from app.jobs.incidents import close_orphan_incidents
from app.models import Camera, Incident


def test_open_incidents_from_previous_run_are_closed(tmp_path: Path) -> None:
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine, expire_on_commit=False)
    started = datetime(2026, 11, 16, 13, 26, 34, tzinfo=UTC)
    clip = tmp_path / "data" / "incidents" / "clip.mp4"
    clip.parent.mkdir(parents=True)
    clip.write_bytes(b"x")
    clip_end = started + timedelta(seconds=40)
    os.utime(clip, (clip_end.timestamp(), clip_end.timestamp()))
    with session() as db:
        db.add(Camera(name="C", gate="G", direction="IN", rtsp_url_encrypted="x"))
        db.add_all(
            [
                Incident(camera_id=1, kind="person", started_at=started, clip_path="data/incidents/clip.mp4"),
                Incident(camera_id=1, kind="person", started_at=started, clip_path="data/incidents/missing.mp4"),
                Incident(camera_id=1, kind="person", started_at=started, ended_at=started),
            ]
        )
        db.commit()

        assert close_orphan_incidents(db, root=tmp_path) == 2
        assert close_orphan_incidents(db, root=tmp_path) == 0

        ends = [incident.ended_at.replace(tzinfo=UTC) for incident in db.query(Incident).order_by(Incident.id)]
    assert abs((ends[0] - clip_end).total_seconds()) < 1
    assert ends[1] == started
    engine.dispose()
