import logging
import threading
import time
from datetime import UTC, datetime

from cryptography.fernet import Fernet
from sqlalchemy import select

from app.agent.verify import is_ai_verifier_alive, restart_ai_verifier
from app.camera.manager import manager
from app.config import settings
from app.db import SessionLocal
from app.jobs.retention import guard_disk_space
from app.models import Camera
from app.notify.sources import notify_worker_restarted
from app.pipeline.worker import pipeline_manager
from app.surveillance.worker import surveillance_manager


logger = logging.getLogger(__name__)
_fernet = Fernet(settings.fernet_key.encode())


class SystemWatchdog:
    """Monitors worker threads, camera feeds, and storage to ensure 24/7 uptime."""

    def __init__(self) -> None:
        self.last_check_at: datetime | None = None
        self.restarted_cameras: list[int] = []
        self.total_restarts: int = 0

    def check_once(self) -> dict:
        self.last_check_at = datetime.now(UTC)
        restarted_in_cycle: list[int] = []

        try:
            with SessionLocal() as db:
                cameras = list(db.scalars(select(Camera).where(Camera.deleted_at.is_(None), Camera.enabled.is_(True))))
                for camera in cameras:
                    source = _fernet.decrypt(camera.rtsp_url_encrypted.encode()).decode()
                    state = manager.get_state(camera.id)

                    # Check reader status / responsiveness
                    needs_restart = False
                    with manager._lock:
                        reader = manager._readers.get(camera.id)
                        if reader is None or (reader._thread is not None and not reader._thread.is_alive()):
                            needs_restart = True

                    if needs_restart:
                        logger.warning("Watchdog detected dead reader for camera %s (#%s). Restarting...", camera.name, camera.id)
                        manager.restart(camera.id, source)
                        restarted_in_cycle.append(camera.id)
                        self.total_restarts += 1
                        notify_worker_restarted(camera.name)

                    # Check face recognition pipeline
                    if camera.face_enabled and not pipeline_manager.running(camera.id):
                        logger.warning("Watchdog restarting face pipeline for camera %s (#%s)", camera.name, camera.id)
                        pipeline_manager.start_camera(camera.id, camera.gate, camera.direction)

                    # Check surveillance worker
                    if camera.surveillance_enabled and settings.surveillance_enabled:
                        if not surveillance_manager.running(camera.id):
                            logger.warning("Watchdog restarting surveillance worker for camera %s (#%s)", camera.name, camera.id)
                            surveillance_manager.start(camera.id)

                # Check AI Verifier worker
                if settings.ai_verify_enabled and not is_ai_verifier_alive():
                    logger.warning("Watchdog restarting AI verify worker.")
                    restart_ai_verifier()

                # Guard disk space
                disk_status = guard_disk_space(db)

                try:
                    from app.jobs.daily_summary import maybe_send_daily_summary
                    from app.web.routes.dashboard import _today_range
                    from app.web.routes.events import _local_zone

                    maybe_send_daily_summary(db, datetime.now(_local_zone()), *_today_range())
                except Exception:
                    logger.exception("Daily summary failed.")

        except Exception:
            logger.exception("System watchdog encountered error during check.")
            disk_status = {"triggered": False}

        self.restarted_cameras = restarted_in_cycle
        return {
            "last_check_at": self.last_check_at.isoformat(),
            "restarted_cameras": restarted_in_cycle,
            "total_restarts": self.total_restarts,
            "disk": disk_status,
        }


watchdog = SystemWatchdog()


def _watchdog_loop(stop_event: threading.Event, interval_seconds: float) -> None:
    while not stop_event.is_set():
        try:
            watchdog.check_once()
        except Exception:
            logger.exception("Watchdog iteration failed.")
        if stop_event.wait(interval_seconds):
            return


def start_watchdog(
    interval_seconds: float | None = None,
) -> tuple[threading.Event, threading.Thread]:
    interval = interval_seconds or settings.watchdog_interval_seconds
    stop_event = threading.Event()
    thread = threading.Thread(
        target=_watchdog_loop,
        args=(stop_event, interval),
        name="gate-face-watchdog",
        daemon=True,
    )
    thread.start()
    return stop_event, thread


def stop_watchdog(
    stop_event: threading.Event,
    thread: threading.Thread,
) -> None:
    stop_event.set()
    thread.join(timeout=10)
