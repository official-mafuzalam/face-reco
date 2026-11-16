"""Attendance: daily and monthly reports, per-person history, corrections, shifts and holidays."""

import calendar
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response, StreamingResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.auth import get_current_user, require_role
from app.db import get_db
from app.i18n import TRANSLATIONS
from app.models import AttendanceAdjustment, AuditLog, Holiday, Person, Shift, User
from app.services import xlsx
from app.services.attendance import format_duration, summarise
from app.services.attendance_report import build, departments, has_out_camera, local_hhmm, parse_local
from app.services.data_io import csv_stream
from app.web.routes.events import _local_zone

router = APIRouter(prefix="/attendance", dependencies=[Depends(get_current_user)])
templates = Jinja2Templates(directory=Path(__file__).resolve().parents[1] / "templates")
WEEKDAYS = (0, 1, 2, 3, 4, 5, 6)
MARKS = ("present", "absent", "leave")


def _lang(request: Request) -> str:
    return "bn" if request.query_params.get("lang") == "bn" else "en"


def _today() -> date:
    return datetime.now(_local_zone()).date()


def _parse_day(value: str | None) -> date:
    try:
        return date.fromisoformat(value) if value else _today()
    except ValueError as exc:
        raise HTTPException(status_code=422, detail="Invalid date") from exc


def _parse_month(value: str | None) -> tuple[date, date]:
    try:
        first = date.fromisoformat(f"{value}-01") if value else _today().replace(day=1)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail="Invalid month") from exc
    return first, first.replace(day=calendar.monthrange(first.year, first.month)[1])


def _render(request: Request, name: str, user: User, **context) -> HTMLResponse:
    language = _lang(request)
    t = TRANSLATIONS[language]
    zone = _local_zone()
    return templates.TemplateResponse(
        request=request,
        name=name,
        context={
            "language": language, "t": t, "title": t["attendance_title"], "is_admin": user.role == "admin",
            "hhmm": lambda value: local_hhmm(value, zone), "duration": format_duration,
            "status_text": lambda result: t[f"att_{result.status}"], **context,
        },
    )


def _status_label(t: dict, result) -> str:
    return t[f"att_{result.status}"]


# ---------------------------------------------------------------- daily


def _daily_table(db: Session, day: date, department: str | None, t: dict) -> tuple[list[str], list[list]]:
    zone = _local_zone()
    header = [t["person_code"], t["person_name"], t["att_department"], t["att_shift"], t["att_status"],
              t["att_in"], t["att_out"], t["att_worked"], t["att_late"], t["att_early"], t["att_note"]]
    rows = []
    for row in build(db, day, day, zone, department=department):
        result = row.days[0]
        notes = [result.note] if result.note else []
        if result.missing_out:
            notes.append(t["att_missing_out"])
        rows.append([
            row.person.code, row.person.name, row.person.department, row.shift.name if row.shift else "",
            _status_label(t, result), local_hhmm(result.first_in, zone), local_hhmm(result.last_out, zone),
            format_duration(result.worked), format_duration(result.late), format_duration(result.early_leave),
            "; ".join(notes),
        ])
    return header, rows


@router.get("", response_class=HTMLResponse)
def daily(request: Request, day: str | None = None, department: str | None = None,
          user: User = Depends(get_current_user), db: Session = Depends(get_db)) -> HTMLResponse:
    selected = _parse_day(day)
    rows = build(db, selected, selected, _local_zone(), department=department or None)
    results = [row.days[0] for row in rows]
    counts = {status: sum(r.status == status for r in results) for status in ("present", "late", "absent", "leave", "future")}
    counts["present"] += counts["late"]
    return _render(
        request, "attendance_daily.html", user, tab="daily",
        day=selected, rows=rows, counts=counts, departments=departments(db), department=department or "",
        single_camera=not has_out_camera(db), prev_day=selected - timedelta(days=1), next_day=selected + timedelta(days=1),
    )


