import threading
from dataclasses import dataclass
from pathlib import Path

import cv2
import numpy as np
from cryptography.fernet import Fernet
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.camera.manager import manager
from app.config import settings
from app.models import AuditLog, Embedding, Person
from app.vision.detector import Face, FaceDetector
from app.vision.embedder import FaceEmbedder
from app.vision.matcher import Gallery
from app.vision.quality import FaceQuality


_fernet = Fernet(settings.fernet_key.encode())
_gallery_lock = threading.RLock()
_gallery_cache: Gallery | None = None
_detector: FaceDetector | None = None
_embedder: FaceEmbedder | None = None
_quality = FaceQuality()
DUPLICATE_THRESHOLD = 0.363
SAME_PERSON_SIMILARITY = 0.98
MAX_UPLOAD_BYTES = 5 * 1024 * 1024
_MODEL_DIR = Path(__file__).resolve().parents[2] / "models"
_FACES_DIR = Path(__file__).resolve().parents[2] / "data" / "faces"


@dataclass(frozen=True)
class EnrollmentResult:
    accepted: bool
    reasons: tuple[str, ...] = ()
    duplicate_person: tuple[str, str] | None = None
    embedding_id: int | None = None


def _vision_components() -> tuple[FaceDetector, FaceEmbedder, FaceQuality]:
    global _detector, _embedder
    if _detector is None:
        _detector = FaceDetector(_MODEL_DIR / "face_detection_yunet_2023mar.onnx")
    if _embedder is None:
        _embedder = FaceEmbedder(_MODEL_DIR / "face_recognition_sface_2021dec.onnx")
    return _detector, _embedder, _quality


def _decode_vector(vector_encrypted: bytes) -> np.ndarray:
    vector = np.frombuffer(_fernet.decrypt(vector_encrypted), dtype=np.float32)
    if vector.shape != (128,) or not np.isfinite(vector).all():
        raise ValueError("Stored embedding is invalid.")
    return vector.copy()


def rebuild_gallery_cache(db: Session) -> Gallery:
    global _gallery_cache
    rows = list(db.scalars(select(Embedding).order_by(Embedding.id)))
    vectors = [_decode_vector(row.vector_encrypted) for row in rows]
    matrix = np.stack(vectors).astype(np.float32) if vectors else np.empty((0, 128), dtype=np.float32)
    gallery = Gallery([row.person_id for row in rows], matrix)
    with _gallery_lock:
        _gallery_cache = gallery
    return gallery


def clear_gallery_cache() -> None:
    global _gallery_cache
    with _gallery_lock:
        _gallery_cache = None


def _get_gallery(db: Session) -> Gallery:
    with _gallery_lock:
        cached = _gallery_cache
    return cached if cached is not None else rebuild_gallery_cache(db)


def _normalized_embedding(embedding: np.ndarray) -> np.ndarray:
    vector = np.asarray(embedding, dtype=np.float32).reshape(-1)
    if vector.shape != (128,):
        raise ValueError("Embedding must contain 128 values.")
    norm = float(np.linalg.norm(vector))
    if not np.isfinite(norm) or norm == 0:
        raise ValueError("Embedding must be finite and non-zero.")
    return (vector / norm).astype(np.float32)


def _duplicate_result(
    db: Session,
    person_id: int,
    embedding: np.ndarray,
) -> tuple[bool, tuple[str, str] | None]:
    gallery = _get_gallery(db)
    if not gallery.person_ids:
        return False, None
    scores = gallery.embeddings @ embedding

    for existing_person_id, score in zip(gallery.person_ids, scores, strict=True):
        if existing_person_id == person_id and float(score) > SAME_PERSON_SIMILARITY:
            return True, None

    candidates = [
        (int(existing_id), float(score))
        for existing_id, score in zip(gallery.person_ids, scores, strict=True)
        if existing_id != person_id and float(score) >= DUPLICATE_THRESHOLD
    ]
    if not candidates:
        return False, None
    duplicate_id, _score = max(candidates, key=lambda match: match[1])
    duplicate = db.get(Person, duplicate_id)
    return False, (duplicate.code, duplicate.name) if duplicate is not None else None


def _make_thumbnail(bgr: np.ndarray, face: Face) -> np.ndarray:
    x, y, width, height = face.box
    image_height, image_width = bgr.shape[:2]
    left = max(0, int(np.floor(x)))
    top = max(0, int(np.floor(y)))
    right = min(image_width, int(np.ceil(x + width)))
    bottom = min(image_height, int(np.ceil(y + height)))
    if right <= left or bottom <= top:
        raise ValueError("Face crop is outside the source image.")
    return cv2.resize(bgr[top:bottom, left:right], (112, 112), interpolation=cv2.INTER_AREA)


