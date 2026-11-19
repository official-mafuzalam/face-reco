import threading
import time
from collections import deque
from urllib.parse import urlsplit

from cryptography.fernet import Fernet
from fastapi import Request
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app.config import settings


_UNSAFE_METHODS = {"POST", "PUT", "PATCH", "DELETE"}
_SECURITY_HEADERS = [
    (b"x-content-type-options", b"nosniff"),
    (b"x-frame-options", b"DENY"),
    (b"referrer-policy", b"same-origin"),
    (b"content-security-policy", b"frame-ancestors 'none'"),
]


class SecurityMiddleware:
    """Adds security headers and rejects cross-origin state-changing requests.

    Browsers send Origin (or at least Referer) on form POSTs, so a request whose
    origin host differs from the Host header is treated as CSRF and refused.
    Requests without either header (non-browser clients) are allowed.
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        headers = {key.lower(): value for key, value in scope["headers"]}
        if scope["method"] in _UNSAFE_METHODS and not _same_origin(headers):
            await _forbidden(send)
            return

        async def send_with_headers(message: Message) -> None:
            if message["type"] == "http.response.start":
                existing = {key.lower() for key, _ in message.get("headers", [])}
                extra = [header for header in _SECURITY_HEADERS if header[0] not in existing]
                refresh = scope.get("state", {}).get("refresh_session")
                if refresh is not None:
                    from app.auth import session_cookie_header

                    extra.append(session_cookie_header(*refresh))
                message["headers"] = list(message.get("headers", [])) + extra
            await send(message)

        await self.app(scope, receive, send_with_headers)


def _same_origin(headers: dict[bytes, bytes]) -> bool:
    host = headers.get(b"host", b"").decode("latin-1").lower()
    source = headers.get(b"origin") or headers.get(b"referer")
    if source is None:
        return True
    value = source.decode("latin-1")
    if value == "null":
        return False
    return urlsplit(value).netloc.lower() == host


async def _forbidden(send: Send) -> None:
    body = b"Cross-origin request blocked"
    await send(
        {
            "type": "http.response.start",
            "status": 403,
            "headers": [
                (b"content-type", b"text/plain; charset=utf-8"),
                (b"content-length", str(len(body)).encode()),
                *_SECURITY_HEADERS,
            ],
        }
    )
    await send({"type": "http.response.body", "body": body})


class LoginRateLimiter:
    """Limits failed logins per client address and per username."""

    def __init__(self, max_failures: int = 5, window_s: float = 900.0) -> None:
        self.max_failures = max_failures
        self.window_s = window_s
        self._failures: dict[str, deque[float]] = {}
        self._lock = threading.Lock()

    def _keys(self, client: str, username: str) -> list[str]:
        return [f"ip:{client}", f"user:{username.lower()}"]

    def _recent(self, key: str, now: float) -> deque[float]:
        entries = self._failures.setdefault(key, deque())
        while entries and now - entries[0] > self.window_s:
            entries.popleft()
        return entries

    def blocked(self, client: str, username: str) -> bool:
        now = time.monotonic()
        with self._lock:
            return any(
                len(self._recent(key, now)) >= self.max_failures
                for key in self._keys(client, username)
            )

    def record_failure(self, client: str, username: str) -> None:
        now = time.monotonic()
        with self._lock:
            for key in self._keys(client, username):
                self._recent(key, now).append(now)

    def reset(self, client: str, username: str) -> None:
        with self._lock:
            for key in self._keys(client, username):
                self._failures.pop(key, None)


login_limiter = LoginRateLimiter()

_LOOPBACK_IPS = {"127.0.0.1", "::1", "localhost", "testclient"}


def get_client_ip(request: Request, trust_cloudflare: bool = False) -> str:
    """Extracts client IP. Trusts CF-Connecting-IP only if request comes from loopback and trust_cloudflare is True."""
    direct_ip = request.client.host if request.client else "unknown"
    if trust_cloudflare and direct_ip in _LOOPBACK_IPS:
        cf_ip = request.headers.get("cf-connecting-ip")
        if cf_ip:
            return cf_ip.strip()
    return direct_ip


def encrypt_totp_secret(secret: str) -> str:
    fernet = Fernet(settings.fernet_key.encode())
    return fernet.encrypt(secret.encode()).decode()


def decrypt_totp_secret(encrypted_secret: str) -> str:
    fernet = Fernet(settings.fernet_key.encode())
    return fernet.decrypt(encrypted_secret.encode()).decode()

