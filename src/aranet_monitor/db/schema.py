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

# Hourly rollup used for long windows (bucket >= 1 h): per-metric sum/count/min/max so means
# can be re-aggregated exactly into any multiple of an hour. CO₂ exposure counts are kept at
# fixed levels so "time above threshold" stats for long windows don't need the raw table.
ROLLUP_METRICS: tuple[str, ...] = (
    "co2",
    "temperature",
    "humidity",
    "pressure",
    "battery",
    "radon",
    "radiation_rate",
)
CO2_LEVELS: tuple[int, ...] = (800, 1000, 1200, 1400, 1500, 2000)

_rollup_cols = []
for _m in ROLLUP_METRICS:
    _rollup_cols += [
        Column(f"{_m}_sum", Float),
        Column(f"{_m}_cnt", Integer),
        Column(f"{_m}_min", Float),
        Column(f"{_m}_max", Float),
    ]
_rollup_cols += [Column(f"co2_ge_{lvl}", Integer) for lvl in CO2_LEVELS]

readings_hourly = Table(
    "readings_hourly",
    metadata,
    Column("device_id", Integer, ForeignKey("devices.id"), nullable=False),
    Column("hour", BigInteger, nullable=False),  # epoch seconds, multiple of 3600
    Column("n", Integer, nullable=False),
    *_rollup_cols,
    PrimaryKeyConstraint("device_id", "hour", name="pk_readings_hourly"),
    sqlite_with_rowid=False,
)
