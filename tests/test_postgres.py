"""Postgres-only behaviour: schemas, read-only transactions and the read-only role."""

import secrets
import uuid

import pytest
from sqlalchemy import create_engine, func, select, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import DBAPIError, ProgrammingError

from aranet_monitor import cli
from aranet_monitor.db.schema import readings
from aranet_monitor.db.store import Store, check_schema, init_db, make_engine
from aranet_monitor.models import DeviceInfo, Reading
from conftest import ADDR, PG_URL

pytestmark = pytest.mark.skipif(not PG_URL, reason="needs ARANET_TEST_PG_URL")


@pytest.fixture
def ro_role():
    """A throwaway login role (skipped if the test user may not create roles)."""
    name, password = f"ro_{uuid.uuid4().hex[:8]}", secrets.token_hex(8)
    admin = create_engine(PG_URL, isolation_level="AUTOCOMMIT")
    try:
        with admin.connect() as conn:
            try:
                conn.execute(text(f"CREATE ROLE {name} LOGIN PASSWORD '{password}'"))
            except ProgrammingError:
                pytest.skip("test user lacks CREATEROLE")
        try:
            yield name, password
        finally:
            with admin.connect() as conn:
                conn.execute(text(f"DROP OWNED BY {name}"))
                conn.execute(text(f"DROP ROLE {name}"))
    finally:
        admin.dispose()


def test_grant_read_role_can_select_but_not_write(db, ro_role, monkeypatch, capsys):
    role, password = ro_role
    eng = db.engine()
    init_db(eng)
    st = Store(eng)
    dev = st.upsert_device(DeviceInfo(ADDR, "Aranet4 TEST", "Aranet4", "v1", 60), seen=0)
    st.bulk_insert_raw(dev, [Reading(ADDR, t * 60, "adv", co2=500) for t in range(5)])
    db.use_env(monkeypatch)
    assert cli.main(["grant-read", role]) == 0
    capsys.readouterr()

    url = make_url(PG_URL).set(username=role, password=password)
    ro = make_engine(url.render_as_string(hide_password=False), schema=db.schema)
    try:
        with ro.connect() as conn:
            assert conn.execute(select(func.count()).select_from(readings)).scalar() == 5
        with pytest.raises(ProgrammingError, match="permission denied"):
            Store(ro).log_event("start", "must fail", dev)
    finally:
        ro.dispose()
        eng.dispose()


def test_read_only_engine_rejects_writes(db):
    init_db(db.engine())
    ro = db.engine(read_only=True)
    try:
        with pytest.raises(DBAPIError, match="read-only transaction"):
            Store(ro).log_event("start", "must fail", None)
    finally:
        ro.dispose()


def test_check_schema_with_schema(db):
    eng = db.engine()
    with pytest.raises(RuntimeError, match="db-init"):
        check_schema(eng)
    init_db(eng)
    check_schema(eng)
    eng.dispose()