def enroll_image(
    db: Session,
    person: Person,
    created_by: int,
    bgr: np.ndarray,
    *,
    detector: object | None = None,
    embedder: object | None = None,
    quality: object | None = None,
) -> EnrollmentResult:
    if bgr is None or not isinstance(bgr, np.ndarray) or bgr.ndim != 3 or bgr.shape[2] != 3:
        return EnrollmentResult(False, ("invalid_image",))
    if detector is None or embedder is None or quality is None:
        default_detector, default_embedder, default_quality = _vision_components()
        detector = detector or default_detector
        embedder = embedder or default_embedder
        quality = quality or default_quality

    faces: list[Face] = detector.detect(bgr)
    if len(faces) != 1:
        return EnrollmentResult(False, ("exactly_one_face_required",))
    face = faces[0]
    quality_ok, reasons = quality.check(bgr, face)
    if not quality_ok:
        return EnrollmentResult(False, tuple(reasons))

    vector = _normalized_embedding(embedder.align_and_embed(bgr, face))
    similar, duplicate = _duplicate_result(db, person.id, vector)
    if similar:
        return EnrollmentResult(False, ("sample_too_similar",))
    if duplicate is not None:
        return EnrollmentResult(False, ("duplicate_person",), duplicate_person=duplicate)

    thumbnail = _make_thumbnail(bgr, face)
    record = Embedding(
        person_id=person.id,
        vector_encrypted=_fernet.encrypt(vector.tobytes()),
        created_by=created_by,
    )
    db.add(record)
    db.flush()
    person_dir = _FACES_DIR / str(person.id)
    thumb_path = person_dir / f"{record.id}.jpg"
    person_dir.mkdir(parents=True, exist_ok=True)
    if not cv2.imwrite(str(thumb_path), thumbnail):
        db.rollback()
        raise OSError("Unable to save enrollment thumbnail.")
    record.thumb_path = thumb_path.relative_to(_FACES_DIR.parent).as_posix()
    db.add(
        AuditLog(
            user_id=created_by,
            action="person.enroll",
            detail=f"person_id={person.id}; embedding_id={record.id}",
        )
    )
    try:
        db.commit()
    except Exception:
        db.rollback()
        thumb_path.unlink(missing_ok=True)
        raise
    rebuild_gallery_cache(db)
    return EnrollmentResult(True, embedding_id=record.id)


def clear_person_samples(db: Session, person: Person, created_by: int) -> int:
    records = list(db.scalars(select(Embedding).where(Embedding.person_id == person.id)))
    faces_root = _FACES_DIR.resolve()
    paths: list[Path] = []
    for record in records:
        if not record.thumb_path:
            continue
        candidate = (faces_root.parent / record.thumb_path).resolve()
        if candidate == faces_root or faces_root not in candidate.parents:
            raise ValueError("Enrollment thumbnail path is outside the faces directory.")
        paths.append(candidate)

    for record in records:
        db.delete(record)
    db.add(
        AuditLog(
            user_id=created_by,
            action="person.enroll.delete",
            detail=f"person_id={person.id}; samples={len(records)}",
        )
    )
    db.commit()
    for path in paths:
        path.unlink(missing_ok=True)
    rebuild_gallery_cache(db)
    return len(records)


def decode_upload(data: bytes, filename: str | None) -> np.ndarray:
    extension = Path(filename or "").suffix.lower()
    if extension not in {".jpg", ".jpeg", ".png"}:
        raise ValueError("unsupported_image_type")
    if not data or len(data) > MAX_UPLOAD_BYTES:
        raise ValueError("image_size_invalid")
    image = cv2.imdecode(np.frombuffer(data, dtype=np.uint8), cv2.IMREAD_COLOR)
    if image is None:
        raise ValueError("invalid_image")
    return image


def camera_frame(camera_id: int) -> np.ndarray | None:
    return manager.get_latest_frame(camera_id)


def thumbnail_path(record: Embedding) -> Path | None:
    if not record.thumb_path:
        return None
    root = _FACES_DIR.resolve()
    path = (root.parent / record.thumb_path).resolve()
    if root not in path.parents:
        raise ValueError("Enrollment thumbnail path is outside the faces directory.")
    return path
