"""Download logged history from a device over GATT (used to backfill gaps).

``aranet4.client.get_all_records`` is not used because in aranet4 2.6.0 it returns an empty
record for devices whose name starts with "Aranet4" (they fall into its "unknown model"
branch), and because its read loops have no timeouts. We drive the lower-level
:class:`aranet4.client.Aranet4` methods ourselves and wrap every GATT step in a timeout.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from aranet4.client import Aranet4, Param
from bleak import BleakClient
from bleak.backends.device import BLEDevice
from bleak.exc import BleakError

from aranet_monitor.ble.adapter import bleak_kwargs
from aranet_monitor.models import DeviceInfo, Reading

log = logging.getLogger(__name__)

MAX_ATTEMPTS = 3  # bounds re-reads after ring-buffer shifts (x4)
CHUNK_RECORDS = 500  # records per GATT history request
MAX_CHUNK_RETRIES = 8  # reconnect + retry an operation this many times on link errors
RECONNECT_PAUSE_S = 3.0  # first pause; grows linearly per consecutive retry, capped at 20 s


@dataclass(frozen=True)
class HistoryResult:
    """Outcome of a history download."""

    device: DeviceInfo
    readings: list[Reading]
    total_on_device: int
    requested_from_index: int


def log_times(now: int, total: int, interval: int, ago: int) -> list[int]:
    """Epoch-second timestamps of the ``total`` records stored on the device (oldest first)."""
    first = now - ago - (total - 1) * interval
    return [first + i * interval for i in range(total)]


def first_index_after(times: list[int], since_ts: int | None, tol: int) -> int:
    """1-based index of the first record newer than ``since_ts`` (+tolerance); 0 if none."""
    if since_ts is None:
        return 1 if times else 0
    for i, t in enumerate(times, start=1):
        if t > since_ts + tol:
            return i
    return 0


def _params_for(name: str) -> dict[str, Param]:
    """History parameters to download for a model, keyed by our column name."""
    if name.startswith("Aranet2"):
        return {"temperature": Param.TEMPERATURE, "humidity": Param.HUMIDITY2}
    if name.startswith("AranetRn"):
        p = {"radon": Param.RADON_CONCENTRATION, "pressure": Param.PRESSURE}
        if name.startswith("AranetRn+"):
            p |= {"temperature": Param.TEMPERATURE, "humidity": Param.HUMIDITY2}
        return p
    if name.startswith("Aranet☢"):
        return {}  # radiation history units are not yet mapped; adverts still work
    return {
        "co2": Param.CO2,
        "temperature": Param.TEMPERATURE,
        "humidity": Param.HUMIDITY,
        "pressure": Param.PRESSURE,
    }


async def fetch_history(
    target: str | BLEDevice,
    since_ts: int | None,
    *,
    timeout: float = 30.0,
    max_records: int | None = None,
    on_chunk: Callable[[DeviceInfo, list[Reading]], Awaitable[None] | None] | None = None,
    chunk_size: int = CHUNK_RECORDS,
    adapter: str | None = None,
) -> HistoryResult:
    """Connect to a device and download records logged after ``since_ts``.

    The download is split into chunks of ``chunk_size`` records (oldest first). Each chunk
    reads every parameter, is converted to readings and handed to ``on_chunk`` (if given) so
    callers can store progress as it arrives. BLE links -- especially at weak signal -- drop
    now and then mid-transfer ("GATT Protocol Error: Unlikely Error"); a failed chunk is
    retried after reconnecting, up to ``MAX_CHUNK_RETRIES`` times.

    Parameters
    ----------
    target
        BLE address or a ``BLEDevice`` from a recent scan (preferred: avoids a re-scan).
    since_ts
        Epoch seconds of the newest reading we already have; ``None`` downloads everything.
    timeout
        Per-GATT-operation timeout in seconds.
    max_records
        Optional cap on how many of the newest records to fetch.
    on_chunk
        Callback (sync or async) receiving each chunk's readings.
    chunk_size
        Records per request.
    adapter
        Local BlueZ adapter to connect through (``hci1``, ...); ``None`` = default.
    """
    address = target if isinstance(target, str) else target.address
    monitor = Aranet4(address=address)
    monitor.device = BleakClient(target, timeout=timeout, **bleak_kwargs(adapter))

    async def step(coro, mult: float = 1.0):
        return await asyncio.wait_for(coro, timeout * mult)

    async def reconnect(attempt: int = 1) -> None:
        with contextlib.suppress(Exception):
            await asyncio.wait_for(monitor.device.disconnect(), 10)
        await asyncio.sleep(min(20.0, RECONNECT_PAUSE_S * attempt))
        await step(monitor.connect(), 1.5)  # failures here count as a retry by the caller

    async def call(make_coro, mult: float = 1.0, what: str = "gatt"):
        """Run one GATT operation; on a link error reconnect and retry (bounded)."""
        last_exc: Exception | None = None
        for retry in range(MAX_CHUNK_RETRIES + 1):
            try:
                if retry:
                    await reconnect(retry)
                    if make_coro == monitor.connect:  # bound methods: == not `is`
                        return None  # reconnect() already connected
                return await step(make_coro(), mult)
            except (BleakError, TimeoutError, EOFError, OSError) as exc:
                last_exc = exc
                log.info(
                    "history %s: %s failed (%r); reconnect + retry %d/%d",
                    address,
                    what,
                    exc,
                    retry + 1,
                    MAX_CHUNK_RETRIES,
                )
        raise last_exc  # type: ignore[misc]

    try:
        await call(monitor.connect, 1.5, "connect")
        name = await call(monitor.get_name)
        firmware = await call(monitor.get_version)
        interval = await call(monitor.get_interval)
        info = DeviceInfo(
            address=address.upper(), name=name, firmware=firmware, interval_s=interval
        )
        params = _params_for(name)

        ago = await call(monitor.get_seconds_since_update)
        if interval - ago < 8:
            # A new point is about to be logged; let it happen before anchoring.
            await asyncio.sleep(interval - ago + 2)
            ago = await call(monitor.get_seconds_since_update)
        anchor_now, anchor_ago = int(time.time()), ago
        total = await call(monitor.get_total_readings)
        times = log_times(anchor_now, total, interval, anchor_ago)
        start = first_index_after(times, since_ts, tol=interval // 2)
        if max_records and start and total - start + 1 > max_records:
            start = total - max_records + 1
        if start == 0:
            return HistoryResult(info, [], total, 0)

        async def read_param(param, lo: int, hi: int) -> list:
            # log_size padded: the library may write one slot past `end`.
            return await call(
                lambda: monitor.get_records(param, total + 8, lo, hi),
                2,
                f"param {int(param)} {lo}..{hi}",
            )

        out: list[Reading] = []
        lo = start
        rereads = 0
        while lo <= total:
            hi = min(total, lo + chunk_size - 1)
            columns = {col: await read_param(param, lo, hi) for col, param in params.items()}
            # Did the device log a new point while we read this chunk? `ago` would have reset.
            ago_now = await call(monitor.get_seconds_since_update)
            t_now = int(time.time())
            expected = anchor_ago + (t_now - anchor_now)
            if ago_now < expected - 3:
                total_now = await call(monitor.get_total_readings)
                if total_now == total:
                    # Full ring buffer: every index moved down by `k`. Re-anchor and re-read
                    # this chunk from its new position (indices before `lo` are done).
                    k = max(1, round((expected - ago_now) / interval))
                    rereads += 1
                    if rereads > MAX_ATTEMPTS * 4:
                        raise RuntimeError("history kept shifting under the download")
                    log.info(
                        "history %s: ring buffer shifted by %d during %d..%d; re-reading",
                        address,
                        k,
                        lo,
                        hi,
                    )
                    anchor_now, anchor_ago = t_now, ago_now
                    times = log_times(anchor_now, total, interval, anchor_ago)
                    lo = max(1, lo - k)
                    continue
                # Growing log: new records were appended; existing indices are stable.
            anchor_now, anchor_ago = t_now, ago_now
            chunk = _to_readings(address, columns, times, lo, hi)
            out += chunk
            log.info("history %s: records %d..%d of %d", address, lo, hi, total)
            if on_chunk is not None and chunk:
                res = on_chunk(info, chunk)
                if asyncio.iscoroutine(res):
                    await res
            lo = hi + 1
        return HistoryResult(info, out, total, start)
    finally:
        try:
            await asyncio.wait_for(monitor.device.disconnect(), 10)
        except Exception as exc:  # pragma: no cover - best effort
            log.debug("disconnect failed: %s", exc)


def _to_readings(
    address: str, columns: dict[str, list], times: list[int], lo: int, hi: int
) -> list[Reading]:
    """Assemble readings for 1-based record indices ``lo..hi``."""
    out = []
    for idx in range(lo - 1, hi):
        vals = {}
        for col, series in columns.items():
            v = series[idx] if idx < len(series) else -1
            vals[col] = None if v == -1 else v
        if all(v is None for v in vals.values()):
            continue
        if vals.get("co2") is not None:
            vals["co2"] = int(vals["co2"])
        out.append(Reading(address=address.upper(), ts=times[idx], source="history", **vals))
    return out
