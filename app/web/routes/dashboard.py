from datetime import UTC, datetime, time, timedelta
from pathlib import Path

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.auth import get_optional_current_user
from app.camera.manager import manager
from app.db import get_db
from app.i18n import TRANSLATIONS
from app.models import Camera, Event, Incident, Person, User
from app.services.attendance_report import build as build_attendance
from app.services.summary import build_daily_summary, summary_text
from app.web.routes.events import _local_zone
from app.web.routes.health import health_problems


router = APIRouter()
templates = Jinja2Templates(directory=Path(__file__).resolve().parents[1] / "templates")


def _zone():
    return _local_zone()


def _today_range() -> tuple[datetime, datetime]:
    today = datetime.now(_zone()).date()
    start = datetime.combine(today, time.min, tzinfo=_zone()).astimezone(UTC).replace(tzinfo=None)
    next_day = datetime.combine(today + timedelta(days=1), time.min, tzinfo=_zone())
    return start, next_day.astimezone(UTC).replace(tzinfo=None)


def _time_label(value: datetime) -> str:
    aware = value.replace(tzinfo=UTC) if value.tzinfo is None else value
    return aware.astimezone(_zone()).strftime("%Y-%m-%d %H:%M:%S")


def _row(event: Event, person: Person | None) -> dict[str, object]:
    return {
        "event": event,
        "person": person,
        "time_display": _time_label(event.at),
        "person_display": (
            f"{person.name} ({person.code})" if person is not None else "Unknown"
        ),
    }


def _attendance_today(db: Session) -> dict[str, int] | None:
    """Present / late / absent employees today; None when nobody is set up for attendance."""
    today = datetime.now(_zone()).date()
    results = [row.days[0] for row in build_attendance(db, today, today, _zone())]
    if not results:
        return None
    return {
        "present": sum(r.status in ("present", "late") for r in results),
        "late": sum(r.status == "late" for r in results),
        "absent": sum(r.status == "absent" for r in results),
        "total": len(results),
    }


def _dashboard_data(db: Session) -> dict[str, object]:
    start, end = _today_range()
    incoming = db.scalar(
        select(func.count(Event.id)).where(
            Event.at >= start,
            Event.at < end,
            Event.direction == "IN",
            Event.status == "matched",
        )
    ) or 0
    outgoing = db.scalar(
        select(func.count(Event.id)).where(
            Event.at >= start,
            Event.at < end,
            Event.direction == "OUT",
            Event.status == "matched",
        )
    ) or 0
    unknown = db.scalar(
        select(func.count(Event.id)).where(
            Event.at >= start,
            Event.at < end,
            Event.status == "unknown",
        )
    ) or 0

    ordered = list(
        db.execute(
            select(Event, Person)
            .outerjoin(Person, Event.person_id == Person.id)
            .where(Event.at >= start, Event.at < end)
            .order_by(Event.at.desc(), Event.id.desc())
        )
    )
    inside: list[dict[str, object]] = []
    seen_people: set[int] = set()
    for event, person in ordered:
        if event.person_id is None or event.person_id in seen_people:
            continue
        seen_people.add(event.person_id)
        if event.status == "matched" and event.direction == "IN" and person is not None:
            inside.append(_row(event, person))

    recent = db.execute(
        select(Event, Person)
        .outerjoin(Person, Event.person_id == Person.id)
        .order_by(Event.at.desc(), Event.id.desc())
        .limit(20)
    ).all()
    cameras = [
        {
            "camera": camera,
            "status": manager.get_state(camera.id).status if camera.enabled else "offline",
        }
        for camera in db.scalars(select(Camera).where(Camera.deleted_at.is_(None)).order_by(Camera.name))
    ]
    return {
        "incoming": incoming,
        "outgoing": outgoing,
        "unknown": unknown,
        "inside": inside,
        "recent": [_row(event, person) for event, person in recent],
        "cameras": cameras,
        "camera_names": {item["camera"].id: item["camera"].name for item in cameras},
        "recent_alerts": [
            {"incident": incident, "time_display": _time_label(incident.started_at)}
            for incident in db.scalars(
                select(Incident)
                .where(Incident.severity.in_(("warning", "critical")), Incident.reviewed_at.is_(None))
                .order_by(Incident.started_at.desc())
                .limit(5)
            )
        ],
        "attendance": _attendance_today(db),
        "unreviewed_count": db.scalar(
            select(func.count(Event.id)).where(Event.status == "unknown", Event.reviewed_at.is_(None))
        )
        or 0,
    }


def _render_dashboard(request: Request, db: Session, user: User | None = None) -> HTMLResponse:
    language = "bn" if request.query_params.get("lang") == "bn" else "en"
    summary = build_daily_summary(db, *_today_range())
    return templates.TemplateResponse(
        request=request,
        name="dashboard.html",
        context={
            "language": language,
            "t": TRANSLATIONS[language],
            "title": TRANSLATIONS[language]["dashboard_title"],
            "is_admin": user is not None and user.role == "admin",
            "health_problems": health_problems(db, language),
            "summary": summary,
            "summary_text": summary_text(summary, TRANSLATIONS[language]),
            **_dashboard_data(db),
        },
    )


def _dashboard_response(
    request: Request,
    user: User | None,
    db: Session,
) -> Response:
    if user is None:
        language = "bn" if request.query_params.get("lang") == "bn" else "en"
        return RedirectResponse(f"/login?lang={language}", status_code=303)
    return _render_dashboard(request, db, user)


@router.get("/dashboard/refresh", response_class=HTMLResponse)
def refresh_dashboard(
    request: Request,
    user: User | None = Depends(get_optional_current_user),
    db: Session = Depends(get_db),
) -> Response:
    return _dashboard_response(request, user, db)
