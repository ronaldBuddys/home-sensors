"""Engine creation and all write paths (devices, readings with dedupe, events)."""

from __future__ import annotations

import bisect
import logging
import time
from collections.abc import Iterable, Sequence
from pathlib import Path

from alembic import command
from alembic.config import Config
from alembic.migration import MigrationContext
from sqlalchemy import (
    Connection,
    Engine,
    create_engine,
    event,
    func,
    insert,
    inspect,
    select,
    update,
)
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.exc import DBAPIError

from aranet_monitor.config import ensure_sqlite_dir
from aranet_monitor.db.rollup import rebuild_rollup, refresh_rollup
from aranet_monitor.db.schema import devices, events, readings, readings_hourly
from aranet_monitor.models import METRICS, DeviceInfo, Reading

DEFAULT_INTERVAL_S = 60
COPY_CHUNK = 10_000
BASELINE_REVISION = "0001"  # the schema as ``metadata.create_all`` made it before migrations

log = logging.getLogger(__name__)


def make_engine(db_url: str, *, schema: str | None = None, read_only: bool = False) -> Engine:
    """Create an engine; SQLite gets WAL + busy timeout so collector and web can share it.

    ``schema`` (Postgres only) places every table in that schema instead of the default one.
    The schema must already exist and be writable by the connecting role. ``read_only``
    (Postgres only) makes every transaction read-only -- a guard against bugs; the real
    boundary is a role with only SELECT (``aranet grant-read``).
    """
    if db_url.startswith("sqlite"):
        ensure_sqlite_dir(db_url)
        engine = create_engine(db_url, connect_args={"timeout": 30})

        @event.listens_for(engine, "connect")
        def _sqlite_pragmas(dbapi_conn, _record):  # pragma: no cover - trivial
            cur = dbapi_conn.cursor()
            cur.execute("PRAGMA journal_mode=WAL")
            cur.execute("PRAGMA synchronous=NORMAL")
            cur.execute("PRAGMA busy_timeout=10000")
            cur.execute("PRAGMA foreign_keys=ON")
            cur.execute("PRAGMA temp_store=MEMORY")
            cur.execute("PRAGMA cache_size=-32000")  # ~32 MB page cache
            cur.close()

        return engine
    connect_args = {"options": "-c default_transaction_read_only=on"} if read_only else {}
    engine = create_engine(db_url, pool_pre_ping=True, connect_args=connect_args)
    if schema:
        # Every unqualified table (all of ours) is rendered as "<schema>.<table>".
        engine = engine.execution_options(schema_translate_map={None: schema})
    return engine


def schema_of(bind: Engine | Connection) -> str | None:
    """The Postgres schema ``make_engine`` maps unqualified tables to (None: the default)."""
    return bind.get_execution_options().get("schema_translate_map", {}).get(None)


def alembic_config(connection: Connection) -> Config:
    """Alembic configuration for the packaged migrations, run on ``connection``."""
    cfg = Config()
    cfg.set_main_option("script_location", str(Path(__file__).with_name("migrations")))
    cfg.attributes["connection"] = connection
    return cfg


def db_revision(engine: Engine) -> str | None:
    """Current migration revision of the database (None: empty or created before Alembic)."""
    with engine.connect() as conn:
        opts = {"version_table_schema": schema_of(conn)}
        return MigrationContext.configure(conn, opts=opts).get_current_revision()


def init_db(engine: Engine) -> None:
    """Migrate the schema to the newest revision and backfill the hourly rollup if empty.

    Idempotent. A database created before migrations existed (tables from ``create_all``,
    no ``alembic_version``) is checked and stamped at the baseline revision first, so it is
    upgraded in place without data loss or a manual step.
    """
    unversioned = db_revision(engine) is None
    legacy = unversioned and inspect(engine).has_table("devices", schema=schema_of(engine))
    if legacy:
        check_schema(engine)
    with engine.begin() as conn:
        cfg = alembic_config(conn)
        if legacy:
            log.info("stamping a database created before migrations at %s", BASELINE_REVISION)
            command.stamp(cfg, BASELINE_REVISION)
        command.upgrade(cfg, "head")
        has_raw = conn.execute(select(readings.c.ts).limit(1)).first() is not None
        has_rollup = conn.execute(select(readings_hourly.c.hour).limit(1)).first() is not None
        if has_raw and not has_rollup:
            rebuild_rollup(conn)


def check_schema(engine: Engine) -> None:
    """Fail fast if the tables are missing; read-only paths use this instead of DDL.

    Only writers (collector, ``db-init``, seeding, ``copy-db`` target) create tables, so the
    dashboard and read-only commands can run with a role that has no CREATE privilege.
    """
    try:
        with engine.connect() as conn:
            for table in (devices, readings, readings_hourly, events):
                conn.execute(select(table).limit(0))
    except DBAPIError as exc:
        raise RuntimeError(
            "database not reachable or schema missing; run `aranet db-init` "
            f"({type(exc.orig).__name__})"
        ) from exc


