"""Continuous, battery-friendly collector.

Normal operation is a *passive* BLE scan: with "Smart Home integrations" enabled the sensor
broadcasts its latest measurement in every advertisement, so we never connect to it. A GATT
connection (which costs sensor battery) is only made to backfill history:

* once at start-up, if the newest stored row is older than ``gap_backfill_min_s``;
* when a device re-appears after being silent for longer than ``gap_backfill_min_s``.

The Aranet4 advert carries a rolling ``counter`` that increments once per logged measurement
(verified empirically), which we use as the cheap "is this new?" test before touching the DB.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import signal
import time
from collections.abc import Callable
from dataclasses import dataclass, field

from bleak import BleakScanner
from bleak.backends.device import BLEDevice

from aranet_monitor.ble.history import fetch_history
from aranet_monitor.ble.parse import MANUFACTURER_ID, parse_advert
from aranet_monitor.config import Settings
from aranet_monitor.db.store import Store

log = logging.getLogger(__name__)


@dataclass
class _DeviceState:
    device_id: int
    interval_s: int
    last_counter: int | None = None
    last_ts: int | None = None  # newest stored measurement time
    last_heard: float = 0.0  # monotonic time of last advert
    last_meta_write: float = 0.0
    ble_device: BLEDevice | None = None
    integrations_warned: bool = False
    backfill_pending: bool = False
    backfill_blocked_until: float = 0.0  # monotonic; set after a failed backfill


@dataclass
class _Stats:
    adverts: int = 0
    stored: int = 0
    backfilled: int = 0
    started: float = field(default_factory=time.time)


class Collector:
    """Passive scanner + gap backfill. Run with :meth:`run`."""

    META_EVERY_S = 600  # refresh devices.last_seen / rssi at most this often
    BACKFILL_COOLDOWN_S = 3600

    def __init__(
        self,
        settings: Settings,
        store: Store,
        scanner_factory: Callable[..., BleakScanner] = BleakScanner,
        history_fetcher=fetch_history,
    ):
        self.settings = settings
        self.store = store
        self._scanner_factory = scanner_factory
        self._fetch_history = history_fetcher
        self._queue: asyncio.Queue = asyncio.Queue(maxsize=1000)
        self._devices: dict[str, _DeviceState] = {}
        self._scanner: BleakScanner | None = None
        self._ble_lock = asyncio.Lock()
        self._last_advert = time.monotonic()
        self._stop = asyncio.Event()
        self.stats = _Stats()
        self._tasks: set[asyncio.Task] = set()

    # ------------------------------------------------------------------ scanner
    def _on_advert(self, device: BLEDevice, ad) -> None:
        raw = ad.manufacturer_data.get(MANUFACTURER_ID)
        if raw is None:
            return
        address = device.address.upper()
        if self.settings.devices and address not in self.settings.devices:
            return
        self._last_advert = time.monotonic()
        try:
            self._queue.put_nowait(
                (device, device.name or ad.local_name, bytes(raw), ad.rssi, time.time())
            )
        except asyncio.QueueFull:  # pragma: no cover - consumer stalled
            log.warning("advert queue full; dropping")

    async def _start_scanner(self) -> None:
        self._scanner = self._scanner_factory(detection_callback=self._on_advert)
        await self._scanner.start()
        log.info("passive scan started")

    async def _stop_scanner(self) -> None:
        if self._scanner is not None:
            with contextlib.suppress(Exception):
                await self._scanner.stop()
            self._scanner = None

    # ------------------------------------------------------------------ processing
    async def _consume(self) -> None:
        while not self._stop.is_set():
            try:
                item = await asyncio.wait_for(self._queue.get(), timeout=1.0)
            except TimeoutError:
                continue
            try:
                await self._handle(*item)
            except Exception:
                log.exception("failed to handle advert")

    async def _handle(self, device: BLEDevice, name, raw: bytes, rssi, received: float) -> None:
        self.stats.adverts += 1
        parsed = parse_advert(device.address.upper(), name, raw, rssi, now=received)
        if parsed is None:
            return
        address = parsed.device.address
        state = self._devices.get(address)
        if state is None:
            device_id = await asyncio.to_thread(
                self.store.upsert_device, parsed.device, int(received), rssi
            )
            last_ts = await asyncio.to_thread(self.store.last_ts, device_id)
            state = _DeviceState(
                device_id=device_id,
                interval_s=parsed.device.interval_s or self.store.interval_for(address),
                last_ts=last_ts,
                last_meta_write=time.monotonic(),
            )
            self._devices[address] = state
            log.info("tracking %s (%s) id=%s last_ts=%s", name, address, device_id, last_ts)
            await asyncio.to_thread(
                self.store.log_event, "device_seen", f"{name} {parsed.device.firmware}", device_id
            )
            if self.settings.backfill_on_start and self._gap_exceeded(state, received):
                state.backfill_pending = True
        else:
            silent_for = time.monotonic() - state.last_heard
            if silent_for > self.settings.gap_backfill_min_s and self._gap_exceeded(
                state, received
            ):
                log.info("%s silent for %.0fs; scheduling backfill", address, silent_for)
                state.backfill_pending = True
        state.last_heard = time.monotonic()
        state.ble_device = device

        if parsed.reading is None:
            if not state.integrations_warned:
                state.integrations_warned = True
                msg = "advert has no readings: enable Smart Home integrations in Aranet Home app"
                log.warning("%s: %s", address, msg)
                await asyncio.to_thread(self.store.log_event, "warning", msg, state.device_id)
            return

        if parsed.device.interval_s:
            state.interval_s = parsed.device.interval_s
        if time.monotonic() - state.last_meta_write > self.META_EVERY_S:
            state.last_meta_write = time.monotonic()
            await asyncio.to_thread(self.store.upsert_device, parsed.device, int(received), rssi)

        is_new = parsed.counter is None or parsed.counter != state.last_counter
        if is_new and (
            state.last_ts is None or parsed.reading.ts > state.last_ts + state.interval_s // 2
        ):
            n = await asyncio.to_thread(
                self.store.insert_readings, state.device_id, [parsed.reading], state.interval_s
            )
            if n:
                self.stats.stored += n
                state.last_ts = parsed.reading.ts
                log.info(
                    "%s co2=%s T=%s RH=%s P=%s batt=%s rssi=%s",
                    name,
                    parsed.reading.co2,
                    parsed.reading.temperature,
                    parsed.reading.humidity,
                    parsed.reading.pressure,
                    parsed.reading.battery,
                    rssi,
                )
        state.last_counter = parsed.counter

        if state.backfill_pending:
            state.backfill_pending = False
            if time.monotonic() >= state.backfill_blocked_until:
                self._tasks.add(task := asyncio.create_task(self._backfill(address)))
                task.add_done_callback(self._tasks.discard)
            else:
                log.info("%s: backfill skipped (cooling down after failure)", address)

    def _gap_exceeded(self, state: _DeviceState, now: float) -> bool:
        if state.last_ts is None:
            return True
        return now - state.last_ts > self.settings.gap_backfill_min_s

    # ------------------------------------------------------------------ backfill
    async def _backfill(self, address: str) -> int:
        state = self._devices[address]
        async with self._ble_lock:
            await self._stop_scanner()  # BlueZ connect-while-scanning is unreliable
            # last_ts may have advanced from the advert that triggered us; backfill before it.
            since = await asyncio.to_thread(self._newest_before_gap, state)
            log.info("backfill %s since %s", address, since)
            t0 = time.monotonic()
            n = 0
            try:
                result = await self._fetch_history(
                    state.ble_device or address, since, timeout=self.settings.ble_timeout_s
                )
                await asyncio.to_thread(
                    self.store.upsert_device, result.device, int(time.time()), None
                )
                n = await asyncio.to_thread(
                    self.store.insert_readings, state.device_id, result.readings, state.interval_s
                )
                self.stats.backfilled += n
                detail = (
                    f"{n} new of {len(result.readings)} fetched "
                    f"(device holds {result.total_on_device}) in {time.monotonic() - t0:.1f}s"
                )
                log.info("backfill %s: %s", address, detail)
                await asyncio.to_thread(self.store.log_event, "backfill", detail, state.device_id)
            except Exception as exc:
                # Typical causes: device not paired (Aranet4 needs a PIN-paired bond for GATT),
                # weak signal, or the phone app holding the connection. Back off.
                state.backfill_blocked_until = time.monotonic() + self.BACKFILL_COOLDOWN_S
                log.warning(
                    "backfill %s failed: %r (is the device paired? see docs/DEPLOYMENT.md); "
                    "retrying no sooner than %ds",
                    address,
                    exc,
                    self.BACKFILL_COOLDOWN_S,
                )
                await asyncio.to_thread(
                    self.store.log_event, "backfill_error", repr(exc), state.device_id
                )
            finally:
                await self._start_scanner()
                self._last_advert = time.monotonic()
        return n

    def _newest_before_gap(self, state: _DeviceState) -> int | None:
        """Newest stored ts strictly older than the reading that just arrived."""
        from sqlalchemy import select

        from aranet_monitor.db.schema import readings

        bound = (state.last_ts or int(time.time())) - state.interval_s // 2
        with self.store.engine.connect() as conn:
            return conn.execute(
                select(readings.c.ts)
                .where(readings.c.device_id == state.device_id, readings.c.ts < bound)
                .order_by(readings.c.ts.desc())
                .limit(1)
            ).scalar()

    async def backfill_now(self, address: str, since: int | None = None):
        """One-shot backfill (CLI ``aranet backfill``)."""
        return await self._fetch_history(address, since, timeout=self.settings.ble_timeout_s)

    # ------------------------------------------------------------------ watchdog
    async def _watchdog(self) -> None:
        while not self._stop.is_set():
            await asyncio.sleep(15)
            if self._ble_lock.locked():
                continue
            silent = time.monotonic() - self._last_advert
            if silent > self.settings.watchdog_s:
                log.warning("no adverts for %.0fs; restarting scanner", silent)
                await asyncio.to_thread(
                    self.store.log_event, "scanner_restart", f"silent {silent:.0f}s"
                )
                async with self._ble_lock:
                    await self._stop_scanner()
                    await asyncio.sleep(2)
                    try:
                        await self._start_scanner()
                    except Exception as exc:
                        log.error("scanner restart failed: %r", exc)
                self._last_advert = time.monotonic()

    # ------------------------------------------------------------------ lifecycle
    def stop(self) -> None:
        """Request a graceful shutdown."""
        self._stop.set()

    async def run(self) -> None:
        """Run until :meth:`stop` is called or SIGINT/SIGTERM is received."""
        loop = asyncio.get_running_loop()
        loop.set_exception_handler(_quiet_dbus_noise)
        for sig in (signal.SIGINT, signal.SIGTERM):
            with contextlib.suppress(NotImplementedError, RuntimeError):
                loop.add_signal_handler(sig, self.stop)
        await asyncio.to_thread(self.store.log_event, "start", "collector started")
        await self._start_scanner()
        tasks = [asyncio.create_task(self._consume()), asyncio.create_task(self._watchdog())]
        try:
            await self._stop.wait()
        finally:
            for t in tasks:
                t.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            await self._stop_scanner()
            await asyncio.to_thread(
                self.store.log_event,
                "stop",
                f"adverts={self.stats.adverts} stored={self.stats.stored} "
                f"backfilled={self.stats.backfilled}",
            )
            log.info("collector stopped")


def _quiet_dbus_noise(loop: asyncio.AbstractEventLoop, context: dict) -> None:
    """Downgrade bleak/dbus-fast's orphaned BrokenPipe futures after a cancelled connect."""
    exc = context.get("exception")
    if isinstance(exc, BrokenPipeError | ConnectionResetError | EOFError):
        log.debug("ignored background dbus error: %r", exc)
        return
    loop.default_exception_handler(context)
