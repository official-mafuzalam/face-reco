from pathlib import Path

import cv2
import numpy as np

from app.vision.detector import Face


class FaceEmbedder:
    def __init__(
        self,
        model_path: str | Path = "models/face_recognition_sface_2021dec.onnx",
    ) -> None:
        self._recognizer = cv2.FaceRecognizerSF.create(str(model_path), "")

    def align_and_embed(self, bgr: np.ndarray, face: Face) -> np.ndarray:
        landmarks = np.asarray(face.landmarks5, dtype=np.float32).reshape(5, 2)
        x, y, width, height = face.box
        face_row = np.concatenate(
            (
                np.asarray([x, y, width, height], dtype=np.float32),
                landmarks.reshape(10),
                np.asarray([face.score], dtype=np.float32),
            )
        ).reshape(1, 15)
        aligned = self._recognizer.alignCrop(bgr, face_row)
        embedding = np.asarray(self._recognizer.feature(aligned), dtype=np.float32).reshape(-1)
        if embedding.shape != (128,):
            raise ValueError(f"SFace returned an embedding with {embedding.size} values; expected 128.")
        norm = float(np.linalg.norm(embedding))
        if not np.isfinite(norm) or norm == 0:
            raise ValueError("SFace returned an invalid embedding.")
        return (embedding / norm).astype(np.float32)

    def align_embed(self, bgr: np.ndarray, face: Face) -> np.ndarray:
        return self.align_and_embed(bgr, face)
