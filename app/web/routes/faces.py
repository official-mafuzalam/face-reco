"""Search a face across the gate log, and enrol an unknown face as a person."""

from datetime import UTC, datetime
from pathlib import Path

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.auth import get_current_user, require_role
from app.db import get_db
from app.i18n import TRANSLATIONS
from app.models import PERSON_CATEGORIES, AuditLog, Event, Person, User
from app.services.enroll import decode_upload
from app.services.face_search import (
    decrypt_vector,
    embedding_from_image,
    enroll_from_event,
    similar_events,
    similar_people,
)
from app.web.routes.events import _display_time

router = APIRouter(prefix="/faces", dependencies=[Depends(get_current_user)])
templates = Jinja2Templates(directory=Path(__file__).resolve().parents[1] / "templates")
templates.env.filters["local_time"] = _display_time

# Enrolment failure reasons -> translation keys (shared with the People page where possible).
_REASONS = {
    "face_source_missing": "face_source_missing",
    "sample_too_similar": "enroll_similar",
    "duplicate_person": "enroll_duplicate",
}


def _lang(request: Request) -> str:
    return "bn" if request.query_params.get("lang") == "bn" else "en"


def _render(request: Request, user: User, **context) -> HTMLResponse:
    language = _lang(request)
    t = TRANSLATIONS[language]
    defaults = {
        "event": None, "matches": [], "people": [], "error": None, "message": None,
        "searched": False, "is_admin": user.role == "admin", "categories": PERSON_CATEGORIES,
        "all_people": [],
    }
    defaults.update(context)
    return templates.TemplateResponse(
        request=request,
        name="faces.html",
        context={"language": language, "t": t, "title": t["face_search_title"], **defaults},
    )


def _unknown_event(db: Session, event_id: int) -> Event:
    event = db.get(Event, event_id)
    if event is None or event.embedding_encrypted is None:
        raise HTTPException(status_code=404, detail="Face not found")
    return event


@router.get("/search", response_class=HTMLResponse)
def search_page(request: Request, user: User = Depends(get_current_user)) -> HTMLResponse:
    return _render(request, user)


@router.post("/search", response_class=HTMLResponse)
def search_photo(
    request: Request,
    photo: UploadFile = File(...),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> HTMLResponse:
    t = TRANSLATIONS[_lang(request)]
    try:
        image = decode_upload(photo.file.read(), photo.filename)
        query = embedding_from_image(image)
    except ValueError as exc:
        key = {"exactly_one_face_required": "enroll_one_face"}.get(str(exc), "enroll_invalid_image")
        return _render(request, user, error=t.get(key, t["enroll_invalid_image"]))
    db.add(AuditLog(user_id=user.id, action="face.search", detail="by uploaded photo"))
    db.commit()
    return _render(
        request, user, searched=True,
        matches=similar_events(db, query), people=similar_people(db, query),
    )


@router.get("/similar/{event_id}", response_class=HTMLResponse)
def similar_page(
    event_id: int,
    request: Request,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> HTMLResponse:
    event = _unknown_event(db, event_id)
    query = decrypt_vector(event.embedding_encrypted)
    if query is None:
        raise HTTPException(status_code=404, detail="Face not readable")
    all_people = db.scalars(select(Person).where(Person.active.is_(True)).order_by(Person.code)).all() if user.role == "admin" else []
    return _render(
        request, user, searched=True, event=event,
        matches=similar_events(db, query, exclude_id=event.id),
        people=similar_people(db, query),
        all_people=all_people,
        message=TRANSLATIONS[_lang(request)].get(request.query_params.get("msg") or "", None),
    )


@router.post("/enroll/{event_id}", response_class=HTMLResponse)
def enroll_unknown(
    event_id: int,
    request: Request,
    mode: str = Form(...),
    person_id: int | None = Form(None),
    code: str = Form(""),
    name: str = Form(""),
    category: str = Form("employee"),
    user: User = Depends(require_role("admin")),
    db: Session = Depends(get_db),
) -> HTMLResponse:
    """Add this unknown face to a new or an existing person."""
    t = TRANSLATIONS[_lang(request)]
    event = _unknown_event(db, event_id)
    if mode == "existing":
        person = db.get(Person, person_id) if person_id else None
        if person is None:
            return _error(request, user, db, event, t["person_required"])
        created = False
    else:
        code, name = code.strip(), name.strip()
        if not code or not name:
            return _error(request, user, db, event, t["person_required"])
        person = Person(code=code, name=name, category=category if category in PERSON_CATEGORIES else "employee")
        db.add(person)
        try:
            db.flush()
        except IntegrityError:
            db.rollback()
            return _error(request, user, db, event, t["person_code_exists"])
        created = True

    result = enroll_from_event(db, event, person, user.id)
    if not result.accepted:
        db.rollback()
        reason = result.reasons[0] if result.reasons else "face_source_missing"
        message = t.get(_REASONS.get(reason, "enroll_invalid_image"), t["enroll_invalid_image"])
        if result.duplicate_person is not None:
            message = t["enroll_duplicate_detail"].format(code=result.duplicate_person[0], name=result.duplicate_person[1])
        return _error(request, user, db, event, message)

    event = db.get(Event, event_id)
    if event.reviewed_at is None:
        event.reviewed_by = user.id
        event.reviewed_at = datetime.now(UTC)
    db.add(
        AuditLog(
            user_id=user.id,
            action="person.create_from_unknown" if created else "person.enroll_from_unknown",
            detail=f"person_id={person.id}; event_id={event_id}",
        )
    )
    db.commit()
    return _render(
        request, user, searched=True, event=event,
        matches=similar_events(db, decrypt_vector(event.embedding_encrypted), exclude_id=event.id),
        people=similar_people(db, decrypt_vector(event.embedding_encrypted)),
        message=t["face_enrolled"].format(name=person.name, code=person.code),
    )


def _error(request: Request, user: User, db: Session, event: Event, message: str) -> HTMLResponse:
    query = decrypt_vector(event.embedding_encrypted)
    all_people = db.scalars(select(Person).where(Person.active.is_(True)).order_by(Person.code)).all()
    return _render(
        request, user, searched=True, event=event, error=message, all_people=all_people,
        matches=similar_events(db, query, exclude_id=event.id) if query is not None else [],
        people=similar_people(db, query) if query is not None else [],
    )
