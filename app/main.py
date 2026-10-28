from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import parse_qs

from cryptography.fernet import Fernet
from fastapi import Depends, FastAPI, Request, Response
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from app.auth import (
    authenticate,
    clear_login_cookie,
    get_optional_current_user,
    set_login_cookie,
)
from app.config import settings
from app.db import Base, engine, get_db
from app.db import SessionLocal
from app.i18n import TRANSLATIONS
from app.models import User
from app.models import Camera
from app.web.routes.cameras import router as cameras_router, snapshot_router
from app.camera.manager import manager
from app.web.routes.people import router as people_router, stream_router as people_stream_router, thumbnail_router
from app.pipeline.worker import load_active_gallery, pipeline_manager
from app.web.routes.dashboard import _render_dashboard, router as dashboard_router
from app.web.routes.events import router as events_router, snapshot_router as event_snapshot_router
from app.web.routes.alerts import router as alerts_router
from app.jobs.retention import start_daily_retention, stop_daily_retention


@asynccontextmanager
async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
    Base.metadata.create_all(engine)
    retention_job = None
    try:
        with SessionLocal() as db:
            cameras = db.query(Camera).filter(Camera.enabled.is_(True)).all()
            manager.start_enabled(
                (camera.id, _fernet.decrypt(camera.rtsp_url_encrypted.encode()).decode())
                for camera in cameras
            )
            gallery = load_active_gallery(db)
            pipeline_manager.start_enabled(
                [(camera.id, camera.gate, camera.direction) for camera in cameras],
                gallery,
            )
        retention_job = start_daily_retention()
        yield
    finally:
        try:
            if retention_job is not None:
                stop_daily_retention(*retention_job)
        finally:
            try:
                pipeline_manager.stop_all()
            finally:
                manager.stop_all()


app = FastAPI(title="gate-face", lifespan=lifespan)
templates = Jinja2Templates(directory=Path(__file__).resolve().parent / "web" / "templates")
_fernet = Fernet(settings.fernet_key.encode())
_TEXT = TRANSLATIONS
app.include_router(cameras_router)
app.include_router(snapshot_router)
app.include_router(dashboard_router)
app.include_router(events_router)
app.include_router(event_snapshot_router)
app.include_router(alerts_router)
app.include_router(people_router)
app.include_router(people_stream_router)
app.include_router(thumbnail_router)


def encrypt_rtsp_url(rtsp_url: str) -> str:
    return _fernet.encrypt(rtsp_url.encode()).decode()


def decrypt_rtsp_url(encrypted_url: str) -> str:
    return _fernet.decrypt(encrypted_url.encode()).decode()


def _language(request: Request) -> str:
    return "bn" if request.query_params.get("lang") == "bn" else "en"


@app.get("/health")
def health(db: Session = Depends(get_db)) -> dict[str, str | int]:
    cameras = db.query(Camera).all()
    online = sum(
        camera.enabled and manager.get_state(camera.id).status == "online"
        for camera in cameras
    )
    return {
        "status": "ok",
        "cameras_online": online,
        "cameras_offline": len(cameras) - online,
    }


@app.get("/", response_class=HTMLResponse)
def home(
    request: Request,
    user: User | None = Depends(get_optional_current_user),
    db: Session = Depends(get_db),
) -> Response:
    language = _language(request)
    if user is None:
        return RedirectResponse(f"/login?lang={language}", status_code=303)
    return _render_dashboard(request, db)


@app.get("/login", response_class=HTMLResponse)
def login_page(request: Request) -> HTMLResponse:
    language = _language(request)
    return templates.TemplateResponse(
        request=request,
        name="login.html",
        context={
            "language": language,
            "t": _TEXT[language],
            "title": _TEXT[language]["login_title"],
            "error": None,
        },
    )


@app.post("/login", response_class=HTMLResponse)
async def login(request: Request, db: Session = Depends(get_db)) -> Response:
    form = parse_qs((await request.body()).decode("utf-8"), keep_blank_values=True)
    username = form.get("username", [""])[0].strip()
    password = form.get("password", [""])[0]
    user = authenticate(db, username, password)
    language = _language(request)
    if user is None:
        return templates.TemplateResponse(
            request=request,
            name="login.html",
            context={
                "language": language,
                "t": _TEXT[language],
                "title": _TEXT[language]["login_title"],
                "error": _TEXT[language]["invalid_login"],
            },
            status_code=401,
        )
    response = RedirectResponse("/", status_code=303)
    set_login_cookie(response, user.id)
    return response


@app.post("/logout")
def logout() -> Response:
    response = RedirectResponse("/login", status_code=303)
    clear_login_cookie(response)
    return response
