"""Admin tools under /system: monitor history, logs, audit trail, backup/restore, data import/export."""

import logging
import tempfile
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse, StreamingResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.auth import require_role
from app.db import SessionLocal, engine, get_db, migrate
from app.i18n import TRANSLATIONS
from app.jobs.incidents import close_orphan_incidents
from app.jobs.monitor import load_samples, outages, uptime_percent
from app.logs import AREAS, LOG_FILE, log_buffer
from app.models import AuditLog, Camera, Embedding, Incident, Person, User
from app.runtime import start_workers, stop_workers, workers_lock
from app.services import backup as backup_service
from app.services.data_io import csv_stream, import_people
from app.web.routes.events import _display_time, _local_zone

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/system", dependencies=[Depends(require_role("admin"))])
templates = Jinja2Templates(directory=Path(__file__).resolve().parents[1] / "templates")
templates.env.filters["local_time"] = _display_time

LEVELS = {"DEBUG": logging.DEBUG, "INFO": logging.INFO, "WARNING": logging.WARNING, "ERROR": logging.ERROR}
PERIODS = {"24h": timedelta(hours=24), "7d": timedelta(days=7)}
AUDIT_PAGE = 100
SPARK_POINTS = 120


def _lang(request: Request) -> str:
    return "bn" if request.query_params.get("lang") == "bn" else "en"


def _page(request: Request, name: str, tab: str, **context) -> HTMLResponse:
    language = _lang(request)
    t = TRANSLATIONS[language]
    return templates.TemplateResponse(
        request=request,
        name=name,
        context={"language": language, "t": t, "title": t["health_title"], "tab": tab, **context},
    )


# ---------------------------------------------------------------- monitor


def sparkline(values: list[float | None], width: int = 240, height: int = 36) -> str:
    """SVG polyline points for a small trend chart; gaps (None) are drawn at zero."""
    if not values:
        return ""
    if len(values) > SPARK_POINTS:
        step = len(values) / SPARK_POINTS
        values = [values[int(index * step)] for index in range(SPARK_POINTS)]
    numbers = [value or 0.0 for value in values]
    top = max(numbers) or 1.0
    count = max(1, len(numbers) - 1)
    return " ".join(
        f"{index * width / count:.1f},{height - 2 - (value / top) * (height - 4):.1f}"
        for index, value in enumerate(numbers)
    )


@router.get("/monitor", response_class=HTMLResponse)
def monitor_page(request: Request, period: str = "24h", db: Session = Depends(get_db)) -> HTMLResponse:
    period = period if period in PERIODS else "24h"
    since = datetime.now(UTC) - PERIODS[period]
    samples = load_samples(db, since)
    cameras = list(db.scalars(select(Camera).order_by(Camera.id)))
    names = {camera.id: camera.name for camera in cameras}
    found = outages(samples)
    rows = []
    for camera in cameras:
        own = [sample for sample in samples if sample.camera_id == camera.id]
        camera_outages = [outage for outage in found if outage.camera_id == camera.id]
        rows.append(
            {
                "camera": camera,
                "uptime": uptime_percent(own),
                "outages": len(camera_outages),
                "outage_minutes": sum(outage.minutes for outage in camera_outages),
                "fps_line": sparkline([sample.fps for sample in own]),
                "face_line": sparkline([sample.face_fps for sample in own]),
                "samples": len(own),
            }
        )
    system = [sample for sample in samples if sample.camera_id is None]
    cpu_values = [sample.cpu_percent for sample in system if sample.cpu_percent is not None]
    return _page(
        request,
        "system_monitor.html",
        "monitor",
        period=period,
        rows=rows,
        outages=found[:200],
        names=names,
        cpu_line=sparkline([sample.cpu_percent for sample in system]),
        cpu_avg=sum(cpu_values) / len(cpu_values) if cpu_values else None,
        cpu_max=max(cpu_values) if cpu_values else None,
        has_samples=bool(samples),
    )


# ---------------------------------------------------------------- logs


def _log_query(request: Request) -> dict:
    level = request.query_params.get("level", "INFO").upper()
    area = request.query_params.get("area") or None
    return {
        "level": level if level in LEVELS else "INFO",
        "area": area if area in set(AREAS.values()) | {"app"} else None,
        "q": (request.query_params.get("q") or "").strip()[:200],
    }


def _log_records(query: dict) -> list:
    records = log_buffer.records(
        min_level=LEVELS[query["level"]], area=query["area"], search=query["q"] or None, limit=500
    )
    return list(reversed(records))


@router.get("/logs", response_class=HTMLResponse)
def logs_page(request: Request) -> HTMLResponse:
    query = _log_query(request)
    return _page(
        request,
        "system_logs.html",
        "logs",
        query=query,
        records=_log_records(query),
        areas=sorted(set(AREAS.values()) | {"app"}),
        levels=list(LEVELS),
        fragment=False,
        log_file_exists=LOG_FILE.is_file(),
    )


