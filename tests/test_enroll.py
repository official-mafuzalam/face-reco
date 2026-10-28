from collections.abc import Generator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import AsyncIterator

import numpy as np
import pytest
import cv2
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.auth import hash_password
from app.db import Base, get_db
from app.main import app
from app.models import AuditLog, Embedding, Person, User
from app.services import enroll
from app.vision.detector import Face


@pytest.fixture
def db_session(monkeypatch: pytest.MonkeyPatch) -> Generator[Session, None, None]:
    test_engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(test_engine)
    test_session = sessionmaker(bind=test_engine, autoflush=False, expire_on_commit=False)

    @asynccontextmanager
    async def test_lifespan(_app: FastAPI) -> AsyncIterator[None]:
        yield

    monkeypatch.setattr(app.router, "lifespan_context", test_lifespan)
    enroll.clear_gallery_cache()
    with test_session() as db:
        db.add_all(
            [
                User(id=1, username="enroll-admin", password_hash=hash_password("test-password-123"), role="admin"),
                User(id=2, username="enroll-security", password_hash=hash_password("test-password-123"), role="security"),
                Person(id=1, code="P-001", name="One"),
                Person(id=2, code="P-002", name="Two"),
            ]
        )
        db.commit()
        yield db
    enroll.clear_gallery_cache()
    test_engine.dispose()


class FakeDetector:
    def __init__(self, faces: list[Face]) -> None:
        self.faces = faces

    def detect(self, _bgr: np.ndarray) -> list[Face]:
        return self.faces


class FakeEmbedder:
    def __init__(self, vector: np.ndarray | None = None) -> None:
        self.vector = vector if vector is not None else _unit(0)

    def align_and_embed(self, _bgr: np.ndarray, _face: Face) -> np.ndarray:
        return self.vector.copy()


class FakeQuality:
    def __init__(self, ok: bool = True, reasons: list[str] | None = None) -> None:
        self.ok = ok
        self.reasons = reasons or []

    def check(self, _bgr: np.ndarray, _face: Face) -> tuple[bool, list[str]]:
        return self.ok, self.reasons


def _unit(index: int) -> np.ndarray:
    embedding = np.zeros(128, dtype=np.float32)
    embedding[index] = 1.0
    return embedding


def _face() -> Face:
    return Face(
        box=(20, 20, 100, 100),
        score=0.99,
        landmarks5=np.asarray([[40, 45], [100, 45], [70, 65], [48, 90], [92, 90]], dtype=np.float32),
    )


def _process(
    db: Session,
    *,
    faces: list[Face] | None = None,
    quality: FakeQuality | None = None,
    embedding: np.ndarray | None = None,
):
    return enroll.enroll_image(
        db,
        db.get(Person, 1),
        1,
        np.full((160, 160, 3), 127, dtype=np.uint8),
        detector=FakeDetector([_face()] if faces is None else faces),
        embedder=FakeEmbedder(embedding),
        quality=quality or FakeQuality(),
    )


def test_enrollment_requires_exactly_one_face(db_session: Session) -> None:
    result = _process(db_session, faces=[])

    assert not result.accepted
    assert result.reasons == ("exactly_one_face_required",)
    assert db_session.scalars(select(Embedding)).all() == []


def test_enrollment_rejects_quality_failure(db_session: Session) -> None:
    result = _process(db_session, quality=FakeQuality(False, ["blurred"]))

    assert not result.accepted
    assert result.reasons == ("blurred",)
    assert db_session.scalars(select(Embedding)).all() == []


def test_duplicate_person_is_blocked_with_code_and_name(db_session: Session) -> None:
    db_session.add(
        Embedding(
            person_id=2,
            vector_encrypted=enroll._fernet.encrypt(_unit(0).tobytes()),
            created_by=1,
        )
    )
    db_session.commit()
    enroll.rebuild_gallery_cache(db_session)

    result = _process(db_session)

    assert not result.accepted
    assert result.reasons == ("duplicate_person",)
    assert result.duplicate_person == ("P-002", "Two")
    assert db_session.scalars(select(Embedding).where(Embedding.person_id == 1)).all() == []


def test_enrollment_encrypts_vector_and_audits_sample(
    db_session: Session,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(enroll, "_FACES_DIR", tmp_path / "faces")
    vector = _unit(7)
    result = _process(db_session, embedding=vector)

    assert result.accepted
    record = db_session.get(Embedding, result.embedding_id)
    assert record is not None
    assert record.vector_encrypted != vector.tobytes()
    decrypted = enroll._decode_vector(record.vector_encrypted)
    np.testing.assert_array_equal(decrypted, vector)
    assert record.thumb_path is not None
    thumbnail = tmp_path / record.thumb_path
    assert thumbnail.is_file()
    image = cv2.imread(str(thumbnail))
    assert image.shape == (112, 112, 3)
    audit = db_session.scalar(select(AuditLog).where(AuditLog.action == "person.enroll"))
    assert audit is not None


def test_same_person_similar_sample_is_rejected(db_session: Session) -> None:
    db_session.add(
        Embedding(
            person_id=1,
            vector_encrypted=enroll._fernet.encrypt(_unit(0).tobytes()),
            created_by=1,
        )
    )
    db_session.commit()
    enroll.rebuild_gallery_cache(db_session)

    result = _process(db_session)

    assert not result.accepted
    assert result.reasons == ("sample_too_similar",)


def test_reset_deletes_samples_and_audits(
    db_session: Session,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(enroll, "_FACES_DIR", tmp_path / "faces")
    result = _process(db_session, embedding=_unit(5))
    assert result.accepted

    deleted = enroll.clear_person_samples(db_session, db_session.get(Person, 1), 1)

    assert deleted == 1
    assert db_session.scalars(select(Embedding).where(Embedding.person_id == 1)).all() == []
    assert db_session.scalar(select(AuditLog).where(AuditLog.action == "person.enroll.delete")) is not None
    assert not (tmp_path / "faces" / "1" / f"{result.embedding_id}.jpg").exists()


def test_security_user_cannot_open_people_pages(db_session: Session) -> None:
    def override_get_db() -> Generator[Session, None, None]:
        yield db_session

    app.dependency_overrides[get_db] = override_get_db
    try:
        with TestClient(app) as client:
            response = client.post(
                "/login",
                data={"username": "enroll-security", "password": "test-password-123"},
                follow_redirects=False,
            )
            assert response.status_code == 303
            assert client.get("/people").status_code == 403
            client.post("/logout")
            client.post(
                "/login",
                data={"username": "enroll-admin", "password": "test-password-123"},
                follow_redirects=False,
            )
            assert client.get("/people").status_code == 200
            assert client.get("/people/new").status_code == 200
            assert client.get("/people/1/enroll").status_code == 200
    finally:
        app.dependency_overrides.clear()
