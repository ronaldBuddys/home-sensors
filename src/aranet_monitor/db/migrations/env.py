"""Alembic environment; ``init_db`` runs it on its own connection (``aranet db-upgrade``).

Every writer (collector, ``db-upgrade``, seeding, ``copy-db`` target) migrates the database
to the newest revision on start-up, so a schema change is: edit ``db/schema.py``, then add
a revision. Autogenerate it against a SQLite database that is at head, using the developer
``alembic.ini`` at the repo root (it only points Alembic at this directory)::

    export ARANET_DB_URL=sqlite:///data/dev.sqlite
    uv run aranet db-upgrade
    uv run alembic revision --autogenerate --rev-id 0002 -m "add annotations"

Programmatically: ``alembic.command.revision(config, autogenerate=True, rev_id=...,
message=...)`` with a ``Config`` from ``aranet_monitor.db.store.alembic_config``. Review the
generated file -- autogenerate does not see everything (e.g. SQLite's ``WITHOUT ROWID``) --
and keep ``tests/test_migrations.py`` green on SQLite and Postgres.
"""

from __future__ import annotations

from alembic import context
from sqlalchemy import Connection

from aranet_monitor.db.schema import metadata
from aranet_monitor.db.store import make_engine, schema_of


def run_migrations(connection: Connection) -> None:
    """Run the migrations on ``connection``, inside its Postgres schema if it has one."""
    context.configure(
        connection=connection,
        target_metadata=metadata,
        # tables follow the engine's schema_translate_map; alembic_version goes with them
        version_table_schema=schema_of(connection),
        render_as_batch=connection.dialect.name == "sqlite",  # ALTER TABLE by table copy
    )
    with context.begin_transaction():  # a no-op inside init_db's transaction
        context.run_migrations()


_connection = context.config.attributes.get("connection")
if _connection is not None:
    run_migrations(_connection)
else:  # the ``alembic`` command line: the configured database (ARANET_DB_URL, ...)
    from aranet_monitor.config import Settings

    _settings = Settings.from_env()
    _engine = make_engine(_settings.db_url, schema=_settings.db_schema)
    with _engine.begin() as _conn:
        run_migrations(_conn)
