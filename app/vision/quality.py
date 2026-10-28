import cv2
import numpy as np

from app.vision.detector import Face


class FaceQuality:
    def __init__(
        self,
        min_face_width: float = 80,
        min_detection_score: float = 0.9,
        min_blur_variance: float = 50.0,
        max_yaw_imbalance: float = 0.45,
    ) -> None:
        self.min_face_width = min_face_width
        self.min_detection_score = min_detection_score
        self.min_blur_variance = min_blur_variance
        self.max_yaw_imbalance = max_yaw_imbalance

    def check(self, bgr: np.ndarray, face: Face) -> tuple[bool, list[str]]:
        reasons: list[str] = []
        if face.box[2] < self.min_face_width:
            reasons.append("face_too_small")
        if face.score < self.min_detection_score:
            reasons.append("detection_score_low")

        x, y, width, height = face.box
        image_height, image_width = bgr.shape[:2]
        left = max(0, int(np.floor(x)))
        top = max(0, int(np.floor(y)))
        right = min(image_width, int(np.ceil(x + width)))
        bottom = min(image_height, int(np.ceil(y + height)))
        if right <= left or bottom <= top:
            reasons.append("face_out_of_bounds")
        else:
            face_crop = bgr[top:bottom, left:right]
            gray = cv2.cvtColor(face_crop, cv2.COLOR_BGR2GRAY)
            blur_variance = float(cv2.Laplacian(gray, cv2.CV_64F).var())
            if blur_variance < self.min_blur_variance:
                reasons.append("blurred")

        landmarks = np.asarray(face.landmarks5, dtype=np.float32).reshape(5, 2)
        left_eye_x, right_eye_x, nose_x = landmarks[0, 0], landmarks[1, 0], landmarks[2, 0]
        left_distance = nose_x - left_eye_x
        right_distance = right_eye_x - nose_x
        total_distance = left_distance + right_distance
        if total_distance <= 0:
            reasons.append("invalid_landmarks")
        elif abs(left_distance - right_distance) / total_distance > self.max_yaw_imbalance:
            reasons.append("extreme_yaw")

        return not reasons, reasons


def check(
    bgr: np.ndarray,
    face: Face,
    min_face_width: float = 80,
    min_detection_score: float = 0.9,
    min_blur_variance: float = 50.0,
    max_yaw_imbalance: float = 0.45,
) -> tuple[bool, list[str]]:
    return FaceQuality(
        min_face_width=min_face_width,
        min_detection_score=min_detection_score,
        min_blur_variance=min_blur_variance,
        max_yaw_imbalance=max_yaw_imbalance,
    ).check(bgr, face)
