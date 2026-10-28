import logging
import os
import re
import threading
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit

os.environ["OPENCV_FFMPEG_CAPTURE_OPTIONS"] = "rtsp_transport;tcp|stimeout;5000000"

import cv2
import numpy as np

if hasattr(cv2, "setLogLevel"):
    cv2.setLogLevel(getattr(cv2, "LOG_LEVEL_SILENT", 0))


logger = logging.getLogger(__name__)
Status = Literal["connecting", "online", "offline"]
READ_TIMEOUT_SECONDS = 10.0
BACKOFF_START_SECONDS = 1.0
BACKOFF_MAX_SECONDS = 30.0


def mask_credentials(value: str) -> str:
    return re.sub(
        r"(?i)([a-z][a-z0-9+.-]*://)([^/@\s]+)@",
        r"\1***:***@",
        value,
    )


def capture_source(source: str) -> tuple[cv2.VideoCapture, bool]:
    if source.startswith("webcam:"):
        index_text = source.removeprefix("webcam:")
        if not index_text.isdigit():
            raise ValueError("Invalid webcam source.")
        return cv2.VideoCapture(int(index_text)), False
    if source.startswith("file:"):
        path = source.removeprefix("file:")
        if not path:
            raise ValueError("Video file path is empty.")
        return cv2.VideoCapture(str(Path(path))), True
    if source.lower().startswith("rtsp://"):
        return cv2.VideoCapture(source, cv2.CAP_FFMPEG), False
    raise ValueError("Source must be webcam:<index>, file:<path>, or rtsp://.")


@dataclass(frozen=True)
class ReaderState:
    status: Status
    fps: float
    last_frame_at: datetime | None
    last_error: str | None
    attempts: int


class CameraReader:
    def __init__(self, camera_id: int, source: str) -> None:
        self.camera_id = camera_id
        self.source = source
        self._lock = threading.Lock()
        self._stop_event = threading.Event()
        self._thread = threading.Thread(
            target=self._run,
            name=f"camera-reader-{camera_id}",
            daemon=True,
        )
        self._latest_frame: np.ndarray | None = None
        self._status: Status = "connecting"
        self._fps = 0.0
        self._last_frame_at: datetime | None = None
        self._last_frame_monotonic: float | None = None
        self._started_monotonic: float | None = None
        self._last_error: str | None = None
        self._attempts = 0

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stop_event.set()
        if self._thread.is_alive() and self._thread is not threading.current_thread():
            self._thread.join(timeout=6)

    def latest_frame(self) -> np.ndarray | None:
        with self._lock:
            return self._latest_frame.copy() if self._latest_frame is not None else None

    def state(self) -> ReaderState:
        with self._lock:
            status = self._status
            last_frame_at = self._last_frame_at
            last_error = self._last_error
            if (
                last_frame_at is not None
                and self._last_frame_monotonic is not None
                and time.monotonic() - self._last_frame_monotonic > READ_TIMEOUT_SECONDS
            ):
                status = "offline"
                last_error = last_error or "No frame received within 10 seconds."
            elif (
                last_frame_at is None
                and self._started_monotonic is not None
                and time.monotonic() - self._started_monotonic > READ_TIMEOUT_SECONDS
            ):
                status = "offline"
                last_error = last_error or "No frame received within 10 seconds."
            return ReaderState(status, self._fps, last_frame_at, last_error, self._attempts)

    def _set_failure(self, error: str) -> None:
        safe_error = mask_credentials(error)
        with self._lock:
            self._status = "offline"
            self._last_error = safe_error
        logger.warning("Camera %s offline: %s", self.camera_id, safe_error)

    def _run(self) -> None:
        self._started_monotonic = time.monotonic()
        backoff = BACKOFF_START_SECONDS
        is_file = self.source.startswith("file:")
        while not self._stop_event.is_set():
            with self._lock:
                self._status = "connecting"
                self._attempts += 1
            capture: cv2.VideoCapture | None = None
            try:
                capture, source_is_file = capture_source(self.source)
                is_file = source_is_file
                if not capture.isOpened():
                    self._set_failure(f"Unable to open source {mask_credentials(self.source)}")
                    capture.release()
                    capture = None
                    if self._stop_event.wait(backoff):
                        break
                    backoff = min(backoff * 2, BACKOFF_MAX_SECONDS)
                    continue

                backoff = BACKOFF_START_SECONDS
                frame_count = 0
                fps_started = time.monotonic()
                while not self._stop_event.is_set():
                    try:
                        ok, frame = capture.read()
                    except Exception as exc:
                        self._set_failure(mask_credentials(str(exc)))
                        break
                    if not ok or frame is None:
                        if is_file and capture.set(cv2.CAP_PROP_POS_FRAMES, 0):
                            continue
                        self._set_failure("No frame received from source.")
                        break

                    now = time.monotonic()
                    frame_count += 1
                    elapsed = now - fps_started
                    with self._lock:
                        self._latest_frame = frame
                        self._last_frame_at = datetime.now(UTC)
                        self._last_frame_monotonic = now
                        self._status = "online"
                        self._last_error = None
                        if elapsed >= 1.0:
                            self._fps = frame_count / elapsed
                            frame_count = 0
                            fps_started = now

                if self._stop_event.is_set():
                    break
            except Exception as exc:
                self._set_failure(mask_credentials(str(exc)))
            finally:
                if capture is not None:
                    capture.release()

            if not self._stop_event.wait(backoff):
                backoff = min(backoff * 2, BACKOFF_MAX_SECONDS)
