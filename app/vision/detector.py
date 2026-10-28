from dataclasses import dataclass
from pathlib import Path

import cv2
import numpy as np


@dataclass(frozen=True)
class Face:
    box: tuple[float, float, float, float]
    score: float
    landmarks5: np.ndarray


class FaceDetector:
    def __init__(
        self,
        model_path: str | Path = "models/face_detection_yunet_2023mar.onnx",
        score_threshold: float = 0.9,
        max_side: int = 1280,
    ) -> None:
        if max_side < 1:
            raise ValueError("max_side must be positive.")
        self.score_threshold = score_threshold
        self.max_side = max_side
        self._detector = cv2.FaceDetectorYN.create(
            str(model_path),
            "",
            (320, 320),
            score_threshold,
            0.3,
            5000,
        )

    def detect(self, bgr: np.ndarray) -> list[Face]:
        if bgr is None or bgr.ndim != 3 or bgr.shape[2] != 3:
            raise ValueError("bgr must be a non-empty BGR image.")
        height, width = bgr.shape[:2]
        if width < 1 or height < 1:
            raise ValueError("bgr must be a non-empty BGR image.")

        scale = min(1.0, self.max_side / max(width, height))
        if scale < 1.0:
            resized = cv2.resize(
                bgr,
                (max(1, round(width * scale)), max(1, round(height * scale))),
                interpolation=cv2.INTER_AREA,
            )
        else:
            resized = bgr

        resized_height, resized_width = resized.shape[:2]
        self._detector.setInputSize((resized_width, resized_height))
        _result, rows = self._detector.detect(resized)
        if rows is None:
            return []

        scale_x = resized_width / width
        scale_y = resized_height / height
        faces: list[Face] = []
        for row in rows:
            values = np.asarray(row, dtype=np.float32)
            if values.size < 15:
                continue
            box = (
                float(values[0] / scale_x),
                float(values[1] / scale_y),
                float(values[2] / scale_x),
                float(values[3] / scale_y),
            )
            landmarks = values[4:14].reshape(5, 2).copy()
            landmarks[:, 0] /= scale_x
            landmarks[:, 1] /= scale_y
            faces.append(Face(box=box, score=float(values[14]), landmarks5=landmarks))
        return faces
