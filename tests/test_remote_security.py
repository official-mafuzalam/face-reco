from collections.abc import AsyncIterator, Generator
from contextlib import asynccontextmanager
import time

import pyotp
import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.auth import (
    CHALLENGE_COOKIE,
    SESSION_COOKIE,
    _decode_session,
    _encode_session,
    authenticate,
    decode_2fa_challenge,
    encode_2fa_challenge,
    hash_password,
    revoke_all_sessions,
)
from app.config import settings
from app.db import Base, get_db
from app.main import app
from app.models import User
from app.security import (
    decrypt_totp_secret,
    encrypt_totp_secret,
    get_client_ip,
)

PASSWORD = "test-password-123"


@pytest.fixture
def test_env(monkeypatch: pytest.MonkeyPatch):
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)

    def override_get_db() -> Generator[Session, None, None]:
        with factory() as db:
            yield db

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        yield

    monkeypatch.setattr(app.router, "lifespan_context", lifespan)
    app.dependency_overrides[get_db] = override_get_db
    with factory() as db:
        admin = User(username="admin", password_hash=hash_password(PASSWORD), role="admin", token_version=1)
        guard = User(username="guard", password_hash=hash_password(PASSWORD), role="security", token_version=1)
        db.add_all([admin, guard])
        db.commit()
    with TestClient(app) as client:
        yield client, factory
    app.dependency_overrides.clear()
    engine.dispose()


def test_get_client_ip():
    class DummyClient:
        host = "127.0.0.1"

    req_loopback = Request(
        scope={
            "type": "http",
            "client": ("127.0.0.1", 1234),
            "headers": [(b"cf-connecting-ip", b"203.0.113.195")],
        }
    )
    # Without trust_cloudflare: returns direct host
    assert get_client_ip(req_loopback, trust_cloudflare=False) == "127.0.0.1"
    # With trust_cloudflare: returns CF-Connecting-IP
    assert get_client_ip(req_loopback, trust_cloudflare=True) == "203.0.113.195"

    # From non-loopback LAN host: ignores CF-Connecting-IP even if trust_cloudflare is True
    req_lan = Request(
        scope={
            "type": "http",
            "client": ("192.168.1.100", 1234),
            "headers": [(b"cf-connecting-ip", b"203.0.113.195")],
        }
    )
    assert get_client_ip(req_lan, trust_cloudflare=True) == "192.168.1.100"


def test_totp_secret_encryption():
    secret = pyotp.random_base32()
    encrypted = encrypt_totp_secret(secret)
    assert encrypted != secret
    decrypted = decrypt_totp_secret(encrypted)
    assert decrypted == secret


def test_session_version_and_invalidation(test_env):
    client, factory = test_env
    with factory() as db:
        user = db.query(User).filter(User.username == "admin").one()
        user_id = user.id
        token_v1 = _encode_session(user_id, token_version=1)

    # Valid session token with token_version=1
    decoded = _decode_session(token_v1)
    assert decoded is not None
    assert decoded["user_id"] == user_id
    assert decoded["token_version"] == 1

    # Invalidate by revoking all sessions
    with factory() as db:
        user = db.get(User, user_id)
        revoke_all_sessions(db, user)
        assert user.token_version == 2

    # Now verify get_optional_current_user with old token returns None
    client.cookies.set(SESSION_COOKIE, token_v1)
    response = client.get("/users")
    # Redirects to login because old session token version is 1 but user is at 2
    assert response.status_code in (303, 401)


