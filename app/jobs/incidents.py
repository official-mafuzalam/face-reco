"""Startup housekeeping for incidents left open by a crash or restart."""

import logging
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Incident

logger = logging.getLogger(__name__)
_PROJECT_ROOT = Path(__file__).resolve().parents[2]


def _as_utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def close_orphan_incidents(db: Session, root: Path = _PROJECT_ROOT) -> int:
    """Give every incident still marked as running an end time.

    No surveillance worker is running yet at startup, so an open incident can only be a
    leftover from the previous run. Its clip file stopped growing when that run ended, so
    the clip's last write time is the best estimate of when the activity ended.
    """
    incidents = db.scalars(select(Incident).where(Incident.ended_at.is_(None))).all()
    for incident in incidents:
        started = _as_utc(incident.started_at)
        ended = started
        if incident.clip_path:
            clip = root / incident.clip_path
            try:
                ended = max(started, datetime.fromtimestamp(clip.stat().st_mtime, UTC))
            except OSError:
                pass
        incident.ended_at = ended
    if incidents:
        db.commit()
        logger.info("Closed %s incident(s) left open by the previous run.", len(incidents))
    return len(incidents)
