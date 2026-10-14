import asyncio
import time
from types import SimpleNamespace

from sqlalchemy import func, select

from aranet_monitor.ble.collector import Collector
from aranet_monitor.ble.history import HistoryResult, first_index_after, log_times
from aranet_monitor.config import Settings
from aranet_monitor.db.schema import events, readings
from aranet_monitor.models import DeviceInfo, Reading
from conftest import REAL_ADVERT

ADDR = "AA:BB:CC:00:11:22"


class FakeScanner:
    instances = []

    def __init__(self, detection_callback):
        self.cb = detection_callback
        self.running = False
        FakeScanner.instances.append(self)

    async def start(self):
        self.running = True

    async def stop(self):
        self.running = False

    def emit(self, raw=REAL_ADVERT, name="Aranet4 12345", rssi=-70, address=ADDR):
        dev = SimpleNamespace(address=address, name=name)
        ad = SimpleNamespace(manufacturer_data={0x0702: raw}, rssi=rssi, local_name=name)
        self.cb(dev, ad)


def with_counter(counter, ago=5, co2=831):
    raw = bytearray(REAL_ADVERT)
    raw[21] = counter
    raw[19:21] = ago.to_bytes(2, "little")
    raw[8:10] = co2.to_bytes(2, "little")
    return bytes(raw)


def n_rows(engine):
    with engine.connect() as c:
        return c.execute(select(func.count()).select_from(readings)).scalar()


async def drive(collector, actions, settle=0.3):
    task = asyncio.create_task(collector.run())
    await asyncio.sleep(0.05)
    for act in actions:
        act(FakeScanner.instances[-1])
        await asyncio.sleep(0.05)
    await asyncio.sleep(settle)
    collector.stop()
    await task


def test_collector_stores_one_row_per_measurement(store, engine):
    FakeScanner.instances.clear()
    calls = []

    async def fake_history(target, since, timeout, **kw):
        calls.append(since)
        return HistoryResult(DeviceInfo(ADDR, "Aranet4 12345", interval_s=60), [], 2016, 0)

    c = Collector(
        Settings(devices=(ADDR,), backfill_on_start=True), store, FakeScanner, fake_history
    )
    # Same counter repeated (same measurement re-advertised) -> 1 row; new counter -> new row.
    asyncio.run(
        drive(
            c,
            [
                lambda s: s.emit(with_counter(10, ago=5)),
                lambda s: s.emit(with_counter(10, ago=15)),
                lambda s: s.emit(with_counter(10, ago=25)),
            ],
        )
    )
    assert n_rows(engine) == 1
    assert calls == [None]  # empty DB -> one startup backfill of everything
    assert c.stats.adverts == 3 and c.stats.stored == 1


def test_backfill_inserts_history_and_dedupes(store, engine):
    FakeScanner.instances.clear()
    now = int(time.time())

    async def fake_history(target, since, timeout, **kw):
        # 10 older records + one that duplicates the advert-derived latest point (+1 s skew).
        pts = [Reading(ADDR, now - 5 - 60 * k + 1, "history", co2=700 + k) for k in range(11)]
        return HistoryResult(DeviceInfo(ADDR, "Aranet4 12345", interval_s=60), pts, 2016, 1)

    c = Collector(Settings(devices=(ADDR,)), store, FakeScanner, fake_history)
    asyncio.run(drive(c, [lambda s: s.emit(with_counter(1, ago=5))], settle=0.5))
    assert n_rows(engine) == 11  # 1 advert + 10 history (the duplicate was skipped)
    assert c.stats.backfilled == 10
    # Scanner was restarted after the GATT session.
    assert FakeScanner.instances[-1].running is False  # stopped by shutdown
    assert len(FakeScanner.instances) >= 2


def test_failed_backfill_is_logged_and_cools_down(store, engine):
    FakeScanner.instances.clear()

    async def failing(target, since, timeout, **kw):
        raise TimeoutError("not paired")

    c = Collector(Settings(devices=(ADDR,)), store, FakeScanner, failing)
    asyncio.run(drive(c, [lambda s: s.emit(with_counter(1))], settle=0.3))
    with engine.connect() as conn:
        assert n_rows(engine) == 1
    assert n_rows(engine) == 1  # passive data still recorded
    with engine.connect() as conn:
        kinds = conn.execute(select(events.c.kind)).scalars().all()
    assert "backfill_error" in kinds
    st = next(iter(c._devices.values()))
    assert st.backfill_pending and st.backfill_not_before > time.monotonic() + 800


def _collector(store, fetcher, **settings):
    FakeScanner.instances.clear()
    return Collector(Settings(**{"devices": (ADDR,), **settings}), store, FakeScanner, fetcher)


def test_failed_backfill_is_retried_after_backoff(store, engine):
    calls = []

    async def flaky(target, since, timeout, **kw):
        calls.append(since)
        if len(calls) == 1:
            raise TimeoutError("weak signal")
        return HistoryResult(DeviceInfo(ADDR, "Aranet4 12345", interval_s=60), [], 2016, 0)

    c = _collector(store, flaky)

    def expire_backoff(_s):
        next(iter(c._devices.values())).backfill_not_before = 0

    asyncio.run(
        drive(
            c,
            [
                lambda s: s.emit(with_counter(1, ago=5)),
                expire_backoff,
                lambda s: s.emit(with_counter(1, ago=15)),  # re-advert of the same point
            ],
            settle=0.3,
        )
    )
    assert calls == [None, None]
    st = next(iter(c._devices.values()))
    assert not st.backfill_pending and st.backfill_failures == 0


