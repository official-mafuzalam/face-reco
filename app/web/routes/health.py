from pathlib import Path

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.auth import require_role
from app.db import get_db
from app.health import camera_health, problems, system_health
from app.i18n import TRANSLATIONS
from app.web.routes.events import _display_time

router = APIRouter(prefix="/system", dependencies=[Depends(require_role("admin"))])
templates = Jinja2Templates(directory=Path(__file__).resolve().parents[1] / "templates")
templates.env.filters["local_time"] = _display_time


def health_problems(db: Session, language: str) -> list[str]:
    t = TRANSLATIONS[language]
    return [t[key].format(**values) for key, values in problems(camera_health(db), system_health())]


def _render(request: Request, db: Session, fragment: bool) -> HTMLResponse:
    language = "bn" if request.query_params.get("lang") == "bn" else "en"
    t = TRANSLATIONS[language]
    cameras = camera_health(db)
    system = system_health()
    return templates.TemplateResponse(
        request=request,
        name="health.html",
        context={
            "language": language,
            "t": t,
            "title": t["health_title"],
            "cameras": cameras,
            "system": system,
            "problems": [t[key].format(**values) for key, values in problems(cameras, system)],
            "fragment": fragment,
        },
    )


@router.get("", response_class=HTMLResponse)
def health_page(request: Request, db: Session = Depends(get_db)) -> HTMLResponse:
    return _render(request, db, fragment=False)


@router.get("/fragment", response_class=HTMLResponse)
def health_fragment(request: Request, db: Session = Depends(get_db)) -> HTMLResponse:
    return _render(request, db, fragment=True)
