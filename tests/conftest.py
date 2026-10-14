import os

import pytest

from aranet_monitor.db.queries import Queries
from aranet_monitor.db.store import Store, init_db, make_engine
from aranet_monitor.models import DeviceInfo, Reading

# Real advert captured from an Aranet4 (fw v1.x/"v2.0.12" per advert) on 2026-10-14.
REAL_ADVERT = bytes.fromhex("210c000200120f013f03bb011828361c013c000e0044")
ADDR = "AA:BB:CC:DD:EE:01"


PG_URL = os.environ.get("ARANET_TEST_PG_URL")  # e.g. postgresql+psycopg://u:p@localhost:55432/t
PG_SCHEMA = os.environ.get("ARANET_TEST_PG_SCHEMA")  # optional, must exist and be writable


@pytest.fixture
def engine(tmp_path):
    """SQLite by default; the same tests run on Postgres when ARANET_TEST_PG_URL is set."""
    if PG_URL:
        from aranet_monitor.db.schema import metadata

        eng = make_engine(PG_URL, schema=PG_SCHEMA)
        metadata.drop_all(eng)
        init_db(eng)
        yield eng
        eng.dispose()
        return
    eng = make_engine(f"sqlite:///{tmp_path}/test.sqlite")
    init_db(eng)
    yield eng


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