def test_session_idle_timeout(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(settings, "session_idle_timeout_minutes", 15)
    now = time.time()
    # Active session: within 15 minutes
    valid_token = _encode_session(1, 1)
    assert _decode_session(valid_token) is not None

    # Simulate expired session (idle for 20 minutes)
    expired_token = _encode_session(1, 1)
    monkeypatch.setattr(time, "time", lambda: now + 20 * 60)
    assert _decode_session(expired_token) is None


def test_2fa_challenge_tokens():
    token = encode_2fa_challenge(42)
    user_id = decode_2fa_challenge(token)
    assert user_id == 42


def test_login_flow_with_and_without_2fa(test_env):
    client, factory = test_env

    # 1. User without 2FA: logs in directly
    res = client.post("/login", data={"username": "guard", "password": PASSWORD}, follow_redirects=False)
    assert res.status_code == 303
    assert res.headers["location"] == "/"
    assert SESSION_COOKIE in client.cookies

    # 2. Enable 2FA for admin
    totp_secret = pyotp.random_base32()
    totp = pyotp.TOTP(totp_secret)
    with factory() as db:
        admin = db.query(User).filter(User.username == "admin").one()
        admin.totp_secret_encrypted = encrypt_totp_secret(totp_secret)
        db.commit()

    # Clear cookies
    client.cookies.clear()

    # 3. Try login for admin -> redirects to /login/2fa with challenge cookie
    res2 = client.post("/login", data={"username": "admin", "password": PASSWORD}, follow_redirects=False)
    assert res2.status_code == 303
    assert "/login/2fa" in res2.headers["location"]
    assert CHALLENGE_COOKIE in client.cookies
    assert SESSION_COOKIE not in client.cookies

    # 4. View 2FA login page
    page_res = client.get("/login/2fa")
    assert page_res.status_code == 200

    # 5. Invalid TOTP code
    bad_code_res = client.post("/login/2fa", data={"code": "000000"})
    assert bad_code_res.status_code == 401

    # 6. Valid TOTP code
    valid_code = totp.now()
    good_res = client.post("/login/2fa", data={"code": valid_code}, follow_redirects=False)
    assert good_res.status_code == 303
    assert good_res.headers["location"] == "/"
    assert SESSION_COOKIE in client.cookies


def test_user_routes_2fa_management(test_env):
    client, factory = test_env

    # Login as admin directly before 2FA is set
    client.post("/login", data={"username": "admin", "password": PASSWORD}, follow_redirects=False)

    # Access 2FA setup page
    setup_page = client.get("/account/2fa")
    assert setup_page.status_code == 200

    # Try bad code on setup
    bad_setup = client.post("/account/2fa", data={"secret": "JBSWY3DPEHPK3PXP", "code": "999999"})
    assert bad_setup.status_code == 400

    # Valid setup
    secret = pyotp.random_base32()
    totp = pyotp.TOTP(secret)
    good_setup = client.post("/account/2fa", data={"secret": secret, "code": totp.now()}, follow_redirects=False)
    assert good_setup.status_code == 303

    with factory() as db:
        admin = db.query(User).filter(User.username == "admin").one()
        assert admin.totp_secret_encrypted is not None

    # Disable 2FA
    disable_res = client.post(f"/users/{admin.id}/totp/disable", follow_redirects=False)
    assert disable_res.status_code == 200
    with factory() as db:
        admin = db.get(User, admin.id)
        assert admin.totp_secret_encrypted is None

    # Revoke sessions
    revoke_res = client.post(f"/users/{admin.id}/revoke_sessions", follow_redirects=False)
    assert revoke_res.status_code == 200
    with factory() as db:
        admin = db.get(User, admin.id)
        assert admin.token_version > 1


def test_active_session_is_renewed_but_not_on_every_request(test_env):
    client, _ = test_env
    client.post("/login", data={"username": "admin", "password": PASSWORD})
    assert "set-cookie" not in client.get("/people").headers  # fresh session: nothing to renew

    # A session last renewed 10 minutes ago gets a new cookie, which restarts the idle clock.
    old = _encode_session(1, 1)
    real_time = time.time
    try:
        time.time = lambda: real_time() + 600
        client.cookies.set(SESSION_COOKIE, old)
        response = client.get("/people")
        assert SESSION_COOKIE in response.headers.get("set-cookie", "")
    finally:
        time.time = real_time


def test_security_guard_can_enable_2fa_and_see_qr(test_env):
    client, factory = test_env
    client.post("/login", data={"username": "guard", "password": PASSWORD})
    assert client.get("/users").status_code == 403  # still not an admin

    page = client.get("/account/2fa")
    assert page.status_code == 200 and "<svg" in page.text  # QR code for the authenticator
    secret = pyotp.random_base32()
    done = client.post("/account/2fa", data={"secret": secret, "code": pyotp.TOTP(secret).now()}, follow_redirects=False)
    assert done.status_code == 303
    with factory() as db:
        assert db.query(User).filter(User.username == "guard").one().totp_secret_encrypted
    assert "2FA" in client.get("/account").text


def test_account_page_requires_login(test_env):
    client, _ = test_env
    assert client.get("/account").status_code == 401
