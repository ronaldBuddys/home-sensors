"""Runtime configuration, read from environment variables (and an optional ``.env``)."""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from pathlib import Path

DEFAULT_DB_URL = "sqlite:///data/aranet.sqlite"

log = logging.getLogger(__name__)


def _load_dotenv() -> None:
    """Load ``.env`` (python-dotenv searches upward from this package, i.e. the repo root)."""
    from dotenv import load_dotenv

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

    port = os.environ.get("POSTGRES_PORT", "5432")
    if not port.isdigit():
        raise ValueError(f"POSTGRES_PORT must be a number, got {port!r}")
    password = os.environ.get("POSTGRES_PASSWORD") or None
    if password is None:
        log.warning("POSTGRES_HOST is set but POSTGRES_PASSWORD is empty; connecting without one")
    url = URL.create(
        "postgresql+psycopg",
        username=os.environ.get("POSTGRES_USER", "aranet"),
        password=password,  # URL.create escapes specials
        host=host,
        port=int(port),
        database=os.environ.get("POSTGRES_DB", "aranet"),
        query={  # libpq TLS options; e.g. POSTGRES_SSLMODE=verify-full + POSTGRES_SSLROOTCERT
            key: value
            for key, value in (
                ("sslmode", os.environ.get("POSTGRES_SSLMODE")),
                ("sslrootcert", os.environ.get("POSTGRES_SSLROOTCERT")),
            )
            if value
        },
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

    db_url: str = field(default=DEFAULT_DB_URL, repr=False)  # may contain the DB password
    db_schema: str | None = None  # Postgres schema for all tables (default: search_path/public)
    devices: tuple[str, ...] = field(default_factory=tuple)
    web_host: str = "127.0.0.1"
    web_port: int = 8050
    backfill_on_start: bool = True
    gap_backfill_min_s: int = 150  # also always >= 1.5 x the device's logging interval
    watchdog_s: int = 180
    ble_adapter: str | None = None  # BlueZ adapter name, e.g. "hci1"; None = system default
    adapter_reset: bool = True  # power-cycle the adapter when it stops hearing anything
    ble_timeout_s: float = 30.0
    wait_clock_sync_s: int = 0  # collector waits up to this long for NTP sync (0 = don't wait)
    co2_warn: int = 1000
    co2_alert: int = 1400
    refresh_s: int = 30
    display_tz: str | None = None  # IANA name, e.g. "Europe/London"; None = system local
    web_user: str = "aranet"
    web_password: str | None = field(default=None, repr=False)  # set => HTTP Basic auth on
    api_docs: bool = False  # serve Swagger UI at /api/docs (loads JS from a CDN)
    web_tls_cert: str | None = None  # PEM certificate (chain) => the web app serves HTTPS
    web_tls_key: str | None = None
    # Optional separate (read-only) DB login for the web app; same host/db/schema/TLS options.
    web_db_user: str | None = None
    web_db_password: str | None = field(default=None, repr=False)

    @property
    def web_db_url(self) -> str:
        """DB URL for the web app: ``db_url`` with the web login swapped in, if one is set."""
        if not self.web_db_user:
            return self.db_url
        from sqlalchemy.engine import make_url

        url = make_url(self.db_url).set(username=self.web_db_user, password=self.web_db_password)
        return url.render_as_string(hide_password=False)

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
            ble_adapter=env.get("ARANET_BLE_ADAPTER") or None,
            adapter_reset=env.get("ARANET_ADAPTER_RESET", "1").lower() not in ("0", "false", "no"),
            ble_timeout_s=float(env.get("ARANET_BLE_TIMEOUT_S", cls.ble_timeout_s)),
            wait_clock_sync_s=int(env.get("ARANET_WAIT_CLOCK_SYNC_S", cls.wait_clock_sync_s)),
            co2_warn=int(env.get("ARANET_CO2_WARN", cls.co2_warn)),
            co2_alert=int(env.get("ARANET_CO2_ALERT", cls.co2_alert)),
            refresh_s=int(env.get("ARANET_REFRESH_S", cls.refresh_s)),
            display_tz=env.get("ARANET_TZ") or None,
            web_user=env.get("ARANET_WEB_USER", cls.web_user),
            web_password=env.get("ARANET_WEB_PASSWORD") or None,
            api_docs=env.get("ARANET_API_DOCS", "0").lower() in ("1", "true", "yes"),
            web_tls_cert=env.get("ARANET_WEB_TLS_CERT") or None,
            web_tls_key=env.get("ARANET_WEB_TLS_KEY") or None,
            web_db_user=env.get("ARANET_WEB_DB_USER") or None,
            web_db_password=env.get("ARANET_WEB_DB_PASSWORD") or None,
        )


def ensure_sqlite_dir(db_url: str) -> None:
    """Create the parent directory of a file-based SQLite URL if needed."""
    prefix = "sqlite:///"
    if db_url.startswith(prefix) and ":memory:" not in db_url:
        Path(db_url[len(prefix) :]).expanduser().parent.mkdir(parents=True, exist_ok=True)
