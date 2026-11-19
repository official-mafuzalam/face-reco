"""The signed-in user's own settings: 2FA and phone push preferences (every role)."""

import io
from pathlib import Path

import pyotp
import segno
from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates
from sqlalchemy import select, update
from sqlalchemy.orm import Session

from app.auth import get_current_user
from app.db import get_db
from app.i18n import TRANSLATIONS
from app.models import AuditLog, PushSubscription, User
from app.notify.push import send_push_notification
from app.security import encrypt_totp_secret
from app.surveillance.rules import parse_hhmm

router = APIRouter(prefix="/account", dependencies=[Depends(get_current_user)])
templates = Jinja2Templates(directory=Path(__file__).resolve().parents[1] / "templates")

PUSH_SEVERITIES = ("warning", "critical")


def _lang(request: Request) -> str:
    return "bn" if request.query_params.get("lang") == "bn" else "en"


def _qr_svg(uri: str) -> str:
    buffer = io.BytesIO()
    segno.make(uri, error="m").save(buffer, kind="svg", xmldecl=False, svgns=True, nl=False, border=2, scale=5)
    return buffer.getvalue().decode()


def _devices(db: Session, user: User) -> list[PushSubscription]:
    return list(db.scalars(select(PushSubscription).where(PushSubscription.user_id == user.id)))


def _render_account(
    request: Request, db: Session, user: User, *, message: str | None = None, error: str | None = None,
    status_code: int = 200,
) -> HTMLResponse:
    language = _lang(request)
    t = TRANSLATIONS[language]
    devices = _devices(db, user)
    first = devices[0] if devices else None
    return templates.TemplateResponse(
        request=request,
        name="account.html",
        context={
            "language": language, "t": t, "title": t["account_title"],
            "has_2fa": bool(user.totp_secret_encrypted),
            "device_count": len(devices),
            "quiet_start": first.quiet_start if first else "22:00",
            "quiet_end": first.quiet_end if first else "06:00",
            "min_severity": first.min_severity if first else "warning",
            "severities": PUSH_SEVERITIES,
            "message": message, "error": error,
        },
        status_code=status_code,
    )


def _render_setup(request: Request, user: User, secret: str, error: str | None, status_code: int = 200) -> HTMLResponse:
    language = _lang(request)
    t = TRANSLATIONS[language]
    uri = pyotp.TOTP(secret).provisioning_uri(name=user.username, issuer_name="Gate Face")
    return templates.TemplateResponse(
        request=request,
        name="users_2fa_setup.html",
        context={
            "language": language, "t": t, "title": t["setup_2fa_title"],
            "secret": secret, "qr_svg": _qr_svg(uri), "error": error,
        },
        status_code=status_code,
    )


@router.get("", response_class=HTMLResponse)
def account_page(request: Request, user: User = Depends(get_current_user), db: Session = Depends(get_db)) -> HTMLResponse:
    return _render_account(request, db, user)


@router.get("/2fa", response_class=HTMLResponse)
def setup_2fa_page(request: Request, user: User = Depends(get_current_user)) -> HTMLResponse:
    return _render_setup(request, user, pyotp.random_base32(), None)


@router.post("/2fa", response_class=HTMLResponse)
def setup_2fa_confirm(
    request: Request,
    secret: str = Form(...),
    code: str = Form(...),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> Response:
    secret, code = secret.strip(), code.strip()
    try:
        valid = pyotp.TOTP(secret).verify(code, valid_window=1)
    except Exception:
        valid = False
    if not valid:
        # A secret that does not even parse is replaced by a fresh one so the QR code still renders.
        try:
            pyotp.TOTP(secret).now()
        except Exception:
            secret = pyotp.random_base32()
        return _render_setup(request, user, secret, TRANSLATIONS[_lang(request)]["invalid_totp_code"], 400)
    target = db.get(User, user.id)
    if target is not None:
        target.totp_secret_encrypted = encrypt_totp_secret(secret)
        db.add(AuditLog(user_id=user.id, action="user.totp_enabled", detail=f"user_id={user.id}"))
        db.commit()
    return RedirectResponse(f"/account?lang={_lang(request)}", status_code=303)


@router.post("/push", response_class=HTMLResponse)
def save_push_settings(
    request: Request,
    quiet_start: str = Form(""),
    quiet_end: str = Form(""),
    min_severity: str = Form("warning"),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> Response:
    t = TRANSLATIONS[_lang(request)]
    quiet_start, quiet_end = quiet_start.strip(), quiet_end.strip()
    times_ok = (not quiet_start and not quiet_end) or (
        parse_hhmm(quiet_start) is not None and parse_hhmm(quiet_end) is not None
    )
    if not times_ok or min_severity not in PUSH_SEVERITIES:
        return _render_account(request, db, user, error=t["push_invalid"], status_code=400)
    # Zero-padded so the plain string comparison in is_in_quiet_hours stays correct ("6:00" > "22:00").
    if quiet_start:
        quiet_start, quiet_end = (f"{parse_hhmm(v).hour:02d}:{parse_hhmm(v).minute:02d}" for v in (quiet_start, quiet_end))
    result = db.execute(
        update(PushSubscription).where(PushSubscription.user_id == user.id)
        .values(quiet_start=quiet_start, quiet_end=quiet_end, min_severity=min_severity)
    )
    db.add(AuditLog(user_id=user.id, action="push.settings", detail=f"user_id={user.id}"))
    db.commit()
    key = "push_saved" if result.rowcount else "push_no_devices_saved"
    return _render_account(request, db, user, message=t[key])


@router.post("/push/test", response_class=HTMLResponse)
def send_test_push(request: Request, user: User = Depends(get_current_user), db: Session = Depends(get_db)) -> Response:
    t = TRANSLATIONS[_lang(request)]
    count = send_push_notification(
        db, "Gate Face", f"{user.username}: পুশ নোটিফিকেশন কাজ করছে।", url="/account", severity="critical",
        user_id=user.id,
    )
    if count:
        return _render_account(request, db, user, message=t["push_test_sent"].format(count=count))
    return _render_account(request, db, user, error=t["push_test_none"], status_code=200)
