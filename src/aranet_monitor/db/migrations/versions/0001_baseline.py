"""Baseline: the schema as ``metadata.create_all`` made it before migrations.

Databases created that way (no ``alembic_version`` table) are stamped at this revision by
``init_db`` instead of running it. SQLite keeps ``WITHOUT ROWID`` for the two time-series
tables, so rows are clustered by their ``(device_id, ts|hour)`` primary key.

Revision ID: 0001
Revises: -
Create Date: 2026-10-19 13:16:06.889848
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Create the tables, keys and index of the original schema."""
    op.create_table(
        "devices",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("address", sa.String(length=64), nullable=False),
        sa.Column("name", sa.String(length=128), nullable=True),
        sa.Column("model", sa.String(length=64), nullable=True),
        sa.Column("firmware", sa.String(length=32), nullable=True),
        sa.Column("interval_s", sa.Integer(), nullable=True),
        sa.Column("first_seen", sa.BigInteger(), nullable=True),
        sa.Column("last_seen", sa.BigInteger(), nullable=True),
        sa.Column("last_rssi", sa.SmallInteger(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("address"),
    )
    op.create_table(
        "collector_events",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("ts", sa.BigInteger(), nullable=False),
        sa.Column("device_id", sa.Integer(), nullable=True),
        sa.Column("kind", sa.String(length=32), nullable=False),
        sa.Column("detail", sa.Text(), nullable=True),
        sa.ForeignKeyConstraint(["device_id"], ["devices.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_events_ts", "collector_events", ["ts"], unique=False)
    op.create_table(
        "readings",
        sa.Column("device_id", sa.Integer(), nullable=False),
        sa.Column("ts", sa.BigInteger(), nullable=False),
        sa.Column("source", sa.String(length=8), nullable=False),
        sa.Column("co2", sa.SmallInteger(), nullable=True),
        sa.Column("temperature", sa.Float(), nullable=True),
        sa.Column("humidity", sa.Float(), nullable=True),
        sa.Column("pressure", sa.Float(), nullable=True),
        sa.Column("battery", sa.SmallInteger(), nullable=True),
        sa.Column("status", sa.SmallInteger(), nullable=True),
        sa.Column("rssi", sa.SmallInteger(), nullable=True),
        sa.Column("radon", sa.Integer(), nullable=True),
        sa.Column("radiation_rate", sa.Float(), nullable=True),
        sa.Column("radiation_total", sa.Float(), nullable=True),
        sa.ForeignKeyConstraint(["device_id"], ["devices.id"]),
        sa.PrimaryKeyConstraint("device_id", "ts", name="pk_readings"),
        sqlite_with_rowid=False,
    )
    op.create_table(
        "readings_hourly",
        sa.Column("device_id", sa.Integer(), nullable=False),
        sa.Column("hour", sa.BigInteger(), nullable=False),
        sa.Column("n", sa.Integer(), nullable=False),
        sa.Column("co2_sum", sa.Float(), nullable=True),
        sa.Column("co2_cnt", sa.Integer(), nullable=True),
        sa.Column("co2_min", sa.Float(), nullable=True),
        sa.Column("co2_max", sa.Float(), nullable=True),
        sa.Column("temperature_sum", sa.Float(), nullable=True),
        sa.Column("temperature_cnt", sa.Integer(), nullable=True),
        sa.Column("temperature_min", sa.Float(), nullable=True),
        sa.Column("temperature_max", sa.Float(), nullable=True),
        sa.Column("humidity_sum", sa.Float(), nullable=True),
        sa.Column("humidity_cnt", sa.Integer(), nullable=True),
        sa.Column("humidity_min", sa.Float(), nullable=True),
        sa.Column("humidity_max", sa.Float(), nullable=True),
        sa.Column("pressure_sum", sa.Float(), nullable=True),
        sa.Column("pressure_cnt", sa.Integer(), nullable=True),
        sa.Column("pressure_min", sa.Float(), nullable=True),
        sa.Column("pressure_max", sa.Float(), nullable=True),
        sa.Column("battery_sum", sa.Float(), nullable=True),
        sa.Column("battery_cnt", sa.Integer(), nullable=True),
        sa.Column("battery_min", sa.Float(), nullable=True),
        sa.Column("battery_max", sa.Float(), nullable=True),
        sa.Column("radon_sum", sa.Float(), nullable=True),
        sa.Column("radon_cnt", sa.Integer(), nullable=True),
        sa.Column("radon_min", sa.Float(), nullable=True),
        sa.Column("radon_max", sa.Float(), nullable=True),
        sa.Column("radiation_rate_sum", sa.Float(), nullable=True),
        sa.Column("radiation_rate_cnt", sa.Integer(), nullable=True),
        sa.Column("radiation_rate_min", sa.Float(), nullable=True),
        sa.Column("radiation_rate_max", sa.Float(), nullable=True),
        sa.Column("co2_ge_800", sa.Integer(), nullable=True),
        sa.Column("co2_ge_1000", sa.Integer(), nullable=True),
        sa.Column("co2_ge_1200", sa.Integer(), nullable=True),
        sa.Column("co2_ge_1400", sa.Integer(), nullable=True),
        sa.Column("co2_ge_1500", sa.Integer(), nullable=True),
        sa.Column("co2_ge_2000", sa.Integer(), nullable=True),
        sa.ForeignKeyConstraint(["device_id"], ["devices.id"]),
        sa.PrimaryKeyConstraint("device_id", "hour", name="pk_readings_hourly"),
        sqlite_with_rowid=False,
    )


def downgrade() -> None:
    """Drop every table (all data is lost)."""
    op.drop_table("readings_hourly")
    op.drop_table("readings")
    op.drop_table("collector_events")  # drops ix_events_ts with it
    op.drop_table("devices")
