from contextlib import asynccontextmanager
from collections.abc import AsyncIterator, Generator

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.auth import authenticate, hash_password
from app.db import Base, get_db
from app.main import app
from app.models import User

PASSWORD = "admin-password-123"


@pytest.fixture
def env(monkeypatch: pytest.MonkeyPatch):
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
        db.add(User(username="admin", password_hash=hash_password(PASSWORD), role="admin"))
        db.add(User(username="guard", password_hash=hash_password(PASSWORD), role="security"))
        db.commit()
    with TestClient(app) as client:
        yield client, factory
    app.dependency_overrides.clear()
    engine.dispose()


def _login(client: TestClient, username: str) -> None:
    response = client.post("/login", data={"username": username, "password": PASSWORD}, follow_redirects=False)
    assert response.status_code == 303


def _user(factory, username: str) -> User | None:
    with factory() as db:
        return db.scalar(select(User).where(User.username == username))


def test_security_user_cannot_open_users_page(env) -> None:
    client, _ = env
    _login(client, "guard")
    assert client.get("/users").status_code == 403


def test_admin_adds_user_who_can_log_in(env) -> None:
    client, factory = env
    _login(client, "admin")
    assert client.get("/users").status_code == 200
    response = client.post(
        "/users/new",
        data={"username": "gate2", "password": "new-password-123", "confirm_password": "new-password-123", "role": "security"},
        follow_redirects=False,
    )
    assert response.status_code == 303
    created = _user(factory, "gate2")
    assert created is not None and created.role == "security"
    with factory() as db:
        assert authenticate(db, "gate2", "new-password-123") is not None


@pytest.mark.parametrize(
    "data",
    [
        {"username": "x", "password": "short", "confirm_password": "short", "role": "security"},
        {"username": "x", "password": "new-password-123", "confirm_password": "other-password-123", "role": "security"},
        {"username": "x", "password": "new-password-123", "confirm_password": "new-password-123", "role": "root"},
        {"username": "guard", "password": "new-password-123", "confirm_password": "new-password-123", "role": "security"},
    ],
)
def test_invalid_new_user_is_rejected(env, data) -> None:
    client, factory = env
    _login(client, "admin")
    assert client.post("/users/new", data=data).status_code == 400
    if data["username"] == "x":
        assert _user(factory, "x") is None


def test_admin_cannot_deactivate_self_but_can_deactivate_others(env) -> None:
    client, factory = env
    _login(client, "admin")
    admin_id = _user(factory, "admin").id
    guard_id = _user(factory, "guard").id
    assert client.post(f"/users/{admin_id}/active", data={"active": "false"}).status_code == 400
    assert _user(factory, "admin").active
    assert client.post(f"/users/{guard_id}/active", data={"active": "false"}, follow_redirects=False).status_code == 303
    assert not _user(factory, "guard").active


def test_admin_resets_password(env) -> None:
    client, factory = env
    _login(client, "admin")
    guard_id = _user(factory, "guard").id
    response = client.post(
        f"/users/{guard_id}/password",
        data={"password": "changed-password-1", "confirm_password": "changed-password-1"},
    )
    assert response.status_code == 200
    with factory() as db:
        assert authenticate(db, "guard", "changed-password-1") is not None
        assert authenticate(db, "guard", PASSWORD) is None


def test_dashboard_shows_admin_links_only_to_admin(env) -> None:
    client, _ = env
    _login(client, "admin")
    assert 'href="/users' in client.get("/").text
    client.post("/logout")
    _login(client, "guard")
    page = client.get("/").text
    assert 'href="/users' not in page and "/logout" in page
