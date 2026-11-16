"""Daily attendance from gate-log punches. Pure functions: no database, easy to test.

A "punch" is a matched gate-log event: (UTC time, "IN"|"OUT").
"""

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta, tzinfo
from typing import Literal

Status = Literal["present", "late", "absent", "holiday", "off", "leave", "future"]
Punch = tuple[datetime, str]

# How far outside the shift a punch still belongs to that day's attendance.
BEFORE_SHIFT = timedelta(hours=4)
AFTER_SHIFT = timedelta(hours=6)


@dataclass(frozen=True)
class ShiftRule:
    start: time
    end: time
    late_grace: timedelta = timedelta(minutes=10)
    early_grace: timedelta = timedelta(minutes=10)
    weekdays: frozenset[int] = frozenset({0, 1, 2, 3, 5, 6})  # Monday=0; Friday (4) off

    @classmethod
    def parse(cls, start: str, end: str, late: int, early: int, weekdays: str) -> "ShiftRule":
        days = frozenset(int(part) for part in weekdays.split(",") if part.strip().isdigit() and 0 <= int(part) <= 6)
        return cls(
            time.fromisoformat(start), time.fromisoformat(end),
            timedelta(minutes=max(0, late)), timedelta(minutes=max(0, early)), days,
        )

    @property
    def overnight(self) -> bool:
        return self.end <= self.start


@dataclass(frozen=True)
class Adjustment:
    in_at: datetime | None = None  # UTC
    out_at: datetime | None = None
    mark: str = "present"  # present | absent | leave
    note: str = ""


@dataclass(frozen=True)
class DayResult:
    day: date
    status: Status
    first_in: datetime | None = None  # UTC
    last_out: datetime | None = None
    worked: timedelta | None = None
    late: timedelta | None = None
    early_leave: timedelta | None = None
    missing_out: bool = False
    adjusted: bool = False
    note: str = ""
    worked_on_day_off: bool = False

    @property
    def code(self) -> str:
        """One letter for the monthly grid."""
        return {"present": "P", "late": "L", "absent": "A", "holiday": "H", "off": "W", "leave": "LV", "future": ""}[self.status]


def _utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def window(day: date, shift: ShiftRule | None, zone: tzinfo) -> tuple[datetime, datetime, datetime | None, datetime | None]:
    """(collect_from, collect_to, shift_start, shift_end) in UTC for one attendance day."""
    if shift is None:
        start = datetime.combine(day, time.min, tzinfo=zone)
        return _utc(start), _utc(start + timedelta(days=1)), None, None
    start = datetime.combine(day, shift.start, tzinfo=zone)
    end_day = day + timedelta(days=1) if shift.overnight else day
    end = datetime.combine(end_day, shift.end, tzinfo=zone)
    return _utc(start - BEFORE_SHIFT), _utc(end + AFTER_SHIFT), _utc(start), _utc(end)


def compute_day(
    day: date,
    punches: Iterable[Punch],
    *,
    shift: ShiftRule | None,
    zone: tzinfo,
    now: datetime,
    holiday: bool = False,
    adjustment: Adjustment | None = None,
    last_seen_is_out: bool = False,
) -> DayResult:
    """`last_seen_is_out`: the site has no OUT camera, so the last sighting stands in for leaving."""
    now = _utc(now)
    collect_from, collect_to, shift_start, shift_end = window(day, shift, zone)
    inside = sorted((_utc(at), direction) for at, direction in punches if collect_from <= _utc(at) < collect_to)
    ins = [at for at, direction in inside if direction == "IN"]
    outs = [at for at, direction in inside if direction == "OUT"]
    first_in = ins[0] if ins else None
    last_out = outs[-1] if outs else None
    if last_seen_is_out and len(inside) > 1:
        last_out = inside[-1][0]

    adjusted = adjustment is not None
    note = adjustment.note if adjustment else ""
    if adjustment is not None:
        if adjustment.mark == "leave":
            return DayResult(day, "leave", adjusted=True, note=note)
        if adjustment.mark == "absent":
            return DayResult(day, "absent", adjusted=True, note=note)
        first_in = _utc(adjustment.in_at) if adjustment.in_at else first_in
        last_out = _utc(adjustment.out_at) if adjustment.out_at else last_out

    seen = first_in is not None or last_out is not None
    workday = shift is None or day.weekday() in shift.weekdays
    day_over = now >= (shift_end or collect_to)

    if not seen:
        if holiday:
            return DayResult(day, "holiday", note=note)
        if not workday:
            return DayResult(day, "off", note=note)
        # Absent once it is too late to arrive on time; until then the day is still open.
        absent_from = shift_start + shift.late_grace if shift is not None and shift_start is not None else collect_to
        if now < absent_from:
            return DayResult(day, "future")
        return DayResult(day, "absent", adjusted=adjusted, note=note)

    late = early = None
    if shift_start is not None and first_in is not None and first_in > shift_start + shift.late_grace:
        late = first_in - shift_start
    if shift_end is not None and last_out is not None and last_out < shift_end - shift.early_grace:
        early = shift_end - last_out
    worked = last_out - first_in if first_in is not None and last_out is not None and last_out > first_in else None
    missing_out = first_in is not None and last_out is None and day_over
    status: Status = "late" if late else "present"
    return DayResult(
        day,
        status,
        first_in=first_in,
        last_out=last_out,
        worked=worked,
        late=late,
        early_leave=early,
        missing_out=missing_out,
        adjusted=adjusted,
        note=note,
        worked_on_day_off=holiday or not workday,
    )


@dataclass(frozen=True)
class MonthSummary:
    present: int
    late: int
    absent: int
    leave: int
    holiday: int
    worked: timedelta
    late_total: timedelta


def summarise(days: Iterable[DayResult]) -> MonthSummary:
    items = list(days)
    return MonthSummary(
        present=sum(item.status in ("present", "late") for item in items),
        late=sum(item.status == "late" for item in items),
        absent=sum(item.status == "absent" for item in items),
        leave=sum(item.status == "leave" for item in items),
        holiday=sum(item.status in ("holiday", "off") for item in items),
        worked=sum((item.worked for item in items if item.worked), timedelta()),
        late_total=sum((item.late for item in items if item.late), timedelta()),
    )


def format_duration(value: timedelta | None) -> str:
    if value is None:
        return ""
    minutes = int(value.total_seconds() // 60)
    return f"{minutes // 60}:{minutes % 60:02d}"
