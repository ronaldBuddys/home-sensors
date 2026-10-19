"""Read-side queries shared by the FastAPI routes and the Dash callbacks."""

from __future__ import annotations

import math
import time
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, tzinfo

from sqlalchemy import BigInteger, Engine, Select, and_, bindparam, case, func, select

from aranet_monitor.db.schema import (
    CO2_LEVELS,
    ROLLUP_METRICS,
    devices,
    events,
    readings,
    readings_hourly,
)
from aranet_monitor.models import METRICS, SERIES_METRICS

# Bucket sizes (seconds) the aggregator snaps to, so bucket edges land on "nice" times.
NICE_BUCKETS: tuple[int, ...] = (
    60,
    120,
    300,
    600,
    900,
    1800,
    3600,
    7200,
    10800,
    21600,
    43200,
    86400,
    172800,
    604800,
)

RANGES: dict[str, int] = {
    "1h": 3600,
    "6h": 6 * 3600,
    "24h": 86400,
    "7d": 7 * 86400,
    "30d": 30 * 86400,
    "90d": 90 * 86400,
    "1y": 365 * 86400,
}
STATS_METRICS = ("co2", "temperature", "humidity", "pressure")  # min/mean/max in stats()
MAX_SPAN_S = 2 * 366 * 86400  # longest window one request may scan (CSV export: page by year)
HOUR = 3600
WEEK_HOURS = 168
WEEKDAYS = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")


def pick_bucket(span_s: int, max_points: int, raw_interval_s: int = 60) -> int:
    """Return the aggregation bucket (seconds) for a time span; 0 means "raw rows"."""
    if span_s <= 0:
        return 0
    if span_s / max(raw_interval_s, 1) <= max_points:
        return 0
    want = math.ceil(span_s / max_points)
    for b in NICE_BUCKETS:
        if b >= want:
            return b
    return NICE_BUCKETS[-1]


@dataclass
class Series:
    """Column-oriented time series ready for JSON / Plotly."""

    t: list[int]
    mean: dict[str, list[float | None]]
    lo: dict[str, list[float | None]]
    hi: dict[str, list[float | None]]
    bucket_s: int
    count: int

    def as_dict(self) -> dict:
        """JSON-friendly representation."""
        return {
            "bucket_s": self.bucket_s,
            "count": self.count,
            "t": self.t,
            "mean": self.mean,
            "min": self.lo,
            "max": self.hi,
        }


def _validate_metrics(metrics: Sequence[str] | None) -> list[str]:
    if not metrics:
        return list(SERIES_METRICS)
    bad = [m for m in metrics if m not in METRICS]
    if bad:
        raise ValueError(f"unknown metric(s): {', '.join(bad)}")
    return list(metrics)


def _round(v, nd: int = 2):
    return None if v is None else round(float(v), nd)


def recent_hours(days: int, now: int | None = None) -> tuple[int, int]:
    """Hour-aligned ``[start, end)`` covering the last ``days`` days up to the current hour.

    ``end`` is the end of the hour in progress, so the window holds exactly ``days * 24``
    rollup hours, including the partial current one.
    """
    now = int(time.time()) if now is None else now
    end = now - now % HOUR + HOUR
    return end - days * 86400, end


def _utc_offset(t: int, tz: tzinfo | None) -> int:
    """UTC offset (seconds) of ``tz`` at epoch ``t``; ``None`` means the system local zone."""
    dt = datetime.fromtimestamp(t, tz) if tz is not None else datetime.fromtimestamp(t).astimezone()
    return int(dt.utcoffset().total_seconds())


