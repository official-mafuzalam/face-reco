import time
from collections.abc import Iterator
from pathlib import Path

import cv2
import numpy as np
from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse, StreamingResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.auth import get_current_user, require_role
from app.camera.manager import manager
from app.db import get_db
from app.i18n import TRANSLATIONS
from app.models import AuditLog, Camera, Embedding, Person, User
from app.services.enroll import (
    MAX_UPLOAD_BYTES,
    camera_frame,
    clear_person_samples,
    decode_upload,
    enroll_image,
    thumbnail_path,
)


router = APIRouter(prefix="/people", dependencies=[Depends(require_role("admin"))])
stream_router = APIRouter()
thumbnail_router = APIRouter()
templates = Jinja2Templates(directory=Path(__file__).resolve().parents[1] / "templates")

_REASON_KEYS = {
    "exactly_one_face_required": "enroll_one_face",
    "face_too_small": "enroll_face_small",
    "detection_score_low": "enroll_score_low",
    "blurred": "enroll_blurred",
    "face_out_of_bounds": "enroll_out_of_bounds",
    "invalid_landmarks": "enroll_invalid_landmarks",
    "extreme_yaw": "enroll_extreme_yaw",
    "sample_too_similar": "enroll_similar",
    "duplicate_person": "enroll_duplicate",
    "invalid_image": "enroll_invalid_image",
    "unsupported_image_type": "enroll_unsupported_image",
    "image_size_invalid": "enroll_image_size",
}


def _language(request: Request) -> str:
    return "bn" if request.query_params.get("lang") == "bn" else "en"


def _render(
    request: Request,
    name: str,
    title_key: str,
    context: dict[str, object] | None = None,
) -> HTMLResponse:
    language = _language(request)
    values = {
        "language": language,
        "t": TRANSLATIONS[language],
        "title": TRANSLATIONS[language][title_key],
    }
    if context:
        values.update(context)
    return templates.TemplateResponse(request=request, name=name, context=values)


def _person_or_404(db: Session, person_id: int) -> Person:
    person = db.get(Person, person_id)
    if person is None:
        raise HTTPException(status_code=404, detail="Person not found")
    return person


def _result_message(result, language: str) -> str | None:
    if result.accepted:
        return TRANSLATIONS[language]["enroll_success"]
    t = TRANSLATIONS[language]
    if result.duplicate_person is not None:
        code, name = result.duplicate_person
        return t["enroll_duplicate_detail"].format(code=code, name=name)
    if result.reasons:
        return t[_REASON_KEYS.get(result.reasons[0], "enroll_invalid_image")]
    return t["enroll_invalid_image"]


def _people_rows(db: Session, query: str) -> list[tuple[Person, int]]:
    statement = select(Person, func.count(Embedding.id)).outerjoin(
        Embedding, Embedding.person_id == Person.id
    )
    if query:
        pattern = f"%{query}%"
        statement = statement.where(or_(Person.code.ilike(pattern), Person.name.ilike(pattern)))
    statement = statement.group_by(Person.id).order_by(Person.code)
    return [(person, int(count)) for person, count in db.execute(statement)]


@router.get("", response_class=HTMLResponse)
def list_people(
    request: Request,
    q: str = "",
    db: Session = Depends(get_db),
) -> HTMLResponse:
    return _render(
        request,
        "people.html",
        "people_title",
        {"people": _people_rows(db, q.strip()), "query": q},
    )


@router.get("/new", response_class=HTMLResponse)
def new_person(request: Request) -> HTMLResponse:
    return _render(
        request,
        "person_form.html",
        "add_person",
        {"form_code": "", "form_name": "", "error": None},
    )


@router.post("/new", response_class=HTMLResponse)
def add_person(
    request: Request,
    code: str = Form(...),
    name: str = Form(...),
    db: Session = Depends(get_db),
) -> HTMLResponse:
    code = code.strip()
    name = name.strip()
    if not code or not name:
        return _render(
            request,
            "person_form.html",
            "add_person",
            {
                "error": TRANSLATIONS[_language(request)]["person_required"],
                "form_code": code,
                "form_name": name,
            },
        )
    person = Person(code=code, name=name)
    db.add(person)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        return _render(
            request,
            "person_form.html",
            "add_person",
            {
                "error": TRANSLATIONS[_language(request)]["person_code_exists"],
                "form_code": code,
                "form_name": name,
            },
        )
    db.refresh(person)
    db.add(AuditLog(action="person.create", detail=f"person_id={person.id}"))
    db.commit()
    return RedirectResponse(f"/people?lang={_language(request)}", status_code=303)


@router.post("/{person_id}/active")
def set_person_active(
    person_id: int,
    request: Request,
    active: str = Form(...),
    db: Session = Depends(get_db),
) -> RedirectResponse:
    person = _person_or_404(db, person_id)
    person.active = active == "true"
    db.add(
        AuditLog(
            action="person.activate" if person.active else "person.deactivate",
            detail=f"person_id={person.id}",
        )
    )
    db.commit()
    return RedirectResponse(f"/people?lang={_language(request)}", status_code=303)


