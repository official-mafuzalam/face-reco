import cv2
import numpy as np


class MotionGate:
    """Cheap frame-difference check so object detection only runs when something moves."""

    def __init__(self, threshold: float = 0.003, width: int = 160) -> None:
        self.threshold = threshold
        self.width = width
        self._previous: np.ndarray | None = None

    def moved(self, frame: np.ndarray) -> bool:
        height = max(1, int(frame.shape[0] * self.width / frame.shape[1]))
        small = cv2.resize(frame, (self.width, height), interpolation=cv2.INTER_AREA)
        gray = cv2.GaussianBlur(cv2.cvtColor(small, cv2.COLOR_BGR2GRAY), (5, 5), 0)
        previous, self._previous = self._previous, gray
        if previous is None or previous.shape != gray.shape:
            return True
        changed = cv2.absdiff(previous, gray) > 25
        return float(np.count_nonzero(changed)) / changed.size >= self.threshold
