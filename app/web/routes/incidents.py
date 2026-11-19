import json
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.auth import get_current_user, require_role
from app.config import settings
from app.db import get_db
from app.i18n import TRANSLATIONS
from app.models import AuditLog, Camera, Incident, User
from app.services.deletion import DeleteRefused, delete_incidents
from app.services.feedback import record_feedback
from app.web.routes.events import _display_time, _local_zone, safe_back


router = APIRouter(prefix="/incidents", dependencies=[Depends(get_current_user)])
templates = Jinja2Templates(directory=Path(__file__).resolve().parents[1] / "templates")
PROJECT_ROOT = Path(__file__).resolve().parents[3]
MEDIA_ROOT = (PROJECT_ROOT / "data" / "incidents").resolve()
KINDS = (
    "person", "vehicle", "intrusion", "line", "loiter", "abandoned", "removed",
    "running", "fall", "authorized_zone", "drawer", "after_hours", "tailgating",
    "count_limit", "tamper",
)
SEVERITIES = ("info", "warning", "critical")
PAGE_SIZE = 40


def _lang(request: Request) -> str:
    return "bn" if request.query_params.get("lang") == "bn" else "en"


def _media_file(relative: str | None) -> Path | None:
    if not relative:
        return None
    path = (PROJECT_ROOT / relative).resolve()
    if MEDIA_ROOT not in path.parents or not path.is_file():
        return None
    return path


def _parse_day(value: str) -> date | None:
    try:
        return date.fromisoformat(value) if value else None
    except ValueError:
        return None


def _utc_naive(day: date) -> datetime:
    local = datetime.combine(day, time.min, tzinfo=_local_zone())
    return local.astimezone(UTC).replace(tzinfo=None)


def _duration(incident: Incident) -> str:
    if incident.ended_at is None:
        return ""
    seconds = int((incident.ended_at - incident.started_at).total_seconds())
    return f"{seconds // 60}:{seconds % 60:02d}"


def _row(incident: Incident) -> dict[str, object]:
    try:
        details = json.loads(incident.details or "{}")
    except ValueError:
        details = {}
    labels = details.get("labels", {})
    return {
        "incident": incident,
        "time_display": _display_time(incident.started_at),
        "duration": _duration(incident),
        "live": incident.ended_at is None,
        "labels": labels,
        "rule": details.get("rule", ""),
        "rule_detail": details.get("detail", ""),
        "has_snapshot": _media_file(incident.snapshot_path) is not None,
        "has_clip": _media_file(incident.clip_path) is not None,
    }


