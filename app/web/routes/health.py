from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.auth import require_role
from app.config import settings
from app.db import get_db
from app.health import camera_health, problems, system_health
from app.i18n import TRANSLATIONS
from app.models import AuditLog, Camera, User
from app.services.feedback import get_feedback_summary
from app.web.routes.events import _display_time

router = APIRouter(prefix="/system", dependencies=[Depends(require_role("admin"))])
templates = Jinja2Templates(directory=Path(__file__).resolve().parents[1] / "templates")
templates.env.filters["local_time"] = _display_time


def health_problems(db: Session, language: str) -> list[str]:
    t = TRANSLATIONS[language]
    return [t[key].format(**values) for key, values in problems(camera_health(db), system_health())]


def _tuning_rows(db: Session, language: str) -> list[dict]:
    summary = get_feedback_summary(db)
    suggestions = {rec["camera_id"]: rec for rec in summary["recommendations"]}
    cameras = {c.id: c for c in db.scalars(select(Camera))}
    rows = []
    for camera_id, stats in sorted(summary["by_camera"].items()):
        camera = cameras.get(camera_id)
        if camera is None or camera.deleted_at is not None:
            continue
        rec = suggestions.get(camera_id)
        rows.append({
            "camera_id": camera_id, "name": camera.name, "total": stats["total"],
            "false_alarm": stats["false_alarm"],
            "rate": stats["false_alarm"] / stats["total"] if stats["total"] else 0.0,
            "confidence": camera.object_confidence or settings.object_confidence,
            "overridden": camera.object_confidence is not None,
            "suggested": rec["suggested_confidence"] if rec else None,
            "reason": rec["reason_bn" if language == "bn" else "reason_en"] if rec else "",
        })
    return rows


def _render(request: Request, db: Session, fragment: bool, message: str | None = None) -> HTMLResponse:
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
            "tuning": [] if fragment else _tuning_rows(db, language),
            "ai_model": settings.ollama_model,
            "message": message,
        },
    )


@router.get("", response_class=HTMLResponse)
def health_page(request: Request, db: Session = Depends(get_db)) -> HTMLResponse:
    return _render(request, db, fragment=False)


@router.get("/fragment", response_class=HTMLResponse)
def health_fragment(request: Request, db: Session = Depends(get_db)) -> HTMLResponse:
    return _render(request, db, fragment=True)


def _camera_or_404(db: Session, camera_id: int) -> Camera:
    camera = db.get(Camera, camera_id)
    if camera is None or camera.deleted_at is not None:
        raise HTTPException(status_code=404, detail="Camera not found")
    return camera


@router.post("/tuning/{camera_id}/apply")
def apply_tuning(
    camera_id: int, request: Request,
    user: User = Depends(require_role("admin")), db: Session = Depends(get_db),
) -> RedirectResponse:
    camera = _camera_or_404(db, camera_id)
    # The value is recomputed here, never read from the form, so only a real suggestion can be applied.
    suggestion = next((r for r in get_feedback_summary(db)["recommendations"] if r["camera_id"] == camera_id), None)
    if suggestion is not None:
        camera.object_confidence = suggestion["suggested_confidence"]
        db.add(AuditLog(user_id=user.id, action="camera.tuning_apply",
                        detail=f"camera_id={camera_id} confidence={camera.object_confidence}"))
        db.commit()
    language = "bn" if request.query_params.get("lang") == "bn" else "en"
    return RedirectResponse(f"/system?lang={language}", status_code=303)


@router.post("/tuning/{camera_id}/reset")
def reset_tuning(
    camera_id: int, request: Request,
    user: User = Depends(require_role("admin")), db: Session = Depends(get_db),
) -> RedirectResponse:
    camera = _camera_or_404(db, camera_id)
    camera.object_confidence = None
    db.add(AuditLog(user_id=user.id, action="camera.tuning_reset", detail=f"camera_id={camera_id}"))
    db.commit()
    language = "bn" if request.query_params.get("lang") == "bn" else "en"
    return RedirectResponse(f"/system?lang={language}", status_code=303)
