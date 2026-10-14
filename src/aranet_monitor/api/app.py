"""ASGI application: FastAPI owns the process; the Dash dashboard is mounted at ``/``."""

from __future__ import annotations

import base64
import secrets

from a2wsgi import WSGIMiddleware
from fastapi import FastAPI
from fastapi.middleware.gzip import GZipMiddleware

from aranet_monitor import __version__
from aranet_monitor.api.routes import router
from aranet_monitor.config import Settings
from aranet_monitor.db.queries import Queries
from aranet_monitor.db.store import init_db, make_engine


class BasicAuthMiddleware:
    """Pure-ASGI HTTP Basic auth covering every route, including the mounted Dash app.

    ``/healthz`` stays open for container/systemd health checks.
    """

    def __init__(self, app, user: str, password: str):
        self.app = app
        self._expected = f"{user}:{password}".encode()

    def _ok(self, header: bytes | None) -> bool:
        if not header or not header.startswith(b"Basic "):
            return False
        try:
            supplied = base64.b64decode(header[6:], validate=True)
        except ValueError:
            return False
        return secrets.compare_digest(supplied, self._expected)

    async def __call__(self, scope, receive, send):
        """Reject unauthenticated HTTP requests with 401."""
        if scope["type"] != "http" or scope.get("path") == "/healthz":
            return await self.app(scope, receive, send)
        headers = dict(scope.get("headers") or [])
        if self._ok(headers.get(b"authorization")):
            return await self.app(scope, receive, send)
        await send(
            {
                "type": "http.response.start",
                "status": 401,
                "headers": [
                    (b"www-authenticate", b'Basic realm="Aranet Monitor", charset="UTF-8"'),
                    (b"content-type", b"text/plain"),
                ],
            }
        )
        await send({"type": "http.response.body", "body": b"authentication required"})


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
    engine = make_engine(settings.db_url)
    init_db(engine)
    queries = Queries(engine)

    app = FastAPI(
        title="Aranet Monitor",
        version=__version__,
        docs_url="/api/docs",
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
