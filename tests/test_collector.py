import asyncio
import time
from types import SimpleNamespace

import pytest
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

    def __init__(self, detection_callback, **kwargs):
        self.cb = detection_callback
        self.kwargs = kwargs
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

    def emit_other(self, address="11:22:33:44:55:66"):
        dev = SimpleNamespace(address=address, name=None)
        self.cb(dev, SimpleNamespace(manufacturer_data={}, rssi=-60, local_name=None))


def with_counter(counter, ago=5, co2=831):
    raw = bytearray(REAL_ADVERT)
    raw[21] = counter
    raw[19:21] = ago.to_bytes(2, "little")
    raw[8:10] = co2.to_bytes(2, "little")
    return bytes(raw)


def n_rows(engine):
    with engine.connect() as c:
        return c.execute(select(func.count()).select_from(readings)).scalar()


async def _wait_for_scanner(task, timeout=5.0):
    # The collector may do start-up work (e.g. backfill) before it creates the scanner.
    deadline = time.monotonic() + timeout
    while not (FakeScanner.instances and FakeScanner.instances[-1].running):
        if task.done():
            task.result()  # surface a collector crash instead of timing out
        assert time.monotonic() < deadline, "collector never started a scanner"
        await asyncio.sleep(0.01)


async def wait_until(predicate, what, timeout=5.0):
    # Bounded poll for a condition that the collector reaches asynchronously.
    deadline = time.monotonic() + timeout
    while not predicate():
        assert time.monotonic() < deadline, f"timed out waiting for {what}"
        await asyncio.sleep(0.01)


async def drive(collector, actions, settle=0.3):
    # An action may be a coroutine function to wait for collector state first.
    task = asyncio.create_task(collector.run())
    await _wait_for_scanner(task)
    for act in actions:
        res = act(FakeScanner.instances[-1])
        if asyncio.iscoroutine(res):
            await res
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

    async def expire_backoff(_s):
        # the device registers and the first backfill fails (setting the backoff) asynchronously
        await wait_until(
            lambda: any(d.backfill_failures for d in c._devices.values()),
            "the device to register and its first backfill to fail",
        )
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


def _event_kinds(engine):
    with engine.connect() as conn:
        return conn.execute(select(events.c.kind)).scalars().all()


def _clock_collector(store, probe, order=None, **settings):
    FakeScanner.instances.clear()

    def factory(**kw):
        if order is not None:
            order.append("scanner")
        return FakeScanner(**kw)

    return Collector(
        Settings(devices=(ADDR,), backfill_on_start=False, **settings),
        store,
        factory,
        clock_probe=probe,
    )


def test_waits_for_clock_sync_before_scanning(store, engine, monkeypatch):
    monkeypatch.setattr(Collector, "CLOCK_POLL_S", 0.01)
    results = iter([False, False, True])
    order = []

    def probe():
        order.append("probe")
        return next(results)

    c = _clock_collector(store, probe, order, wait_clock_sync_s=60)
    asyncio.run(drive(c, [lambda s: s.emit(with_counter(1))]))
    assert order == ["probe", "probe", "probe", "scanner"]
    kinds = _event_kinds(engine)
    assert "clock_wait" in kinds and "clock_synced" in kinds
    assert n_rows(engine) == 1


def test_clock_sync_timeout_proceeds(store, engine, monkeypatch):
    monkeypatch.setattr(Collector, "CLOCK_POLL_S", 0.01)
    c = _clock_collector(store, lambda: False, wait_clock_sync_s=1)
    asyncio.run(drive(c, [lambda s: s.emit(with_counter(1))]))
    assert "clock_unsynced" in _event_kinds(engine)
    assert n_rows(engine) == 1


def test_stop_during_clock_wait_ends_run(store, engine, monkeypatch):
    monkeypatch.setattr(Collector, "CLOCK_POLL_S", 0.01)
    c = _clock_collector(store, lambda: False, wait_clock_sync_s=60)

    async def go():
        task = asyncio.create_task(c.run())
        await asyncio.sleep(0.1)
        c.stop()
        await asyncio.wait_for(task, 2)

    asyncio.run(go())
    assert not FakeScanner.instances
    assert "stop" in _event_kinds(engine)