def _offset_segments(start: int, end: int, tz: tzinfo | None) -> list[tuple[int, int, int]]:
    """Split the hours of ``[start, end)`` into runs of constant UTC offset.

    Parameters
    ----------
    start, end
        Epoch seconds; ``start`` is snapped down to the hour.
    tz
        Display time zone (``None`` = system local).

    Returns
    -------
    list of (seg_start, seg_end, offset_s)
        Half-open, hour-aligned segments (the last one ends at ``end``). The window is probed
        a day at a time and bisected by hour inside a day whose offset changes, so a year
        costs ~370 offset lookups however many DST changes it holds.
    """
    t = start - start % HOUR
    if t >= end:
        return []
    last = (end - 1) - (end - 1) % HOUR  # last hour that starts inside the window
    seg_start, off = t, _utc_offset(t, tz)
    segments = []
    while t < last:
        probe = min(t + 86400, last)
        if _utc_offset(probe, tz) == off:
            t = probe
            continue
        lo, hi = t, probe  # offset(lo) == off != offset(hi); find the first changed hour
        while hi - lo > HOUR:
            mid = lo + (hi - lo) // 2 // HOUR * HOUR
            if _utc_offset(mid, tz) == off:
                lo = mid
            else:
                hi = mid
        segments.append((seg_start, hi, off))
        seg_start, off, t = hi, _utc_offset(hi, tz), hi
    segments.append((seg_start, end, off))
    return segments


