import base64
import hashlib
import hmac
import json
import time
from collections.abc import Callable

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError
from fastapi import Depends, HTTPException, Request, Response, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import settings
from app.db import get_db
from app.models import User


SESSION_COOKIE = "gate_session"
SESSION_MAX_AGE = 8 * 60 * 60
_password_hasher = PasswordHasher()


def hash_password(password: str) -> str:
    return _password_hasher.hash(password)


def authenticate(db: Session, username: str, password: str) -> User | None:
    user = db.scalar(select(User).where(User.username == username))
    if user is None or not user.active:
        return None
    try:
        if _password_hasher.verify(user.password_hash, password):
            return user
    except (InvalidHashError, VerificationError, VerifyMismatchError):
        return None
    return None


def _sign(payload: bytes) -> str:
    signature = hmac.new(settings.secret_key.encode(), payload, hashlib.sha256).digest()
    return base64.urlsafe_b64encode(payload).decode().rstrip("=") + "." + base64.urlsafe_b64encode(
        signature
    ).decode().rstrip("=")


def _encode_session(user_id: int) -> str:
    payload = json.dumps(
        {"user_id": user_id, "expires": int(time.time()) + SESSION_MAX_AGE},
        separators=(",", ":"),
    ).encode()
    return _sign(payload)


def _decode_session(value: str) -> int | None:
    try:
        encoded_payload, encoded_signature = value.split(".", 1)
        payload = base64.urlsafe_b64decode(encoded_payload + "=" * (-len(encoded_payload) % 4))
        signature = base64.urlsafe_b64decode(encoded_signature + "=" * (-len(encoded_signature) % 4))
        expected = hmac.new(settings.secret_key.encode(), payload, hashlib.sha256).digest()
        if not hmac.compare_digest(signature, expected):
            return None
        data = json.loads(payload)
        if data["expires"] < int(time.time()):
            return None
        return int(data["user_id"])
    except (ValueError, KeyError, TypeError, json.JSONDecodeError):
        return None


def set_login_cookie(response: Response, user_id: int) -> None:
    response.set_cookie(
        SESSION_COOKIE,
        _encode_session(user_id),
        max_age=SESSION_MAX_AGE,
        httponly=True,
        samesite="lax",
        secure=settings.cookie_secure,
        path="/",
    )


def clear_login_cookie(response: Response) -> None:
    response.delete_cookie(SESSION_COOKIE, path="/", httponly=True, samesite="lax")


def get_optional_current_user(
    request: Request,
    db: Session = Depends(get_db),
) -> User | None:
    token = request.cookies.get(SESSION_COOKIE)
    user_id = _decode_session(token) if token else None
    user = db.get(User, user_id) if user_id is not None else None
    if user is None or not user.active:
        return None
    return user


def get_current_user(
    request: Request,
    db: Session = Depends(get_db),
) -> User:
    user = get_optional_current_user(request, db)
    if user is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Authentication required",
        )
    return user


def require_role(role: str) -> Callable:
    def role_guard(user: User = Depends(get_current_user)) -> User:
        if user.role != role:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Insufficient permissions",
            )
        return user

    return role_guard
