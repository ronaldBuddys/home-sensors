"""Download logged history from a device over GATT (used to backfill gaps).

``aranet4.client.get_all_records`` is not used because in aranet4 2.6.0 it returns an empty
record for devices whose name starts with "Aranet4" (they fall into its "unknown model"
branch), and because its read loops have no timeouts. We drive the lower-level
:class:`aranet4.client.Aranet4` methods ourselves and wrap every GATT step in a timeout.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass

from aranet4.client import Aranet4, Param
from bleak import BleakClient
from bleak.backends.device import BLEDevice

from aranet_monitor.models import DeviceInfo, Reading

log = logging.getLogger(__name__)


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
) -> HistoryResult:
    """Connect to a device and download records logged after ``since_ts``.

    Parameters
    ----------
    target
        BLE address or a ``BLEDevice`` from a recent scan (preferred: avoids a re-scan).
    since_ts
        Epoch seconds of the newest reading we already have; ``None`` downloads everything.
    timeout
        Per-GATT-operation timeout in seconds. A whole parameter download gets 4x this.
    max_records
        Optional cap on how many of the newest records to fetch.
    """
    address = target if isinstance(target, str) else target.address
    monitor = Aranet4(address=address)
    monitor.device = BleakClient(target, timeout=timeout)

    async def step(coro, mult: float = 1.0):
        return await asyncio.wait_for(coro, timeout * mult)

    try:
        await step(monitor.connect(), 1.5)
        name = await step(monitor.get_name())
        firmware = await step(monitor.get_version())
        interval = await step(monitor.get_interval())
        ago = await step(monitor.get_seconds_since_update())
        now = int(time.time())
        if interval - ago < 8:
            # A new point is about to be logged; wait so indices don't shift mid-download.
            await asyncio.sleep(interval - ago + 2)
            ago = await step(monitor.get_seconds_since_update())
            now = int(time.time())
        total = await step(monitor.get_total_readings())
        times = log_times(now, total, interval, ago)
        start = first_index_after(times, since_ts, tol=interval // 2)
        if max_records and start and total - start + 1 > max_records:
            start = total - max_records + 1
        info = DeviceInfo(
            address=address.upper(), name=name, firmware=firmware, interval_s=interval
        )
        if start == 0:
            return HistoryResult(info, [], total, 0)

        columns: dict[str, list] = {}
        for col, param in _params_for(name).items():
            t0 = time.monotonic()
            columns[col] = await step(monitor.get_records(param, total, start, total), 4)
            log.info(
                "history %s: %s records %d..%d in %.1fs",
                address,
                col,
                start,
                total,
                time.monotonic() - t0,
            )

        out: list[Reading] = []
        for idx in range(start - 1, total):
            vals = {}
            for col, series in columns.items():
                v = series[idx]
                vals[col] = None if v == -1 else v
            if all(v is None for v in vals.values()):
                continue
            if vals.get("co2") is not None:
                vals["co2"] = int(vals["co2"])
            out.append(Reading(address=address.upper(), ts=times[idx], source="history", **vals))
        return HistoryResult(info, out, total, start)
    finally:
        try:
            await asyncio.wait_for(monitor.device.disconnect(), 10)
        except Exception as exc:  # pragma: no cover - best effort
            log.debug("disconnect failed: %s", exc)
