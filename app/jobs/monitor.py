"""Records a health reading every minute so uptime and outages can be reviewed later."""

import logging
import threading
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app.db import SessionLocal
from app.health import CameraHealth, SystemHealth, camera_health, system_health
from app.models import HealthSample

logger = logging.getLogger(__name__)
SAMPLE_SECONDS = 60.0
KEEP_DAYS = 14


def record_sample(
    db: Session,
    cameras: list[CameraHealth],
    system: SystemHealth,
    now: datetime | None = None,
) -> None:
    at = now or datetime.now(UTC)
    for camera in cameras:
        if not camera.enabled:
            status = "disabled"
        else:
            status = "online" if camera.state.status == "online" else "offline"
        db.add(
            HealthSample(
                at=at,
                camera_id=camera.camera_id,
                status=status,
                fps=camera.state.fps,
                face_fps=camera.face.fps if camera.face else None,
                surveillance_fps=camera.surveillance.fps if camera.surveillance else None,
            )
        )
    db.add(
        HealthSample(
            at=at,
            camera_id=None,
            status="system",
            cpu_percent=system.cpu_percent,
            disk_free_gb=system.disk_free_gb,
        )
    )
    db.execute(delete(HealthSample).where(HealthSample.at < at - timedelta(days=KEEP_DAYS)))
    db.commit()


@dataclass(frozen=True)
class Outage:
    camera_id: int
    started_at: datetime
    ended_at: datetime | None  # None: still offline at the latest sample
    minutes: int


def _utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value


def outages(samples: Iterable[HealthSample], gap_seconds: float = SAMPLE_SECONDS * 3) -> list[Outage]:
    """Group consecutive offline readings per camera into outage periods.

    A missing stretch of samples (app not running) also ends a period, so a weekend
    with the PC switched off is not reported as one long camera outage.
    """
    by_camera: dict[int, list[HealthSample]] = {}
    for sample in samples:
        if sample.camera_id is not None and sample.status != "disabled":
            by_camera.setdefault(sample.camera_id, []).append(sample)
    found: list[Outage] = []
    for camera_id, items in by_camera.items():
        items.sort(key=lambda item: _utc(item.at))
        start: datetime | None = None
        last: datetime | None = None
        for item in items:
            at = _utc(item.at)
            gap = last is not None and (at - last).total_seconds() > gap_seconds
            if start is not None and (item.status == "online" or gap):
                end = at if item.status == "online" and not gap else last
                found.append(Outage(camera_id, start, end, max(1, round((end - start).total_seconds() / 60))))
                start = None
            if item.status == "offline" and start is None:
                start = at
            last = at
        if start is not None and last is not None:
            found.append(Outage(camera_id, start, None, max(1, round((last - start).total_seconds() / 60))))
    return sorted(found, key=lambda outage: outage.started_at, reverse=True)


def uptime_percent(samples: Iterable[HealthSample]) -> float | None:
    counted = [sample for sample in samples if sample.status in ("online", "offline")]
    if not counted:
        return None
    return 100.0 * sum(sample.status == "online" for sample in counted) / len(counted)


def load_samples(db: Session, since: datetime) -> list[HealthSample]:
    return list(db.scalars(select(HealthSample).where(HealthSample.at >= since).order_by(HealthSample.at)))


def _monitor_loop(stop_event: threading.Event, session_factory: Callable[[], Session]) -> None:
    # First reading after one interval, so cameras have had time to connect.
    while not stop_event.wait(SAMPLE_SECONDS):
        try:
            with session_factory() as db:
                record_sample(db, camera_health(db), system_health())
        except Exception:
            logger.exception("Health monitor sample failed.")


def start_monitor(
    session_factory: Callable[[], Session] = SessionLocal,
) -> tuple[threading.Event, threading.Thread]:
    stop_event = threading.Event()
    thread = threading.Thread(
        target=_monitor_loop, args=(stop_event, session_factory), name="gate-face-monitor", daemon=True
    )
    thread.start()
    return stop_event, thread


def stop_monitor(stop_event: threading.Event, thread: threading.Thread) -> None:
    stop_event.set()
    thread.join(timeout=10)
