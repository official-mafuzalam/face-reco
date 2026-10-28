from pathlib import Path
from urllib.parse import quote, urlsplit, urlunsplit

from cryptography.fernet import Fernet
from fastapi import APIRouter, Depends, Form, HTTPException, Request, Response
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.auth import get_current_user, require_role
from app.camera.manager import manager
from app.camera.reader import mask_credentials
from app.config import settings
from app.db import get_db
from app.i18n import TRANSLATIONS
from app.models import Camera


router = APIRouter(prefix="/cameras", dependencies=[Depends(require_role("admin"))])
snapshot_router = APIRouter()
templates = Jinja2Templates(directory=Path(__file__).resolve().parents[1] / "templates")
fernet = Fernet(settings.fernet_key.encode())


def _lang(request: Request) -> str:
    return "bn" if request.query_params.get("lang") == "bn" else "en"


def _source_error(source: str) -> str | None:
    if source.startswith("webcam:"):
        return None if source.removeprefix("webcam:").isdigit() else "invalid_camera"
    if source.startswith("file:"):
        return None if source.removeprefix("file:").strip() else "invalid_camera"
    if source.lower().startswith("rtsp://"):
        try:
            parts = urlsplit(source)
            if not parts.hostname:
                return "invalid_camera"
            _ = parts.port
            return None
        except ValueError:
            return "invalid_camera"
    return "invalid_camera"


def _source_for_edit(source: str) -> str:
    return mask_credentials(source)


def _keep_password(source: str, old_source: str, password: str) -> str:
    if not source.lower().startswith("rtsp://"):
        return source
    parts = urlsplit(source)
    if password:
        selected_password = password
    else:
        old_parts = urlsplit(old_source)
        if parts.password == "***":
            selected_password = old_parts.password or ""
        elif old_source:
            selected_password = old_parts.password or ""
        else:
            selected_password = parts.password or ""
    username = parts.username
    if username in (None, "***"):
        username = urlsplit(old_source).username or username
    if username is None and not selected_password:
        return source
    host = parts.hostname or ""
    if ":" in host and not host.startswith("["):
        host = f"[{host}]"
    if parts.port is not None:
        host = f"{host}:{parts.port}"
    if username is not None or selected_password:
        userinfo = quote(username or "", safe="")
        if selected_password:
            userinfo += ":" + quote(selected_password, safe="")
        host = f"{userinfo}@{host}"
    netloc = host
    return urlunsplit((parts.scheme, netloc, parts.path, parts.query, parts.fragment))


def _decrypt(camera: Camera) -> str:
    return fernet.decrypt(camera.rtsp_url_encrypted.encode()).decode()


def _camera_rows(db: Session) -> list[tuple[Camera, object]]:
    cameras = list(db.scalars(select(Camera).order_by(Camera.id)))
    return [(camera, manager.get_state(camera.id)) for camera in cameras]


def _render_form(
    request: Request,
    *,
    camera: Camera | None,
    error: str | None = None,
    source: str | None = None,
    password: str = "",
) -> HTMLResponse:
    language = _lang(request)
    t = TRANSLATIONS[language]
    raw_source = source if source is not None else (_decrypt(camera) if camera else "")
    return templates.TemplateResponse(
        request=request,
        name="camera_form.html",
        context={
            "language": language,
            "t": t,
            "title": t["edit_camera_title"] if camera else t["add_camera_title"],
            "camera": camera,
            "source": _source_for_edit(raw_source),
            "password": password,
            "error": t[error] if error else None,
        },
    )


@router.get("", response_class=HTMLResponse)
def list_cameras(request: Request, db: Session = Depends(get_db)) -> HTMLResponse:
    language = _lang(request)
    return templates.TemplateResponse(
        request=request,
        name="cameras.html",
        context={
            "language": language,
            "t": TRANSLATIONS[language],
            "title": TRANSLATIONS[language]["cameras_title"],
            "camera_rows": _camera_rows(db),
            "fragment": False,
        },
    )


@router.get("/status", response_class=HTMLResponse)
def camera_status(request: Request, db: Session = Depends(get_db)) -> HTMLResponse:
    language = _lang(request)
    return templates.TemplateResponse(
        request=request,
        name="cameras.html",
        context={
            "language": language,
            "t": TRANSLATIONS[language],
            "title": TRANSLATIONS[language]["cameras_title"],
            "camera_rows": _camera_rows(db),
            "fragment": True,
        },
    )