def test_clock_wait_is_bounded_by_the_limit(store, engine, monkeypatch):
    monkeypatch.setattr(Collector, "CLOCK_POLL_S", 0.7)
    c = _clock_collector(store, lambda: False, wait_clock_sync_s=1)

    async def go():
        t0 = time.monotonic()
        await c._wait_for_clock()
        return time.monotonic() - t0

    assert asyncio.run(go()) < 1.3
    assert "clock_unsynced" in _event_kinds(engine)


def test_unknown_clock_state_does_not_wait(store, engine):
    c = _clock_collector(store, lambda: None, wait_clock_sync_s=60)
    asyncio.run(drive(c, [lambda s: s.emit(with_counter(1))]))
    assert "clock_wait" not in _event_kinds(engine)
    assert n_rows(engine) == 1


def test_clock_wait_disabled_by_default(store, engine):
    def probe():
        raise AssertionError("probe must not be called when waiting is off")

    c = _clock_collector(store, probe)
    asyncio.run(drive(c, [lambda s: s.emit(with_counter(1))]))
    assert n_rows(engine) == 1


def test_clock_synchronised_marker(monkeypatch):
    import subprocess
    from pathlib import Path

    from aranet_monitor.ble import collector as mod

    monkeypatch.setattr(Path, "exists", lambda self: True)
    assert mod.clock_synchronised() is True

    monkeypatch.setattr(Path, "exists", lambda self: False)
    monkeypatch.setattr(mod.shutil, "which", lambda name: None)
    monkeypatch.setattr(
        mod.subprocess, "run", lambda *a, **k: pytest.fail("no subprocess expected")
    )
    assert mod.clock_synchronised() is None

    def fake_run(out):
        return lambda *a, **k: subprocess.CompletedProcess(a, 0, stdout=out, stderr="")

    monkeypatch.setattr(mod.shutil, "which", lambda name: "/usr/bin/timedatectl")
    monkeypatch.setattr(mod.subprocess, "run", fake_run("no\n"))
    assert mod.clock_synchronised() is False
    monkeypatch.setattr(mod.subprocess, "run", fake_run("yes\n"))
    assert mod.clock_synchronised() is True
    monkeypatch.setattr(mod.subprocess, "run", fake_run("weird\n"))
    assert mod.clock_synchronised() is None

    def boom(*a, **k):
        raise subprocess.TimeoutExpired("timedatectl", 5)

    monkeypatch.setattr(mod.subprocess, "run", boom)
    assert mod.clock_synchronised() is None


def _watchdog_collector(store, monkeypatch, watchdog_s, **settings_kw):
    FakeScanner.instances.clear()
    monkeypatch.setattr(Collector, "WATCHDOG_POLL_S", 0.02)
    monkeypatch.setattr(Collector, "SCANNER_RESTART_PAUSE_S", 0.0)
    resets = []

    async def reset(adapter):
        resets.append(adapter)

    settings = Settings(backfill_on_start=False, watchdog_s=watchdog_s, **settings_kw)
    return Collector(settings, store, FakeScanner, adapter_reset=reset), resets


def _events(engine, kind):
    with engine.connect() as c:
        return c.execute(
            select(func.count()).select_from(events).where(events.c.kind == kind)
        ).scalar()


def test_deaf_radio_gets_adapter_power_cycled(store, engine, monkeypatch):
    c, resets = _watchdog_collector(store, monkeypatch, watchdog_s=0.1)

    async def until_reset(_s):
        await wait_until(lambda: resets, "adapter power-cycle")

    asyncio.run(drive(c, [until_reset], settle=0.0))
    # one plain restart first, then the power-cycle, then a fresh scanner
    assert _events(engine, "scanner_restart") >= 1
    assert _events(engine, "adapter_reset") >= 1
    assert len(FakeScanner.instances) >= 3
    assert resets[0] is None  # no adapter configured: power-cycle the default one


