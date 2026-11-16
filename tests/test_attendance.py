from datetime import UTC, date, datetime, time, timedelta, timezone

import pytest

from app.services.attendance import Adjustment, ShiftRule, compute_day, format_duration, summarise

DHAKA = timezone(timedelta(hours=6))
DAY = date(2026, 11, 16)  # a Monday
DAY_SHIFT = ShiftRule(time(9, 0), time(17, 0))
NIGHT_SHIFT = ShiftRule(time(22, 0), time(6, 0))
LATER = datetime(2026, 11, 20, tzinfo=UTC)


def local(day: date, hour: int, minute: int = 0) -> datetime:
    return datetime.combine(day, time(hour, minute), tzinfo=DHAKA).astimezone(UTC)


def test_on_time_day_with_in_and_out() -> None:
    result = compute_day(
        DAY, [(local(DAY, 8, 55), "IN"), (local(DAY, 12, 0), "IN"), (local(DAY, 17, 5), "OUT")],
        shift=DAY_SHIFT, zone=DHAKA, now=LATER,
    )
    assert result.status == "present"
    assert result.first_in == local(DAY, 8, 55)
    assert result.last_out == local(DAY, 17, 5)
    assert format_duration(result.worked) == "8:10"
    assert result.late is None and result.early_leave is None and not result.missing_out


def test_grace_late_and_early_leave() -> None:
    within_grace = compute_day(DAY, [(local(DAY, 9, 10), "IN")], shift=DAY_SHIFT, zone=DHAKA, now=LATER)
    assert within_grace.status == "present"
    late = compute_day(
        DAY, [(local(DAY, 9, 25), "IN"), (local(DAY, 16, 30), "OUT")], shift=DAY_SHIFT, zone=DHAKA, now=LATER
    )
    assert late.status == "late"
    assert late.late == timedelta(minutes=25)
    assert late.early_leave == timedelta(minutes=30)


def test_night_shift_crosses_midnight() -> None:
    punches = [(local(DAY, 21, 50), "IN"), (local(DAY + timedelta(days=1), 6, 10), "OUT")]
    result = compute_day(DAY, punches, shift=NIGHT_SHIFT, zone=DHAKA, now=LATER)
    assert result.status == "present"
    assert format_duration(result.worked) == "8:20"
    # The same morning exit must not count as the next night's arrival.
    next_night = compute_day(DAY + timedelta(days=1), punches, shift=NIGHT_SHIFT, zone=DHAKA, now=LATER)
    assert next_night.status == "absent" and next_night.first_in is None


def test_absent_only_after_grace_and_not_on_days_off() -> None:
    before_grace = compute_day(DAY, [], shift=DAY_SHIFT, zone=DHAKA, now=local(DAY, 9, 5))
    assert before_grace.status == "future"
    after_grace = compute_day(DAY, [], shift=DAY_SHIFT, zone=DHAKA, now=local(DAY, 9, 11))
    assert after_grace.status == "absent"
    friday = date(2026, 11, 20)
    assert compute_day(friday, [], shift=DAY_SHIFT, zone=DHAKA, now=LATER).status == "off"
    assert compute_day(DAY, [], shift=DAY_SHIFT, zone=DHAKA, now=LATER, holiday=True).status == "holiday"
    worked_friday = compute_day(friday, [(local(friday, 10), "IN")], shift=DAY_SHIFT, zone=DHAKA, now=LATER)
    assert worked_friday.status == "late" and worked_friday.worked_on_day_off


def test_missing_out_and_single_in_camera_sites() -> None:
    punches = [(local(DAY, 8, 50), "IN"), (local(DAY, 13, 0), "IN"), (local(DAY, 17, 30), "IN")]
    with_out_camera = compute_day(DAY, punches, shift=DAY_SHIFT, zone=DHAKA, now=LATER)
    assert with_out_camera.missing_out and with_out_camera.worked is None

    single_camera = compute_day(DAY, punches, shift=DAY_SHIFT, zone=DHAKA, now=LATER, last_seen_is_out=True)
    assert single_camera.last_out == local(DAY, 17, 30)
    assert format_duration(single_camera.worked) == "8:40"
    assert not single_camera.missing_out

    only_once = compute_day(DAY, punches[:1], shift=DAY_SHIFT, zone=DHAKA, now=LATER, last_seen_is_out=True)
    assert only_once.last_out is None  # one sighting is an arrival, not also a departure


def test_manual_adjustments() -> None:
    fixed = compute_day(
        DAY, [(local(DAY, 8, 58), "IN")], shift=DAY_SHIFT, zone=DHAKA, now=LATER,
        adjustment=Adjustment(out_at=local(DAY, 17, 0), note="forgot to pass the gate"),
    )
    assert fixed.adjusted and fixed.last_out == local(DAY, 17, 0) and not fixed.missing_out
    leave = compute_day(DAY, [], shift=DAY_SHIFT, zone=DHAKA, now=LATER, adjustment=Adjustment(mark="leave"))
    assert leave.status == "leave" and leave.code == "LV"


def test_no_shift_uses_the_calendar_day() -> None:
    result = compute_day(DAY, [(local(DAY, 11), "IN"), (local(DAY, 15), "OUT")], shift=None, zone=DHAKA, now=LATER)
    assert result.status == "present" and result.late is None
    assert compute_day(DAY, [], shift=None, zone=DHAKA, now=local(DAY, 15)).status == "future"
    assert compute_day(DAY, [], shift=None, zone=DHAKA, now=LATER).status == "absent"


def test_shift_parse_and_summary() -> None:
    rule = ShiftRule.parse("08:30", "17:30", 5, 0, "0,1,2,9,x")
    assert rule.weekdays == {0, 1, 2} and rule.late_grace == timedelta(minutes=5)
    days = [
        compute_day(DAY, [(local(DAY, 9, 30), "IN"), (local(DAY, 17), "OUT")], shift=DAY_SHIFT, zone=DHAKA, now=LATER),
        compute_day(DAY + timedelta(days=1), [], shift=DAY_SHIFT, zone=DHAKA, now=LATER),
    ]
    summary = summarise(days)
    assert (summary.present, summary.late, summary.absent) == (1, 1, 1)
    assert format_duration(summary.worked) == "7:30"
    assert [day.code for day in days] == ["L", "A"]


@pytest.mark.parametrize("value, expected", [(None, ""), (timedelta(minutes=5), "0:05"), (timedelta(hours=26), "26:00")])
def test_format_duration(value, expected) -> None:
    assert format_duration(value) == expected