@router.get("/export.{fmt}")
def daily_export(fmt: str, request: Request, day: str | None = None, department: str | None = None,
                 db: Session = Depends(get_db)) -> Response:
    selected = _parse_day(day)
    t = TRANSLATIONS[_lang(request)]
    header, rows = _daily_table(db, selected, department or None, t)
    return _export(fmt, f"attendance-{selected.isoformat()}", [(selected.isoformat(), header, rows)])


# ---------------------------------------------------------------- monthly


def _month_tables(db: Session, first: date, last: date, department: str | None, t: dict):
    rows = build(db, first, last, _local_zone(), department=department)
    days = [first + timedelta(days=offset) for offset in range((last - first).days + 1)]
    grid_header = [t["person_code"], t["person_name"], t["att_department"]] + [str(day.day) for day in days] + [
        t["att_present"], t["att_late"], t["att_absent"], t["att_leave"], t["att_worked"], t["att_late_total"],
    ]
    grid, summaries = [], []
    for row in rows:
        summary = summarise(row.days)
        summaries.append((row, summary))
        grid.append(
            [row.person.code, row.person.name, row.person.department] + [result.code for result in row.days]
            + [summary.present, summary.late, summary.absent, summary.leave,
               format_duration(summary.worked), format_duration(summary.late_total)]
        )
    zone = _local_zone()
    detail_header = [t["person_code"], t["person_name"], t["time"], t["att_status"], t["att_in"], t["att_out"],
                     t["att_worked"], t["att_late"], t["att_early"], t["att_note"]]
    details = [
        [row.person.code, row.person.name, result.day.isoformat(), _status_label(t, result),
         local_hhmm(result.first_in, zone), local_hhmm(result.last_out, zone), format_duration(result.worked),
         format_duration(result.late), format_duration(result.early_leave),
         "; ".join(filter(None, [result.note, t["att_missing_out"] if result.missing_out else ""]))]
        for row in rows for result in row.days if result.status != "future"
    ]
    return days, rows, summaries, (grid_header, grid), (detail_header, details)


@router.get("/month", response_class=HTMLResponse)
def monthly(request: Request, month: str | None = None, department: str | None = None,
            user: User = Depends(get_current_user), db: Session = Depends(get_db)) -> HTMLResponse:
    first, last = _parse_month(month)
    t = TRANSLATIONS[_lang(request)]
    days, rows, summaries, _grid, _details = _month_tables(db, first, last, department or None, t)
    prev_month = (first - timedelta(days=1)).strftime("%Y-%m")
    next_month = (last + timedelta(days=1)).strftime("%Y-%m")
    return _render(
        request, "attendance_month.html", user, tab="month",
        first=first, days=days, summaries=summaries, departments=departments(db), department=department or "",
        month=first.strftime("%Y-%m"), prev_month=prev_month, next_month=next_month,
    )


@router.get("/month/export.{fmt}")
def monthly_export(fmt: str, request: Request, month: str | None = None, department: str | None = None,
                   db: Session = Depends(get_db)) -> Response:
    first, last = _parse_month(month)
    t = TRANSLATIONS[_lang(request)]
    _days, _rows, _summaries, (grid_header, grid), (detail_header, details) = _month_tables(
        db, first, last, department or None, t
    )
    name = f"attendance-{first.strftime('%Y-%m')}"
    if fmt == "csv":
        return _export("csv", name, [(name, grid_header, grid)])
    return _export(fmt, name, [(t["att_sheet_month"], grid_header, grid), (t["att_sheet_days"], detail_header, details)])


def _export(fmt: str, name: str, sheets: list) -> Response:
    if fmt == "csv":
        _title, header, rows = sheets[0]
        return StreamingResponse(
            csv_stream(header, rows), media_type="text/csv; charset=utf-8",
            headers={"Content-Disposition": f'attachment; filename="{name}.csv"'},
        )
    if fmt == "xlsx":
        return Response(
            xlsx.workbook(sheets),
            media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            headers={"Content-Disposition": f'attachment; filename="{name}.xlsx"'},
        )
    raise HTTPException(status_code=404, detail="Unknown format")


# ---------------------------------------------------------------- one person


