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
CHALLENGE_COOKIE = "gate_2fa_challenge"
CHALLENGE_MAX_AGE = 5 * 60
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


def _encode_session(user_id: int, token_version: int = 1) -> str:
    now = int(time.time())
    payload = json.dumps(
        {
            "user_id": user_id,
            "token_version": token_version,
            "last_active": now,
            "expires": now + SESSION_MAX_AGE,
        },
        separators=(",", ":"),
    ).encode()
    return _sign(payload)


def _decode_session(value: str) -> dict | None:
    try:
        encoded_payload, encoded_signature = value.split(".", 1)
        payload = base64.urlsafe_b64decode(encoded_payload + "=" * (-len(encoded_payload) % 4))
        signature = base64.urlsafe_b64decode(encoded_signature + "=" * (-len(encoded_signature) % 4))
        expected = hmac.new(settings.secret_key.encode(), payload, hashlib.sha256).digest()
        if not hmac.compare_digest(signature, expected):
            return None
        data = json.loads(payload)
        now = time.time()
        if data.get("expires", 0) < now:
            return None
        idle_limit = settings.session_idle_timeout_minutes * 60
        last_active = data.get("last_active", data.get("expires", 0) - SESSION_MAX_AGE)
        if now - last_active > idle_limit:
            return None
        return {
            "user_id": int(data["user_id"]),
            "token_version": int(data.get("token_version", 1)),
            "last_active": last_active,
        }
    except (ValueError, KeyError, TypeError, json.JSONDecodeError):
        return None


def encode_2fa_challenge(user_id: int) -> str:
    now = int(time.time())
    payload = json.dumps(
        {
            "user_id": user_id,
            "type": "2fa_challenge",
            "expires": now + CHALLENGE_MAX_AGE,
        },
        separators=(",", ":"),
    ).encode()
    return _sign(payload)


def decode_2fa_challenge(value: str) -> int | None:
    try:
        encoded_payload, encoded_signature = value.split(".", 1)
        payload = base64.urlsafe_b64decode(encoded_payload + "=" * (-len(encoded_payload) % 4))
        signature = base64.urlsafe_b64decode(encoded_signature + "=" * (-len(encoded_signature) % 4))
        expected = hmac.new(settings.secret_key.encode(), payload, hashlib.sha256).digest()
        if not hmac.compare_digest(signature, expected):
            return None
        data = json.loads(payload)
        if data.get("type") != "2fa_challenge" or data.get("expires", 0) < time.time():
            return None
        return int(data["user_id"])
    except (ValueError, KeyError, TypeError, json.JSONDecodeError):
        return None


# Re-issue the cookie at most this often, so an open page keeps the session alive
# without a Set-Cookie header on every request.
SESSION_REFRESH_SECONDS = 120


def session_cookie_header(user_id: int, token_version: int) -> tuple[bytes, bytes]:
    """The Set-Cookie header of a fresh session, built the same way as set_login_cookie."""
    carrier = Response()
    set_login_cookie(carrier, user_id, token_version)
    return next(header for header in carrier.raw_headers if header[0] == b"set-cookie")


def set_login_cookie(response: Response, user_id: int, token_version: int = 1) -> None:
    response.set_cookie(
        SESSION_COOKIE,
        _encode_session(user_id, token_version),
        max_age=SESSION_MAX_AGE,
        httponly=True,
        samesite="lax",
        secure=settings.cookie_secure,
        path="/",
    )


def clear_login_cookie(response: Response) -> None:
    response.delete_cookie(SESSION_COOKIE, path="/", httponly=True, samesite="lax")


def set_2fa_challenge_cookie(response: Response, user_id: int) -> None:
    response.set_cookie(
        CHALLENGE_COOKIE,
        encode_2fa_challenge(user_id),
        max_age=CHALLENGE_MAX_AGE,
        httponly=True,
        samesite="lax",
        secure=settings.cookie_secure,
        path="/",
    )


def clear_2fa_challenge_cookie(response: Response) -> None:
    response.delete_cookie(CHALLENGE_COOKIE, path="/", httponly=True, samesite="lax")


def revoke_all_sessions(db: Session, user: User) -> None:
    user.token_version += 1
    db.commit()


def get_optional_current_user(
    request: Request,
    db: Session = Depends(get_db),
) -> User | None:
    token = request.cookies.get(SESSION_COOKIE)
    session_data = _decode_session(token) if token else None
    if session_data is None:
        user = None
    else:
        user = db.get(User, session_data["user_id"])
        if user is None or not user.active or user.token_version != session_data["token_version"]:
            user = None
        elif time.time() - session_data["last_active"] > SESSION_REFRESH_SECONDS:
            # SecurityMiddleware adds the header: routes return their own Response objects.
            request.state.refresh_session = (user.id, user.token_version)
    request.state.user = user
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
