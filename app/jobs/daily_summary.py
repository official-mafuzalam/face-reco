"""Sends the day's summary as one push in the evening (checked by the watchdog loop)."""

import logging
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import settings
from app.i18n import TRANSLATIONS
from app.models import AuditLog
from app.notify.push import send_push_notification
from app.services.summary import build_daily_summary, summary_text
from app.surveillance.rules import parse_hhmm

logger = logging.getLogger(__name__)
ACTION = "summary.push"


def maybe_send_daily_summary(db: Session, local_now: datetime, start: datetime, end: datetime) -> bool:
    """Push the summary once per local day, at or after DAILY_SUMMARY_TIME. True when it was sent now.

    The sent-marker is an audit row, so a restart in the evening does not send it twice.
    """
    due = parse_hhmm(settings.daily_summary_time)
    if due is None or local_now.time() < due:
        return False
    marker = f"date={local_now.date().isoformat()}"
    if db.scalar(select(AuditLog.id).where(AuditLog.action == ACTION, AuditLog.detail == marker)) is not None:
        return False
    t = TRANSLATIONS["bn"]
    summary = build_daily_summary(db, start, end)
    # Record first: a failing push must not retry every 15 seconds all evening.
    db.add(AuditLog(user_id=None, action=ACTION, detail=marker))
    db.commit()
    send_push_notification(db, t["summary_title"], summary_text(summary, t), url="/", severity="warning")
    return True
