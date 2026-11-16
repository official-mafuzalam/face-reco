"""Turn things the workers see into notifications."""

import logging
import threading
import time
from collections.abc import Callable

from sqlalchemy import select

from app.camera.manager import manager
from app.db import SessionLocal
from app.i18n import TRANSLATIONS
from app.models import Camera
from app.notify.bus import safe_publish

logger = logging.getLogger(__name__)
NAME_REFRESH_SECONDS = 30.0
OFFLINE_NOTIFY_SECONDS = 30.0
WATCH_SECONDS = 5.0

_names_lock = threading.Lock()
_names: dict[int, str] = {}
_names_at = -1e9


def camera_name(camera_id: int) -> str:
    global _names, _names_at
    now = time.monotonic()
    with _names_lock:
        if now - _names_at >= NAME_REFRESH_SECONDS or camera_id not in _names:
            _names_at = now
            try:
                with SessionLocal() as db:
                    _names = dict(db.execute(select(Camera.id, Camera.name)).all())
            except Exception:
                logger.exception("Unable to load camera names for notifications.")
        return _names.get(camera_id, f"#{camera_id}")


def notify_unknown_face(event_id: int, gate: str, direction: str) -> None:
    safe_publish(
        "unknown_face",
        "warning",
        "notify_unknown_title",
        "notify_unknown_body",
        image=f"/events/{event_id}/snapshot",
        link="/alerts",
        ack={"type": "event", "id": event_id},
        gate=gate,
        direction={language: table["in" if direction == "IN" else "out"] for language, table in TRANSLATIONS.items()},
    )


def notify_watchlist(
    event_id: int, person_name: str, person_code: str, category: str, gate: str, direction: str, has_snapshot: bool = True
) -> None:
    """Blacklisted person: full-screen alarm. VIP: a friendly heads-up."""
    if category not in ("blacklist", "vip"):
        return
    critical = category == "blacklist"
    safe_publish(
        f"watchlist_{category}",
        "critical" if critical else "info",
        f"notify_{category}_title",
        "notify_watchlist_body",
        image=f"/events/{event_id}/snapshot" if has_snapshot else None,
        link="/events",
        ack={"type": "event", "id": event_id} if critical else None,
        name=person_name,
        code=person_code,
        gate=gate,
        direction={language: table["in" if direction == "IN" else "out"] for language, table in TRANSLATIONS.items()},
    )


def notify_rule_hit(incident_id: int, camera_id: int, rule_name: str, severity: str) -> None:
    if severity not in ("warning", "critical"):
        return  # "info" rules are for the record only
    safe_publish(
        "rule",
        severity,  # type: ignore[arg-type]
        "notify_rule_title",
        "notify_rule_body",
        image=f"/incidents/{incident_id}/snapshot.jpg",
        link=f"/incidents/{incident_id}",
        ack={"type": "incident", "id": incident_id},
        rule=rule_name,
        camera=camera_name(camera_id),
    )


class CameraWatcher:
    """Notifies once when a camera has been without video for a while, and once when it is back.

    Short reconnects (a few seconds) are normal and stay silent.
    """

    def __init__(
        self,
        camera_ids: Callable[[], list[int]],
        offline_after: float = OFFLINE_NOTIFY_SECONDS,
    ) -> None:
        self._camera_ids = camera_ids
        self._offline_after = offline_after
        self._reported: set[int] = set()

    def check(self) -> None:
        for camera_id in self._camera_ids():
            state = manager.get_state(camera_id)
            age = state.frame_age_seconds
            offline = state.status != "online" and (age is None or age >= self._offline_after)
            if offline and camera_id not in self._reported:
                if age is None and state.attempts == 0:
                    continue  # reader not started yet
                self._reported.add(camera_id)
                safe_publish(
                    "camera_offline", "warning", "notify_camera_offline_title", "notify_camera_offline_body",
                    link="/system", camera=camera_name(camera_id),
                )
            elif state.status == "online" and camera_id in self._reported:
                self._reported.discard(camera_id)
                safe_publish(
                    "camera_online", "info", "notify_camera_online_title", "notify_camera_online_body",
                    link="/live", camera=camera_name(camera_id),
                )


def _enabled_camera_ids() -> list[int]:
    with SessionLocal() as db:
        return list(db.scalars(select(Camera.id).where(Camera.enabled.is_(True))))


def _watch_loop(stop_event: threading.Event, watcher: CameraWatcher) -> None:
    while not stop_event.wait(WATCH_SECONDS):
        try:
            watcher.check()
        except Exception:
            logger.exception("Camera watcher failed.")


def start_camera_watch() -> tuple[threading.Event, threading.Thread]:
    stop_event = threading.Event()
    thread = threading.Thread(
        target=_watch_loop, args=(stop_event, CameraWatcher(_enabled_camera_ids)), name="gate-face-camera-watch", daemon=True
    )
    thread.start()
    return stop_event, thread


def stop_camera_watch(stop_event: threading.Event, thread: threading.Thread) -> None:
    stop_event.set()
    thread.join(timeout=10)
