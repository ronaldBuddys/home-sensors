"""``aranet`` command-line entry point."""

from __future__ import annotations

import argparse
import asyncio
import csv
import json
import logging
import sys
import time
from datetime import datetime

from aranet_monitor.config import Settings
from aranet_monitor.db.store import Store, init_db, make_engine
from aranet_monitor.models import METRICS, DeviceInfo

log = logging.getLogger("aranet")


def _setup_logging(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    if not verbose:
        for noisy in ("bleak", "httpx", "uvicorn.access"):
            logging.getLogger(noisy).setLevel(logging.WARNING)


def _store(settings: Settings, db_url: str | None = None) -> Store:
    engine = make_engine(db_url or settings.db_url, schema=None if db_url else settings.db_schema)
    init_db(engine)
    return Store(engine)


def _parse_time(value: str | None, default: int) -> int:
    if not value:
        return default
    if value.isdigit():
        return int(value)
    return int(datetime.fromisoformat(value).timestamp())


# ---------------------------------------------------------------------- commands
async def _scan(seconds: float) -> None:
    from bleak import BleakScanner

    from aranet_monitor.ble.parse import MANUFACTURER_ID, parse_advert

    seen: dict[str, object] = {}

    def cb(device, ad):
        raw = ad.manufacturer_data.get(MANUFACTURER_ID)
        if raw is not None:
            seen[device.address] = parse_advert(
                device.address, device.name or ad.local_name, bytes(raw), ad.rssi
            )

    async with BleakScanner(detection_callback=cb):
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
    asyncio.run(_scan(args.seconds))
    return 0


def cmd_collect(args, settings: Settings) -> int:
    """Run the continuous collector."""
    from aranet_monitor.ble.collector import Collector

    store = _store(settings)
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
    import uvicorn

    from aranet_monitor.api.app import create_app

    host = args.host or settings.web_host
    port = args.port or settings.web_port
    log.info(
        "dashboard on http://%s:%s/  (db %s%s)",
        host,
        port,
        _safe_url(settings.db_url),
        f", schema {settings.db_schema}" if settings.db_schema else "",
    )
    uvicorn.run(create_app(settings), host=host, port=port, log_level="warning")
    return 0


def cmd_simulate(args, settings: Settings) -> int:
    """Write live synthetic readings (for UI development without a sensor)."""
    from aranet_monitor.sim.simulator import SIM_NAME, Aranet4Simulator

    store = _store(settings, args.db)
    sim = Aranet4Simulator(interval_s=args.interval)
    dev = store.upsert_device(DeviceInfo(sim.address, SIM_NAME, "Aranet4", "sim", args.interval))
    now = int(time.time())
    last = store.last_ts(dev)
    if last is None:
        n = store.bulk_insert_raw(dev, sim.generate(now - int(args.history_days * 86400), now))
        log.info("seeded %d simulated rows", n)
    log.info("simulating %s every %ss (Ctrl-C to stop)", SIM_NAME, args.interval)
    try:
        while True:
            ts = int(time.time())
            store.insert_readings(dev, [sim.step(ts)], args.interval)
            time.sleep(args.interval)
    except KeyboardInterrupt:
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
    from aranet_monitor.db.queries import Queries

    store = _store(settings)
    q = Queries(store.engine)
    device_id = args.device
    if device_id is None:
        devs = q.list_devices()
        if not devs:
            print("no devices", file=sys.stderr)
            return 1
        device_id = devs[0]["id"]
    end = _parse_time(args.end, int(time.time()))
    start = _parse_time(args.start, 0)
    w = csv.writer(sys.stdout)
    w.writerow(["time_utc", "ts", "source", *METRICS])
    for row in q.iter_rows(device_id, start, end):
        iso = datetime.fromtimestamp(row[0], tz=None).astimezone().isoformat()
        w.writerow([iso, *row])
    return 0


def cmd_status(args, settings: Settings) -> int:
    """Print data freshness and recent collector events."""
    from aranet_monitor.db.queries import Queries

    q = Queries(_store(settings).engine)
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


def cmd_db_init(args, settings: Settings) -> int:
    """Create the schema."""
    _store(settings)
    print(f"schema ready at {_safe_url(settings.db_url)}")
    return 0


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
    s.set_defaults(func=cmd_serve)

    s = sub.add_parser("simulate", help="write live synthetic readings")
    s.add_argument("--db", help="DB URL (default: ARANET_DB_URL). Use a separate DB!")
    s.add_argument("--interval", type=int, default=60)
    s.add_argument("--history-days", type=float, default=7)
    s.set_defaults(func=cmd_simulate)

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

    s = sub.add_parser("db-init", help="create the database schema")
    s.set_defaults(func=cmd_db_init)
    return p


def main(argv: list[str] | None = None) -> int:
    """Entry point."""
    args = build_parser().parse_args(argv)
    _setup_logging(args.verbose)
    settings = Settings.from_env()
    return args.func(args, settings) or 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
