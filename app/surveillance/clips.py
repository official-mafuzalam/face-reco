import logging
import subprocess
import threading
from collections import deque
from pathlib import Path
from typing import Protocol

import cv2
import numpy as np


logger = logging.getLogger(__name__)


def ffmpeg_executable() -> str | None:
    try:
        import imageio_ffmpeg

        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:
        return None


def fit_width(frame: np.ndarray, max_width: int) -> np.ndarray:
    height, width = frame.shape[:2]
    if width > max_width:
        height = int(height * max_width / width)
        width = max_width
    # H.264 (yuv420p) and VP8 want even dimensions.
    width, height = width - width % 2, height - height % 2
    if (width, height) != (frame.shape[1], frame.shape[0]):
        frame = cv2.resize(frame, (width, height), interpolation=cv2.INTER_AREA)
    return frame


class _Writer(Protocol):
    def write(self, frame: np.ndarray) -> None: ...

    def close(self) -> None: ...


class _FfmpegWriter:
    """H.264 MP4 through the FFmpeg binary bundled with imageio-ffmpeg (small, plays everywhere)."""

    def __init__(self, executable: str, path: Path, fps: float, size: tuple[int, int], crf: int) -> None:
        width, height = size
        self._process = subprocess.Popen(
            [
                executable, "-hide_banner", "-loglevel", "error", "-y",
                "-f", "rawvideo", "-pix_fmt", "bgr24", "-s", f"{width}x{height}", "-r", f"{fps:g}",
                "-i", "-",
                "-c:v", "libx264", "-preset", "veryfast", "-crf", str(crf),
                "-pix_fmt", "yuv420p", "-movflags", "+faststart",
                str(path),
            ],
            stdin=subprocess.PIPE,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )

    def write(self, frame: np.ndarray) -> None:
        self._process.stdin.write(np.ascontiguousarray(frame).tobytes())

    def close(self) -> None:
        try:
            self._process.stdin.close()
        except OSError:
            pass
        try:
            self._process.wait(timeout=30)
        except subprocess.TimeoutExpired:
            self._process.kill()
        if self._process.returncode:
            error = self._process.stderr.read().decode("utf-8", "replace").strip()
            logger.error("FFmpeg clip encoding failed: %s", error[-500:])


class _OpenCvWriter:
    """Fallback: VP8 WebM through OpenCV (no bitrate control, larger files)."""

    def __init__(self, path: Path, fps: float, size: tuple[int, int]) -> None:
        self._writer = cv2.VideoWriter(str(path), cv2.VideoWriter_fourcc(*"VP80"), fps, size)
        if not self._writer.isOpened():
            raise OSError(f"Unable to open clip writer for {path}")

    def write(self, frame: np.ndarray) -> None:
        self._writer.write(frame)

    def close(self) -> None:
        self._writer.release()


class ClipRecorder:
    """Keeps a short pre-roll of frames and writes clips that browsers can play."""

    def __init__(
        self,
        fps: float,
        pre_seconds: float,
        max_width: int = 640,
        *,
        crf: int = 28,
        use_ffmpeg: bool = True,
    ) -> None:
        self.fps = fps
        self.max_width = max_width
        self.crf = crf
        self._ffmpeg = ffmpeg_executable() if use_ffmpeg else None
        self._ring: deque[np.ndarray] = deque(maxlen=max(1, int(fps * pre_seconds)))
        self._writer: _Writer | None = None
        self._size: tuple[int, int] | None = None
        self._lock = threading.Lock()

    @property
    def extension(self) -> str:
        return ".mp4" if self._ffmpeg else ".webm"

    @property
    def recording(self) -> bool:
        return self._writer is not None

    def push(self, frame: np.ndarray) -> None:
        frame = fit_width(frame, self.max_width)
        with self._lock:
            self._ring.append(frame)
            if self._writer is not None:
                self._write(frame)

    def start(self, base_path: Path) -> Path | None:
        """Start a clip at `base_path` + extension; returns the file path, or None if not started."""
        with self._lock:
            if self._writer is not None or not self._ring:
                return None
            path = base_path.with_suffix(self.extension)
            path.parent.mkdir(parents=True, exist_ok=True)
            height, width = self._ring[-1].shape[:2]
            try:
                if self._ffmpeg:
                    writer: _Writer = _FfmpegWriter(self._ffmpeg, path, self.fps, (width, height), self.crf)
                else:
                    writer = _OpenCvWriter(path, self.fps, (width, height))
            except OSError:
                logger.exception("Unable to start clip %s", path)
                return None
            self._writer, self._size = writer, (width, height)
            for frame in self._ring:
                self._write(frame)
            return path

    def stop(self) -> None:
        with self._lock:
            writer = self._writer
            self._writer, self._size = None, None
        if writer is not None:
            writer.close()

    def _write(self, frame: np.ndarray) -> None:
        if self._size and (frame.shape[1], frame.shape[0]) != self._size:
            frame = cv2.resize(frame, self._size, interpolation=cv2.INTER_AREA)
        try:
            self._writer.write(frame)
        except OSError:
            logger.exception("Clip writer failed; stopping this clip.")
            writer, self._writer, self._size = self._writer, None, None
            threading.Thread(target=writer.close, daemon=True).start()
