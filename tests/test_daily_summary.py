from datetime import UTC, datetime, timedelta
from unittest.mock import patch

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.config import settings
from app.db import Base
from app.i18n import TRANSLATIONS
from app.jobs.daily_summary import maybe_send_daily_summary
from app.models import AuditLog, Camera, Event, Incident
from app.services.summary import build_daily_summary, summary_text

START = datetime(2026, 11, 19, 0, 0)
END = START + timedelta(days=1)


@pytest.fixture
def db():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    with sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)() as session:
        session.add_all([Camera(id=1, name="Gate", gate="N", direction="IN", rtsp_url_encrypted="x"),
                         Camera(id=2, name="Store", gate="S", direction="IN", rtsp_url_encrypted="x")])
        session.commit()
        yield session


def _incident(db, camera, severity, when, reviewed=False):
    db.add(Incident(camera_id=camera, kind="intrusion", severity=severity, started_at=when,
                    reviewed_at=when if reviewed else None))


def test_summary_counts_only_todays_warnings_and_criticals(db) -> None:
    noon = START + timedelta(hours=12)
    _incident(db, 1, "critical", noon)
    _incident(db, 1, "warning", noon, reviewed=True)
    _incident(db, 2, "warning", noon)
    _incident(db, 2, "info", noon)  # plain movement is not an alert
    _incident(db, 1, "critical", START - timedelta(hours=1))  # yesterday
    db.add(Event(camera_id=1, gate="N", direction="IN", status="unknown", at=noon))
    db.commit()

    summary = build_daily_summary(db, START, END)
    assert (summary["incidents"], summary["critical"], summary["warning"]) == (3, 1, 2)
    assert (summary["unreviewed"], summary["unknown"]) == (2, 1)
    assert summary["cameras"][0] == ("Gate", 2)
    assert "3" in summary_text(summary, TRANSLATIONS["en"]) and "৩" not in summary_text(summary, TRANSLATIONS["en"])


def test_quiet_day_has_its_own_sentence(db) -> None:
    summary = build_daily_summary(db, START, END)
    assert summary_text(summary, TRANSLATIONS["bn"]) == TRANSLATIONS["bn"]["summary_none"]


def test_summary_push_goes_out_once_per_day_after_the_set_time(db, monkeypatch) -> None:
    monkeypatch.setattr(settings, "daily_summary_time", "20:00")
    with patch("app.jobs.daily_summary.send_push_notification") as push:
        assert maybe_send_daily_summary(db, datetime(2026, 11, 19, 19, 59), START, END) is False  # too early
        assert maybe_send_daily_summary(db, datetime(2026, 11, 19, 20, 0), START, END) is True
        assert maybe_send_daily_summary(db, datetime(2026, 11, 19, 23, 0), START, END) is False  # already sent
        assert maybe_send_daily_summary(db, datetime(2026, 11, 20, 20, 5), START, END) is True  # next day
    assert push.call_count == 2
    assert db.query(AuditLog).filter(AuditLog.action == "summary.push").count() == 2
    monkeypatch.setattr(settings, "daily_summary_time", "")
    assert maybe_send_daily_summary(db, datetime(2026, 11, 21, 21, 0), START, END) is False  # switched off
