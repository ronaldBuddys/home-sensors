"""ASGI application: FastAPI owns the process; the Dash dashboard is mounted at ``/``."""

from __future__ import annotations

import base64
import secrets
import time

from a2wsgi import WSGIMiddleware
from fastapi import FastAPI
from fastapi.middleware.gzip import GZipMiddleware

from aranet_monitor import __version__
from aranet_monitor.api.routes import router
from aranet_monitor.config import Settings
from aranet_monitor.db.queries import Queries
from aranet_monitor.db.store import check_schema, make_engine


class BasicAuthMiddleware:
    """Pure-ASGI HTTP Basic auth covering every route, including the mounted Dash app.

    ``/healthz`` stays open for health checks.
    """

    MAX_FAILURES = 5  # per client address ...
    WINDOW_S = 600  # ... within this window, then 429 until the window has passed
    MAX_TRACKED = 1024  # bound memory when many addresses fail

    def __init__(self, app, user: str, password: str):
        self.app = app
        self._expected = f"{user}:{password}".encode()
        self._failures: dict[str, tuple[int, float]] = {}  # client -> (count, window start)

    def _locked(self, client: str, now: float) -> bool:
        count, since = self._failures.get(client, (0, now))
        if now - since > self.WINDOW_S:
            self._failures.pop(client, None)
            return False
        return count >= self.MAX_FAILURES

    def _record_failure(self, client: str, now: float) -> None:
        if len(self._failures) >= self.MAX_TRACKED:
            self._failures = {
                c: v for c, v in self._failures.items() if now - v[1] <= self.WINDOW_S
            }
        count, since = self._failures.get(client, (0, now))
        if now - since > self.WINDOW_S:
            count, since = 0, now
        self._failures[client] = (count + 1, since)

    def _ok(self, header: bytes | None) -> bool:
        if not header or header[:6].lower() != b"basic ":
            return False
        try:
            supplied = base64.b64decode(header[6:], validate=True)
        except ValueError:
            return False
        return secrets.compare_digest(supplied, self._expected)

    async def __call__(self, scope, receive, send):
        """Reject unauthenticated HTTP (401) and websocket (close 1008) connections."""
        if scope["type"] == "lifespan" or scope.get("path") == "/healthz":
            return await self.app(scope, receive, send)
        client = (scope.get("client") or ("?",))[0]
        now = time.monotonic()
        if self._locked(client, now):
            return await self._reject(scope, send, 429, b"too many failed logins; try later")
        headers = dict(scope.get("headers") or [])
        header = headers.get(b"authorization")
        if self._ok(header):
            self._failures.pop(client, None)
            return await self.app(scope, receive, send)
        if header:  # a wrong password, not just the browser's first unauthenticated request
            self._record_failure(client, now)
        return await self._reject(scope, send, 401, b"authentication required")

    @staticmethod
    async def _reject(scope, send, status: int, body: bytes) -> None:
        if scope["type"] != "http":
            await send({"type": "websocket.close", "code": 1008})
            return
        await send(
            {
                "type": "http.response.start",
                "status": status,
                "headers": [
                    (b"www-authenticate", b'Basic realm="Aranet Monitor", charset="UTF-8"'),
                    (b"content-type", b"text/plain"),
                ],
            }
        )
        await send({"type": "http.response.body", "body": body})


# Dash needs inline/eval script (renderer bootstrap, Plotly); everything is served locally.
CSP = (
    "default-src 'self'; script-src 'self' 'unsafe-inline' 'unsafe-eval'; "
    "style-src 'self' 'unsafe-inline'; img-src 'self' data: blob:; font-src 'self' data:; "
    "connect-src 'self'; object-src 'none'; base-uri 'self'; form-action 'self'; "
    "frame-ancestors 'none'"
)
SECURITY_HEADERS = [
    (b"x-content-type-options", b"nosniff"),
    (b"x-frame-options", b"DENY"),
    (b"referrer-policy", b"no-referrer"),
    (b"cross-origin-opener-policy", b"same-origin"),
]


class SecurityHeadersMiddleware:
    """Add security headers to every HTTP response (CSP except on the optional API docs page)."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        """Wrap ``send`` to append the headers to ``http.response.start``."""
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        extra = list(SECURITY_HEADERS)
        if scope.get("path") != "/api/docs":  # Swagger UI loads its JS/CSS from a CDN
            extra.append((b"content-security-policy", CSP.encode()))

        async def send_with_headers(message):
            if message["type"] == "http.response.start":
                message["headers"] = [*message.get("headers", []), *extra]
            await send(message)

        return await self.app(scope, receive, send_with_headers)


def create_app(settings: Settings | None = None, *, with_dash: bool = True) -> FastAPI:
    """Build the web application.

    Parameters
    ----------
    settings
        Defaults to :meth:`Settings.from_env`.
    with_dash
        Mount the Dash UI at ``/`` (disable for API-only tests).
    """
    settings = settings or Settings.from_env()
    engine = make_engine(settings.web_db_url, schema=settings.db_schema, read_only=True)
    check_schema(engine)  # read-only: no DDL, so a read-only DB role is enough
    queries = Queries(engine)

    app = FastAPI(
        title="Aranet Monitor",
        version=__version__,
        docs_url="/api/docs" if settings.api_docs else None,
        redoc_url=None,
        openapi_url="/api/openapi.json",
    )
    app.state.settings = settings
    app.state.queries = queries
    app.add_middleware(GZipMiddleware, minimum_size=1024)
    if settings.web_password:
        app.add_middleware(
            BasicAuthMiddleware, user=settings.web_user, password=settings.web_password
        )
    app.add_middleware(SecurityHeadersMiddleware)  # added last = outermost, so 401s get them too
    app.include_router(router)

    @app.get("/healthz", include_in_schema=False)
    def healthz() -> dict:
        return {"ok": True}

    if with_dash:
        from aranet_monitor.web.dash_app import create_dash_app

        dash_app = create_dash_app(queries, settings)
        # Routes registered above take precedence; everything else goes to Dash.
        app.mount("/", WSGIMiddleware(dash_app.server))
    return app
