from pathlib import Path
import numpy as np
import pytest

from app.surveillance.detector import (
    PROFILES,
    HardwareProfile,
    ObjectDetector,
    ONNXRuntimeBackend,
    OpenCVDNNBackend,
    detect_hardware,
)

MODEL_PATH = Path("models/object_detection_yolox_2022nov.onnx")


def test_detect_hardware():
    hw = detect_hardware()
    assert "cpu_cores" in hw
    assert "recommended_profile" in hw
    assert hw["recommended_profile"] in PROFILES


def test_hardware_profiles_definitions():
    assert "cpu_small" in PROFILES
    assert "cpu_large" in PROFILES
    assert "openvino" in PROFILES
    assert "cuda" in PROFILES

    for name, profile in PROFILES.items():
        assert isinstance(profile, HardwareProfile)
        assert profile.fps > 0
        assert profile.input_size in (416, 640)
        assert profile.max_cameras >= 4


@pytest.mark.models
def test_detector_backends_parity():
    if not MODEL_PATH.exists():
        pytest.skip("YOLOX model not downloaded")

    dummy_frame = np.zeros((480, 640, 3), dtype=np.uint8)

    # 1. OpenCV DNN Backend
    detector_cv = ObjectDetector(MODEL_PATH, backend="opencv")
    dets_cv = detector_cv.detect(dummy_frame)
    assert isinstance(dets_cv, list)

    # 2. ONNX Runtime Backend
    detector_ort = ObjectDetector(MODEL_PATH, backend="onnxruntime")
    dets_ort = detector_ort.detect(dummy_frame)
    assert isinstance(dets_ort, list)
    assert detector_ort.backend_type == "ONNXRuntimeBackend"


def test_detector_missing_model_raises():
    with pytest.raises(FileNotFoundError):
        ObjectDetector(Path("models/non_existent_model.onnx"))
