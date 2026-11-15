"""YOLOX object detector (OpenCV model zoo, Apache-2.0) running on OpenCV DNN."""

import threading
from dataclasses import dataclass
from pathlib import Path

import cv2
import numpy as np


COCO_CLASSES = (
    "person", "bicycle", "car", "motorcycle", "airplane", "bus", "train", "truck", "boat",
    "traffic light", "fire hydrant", "stop sign", "parking meter", "bench", "bird", "cat", "dog",
    "horse", "sheep", "cow", "elephant", "bear", "zebra", "giraffe", "backpack", "umbrella",
    "handbag", "tie", "suitcase", "frisbee", "skis", "snowboard", "sports ball", "kite",
    "baseball bat", "baseball glove", "skateboard", "surfboard", "tennis racket", "bottle",
    "wine glass", "cup", "fork", "knife", "spoon", "bowl", "banana", "apple", "sandwich", "orange",
    "broccoli", "carrot", "hot dog", "pizza", "donut", "cake", "chair", "couch", "potted plant",
    "bed", "dining table", "toilet", "tv", "laptop", "mouse", "remote", "keyboard", "cell phone",
    "microwave", "oven", "toaster", "sink", "refrigerator", "book", "clock", "vase", "scissors",
    "teddy bear", "hair drier", "toothbrush",
)

# Classes the surveillance pipeline cares about, grouped into the categories shown to users.
CATEGORY_BY_CLASS = {
    "person": "person",
    "bicycle": "vehicle",
    "car": "vehicle",
    "motorcycle": "vehicle",
    "bus": "vehicle",
    "truck": "vehicle",
    "backpack": "bag",
    "handbag": "bag",
    "suitcase": "bag",
}


@dataclass(frozen=True)
class ObjectDetection:
    box: tuple[float, float, float, float]  # x, y, width, height in frame pixels
    label: str
    category: str
    score: float


def _grids(size: int) -> tuple[np.ndarray, np.ndarray]:
    grids, strides = [], []
    for stride in (8, 16, 32):
        cells = size // stride
        xv, yv = np.meshgrid(np.arange(cells), np.arange(cells))
        grid = np.stack((xv, yv), 2).reshape(-1, 2)
        grids.append(grid)
        strides.append(np.full((grid.shape[0], 1), stride))
    return np.concatenate(grids).astype(np.float32), np.concatenate(strides).astype(np.float32)


def letterbox(frame: np.ndarray, size: int) -> tuple[np.ndarray, float]:
    ratio = min(size / frame.shape[0], size / frame.shape[1])
    width, height = int(frame.shape[1] * ratio), int(frame.shape[0] * ratio)
    padded = np.full((size, size, 3), 114.0, dtype=np.float32)
    padded[:height, :width] = cv2.resize(frame, (width, height), interpolation=cv2.INTER_LINEAR)
    return padded, ratio


def decode(
    raw: np.ndarray,
    ratio: float,
    frame_shape: tuple[int, ...],
    grids: np.ndarray,
    strides: np.ndarray,
    confidence: float,
    nms: float,
) -> list[ObjectDetection]:
    dets = np.array(raw, dtype=np.float32).reshape(-1, 85)
    centers = (dets[:, :2] + grids) * strides
    sizes = np.exp(np.clip(dets[:, 2:4], -10, 10)) * strides
    scores = dets[:, 4:5] * dets[:, 5:]
    class_ids = np.argmax(scores, axis=1)
    best = scores[np.arange(len(scores)), class_ids]
    wanted = np.array([COCO_CLASSES[i] in CATEGORY_BY_CLASS for i in class_ids]) & (best >= confidence)
    if not wanted.any():
        return []
    centers, sizes, best, class_ids = centers[wanted], sizes[wanted], best[wanted], class_ids[wanted]
    xywh = np.concatenate((centers - sizes / 2, sizes), axis=1) / ratio
    keep = cv2.dnn.NMSBoxesBatched(
        xywh.tolist(), best.tolist(), class_ids.tolist(), confidence, nms
    )
    height, width = frame_shape[:2]
    detections = []
    for index in np.array(keep).reshape(-1):
        x, y, w, h = xywh[index]
        x0, y0 = max(0.0, float(x)), max(0.0, float(y))
        x1, y1 = min(float(width), float(x + w)), min(float(height), float(y + h))
        if x1 <= x0 or y1 <= y0:
            continue
        label = COCO_CLASSES[int(class_ids[index])]
        detections.append(
            ObjectDetection((x0, y0, x1 - x0, y1 - y0), label, CATEGORY_BY_CLASS[label], float(best[index]))
        )
    return detections


class ObjectDetector:
    """Thread-safe wrapper; one network is shared by every camera."""

    def __init__(self, model_path: Path, input_size: int = 640) -> None:
        if not Path(model_path).is_file():
            raise FileNotFoundError(
                f"Object model not found: {model_path}. Run: python -m tools.download_models"
            )
        self.input_size = input_size
        self._net = cv2.dnn.readNet(str(model_path))
        self._grids, self._strides = _grids(input_size)
        self._lock = threading.Lock()

    def detect(
        self, frame: np.ndarray, confidence: float = 0.45, nms: float = 0.5
    ) -> list[ObjectDetection]:
        rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
        padded, ratio = letterbox(rgb, self.input_size)
        blob = np.transpose(padded, (2, 0, 1))[np.newaxis]
        with self._lock:
            self._net.setInput(blob)
            raw = self._net.forward()
        return decode(raw, ratio, frame.shape, self._grids, self._strides, confidence, nms)
