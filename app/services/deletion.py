"""Deleting people, face samples, cameras, users, gate-log entries and incidents.

Every deletion is written to the audit trail. Media files are only removed from inside the
app's own data folders, whatever path a database row claims.
"""

import logging
import secrets
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from cryptography.fernet import Fernet
from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from app.auth import hash_password
from app.config import settings
from app.models import (
    AttendanceAdjustment,
    AuditLog,
    Camera,
    Embedding,
    Event,
    HealthSample,
    Incident,
    Person,
    SurveillanceRule,
    User,
)
from app.services import enroll
from app.services.enroll import clear_person_samples, rebuild_gallery_cache

logger = logging.getLogger(__name__)
PROJECT_ROOT = Path(__file__).resolve().parents[2]
MAX_BULK = 1000


def _remove_media(relative: str | None, folder: str, root: Path = PROJECT_ROOT) -> None:
    """Delete one media file, but only if it really lies inside data/<folder>."""
    if not relative:
        return
    base = (root / "data" / folder).resolve()
    path = (root / relative).resolve()
    if base not in path.parents:
        logger.warning("Refusing to delete media outside %s: %s", base, relative)
        return
    path.unlink(missing_ok=True)


class DeleteRefused(ValueError):
    """`code` is a translation key explaining why."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


# ---------------------------------------------------------------- people and samples


def delete_person(db: Session, person: Person, user_id: int) -> None:
    """Remove a person, their face samples and corrections. Old gate-log lines stay, without a name."""
    label = f"{person.code} {person.name}"
    person_id = person.id
    clear_person_samples(db, person, user_id)
    db.execute(
        update(Event)
        .where(Event.person_id == person_id)
        .values(person_id=None, reason="person_deleted")
    )
    db.query(AttendanceAdjustment).filter(AttendanceAdjustment.person_id == person_id).delete()
    db.delete(person)
    db.add(AuditLog(user_id=user_id, action="person.delete", detail=f"person_id={person_id}; {label}"))
    db.commit()
    rebuild_gallery_cache(db)


def delete_sample(db: Session, person: Person, embedding_id: int, user_id: int, faces_dir: Path | None = None) -> None:
    sample = db.get(Embedding, embedding_id)
    if sample is None or sample.person_id != person.id:
        raise DeleteRefused("not_found")
    faces_root = (faces_dir or enroll._FACES_DIR).resolve()
    if sample.thumb_path:
        path = (faces_root.parent / sample.thumb_path).resolve()
        if faces_root in path.parents:
            path.unlink(missing_ok=True)
    db.delete(sample)
    db.add(AuditLog(user_id=user_id, action="person.sample.delete", detail=f"person_id={person.id}; embedding_id={embedding_id}"))
    db.commit()
    rebuild_gallery_cache(db)


# ---------------------------------------------------------------- cameras and users


def delete_camera(db: Session, camera: Camera, user_id: int) -> None:
    """Hide a camera for good. Its old gate-log entries and incidents keep pointing at it.

    The caller stops the camera's workers. The saved address (with its password) is wiped.
    """
    db.query(SurveillanceRule).filter(SurveillanceRule.camera_id == camera.id).delete()
    db.query(HealthSample).filter(HealthSample.camera_id == camera.id).delete()
    camera.enabled = False
    camera.face_enabled = False
    camera.surveillance_enabled = False
    camera.deleted_at = datetime.now(UTC)
    camera.rtsp_url_encrypted = Fernet(settings.fernet_key.encode()).encrypt(b"deleted").decode()
    db.add(AuditLog(user_id=user_id, action="camera.delete", detail=f"camera_id={camera.id}; {camera.name}"))
    db.commit()


def delete_user(db: Session, target: User, current: User) -> None:
    """Close an account for good; the audit trail keeps its (renamed) name and the name can be reused."""
    if target.id == current.id:
        raise DeleteRefused("user_cannot_delete_self")
    if target.role == "admin" and target.active:
        admins = db.scalar(
            select(func.count(User.id)).where(User.role == "admin", User.active.is_(True), User.deleted_at.is_(None))
        ) or 0
        if admins <= 1:
            raise DeleteRefused("user_last_admin")
    original = target.username
    target.username = f"{original}~deleted{target.id}"[:100]
    target.active = False
    target.deleted_at = datetime.now(UTC)
    target.password_hash = hash_password(secrets.token_urlsafe(32))  # the old password can never work again
    db.add(AuditLog(user_id=current.id, action="user.delete", detail=f"user_id={target.id}; {original}"))
    db.commit()


# ---------------------------------------------------------------- gate log and incidents


def _ids(ids: list[int]) -> list[int]:
    unique = sorted({int(value) for value in ids})
    if not unique:
        raise DeleteRefused("delete_nothing_selected")
    if len(unique) > MAX_BULK:
        raise DeleteRefused("delete_too_many")
    return unique


def delete_events(db: Session, ids: list[int], user_id: int, root: Path = PROJECT_ROOT) -> int:
    events = db.scalars(select(Event).where(Event.id.in_(_ids(ids)))).all()
    for event in events:
        _remove_media(event.snapshot_path, "snapshots", root)
        db.delete(event)
    if events:
        db.add(AuditLog(user_id=user_id, action="event.delete",
                        detail=f"count={len(events)}; ids={','.join(str(e.id) for e in events)[:900]}"))
    db.commit()
    return len(events)


@dataclass(frozen=True)
class IncidentDeleteResult:
    deleted: int
    skipped_running: int


def delete_incidents(db: Session, ids: list[int], user_id: int, root: Path = PROJECT_ROOT) -> IncidentDeleteResult:
    """Incidents still being recorded are skipped. A clip shared by several incidents is only
    removed when the last incident using it is deleted."""
    selected = db.scalars(select(Incident).where(Incident.id.in_(_ids(ids)))).all()
    running = [item for item in selected if item.ended_at is None]
    finished = [item for item in selected if item.ended_at is not None]
    finished_ids = {item.id for item in finished}
    for incident in finished:
        _remove_media(incident.snapshot_path, "incidents", root)
        if incident.clip_path:
            shared = db.scalar(
                select(func.count(Incident.id)).where(
                    Incident.clip_path == incident.clip_path, Incident.id.not_in(finished_ids)
                )
            )
            if not shared:
                _remove_media(incident.clip_path, "incidents", root)
        db.delete(incident)
    if finished:
        db.add(AuditLog(user_id=user_id, action="incident.delete",
                        detail=f"count={len(finished)}; ids={','.join(str(i.id) for i in finished)[:900]}"))
    db.commit()
    return IncidentDeleteResult(len(finished), len(running))
