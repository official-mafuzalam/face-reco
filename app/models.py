from datetime import UTC, date, datetime

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    LargeBinary,
    String,
    Text,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base


class User(Base):
    __tablename__ = "users"
    __table_args__ = (
        CheckConstraint("role IN ('admin', 'security')", name="ck_users_role"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    username: Mapped[str] = mapped_column(String(100), unique=True, nullable=False)
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    role: Mapped[str] = mapped_column(String(20), nullable=False)
    active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    # Fernet-encrypted TOTP secret for 2FA (admins / security).
    totp_secret_encrypted: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Incremented on password change or "logout all devices" to invalidate existing sessions.
    token_version: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    # Set when an admin deletes the account. The row stays so the audit trail keeps its name.
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class Person(Base):
    __tablename__ = "people"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    code: Mapped[str] = mapped_column(String(100), unique=True, nullable=False)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC), nullable=False
    )
    # employee | visitor | vip | blacklist (validated in the app; see PERSON_CATEGORIES)
    category: Mapped[str] = mapped_column(String(20), nullable=False, default="employee")
    department: Mapped[str] = mapped_column(String(100), nullable=False, default="")
    shift_id: Mapped[int | None] = mapped_column(ForeignKey("shifts.id", ondelete="SET NULL"), nullable=True)


class Shift(Base):
    """Working hours. An end time at or before the start means the shift ends the next day."""

    __tablename__ = "shifts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(100), nullable=False, unique=True)
    start: Mapped[str] = mapped_column(String(5), nullable=False)  # "HH:MM" local
    end: Mapped[str] = mapped_column(String(5), nullable=False)
    late_grace_minutes: Mapped[int] = mapped_column(Integer, nullable=False, default=10)
    early_grace_minutes: Mapped[int] = mapped_column(Integer, nullable=False, default=10)
    # Working weekdays, Monday=0 … Sunday=6. Default: Saturday–Thursday (Friday off).
    weekdays: Mapped[str] = mapped_column(String(20), nullable=False, default="0,1,2,3,5,6")


class Holiday(Base):
    __tablename__ = "holidays"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    day: Mapped[date] = mapped_column(Date, nullable=False, unique=True)
    name: Mapped[str] = mapped_column(String(200), nullable=False)


class AttendanceAdjustment(Base):
    """A manual correction for one person and day (forgotten punch, camera down…)."""

    __tablename__ = "attendance_adjustments"
    __table_args__ = (Index("ix_adjustments_person_day", "person_id", "day", unique=True),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    person_id: Mapped[int] = mapped_column(ForeignKey("people.id"), nullable=False)
    day: Mapped[date] = mapped_column(Date, nullable=False)
    in_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)  # UTC
    out_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # present | absent | leave — "leave" marks an approved absence.
    mark: Mapped[str] = mapped_column(String(10), nullable=False, default="present")
    note: Mapped[str] = mapped_column(Text, nullable=False, default="")
    created_by: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC), nullable=False
    )


PERSON_CATEGORIES = ("employee", "visitor", "vip", "blacklist")


class Camera(Base):
    __tablename__ = "cameras"
    __table_args__ = (
        CheckConstraint("direction IN ('IN', 'OUT')", name="ck_cameras_direction"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    gate: Mapped[str] = mapped_column(String(200), nullable=False)
    direction: Mapped[str] = mapped_column(String(3), nullable=False)
    rtsp_url_encrypted: Mapped[str] = mapped_column(Text, nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    face_enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    surveillance_enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    # Set when an admin deletes the camera: hidden everywhere, but old events keep their camera.
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class Embedding(Base):
    __tablename__ = "embeddings"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    person_id: Mapped[int] = mapped_column(ForeignKey("people.id"), nullable=False, index=True)
    vector_encrypted: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    thumb_path: Mapped[str | None] = mapped_column(String(500), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC), nullable=False
    )
    created_by: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False)

    person: Mapped[Person] = relationship()
    creator: Mapped[User] = relationship()


class HealthSample(Base):
    """One per-minute reading per camera (camera_id set) or for the computer (camera_id NULL)."""

    __tablename__ = "health_samples"
    __table_args__ = (Index("ix_health_samples_camera_at", "camera_id", "at"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, index=True)
    camera_id: Mapped[int | None] = mapped_column(ForeignKey("cameras.id"), nullable=True)
    # online | offline | disabled for cameras; "system" for the computer row.
    status: Mapped[str] = mapped_column(String(10), nullable=False)
    fps: Mapped[float | None] = mapped_column(Float, nullable=True)
    face_fps: Mapped[float | None] = mapped_column(Float, nullable=True)
    surveillance_fps: Mapped[float | None] = mapped_column(Float, nullable=True)
    cpu_percent: Mapped[float | None] = mapped_column(Float, nullable=True)
    disk_free_gb: Mapped[float | None] = mapped_column(Float, nullable=True)


class AuditLog(Base):
    __tablename__ = "audit_logs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    action: Mapped[str] = mapped_column(String(100), nullable=False)
    detail: Mapped[str] = mapped_column(Text, nullable=False, default="")
    at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC), nullable=False
    )

    user: Mapped[User | None] = relationship()


