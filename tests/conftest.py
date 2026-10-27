# Run the whole suite against Postgres as well as SQLite (each test gets its own schema):
#   docker run -d --rm --name hs-pg -e POSTGRES_PASSWORD=pg -p 55432:5432 postgres:17
#   ARANET_TEST_PG_URL=postgresql+psycopg://postgres:pg@localhost:55432/postgres uv run pytest -q
import os
import tempfile
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

import pytest
from sqlalchemy import create_engine, text

from aranet_monitor.config import Settings
from aranet_monitor.db.queries import Queries
from aranet_monitor.db.store import Store, init_db, make_engine
from aranet_monitor.models import DeviceInfo, Reading

# Real advert captured from an Aranet4 (fw v1.x/"v2.0.12" per advert) on 2026-10-14.
REAL_ADVERT = bytes.fromhex("210c000200120f013f03bb011828361c013c000e0044")
ADDR = "AA:BB:CC:DD:EE:01"


@pytest.fixture(autouse=True)
def _isolate_from_local_env(monkeypatch):
    """Never let the developer's .env / shell config (real Postgres!) leak into tests."""
    import aranet_monitor.config as config

    monkeypatch.setattr(config, "_load_dotenv", lambda: None)
    for key in list(os.environ):
        if key.startswith(("ARANET_", "POSTGRES_")) and not key.startswith("ARANET_TEST_"):
            monkeypatch.delenv(key, raising=False)


@pytest.fixture(autouse=True)
def _no_real_bluez(monkeypatch):
    """The collector's default scan-mode probe asks the host's BlueZ; keep tests hermetic."""
    import aranet_monitor.ble.collector as collector

    async def no_passive(_adapter):
        return False

    monkeypatch.setattr(collector, "passive_scan_supported", no_passive)


PG_URL = os.environ.get("ARANET_TEST_PG_URL")  # e.g. postgresql+psycopg://u:p@localhost:55432/t


@dataclass(frozen=True)
class TestDb:
    """One empty database for a test: a tmp SQLite file, or a fresh Postgres schema."""

    __test__ = False  # not a test class, despite the name

    url: str
    schema: str | None

    def settings(self, **kw) -> Settings:
        return Settings(db_url=self.url, db_schema=self.schema, **kw)

    def use_env(self, monkeypatch) -> None:
        monkeypatch.setenv("ARANET_DB_URL", self.url)
        if self.schema:
            monkeypatch.setenv("ARANET_DB_SCHEMA", self.schema)

    def engine(self, **kw):
        return make_engine(self.url, schema=self.schema, **kw)

    def target_args(self) -> list[str]:
        """``copy-db`` arguments that point at this database as the target."""
        return ["--target", self.url, *(["--target-schema", self.schema] if self.schema else [])]


class _DbFactory:
    """Hands out empty databases and removes them again (Postgres schemas are dropped)."""

    def __init__(self, tmp_dir: Path):
        self._tmp = tmp_dir
        self._schemas: list[str] = []
        self._n = 0

    def __call__(self) -> TestDb:
        if not PG_URL:
            self._n += 1
            return TestDb(f"sqlite:///{self._tmp}/test{self._n}.sqlite", None)
        schema = f"t_{uuid.uuid4().hex[:8]}"
        self._admin(f'CREATE SCHEMA "{schema}"')
        self._schemas.append(schema)
        return TestDb(PG_URL, schema)

    def _admin(self, sql: str) -> None:
        eng = create_engine(PG_URL, isolation_level="AUTOCOMMIT")
        try:
            with eng.connect() as conn:
                conn.execute(text(sql))
        finally:
            eng.dispose()

    def close(self) -> None:
        for schema in self._schemas:
            self._admin(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')


@pytest.fixture
def db_factory(tmp_path) -> Callable[[], TestDb]:
    """Call it for each extra empty database a test needs."""
    factory = _DbFactory(tmp_path)
    yield factory
    factory.close()


@pytest.fixture
def db(db_factory) -> TestDb:
    return db_factory()


@pytest.fixture(scope="module")
def module_db() -> TestDb:
    """Like ``db`` but shared by a whole module (for expensive, read-only fixtures)."""
    with tempfile.TemporaryDirectory() as tmp:
        factory = _DbFactory(Path(tmp))
        try:
            yield factory()
        finally:
            factory.close()


@pytest.fixture
def engine(db):
    """SQLite by default; the same tests run on Postgres when ARANET_TEST_PG_URL is set."""
    eng = db.engine()
    init_db(eng)
    yield eng
    eng.dispose()


@pytest.fixture
def store(engine):
    return Store(engine)


@pytest.fixture
def queries(engine):
    return Queries(engine)


@pytest.fixture
def device_id(store):
    return store.upsert_device(DeviceInfo(ADDR, "Aranet4 TEST", "Aranet4", "v1", 60), seen=0)


def make_reading(ts, co2=800, source="adv", **kw):
    return Reading(
        address=ADDR,
        ts=ts,
        source=source,
        co2=co2,
        temperature=21.0,
        humidity=50.0,
        pressure=1010.0,
        **kw,
    )
