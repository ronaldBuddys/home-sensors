"""SQLAlchemy Core schema; identical on SQLite and Postgres."""

from __future__ import annotations

from sqlalchemy import (
    BigInteger,
    Column,
    Float,
    ForeignKey,
    Index,
    Integer,
    MetaData,
    PrimaryKeyConstraint,
    SmallInteger,
    String,
    Table,
    Text,
)

metadata = MetaData()

devices = Table(
    "devices",
    metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("address", String(64), nullable=False, unique=True),
    Column("name", String(128)),
    Column("model", String(64)),
    Column("firmware", String(32)),
    Column("interval_s", Integer),
    Column("first_seen", BigInteger),
    Column("last_seen", BigInteger),
    Column("last_rssi", SmallInteger),
)

# One row per logged measurement. The composite PK doubles as the time index; on SQLite the
# table is WITHOUT ROWID so rows are physically clustered by (device_id, ts) and range scans
# for charts are sequential reads.
readings = Table(
    "readings",
    metadata,
    Column("device_id", Integer, ForeignKey("devices.id"), nullable=False),
    Column("ts", BigInteger, nullable=False),  # epoch seconds, UTC, measurement time
    Column("source", String(8), nullable=False),  # adv | history | sim
    Column("co2", SmallInteger),
    Column("temperature", Float),
    Column("humidity", Float),
    Column("pressure", Float),
    Column("battery", SmallInteger),
    Column("status", SmallInteger),
    Column("rssi", SmallInteger),
    Column("radon", Integer),
    Column("radiation_rate", Float),
    Column("radiation_total", Float),
    PrimaryKeyConstraint("device_id", "ts", name="pk_readings"),
    sqlite_with_rowid=False,
)

events = Table(
    "collector_events",
    metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("ts", BigInteger, nullable=False),
    Column("device_id", Integer, ForeignKey("devices.id")),
    Column("kind", String(32), nullable=False),
    Column("detail", Text),
)
Index("ix_events_ts", events.c.ts)
