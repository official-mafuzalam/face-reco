import json
from pathlib import Path

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.auth import require_role
from app.db import get_db
from app.i18n import TRANSLATIONS
from app.models import AuditLog, Camera, SurveillanceRule, User
from app.surveillance.rules import parse_hhmm


router = APIRouter(prefix="/cameras", dependencies=[Depends(require_role("admin"))])
templates = Jinja2Templates(directory=Path(__file__).resolve().parents[1] / "templates")
RULE_KINDS = (
    "intrusion", "line", "loiter", "abandoned", "removed", "running", "fall",
    "authorized_zone", "drawer", "after_hours", "tailgating", "count_limit", "tamper",
)
# Kinds whose watched classes are fixed rather than chosen in the form.
FIXED_CLASSES = {
    "abandoned": ["bag"],
    "removed": [],
    "running": ["person"],
    "fall": ["person"],
    "authorized_zone": ["person"],
    "drawer": ["person"],
    "after_hours": ["person"],
    "tailgating": ["person"],
    "count_limit": ["person"],
    "tamper": [],
}
CLASSES = ("person", "vehicle")
SEVERITIES = ("info", "warning", "critical")
DIRECTIONS = ("any", "ab", "ba")

SITE_TEMPLATES = {
    "factory_gate": {
        "name_key": "template_factory_gate",
        "rules": [
            {"name": "Gate Line Cross", "kind": "line", "direction": "ab", "classes": "person", "points": [[0.5, 0.2], [0.5, 0.8]], "severity": "info"},
            {"name": "Tailgating Alert", "kind": "tailgating", "direction": "ab", "classes": "person", "points": [[0.5, 0.2], [0.5, 0.8]], "severity": "warning"},
            {"name": "After Hours Gate Intrusion", "kind": "after_hours", "schedule_start": "20:00", "schedule_end": "06:00", "classes": "person", "points": [[0.2, 0.2], [0.8, 0.2], [0.8, 0.8], [0.2, 0.8]], "severity": "critical"},
            {"name": "Camera Tamper Guard", "kind": "tamper", "classes": "", "points": [[0.0, 0.0], [1.0, 0.0], [1.0, 1.0], [0.0, 1.0]], "severity": "critical"},
        ],
    },
    "finance_room": {
        "name_key": "template_finance_room",
        "rules": [
            {"name": "Unauthorized Access", "kind": "authorized_zone", "classes": "person", "points": [[0.1, 0.1], [0.9, 0.1], [0.9, 0.9], [0.1, 0.9]], "severity": "critical"},
            {"name": "Max People Limit", "kind": "count_limit", "classes": "person", "points": [[0.1, 0.1], [0.9, 0.1], [0.9, 0.9], [0.1, 0.9]], "severity": "warning"},
            {"name": "Camera Tamper Guard", "kind": "tamper", "classes": "", "points": [[0.0, 0.0], [1.0, 0.0], [1.0, 1.0], [0.0, 1.0]], "severity": "critical"},
        ],
    },
    "cash_drawer": {
        "name_key": "template_cash_drawer",
        "rules": [
            {"name": "Cash Drawer Activity", "kind": "drawer", "classes": "person", "points": [[0.3, 0.4], [0.7, 0.4], [0.7, 0.8], [0.3, 0.8]], "severity": "critical"},
            {"name": "Counter Loitering", "kind": "loiter", "loiter_seconds": 90, "classes": "person", "points": [[0.1, 0.2], [0.9, 0.2], [0.9, 0.9], [0.1, 0.9]], "severity": "warning"},
            {"name": "Camera Tamper Guard", "kind": "tamper", "classes": "", "points": [[0.0, 0.0], [1.0, 0.0], [1.0, 1.0], [0.0, 1.0]], "severity": "critical"},
        ],
    },
    "store_entrance": {
        "name_key": "template_store_entrance",
        "rules": [
            {"name": "After Hours Intrusion", "kind": "after_hours", "schedule_start": "22:00", "schedule_end": "07:00", "classes": "person", "points": [[0.1, 0.1], [0.9, 0.1], [0.9, 0.9], [0.1, 0.9]], "severity": "critical"},
            {"name": "Night Loitering", "kind": "loiter", "loiter_seconds": 60, "schedule_start": "22:00", "schedule_end": "06:00", "classes": "person", "points": [[0.1, 0.1], [0.9, 0.1], [0.9, 0.9], [0.1, 0.9]], "severity": "warning"},
            {"name": "Camera Tamper Guard", "kind": "tamper", "classes": "", "points": [[0.0, 0.0], [1.0, 0.0], [1.0, 1.0], [0.0, 1.0]], "severity": "critical"},
        ],
    },
}


