"""Throughput numbers for the analysis loops, shown on the health page."""

import threading
import time
from collections import deque
from dataclasses import dataclass


@dataclass(frozen=True)
class LoopSnapshot:
    fps: float  # analysed frames per second over the recent window
    avg_ms: float  # average time spent analysing one frame
    last_at: float | None  # monotonic time of the latest analysed frame


class LoopStats:
    def __init__(self, window_seconds: float = 10.0) -> None:
        self._window = window_seconds
        self._lock = threading.Lock()
        self._samples: deque[tuple[float, float]] = deque()  # (finished_at, duration)

    def record(self, duration: float, now: float | None = None) -> None:
        finished = time.monotonic() if now is None else now
        with self._lock:
            self._samples.append((finished, duration))
            self._trim(finished)

    def snapshot(self, now: float | None = None) -> LoopSnapshot:
        current = time.monotonic() if now is None else now
        with self._lock:
            self._trim(current)
            samples = list(self._samples)
        if not samples:
            return LoopSnapshot(0.0, 0.0, None)
        fps = len(samples) / self._window
        avg_ms = 1000.0 * sum(duration for _, duration in samples) / len(samples)
        return LoopSnapshot(fps, avg_ms, samples[-1][0])

    def _trim(self, now: float) -> None:
        while self._samples and now - self._samples[0][0] > self._window:
            self._samples.popleft()