@router.get("/logs/fragment", response_class=HTMLResponse)
def logs_fragment(request: Request) -> HTMLResponse:
    query = _log_query(request)
    return _page(request, "system_logs.html", "logs", query=query, records=_log_records(query), fragment=True)


@router.get("/logs/download")
def logs_download() -> FileResponse:
    if not LOG_FILE.is_file():
        raise HTTPException(status_code=404, detail="Log file not found")
    stamp = datetime.now(UTC).strftime("%Y%m%d-%H%M")
    return FileResponse(LOG_FILE, media_type="text/plain; charset=utf-8", filename=f"gate-face-{stamp}.log")


# ---------------------------------------------------------------- audit


def _audit_filters(request: Request) -> tuple[list, dict]:
    action = (request.query_params.get("action") or "").strip()[:100]
    user = (request.query_params.get("user") or "").strip()[:100]
    start = request.query_params.get("start") or ""
    end = request.query_params.get("end") or ""
    conditions = []
    if action:
        conditions.append(AuditLog.action.contains(action))
    if user:
        conditions.append(User.username.contains(user))
    zone = _local_zone()
    try:
        if start:
            local = datetime.fromisoformat(start).replace(tzinfo=zone)
            conditions.append(AuditLog.at >= local.astimezone(UTC).replace(tzinfo=None))
        if end:
            local = (datetime.fromisoformat(end) + timedelta(days=1)).replace(tzinfo=zone)
            conditions.append(AuditLog.at < local.astimezone(UTC).replace(tzinfo=None))
    except ValueError as exc:
        raise HTTPException(status_code=422, detail="Invalid date") from exc
    return conditions, {"action": action, "user": user, "start": start, "end": end}


def _audit_select(conditions: list):
    return select(AuditLog, User).outerjoin(User, User.id == AuditLog.user_id).where(*conditions)