@router.get("/{person_id}/enroll", response_class=HTMLResponse)
def enroll_page(
    person_id: int,
    request: Request,
    db: Session = Depends(get_db),
) -> HTMLResponse:
    person = _person_or_404(db, person_id)
    cameras = list(db.scalars(select(Camera).where(Camera.enabled.is_(True)).order_by(Camera.name)))
    sample_count = db.scalar(select(func.count(Embedding.id)).where(Embedding.person_id == person_id)) or 0
    return _render(
        request,
        "enroll.html",
        "enroll_title",
        {
            "person": person,
            "cameras": cameras,
            "sample_count": sample_count,
            "message": None,
            "message_error": False,
        },
    )


def _enroll_response(
    request: Request,
    person_id: int,
    db: Session,
    camera_id: int | None,
    image: np.ndarray | None,
    user: User,
) -> HTMLResponse:
    person = _person_or_404(db, person_id)
    if image is None:
        message = TRANSLATIONS[_language(request)]["camera_no_frame"]
        accepted = False
    else:
        result = enroll_image(db, person, user.id, image)
        message = _result_message(result, _language(request))
        accepted = result.accepted
    cameras = list(db.scalars(select(Camera).where(Camera.enabled.is_(True)).order_by(Camera.name)))
    sample_count = db.scalar(select(func.count(Embedding.id)).where(Embedding.person_id == person_id)) or 0
    return _render(
        request,
        "enroll.html",
        "enroll_title",
        {
            "person": person,
            "cameras": cameras,
            "camera_id": camera_id,
            "sample_count": sample_count,
            "message": message,
            "message_error": not accepted,
        },
    )


@router.post("/{person_id}/enroll/capture", response_class=HTMLResponse)
def capture_enrollment(
    person_id: int,
    request: Request,
    camera_id: int = Form(...),
    user: User = Depends(require_role("admin")),
    db: Session = Depends(get_db),
) -> HTMLResponse:
    camera = db.get(Camera, camera_id)
    frame = camera_frame(camera_id) if camera is not None and camera.enabled else None
    return _enroll_response(request, person_id, db, camera_id, frame, user)


@router.post("/{person_id}/enroll/upload", response_class=HTMLResponse)
async def upload_enrollment(
    person_id: int,
    request: Request,
    photo: UploadFile = File(...),
    user: User = Depends(require_role("admin")),
    db: Session = Depends(get_db),
) -> HTMLResponse:
    data = await photo.read(MAX_UPLOAD_BYTES + 1)
    try:
        image = decode_upload(data, photo.filename)
    except ValueError as exc:
        language = _language(request)
        error_key = str(exc) if str(exc) in TRANSLATIONS[language] else "enroll_invalid_image"
        return _enroll_response_with_message(
            request,
            person_id,
            db,
            TRANSLATIONS[language][error_key],
        )
    return _enroll_response(request, person_id, db, None, image, user)


def _enroll_response_with_message(
    request: Request,
    person_id: int,
    db: Session,
    message: str,
) -> HTMLResponse:
    person = _person_or_404(db, person_id)
    cameras = list(db.scalars(select(Camera).where(Camera.enabled.is_(True)).order_by(Camera.name)))
    sample_count = db.scalar(select(func.count(Embedding.id)).where(Embedding.person_id == person_id)) or 0
    return _render(
        request,
        "enroll.html",
        "enroll_title",
        {
            "person": person,
            "cameras": cameras,
            "sample_count": sample_count,
            "message": message,
            "message_error": True,
        },
    )


@router.post("/{person_id}/enroll/reset")
def reset_enrollment(
    person_id: int,
    request: Request,
    user: User = Depends(require_role("admin")),
    db: Session = Depends(get_db),
) -> RedirectResponse:
    person = _person_or_404(db, person_id)
    clear_person_samples(db, person, user.id)
    return RedirectResponse(f"/people/{person_id}/enroll?lang={_language(request)}", status_code=303)


@stream_router.get("/cameras/{camera_id}/stream.mjpg")
def camera_stream(
    camera_id: int,
    _user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> StreamingResponse:
    camera = db.get(Camera, camera_id)
    if camera is None or not camera.enabled:
        raise HTTPException(status_code=404, detail="Camera not available")

    def frames() -> Iterator[bytes]:
        while True:
            frame = manager.get_latest_frame(camera_id)
            if frame is not None:
                ok, encoded = cv2.imencode(".jpg", frame)
                if ok:
                    yield (
                        b"--frame\r\nContent-Type: image/jpeg\r\n\r\n"
                        + encoded.tobytes()
                        + b"\r\n"
                    )
            time.sleep(0.2)

    return StreamingResponse(
        frames(),
        media_type="multipart/x-mixed-replace; boundary=frame",
        headers={"Cache-Control": "no-store"},
    )


@thumbnail_router.get("/people/{person_id}/samples/{embedding_id}/thumbnail")
def enrollment_thumbnail(
    person_id: int,
    embedding_id: int,
    _user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> FileResponse:
    record = db.get(Embedding, embedding_id)
    if record is None or record.person_id != person_id:
        raise HTTPException(status_code=404, detail="Thumbnail not found")
    path = thumbnail_path(record)
    if path is None or not path.is_file():
        raise HTTPException(status_code=404, detail="Thumbnail not found")
    return FileResponse(path, media_type="image/jpeg")