@router.get("", response_class=HTMLResponse)
def list_incidents(
    request: Request,
    camera: str = "",
    kind: str = "",
    start: str = "",
    end: str = "",
    reviewed: str = "all",
    severity: str = "",
    page: int = 1,
    db: Session = Depends(get_db),
) -> HTMLResponse:
    language = _lang(request)
    t = TRANSLATIONS[language]
    statement = select(Incident)
    if camera.isdigit():
        statement = statement.where(Incident.camera_id == int(camera))
    if kind in KINDS:
        statement = statement.where(Incident.kind == kind)
    if severity == "alerts":
        statement = statement.where(Incident.severity.in_(("warning", "critical")))
    elif severity in SEVERITIES:
        statement = statement.where(Incident.severity == severity)
    start_day, end_day = _parse_day(start), _parse_day(end)
    if start_day:
        statement = statement.where(Incident.started_at >= _utc_naive(start_day))
    if end_day:
        statement = statement.where(Incident.started_at < _utc_naive(end_day + timedelta(days=1)))
    if reviewed == "reviewed":
        statement = statement.where(Incident.reviewed_at.is_not(None))
    elif reviewed == "unreviewed":
        statement = statement.where(Incident.reviewed_at.is_(None))
    total = db.scalar(select(func.count()).select_from(statement.subquery())) or 0
    pages = max(1, (total + PAGE_SIZE - 1) // PAGE_SIZE)
    page = min(max(1, page), pages)
    incidents = db.scalars(
        statement.order_by(Incident.started_at.desc(), Incident.id.desc())
        .offset((page - 1) * PAGE_SIZE)
        .limit(PAGE_SIZE)
    ).all()
    cameras = {c.id: c for c in db.scalars(select(Camera).order_by(Camera.name))}
    return templates.TemplateResponse(
        request=request,
        name="incidents.html",
        context={
            "language": language,
            "t": t,
            "title": t["incidents_title"],
            "rows": [_row(incident) for incident in incidents],
            "cameras": cameras,
            "kinds": KINDS,
            "filters": {"camera": camera, "kind": kind, "start": start, "end": end, "reviewed": reviewed,
                        "severity": severity},
            "severities": SEVERITIES,
            "page": page,
            "pages": pages,
            "total": total,
        },
    )


def _incident_or_404(db: Session, incident_id: int) -> Incident:
    incident = db.get(Incident, incident_id)
    if incident is None:
        raise HTTPException(status_code=404, detail="Incident not found")
    return incident


@router.get("/{incident_id}", response_class=HTMLResponse)
def incident_detail(incident_id: int, request: Request, db: Session = Depends(get_db)) -> HTMLResponse:
    language = _lang(request)
    t = TRANSLATIONS[language]
    incident = _incident_or_404(db, incident_id)
    reviewer = db.get(User, incident.reviewed_by) if incident.reviewed_by else None
    return templates.TemplateResponse(
        request=request,
        name="incident_detail.html",
        context={
            "clip_fps": settings.clip_fps,
            "language": language,
            "t": t,
            "title": t["incident_title"],
            "row": _row(incident),
            "camera": db.get(Camera, incident.camera_id),
            "reviewer": reviewer,
            "reviewed_display": _display_time(incident.reviewed_at) if incident.reviewed_at else "",
        },
    )


@router.get("/{incident_id}/snapshot.jpg")
def incident_snapshot(incident_id: int, db: Session = Depends(get_db)) -> FileResponse:
    path = _media_file(_incident_or_404(db, incident_id).snapshot_path)
    if path is None:
        raise HTTPException(status_code=404, detail="Snapshot not found")
    return FileResponse(path, media_type="image/jpeg")


_CLIP_TYPES = {".mp4": "video/mp4", ".webm": "video/webm"}


@router.get("/{incident_id}/clip")
def incident_clip(incident_id: int, db: Session = Depends(get_db)) -> FileResponse:
    incident = _incident_or_404(db, incident_id)
    path = _media_file(incident.clip_path)
    if path is None or incident.ended_at is None or path.suffix not in _CLIP_TYPES:
        raise HTTPException(status_code=404, detail="Clip not available")
    return FileResponse(
        path,
        media_type=_CLIP_TYPES[path.suffix],
        filename=f"incident-{incident.id}{path.suffix}",
        content_disposition_type="inline",
    )


@router.post("/{incident_id}/review")
def review_incident(
    incident_id: int,
    request: Request,
    verdict: str = Form("confirmed"),
    note: str = Form(""),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> RedirectResponse:
    incident = _incident_or_404(db, incident_id)
    try:
        record_feedback(db, incident, user.id, verdict=verdict, note=note)
    except ValueError:
        pass
    return RedirectResponse(f"/incidents/{incident.id}?lang={_lang(request)}", status_code=303)


@router.post("/delete")
def delete_incidents_route(
    request: Request,
    ids: list[int] = Form([]),
    back: str = Form(""),
    user: User = Depends(require_role("admin")),
    db: Session = Depends(get_db),
) -> RedirectResponse:
    language = "bn" if request.query_params.get("lang") == "bn" else "en"
    target = safe_back(back, f"/incidents?lang={language}")
    try:
        delete_incidents(db, ids, user.id)
    except DeleteRefused:
        pass
    return RedirectResponse(target, status_code=303)
