"""Load gate-log punches, shifts, holidays and corrections, and build attendance tables."""

from collections import defaultdict
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta, tzinfo

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import AttendanceAdjustment, Camera, Event, Holiday, Person, Shift
from app.services.attendance import Adjustment, DayResult, ShiftRule, compute_day


@dataclass(frozen=True)
class PersonRow:
    person: Person
    shift: Shift | None
    days: list[DayResult]


def shift_rule(shift: Shift | None) -> ShiftRule | None:
    if shift is None:
        return None
    try:
        return ShiftRule.parse(shift.start, shift.end, shift.late_grace_minutes, shift.early_grace_minutes, shift.weekdays)
    except ValueError:
        return None


def _utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def has_out_camera(db: Session) -> bool:
    return db.scalar(
        select(Camera.id).where(Camera.enabled.is_(True), Camera.face_enabled.is_(True), Camera.direction == "OUT").limit(1)
    ) is not None


def departments(db: Session) -> list[str]:
    return sorted({value for value in db.scalars(select(Person.department).distinct()) if value})


def build(
    db: Session,
    first: date,
    last: date,
    zone: tzinfo,
    *,
    department: str | None = None,
    person_id: int | None = None,
    now: datetime | None = None,
) -> list[PersonRow]:
    """Attendance for active employees (not visitors, VIPs or blacklisted people) from `first` to `last`."""
    now = now or datetime.now(UTC)
    query = select(Person).where(Person.active.is_(True), Person.category == "employee")
    if department:
        query = query.where(Person.department == department)
    if person_id is not None:
        query = select(Person).where(Person.id == person_id)
    people = list(db.scalars(query.order_by(Person.department, Person.code)))
    if not people:
        return []
    ids = [person.id for person in people]
    shifts = {shift.id: shift for shift in db.scalars(select(Shift))}

    # Punch range: night shifts reach into the next morning; a margin either side is harmless.
    since = datetime.combine(first - timedelta(days=1), time.min, tzinfo=zone).astimezone(UTC)
    until = datetime.combine(last + timedelta(days=2), time.min, tzinfo=zone).astimezone(UTC)
    punches: dict[int, list[tuple[datetime, str]]] = defaultdict(list)
    for person_key, at, direction in db.execute(
        select(Event.person_id, Event.at, Event.direction).where(
            Event.status == "matched",
            Event.person_id.in_(ids),
            Event.at >= since.replace(tzinfo=None),
            Event.at < until.replace(tzinfo=None),
        )
    ):
        punches[person_key].append((_utc(at), direction))

    holidays = {item.day for item in db.scalars(select(Holiday).where(Holiday.day.between(first, last)))}
    adjustments = {
        (item.person_id, item.day): Adjustment(
            _utc(item.in_at) if item.in_at else None, _utc(item.out_at) if item.out_at else None, item.mark, item.note
        )
        for item in db.scalars(
            select(AttendanceAdjustment).where(
                AttendanceAdjustment.person_id.in_(ids), AttendanceAdjustment.day.between(first, last)
            )
        )
    }
    last_seen_is_out = not has_out_camera(db)
    days = [first + timedelta(days=offset) for offset in range((last - first).days + 1)]
    rows = []
    for person in people:
        shift = shifts.get(person.shift_id) if person.shift_id else None
        rule = shift_rule(shift)
        own = punches.get(person.id, [])
        rows.append(
            PersonRow(
                person,
                shift,
                [
                    compute_day(
                        day, own, shift=rule, zone=zone, now=now, holiday=day in holidays,
                        adjustment=adjustments.get((person.id, day)), last_seen_is_out=last_seen_is_out,
                    )
                    for day in days
                ],
            )
        )
    return rows


def local_hhmm(value: datetime | None, zone: tzinfo) -> str:
    return _utc(value).astimezone(zone).strftime("%H:%M") if value else ""


def parse_local(day: date, hhmm: str, zone: tzinfo, after: datetime | None = None) -> datetime | None:
    """"HH:MM" on `day` (local) -> UTC; a time earlier than `after` is taken as the next day (night shift)."""
    hhmm = (hhmm or "").strip()
    if not hhmm:
        return None
    value = datetime.combine(day, time.fromisoformat(hhmm), tzinfo=zone).astimezone(UTC)
    if after is not None and value <= after:
        value += timedelta(days=1)
    return value
