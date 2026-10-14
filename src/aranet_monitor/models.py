"""Device-agnostic data model shared by the collector, store, API and dashboard."""

from __future__ import annotations

from dataclasses import asdict, dataclass

# Columns stored per reading (besides device_id / ts / source). Order is used for CSV export.
METRICS: tuple[str, ...] = (
    "co2",
    "temperature",
    "humidity",
    "pressure",
    "battery",
    "status",
    "rssi",
    "radon",
    "radiation_rate",
    "radiation_total",
)

# Metrics that make sense to chart / aggregate.
SERIES_METRICS: tuple[str, ...] = (
    "co2",
    "temperature",
    "humidity",
    "pressure",
    "battery",
    "radon",
    "radiation_rate",
)

UNITS: dict[str, str] = {
    "co2": "ppm",
    "temperature": "°C",
    "humidity": "%",
    "pressure": "hPa",
    "battery": "%",
    "rssi": "dBm",
    "radon": "Bq/m³",
    "radiation_rate": "µSv/h",
    "radiation_total": "mSv",
}


@dataclass(frozen=True)
class Reading:
    """One logged measurement from one device.

    ``ts`` is the *measurement* time in integer epoch seconds (UTC), i.e. the moment the
    device logged the value, not the moment we heard it.
    """

    address: str
    ts: int
    source: str  # "adv" | "history" | "sim"
    co2: int | None = None
    temperature: float | None = None
    humidity: float | None = None
    pressure: float | None = None
    battery: int | None = None
    status: int | None = None
    rssi: int | None = None
    radon: int | None = None
    radiation_rate: float | None = None
    radiation_total: float | None = None

    def values(self) -> dict[str, float | int | None]:
        """Return the metric columns as a dict."""
        d = asdict(self)
        return {k: d[k] for k in METRICS}


@dataclass(frozen=True)
class DeviceInfo:
    """Identity and metadata of a sensor as seen over BLE."""

    address: str
    name: str | None = None
    model: str | None = None
    firmware: str | None = None
    interval_s: int | None = None