def test_configured_adapter_is_used_for_scanning_and_reset(store, engine, monkeypatch):
    c, resets = _watchdog_collector(store, monkeypatch, watchdog_s=0.1, ble_adapter="hci1")

    async def until_reset(_s):
        await wait_until(lambda: resets, "adapter power-cycle")

    asyncio.run(drive(c, [until_reset], settle=0.0))
    assert resets[0] == "hci1"
    assert all(s.kwargs == {"bluez": {"adapter": "hci1"}} for s in FakeScanner.instances)


def test_adapter_reset_can_be_disabled(store, engine, monkeypatch):
    c, resets = _watchdog_collector(store, monkeypatch, watchdog_s=0.05, adapter_reset=False)

    async def wait_a_while(_s):
        await asyncio.sleep(0.6)

    asyncio.run(drive(c, [wait_a_while], settle=0.0))
    assert _events(engine, "scanner_restart") >= 2
    assert _events(engine, "adapter_reset") == 0
    assert not resets


def test_sensor_out_of_range_only_restarts_scanner(store, engine, monkeypatch):
    c, resets = _watchdog_collector(store, monkeypatch, watchdog_s=0.15)

    async def other_devices_keep_talking(_s):
        deadline = time.monotonic() + 1.0
        while time.monotonic() < deadline:
            FakeScanner.instances[-1].emit_other()
            await asyncio.sleep(0.02)

    asyncio.run(drive(c, [other_devices_keep_talking], settle=0.0))
    assert _events(engine, "scanner_restart") >= 2
    assert not resets


def test_failed_scanner_start_is_cleaned_up(store, engine, monkeypatch):
    """A start that times out must be stopped, or BlueZ reports InProgress from then on."""
    monkeypatch.setattr(Collector, "SCANNER_OP_TIMEOUT_S", 0.05)
    stopped = []

    class HangingScanner(FakeScanner):
        async def start(self):
            await asyncio.sleep(10)

        async def stop(self):
            stopped.append(self)

    c = Collector(Settings(backfill_on_start=False), store, HangingScanner)
    assert asyncio.run(c._start_scanner()) is False
    assert c._scanner is None
    assert stopped == [HangingScanner.instances[-1]]


def _scan_mode_collector(store, scan_mode="auto", supported=True, **settings_kw):
    FakeScanner.instances.clear()
    probed = []

    async def probe(adapter):
        probed.append(adapter)
        return supported

    settings = Settings(backfill_on_start=False, scan_mode=scan_mode, **settings_kw)
    return Collector(settings, store, FakeScanner, passive_probe=probe), probed


def test_auto_scan_mode_scans_passively_where_supported(store, engine):
    c, probed = _scan_mode_collector(store, ble_adapter="hci1")
    asyncio.run(drive(c, [lambda s: s.emit()]))
    assert probed == ["hci1"]
    kwargs = FakeScanner.instances[-1].kwargs
    assert kwargs["scanning_mode"] == "passive"
    assert kwargs["bluez"]["adapter"] == "hci1"
    patterns = kwargs["bluez"]["or_patterns"]
    assert (0, 0xFF, b"\x02\x07") in patterns  # Aranet (SAF Tehnika, 0x0702) manufacturer data
    assert n_rows(engine) == 1


def test_auto_scan_mode_falls_back_to_active(store, engine, caplog):
    c, _ = _scan_mode_collector(store, supported=False)
    asyncio.run(drive(c, [lambda s: s.emit()]))
    assert FakeScanner.instances[-1].kwargs == {}
    assert "Experimental = true" in caplog.text
    assert n_rows(engine) == 1


@pytest.mark.parametrize("mode", ["active", "passive"])
def test_explicit_scan_mode_skips_the_probe(store, engine, mode):
    c, probed = _scan_mode_collector(store, scan_mode=mode, supported=mode == "active")
    asyncio.run(drive(c, []))
    assert not probed
    assert ("scanning_mode" in FakeScanner.instances[-1].kwargs) == (mode == "passive")