@router.get("/audit", response_class=HTMLResponse)
def audit_page(request: Request, page: int = 1, db: Session = Depends(get_db)) -> HTMLResponse:
    conditions, filters = _audit_filters(request)
    page = max(1, page)
    total = db.scalar(
        select(func.count(AuditLog.id)).select_from(AuditLog).outerjoin(User, User.id == AuditLog.user_id).where(*conditions)
    ) or 0
    rows = db.execute(
        _audit_select(conditions).order_by(AuditLog.at.desc()).offset((page - 1) * AUDIT_PAGE).limit(AUDIT_PAGE)
    ).all()
    actions = [row[0] for row in db.execute(select(AuditLog.action).distinct().order_by(AuditLog.action))]
    return _page(
        request,
        "system_audit.html",
        "audit",
        rows=rows,
        filters=filters,
        page=page,
        pages=max(1, -(-total // AUDIT_PAGE)),
        total=total,
        actions=actions,
    )


@router.get("/audit/export.csv")
def audit_export(request: Request, db: Session = Depends(get_db)) -> StreamingResponse:
    conditions, _ = _audit_filters(request)
    rows = db.execute(_audit_select(conditions).order_by(AuditLog.at.desc())).all()
    body = csv_stream(
        ["time", "user", "action", "detail"],
        ([_display_time(log.at), user.username if user else "", log.action, log.detail] for log, user in rows),
    )
    return _csv_response(body, "audit-log.csv")


def _csv_response(body: Iterator[bytes], filename: str) -> StreamingResponse:
    return StreamingResponse(
        body,
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


# ---------------------------------------------------------------- backup


@router.get("/backup", response_class=HTMLResponse)
def backup_page(request: Request) -> HTMLResponse:
    language = _lang(request)
    t = TRANSLATIONS[language]
    message = request.query_params.get("msg")
    error = request.query_params.get("error")
    return _page(
        request,
        "system_backup.html",
        "backup",
        backups=backup_service.list_backups(),
        backup_dir=str(backup_service.backup_dir()),
        message=t.get(message) if message else None,
        error=t.get(error) if error else None,
        summary=None,
    )


@router.post("/backup/create")
def backup_create(request: Request, user: User = Depends(require_role("admin")), db: Session = Depends(get_db)):
    language = _lang(request)
    try:
        archive = backup_service.create_backup()
    except Exception:
        logger.exception("Manual backup failed.")
        return RedirectResponse(f"/system/backup?lang={language}&error=backup_failed", status_code=303)
    db.add(AuditLog(user_id=user.id, action="backup.create", detail=archive.name))
    db.commit()
    return RedirectResponse(f"/system/backup?lang={language}&msg=backup_done", status_code=303)


@router.get("/backup/file/{name}")
def backup_download(name: str) -> FileResponse:
    path = backup_service.backup_by_name(name)
    if path is None:
        raise HTTPException(status_code=404, detail="Backup not found")
    return FileResponse(path, media_type="application/zip", filename=name)


def _save_upload(upload: UploadFile, target: Path, limit: int) -> None:
    size = 0
    with target.open("wb") as output:
        while chunk := upload.file.read(1024 * 1024):
            size += len(chunk)
            if size > limit:
                raise backup_service.BackupError("backup_too_large")
            output.write(chunk)


@router.post("/backup/restore", response_class=HTMLResponse)
def backup_restore(
    request: Request,
    archive: UploadFile = File(...),
    confirm: str = Form(""),
    user: User = Depends(require_role("admin")),
) -> HTMLResponse:
    language = _lang(request)
    t = TRANSLATIONS[language]
    if confirm.strip().upper() != "RESTORE":
        return _restore_error(request, t["backup_confirm_needed"])
    with tempfile.TemporaryDirectory(prefix="gate-face-restore-") as temp:
        work = Path(temp)
        uploaded = work / "upload.zip"
        try:
            _save_upload(archive, uploaded, backup_service.MAX_UPLOAD_BYTES)
            extracted = work / "extracted"
            extracted.mkdir()
            db_copy, summary = backup_service.inspect_backup(uploaded, extracted)
        except backup_service.BackupError as exc:
            logger.warning("Restore rejected: %s", exc)
            return _restore_error(request, t.get(exc.code, exc.code))

        safety = backup_service.create_backup(label="before-restore", keep=1000)
        logger.warning("Restoring backup %s (uploaded by %s); current data saved to %s.", archive.filename, user.username, safety.name)
        with workers_lock:
            stop_workers()
            try:
                backup_service.restore_database(db_copy, extracted)
                engine.dispose()
                migrate(engine)
            finally:
                with SessionLocal() as db:
                    close_orphan_incidents(db)
                    db.add(
                        AuditLog(
                            user_id=None,
                            action="backup.restore",
                            detail=f"file={archive.filename} by={user.username} safety={safety.name}",
                        )
                    )
                    db.commit()
                    start_workers(db)
    return _page(
        request,
        "system_backup.html",
        "backup",
        backups=backup_service.list_backups(),
        backup_dir=str(backup_service.backup_dir()),
        message=t["backup_restored"].format(name=safety.name),
        error=None,
        summary=summary,
    )


def _restore_error(request: Request, message: str) -> HTMLResponse:
    return _page(
        request,
        "system_backup.html",
        "backup",
        backups=backup_service.list_backups(),
        backup_dir=str(backup_service.backup_dir()),
        message=None,
        error=message,
        summary=None,
    )


# ---------------------------------------------------------------- data


@router.get("/data", response_class=HTMLResponse)
def data_page(request: Request) -> HTMLResponse:
    return _page(request, "system_data.html", "data", result=None, error=None)


@router.get("/data/people.csv")
def people_export(db: Session = Depends(get_db)) -> StreamingResponse:
    counts = dict(db.execute(select(Embedding.person_id, func.count()).group_by(Embedding.person_id)).all())
    people = db.scalars(select(Person).order_by(Person.code)).all()
    body = csv_stream(
        ["code", "name", "active", "created_at", "face_samples"],
        (
            [person.code, person.name, 1 if person.active else 0, _display_time(person.created_at), counts.get(person.id, 0)]
            for person in people
        ),
    )
    return _csv_response(body, "people.csv")


@router.get("/data/incidents.csv")
def incidents_export(request: Request, db: Session = Depends(get_db)) -> StreamingResponse:
    days = min(max(int(request.query_params.get("days", "30") or 30), 1), 3650)
    since = (datetime.now(UTC) - timedelta(days=days)).replace(tzinfo=None)
    rows = db.execute(
        select(Incident, Camera)
        .join(Camera, Camera.id == Incident.camera_id)
        .where(Incident.started_at >= since)
        .order_by(Incident.started_at.desc())
    ).all()
    body = csv_stream(
        ["started", "ended", "camera", "kind", "severity", "people", "vehicles", "reviewed", "note"],
        (
            [
                _display_time(incident.started_at),
                _display_time(incident.ended_at) if incident.ended_at else "",
                camera.name,
                incident.kind,
                incident.severity,
                incident.person_count,
                incident.vehicle_count,
                "yes" if incident.reviewed_at else "no",
                incident.review_note,
            ]
            for incident, camera in rows
        ),
    )
    return _csv_response(body, f"surveillance-{days}d.csv")


@router.get("/data/people-template.csv")
def people_template() -> StreamingResponse:
    return _csv_response(csv_stream(["code", "name", "active"], [["E001", "Example Name", 1]]), "people-template.csv")


@router.post("/data/people-import", response_class=HTMLResponse)
def people_import(
    request: Request,
    file: UploadFile = File(...),
    user: User = Depends(require_role("admin")),
    db: Session = Depends(get_db),
) -> HTMLResponse:
    t = TRANSLATIONS[_lang(request)]
    raw = file.file.read(5 * 1024 * 1024 + 1)
    if len(raw) > 5 * 1024 * 1024:
        return _page(request, "system_data.html", "data", result=None, error=t["import_too_many"])
    try:
        result = import_people(db, raw, user.id)
    except ValueError as exc:
        db.rollback()
        return _page(request, "system_data.html", "data", result=None, error=t.get(str(exc), str(exc)))
    return _page(request, "system_data.html", "data", result=result, error=None)
