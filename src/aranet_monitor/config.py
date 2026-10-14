"""Runtime configuration, read from environment variables (and an optional ``.env``)."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

DEFAULT_DB_URL = "sqlite:///data/aranet.sqlite"


def _load_dotenv() -> None:
    """Load ``.env`` from the working directory if python-dotenv is available."""
    try:
        from dotenv import load_dotenv
    except ImportError:  # pragma: no cover - dependency is declared
        return
    load_dotenv(override=False)


def _split(value: str | None) -> tuple[str, ...]:
    if not value:
        return ()
    return tuple(v.strip().upper() for v in value.split(",") if v.strip())


def _postgres_url_from_parts() -> str | None:
    host = os.environ.get("POSTGRES_HOST")
    if not host:
        return None
    from sqlalchemy.engine import URL

    url = URL.create(
        "postgresql+psycopg",
        username=os.environ.get("POSTGRES_USER", "aranet"),
        password=os.environ.get("POSTGRES_PASSWORD") or None,  # URL.create escapes specials
        host=host,
        port=int(os.environ.get("POSTGRES_PORT", "5432")),
        database=os.environ.get("POSTGRES_DB", "aranet"),
    )
    return url.render_as_string(hide_password=False)


@dataclass(frozen=True)
class Settings:
    """Application settings.

    Attributes
    ----------
    db_url
        SQLAlchemy URL. ``ARANET_DB_URL`` wins; otherwise ``POSTGRES_HOST`` (+ friends)
        builds a Postgres URL; otherwise a local SQLite file is used.
    devices
        Optional allow-list of BLE addresses (``ARANET_DEVICES``, comma separated).
        Empty means "any Aranet device that is heard".
    """

    db_url: str = DEFAULT_DB_URL
    db_schema: str | None = None  # Postgres schema for all tables (default: search_path/public)
    devices: tuple[str, ...] = field(default_factory=tuple)
    web_host: str = "127.0.0.1"
    web_port: int = 8050
    backfill_on_start: bool = True
    gap_backfill_min_s: int = 150  # also always >= 1.5 x the device's logging interval
    watchdog_s: int = 180
    ble_timeout_s: float = 30.0
    co2_warn: int = 1000
    co2_alert: int = 1400
    refresh_s: int = 30
    display_tz: str | None = None  # IANA name, e.g. "Europe/London"; None = system local
    web_user: str = "aranet"
    web_password: str | None = field(default=None, repr=False)  # set => HTTP Basic auth on

    @classmethod
    def from_env(cls) -> Settings:
        """Build settings from the process environment (loading ``.env`` first)."""
        _load_dotenv()
        env = os.environ
        db_url = env.get("ARANET_DB_URL") or _postgres_url_from_parts() or DEFAULT_DB_URL
        return cls(
            db_url=db_url,
            db_schema=env.get("ARANET_DB_SCHEMA") or None,
            devices=_split(env.get("ARANET_DEVICES")),
            web_host=env.get("ARANET_WEB_HOST", cls.web_host),
            web_port=int(env.get("ARANET_WEB_PORT", cls.web_port)),
            backfill_on_start=env.get("ARANET_BACKFILL_ON_START", "1").lower()
            not in ("0", "false", "no"),
            gap_backfill_min_s=int(env.get("ARANET_GAP_BACKFILL_MIN_S", cls.gap_backfill_min_s)),
            watchdog_s=int(env.get("ARANET_WATCHDOG_S", cls.watchdog_s)),
            ble_timeout_s=float(env.get("ARANET_BLE_TIMEOUT_S", cls.ble_timeout_s)),
            co2_warn=int(env.get("ARANET_CO2_WARN", cls.co2_warn)),
            co2_alert=int(env.get("ARANET_CO2_ALERT", cls.co2_alert)),
            refresh_s=int(env.get("ARANET_REFRESH_S", cls.refresh_s)),
            display_tz=env.get("ARANET_TZ") or None,
            web_user=env.get("ARANET_WEB_USER", cls.web_user),
            web_password=env.get("ARANET_WEB_PASSWORD") or None,
        )


def ensure_sqlite_dir(db_url: str) -> None:
    """Create the parent directory of a file-based SQLite URL if needed."""
    prefix = "sqlite:///"
    if db_url.startswith(prefix) and ":memory:" not in db_url:
        Path(db_url[len(prefix) :]).expanduser().parent.mkdir(parents=True, exist_ok=True)