def test_runtime_gap_triggers_backfill_since_last_row(store, engine):
    calls = []

    async def fetch(target, since, timeout, **kw):
        calls.append(since)
        return HistoryResult(DeviceInfo(ADDR, "Aranet4 12345", interval_s=60), [], 2016, 0)

    c = _collector(store, fetch, backfill_on_start=False)
    asyncio.run(
        drive(
            c,
            [
                lambda s: s.emit(with_counter(1, ago=600)),  # measured 10 min ago
                lambda s: s.emit(with_counter(2, ago=5)),  # newest: ~595 s later -> gap
            ],
            settle=0.3,
        )
    )
    assert len(calls) == 1 and calls[0] is not None
    assert abs(calls[0] - (int(time.time()) - 600)) <= 2
    assert n_rows(engine) == 2


def test_constant_counter_does_not_drop_new_measurements(store, engine):
    c = _collector(store, None, backfill_on_start=False)
    asyncio.run(
        drive(
            c,
            [
                lambda s: s.emit(with_counter(7, ago=125)),
                lambda s: s.emit(with_counter(7, ago=65)),  # same counter, next measurement
                lambda s: s.emit(with_counter(7, ago=5)),
                lambda s: s.emit(with_counter(7, ago=15)),  # re-advert of the last one
            ],
        )
    )
    assert n_rows(engine) == 3


def test_shutdown_during_backfill_leaves_no_scanner_running(store, engine):
    async def slow(target, since, timeout, **kw):
        await asyncio.sleep(30)

    c = _collector(store, slow)
    asyncio.run(drive(c, [lambda s: s.emit(with_counter(1, ago=5))], settle=0.2))
    assert all(not s.running for s in FakeScanner.instances)
    assert not c._tasks


def test_allow_list_filters_other_devices(store, engine):
    FakeScanner.instances.clear()
    c = Collector(
        Settings(devices=("11:22:33:44:55:66",), backfill_on_start=False), store, FakeScanner
    )
    asyncio.run(drive(c, [lambda s: s.emit()]))
    assert n_rows(engine) == 0


def test_log_times_and_index():
    times = log_times(now=10_000, total=5, interval=60, ago=10)
    assert times == [9750, 9810, 9870, 9930, 9990]
    assert first_index_after(times, None, 29) == 1
    assert first_index_after(times, 9870, 29) == 4
    assert first_index_after(times, 9990, 29) == 0


def test_unlisted_new_device_is_not_backfilled(store, engine):
    FakeScanner.instances.clear()
    calls = []

    async def fake_history(target, since, timeout, **kw):
        calls.append(target)
        return HistoryResult(DeviceInfo(ADDR, "x", interval_s=60), [], 0, 0)

    c = Collector(Settings(), store, FakeScanner, fake_history)
    asyncio.run(drive(c, [lambda s: s.emit(with_counter(1, ago=5))]))
    assert n_rows(engine) == 1 and calls == []  # stored, but no GATT connection


def test_unlisted_known_device_is_backfilled(store, engine):
    FakeScanner.instances.clear()
    dev = store.upsert_device(DeviceInfo(ADDR, "Aranet4 12345", interval_s=60))
    old = int(time.time()) - 3600
    store.insert_readings(dev, [Reading(ADDR, old, "adv", co2=500)], 60)
    calls = []

    async def fake_history(target, since, timeout, **kw):
        calls.append(since)
        return HistoryResult(DeviceInfo(ADDR, "x", interval_s=60), [], 0, 0)

    c = Collector(Settings(), store, FakeScanner, fake_history)
    asyncio.run(drive(c, [lambda s: s.emit(with_counter(1, ago=5))]))
    assert calls == [old]


def test_unlisted_device_cap(store, engine, monkeypatch):
    FakeScanner.instances.clear()
    monkeypatch.setattr(Collector, "MAX_UNLISTED_DEVICES", 2)
    c = Collector(Settings(backfill_on_start=False), store, FakeScanner)
    addrs = [f"AA:BB:CC:00:00:{i:02X}" for i in range(4)]
    asyncio.run(drive(c, [lambda s, a=a: s.emit(address=a) for a in addrs]))
    assert len(c._devices) == 2


def test_implausible_advert_is_dropped(store, engine):
    FakeScanner.instances.clear()
    c = Collector(Settings(backfill_on_start=False), store, FakeScanner)
    asyncio.run(drive(c, [lambda s: s.emit(with_counter(1, ago=5, co2=20000))]))
    assert n_rows(engine) == 0 and not c._devices


def test_handler_errors_are_rate_limited(store, engine, caplog):
    FakeScanner.instances.clear()
    c = Collector(Settings(backfill_on_start=False), store, FakeScanner)

    async def boom(*a, **kw):
        raise RuntimeError("db down")

    c._handle = boom
    asyncio.run(drive(c, [lambda s: s.emit() for _ in range(5)]))
    assert sum("failed to handle advert" in r.getMessage() for r in caplog.records) == 1
    assert c._suppressed_errors == 4