def _lang(request: Request) -> str:
    return "bn" if request.query_params.get("lang") == "bn" else "en"


def _camera_or_404(db: Session, camera_id: int) -> Camera:
    camera = db.get(Camera, camera_id)
    if camera is not None and camera.deleted_at is not None:
        camera = None
    if camera is None:
        raise HTTPException(status_code=404, detail="Camera not found")
    return camera


def _render(
    request: Request,
    db: Session,
    camera: Camera,
    *,
    error: str | None = None,
    form: dict[str, object] | None = None,
    status_code: int = 200,
) -> HTMLResponse:
    language = _lang(request)
    t = TRANSLATIONS[language]
    rules = list(
        db.scalars(select(SurveillanceRule).where(SurveillanceRule.camera_id == camera.id).order_by(SurveillanceRule.id))
    )
    return templates.TemplateResponse(
        request=request,
        name="rules.html",
        context={
            "language": language,
            "t": t,
            "title": f"{t['rules_title']}: {camera.name}",
            "camera": camera,
            "rules": rules,
            "rules_json": json.dumps(
                [{"name": r.name, "kind": r.kind, "points": json.loads(r.points), "enabled": r.enabled} for r in rules]
            ),
            "kinds": RULE_KINDS,
            "classes": CLASSES,
            "severities": SEVERITIES,
            "directions": DIRECTIONS,
            "site_templates": SITE_TEMPLATES,
            "error": t[error] if error else None,
            "form": form or {"kind": "intrusion", "classes": ["person"], "severity": "warning",
                             "direction": "any", "loiter_seconds": 60},
        },
        status_code=status_code,
    )


def _parse_points(raw: str, kind: str) -> list[list[float]] | None:
    if kind == "tamper":
        if not raw or not raw.strip():
            return [[0.0, 0.0], [1.0, 0.0], [1.0, 1.0], [0.0, 1.0]]
    try:
        points = json.loads(raw)
        cleaned = [[round(min(1.0, max(0.0, float(x))), 4), round(min(1.0, max(0.0, float(y))), 4)] for x, y in points]
    except (ValueError, TypeError):
        return [[0.0, 0.0], [1.0, 0.0], [1.0, 1.0], [0.0, 1.0]] if kind == "tamper" else None
    if kind == "tamper":
        return cleaned if cleaned else [[0.0, 0.0], [1.0, 0.0], [1.0, 1.0], [0.0, 1.0]]
    if kind in ("line", "tailgating"):
        return cleaned if len(cleaned) == 2 and cleaned[0] != cleaned[1] else None
    return cleaned if 3 <= len(cleaned) <= 50 else None


@router.get("/{camera_id}/rules", response_class=HTMLResponse)
def rules_page(camera_id: int, request: Request, db: Session = Depends(get_db)) -> HTMLResponse:
    return _render(request, db, _camera_or_404(db, camera_id))


