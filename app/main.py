import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import parse_qs

from cryptography.fernet import Fernet, InvalidToken
from fastapi import Depends, FastAPI, Request, Response
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from app.auth import (
    authenticate,
    clear_login_cookie,
    get_optional_current_user,
    set_login_cookie,
)
from app.config import ensure_secure_settings, insecure_setting_problems, settings
from app.db import Base, engine, get_db, migrate
from app.security import SecurityMiddleware, login_limiter
from app.db import SessionLocal
from app.i18n import TRANSLATIONS
from app.models import User
from app.models import Camera
from app.web.routes.cameras import router as cameras_router, snapshot_router
from app.web.routes.live import router as live_router
from app.web.routes.health import router as health_router
from app.web.routes.system import router as system_router
from app.web.routes.notify import router as notify_router
from app.web.routes.faces import router as faces_router
from app.web.routes.attendance import router as attendance_router
from app.camera.manager import manager
from app.web.routes.people import router as people_router, stream_router as people_stream_router, thumbnail_router
from app.pipeline.worker import load_active_gallery, pipeline_manager
from app.web.routes.dashboard import _render_dashboard, router as dashboard_router
from app.web.routes.events import router as events_router, snapshot_router as event_snapshot_router
from app.web.routes.alerts import router as alerts_router
from app.web.routes.users import router as users_router
from app.web.routes.incidents import router as incidents_router
from app.web.routes.rules import router as rules_router
from app.surveillance.worker import surveillance_manager
from app.jobs.incidents import close_orphan_incidents
from app.jobs.monitor import start_monitor, stop_monitor
from app.notify.sources import start_camera_watch, stop_camera_watch
from app.logs import setup_logging
from app.runtime import start_workers, stop_workers
from app.jobs.retention import start_daily_retention, stop_daily_retention


logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
    setup_logging()
    ensure_secure_settings(settings)
    for problem in insecure_setting_problems(settings):
        logger.warning("INSECURE SETTING (development only): %s", problem)
    migrate(engine)
    retention_job = None
    monitor_job = None
    camera_watch = None
    try:
        with SessionLocal() as db:
            close_orphan_incidents(db)
            start_workers(db)
        retention_job = start_daily_retention()
        monitor_job = start_monitor()
        camera_watch = start_camera_watch()
        yield
    finally:
        try:
            if camera_watch is not None:
                stop_camera_watch(*camera_watch)
            if monitor_job is not None:
                stop_monitor(*monitor_job)
            if retention_job is not None:
                stop_daily_retention(*retention_job)
        finally:
            stop_workers()


app = FastAPI(
    title="gate-face",
    lifespan=lifespan,
    docs_url=None,
    redoc_url=None,
    openapi_url=None,
)
app.add_middleware(SecurityMiddleware)
app.mount(
    "/static",
    StaticFiles(directory=Path(__file__).resolve().parent / "web" / "static"),
    name="static",
)
templates = Jinja2Templates(directory=Path(__file__).resolve().parent / "web" / "templates")
_fernet = Fernet(settings.fernet_key.encode())
_TEXT = TRANSLATIONS
app.include_router(cameras_router)
app.include_router(snapshot_router)
app.include_router(live_router)
app.include_router(health_router)
app.include_router(system_router)
app.include_router(notify_router)
app.include_router(faces_router)
app.include_router(attendance_router)
app.include_router(dashboard_router)
app.include_router(events_router)
app.include_router(event_snapshot_router)
app.include_router(alerts_router)
app.include_router(people_router)
app.include_router(people_stream_router)
app.include_router(thumbnail_router)
app.include_router(users_router)
app.include_router(incidents_router)
app.include_router(rules_router)


def encrypt_rtsp_url(rtsp_url: str) -> str:
    return _fernet.encrypt(rtsp_url.encode()).decode()


def decrypt_rtsp_url(encrypted_url: str) -> str:
    return _fernet.decrypt(encrypted_url.encode()).decode()


def _language(request: Request) -> str:
    return "bn" if request.query_params.get("lang") == "bn" else "en"


@app.get("/health")
def health(db: Session = Depends(get_db)) -> dict[str, str | int]:
    cameras = db.query(Camera).filter(Camera.deleted_at.is_(None)).all()
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
    return _render_dashboard(request, db, user)


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
    language = _language(request)
    client = request.client.host if request.client else "unknown"
    if login_limiter.blocked(client, username):
        logger.warning("Login blocked by rate limit for client %s.", client)
        return templates.TemplateResponse(
            request=request,
            name="login.html",
            context={
                "language": language,
                "t": _TEXT[language],
                "title": _TEXT[language]["login_title"],
                "error": _TEXT[language]["login_rate_limited"],
            },
            status_code=429,
        )
    user = authenticate(db, username, password)
    if user is None:
        login_limiter.record_failure(client, username)
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
    login_limiter.reset(client, username)
    response = RedirectResponse("/", status_code=303)
    set_login_cookie(response, user.id)
    return response


@app.post("/logout")
def logout() -> Response:
    response = RedirectResponse("/login", status_code=303)
    clear_login_cookie(response)
    return response
