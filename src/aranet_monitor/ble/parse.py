"""Turn raw Aranet BLE advertisements into :class:`~aranet_monitor.models.Reading` objects.

Decoding is delegated to :class:`aranet4.client.Aranet4Advertisement` so we track upstream
format changes; this module adds null-safety, the measurement timestamp and the Aranet4
``counter`` byte (which upstream does not decode for Aranet4).
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from types import SimpleNamespace

from aranet4.client import Aranet4, Aranet4Advertisement, AranetType

from aranet_monitor.models import DeviceInfo, Reading

MANUFACTURER_ID = Aranet4.MANUFACTURER_ID  # 0x0702, SAF Tehnika


@dataclass(frozen=True)
class ParsedAdvert:
    """A decoded advertisement."""

    device: DeviceInfo
    reading: Reading | None  # None when smart-home integrations are disabled
    integrations: bool
    ago: int | None
    counter: int | None


def _clean(value, *, cast=None):
    """Map the library's ``-1`` "no data" sentinel (and None) to None."""
    if value is None or value == -1:
        return None
    return cast(value) if cast else value


def parse_advert(
    address: str,
    name: str | None,
    raw: bytes,
    rssi: int | None = None,
    now: float | None = None,
) -> ParsedAdvert | None:
    """Decode Aranet manufacturer data.

    Parameters
    ----------
    address
        BLE address of the advertiser.
    name
        Advertised local name (may be ``None`` on passive scans).
    raw
        Manufacturer-specific data for company id ``0x0702``.
    rssi
        Received signal strength, stored alongside the reading.
    now
        Receive time (epoch seconds); defaults to ``time.time()``.

    Returns
    -------
    ParsedAdvert or None
        ``None`` if the payload is not a valid Aranet advertisement.
    """
    now = time.time() if now is None else now
    device = SimpleNamespace(name=name, address=address)
    ad_data = SimpleNamespace(manufacturer_data={MANUFACTURER_ID: bytes(raw)}, rssi=rssi)
    try:
        adv = Aranet4Advertisement(device, ad_data)
    except Exception:  # malformed payloads must never kill the scanner
        return None
    mf = adv.manufacturer_data
    if mf is None:
        return None

    firmware = str(mf.version) if mf.version else None
    r = adv.readings
    if r is None or not mf.integrations:
        info = DeviceInfo(address=address, name=name, firmware=firmware)
        return ParsedAdvert(info, None, bool(mf.integrations), None, None)

    model = r.type.model if isinstance(r.type, AranetType) else None
    interval = _clean(r.interval, cast=int)
    ago = _clean(r.ago, cast=int)
    counter = _clean(r.counter, cast=int)
    if counter is None and r.type == AranetType.ARANET4 and len(raw) >= 22:
        # Upstream's Aranet4 struct stops one byte short; byte 21 is a rolling counter.
        counter = raw[21]

    measured = int(round(now)) - (ago or 0)
    radon = _clean(r.radon_concentration, cast=int)
    reading = Reading(
        address=address,
        ts=measured,
        source="adv",
        co2=_clean(r.co2, cast=int),
        temperature=_clean(r.temperature, cast=float),
        humidity=_clean(r.humidity, cast=float),
        pressure=_clean(r.pressure, cast=float),
        battery=_clean(r.battery, cast=int),
        status=_clean(r.status, cast=int),
        rssi=rssi,
        radon=radon,
        radiation_rate=_clean(r.radiation_rate, cast=float),
        radiation_total=_clean(r.radiation_total, cast=float),
    )
    info = DeviceInfo(
        address=address, name=name, model=model, firmware=firmware, interval_s=interval
    )
    return ParsedAdvert(info, reading, True, ago, counter)


def looks_like_aranet(name: str | None, manufacturer_data: dict[int, bytes]) -> bool:
    """Cheap pre-filter for scanner callbacks."""
    return MANUFACTURER_ID in manufacturer_data or bool(name and name.startswith("Aranet"))