@router.post("/{camera_id}/rules/new", response_class=HTMLResponse)
def add_rule(
    camera_id: int,
    request: Request,
    name: str = Form(""),
    kind: str = Form(...),
    points: str = Form(""),
    classes: list[str] = Form([]),
    direction: str = Form("any"),
    loiter_seconds: int = Form(60),
    schedule_start: str = Form(""),
    schedule_end: str = Form(""),
    severity: str = Form("warning"),
    user: User = Depends(require_role("admin")),
    db: Session = Depends(get_db),
) -> Response:
    camera = _camera_or_404(db, camera_id)
    name = name.strip()[:100]
    form = {"name": name, "kind": kind, "classes": classes, "direction": direction, "points": points,
            "loiter_seconds": loiter_seconds, "schedule_start": schedule_start,
            "schedule_end": schedule_end, "severity": severity}
    if kind in FIXED_CLASSES:
        classes = FIXED_CLASSES[kind]
    parsed = _parse_points(points, kind) if kind in RULE_KINDS else None
    error = None
    if not name:
        error = "rule_name_required"
    elif kind not in RULE_KINDS or severity not in SEVERITIES or direction not in DIRECTIONS:
        error = "rule_invalid"
    elif kind not in FIXED_CLASSES and (not classes or any(c not in CLASSES for c in classes)):
        error = "rule_classes_required"
    elif parsed is None:
        error = "rule_points_line" if kind == "line" else "rule_points_zone"
    elif not 5 <= loiter_seconds <= 3600:
        error = "rule_loiter_range"
    elif (schedule_start or schedule_end) and (parse_hhmm(schedule_start) is None or parse_hhmm(schedule_end) is None):
        error = "rule_schedule_invalid"
    if error:
        return _render(request, db, camera, error=error, form=form, status_code=400)
    rule = SurveillanceRule(
        camera_id=camera.id, name=name, kind=kind, points=json.dumps(parsed), classes=",".join(classes),
        direction=direction if kind == "line" else "any", loiter_seconds=loiter_seconds,
        schedule_start=schedule_start, schedule_end=schedule_end, severity=severity, enabled=True,
    )
    db.add(rule)
    db.flush()
    db.add(AuditLog(user_id=user.id, action="rule.create", detail=f"rule_id={rule.id} camera_id={camera.id}"))
    db.commit()
    return RedirectResponse(f"/cameras/{camera.id}/rules?lang={_lang(request)}", status_code=303)


def _rule_or_404(db: Session, camera_id: int, rule_id: int) -> SurveillanceRule:
    rule = db.get(SurveillanceRule, rule_id)
    if rule is None or rule.camera_id != camera_id:
        raise HTTPException(status_code=404, detail="Rule not found")
    return rule


@router.post("/{camera_id}/rules/{rule_id}/toggle")
def toggle_rule(
    camera_id: int, rule_id: int, request: Request,
    user: User = Depends(require_role("admin")), db: Session = Depends(get_db),
) -> RedirectResponse:
    rule = _rule_or_404(db, camera_id, rule_id)
    rule.enabled = not rule.enabled
    db.add(AuditLog(user_id=user.id, action="rule.enable" if rule.enabled else "rule.disable", detail=f"rule_id={rule.id}"))
    db.commit()
    return RedirectResponse(f"/cameras/{camera_id}/rules?lang={_lang(request)}", status_code=303)


@router.post("/{camera_id}/rules/{rule_id}/delete")
def delete_rule(
    camera_id: int, rule_id: int, request: Request,
    user: User = Depends(require_role("admin")), db: Session = Depends(get_db),
) -> RedirectResponse:
    rule = _rule_or_404(db, camera_id, rule_id)
    db.delete(rule)
    db.add(AuditLog(user_id=user.id, action="rule.delete", detail=f"rule_id={rule_id} camera_id={camera_id}"))
    db.commit()
    return RedirectResponse(f"/cameras/{camera_id}/rules?lang={_lang(request)}", status_code=303)


@router.post("/{camera_id}/rules/template")
def apply_site_template(
    camera_id: int,
    request: Request,
    template_key: str = Form(...),
    user: User = Depends(require_role("admin")),
    db: Session = Depends(get_db),
) -> RedirectResponse:
    camera = _camera_or_404(db, camera_id)
    template = SITE_TEMPLATES.get(template_key)
    if template is not None:
        for r_spec in template["rules"]:
            rule = SurveillanceRule(
                camera_id=camera.id,
                name=r_spec["name"],
                kind=r_spec["kind"],
                points=json.dumps(r_spec["points"]),
                classes=r_spec.get("classes", "person"),
                direction=r_spec.get("direction", "any"),
                loiter_seconds=r_spec.get("loiter_seconds", 60),
                schedule_start=r_spec.get("schedule_start", ""),
                schedule_end=r_spec.get("schedule_end", ""),
                severity=r_spec.get("severity", "warning"),
                enabled=True,
            )
            db.add(rule)
        db.add(AuditLog(user_id=user.id, action="rule.apply_template", detail=f"camera_id={camera.id} template={template_key}"))
        db.commit()
    return RedirectResponse(f"/cameras/{camera.id}/rules?lang={_lang(request)}", status_code=303)