def _heatmap_stmt(device_id: int, seg_start: int, seg_end: int, offset_s: int) -> Select:
    """Grouped CO₂ sums per local hour-of-week for one constant-offset segment.

    ``k = ((hour + offset) // 3600) % 168`` is the local hour of the week, ``k = 0`` being
    Thursday 00:00 (the Unix epoch was a Thursday). Integer ``//`` and ``%`` render the same on
    SQLite and Postgres, and the offset is a bound parameter, so one statement serves both.
    """
    h = readings_hourly.c
    k = ((h.hour + bindparam("utc_offset", offset_s, BigInteger)) // HOUR) % WEEK_HOURS
    return (
        select(k.label("k"), func.sum(h.co2_sum).label("co2_sum"), func.sum(h.co2_cnt).label("n"))
        .where(
            h.device_id == device_id,
            h.hour >= seg_start,
            h.hour < seg_end,
            h.co2_cnt > 0,
        )
        .group_by(k)
    )


class Queries:
    """Read-only query facade."""

    def __init__(self, engine: Engine):
        self.engine = engine

    # ------------------------------------------------------------------ devices
    def list_devices(self) -> list[dict]:
        """All known devices with their latest measurement time and row count."""
        r = readings.c

        def per_device(agg):  # correlated subquery: one index lookup per device, one round trip
            return select(agg(r.ts)).where(r.device_id == devices.c.id).scalar_subquery()

        stmt = select(
            devices,
            per_device(func.max).label("latest_ts"),
            per_device(func.min).label("earliest_ts"),
        ).order_by(devices.c.id)
        with self.engine.connect() as conn:
            return [dict(row) for row in conn.execute(stmt).mappings()]

    def device(self, device_id: int) -> dict | None:
        """One device row."""
        with self.engine.connect() as conn:
            row = conn.execute(select(devices).where(devices.c.id == device_id)).mappings().first()
            return dict(row) if row else None

    # ------------------------------------------------------------------ latest
    def latest(self, device_id: int) -> dict | None:
        """Most recent reading for a device, plus the one before (for trend arrows)."""
        with self.engine.connect() as conn:
            rows = (
                conn.execute(
                    select(readings)
                    .where(readings.c.device_id == device_id)
                    .order_by(readings.c.ts.desc())
                    .limit(2)
                )
                .mappings()
                .all()
            )
        if not rows:
            return None
        cur = dict(rows[0])
        cur["previous"] = dict(rows[1]) if len(rows) > 1 else None
        # Battery/RSSI only come from adverts; history rows leave them NULL.
        if cur.get("battery") is None:
            cur["battery"] = self._last_non_null(device_id, "battery")
        return cur

    def _last_non_null(self, device_id: int, col: str):
        c = readings.c[col]
        with self.engine.connect() as conn:
            return conn.execute(
                select(c)
                .where(readings.c.device_id == device_id, c.is_not(None))
                .order_by(readings.c.ts.desc())
                .limit(1)
            ).scalar()

    def latest_ts(self, device_id: int) -> int | None:
        """Cheap change-detection probe used by the dashboard's refresh timer."""
        with self.engine.connect() as conn:
            return conn.execute(
                select(func.max(readings.c.ts)).where(readings.c.device_id == device_id)
            ).scalar()

    def value_at(self, device_id: int, ts: int, tolerance_s: int = 600) -> dict | None:
        """Reading closest to (at or before) ``ts`` within ``tolerance_s``; for deltas."""
        with self.engine.connect() as conn:
            row = (
                conn.execute(
                    select(readings)
                    .where(readings.c.device_id == device_id)
                    .where(readings.c.ts.between(ts - tolerance_s, ts))
                    .order_by(readings.c.ts.desc())
                    .limit(1)
                )
                .mappings()
                .first()
            )
        return dict(row) if row else None

    # ------------------------------------------------------------------ series
    def series(
        self,
        device_id: int,
        start: int,
        end: int,
        metrics: Sequence[str] | None = None,
        max_points: int = 1500,
        raw_interval_s: int = 60,
    ) -> Series:
        """Time series between ``start`` and ``end`` (epoch s), downsampled to ``max_points``.

        Aggregated buckets carry mean/min/max so charts can draw an envelope. Gaps longer than
        2.5 buckets get a ``None`` separator so Plotly breaks the line instead of drawing a
        misleading straight segment across missing data.
        """
        metrics = _validate_metrics(metrics)
        bucket = pick_bucket(end - start, max_points, raw_interval_s)
        where = and_(readings.c.device_id == device_id, readings.c.ts.between(start, end))
        use_rollup = bucket >= 3600 and bucket % 3600 == 0 and set(metrics) <= set(ROLLUP_METRICS)
        if bucket == 0:
            cols = [readings.c.ts] + [readings.c[m] for m in metrics]
            stmt = select(*cols).where(where).order_by(readings.c.ts)
        elif use_rollup:
            h = readings_hourly.c
            b = (h.hour // bucket) * bucket
            cols = [b.label("ts"), func.sum(h.n).label("n")]
            for m in metrics:
                cols += [
                    (func.sum(h[f"{m}_sum"]) / func.nullif(func.sum(h[f"{m}_cnt"]), 0)).label(
                        f"{m}__mean"
                    ),
                    func.min(h[f"{m}_min"]).label(f"{m}__min"),
                    func.max(h[f"{m}_max"]).label(f"{m}__max"),
                ]
            stmt = (
                select(*cols)
                .where(h.device_id == device_id, h.hour.between(start - start % 3600, end))
                .group_by(b)
                .order_by(b)
            )
        else:
            b = (readings.c.ts // bucket) * bucket
            cols = [b.label("ts"), func.count().label("n")]
            for m in metrics:
                cols += [
                    func.avg(readings.c[m]).label(f"{m}__mean"),
                    func.min(readings.c[m]).label(f"{m}__min"),
                    func.max(readings.c[m]).label(f"{m}__max"),
                ]
            stmt = select(*cols).where(where).group_by(b).order_by(b)

        with self.engine.connect() as conn:
            rows = conn.execute(stmt).all()

        step = bucket or raw_interval_s
        gap = int(step * 2.5)
        t: list[int] = []
        mean: dict[str, list] = {m: [] for m in metrics}
        lo: dict[str, list] = {m: [] for m in metrics}
        hi: dict[str, list] = {m: [] for m in metrics}
        prev = None
        for row in rows:
            ts = int(row[0])
            if prev is not None and ts - prev > gap:
                t.append(prev + step)
                for m in metrics:
                    mean[m].append(None)
                    lo[m].append(None)
                    hi[m].append(None)
            prev = ts
            # Plot aggregated buckets at their centre so they line up with raw points.
            t.append(ts + bucket // 2 if bucket else ts)
            if bucket == 0:
                for i, m in enumerate(metrics, start=1):
                    v = _round(row[i])
                    mean[m].append(v)
                    lo[m].append(v)
                    hi[m].append(v)
            else:
                for i, m in enumerate(metrics):
                    base = 2 + 3 * i
                    mean[m].append(_round(row[base]))
                    lo[m].append(_round(row[base + 1]))
                    hi[m].append(_round(row[base + 2]))
        return Series(t=t, mean=mean, lo=lo, hi=hi, bucket_s=bucket, count=len(rows))

    # ------------------------------------------------------------------ stats
    def stats(
        self, device_id: int, start: int, end: int, co2_warn: int = 1000, co2_alert: int = 1400
    ) -> dict:
        """Summary statistics for a window (CO₂ exposure bands, min/mean/max per metric).

        Windows longer than 7 days are answered from the hourly rollup (hour-aligned edges)
        when the thresholds are among the pre-computed ``CO2_LEVELS``.
        """
        if end - start > 7 * 86400 and co2_warn in CO2_LEVELS and co2_alert in CO2_LEVELS:
            return self._stats_rollup(device_id, start, end, co2_warn, co2_alert)
        r = readings.c
        where = and_(r.device_id == device_id, r.ts.between(start, end))
        cols = [func.count().label("n"), func.count(r.co2).label("n_co2")]
        for m in STATS_METRICS:
            cols += [
                func.avg(r[m]).label(f"{m}_mean"),
                func.min(r[m]).label(f"{m}_min"),
                func.max(r[m]).label(f"{m}_max"),
            ]
        cols += [
            func.sum(case((r.co2 >= co2_alert, 1), else_=0)).label("n_alert"),
            func.sum(case((and_(r.co2 >= co2_warn, r.co2 < co2_alert), 1), else_=0)).label(
                "n_warn"
            ),
        ]
        with self.engine.connect() as conn:
            row = conn.execute(select(*cols).where(where)).mappings().first()
        return self._finish_stats(dict(row))

    @staticmethod
    def _finish_stats(row: dict) -> dict:
        out = {
            k: (int(v) if k.startswith("n") else _round(v)) if v is not None else None
            for k, v in row.items()
        }
        out["n"] = out.get("n") or 0
        n_co2 = out.get("n_co2") or 0
        out["pct_warn"] = round(100 * (out["n_warn"] or 0) / n_co2, 1) if n_co2 else None
        out["pct_alert"] = round(100 * (out["n_alert"] or 0) / n_co2, 1) if n_co2 else None
        return out

    def _stats_rollup(
        self, device_id: int, start: int, end: int, co2_warn: int, co2_alert: int
    ) -> dict:
        h = readings_hourly.c
        where = and_(h.device_id == device_id, h.hour.between(start - start % 3600, end))
        cols = [func.sum(h.n).label("n"), func.sum(h.co2_cnt).label("n_co2")]
        for m in STATS_METRICS:
            cols += [
                (func.sum(h[f"{m}_sum"]) / func.nullif(func.sum(h[f"{m}_cnt"]), 0)).label(
                    f"{m}_mean"
                ),
                func.min(h[f"{m}_min"]).label(f"{m}_min"),
                func.max(h[f"{m}_max"]).label(f"{m}_max"),
            ]
        cols += [
            func.sum(h[f"co2_ge_{co2_alert}"]).label("n_alert"),
            (func.sum(h[f"co2_ge_{co2_warn}"]) - func.sum(h[f"co2_ge_{co2_alert}"])).label(
                "n_warn"
            ),
        ]
        with self.engine.connect() as conn:
            row = conn.execute(select(*cols).where(where)).mappings().first()
        return self._finish_stats(dict(row))

    # ------------------------------------------------------------------ weekly pattern
    def co2_heatmap(self, device_id: int, start: int, end: int, tz: tzinfo | None) -> dict:
        """Mean CO₂ for each (local weekday, local hour) over the rollup hours in a window.

        Parameters
        ----------
        device_id
            Device to summarise.
        start, end
            Window in epoch seconds: rollup hours from ``start`` (snapped down to the hour) up
            to, but excluding, ``end``.
        tz
            Display time zone; ``None`` = system local. Hours are bucketed by local wall-clock
            time, so 14:30 lands in hour 14 in winter and in summer. In zones with a
            non-whole-hour offset each UTC hour goes to the local hour it starts in.

        Returns
        -------
        dict
            ``weekdays`` (Mon..Sun), ``hours`` (0..23), and 7 × 24 lists (row = weekday,
            column = hour): ``mean`` (ppm rounded to int, ``None`` without data) and
            ``count`` (number of readings). Means are re-aggregated exactly as
            ``Σ co2_sum / Σ co2_cnt`` across the hours (and DST segments) of each cell.
        """
        sums = [0.0] * WEEK_HOURS
        counts = [0] * WEEK_HOURS
        with self.engine.connect() as conn:
            for seg_start, seg_end, offset in _offset_segments(start, end, tz):
                for k, total, n in conn.execute(
                    _heatmap_stmt(device_id, seg_start, seg_end, offset)
                ):
                    sums[int(k)] += float(total)
                    counts[int(k)] += int(n)
        mean = [[None] * 24 for _ in WEEKDAYS]
        count = [[0] * 24 for _ in WEEKDAYS]
        for k in range(WEEK_HOURS):
            day, hour = (k // 24 + 3) % 7, k % 24  # k = 0 is Thursday 00:00
            count[day][hour] = counts[k]
            if counts[k]:
                mean[day][hour] = round(sums[k] / counts[k])
        return {"weekdays": list(WEEKDAYS), "hours": list(range(24)), "mean": mean, "count": count}

    # ------------------------------------------------------------------ export
    def iter_csv(self, device_id: int, start: int, end: int) -> Iterator[list]:
        """CSV records for raw readings: a header, then ``[time_utc, ts, source, *METRICS]``."""
        yield ["time_utc", "ts", "source", *METRICS]
        for row in self.iter_rows(device_id, start, end):
            yield [datetime.fromtimestamp(row[0], tz=UTC).isoformat(), *row]

    def iter_rows(
        self, device_id: int, start: int, end: int, chunk: int = 10000
    ) -> Iterator[tuple]:
        """Stream raw rows (ts, source, *METRICS) in time order, chunked by key."""
        cols = [readings.c.ts, readings.c.source] + [readings.c[m] for m in METRICS]
        cursor = start - 1
        while True:
            with self.engine.connect() as conn:
                batch = conn.execute(
                    select(*cols)
                    .where(readings.c.device_id == device_id)
                    .where(readings.c.ts > cursor, readings.c.ts <= end)
                    .order_by(readings.c.ts)
                    .limit(chunk)
                ).all()
            if not batch:
                return
            yield from batch
            cursor = batch[-1][0]

    # ------------------------------------------------------------------ health
    def recent_events(self, limit: int = 20) -> list[dict]:
        """Latest collector events, newest first."""
        with self.engine.connect() as conn:
            rows = (
                conn.execute(select(events).order_by(events.c.id.desc()).limit(limit))
                .mappings()
                .all()
            )
        return [dict(r) for r in rows]

    def health(self, stale_after_s: int = 300) -> dict:
        """Report overall freshness: is any device reporting recently."""
        now = int(time.time())
        devs = self.list_devices()
        status = []
        for d in devs:
            age = now - d["latest_ts"] if d["latest_ts"] else None
            # Keep in sync with web/assets/app.js (pill).
            limit = max(stale_after_s, int(2.5 * (d["interval_s"] or 60)))
            status.append(
                {
                    "id": d["id"],
                    "name": d["name"],
                    "latest_ts": d["latest_ts"],
                    "age_s": age,
                    "ok": age is not None and age <= limit,
                }
            )
        return {
            "now": now,
            "ok": bool(status) and all(s["ok"] for s in status),
            "devices": status,
            "events": self.recent_events(5),
        }
