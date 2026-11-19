"""The day's alerts in one paragraph, for the dashboard and the evening push."""

from datetime import datetime

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import Camera, Event, Incident

TOP_CAMERAS = 3


def build_daily_summary(db: Session, start: datetime, end: datetime) -> dict:
    """Counts for [start, end) (naive UTC, like the other date filters)."""
    in_day = (Incident.started_at >= start, Incident.started_at < end, Incident.severity.in_(("warning", "critical")))
    by_severity = dict(db.execute(select(Incident.severity, func.count(Incident.id)).where(*in_day).group_by(Incident.severity)).all())
    unreviewed = db.scalar(select(func.count(Incident.id)).where(*in_day, Incident.reviewed_at.is_(None))) or 0
    unknown = db.scalar(
        select(func.count(Event.id)).where(Event.at >= start, Event.at < end, Event.status == "unknown")
    ) or 0
    busiest = db.execute(
        select(Camera.name, func.count(Incident.id))
        .join(Camera, Camera.id == Incident.camera_id)
        .where(*in_day)
        .group_by(Camera.id)
        .order_by(func.count(Incident.id).desc())
        .limit(TOP_CAMERAS)
    ).all()
    critical, warning = by_severity.get("critical", 0), by_severity.get("warning", 0)
    return {
        "incidents": critical + warning, "critical": critical, "warning": warning,
        "unreviewed": unreviewed, "unknown": unknown, "cameras": [(name, count) for name, count in busiest],
    }


def summary_text(summary: dict, t: dict) -> str:
    if not summary["incidents"] and not summary["unknown"]:
        return t["summary_none"]
    return t["summary_line"].format(**{k: summary[k] for k in ("incidents", "critical", "warning", "unreviewed", "unknown")})
