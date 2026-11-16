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


@dataclass(frozen=True)
class ObjectOverlay:
    box: tuple[float, float, float, float]
    category: str  # person | vehicle
    label: str  # detector label, e.g. "person", "car"
    track_id: int


# Surveillance analyses ~2 frames/s, so its boxes stay valid a little longer.
OBJECT_MAX_AGE_SECONDS = 1.5
RULE_HIT_SHOW_SECONDS = 5.0


class ObjectOverlayStore:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._objects: dict[int, tuple[float, list[ObjectOverlay]]] = {}
        self._hits: dict[int, dict[int, float]] = {}  # camera -> rule id -> when it fired

    def publish(self, camera_id: int, objects: list[ObjectOverlay], hit_rule_ids: set[int], now: float | None = None) -> None:
        current = time.monotonic() if now is None else now
        with self._lock:
            self._objects[camera_id] = (current, list(objects))
            hits = self._hits.setdefault(camera_id, {})
            for rule_id in hit_rule_ids:
                hits[rule_id] = current

    def latest(self, camera_id: int, now: float | None = None) -> tuple[list[ObjectOverlay], set[int]]:
        current = time.monotonic() if now is None else now
        with self._lock:
            item = self._objects.get(camera_id)
            hits = dict(self._hits.get(camera_id, {}))
        objects = [] if item is None or current - item[0] > OBJECT_MAX_AGE_SECONDS else list(item[1])
        return objects, {rule_id for rule_id, at in hits.items() if current - at <= RULE_HIT_SHOW_SECONDS}

    def clear(self, camera_id: int) -> None:
        with self._lock:
            self._objects.pop(camera_id, None)
            self._hits.pop(camera_id, None)


object_overlays = ObjectOverlayStore()
