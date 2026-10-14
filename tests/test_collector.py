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

    def emit(self, raw=REAL_ADVERT, name="Aranet4 12345", rssi=-70):
        dev = SimpleNamespace(address=ADDR, name=name)
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

    async def fake_history(target, since, timeout):
        calls.append(since)
        return HistoryResult(DeviceInfo(ADDR, "Aranet4 12345", interval_s=60), [], 2016, 0)

    c = Collector(Settings(backfill_on_start=True), store, FakeScanner, fake_history)
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

    async def fake_history(target, since, timeout):
        # 10 older records + one that duplicates the advert-derived latest point (+1 s skew).
        pts = [Reading(ADDR, now - 5 - 60 * k + 1, "history", co2=700 + k) for k in range(11)]
        return HistoryResult(DeviceInfo(ADDR, "Aranet4 12345", interval_s=60), pts, 2016, 1)

    c = Collector(Settings(), store, FakeScanner, fake_history)
    asyncio.run(drive(c, [lambda s: s.emit(with_counter(1, ago=5))], settle=0.5))
    assert n_rows(engine) == 11  # 1 advert + 10 history (the duplicate was skipped)
    assert c.stats.backfilled == 10
    # Scanner was restarted after the GATT session.
    assert FakeScanner.instances[-1].running is False  # stopped by shutdown
    assert len(FakeScanner.instances) >= 2


def test_failed_backfill_is_logged_and_cools_down(store, engine):
    FakeScanner.instances.clear()

    async def failing(target, since, timeout):
        raise TimeoutError("not paired")

    c = Collector(Settings(), store, FakeScanner, failing)
    asyncio.run(drive(c, [lambda s: s.emit(with_counter(1))], settle=0.3))
    assert n_rows(engine) == 1  # passive data still recorded
    with engine.connect() as conn:
        kinds = conn.execute(select(events.c.kind)).scalars().all()
    assert "backfill_error" in kinds
    st = next(iter(c._devices.values()))
    assert st.backfill_blocked_until > time.monotonic()


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
