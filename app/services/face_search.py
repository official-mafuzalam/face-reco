"""Find where a face was seen before, and turn an unknown face into an enrolled person."""

from dataclasses import dataclass
from pathlib import Path

import cv2
import numpy as np
from cryptography.fernet import Fernet, InvalidToken
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import settings
from app.models import Embedding, Event, Person
from app.services.enroll import EnrollmentResult, _vision_components, enroll_face
from app.vision.detector import Face

_fernet = Fernet(settings.fernet_key.encode())
PROJECT_ROOT = Path(__file__).resolve().parents[2]
SCAN_LIMIT = 20000  # newest unknown events compared; plenty for a small site
# The stored embedding and the face found again in the saved snapshot are the same image,
# so they agree almost exactly; anything lower means another face in the picture.
SNAPSHOT_FACE_MATCH = 0.8


@dataclass(frozen=True)
class SimilarEvent:
    event: Event
    score: float


@dataclass(frozen=True)
class SimilarPerson:
    person: Person
    score: float


def decrypt_vector(token: bytes | None) -> np.ndarray | None:
    if not token:
        return None
    try:
        vector = np.frombuffer(_fernet.decrypt(token), dtype=np.float32).copy()
    except InvalidToken:
        return None
    norm = float(np.linalg.norm(vector))
    if vector.shape != (128,) or not np.isfinite(norm) or norm == 0:
        return None
    return vector / norm


def similar_events(
    db: Session,
    query: np.ndarray,
    *,
    threshold: float | None = None,
    limit: int = 100,
    exclude_id: int | None = None,
) -> list[SimilarEvent]:
    threshold = settings.match_threshold if threshold is None else threshold
    rows = db.scalars(
        select(Event)
        .where(Event.embedding_encrypted.is_not(None))
        .order_by(Event.at.desc())
        .limit(SCAN_LIMIT)
    ).all()
    found = []
    for event in rows:
        if event.id == exclude_id:
            continue
        vector = decrypt_vector(event.embedding_encrypted)
        if vector is None:
            continue
        score = float(vector @ query)
        if score >= threshold:
            found.append(SimilarEvent(event, score))
    found.sort(key=lambda item: item.score, reverse=True)
    return found[:limit]


def similar_people(db: Session, query: np.ndarray, *, threshold: float | None = None, limit: int = 5) -> list[SimilarPerson]:
    """Enrolled people this face resembles (best sample per person)."""
    threshold = settings.match_threshold if threshold is None else threshold
    best: dict[int, float] = {}
    for person_id, token in db.execute(select(Embedding.person_id, Embedding.vector_encrypted)).all():
        vector = decrypt_vector(token)
        if vector is None:
            continue
        score = float(vector @ query)
        if score >= threshold and score > best.get(person_id, -1.0):
            best[person_id] = score
    people = {person.id: person for person in db.scalars(select(Person).where(Person.id.in_(best)))} if best else {}
    ranked = sorted(best.items(), key=lambda item: item[1], reverse=True)[:limit]
    return [SimilarPerson(people[pid], score) for pid, score in ranked if pid in people]


def embedding_from_image(bgr: np.ndarray, detector=None, embedder=None) -> np.ndarray:
    """One clear face in an uploaded photo -> normalised embedding. Raises ValueError(code)."""
    if detector is None or embedder is None:
        default_detector, default_embedder, _quality = _vision_components()
        detector, embedder = detector or default_detector, embedder or default_embedder
    faces = detector.detect(bgr)
    if len(faces) != 1:
        raise ValueError("exactly_one_face_required")
    vector = np.asarray(embedder.align_and_embed(bgr, faces[0]), dtype=np.float32).reshape(-1)
    return vector / float(np.linalg.norm(vector))


def snapshot_image(event: Event, root: Path = PROJECT_ROOT) -> np.ndarray | None:
    if not event.snapshot_path:
        return None
    snapshots = (root / "data" / "snapshots").resolve()
    path = (root / event.snapshot_path).resolve()
    if snapshots not in path.parents:
        return None
    image = cv2.imread(str(path))
    return image


def enroll_from_event(
    db: Session,
    event: Event,
    person: Person,
    created_by: int,
    *,
    detector=None,
    embedder=None,
    root: Path = PROJECT_ROOT,
) -> EnrollmentResult:
    """Add the unknown face of `event` to `person` as a face sample.

    The snapshot may show several people, so the face whose embedding matches the one
    saved with the event is the one enrolled.
    """
    stored = decrypt_vector(event.embedding_encrypted)
    image = snapshot_image(event, root)
    if stored is None or image is None:
        return EnrollmentResult(False, ("face_source_missing",))
    if detector is None or embedder is None:
        default_detector, default_embedder, _quality = _vision_components()
        detector, embedder = detector or default_detector, embedder or default_embedder
    best: tuple[float, Face, np.ndarray] | None = None
    for face in detector.detect(image):
        vector = np.asarray(embedder.align_and_embed(image, face), dtype=np.float32).reshape(-1)
        vector = vector / float(np.linalg.norm(vector))
        score = float(vector @ stored)
        if best is None or score > best[0]:
            best = (score, face, vector)
    if best is None or best[0] < SNAPSHOT_FACE_MATCH:
        return EnrollmentResult(False, ("face_source_missing",))
    _score, face, _vector = best
    # Enrol the embedding the camera actually produced at the gate, not the re-detected one.
    return enroll_face(db, person, created_by, image, face, stored)
