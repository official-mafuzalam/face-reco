import csv
import io
from collections.abc import Iterator
from datetime import UTC, date, datetime, time, timedelta, timezone, tzinfo
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import FileResponse, HTMLResponse, StreamingResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from app.auth import get_current_user
from app.config import settings
from app.db import get_db
from app.i18n import TRANSLATIONS
from app.models import Camera, Embedding, Event, Person, User


router = APIRouter(prefix="/events", dependencies=[Depends(get_current_user)])
snapshot_router = APIRouter()
templates = Jinja2Templates(directory=Path(__file__).resolve().parents[1] / "templates")
_PROJECT_ROOT = Path(__file__).resolve().parents[3]
_PAGE_SIZE = 50
_STATUSES = ("matched", "unknown", "low_quality")
_DIRECTIONS = ("IN", "OUT")


def _language(request: Request) -> str:
    return "bn" if request.query_params.get("lang") == "bn" else "en"


def _local_zone() -> tzinfo:
    try:
        return ZoneInfo(settings.tz)
    except ZoneInfoNotFoundError as exc:
        if settings.tz == "Asia/Dhaka":
            return timezone(timedelta(hours=6), name="Asia/Dhaka")
        raise RuntimeError(f"Invalid configured timezone: {settings.tz}") from exc


def _utc_boundary(day: date) -> datetime:
    local_midnight = datetime.combine(day, time.min, tzinfo=_local_zone())
    return local_midnight.astimezone(UTC).replace(tzinfo=None)


def _display_time(value: datetime) -> str:
    aware = value.replace(tzinfo=UTC) if value.tzinfo is None else value
    return aware.astimezone(_local_zone()).strftime("%Y-%m-%d %H:%M:%S")


def _digit_format(value: str, enabled: bool) -> str:
    if not enabled:
        return value
    return value.translate(str.maketrans("0123456789", "০১২৩৪৫৬৭৮৯"))


def _filters(
    db: Session,
    *,
    start_date: date | None,
    end_date: date | None,
    gate: str | None,
    direction: str | None,
    person: str | None,
    status: str | None,
):
    statement = select(Event, Person)
    statement = statement.outerjoin(Person, Event.person_id == Person.id)
    conditions = []
    if start_date is not None:
        conditions.append(Event.at >= _utc_boundary(start_date))
    if end_date is not None:
        conditions.append(Event.at < _utc_boundary(end_date + timedelta(days=1)))
    if gate:
        conditions.append(Event.gate == gate)
    if direction:
        conditions.append(Event.direction == direction)
    if person:
        pattern = f"%{person.strip()}%"
        conditions.append(or_(Person.code.ilike(pattern), Person.name.ilike(pattern)))
    if status:
        conditions.append(Event.status == status)
    if conditions:
        statement = statement.where(*conditions)
    return statement


def _event_rows(db: Session, statement, *, page: int = 1, limit: int | None = _PAGE_SIZE):
    if limit is not None:
        statement = statement.limit(limit).offset((page - 1) * limit)
    statement = statement.order_by(Event.at.desc(), Event.id.desc())
    rows = db.execute(statement).all()
    result = []
    for event, person in rows:
        thumb_path: str | None = None
        if person is not None:
            thumb_path = db.scalar(
                select(Embedding.thumb_path)
                .where(Embedding.person_id == person.id, Embedding.thumb_path.is_not(None))
                .order_by(Embedding.id.desc())
                .limit(1)
            )
        result.append(
            {
                "event": event,
                "person": person,
                "time_display": _display_time(event.at),
                "thumbnail": f"/people/{person.id}/samples/{_thumbnail_id(db, person.id)}/thumbnail"
                if person is not None and thumb_path
                else None,
            }
        )
    return result


def _thumbnail_id(db: Session, person_id: int) -> int | None:
    return db.scalar(
        select(Embedding.id)
        .where(Embedding.person_id == person_id, Embedding.thumb_path.is_not(None))
        .order_by(Embedding.id.desc())
        .limit(1)
    )


def _query_filters(request: Request, start: date | None, end: date | None, gate: str | None,
                   direction: str | None, person: str | None, status: str | None):
    language = _language(request)
    today = datetime.now(_local_zone()).date()
    if start is None and end is None:
        start_date = end_date = today
    elif start is None:
        start_date = end_date = end
    elif end is None:
        start_date = end_date = start
    else:
        start_date, end_date = start, end
    if end_date < start_date:
        raise HTTPException(status_code=400, detail=TRANSLATIONS[language]["date_order"])
    if direction and direction not in _DIRECTIONS:
        raise HTTPException(status_code=422, detail=TRANSLATIONS[language]["invalid_filter"])
    if status and status not in _STATUSES:
        raise HTTPException(status_code=422, detail=TRANSLATIONS[language]["invalid_filter"])
    return start_date, end_date, gate, direction, person, status


