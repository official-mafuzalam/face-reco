"""In-process, offline notification fan-out from worker threads to browser streams."""

import asyncio
import json
import logging
import threading
from collections import deque
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from typing import Literal

from app.i18n import TRANSLATIONS

logger = logging.getLogger(__name__)
Severity = Literal["info", "warning", "critical"]
HISTORY = 200


@dataclass(frozen=True)
class Notification:
    id: int
    kind: str  # unknown_face | rule | camera_offline | camera_online
    severity: Severity
    title: dict[str, str]  # language -> text
    body: dict[str, str]
    at: str  # ISO time, UTC
    image: str | None = None  # URL of a snapshot
    link: str | None = None  # page to open on click
    ack: dict | None = None  # {"type": "incident"|"event", "id": n} when it can be acknowledged
    extra: dict = field(default_factory=dict)

    def to_json(self) -> str:
        return json.dumps(asdict(self), ensure_ascii=False)


def _texts(key: str, **values: object) -> dict[str, str]:
    """A value may itself be a {language: text} dict, e.g. a translated direction."""
    return {
        language: table[key].format(
            **{name: value[language] if isinstance(value, dict) else value for name, value in values.items()}
        )
        for language, table in TRANSLATIONS.items()
    }


def _offer(queue: asyncio.Queue, notification: "Notification") -> None:
    try:
        queue.put_nowait(notification)
    except asyncio.QueueFull:  # a stalled browser tab; it catches up from history on reconnect
        pass


class NotificationBus:
    def __init__(self, history: int = HISTORY) -> None:
        self._lock = threading.Lock()
        self._history: deque[Notification] = deque(maxlen=history)
        self._next_id = 1
        self._subscribers: set[tuple[asyncio.AbstractEventLoop, asyncio.Queue]] = set()

    def publish(
        self,
        kind: str,
        severity: Severity,
        title_key: str,
        body_key: str,
        *,
        image: str | None = None,
        link: str | None = None,
        ack: dict | None = None,
        **values: object,
    ) -> Notification:
        """Safe to call from any thread; never raises into the caller's work."""
        with self._lock:
            notification = Notification(
                id=self._next_id,
                kind=kind,
                severity=severity,
                title=_texts(title_key, **values),
                body=_texts(body_key, **values),
                at=datetime.now(UTC).isoformat(),
                image=image,
                link=link,
                ack=ack,
            )
            self._next_id += 1
            self._history.append(notification)
            subscribers = list(self._subscribers)
        for loop, queue in subscribers:
            try:
                loop.call_soon_threadsafe(_offer, queue, notification)
            except RuntimeError:  # loop already closed
                self.unsubscribe(loop, queue)
        return notification

    def since(self, last_id: int) -> list[Notification]:
        with self._lock:
            return [item for item in self._history if item.id > last_id]

    @property
    def last_id(self) -> int:
        with self._lock:
            return self._next_id - 1

    def subscribe(self) -> tuple[asyncio.AbstractEventLoop, asyncio.Queue]:
        entry = (asyncio.get_running_loop(), asyncio.Queue(maxsize=500))
        with self._lock:
            self._subscribers.add(entry)
        return entry

    def unsubscribe(self, loop: asyncio.AbstractEventLoop, queue: asyncio.Queue) -> None:
        with self._lock:
            self._subscribers.discard((loop, queue))

    @property
    def subscriber_count(self) -> int:
        with self._lock:
            return len(self._subscribers)


bus = NotificationBus()


def safe_publish(*args, **kwargs) -> None:
    """Publishing is best effort: a notification problem must never break detection or logging."""
    try:
        bus.publish(*args, **kwargs)
    except Exception:
        logger.exception("Unable to publish notification.")
