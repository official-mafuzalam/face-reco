from contextlib import asynccontextmanager
from collections.abc import Generator
from http.cookies import SimpleCookie
from typing import AsyncIterator

import pytest
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.auth import get_current_user, hash_password, require_role
from app.config import Settings, settings
from app.db import Base, get_db
from app.main import app, decrypt_rtsp_url, encrypt_rtsp_url
from app.models import Camera, User


def test_login_redirect_opens_home_and_reports_cookie(client: TestClient) -> None:
    login_response = client.post(
        "/login",
        data={"username": "admin", "password": "test-password-123"},
        follow_redirects=True,
    )
    login_redirect = login_response.history[0]
    cookie_header = login_redirect.headers.get("set-cookie")
    assert cookie_header is not None
    cookie = SimpleCookie()
    cookie.load(cookie_header)
    attributes = cookie["gate_session"]
    print(
        "gate_session cookie:",
        f"Secure={attributes['secure'] or False},",
        f"SameSite={attributes['samesite']},",
        f"Path={attributes['path']},",
        f"HttpOnly={bool(attributes['httponly'])}",
    )
    assert not attributes["secure"]
    assert attributes["samesite"].lower() == "lax"
    assert attributes["path"] == "/"
    assert bool(attributes["httponly"])

    home_response = client.get("/", follow_redirects=True)
    assert home_response.status_code == 200
    assert 'id="username"' not in home_response.text


def test_wrong_password_stays_on_login(client: TestClient) -> None:
    response = client.post(
        "/login",
        data={"username": "admin", "password": "wrong-password"},
        follow_redirects=True,
    )

    assert response.status_code == 401
    assert response.request.url.path == "/login"
    assert 'id="username"' in response.text


def test_logout_then_home_redirects_to_login(client: TestClient) -> None:
    client.post(
        "/login",
        data={"username": "admin", "password": "test-password-123"},
        follow_redirects=False,
    )
    logout_response = client.post("/logout", follow_redirects=True)
    assert logout_response.request.url.path == "/login"

    home_response = client.get("/", follow_redirects=False)
    assert home_response.status_code == 303
    assert home_response.headers["location"] == "/login?lang=en"


def test_cookie_secure_setting_is_honored(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "cookie_secure", True)
    response = client.post(
        "/login",
        data={"username": "admin", "password": "test-password-123"},
        follow_redirects=False,
    )

    cookie = SimpleCookie()
    cookie.load(response.headers["set-cookie"])
    assert cookie["gate_session"]["secure"]


def test_cookie_secure_setting_reads_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("COOKIE_SECURE", raising=False)
    assert Settings(_env_file=None).cookie_secure is False

    monkeypatch.setenv("COOKIE_SECURE", "true")
    assert Settings(_env_file=None).cookie_secure is True


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch) -> Generator[TestClient, None, None]:
    monkeypatch.setattr(settings, "cookie_secure", False)
    test_engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(test_engine)
    test_session = sessionmaker(bind=test_engine, autoflush=False, expire_on_commit=False)

    def override_get_db() -> Generator[Session, None, None]:
        with test_session() as db:
            yield db

    @asynccontextmanager
    async def test_lifespan(_app: FastAPI) -> AsyncIterator[None]:
        yield

    monkeypatch.setattr(app.router, "lifespan_context", test_lifespan)
    app.dependency_overrides[get_db] = override_get_db
    with test_session() as db:
        db.add(User(username="admin", password_hash=hash_password("test-password-123"), role="admin"))
        db.commit()

    with TestClient(app) as test_client:
        yield test_client

    app.dependency_overrides.clear()
    test_engine.dispose()


def test_login_success_and_failure(client: TestClient) -> None:
    failed = client.post(
        "/login",
        data={"username": "admin", "password": "wrong-password"},
        follow_redirects=False,
    )
    assert failed.status_code == 401
    assert "gate_session" not in client.cookies

    succeeded = client.post(
        "/login",
        data={"username": "admin", "password": "test-password-123"},
        follow_redirects=False,
    )
    assert succeeded.status_code == 303
    assert "gate_session" in client.cookies


def test_role_guard_rejects_security_user() -> None:
    protected_app = FastAPI()

    def security_user() -> User:
        return User(id=1, username="guarded", password_hash="hash", role="security", active=True)

    protected_app.dependency_overrides[get_current_user] = security_user

    @protected_app.get("/admin")
    def admin_only(user: User = Depends(require_role("admin"))) -> dict[str, str]:
        return {"username": user.username}

    with TestClient(protected_app) as test_client:
        response = test_client.get("/admin")
    assert response.status_code == 403


def test_rtsp_url_is_encrypted_in_database() -> None:
    test_engine = create_engine("sqlite://", poolclass=StaticPool)
    Base.metadata.create_all(test_engine)
    test_session = sessionmaker(bind=test_engine)
    rtsp_url = "rtsp://operator:secret@example.invalid/stream"

    with test_session() as db:
        db.add(
            Camera(
                name="Front",
                gate="North",
                direction="IN",
                rtsp_url_encrypted=encrypt_rtsp_url(rtsp_url),
            )
        )
        db.commit()
        stored = db.query(Camera).one().rtsp_url_encrypted
        assert stored != rtsp_url
        assert decrypt_rtsp_url(stored) == rtsp_url

    test_engine.dispose()