def _context(request: Request, db: Session, filters, page: int, total: int):
    start_date, end_date, gate, direction, person, status = filters
    language = _language(request)
    bengali_digits = request.query_params.get("bn_digits") == "1"
    statement = _filters(
        db,
        start_date=start_date,
        end_date=end_date,
        gate=gate,
        direction=direction,
        person=person,
        status=status,
    )
    rows = _event_rows(db, statement, page=page)
    cameras = list(db.scalars(select(Camera.gate).distinct().order_by(Camera.gate)))
    return {
        "language": language,
        "t": TRANSLATIONS[language],
        "title": TRANSLATIONS[language]["events_title"],
        "rows": rows,
        "filters": {
            "start": start_date.isoformat(),
            "end": end_date.isoformat(),
            "gate": gate or "",
            "direction": direction or "",
            "person": person or "",
            "status": status or "",
            "bn_digits": bengali_digits,
        },
        "display": lambda value: _digit_format(value, bengali_digits),
        "gates": cameras,
        "page": page,
        "pages": max(1, (total + _PAGE_SIZE - 1) // _PAGE_SIZE),
        "total": total,
        "unreviewed_count": db.scalar(
            select(func.count(Event.id)).where(Event.status == "unknown", Event.reviewed_at.is_(None))
        )
        or 0,
    }


@router.get("", response_class=HTMLResponse)
def list_events(
    request: Request,
    start: date | None = None,
    end: date | None = None,
    gate: str | None = None,
    direction: str | None = None,
    person: str | None = None,
    status: str | None = None,
    page: int = Query(1, ge=1),
    db: Session = Depends(get_db),
) -> HTMLResponse:
    filters = _query_filters(request, start, end, gate, direction, person, status)
    statement = _filters(
        db,
        start_date=filters[0],
        end_date=filters[1],
        gate=filters[2],
        direction=filters[3],
        person=filters[4],
        status=filters[5],
    )
    total = db.scalar(select(func.count()).select_from(statement.order_by(None).subquery())) or 0
    context = _context(request, db, filters, page, total)
    context["rows"] = _event_rows(db, statement, page=page)
    return templates.TemplateResponse(request=request, name="events.html", context=context)


@router.get("/export.csv")
def export_events(
    request: Request,
    start: date | None = None,
    end: date | None = None,
    gate: str | None = None,
    direction: str | None = None,
    person: str | None = None,
    status: str | None = None,
    db: Session = Depends(get_db),
) -> StreamingResponse:
    filters = _query_filters(request, start, end, gate, direction, person, status)
    rows = _event_rows(
        db,
        _filters(
            db,
            start_date=filters[0],
            end_date=filters[1],
            gate=filters[2],
            direction=filters[3],
            person=filters[4],
            status=filters[5],
        ),
        limit=None,
    )

    def content() -> Iterator[bytes]:
        yield b"\xef\xbb\xbf"
        buffer = io.StringIO(newline="")
        writer = csv.writer(buffer)
        writer.writerow(["time", "person_code", "person_name", "gate", "direction", "score", "status"])
        yield buffer.getvalue().encode("utf-8")
        for row in rows:
            event = row["event"]
            person_row = row["person"]
            buffer.seek(0)
            buffer.truncate(0)
            writer.writerow(
                [
                    row["time_display"],
                    person_row.code if person_row else "",
                    person_row.name if person_row else "",
                    event.gate,
                    event.direction,
                    "" if event.score is None else f"{event.score:.6f}",
                    event.status,
                ]
            )
            yield buffer.getvalue().encode("utf-8")

    return StreamingResponse(
        content(),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": 'attachment; filename="events.csv"'},
    )


@snapshot_router.get("/events/{event_id}/snapshot")
def event_snapshot(
    event_id: int,
    _user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> FileResponse:
    event = db.get(Event, event_id)
    if event is None or not event.snapshot_path:
        raise HTTPException(status_code=404, detail="Snapshot not found")
    root = (_PROJECT_ROOT / "data" / "snapshots").resolve()
    path = (root.parent.parent / event.snapshot_path).resolve()
    if root not in path.parents or not path.is_file():
        raise HTTPException(status_code=404, detail="Snapshot not found")
    return FileResponse(path, media_type="image/jpeg")