@router.get("/new", response_class=HTMLResponse)
def new_camera(request: Request) -> HTMLResponse:
    return _render_form(request, camera=None)


@router.post("/new", response_class=HTMLResponse)
def create_camera(
    request: Request,
    name: str = Form(...),
    gate: str = Form(...),
    direction: str = Form(...),
    source: str = Form(...),
    password: str = Form(""),
    enabled: str | None = Form(None),
    db: Session = Depends(get_db),
) -> Response:
    source = source.strip()
    error = _source_error(source)
    if direction not in {"IN", "OUT"} or not name.strip() or not gate.strip() or error:
        return _render_form(request, camera=None, error=error or "invalid_camera", source=source)
    if source.lower().startswith("rtsp://") and not password and not urlsplit(source).password:
        return _render_form(request, camera=None, error="password_required", source=source)
    source = _keep_password(source, "", password)
    camera = Camera(
        name=name.strip(),
        gate=gate.strip(),
        direction=direction,
        rtsp_url_encrypted=fernet.encrypt(source.encode()).decode(),
        enabled=enabled is not None,
    )
    db.add(camera)
    db.commit()
    db.refresh(camera)
    if camera.enabled:
        manager.start(camera.id, source)
    return RedirectResponse(f"/cameras?lang={_lang(request)}", status_code=303)


@router.get("/{camera_id}/edit", response_class=HTMLResponse)
def edit_camera(camera_id: int, request: Request, db: Session = Depends(get_db)) -> Response:
    camera = db.get(Camera, camera_id)
    if camera is None:
        raise HTTPException(status_code=404, detail=TRANSLATIONS[_lang(request)]["camera_not_found"])
    return _render_form(request, camera=camera)


@router.post("/{camera_id}/edit", response_class=HTMLResponse)
def update_camera(
    camera_id: int,
    request: Request,
    name: str = Form(...),
    gate: str = Form(...),
    direction: str = Form(...),
    source: str = Form(...),
    password: str = Form(""),
    enabled: str | None = Form(None),
    db: Session = Depends(get_db),
) -> Response:
    camera = db.get(Camera, camera_id)
    if camera is None:
        raise HTTPException(status_code=404, detail=TRANSLATIONS[_lang(request)]["camera_not_found"])
    source = source.strip()
    error = _source_error(source)
    if direction not in {"IN", "OUT"} or not name.strip() or not gate.strip() or error:
        return _render_form(request, camera=camera, error=error or "invalid_camera", source=source)
    old_source = _decrypt(camera)
    source = _keep_password(source, old_source, password)
    camera.name = name.strip()
    camera.gate = gate.strip()
    camera.direction = direction
    camera.rtsp_url_encrypted = fernet.encrypt(source.encode()).decode()
    camera.enabled = enabled is not None
    db.commit()
    if camera.enabled:
        manager.restart(camera.id, source)
    else:
        manager.stop(camera.id)
    return RedirectResponse(f"/cameras?lang={_lang(request)}", status_code=303)


@router.post("/{camera_id}/toggle")
def toggle_camera(camera_id: int, request: Request, db: Session = Depends(get_db)) -> Response:
    camera = db.get(Camera, camera_id)
    if camera is None:
        raise HTTPException(status_code=404, detail=TRANSLATIONS[_lang(request)]["camera_not_found"])
    camera.enabled = not camera.enabled
    db.commit()
    if camera.enabled:
        manager.start(camera.id, _decrypt(camera))
    else:
        manager.stop(camera.id)
    return RedirectResponse(f"/cameras?lang={_lang(request)}", status_code=303)


@snapshot_router.get("/cameras/{camera_id}/snapshot.jpg")
def snapshot(
    camera_id: int,
    _user=Depends(get_current_user),
    db: Session = Depends(get_db),
) -> Response:
    import cv2

    camera = db.get(Camera, camera_id)
    if camera is None or not camera.enabled:
        raise HTTPException(status_code=404, detail="Camera not available")
    frame = manager.get_latest_frame(camera_id)
    if frame is None:
        raise HTTPException(status_code=404, detail="Frame not available")
    ok, encoded = cv2.imencode(".jpg", frame)
    if not ok:
        raise HTTPException(status_code=500, detail="Unable to encode frame")
    return Response(content=encoded.tobytes(), media_type="image/jpeg")
