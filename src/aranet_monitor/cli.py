"""``aranet`` command-line entry point."""

from __future__ import annotations

import argparse
import asyncio
import csv
import dataclasses
import json
import logging
import sys
import threading
import time
from datetime import datetime

from aranet_monitor.config import Settings
from aranet_monitor.db.store import Store, check_schema, db_revision, init_db, make_engine
from aranet_monitor.models import DeviceInfo

log = logging.getLogger("aranet")

DEMO_DB_URL = "sqlite:///data/demo.sqlite"


def _setup_logging(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    if not verbose:
        for noisy in ("alembic", "bleak", "uvicorn.access"):  # init_db logs migrations
            logging.getLogger(noisy).setLevel(logging.WARNING)


def _store(settings: Settings, db_url: str | None = None) -> Store:
    """Writer access: creates missing tables (needs CREATE on the schema)."""
    engine = make_engine(db_url or settings.db_url, schema=None if db_url else settings.db_schema)
    init_db(engine)
    return Store(engine)


def _open_store_with_retry(settings: Settings, *, max_wait_s: float | None = None, sleep=None):
    """Writer access that waits for an unreachable database (collector start-up).

    Connection-type errors are logged as one line (no traceback) and retried with back-off
    (2 s doubling to 60 s); anything else, such as a programming or permission error, raises
    at once. ``max_wait_s`` bounds the total wait (None: wait forever, systemd owns the
    lifecycle). ``init_db`` is idempotent, so a retry after a half-finished attempt is safe.
    """
    from sqlalchemy.exc import InterfaceError, OperationalError

    sleep = sleep or time.sleep
    delay, waited, attempts = 2, 0, 0
    while True:
        attempts += 1
        try:
            store = _store(settings)
        except (OperationalError, InterfaceError) as e:
            if max_wait_s is not None and waited + delay > max_wait_s:
                raise
            reason = (str(e.orig or e).splitlines() or [""])[0]
            log.warning(
                "database not reachable (%s: %s); retrying in %ds",
                type(e).__name__,
                reason.replace(settings.db_url, _safe_url(settings.db_url)),
                delay,
            )
            sleep(delay)
            waited += delay
            delay = min(delay * 2, 60)
            continue
        if attempts > 1:
            log.info("database reachable after %d attempt(s)", attempts)
        return store


def _queries(settings: Settings):
    """Read-only access: no DDL, fails clearly if the schema is missing."""
    from aranet_monitor.db.queries import Queries

    engine = make_engine(settings.db_url, schema=settings.db_schema)
    check_schema(engine)
    return Queries(engine)


def _parse_time(value: str | None, default: int) -> int:
    if not value:
        return default
    if value.isdigit():
        return int(value)
    return int(datetime.fromisoformat(value).timestamp())


# ---------------------------------------------------------------------- commands
async def _scan(seconds: float, adapter: str | None = None) -> None:
    from bleak import BleakScanner

    from aranet_monitor.ble.adapter import bleak_kwargs
    from aranet_monitor.ble.parse import MANUFACTURER_ID, parse_advert

    seen: dict[str, object] = {}

    def cb(device, ad):
        raw = ad.manufacturer_data.get(MANUFACTURER_ID)
        if raw is not None:
            seen[device.address] = parse_advert(
                device.address, device.name or ad.local_name, bytes(raw), ad.rssi
            )

    async with BleakScanner(detection_callback=cb, **bleak_kwargs(adapter)):
        await asyncio.sleep(seconds)
    if not seen:
        print("No Aranet devices heard. Is the sensor in range and Bluetooth on?")
        return
    for addr, p in seen.items():
        if p is None:
            print(f"{addr}  (unparseable advert)")
            continue
        d = p.device
        print(f"{addr}  {d.name}  model={d.model}  fw={d.firmware}  interval={d.interval_s}s")
        if p.reading is None:
            print("    no readings in advert -> enable 'Smart Home integrations' in Aranet Home")
        else:
            print(
                "    " + json.dumps({k: v for k, v in p.reading.values().items() if v is not None})
            )


def cmd_scan(args, settings: Settings) -> int:
    """List nearby Aranet devices and their advertised readings."""
    asyncio.run(_scan(args.seconds, settings.ble_adapter))
    return 0


def cmd_collect(args, settings: Settings) -> int:
    """Run the continuous collector."""
    from aranet_monitor.ble.collector import Collector

    try:
        store = _open_store_with_retry(settings)
    except KeyboardInterrupt:
        return 0
    log.info("collector writing to %s", _safe_url(settings.db_url))
    if settings.devices:
        log.info("device allow-list: %s", ", ".join(settings.devices))
    asyncio.run(Collector(settings, store).run())
    return 0


def cmd_backfill(args, settings: Settings) -> int:
    """Download on-device history for one address and store it."""
    from aranet_monitor.ble.history import fetch_history

    store = _store(settings)
    address = args.address.upper()
    device_id = store.device_id(address)
    if args.all or device_id is None:
        since = None
    else:
        # Fill every hole the device can still provide (it keeps ~5040 points = 3.5 days at
        # 60 s): start at the first gap within that horizon, not just after the newest row.
        interval = store.interval_for(address)
        horizon = int(time.time()) - 5040 * interval
        since = store.first_gap_start(device_id, interval, since=horizon)
        if since is None or since < horizon:
            since = None
    t0 = time.monotonic()
    stored = {"n": 0, "device_id": device_id}

    def save(info, chunk):  # progress is kept even if a later chunk fails
        if stored["device_id"] is None:
            stored["device_id"] = store.upsert_device(info)
        stored["n"] += store.insert_readings(stored["device_id"], chunk, info.interval_s)
        print(f"  ...stored {stored['n']} new rows so far", flush=True)

    try:
        result = asyncio.run(
            fetch_history(
                address,
                since,
                timeout=settings.ble_timeout_s,
                max_records=args.last,
                on_chunk=save,
                adapter=settings.ble_adapter,
            )
        )
    except Exception as exc:
        msg = f"backfill failed after storing {stored['n']} new rows: {exc!r}"
        if stored["device_id"] is not None:
            store.log_event("backfill_error", msg + " (cli)", stored["device_id"])
        print(msg, file=sys.stderr)
        print("Re-run the same command to continue; stored rows are skipped.", file=sys.stderr)
        return 1
    device_id = store.upsert_device(result.device)
    n = stored["n"]
    msg = (
        f"{n} new rows of {len(result.readings)} fetched; device holds "
        f"{result.total_on_device} records; {time.monotonic() - t0:.1f}s"
    )
    store.log_event("backfill", msg + " (cli)", device_id)
    print(msg)
    return 0


def cmd_serve(args, settings: Settings) -> int:
    """Serve the FastAPI app (API + Dash dashboard)."""
    host = args.host or settings.web_host
    port = args.port or settings.web_port
    if not settings.web_password and not _is_loopback(host) and not args.allow_no_auth:
        print(
            f"refusing to serve on {host} without ARANET_WEB_PASSWORD "
            "(set a password, bind to 127.0.0.1, or pass --allow-no-auth)",
            file=sys.stderr,
        )
        return 2
    if bool(settings.web_tls_cert) != bool(settings.web_tls_key):
        print("set both ARANET_WEB_TLS_CERT and ARANET_WEB_TLS_KEY (or neither)", file=sys.stderr)
        return 2
    tls = bool(settings.web_tls_cert)
    if not tls and not _is_loopback(host):
        log.warning(
            "serving on %s over plain HTTP: the password crosses the network unencrypted "
            "(set ARANET_WEB_TLS_CERT/ARANET_WEB_TLS_KEY, see docs/DEPLOYMENT.md)",
            host,
        )
    log.info(
        "dashboard on %s://%s:%s/  (db %s%s)",
        "https" if tls else "http",
        host,
        port,
        _safe_url(settings.web_db_url),
        f", schema {settings.db_schema}" if settings.db_schema else "",
    )
    _run_web(settings, host, port)
    return 0


def _run_web(settings: Settings, host: str, port: int) -> None:
    """Run the FastAPI app (API + dashboard) under uvicorn until interrupted."""
    import uvicorn

    from aranet_monitor.api.app import create_app

    uvicorn.run(
        create_app(settings),
        host=host,
        port=port,
        log_level="warning",
        ssl_certfile=settings.web_tls_cert,
        ssl_keyfile=settings.web_tls_key,
    )


def _sim_device(store: Store, interval_s: int, history_days: float):
    """Upsert the simulated device; seed it, or top it up if its data is stale."""
    from aranet_monitor.sim.simulator import SIM_NAME, Aranet4Simulator

    sim = Aranet4Simulator(interval_s=interval_s)
    dev = store.upsert_device(DeviceInfo(sim.address, SIM_NAME, "Aranet4", "sim", interval_s))
    now = int(time.time())
    last = store.last_ts(dev)
    if last is None:
        n = store.bulk_insert_raw(dev, sim.generate(now - int(history_days * 86400), now))
        log.info("seeded %d simulated rows", n)
    elif now - last > 2 * interval_s:
        n = store.bulk_insert_raw(dev, sim.generate(last + interval_s, now))
        log.info("topped up %d simulated rows", n)
    return dev, sim


def cmd_simulate(args, settings: Settings) -> int:
    """Write live synthetic readings (for UI development without a sensor)."""
    from aranet_monitor.sim.simulator import SIM_NAME

    store = _store(settings, args.db)
    dev, sim = _sim_device(store, args.interval, args.history_days)
    log.info("simulating %s every %ss (Ctrl-C to stop)", SIM_NAME, args.interval)
    try:
        while True:
            ts = int(time.time())
            store.insert_readings(dev, [sim.step(ts)], args.interval)
            time.sleep(args.interval)
    except KeyboardInterrupt:
        return 0


def cmd_demo(args, settings: Settings) -> int:
    """Try it without a sensor: simulated data written live + the dashboard."""
    url = args.db or DEMO_DB_URL
    demo = dataclasses.replace(
        settings,
        db_url=url,
        db_schema=None,
        web_db_user=None,
        web_db_password=None,
        web_tls_cert=None,
        web_tls_key=None,
        web_password=None,
    )
    port = args.port or settings.web_port
    store = _store(demo, url)
    dev, sim = _sim_device(store, args.interval, args.days)
    stop = threading.Event()

    def writer() -> None:
        while not stop.wait(args.interval):
            try:
                store.insert_readings(dev, [sim.step(int(time.time()))], args.interval)
            except Exception:
                log.exception("demo writer failed; will retry")

    thread = threading.Thread(target=writer, name="demo-writer", daemon=True)
    thread.start()
    print(
        f"Demo dashboard: http://127.0.0.1:{port}/  "
        f"(simulated sensor, data in {_safe_url(url)}; Ctrl-C to stop)"
    )
    try:
        _run_web(demo, "127.0.0.1", port)
    finally:
        stop.set()
        thread.join(timeout=2)
    return 0


def cmd_seed(args, settings: Settings) -> int:
    """Create a synthetic history (performance testing)."""
    from aranet_monitor.sim.simulator import SIM_NAME, Aranet4Simulator

    store = _store(settings, args.db)
    sim = Aranet4Simulator(interval_s=args.interval, seed=args.seed)
    dev = store.upsert_device(DeviceInfo(sim.address, SIM_NAME, "Aranet4", "sim", args.interval))
    end = int(time.time())
    t0 = time.monotonic()
    n = store.bulk_insert_raw(dev, sim.generate(end - int(args.days * 86400), end))
    print(f"inserted {n} rows in {time.monotonic() - t0:.1f}s")
    return 0


def cmd_export(args, settings: Settings) -> int:
    """Export raw readings as CSV to stdout."""
    q = _queries(settings)
    device_id = args.device
    if device_id is None:
        devs = q.list_devices()
        if not devs:
            print("no devices", file=sys.stderr)
            return 1
        device_id = devs[0]["id"]
    end = _parse_time(args.end, int(time.time()))
    start = _parse_time(args.start, 0)
    csv.writer(sys.stdout).writerows(q.iter_csv(device_id, start, end))
    return 0


def cmd_status(args, settings: Settings) -> int:
    """Print data freshness and recent collector events."""
    q = _queries(settings)
    print(json.dumps(q.health(), indent=2, default=str))
    return 0


def cmd_copy_db(args, settings: Settings) -> int:
    """Copy all data from one database to another (idempotent).

    Either side defaults to the configured database (``ARANET_DB_URL`` / ``POSTGRES_*`` and
    ``ARANET_DB_SCHEMA``), so ``aranet copy-db --source sqlite:///data/aranet.sqlite`` moves
    the local proof-of-concept data into the configured Postgres.
    """
    from aranet_monitor.db.store import copy_db

    if not args.source and not args.target:
        print(
            "give --source and/or --target (the other defaults to the configured DB)",
            file=sys.stderr,
        )
        return 2
    source = args.source or settings.db_url
    target = args.target or settings.db_url
    source_schema = None if args.source else settings.db_schema
    target_schema = args.target_schema if args.target else settings.db_schema
    if (source, source_schema) == (target, target_schema):
        print("source and target are the same", file=sys.stderr)
        return 2
    t0 = time.monotonic()
    counts = copy_db(source, target, source_schema=source_schema, target_schema=target_schema)
    where = _safe_url(target) + (f" (schema {target_schema})" if target_schema else "")
    print(
        f"copied {counts['devices']} device(s), {counts['readings']} new reading(s), "
        f"{counts['events']} event(s) to {where} in {time.monotonic() - t0:.1f}s"
    )
    return 0


def cmd_rollup(args, settings: Settings) -> int:
    """Rebuild the hourly rollup table from raw readings."""
    store = _store(settings)
    t0 = time.monotonic()
    hours = store.rebuild_rollup()
    print(f"rebuilt {hours} hourly rows in {time.monotonic() - t0:.1f}s")
    return 0


def cmd_grant_read(args, settings: Settings) -> int:
    """Give an existing Postgres role read-only access to the app's tables (now and future)."""
    import re

    from sqlalchemy import text

    engine = make_engine(settings.db_url)  # as the app role, which owns the tables
    if engine.dialect.name != "postgresql":
        print("grant-read needs a Postgres database", file=sys.stderr)
        return 2
    if not re.fullmatch(r"[a-z_][a-z0-9_]{0,62}", args.role):
        print(f"invalid role name {args.role!r} (lowercase letters, digits, _)", file=sys.stderr)
        return 2
    quote = engine.dialect.identifier_preparer.quote  # identifiers can't be bound parameters
    schema, role = quote(settings.db_schema or "public"), quote(args.role)
    with engine.begin() as conn:
        for stmt in (
            f"GRANT USAGE ON SCHEMA {schema} TO {role}",
            f"GRANT SELECT ON ALL TABLES IN SCHEMA {schema} TO {role}",
            # tables the app role creates later (new features) become readable too
            f"ALTER DEFAULT PRIVILEGES IN SCHEMA {schema} GRANT SELECT ON TABLES TO {role}",
        ):
            conn.execute(text(stmt))
    print(f"granted read-only access on schema {schema} to {role}")
    return 0


def cmd_db_upgrade(args, settings: Settings) -> int:
    """Create the schema or migrate it to the newest revision (``db-init`` is an alias)."""
    engine = make_engine(settings.db_url, schema=settings.db_schema)
    before = db_revision(engine)
    init_db(engine)
    where = _safe_url(settings.db_url) + (
        f" (schema {settings.db_schema})" if settings.db_schema else ""
    )
    print(f"schema at {where}: {before or 'unversioned'} -> {db_revision(engine)}")
    return 0


def _is_loopback(host: str) -> bool:
    """True for ``localhost`` and loopback IP addresses."""
    import ipaddress

    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _safe_url(url: str) -> str:
    """Hide the password part of a DB URL for logging."""
    from sqlalchemy.engine import make_url

    try:
        return make_url(url).render_as_string(hide_password=True)
    except Exception:
        return "<db>"


# ---------------------------------------------------------------------- parser
def build_parser() -> argparse.ArgumentParser:
    """CLI definition."""
    p = argparse.ArgumentParser(prog="aranet", description="Aranet BLE logger and dashboard")
    p.add_argument("-v", "--verbose", action="store_true")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("scan", help="list nearby Aranet devices")
    s.add_argument("--seconds", type=float, default=12)
    s.set_defaults(func=cmd_scan)

    s = sub.add_parser("collect", help="run the continuous collector")
    s.set_defaults(func=cmd_collect)

    s = sub.add_parser("backfill", help="download on-device history into the DB")
    s.add_argument("address")
    s.add_argument(
        "--all",
        action="store_true",
        help="fetch everything on the device (default: from the first gap in stored data)",
    )
    s.add_argument("--last", type=int, default=None, help="only the newest N records")
    s.set_defaults(func=cmd_backfill)

    s = sub.add_parser("serve", help="run the API + dashboard")
    s.add_argument("--host")
    s.add_argument("--port", type=int)
    s.add_argument(
        "--allow-no-auth",
        action="store_true",
        help="allow a non-loopback bind without ARANET_WEB_PASSWORD",
    )
    s.set_defaults(func=cmd_serve)

    s = sub.add_parser("simulate", help="write live synthetic readings")
    s.add_argument("--db", help="DB URL (default: ARANET_DB_URL). Use a separate DB!")
    s.add_argument("--interval", type=int, default=60)
    s.add_argument("--history-days", type=float, default=7)
    s.set_defaults(func=cmd_simulate)

    s = sub.add_parser("demo", help="try it without a sensor: simulated data + dashboard")
    s.add_argument("--db", help=f"DB URL (default: {DEMO_DB_URL}; never ARANET_DB_URL)")
    s.add_argument("--days", type=float, default=30, help="days of history to seed")
    s.add_argument("--interval", type=int, default=60)
    s.add_argument("--port", type=int)
    s.set_defaults(func=cmd_demo)

    s = sub.add_parser("seed", help="generate synthetic history")
    s.add_argument("--db", help="DB URL (default: ARANET_DB_URL). Use a separate DB!")
    s.add_argument("--days", type=float, default=365)
    s.add_argument("--interval", type=int, default=60)
    s.add_argument("--seed", type=int, default=4)
    s.set_defaults(func=cmd_seed)

    s = sub.add_parser("export", help="export readings as CSV")
    s.add_argument("--device", type=int)
    s.add_argument("--start", help="ISO datetime or epoch seconds")
    s.add_argument("--end", help="ISO datetime or epoch seconds")
    s.set_defaults(func=cmd_export)

    s = sub.add_parser("status", help="show data freshness and recent events")
    s.set_defaults(func=cmd_status)

    s = sub.add_parser("copy-db", help="copy data between databases (e.g. SQLite -> Postgres)")
    s.add_argument("--source", help="DB URL to read (default: the configured DB)")
    s.add_argument("--target", help="DB URL to write (default: the configured DB)")
    s.add_argument("--target-schema", help="Postgres schema when --target is given")
    s.set_defaults(func=cmd_copy_db)

    s = sub.add_parser("rollup", help="rebuild the hourly rollup table")
    s.set_defaults(func=cmd_rollup)

    s = sub.add_parser("grant-read", help="give a Postgres role read-only access (dashboard)")
    s.add_argument("role", help="existing role, created by a DB admin")
    s.set_defaults(func=cmd_grant_read)

    s = sub.add_parser(
        "db-upgrade", aliases=["db-init"], help="create or migrate the database schema"
    )
    s.set_defaults(func=cmd_db_upgrade)
    return p


def main(argv: list[str] | None = None) -> int:
    """Entry point."""
    args = build_parser().parse_args(argv)
    _setup_logging(args.verbose)
    settings = Settings.from_env()
    return args.func(args, settings) or 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