@router.get("/person/{person_id}", response_class=HTMLResponse)
def person_history(person_id: int, request: Request, month: str | None = None,
                   user: User = Depends(get_current_user), db: Session = Depends(get_db)) -> HTMLResponse:
    person = db.get(Person, person_id)
    if person is None:
        raise HTTPException(status_code=404, detail="Person not found")
    first, last = _parse_month(month)
    rows = build(db, first, last, _local_zone(), person_id=person_id)
    days = rows[0].days if rows else []
    adjustments = {
        item.day: item for item in db.scalars(
            select(AttendanceAdjustment).where(
                AttendanceAdjustment.person_id == person_id, AttendanceAdjustment.day.between(first, last)
            )
        )
    }
    return _render(
        request, "attendance_person.html", user, tab="",
        person=person, shift=rows[0].shift if rows else None, days=list(reversed(days)), summary=summarise(days),
        adjustments=adjustments, month=first.strftime("%Y-%m"), marks=MARKS,
        prev_month=(first - timedelta(days=1)).strftime("%Y-%m"), next_month=(last + timedelta(days=1)).strftime("%Y-%m"),
        error=TRANSLATIONS[_lang(request)].get(request.query_params.get("error") or ""),
    )


@router.post("/person/{person_id}/adjust")
def adjust(person_id: int, request: Request, day: str = Form(...), mark: str = Form("present"),
           in_time: str = Form(""), out_time: str = Form(""), note: str = Form(""),
           user: User = Depends(require_role("admin")), db: Session = Depends(get_db)) -> RedirectResponse:
    person = db.get(Person, person_id)
    if person is None:
        raise HTTPException(status_code=404, detail="Person not found")
    selected = _parse_day(day)
    language = _lang(request)
    back = f"/attendance/person/{person_id}?month={selected.strftime('%Y-%m')}&lang={language}"
    if mark not in MARKS or not note.strip():
        return RedirectResponse(back + "&error=att_note_required", status_code=303)
    zone = _local_zone()
    try:
        in_at = parse_local(selected, in_time, zone)
        out_at = parse_local(selected, out_time, zone, after=in_at)
    except ValueError:
        return RedirectResponse(back + "&error=att_bad_time", status_code=303)
    item = db.scalar(select(AttendanceAdjustment).where(
        AttendanceAdjustment.person_id == person_id, AttendanceAdjustment.day == selected))
    if item is None:
        item = AttendanceAdjustment(person_id=person_id, day=selected)
        db.add(item)
    item.mark, item.in_at, item.out_at = mark, in_at, out_at
    item.note, item.created_by, item.created_at = note.strip()[:500], user.id, datetime.now(UTC)
    db.add(AuditLog(user_id=user.id, action="attendance.adjust",
                    detail=f"person_id={person_id}; day={selected}; mark={mark}; in={in_time}; out={out_time}; note={note.strip()[:200]}"))
    db.commit()
    return RedirectResponse(back, status_code=303)


@router.post("/adjust/{adjustment_id}/delete")
def delete_adjustment(adjustment_id: int, request: Request, user: User = Depends(require_role("admin")),
                      db: Session = Depends(get_db)) -> RedirectResponse:
    item = db.get(AttendanceAdjustment, adjustment_id)
    if item is None:
        raise HTTPException(status_code=404, detail="Not found")
    person_id, day = item.person_id, item.day
    db.delete(item)
    db.add(AuditLog(user_id=user.id, action="attendance.adjust.delete", detail=f"person_id={person_id}; day={day}"))
    db.commit()
    return RedirectResponse(f"/attendance/person/{person_id}?month={day.strftime('%Y-%m')}&lang={_lang(request)}", status_code=303)


# ---------------------------------------------------------------- settings (admin)


@router.get("/settings", response_class=HTMLResponse)
def settings_page(request: Request, user: User = Depends(require_role("admin")), db: Session = Depends(get_db)) -> HTMLResponse:
    return _render(
        request, "attendance_settings.html", user, tab="settings",
        shifts=db.scalars(select(Shift).order_by(Shift.name)).all(),
        holidays=db.scalars(select(Holiday).order_by(Holiday.day.desc())).all(),
        people=db.scalars(select(Person).where(Person.active.is_(True), Person.category == "employee").order_by(Person.code)).all(),
        weekdays=WEEKDAYS, error=TRANSLATIONS[_lang(request)].get(request.query_params.get("error") or ""),
    )


