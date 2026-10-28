from datetime import UTC, datetime
from pathlib import Path

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.auth import get_current_user
from app.db import get_db
from app.i18n import TRANSLATIONS
from app.models import AuditLog, Event, Person, User
from app.web.routes.events import _display_time


router = APIRouter(prefix="/alerts", dependencies=[Depends(get_current_user)])
templates = Jinja2Templates(directory=Path(__file__).resolve().parents[1] / "templates")


def _lang(request: Request) -> str:
    return "bn" if request.query_params.get("lang") == "bn" else "en"


def _alert_rows(db: Session, reviewed: str) -> list[dict[str, object]]:
    statement = (
        select(Event, Person)
        .outerjoin(Person, Event.person_id == Person.id)
        .where(Event.status == "unknown")
        .order_by(Event.at.desc(), Event.id.desc())
    )
    if reviewed == "reviewed":
        statement = statement.where(Event.reviewed_at.is_not(None))
    elif reviewed == "unreviewed":
        statement = statement.where(Event.reviewed_at.is_(None))
    return [
        {
            "event": event,
            "person": person,
            "time_display": _display_time(event.at),
        }
        for event, person in db.execute(statement)
    ]


def _render(request: Request, db: Session, reviewed: str) -> HTMLResponse:
    language = _lang(request)
    if reviewed not in {"all", "reviewed", "unreviewed"}:
        raise HTTPException(status_code=422, detail=TRANSLATIONS[language]["invalid_filter"])
    rows = _alert_rows(db, reviewed)
    count = db.scalar(
        select(func.count(Event.id)).where(Event.status == "unknown", Event.reviewed_at.is_(None))
    ) or 0
    latest_id = max((row["event"].id for row in rows), default=0)
    return templates.TemplateResponse(
        request=request,
        name="alerts.html",
        context={
            "language": language,
            "t": TRANSLATIONS[language],
            "title": TRANSLATIONS[language]["alerts_title"],
            "rows": rows,
            "reviewed": reviewed,
            "unreviewed_count": count,
            "latest_id": latest_id,
            "fragment": request.url.path.endswith("/fragment"),
        },
    )


@router.get("", response_class=HTMLResponse)
def alerts(
    request: Request,
    reviewed: str = "unreviewed",
    db: Session = Depends(get_db),
) -> HTMLResponse:
    return _render(request, db, reviewed)


@router.get("/fragment", response_class=HTMLResponse)
def alert_fragment(
    request: Request,
    reviewed: str = "unreviewed",
    db: Session = Depends(get_db),
) -> HTMLResponse:
    return _render(request, db, reviewed)


@router.post("/{event_id}/review")
def review_alert(
    event_id: int,
    request: Request,
    note: str = Form(""),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> RedirectResponse:
    event = db.get(Event, event_id)
    if event is None or event.status != "unknown":
        raise HTTPException(status_code=404, detail="Unknown event not found")
    event.reviewed_by = user.id
    event.reviewed_at = datetime.now(UTC)
    db.add(
        AuditLog(
            user_id=user.id,
            action="event.review",
            detail=f"event_id={event.id}; note={note.strip()[:1000]}",
        )
    )
    db.commit()
    return RedirectResponse(
        f"/alerts?reviewed={request.query_params.get('reviewed', 'unreviewed')}"
        f"&lang={_lang(request)}",
        status_code=303,
    )
