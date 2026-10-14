"""ASGI application: FastAPI owns the process; the Dash dashboard is mounted at ``/``."""

from __future__ import annotations

from a2wsgi import WSGIMiddleware
from fastapi import FastAPI
from fastapi.middleware.gzip import GZipMiddleware

from aranet_monitor import __version__
from aranet_monitor.api.routes import router
from aranet_monitor.config import Settings
from aranet_monitor.db.queries import Queries
from aranet_monitor.db.store import init_db, make_engine


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