def _insert_ignore(engine: Engine, table):
    if engine.dialect.name == "postgresql":
        return pg_insert(table).on_conflict_do_nothing()
    if engine.dialect.name == "sqlite":
        return sqlite_insert(table).on_conflict_do_nothing()
    return insert(table)  # pragma: no cover


class Store:
    """Write-side facade over the database.

    Keeps a small cache of ``address -> (device_id, interval_s)`` so the hot path (one advert
    every few seconds) does not hit the ``devices`` table.
    """

    def __init__(self, engine: Engine):
        self.engine = engine
        self._device_cache: dict[str, tuple[int, int]] = {}

    # ------------------------------------------------------------------ devices
    def upsert_device(
        self, info: DeviceInfo, seen: int | None = None, rssi: int | None = None
    ) -> int:
        """Insert or refresh a device row and return its id."""
        seen = int(seen if seen is not None else time.time())
        address = info.address.upper()
        with self.engine.begin() as conn:
            row = conn.execute(
                select(devices.c.id, devices.c.interval_s).where(devices.c.address == address)
            ).first()
            changes = {"last_seen": seen}
            if rssi is not None:
                changes["last_rssi"] = rssi
            for col in ("name", "model", "firmware", "interval_s"):
                val = getattr(info, col)
                if val is not None:
                    changes[col] = val
            if row is None:
                res = conn.execute(
                    insert(devices).values(address=address, first_seen=seen, **changes)
                )
                device_id = int(res.inserted_primary_key[0])
            else:
                device_id = int(row.id)
                conn.execute(update(devices).where(devices.c.id == device_id).values(**changes))
        interval = info.interval_s or (row.interval_s if row is not None else None)
        self._device_cache[address] = (device_id, interval or DEFAULT_INTERVAL_S)
        return device_id

    def device_id(self, address: str) -> int | None:
        """Return the cached/stored id for an address, or None."""
        address = address.upper()
        if address in self._device_cache:
            return self._device_cache[address][0]
        with self.engine.connect() as conn:
            row = conn.execute(
                select(devices.c.id, devices.c.interval_s).where(devices.c.address == address)
            ).first()
        if row is None:
            return None
        self._device_cache[address] = (int(row.id), row.interval_s or DEFAULT_INTERVAL_S)
        return int(row.id)

    def interval_for(self, address: str) -> int:
        """Logging interval for a device (seconds), defaulting to 60."""
        self.device_id(address)
        return self._device_cache.get(address.upper(), (0, DEFAULT_INTERVAL_S))[1]

    # ------------------------------------------------------------------ readings
    def last_ts(self, device_id: int) -> int | None:
        """Most recent stored measurement time for a device."""
        with self.engine.connect() as conn:
            return conn.execute(
                select(readings.c.ts)
                .where(readings.c.device_id == device_id)
                .order_by(readings.c.ts.desc())
                .limit(1)
            ).scalar()

    def insert_readings(
        self, device_id: int, batch: Sequence[Reading], interval_s: int | None = None
    ) -> int:
        """Insert readings, skipping any within ±interval/2 of an existing row.

        Advert- and history-derived timestamps are both computed on the host as
        ``now - seconds_ago`` and jitter by a second or two, so an exact-match key alone would
        create near-duplicates. Rows are therefore deduplicated against a time *window*.

        Returns
        -------
        int
            Number of rows actually inserted.
        """
        if not batch:
            return 0
        interval_s = interval_s or DEFAULT_INTERVAL_S
        tol = max(1, interval_s // 2 - 1)
        candidates = sorted(batch, key=lambda r: r.ts)
        lo, hi = candidates[0].ts - tol, candidates[-1].ts + tol
        with self.engine.begin() as conn:
            existing = list(
                conn.execute(
                    select(readings.c.ts)
                    .where(readings.c.device_id == device_id)
                    .where(readings.c.ts.between(lo, hi))
                    .order_by(readings.c.ts)
                ).scalars()
            )
            rows = []
            for r in candidates:
                i = bisect.bisect_left(existing, r.ts - tol)
                if i < len(existing) and existing[i] <= r.ts + tol:
                    continue
                bisect.insort(existing, r.ts)
                rows.append({"device_id": device_id, "ts": r.ts, "source": r.source, **r.values()})
            if not rows:
                return 0
            stmt = _insert_ignore(self.engine, readings)
            # Chunk to stay below SQLite's bound-parameter limit on large backfills/seeds.
            for start in range(0, len(rows), 500):
                conn.execute(stmt, rows[start : start + 500])
            refresh_rollup(conn, device_id, rows[0]["ts"], rows[-1]["ts"])
            return len(rows)

    def bulk_insert_raw(self, device_id: int, batch: Iterable[Reading], chunk: int = 5000) -> int:
        """Fast path without window dedupe (used by the seeder on an empty range)."""
        stmt = _insert_ignore(self.engine, readings)
        n = 0
        lo = hi = None
        buf: list[dict] = []
        with self.engine.begin() as conn:
            for r in batch:
                buf.append({"device_id": device_id, "ts": r.ts, "source": r.source, **r.values()})
                lo = r.ts if lo is None else min(lo, r.ts)
                hi = r.ts if hi is None else max(hi, r.ts)
                if len(buf) >= chunk:
                    conn.execute(stmt, buf)
                    n += len(buf)
                    buf.clear()
            if buf:
                conn.execute(stmt, buf)
                n += len(buf)
            if lo is not None:
                step = 30 * 86400
                for start in range(lo, hi + 1, step):
                    refresh_rollup(conn, device_id, start, min(hi, start + step - 1))
        return n

    def rebuild_rollup(self) -> int:
        """Recompute the hourly rollup from raw readings; returns hours written."""
        with self.engine.begin() as conn:
            return rebuild_rollup(conn)

    def first_gap_start(
        self, device_id: int, interval_s: int, since: int | None = None
    ) -> int | None:
        """Newest stored ts just before the first gap (> 1.5 x interval), or the last ts.

        This is the right ``since`` for a history download that should fill *every* hole the
        device can still provide. ``since`` limits the search to rows at or after it.
        """
        r = readings.c
        nxt = func.lead(r.ts).over(order_by=r.ts).label("nxt")
        q = select(r.ts, nxt).where(r.device_id == device_id)
        if since is not None:
            q = q.where(r.ts >= since)
        sub = q.subquery()
        with self.engine.connect() as conn:
            gap = conn.execute(
                select(sub.c.ts)
                .where(sub.c.nxt - sub.c.ts > int(1.5 * interval_s))
                .order_by(sub.c.ts)
                .limit(1)
            ).scalar()
        return gap if gap is not None else self.last_ts(device_id)

    # ------------------------------------------------------------------ events
    def log_event(self, kind: str, detail: str = "", device_id: int | None = None) -> None:
        """Record a collector lifecycle event (start, backfill, gap, error, ...)."""
        with self.engine.begin() as conn:
            conn.execute(
                insert(events).values(
                    ts=int(time.time()), device_id=device_id, kind=kind, detail=detail[:2000]
                )
            )


def _count_rows(engine: Engine, device_id: int) -> int:
    with engine.connect() as conn:
        return conn.execute(
            select(func.count()).select_from(readings).where(readings.c.device_id == device_id)
        ).scalar()


def copy_db(
    source_url: str,
    target_url: str,
    source_schema: str | None = None,
    target_schema: str | None = None,
) -> dict[str, int]:
    """Copy devices, readings and events between databases (e.g. SQLite -> Postgres).

    Idempotent: readings are inserted with ON CONFLICT DO NOTHING on ``(device_id, ts)``,
    events newer than the target's newest event are appended, and the hourly rollup is
    refreshed for the copied range.
    """
    from aranet_monitor.db.queries import Queries

    src = make_engine(source_url, schema=source_schema)
    dst = make_engine(target_url, schema=target_schema)
    check_schema(src)  # never write (DDL) to the source
    init_db(dst)
    q, out = Queries(src), Store(dst)
    counts = {"devices": 0, "readings": 0, "events": 0}
    id_map: dict[int, int] = {}
    with src.connect() as conn:
        rows = conn.execute(select(devices)).mappings().all()
    for d in rows:
        info = DeviceInfo(d["address"], d["name"], d["model"], d["firmware"], d["interval_s"])
        new_id = out.upsert_device(info, seen=d["last_seen"], rssi=d["last_rssi"])
        with dst.begin() as conn:
            cur = conn.execute(select(devices.c.first_seen).where(devices.c.id == new_id)).scalar()
            if d["first_seen"] and (cur is None or d["first_seen"] < cur):
                conn.execute(
                    update(devices).where(devices.c.id == new_id).values(first_seen=d["first_seen"])
                )
        id_map[d["id"]] = new_id
        counts["devices"] += 1
        # Plain insert-or-ignore on the primary key: historical rows must not be window-
        # deduplicated with the device's *current* interval (it may have changed since).
        before = _count_rows(dst, new_id)
        rows = (
            Reading(d["address"], int(ts), source, **dict(zip(METRICS, vals, strict=True)))
            for ts, source, *vals in q.iter_rows(d["id"], 0, 2**62, chunk=COPY_CHUNK)
        )
        out.bulk_insert_raw(new_id, rows, chunk=COPY_CHUNK)
        counts["readings"] += _count_rows(dst, new_id) - before

    with dst.connect() as conn:
        newest = conn.execute(select(func.max(events.c.ts))).scalar() or -1
    with src.connect() as conn:
        evs = conn.execute(
            select(events).where(events.c.ts > newest).order_by(events.c.id)
        ).mappings()
        payload = [
            {
                "ts": e["ts"],
                "device_id": id_map.get(e["device_id"]),
                "kind": e["kind"],
                "detail": e["detail"],
            }
            for e in evs
        ]
    if payload:
        with dst.begin() as conn:
            conn.execute(insert(events), payload)
    counts["events"] = len(payload)
    return counts


__all__ = [
    "METRICS",
    "Store",
    "alembic_config",
    "check_schema",
    "copy_db",
    "db_revision",
    "init_db",
    "make_engine",
    "schema_of",
]
