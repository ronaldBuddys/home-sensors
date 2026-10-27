"""Continuous, battery-friendly collector.

Normal operation only listens to BLE advertisements (we never connect): with "Smart Home
integrations" enabled the sensor broadcasts its latest measurement in every advertisement. A
GATT connection (which costs sensor battery) is only made to backfill history, and only for
*trusted* devices -- allow-listed ones (``ARANET_DEVICES``) or, without an allow-list, devices
that already had stored readings before this session (so a spoofed advert can't make the
collector pause scanning to connect to it):

* when the measurement time of a new advert is more than ``max(gap_backfill_min_s,
  1.5 x interval)`` after the newest stored row (collector was down, scanner was paused,
  radio dropped out, ...) -- this includes the first advert after start-up;
* at most once every ``BACKFILL_MIN_SPACING_S``; a failed attempt stays pending and is retried
  with exponential back-off (the device keeps ~5040 records, so deferring loses nothing).

A re-advertised measurement is recognised purely by its timestamp (within interval/2 of the
newest stored row), not by the advert's rolling counter byte, so firmware that doesn't
increment it cannot cause silent data loss.

Scanning is *passive* where BlueZ supports it (``ARANET_SCAN_MODE=auto``, the default): the
kernel runs one continuous low-duty-cycle passive scan and BlueZ hands us matching adverts via
an advertisement monitor. The fallback, BlueZ discovery, is an *active* scan (a scan request to
every device heard) that BlueZ stops and restarts every ~10 s; on the Raspberry Pi's onboard
UART chip that load is what eventually wedges the controller (``LE Set Scan Enable`` times out).
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import shutil
import signal
import subprocess
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path

from bleak import BleakScanner
from bleak.args.bluez import OrPattern
from bleak.assigned_numbers import AdvertisementDataType
from bleak.backends.device import BLEDevice

from aranet_monitor.ble.adapter import (
    bleak_kwargs,
    can_power_cycle,
    passive_scan_supported,
    power_cycle_adapter,
)
from aranet_monitor.ble.history import fetch_history
from aranet_monitor.ble.parse import MANUFACTURER_ID, parse_advert
from aranet_monitor.config import Settings
from aranet_monitor.db.store import Store

log = logging.getLogger(__name__)

# Passive scanning only delivers adverts matching one of these: Aranet's manufacturer data, plus
# the common AD Flags values, so the watchdog can still tell "sensor gone" from "radio deaf".
PASSIVE_PATTERNS = [
    OrPattern(
        0, AdvertisementDataType.MANUFACTURER_SPECIFIC_DATA, MANUFACTURER_ID.to_bytes(2, "little")
    ),
    *(OrPattern(0, AdvertisementDataType.FLAGS, bytes([f])) for f in (0x02, 0x06, 0x18, 0x1A)),
]


@dataclass
class _DeviceState:
    device_id: int
    interval_s: int
    last_ts: int | None = None  # newest stored measurement time
    last_meta_write: float = 0.0
    ble_device: BLEDevice | None = None
    integrations_warned: bool = False
    seen_this_session: bool = False
    trusted: bool = False  # may be backfilled over GATT
    backfill_pending: bool = False
    backfill_since: int | None = None  # newest stored ts before the oldest unfilled gap
    backfill_running: bool = False
    backfill_not_before: float = 0.0  # monotonic; spacing / failure back-off
    backfill_failures: int = 0


@dataclass
class _Stats:
    adverts: int = 0
    stored: int = 0
    backfilled: int = 0
    started: float = field(default_factory=time.time)


TIMESYNC_MARKER = Path("/run/systemd/timesync/synchronized")


def clock_synchronised() -> bool | None:
    """Return whether the system clock is NTP-synchronised, or ``None`` if that is unknown.

    Checks systemd-timesyncd's marker file first, then asks ``timedatectl``. Any failure
    (no systemd, command missing, timeout, odd output) yields ``None``.
    """
    try:
        if TIMESYNC_MARKER.exists():
            return True
        if shutil.which("timedatectl") is None:
            return None
        out = subprocess.run(
            ["timedatectl", "show", "-p", "NTPSynchronized", "--value"],
            capture_output=True,
            text=True,
            timeout=5,
            check=True,
        ).stdout.strip()
    except Exception:
        return None
    return {"yes": True, "no": False}.get(out)


class Collector:
    """Passive scanner + gap backfill. Run with :meth:`run`."""

    META_EVERY_S = 600  # refresh devices.last_seen / rssi at most this often
    BACKFILL_MIN_SPACING_S = 900  # battery: at most one GATT session per 15 min per device
    BACKFILL_RETRY_S = 900  # first retry after a failure; doubles each time ...
    BACKFILL_RETRY_MAX_S = 6 * 3600  # ... up to this
    SCANNER_OP_TIMEOUT_S = 20
    SCANNER_RESTART_PAUSE_S = 2.0
    WATCHDOG_POLL_S = 15.0
    ADAPTER_RESET_AFTER = 1  # scanner restarts that heard nothing at all before power-cycling
    MAX_UNLISTED_DEVICES = 16  # without an allow-list, ignore adverts beyond this many devices
    CLOCK_POLL_S = 5.0  # how often to re-check the clock while waiting for NTP
    ERROR_LOG_EVERY_S = 60  # e.g. DB down: one traceback per minute, not one per advert

    def __init__(
        self,
        settings: Settings,
        store: Store,
        scanner_factory: Callable[..., BleakScanner] = BleakScanner,
        history_fetcher=fetch_history,
        clock_probe: Callable[[], bool | None] = clock_synchronised,
        adapter_reset: Callable[[str | None], Awaitable[object]] | None = None,
        passive_probe: Callable[[str | None], Awaitable[bool]] | None = None,
    ):
        self.settings = settings
        self.store = store
        self._scanner_factory = scanner_factory
        self._fetch_history = history_fetcher
        self._clock_probe = clock_probe
        if adapter_reset is None and can_power_cycle():
            adapter_reset = power_cycle_adapter
        # None: a deaf adapter just gets scanner restarts (disabled, or not on Linux)
        self._adapter_reset = adapter_reset if settings.adapter_reset else None
        if passive_probe is None:
            passive_probe = passive_scan_supported if can_power_cycle() else _no_passive
        self._passive_probe = passive_probe
        self._passive = False  # decided in run()
        self._queue: asyncio.Queue = asyncio.Queue(maxsize=1000)
        self._devices: dict[str, _DeviceState] = {}
        self._scanner: BleakScanner | None = None
        self._ble_lock = asyncio.Lock()
        self._last_advert = time.monotonic()  # last advert from a (wanted) Aranet
        self._last_radio = time.monotonic()  # last advert from any device at all
        self._deaf_restarts = 0  # consecutive scanner restarts after which nothing was heard
        self._stop = asyncio.Event()
        self.stats = _Stats()
        self._tasks: set[asyncio.Task] = set()
        self._last_error_log = -float("inf")
        self._suppressed_errors = 0
        self._cap_warned = False

    # ------------------------------------------------------------------ scanner
    def _on_advert(self, device: BLEDevice, ad) -> None:
        self._last_radio = time.monotonic()
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

    async def _start_scanner(self) -> bool:
        """Start a fresh scanner; on failure log it and leave recovery to the watchdog."""
        kwargs = bleak_kwargs(self.settings.ble_adapter)
        if self._passive:
            kwargs = {
                "scanning_mode": "passive",
                "bluez": {**kwargs.get("bluez", {}), "or_patterns": PASSIVE_PATTERNS},
            }
        scanner = self._scanner_factory(detection_callback=self._on_advert, **kwargs)
        try:
            await asyncio.wait_for(scanner.start(), self.SCANNER_OP_TIMEOUT_S)
        except Exception as exc:
            log.error("scanner start failed: %r (watchdog will retry)", exc)
            # A start that timed out may have left BlueZ discovering on our behalf; without a
            # stop every later start fails with org.bluez.Error.InProgress.
            with contextlib.suppress(Exception):
                await asyncio.wait_for(scanner.stop(), self.SCANNER_OP_TIMEOUT_S)
            self._scanner = None
            return False
        self._scanner = scanner
        log.info("BLE scan started (%s)", "passive" if self._passive else "active")
        return True

    async def _stop_scanner(self) -> None:
        if self._scanner is not None:
            with contextlib.suppress(Exception):
                await asyncio.wait_for(self._scanner.stop(), self.SCANNER_OP_TIMEOUT_S)
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
                now = time.monotonic()
                if now - self._last_error_log < self.ERROR_LOG_EVERY_S:
                    self._suppressed_errors += 1
                    continue
                self._last_error_log = now
                log.exception(
                    "failed to handle advert (%d similar errors suppressed)",
                    self._suppressed_errors,
                )
                self._suppressed_errors = 0

    async def _handle(self, device: BLEDevice, name, raw: bytes, rssi, received: float) -> None:
        self.stats.adverts += 1
        parsed = parse_advert(device.address.upper(), name, raw, rssi, now=received)
        if parsed is None:
            return
        address = parsed.device.address
        state = self._devices.get(address)
        if state is None:
            if not self.settings.devices and len(self._devices) >= self.MAX_UNLISTED_DEVICES:
                if not self._cap_warned:
                    self._cap_warned = True
                    log.warning(
                        "ignoring new device %s: already tracking %d without an allow-list "
                        "(set ARANET_DEVICES)",
                        address,
                        len(self._devices),
                    )
                return
            device_id = await asyncio.to_thread(
                self.store.upsert_device, parsed.device, int(received), rssi
            )
            last_ts = await asyncio.to_thread(self.store.last_ts, device_id)
            interval = parsed.device.interval_s or await asyncio.to_thread(
                self.store.interval_for, address
            )
            state = _DeviceState(
                device_id=device_id,
                interval_s=interval,
                last_ts=last_ts,
                last_meta_write=time.monotonic(),
                trusted=bool(self.settings.devices) or last_ts is not None,
            )
            self._devices[address] = state
            log.info("tracking %s (%s) id=%s last_ts=%s", name, address, device_id, last_ts)
            await asyncio.to_thread(
                self.store.log_event, "device_seen", f"{name} {parsed.device.firmware}", device_id
            )
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

        reading = parsed.reading
        is_new = state.last_ts is None or reading.ts > state.last_ts + state.interval_s // 2
        if is_new:
            first = not state.seen_this_session
            state.seen_this_session = True
            gap = state.last_ts is None or reading.ts - state.last_ts > self._gap_threshold(state)
            wanted = state.trusted and gap and (not first or self.settings.backfill_on_start)
            if wanted and not state.backfill_pending:
                state.backfill_pending = True
                state.backfill_since = state.last_ts
                log.info("%s: gap since %s detected; backfill scheduled", address, state.last_ts)
            n = await asyncio.to_thread(
                self.store.insert_readings, state.device_id, [reading], state.interval_s
            )
            if n:
                self.stats.stored += n
                log.info(
                    "%s co2=%s T=%s RH=%s P=%s batt=%s rssi=%s",
                    name,
                    reading.co2,
                    reading.temperature,
                    reading.humidity,
                    reading.pressure,
                    reading.battery,
                    rssi,
                )
            state.last_ts = max(state.last_ts or 0, reading.ts)

        if (
            state.backfill_pending
            and not state.backfill_running
            and not self._stop.is_set()
            and time.monotonic() >= state.backfill_not_before
        ):
            state.backfill_running = True
            self._tasks.add(task := asyncio.create_task(self._backfill(address)))
            task.add_done_callback(self._tasks.discard)

    def _gap_threshold(self, state: _DeviceState) -> int:
        return max(self.settings.gap_backfill_min_s, int(1.5 * state.interval_s))

    # ------------------------------------------------------------------ backfill
    async def _backfill(self, address: str) -> int:
        state = self._devices[address]
        since = state.backfill_since
        n = 0
        try:
            async with self._ble_lock:
                await self._stop_scanner()  # BlueZ connect-while-scanning is unreliable
                log.info("backfill %s since %s", address, since)
                t0 = time.monotonic()
                try:

                    async def save(_info, chunk) -> None:
                        # Store as we go; on a later failure the retry resumes from here.
                        nonlocal n
                        n += await asyncio.to_thread(
                            self.store.insert_readings, state.device_id, chunk, state.interval_s
                        )
                        state.backfill_since = max(state.backfill_since or 0, chunk[-1].ts)

                    result = await self._fetch_history(
                        state.ble_device or address,
                        since,
                        timeout=self.settings.ble_timeout_s,
                        on_chunk=save,
                        adapter=self.settings.ble_adapter,
                    )
                    await asyncio.to_thread(
                        self.store.upsert_device, result.device, int(time.time()), None
                    )
                    # Idempotent safety net (also covers fetchers that don't stream chunks).
                    n += await asyncio.to_thread(
                        self.store.insert_readings,
                        state.device_id,
                        result.readings,
                        state.interval_s,
                    )
                    self.stats.backfilled += n
                    state.backfill_pending = False
                    state.backfill_since = None
                    state.backfill_failures = 0
                    state.backfill_not_before = time.monotonic() + self.BACKFILL_MIN_SPACING_S
                    if result.readings:
                        state.last_ts = max(state.last_ts or 0, result.readings[-1].ts)
                    detail = (
                        f"{n} new of {len(result.readings)} fetched "
                        f"(device holds {result.total_on_device}) "
                        f"in {time.monotonic() - t0:.1f}s"
                    )
                    log.info("backfill %s: %s", address, detail)
                    await asyncio.to_thread(
                        self.store.log_event, "backfill", detail, state.device_id
                    )
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    # Typical causes: device not paired (Aranet4 needs a PIN-paired bond for
                    # GATT), weak signal, or the phone app holding the connection. The gap
                    # stays pending and is retried with exponential back-off.
                    state.backfill_failures += 1
                    wait = min(
                        self.BACKFILL_RETRY_MAX_S,
                        self.BACKFILL_RETRY_S * 2 ** (state.backfill_failures - 1),
                    )
                    state.backfill_not_before = time.monotonic() + wait
                    log.warning(
                        "backfill %s failed (%d in a row): %r (is the device paired? see "
                        "docs/DEPLOYMENT.md); will retry in %ds",
                        address,
                        state.backfill_failures,
                        exc,
                        wait,
                    )
                    with contextlib.suppress(Exception):  # the DB may be down as well
                        await asyncio.to_thread(
                            self.store.log_event,
                            "backfill_error",
                            f"{exc!r}; retry in {wait}s",
                            state.device_id,
                        )
                finally:
                    if not self._stop.is_set():
                        await self._start_scanner()
                    self._last_advert = time.monotonic()
        finally:
            state.backfill_running = False
        return n

    # ------------------------------------------------------------------ watchdog
    async def _watchdog(self) -> None:
        """Restart the scanner when the sensor goes quiet; power-cycle a deaf adapter.

        If other devices are still heard, the sensor is just out of range and a scanner
        restart is all we can do. If nothing at all is heard, even after a restart, the
        controller has stopped delivering adverts and only an adapter power-cycle helps.
        """
        while not self._stop.is_set():
            await asyncio.sleep(self.WATCHDOG_POLL_S)
            if self._ble_lock.locked():
                continue
            now = time.monotonic()
            silent = now - self._last_advert
            if silent <= self.settings.watchdog_s:
                continue
            deaf = now - self._last_radio > self.settings.watchdog_s
            reset = (
                deaf
                and self._adapter_reset is not None
                and self._deaf_restarts >= self.ADAPTER_RESET_AFTER
            )
            log.warning(
                "no Aranet adverts for %.0fs (%s); %s",
                silent,
                "nothing heard from any device" if deaf else "other devices still heard",
                "power-cycling the Bluetooth adapter" if reset else "restarting scanner",
            )
            await asyncio.to_thread(
                self.store.log_event,
                "adapter_reset" if reset else "scanner_restart",
                f"silent {silent:.0f}s" + ("" if deaf else ", radio alive"),
            )
            async with self._ble_lock:
                await self._stop_scanner()
                if reset:
                    try:
                        await asyncio.wait_for(
                            self._adapter_reset(self.settings.ble_adapter),
                            self.SCANNER_OP_TIMEOUT_S,
                        )
                    except Exception as exc:
                        log.error("adapter power-cycle failed: %r", exc)
                await asyncio.sleep(self.SCANNER_RESTART_PAUSE_S)
                await self._start_scanner()
            self._deaf_restarts = self._deaf_restarts + 1 if deaf and not reset else 0
            self._last_advert = self._last_radio = time.monotonic()

    # ------------------------------------------------------------------ lifecycle
    def stop(self) -> None:
        """Request a graceful shutdown."""
        self._stop.set()

    async def _wait_for_clock(self) -> None:
        """Wait (bounded) for NTP sync so readings are not stamped with a stale clock."""
        limit = self.settings.wait_clock_sync_s
        synced = await asyncio.to_thread(self._clock_probe)
        if synced is None:
            log.info("cannot tell whether the clock is synchronised; not waiting")
            return
        if synced:
            return
        log.warning("system clock not synchronised yet; waiting up to %d s before recording", limit)
        await asyncio.to_thread(self.store.log_event, "clock_wait", f"waiting up to {limit}s")
        start = time.monotonic()
        deadline = start + limit
        while not self._stop.is_set():
            if time.monotonic() >= deadline:
                log.warning("clock still not synchronised after %d s; recording anyway", limit)
                await asyncio.to_thread(
                    self.store.log_event, "clock_unsynced", f"not synchronised after {limit}s"
                )
                return
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(
                    self._stop.wait(),
                    min(self.CLOCK_POLL_S, max(0.0, deadline - time.monotonic())),
                )
            if self._stop.is_set():
                return
            if await asyncio.to_thread(self._clock_probe):
                waited = time.monotonic() - start
                log.info("clock synchronised after %.0f s", waited)
                await asyncio.to_thread(
                    self.store.log_event, "clock_synced", f"synchronised after {waited:.0f}s"
                )
                return

    async def _choose_scan_mode(self) -> None:
        mode = self.settings.scan_mode
        if mode == "auto":
            self._passive = await self._passive_probe(self.settings.ble_adapter)
            if not self._passive:
                log.warning(
                    "passive BLE scanning unavailable (BlueZ needs `Experimental = true` in "
                    "/etc/bluetooth/main.conf); falling back to active scanning, which some "
                    "adapters (e.g. the Raspberry Pi's onboard chip) don't survive for long"
                )
        else:
            self._passive = mode == "passive"
        log.info("scan mode: %s", "passive" if self._passive else "active")

    async def run(self) -> None:
        """Run until :meth:`stop` is called or SIGINT/SIGTERM is received."""
        loop = asyncio.get_running_loop()
        loop.set_exception_handler(_quiet_dbus_noise)
        for sig in (signal.SIGINT, signal.SIGTERM):
            with contextlib.suppress(NotImplementedError, RuntimeError):
                loop.add_signal_handler(sig, self.stop)
        await asyncio.to_thread(self.store.log_event, "start", "collector started")
        log.info("Bluetooth adapter: %s", self.settings.ble_adapter or "system default")
        await self._choose_scan_mode()
        if not self.settings.devices:
            log.warning(
                "ARANET_DEVICES is not set: accepting adverts from any Aranet device; new "
                "devices are not backfilled automatically (use `aranet backfill ADDR`)"
            )
        tasks: list[asyncio.Task] = []
        try:
            if self.settings.wait_clock_sync_s > 0:
                await self._wait_for_clock()
            if not self._stop.is_set():
                await self._start_scanner()
                tasks = [
                    asyncio.create_task(self._consume()),
                    asyncio.create_task(self._watchdog()),
                ]
            await self._stop.wait()
        finally:
            self._stop.set()
            pending = [*tasks, *self._tasks]
            for t in pending:
                t.cancel()
            await asyncio.gather(*pending, return_exceptions=True)
            await self._stop_scanner()
            await asyncio.to_thread(
                self.store.log_event,
                "stop",
                f"adverts={self.stats.adverts} stored={self.stats.stored} "
                f"backfilled={self.stats.backfilled}",
            )
            log.info("collector stopped")


async def _no_passive(_adapter: str | None) -> bool:
    return False


def _quiet_dbus_noise(loop: asyncio.AbstractEventLoop, context: dict) -> None:
    """Downgrade bleak/dbus-fast's orphaned BrokenPipe futures after a cancelled connect."""
    exc = context.get("exception")
    if isinstance(exc, BrokenPipeError | ConnectionResetError | EOFError):
        log.debug("ignored background dbus error: %r", exc)
        return
    loop.default_exception_handler(context)
