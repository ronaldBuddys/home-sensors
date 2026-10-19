"""Maintenance of the ``readings_hourly`` rollup table."""

from __future__ import annotations

from sqlalchemy import Connection, case, func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from aranet_monitor.db.schema import CO2_LEVELS, ROLLUP_METRICS, readings, readings_hourly

HOUR = 3600


def _rollup_select(device_id: int, lo: int, hi: int):
    """SELECT producing rollup rows for hours overlapping [lo, hi]."""
    r = readings.c
    hour = (r.ts // HOUR) * HOUR
    cols = [r.device_id, hour.label("hour"), func.count().label("n")]
    for m in ROLLUP_METRICS:
        cols += [
            func.sum(r[m]).label(f"{m}_sum"),
            func.count(r[m]).label(f"{m}_cnt"),
            func.min(r[m]).label(f"{m}_min"),
            func.max(r[m]).label(f"{m}_max"),
        ]
    cols += [
        func.sum(case((r.co2 >= lvl, 1), else_=0)).label(f"co2_ge_{lvl}") for lvl in CO2_LEVELS
    ]
    h0 = (lo // HOUR) * HOUR
    h1 = (hi // HOUR) * HOUR + HOUR
    return (
        select(*cols)
        .where(r.device_id == device_id, r.ts >= h0, r.ts < h1)
        .group_by(r.device_id, hour)
    )


def refresh_rollup(conn: Connection, device_id: int, lo: int, hi: int) -> None:
    """Recompute (upsert) every hourly bucket touching ``[lo, hi]`` from raw readings."""
    sel = _rollup_select(device_id, lo, hi)
    names = [c.name for c in readings_hourly.columns]
    ins = pg_insert if conn.dialect.name == "postgresql" else sqlite_insert
    stmt = ins(readings_hourly).from_select(names, sel)
    stmt = stmt.on_conflict_do_update(
        index_elements=["device_id", "hour"],
        set_={n: stmt.excluded[n] for n in names if n not in ("device_id", "hour")},
    )
    conn.execute(stmt)


def rebuild_rollup(
    conn: Connection, device_id: int | None = None, chunk_s: int = 30 * 86400
) -> int:
    """Rebuild the rollup for one or all devices in month-sized chunks; returns hours written."""
    r = readings.c
    q = select(r.device_id, func.min(r.ts), func.max(r.ts)).group_by(r.device_id)
    if device_id is not None:
        q = q.where(r.device_id == device_id)
    hours = 0
    for dev, lo, hi in conn.execute(q).all():
        conn.execute(readings_hourly.delete().where(readings_hourly.c.device_id == dev))
        start = lo
        while start <= hi:
            end = min(hi, start + chunk_s - 1)
            refresh_rollup(conn, dev, start, end)
            start = end + 1
        hours += conn.execute(
            select(func.count())
            .select_from(readings_hourly)
            .where(readings_hourly.c.device_id == dev)
        ).scalar()
    return hours
