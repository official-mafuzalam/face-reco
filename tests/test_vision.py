from pathlib import Path

import numpy as np
import pytest

from app.vision.detector import Face, FaceDetector
from app.vision.embedder import FaceEmbedder
from app.vision.matcher import Gallery
from app.vision.quality import FaceQuality


def _unit(index: int) -> np.ndarray:
    embedding = np.zeros(128, dtype=np.float32)
    embedding[index] = 1.0
    return embedding


def test_gallery_accepts_clear_match() -> None:
    gallery = Gallery(["person-a", "person-b"], np.stack([_unit(0), _unit(1)]))

    person_id, score, second_best = gallery.match(_unit(0))

    assert person_id == "person-a"
    assert score == pytest.approx(1.0)
    assert second_best == pytest.approx(0.0)


def test_gallery_rejects_below_threshold() -> None:
    gallery = Gallery(["person-a"], _unit(0)[None, :], threshold=0.8)
    query = _unit(0) * 0.7 + _unit(1) * np.sqrt(1 - 0.7**2)

    person_id, score, _second_best = gallery.match(query)

    assert person_id is None
    assert score == pytest.approx(0.7)


def test_gallery_rejects_small_best_match_margin() -> None:
    first = _unit(0)
    second = np.zeros(128, dtype=np.float32)
    second[0] = 0.99
    second[1] = np.sqrt(1 - 0.99**2)
    gallery = Gallery(["person-a", "person-b"], np.stack([first, second]), threshold=0.8, margin=0.05)

    person_id, score, second_best = gallery.match(first)

    assert person_id is None
    assert score == pytest.approx(1.0)
    assert second_best == pytest.approx(0.99, abs=1e-5)


def test_gallery_empty() -> None:
    gallery = Gallery([], np.empty((0, 128), dtype=np.float32))

    assert gallery.match(_unit(0)) == (None, 0.0, 0.0)


def test_detector_scales_input_and_maps_faces_to_original(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FakeDetector:
        def setInputSize(self, size: tuple[int, int]) -> None:
            assert size == (1280, 360)

        def detect(self, _image: np.ndarray) -> tuple[None, np.ndarray]:
            row = np.asarray(
                [[100, 50, 200, 100, 130, 65, 270, 65, 200, 100, 145, 130, 255, 130, 0.95]],
                dtype=np.float32,
            )
            return None, row

    monkeypatch.setattr("app.vision.detector.cv2.FaceDetectorYN.create", lambda *_args: FakeDetector())
    detector = FaceDetector(model_path="unused.onnx")

    faces = detector.detect(np.zeros((720, 2560, 3), dtype=np.uint8))

    assert len(faces) == 1
    assert faces[0].box == (200.0, 100.0, 400.0, 200.0)
    np.testing.assert_array_equal(
        faces[0].landmarks5[0],
        np.asarray([260.0, 130.0], dtype=np.float32),
    )


def test_embedder_returns_l2_normalized_float32(monkeypatch: pytest.MonkeyPatch) -> None:
    class FakeRecognizer:
        def alignCrop(self, image: np.ndarray, face_row: np.ndarray) -> np.ndarray:
            assert image.shape == (32, 32, 3)
            assert face_row.shape == (1, 15)
            return image

        def feature(self, _aligned: np.ndarray) -> np.ndarray:
            result = np.zeros((1, 128), dtype=np.float32)
            result[0, :2] = [3.0, 4.0]
            return result

    monkeypatch.setattr("app.vision.embedder.cv2.FaceRecognizerSF.create", lambda *_args: FakeRecognizer())
    embedder = FaceEmbedder(model_path="unused.onnx")
    face = Face(
        box=(1, 2, 20, 20),
        score=0.95,
        landmarks5=np.asarray([[5, 5], [15, 5], [10, 10], [6, 15], [14, 15]], dtype=np.float32),
    )

    embedding = embedder.align_and_embed(np.zeros((32, 32, 3), dtype=np.uint8), face)

    assert embedding.shape == (128,)
    assert embedding.dtype == np.float32
    assert np.linalg.norm(embedding) == pytest.approx(1.0)
    np.testing.assert_array_equal(embedding[:2], np.asarray([0.6, 0.8], dtype=np.float32))


def test_quality_accepts_large_sharp_frontal_face() -> None:
    image = np.random.default_rng(42).integers(0, 256, (200, 200, 3), dtype=np.uint8)
    face = Face(
        box=(20, 20, 140, 150),
        score=0.99,
        landmarks5=np.asarray([[65, 65], [115, 65], [90, 90], [70, 120], [110, 120]], dtype=np.float32),
    )

    ok, reasons = FaceQuality().check(image, face)

    assert ok
    assert reasons == []


def test_quality_rejects_small_low_score_blurry_yawed_face() -> None:
    image = np.full((100, 100, 3), 127, dtype=np.uint8)
    face = Face(
        box=(20, 20, 40, 50),
        score=0.4,
        landmarks5=np.asarray([[25, 30], [55, 30], [52, 40], [30, 55], [50, 55]], dtype=np.float32),
    )

    ok, reasons = FaceQuality().check(image, face)

    assert not ok
    assert {"face_too_small", "detection_score_low", "blurred", "extreme_yaw"} <= set(reasons)


@pytest.mark.models
def test_optional_models_load_and_detect_empty_image() -> None:
    detector_model = Path("models/face_detection_yunet_2023mar.onnx")
    embedder_model = Path("models/face_recognition_sface_2021dec.onnx")
    if not detector_model.is_file() or not embedder_model.is_file():
        pytest.skip("Optional ONNX models are not downloaded.")

    detector = FaceDetector(detector_model)
    FaceEmbedder(embedder_model)
    assert detector.detect(np.zeros((64, 64, 3), dtype=np.uint8)) == []