class Event(Base):
    __tablename__ = "events"
    __table_args__ = (
        CheckConstraint(
            "direction IN ('IN', 'OUT')",
            name="ck_events_direction",
        ),
        CheckConstraint(
            "status IN ('matched', 'unknown', 'low_quality')",
            name="ck_events_status",
        ),
        Index("ix_events_at", "at"),
        Index("ix_events_person_at", "person_id", "at"),
        Index("ix_events_gate_at", "gate", "at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    person_id: Mapped[int | None] = mapped_column(ForeignKey("people.id"), nullable=True)
    camera_id: Mapped[int] = mapped_column(ForeignKey("cameras.id"), nullable=False)
    gate: Mapped[str] = mapped_column(String(200), nullable=False)
    direction: Mapped[str] = mapped_column(String(3), nullable=False)
    at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC), nullable=False
    )
    score: Mapped[float | None] = mapped_column(Float, nullable=True)
    status: Mapped[str] = mapped_column(String(20), nullable=False)
    snapshot_path: Mapped[str | None] = mapped_column(String(500), nullable=True)
    reviewed_by: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # Comma list of quality problems for low_quality events, e.g. "face_too_small,blurred".
    reason: Mapped[str] = mapped_column(String(200), nullable=False, default="")
    # Fernet-encrypted face embedding, kept for unknown faces only (search / add as person).
    embedding_encrypted: Mapped[bytes | None] = mapped_column(LargeBinary, nullable=True)


class Incident(Base):
    """Something the surveillance pipeline saw on a camera (person, vehicle, later rule hits)."""

    __tablename__ = "incidents"
    __table_args__ = (
        CheckConstraint("severity IN ('info', 'warning', 'critical')", name="ck_incidents_severity"),
        Index("ix_incidents_started_at", "started_at"),
        Index("ix_incidents_camera_started", "camera_id", "started_at"),
        Index("ix_incidents_kind_started", "kind", "started_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    camera_id: Mapped[int] = mapped_column(ForeignKey("cameras.id"), nullable=False)
    kind: Mapped[str] = mapped_column(String(30), nullable=False)
    severity: Mapped[str] = mapped_column(String(10), nullable=False, default="info")
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC), nullable=False
    )
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    person_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    vehicle_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    details: Mapped[str] = mapped_column(Text, nullable=False, default="")
    snapshot_path: Mapped[str | None] = mapped_column(String(500), nullable=True)
    clip_path: Mapped[str | None] = mapped_column(String(500), nullable=True)
    reviewed_by: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    review_note: Mapped[str] = mapped_column(Text, nullable=False, default="")
    # Human Review Verdict (confirmed / false_alarm / missed)
    review_verdict: Mapped[str | None] = mapped_column(String(20), nullable=True)
    # AI Verification (Local VLM via Ollama: confirmed / false_alarm / inconclusive)
    ai_verdict: Mapped[str | None] = mapped_column(String(20), nullable=True)
    ai_confidence: Mapped[float | None] = mapped_column(Float, nullable=True)
    ai_summary_bn: Mapped[str | None] = mapped_column(Text, nullable=True)
    ai_summary_en: Mapped[str | None] = mapped_column(Text, nullable=True)
    ai_checked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    camera: Mapped[Camera] = relationship()


class SurveillanceRule(Base):
    """A zone or line drawn on one camera. Points are normalised (0..1) image coordinates."""

    __tablename__ = "surveillance_rules"
    # Rule kinds are validated in the app (app.web.routes.rules.RULE_KINDS) so new kinds need no migration.
    __table_args__ = (
        CheckConstraint("severity IN ('info', 'warning', 'critical')", name="ck_rules_severity"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    camera_id: Mapped[int] = mapped_column(ForeignKey("cameras.id"), nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    kind: Mapped[str] = mapped_column(String(20), nullable=False)
    points: Mapped[str] = mapped_column(Text, nullable=False)  # JSON [[x, y], ...]
    classes: Mapped[str] = mapped_column(String(100), nullable=False, default="person")  # comma list
    direction: Mapped[str] = mapped_column(String(10), nullable=False, default="any")  # line: any|ab|ba
    loiter_seconds: Mapped[int] = mapped_column(Integer, nullable=False, default=60)
    schedule_start: Mapped[str] = mapped_column(String(5), nullable=False, default="")  # "HH:MM" local
    schedule_end: Mapped[str] = mapped_column(String(5), nullable=False, default="")
    severity: Mapped[str] = mapped_column(String(10), nullable=False, default="warning")
    extra_params: Mapped[str] = mapped_column(Text, nullable=False, server_default="{}", default="{}")
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)


class PushSubscription(Base):
    """Mobile PWA / Web Push subscription per user device."""

    __tablename__ = "push_subscriptions"
    __table_args__ = (
        CheckConstraint("min_severity IN ('info', 'warning', 'critical')", name="ck_push_min_severity"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    endpoint: Mapped[str] = mapped_column(Text, unique=True, nullable=False)
    p256dh: Mapped[str] = mapped_column(Text, nullable=False)
    auth: Mapped[str] = mapped_column(Text, nullable=False)
    quiet_start: Mapped[str] = mapped_column(String(5), default="22:00", nullable=False)
    quiet_end: Mapped[str] = mapped_column(String(5), default="06:00", nullable=False)
    min_severity: Mapped[str] = mapped_column(String(20), default="warning", nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC), nullable=False
    )

    user: Mapped[User] = relationship()