def _back(request: Request, error: str | None = None) -> RedirectResponse:
    suffix = f"&error={error}" if error else ""
    return RedirectResponse(f"/attendance/settings?lang={_lang(request)}{suffix}", status_code=303)


@router.post("/shifts")
async def create_shift(request: Request, user: User = Depends(require_role("admin")), db: Session = Depends(get_db)):
    form = await request.form()
    name = str(form.get("name", "")).strip()[:100]
    start, end = str(form.get("start", "")), str(form.get("end", ""))
    weekdays = ",".join(sorted({str(day) for day in form.getlist("weekdays") if str(day) in {str(d) for d in WEEKDAYS}}))
    try:
        time.fromisoformat(start), time.fromisoformat(end)
        late, early = int(form.get("late", 10)), int(form.get("early", 10))
    except ValueError:
        return _back(request, "att_bad_time")
    if not name or not weekdays or not (0 <= late <= 240 and 0 <= early <= 240):
        return _back(request, "att_shift_invalid")
    db.add(Shift(name=name, start=start[:5], end=end[:5], late_grace_minutes=late, early_grace_minutes=early, weekdays=weekdays))
    db.add(AuditLog(user_id=user.id, action="shift.create", detail=f"{name} {start}-{end} days={weekdays}"))
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        return _back(request, "att_shift_exists")
    return _back(request)


@router.post("/shifts/{shift_id}/delete")
def delete_shift(shift_id: int, request: Request, user: User = Depends(require_role("admin")), db: Session = Depends(get_db)):
    shift = db.get(Shift, shift_id)
    if shift is not None:
        for person in db.scalars(select(Person).where(Person.shift_id == shift_id)):
            person.shift_id = None
        db.delete(shift)
        db.add(AuditLog(user_id=user.id, action="shift.delete", detail=shift.name))
        db.commit()
    return _back(request)


@router.post("/holidays")
def create_holiday(request: Request, day: str = Form(...), name: str = Form(...),
                   user: User = Depends(require_role("admin")), db: Session = Depends(get_db)):
    try:
        selected = date.fromisoformat(day)
    except ValueError:
        return _back(request, "att_bad_time")
    if not name.strip():
        return _back(request, "att_shift_invalid")
    db.add(Holiday(day=selected, name=name.strip()[:200]))
    db.add(AuditLog(user_id=user.id, action="holiday.create", detail=f"{selected} {name.strip()[:100]}"))
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        return _back(request, "att_holiday_exists")
    return _back(request)


@router.post("/holidays/{holiday_id}/delete")
def delete_holiday(holiday_id: int, request: Request, user: User = Depends(require_role("admin")), db: Session = Depends(get_db)):
    holiday = db.get(Holiday, holiday_id)
    if holiday is not None:
        db.delete(holiday)
        db.add(AuditLog(user_id=user.id, action="holiday.delete", detail=f"{holiday.day} {holiday.name}"))
        db.commit()
    return _back(request)


@router.post("/assign")
async def assign(request: Request, user: User = Depends(require_role("admin")), db: Session = Depends(get_db)):
    """Bulk save of every employee's shift and department from the settings table."""
    form = await request.form()
    shift_ids = set(db.scalars(select(Shift.id)))
    changed = 0
    for person in db.scalars(select(Person).where(Person.active.is_(True), Person.category == "employee")):
        raw_shift = str(form.get(f"shift_{person.id}", ""))
        shift_id = int(raw_shift) if raw_shift.isdigit() and int(raw_shift) in shift_ids else None
        department = str(form.get(f"dept_{person.id}", person.department)).strip()[:100]
        if person.shift_id != shift_id or person.department != department:
            person.shift_id, person.department = shift_id, department
            changed += 1
    if changed:
        db.add(AuditLog(user_id=user.id, action="attendance.assign", detail=f"people={changed}"))
        db.commit()
    return _back(request)
