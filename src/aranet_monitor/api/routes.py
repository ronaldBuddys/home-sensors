"""REST API (``/api/v1``). Read-only; OpenAPI docs are served at ``/api/docs``."""

from __future__ import annotations

import csv
import io
import time
from collections.abc import Iterator
from datetime import UTC, datetime
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from aranet_monitor.config import Settings
from aranet_monitor.db.queries import RANGES, Queries
from aranet_monitor.models import METRICS, UNITS

router = APIRouter(prefix="/api/v1", tags=["aranet"])


# ---------------------------------------------------------------------- dependencies
def get_queries(request: Request) -> Queries:
    """Shared read-side query object (created once at app start-up)."""
    return request.app.state.queries


def get_settings(request: Request) -> Settings:
    """Application settings."""
    return request.app.state.settings


QueriesDep = Annotated[Queries, Depends(get_queries)]
SettingsDep = Annotated[Settings, Depends(get_settings)]


def resolve_window(
    range_: str | None, start: int | None, end: int | None, now: int | None = None
) -> tuple[int, int]:
    """Turn ``range=24h`` or explicit ``start``/``end`` epoch seconds into a window."""
    now = now or int(time.time())
    end = end if end is not None else now
    if start is None:
        span = RANGES.get(range_ or "24h")
        if span is None:
            raise HTTPException(422, f"range must be one of {', '.join(RANGES)}")
        start = end - span
    if start >= end:
        raise HTTPException(422, "start must be before end")
    return start, end


def _device_or_404(q: Queries, device_id: int) -> dict:
    dev = q.device(device_id)
    if dev is None:
        raise HTTPException(404, f"device {device_id} not found")
    return dev


# ---------------------------------------------------------------------- models
class Device(BaseModel):
    """A sensor."""

    id: int
    address: str
    name: str | None
    model: str | None
    firmware: str | None
    interval_s: int | None
    first_seen: int | None
    last_seen: int | None
    last_rssi: int | None
    latest_ts: int | None = None
    earliest_ts: int | None = None


class LatestReading(BaseModel):
    """Newest reading for a device."""

    device_id: int
    ts: int
    age_s: int
    source: str
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


class SeriesOut(BaseModel):
    """Column-oriented (optionally aggregated) time series."""

    device_id: int
    start: int
    end: int
    bucket_s: int = Field(description="0 = raw rows; otherwise bucket width in seconds")
    count: int
    t: list[int] = Field(description="epoch seconds (bucket centre when aggregated)")
    mean: dict[str, list[float | None]]
    min: dict[str, list[float | None]]
    max: dict[str, list[float | None]]
    units: dict[str, str]


# ---------------------------------------------------------------------- routes
@router.get("/health")
def health(q: QueriesDep, settings: SettingsDep) -> dict:
    """Data freshness per device and recent collector events."""
    return q.health(stale_after_s=max(300, settings.watchdog_s * 2))


@router.get("/devices", response_model=list[Device])
def devices(q: QueriesDep) -> list[dict]:
    """All known devices."""
    return q.list_devices()


@router.get("/devices/{device_id}", response_model=Device)
def device(device_id: int, q: QueriesDep) -> dict:
    """One device."""
    return _device_or_404(q, device_id)


@router.get("/devices/{device_id}/latest", response_model=LatestReading)
def latest(device_id: int, q: QueriesDep) -> dict:
    """Most recent reading."""
    _device_or_404(q, device_id)
    row = q.latest(device_id)
    if row is None:
        raise HTTPException(404, "no readings yet")
    row.pop("previous", None)
    row["age_s"] = int(time.time()) - row["ts"]
    return row


@router.get("/devices/{device_id}/readings", response_model=SeriesOut)
def readings(
    device_id: int,
    q: QueriesDep,
    range: Annotated[str | None, Query(description=f"one of {', '.join(RANGES)}")] = None,
    start: int | None = None,
    end: int | None = None,
    metrics: Annotated[
        str | None, Query(description="comma separated, e.g. co2,temperature")
    ] = None,
    max_points: Annotated[int, Query(ge=10, le=20000)] = 1500,
) -> dict:
    """Time series, downsampled server-side to at most ``max_points`` buckets."""
    dev = _device_or_404(q, device_id)
    start, end = resolve_window(range, start, end)
    ms = [m.strip() for m in metrics.split(",")] if metrics else None
    try:
        s = q.series(device_id, start, end, ms, max_points, dev.get("interval_s") or 60)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    out = s.as_dict()
    out.update(
        device_id=device_id, start=start, end=end, units={m: UNITS.get(m, "") for m in s.mean}
    )
    return out


@router.get("/devices/{device_id}/stats")
def stats(
    device_id: int,
    q: QueriesDep,
    settings: SettingsDep,
    range: str | None = None,
    start: int | None = None,
    end: int | None = None,
) -> dict:
    """Min/mean/max and time spent above CO₂ thresholds for a window."""
    _device_or_404(q, device_id)
    start, end = resolve_window(range, start, end)
    return {
        "device_id": device_id,
        "start": start,
        "end": end,
        "co2_warn": settings.co2_warn,
        "co2_alert": settings.co2_alert,
        **q.stats(device_id, start, end, settings.co2_warn, settings.co2_alert),
    }


@router.get("/devices/{device_id}/export.csv")
def export_csv(
    device_id: int,
    q: QueriesDep,
    range: str | None = None,
    start: int | None = None,
    end: int | None = None,
) -> StreamingResponse:
    """Stream raw readings as CSV."""
    dev = _device_or_404(q, device_id)
    start, end = resolve_window(range, start, end)

    def gen() -> Iterator[str]:
        buf = io.StringIO()
        w = csv.writer(buf)
        w.writerow(["time_utc", "ts", "source", *METRICS])
        for row in q.iter_rows(device_id, start, end):
            iso = datetime.fromtimestamp(row[0], tz=UTC).isoformat()
            w.writerow([iso, *row])
            if buf.tell() > 64_000:
                yield buf.getvalue()
                buf.seek(0)
                buf.truncate()
        yield buf.getvalue()

    safe = "".join(c if c.isalnum() else "_" for c in (dev.get("name") or f"device{device_id}"))
    fname = (
        f"{safe}_{datetime.fromtimestamp(start):%Y%m%d}-{datetime.fromtimestamp(end):%Y%m%d}.csv"
    )
    return StreamingResponse(
        gen(),
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{fname}"'},
    )


@router.get("/events")
def collector_events(q: QueriesDep, limit: Annotated[int, Query(ge=1, le=500)] = 50) -> list[dict]:
    """Recent collector events (start/stop, backfills, errors)."""
    return q.recent_events(limit)
