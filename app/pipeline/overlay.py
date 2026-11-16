"""Latest face-recognition results per camera, for drawing on the live view."""

import threading
import time
from dataclasses import dataclass
from typing import Literal

FaceStatus = Literal["matched", "checking", "unknown", "low_quality"]

# The pipeline runs at ~5 fps; older results no longer line up with the video.
OVERLAY_MAX_AGE_SECONDS = 1.0


@dataclass(frozen=True)
class FaceOverlay:
    box: tuple[float, float, float, float]
    status: FaceStatus
    person_id: int | None = None
    score: float | None = None


class OverlayStore:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._items: dict[int, tuple[float, list[FaceOverlay]]] = {}

    def publish(self, camera_id: int, faces: list[FaceOverlay], now: float | None = None) -> None:
        with self._lock:
            self._items[camera_id] = (time.monotonic() if now is None else now, list(faces))

    def latest(self, camera_id: int, now: float | None = None) -> list[FaceOverlay]:
        current = time.monotonic() if now is None else now
        with self._lock:
            item = self._items.get(camera_id)
        if item is None or current - item[0] > OVERLAY_MAX_AGE_SECONDS:
            return []
        return list(item[1])

    def clear(self, camera_id: int) -> None:
        with self._lock:
            self._items.pop(camera_id, None)


face_overlays = OverlayStore()
