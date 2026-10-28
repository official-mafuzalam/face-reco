import threading
from collections.abc import Iterable

import numpy as np

from app.camera.reader import CameraReader, ReaderState


class CameraManager:
    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._readers: dict[int, CameraReader] = {}

    def start(self, camera_id: int, source: str) -> None:
        self.stop(camera_id)
        reader = CameraReader(camera_id, source)
        with self._lock:
            self._readers[camera_id] = reader
        reader.start()

    def stop(self, camera_id: int) -> None:
        with self._lock:
            reader = self._readers.pop(camera_id, None)
        if reader is not None:
            reader.stop()

    def restart(self, camera_id: int, source: str) -> None:
        self.start(camera_id, source)

    def start_enabled(self, cameras: Iterable[tuple[int, str]]) -> None:
        for camera_id, source in cameras:
            self.start(camera_id, source)

    def stop_all(self) -> None:
        with self._lock:
            camera_ids = list(self._readers)
        for camera_id in camera_ids:
            self.stop(camera_id)

    def get_latest_frame(self, camera_id: int) -> np.ndarray | None:
        with self._lock:
            reader = self._readers.get(camera_id)
        return reader.latest_frame() if reader is not None else None

    def get_state(self, camera_id: int) -> ReaderState:
        with self._lock:
            reader = self._readers.get(camera_id)
        if reader is None:
            return ReaderState("offline", 0.0, None, None, 0)
        return reader.state()


manager = CameraManager()
