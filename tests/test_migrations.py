import logging

import pytest
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from sqlalchemy import create_engine, func, insert, inspect, select, text

from aranet_monitor import cli
from aranet_monitor.db.schema import devices, metadata, readings, readings_hourly
from aranet_monitor.db.store import db_revision, init_db
from conftest import PG_URL

TABLES = {"devices", "readings", "readings_hourly", "collector_events"}


def _plain_engine(db):
    """An engine without schema_translate_map whose *default* schema is the test schema.

    Reflection (autogenerate) ignores schema_translate_map, so on Postgres the search_path is
    set at connect time; then the dialect's default schema is the test schema from the start.
    """
    if db.schema is None:
        return create_engine(db.url)
    return create_engine(db.url, connect_args={"options": f"-c search_path={db.schema}"})


def test_upgrade_head_matches_metadata(db):
    eng = db.engine()
    init_db(eng)  # empty database: upgrade head
    assert db_revision(eng) == "0001"
    plain = _plain_engine(db)
    try:
        with plain.connect() as conn:
            # No differences are ignored: the baseline must reproduce `metadata` exactly.
            assert compare_metadata(MigrationContext.configure(conn), metadata) == []
            if conn.dialect.name == "sqlite":  # not covered by autogenerate
                for table in ("readings", "readings_hourly"):
                    sql = conn.execute(
                        text("SELECT sql FROM sqlite_master WHERE name = :t"), {"t": table}
                    ).scalar()
                    assert sql.rstrip().endswith("WITHOUT ROWID"), sql
    finally:
        plain.dispose()
        eng.dispose()


def test_existing_create_all_db_is_stamped_without_data_loss(db, caplog):
    eng = db.engine()
    metadata.create_all(eng)  # how databases were created before migrations
    with eng.begin() as conn:
        conn.execute(insert(devices).values(id=1, address="AA:BB:CC:DD:EE:01"))
        conn.execute(
            insert(readings),
            [
                {"device_id": 1, "ts": 1_699_999_200 + 60 * i, "source": "adv", "co2": 600 + i}
                for i in range(120)
            ],
        )
    with caplog.at_level(logging.INFO):
        init_db(eng)
    assert db_revision(eng) == "0001"
    assert any("stamp" in r.getMessage() for r in caplog.records)
    with eng.connect() as conn:
        assert conn.execute(select(func.count()).select_from(devices)).scalar() == 1
        assert conn.execute(select(func.count()).select_from(readings)).scalar() == 120
        assert conn.execute(select(func.sum(readings.c.co2))).scalar() == sum(
            600 + i for i in range(120)
        )
        # the rollup backfill still runs for a database that has raw rows only (2 hours)
        assert conn.execute(select(func.count()).select_from(readings_hourly)).scalar() == 2
    eng.dispose()


def test_init_db_is_idempotent(db):
    eng = db.engine()
    init_db(eng)
    init_db(eng)
    assert db_revision(eng) == "0001"
    plain = _plain_engine(db)
    try:
        with plain.connect() as conn:
            assert conn.execute(text("SELECT version_num FROM alembic_version")).all() == [
                ("0001",)
            ]
    finally:
        plain.dispose()
        eng.dispose()


@pytest.mark.skipif(not PG_URL, reason="needs ARANET_TEST_PG_URL")
def test_postgres_tables_and_version_table_live_in_the_schema(db):
    eng = db.engine()
    init_db(eng)
    with eng.connect() as conn:
        insp = inspect(conn)
        assert set(insp.get_table_names(schema=db.schema)) == TABLES | {"alembic_version"}
        assert "alembic_version" not in insp.get_table_names(schema="public")
    eng.dispose()


def test_db_upgrade_prints_revision_transition(db, monkeypatch, capsys):
    db.use_env(monkeypatch)
    assert cli.main(["db-upgrade"]) == 0
    assert "unversioned -> 0001" in capsys.readouterr().out
    assert cli.main(["db-upgrade"]) == 0
    assert "0001 -> 0001" in capsys.readouterr().out
    assert cli.main(["db-init"]) == 0  # kept as an alias
