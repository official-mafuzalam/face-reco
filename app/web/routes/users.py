from pathlib import Path

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.auth import hash_password, require_role, revoke_all_sessions
from app.db import get_db
from app.i18n import TRANSLATIONS
from app.models import AuditLog, User
from app.services.deletion import DeleteRefused, delete_user


router = APIRouter(prefix="/users", dependencies=[Depends(require_role("admin"))])
templates = Jinja2Templates(directory=Path(__file__).resolve().parents[1] / "templates")

MIN_PASSWORD_LENGTH = 12
ROLES = ("admin", "security")


def _lang(request: Request) -> str:
    return "bn" if request.query_params.get("lang") == "bn" else "en"


def _render(
    request: Request,
    db: Session,
    current: User,
    *,
    error: str | None = None,
    message: str | None = None,
    form_username: str = "",
    form_role: str = "security",
    status_code: int = 200,
) -> HTMLResponse:
    language = _lang(request)
    t = TRANSLATIONS[language]
    return templates.TemplateResponse(
        request=request,
        name="users.html",
        context={
            "language": language,
            "t": t,
            "title": t["users_title"],
            "users": list(db.scalars(select(User).where(User.deleted_at.is_(None)).order_by(User.username))),
            "current_user_id": current.id,
            "roles": ROLES,
            "error": t[error] if error else None,
            "message": t[message] if message else None,
            "form_username": form_username,
            "form_role": form_role,
            "min_password_length": MIN_PASSWORD_LENGTH,
        },
        status_code=status_code,
    )


def _user_or_404(db: Session, user_id: int) -> User:
    user = db.get(User, user_id)
    if user is None:
        raise HTTPException(status_code=404, detail="User not found")
    return user


def _active_admin_count(db: Session) -> int:
    return db.scalar(
        select(func.count(User.id)).where(User.role == "admin", User.active.is_(True))
    ) or 0


def _password_error(password: str, confirmation: str) -> str | None:
    if len(password) < MIN_PASSWORD_LENGTH:
        return "user_password_short"
    if password != confirmation:
        return "user_password_mismatch"
    return None


@router.get("", response_class=HTMLResponse)
def list_users(
    request: Request,
    current: User = Depends(require_role("admin")),
    db: Session = Depends(get_db),
) -> HTMLResponse:
    return _render(request, db, current)


@router.post("/new", response_class=HTMLResponse)
def add_user(
    request: Request,
    username: str = Form(...),
    password: str = Form(...),
    confirm_password: str = Form(...),
    role: str = Form(...),
    current: User = Depends(require_role("admin")),
    db: Session = Depends(get_db),
) -> HTMLResponse:
    username = username.strip()
    form = {"form_username": username, "form_role": role}
    error = None
    if not username or len(username) > 100:
        error = "user_username_required"
    elif role not in ROLES:
        error = "user_role_invalid"
    else:
        error = _password_error(password, confirm_password)
    if error is None and db.scalar(select(User).where(User.username == username)) is not None:
        error = "user_exists"
    if error:
        return _render(request, db, current, error=error, status_code=400, **form)

    user = User(username=username, password_hash=hash_password(password), role=role, active=True)
    db.add(user)
    db.flush()
    db.add(
        AuditLog(
            user_id=current.id,
            action="user.create",
            detail=f"user_id={user.id} role={role}",
        )
    )
    db.commit()
    return RedirectResponse(f"/users?lang={_lang(request)}", status_code=303)


@router.post("/{user_id}/active", response_class=HTMLResponse)
def set_user_active(
    user_id: int,
    request: Request,
    active: str = Form(...),
    current: User = Depends(require_role("admin")),
    db: Session = Depends(get_db),
) -> HTMLResponse:
    user = _user_or_404(db, user_id)
    make_active = active == "true"
    if not make_active:
        if user.id == current.id:
            return _render(request, db, current, error="user_cannot_disable_self", status_code=400)
        if user.role == "admin" and user.active and _active_admin_count(db) <= 1:
            return _render(request, db, current, error="user_last_admin", status_code=400)
    user.active = make_active
    db.add(
        AuditLog(
            user_id=current.id,
            action="user.activate" if make_active else "user.deactivate",
            detail=f"user_id={user.id}",
        )
    )
    db.commit()
    return RedirectResponse(f"/users?lang={_lang(request)}", status_code=303)


@router.post("/{user_id}/delete", response_class=HTMLResponse)
def delete_user_route(
    user_id: int,
    request: Request,
    current: User = Depends(require_role("admin")),
    db: Session = Depends(get_db),
) -> HTMLResponse:
    user = _user_or_404(db, user_id)
    try:
        delete_user(db, user, current)
    except DeleteRefused as exc:
        return _render(request, db, current, error=exc.code, status_code=400)
    return RedirectResponse(f"/users?lang={_lang(request)}", status_code=303)


@router.post("/{user_id}/password", response_class=HTMLResponse)
def reset_password(
    user_id: int,
    request: Request,
    password: str = Form(...),
    confirm_password: str = Form(...),
    current: User = Depends(require_role("admin")),
    db: Session = Depends(get_db),
) -> HTMLResponse:
    user = _user_or_404(db, user_id)
    error = _password_error(password, confirm_password)
    if error:
        return _render(request, db, current, error=error, status_code=400)
    user.password_hash = hash_password(password)
    user.token_version += 1
    db.add(AuditLog(user_id=current.id, action="user.password_reset", detail=f"user_id={user.id}"))
    db.commit()
    return _render(request, db, current, message="user_password_changed")


@router.post("/{user_id}/revoke_sessions", response_class=HTMLResponse)
def revoke_user_sessions(
    user_id: int,
    request: Request,
    current: User = Depends(require_role("admin")),
    db: Session = Depends(get_db),
) -> HTMLResponse:
    user = _user_or_404(db, user_id)
    revoke_all_sessions(db, user)
    db.add(AuditLog(user_id=current.id, action="user.revoke_sessions", detail=f"user_id={user.id}"))
    db.commit()
    return _render(request, db, current, message="user_sessions_revoked")


@router.post("/{user_id}/totp/disable", response_class=HTMLResponse)
def disable_user_totp(
    user_id: int,
    request: Request,
    current: User = Depends(require_role("admin")),
    db: Session = Depends(get_db),
) -> HTMLResponse:
    user = _user_or_404(db, user_id)
    user.totp_secret_encrypted = None
    db.add(AuditLog(user_id=current.id, action="user.totp_disabled", detail=f"user_id={user.id}"))
    db.commit()
    return _render(request, db, current, message="user_totp_disabled")
