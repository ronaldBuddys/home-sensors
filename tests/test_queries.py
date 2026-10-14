import pytest

from aranet_monitor.db.queries import pick_bucket
from conftest import make_reading


def test_pick_bucket():
    assert pick_bucket(3600, 1500) == 0  # 60 raw points -> raw
    assert pick_bucket(86400, 1500) == 0  # 1440 points -> raw
    assert pick_bucket(7 * 86400, 1500) == 600
    assert pick_bucket(365 * 86400, 1500) == 21600  # 6 h -> 1460 points
    assert pick_bucket(0, 1500) == 0


def test_raw_series(store, queries, device_id):
    store.insert_readings(device_id, [make_reading(t, co2=400 + t // 60) for t in range(0, 600, 60)], 60)
    s = queries.series(device_id, 0, 600, ["co2"])
    assert s.bucket_s == 0 and len(s.t) == 10
    assert s.mean["co2"][0] == 400 and s.mean["co2"][-1] == 409


def test_bucketed_series_has_envelope(store, queries, device_id):
    rows = [make_reading(t, co2=400 + (t // 60) % 10) for t in range(0, 7 * 86400, 60)]
    store.bulk_insert_raw(device_id, rows)
    s = queries.series(device_id, 0, 7 * 86400, ["co2", "temperature"], max_points=1500)
    assert s.bucket_s == 600
    assert len(s.t) <= 1500
    assert s.lo["co2"][0] == 400 and s.hi["co2"][0] == 409
    assert s.mean["co2"][0] == pytest.approx(404.5)
    assert s.t[0] == 300  # bucket centre


def test_gaps_break_lines(store, queries, device_id):
    pts = [make_reading(t) for t in (0, 60, 120, 3000, 3060)]
    store.insert_readings(device_id, pts, 60)
    s = queries.series(device_id, 0, 4000, ["co2"])
    assert None in s.mean["co2"]
    assert len(s.t) == 6


def test_unknown_metric_rejected(queries, device_id):
    with pytest.raises(ValueError):
        queries.series(device_id, 0, 10, ["co2; drop table readings"])


def test_latest_and_stats(store, queries, device_id):
    store.insert_readings(device_id, [make_reading(0, co2=900, battery=50), make_reading(60, co2=1500)], 60)
    latest = queries.latest(device_id)
    assert latest["co2"] == 1500 and latest["previous"]["co2"] == 900
    assert latest["battery"] == 50  # falls back to last known battery
    st = queries.stats(device_id, 0, 100)
    assert st["n"] == 2 and st["co2_max"] == 1500 and st["pct_alert"] == 50.0


def test_iter_rows_chunks(store, queries, device_id):
    store.bulk_insert_raw(device_id, [make_reading(t * 60) for t in range(25)])
    rows = list(queries.iter_rows(device_id, 0, 10**9, chunk=10))
    assert len(rows) == 25 and rows[0][0] == 0


def test_health(store, queries, device_id):
    import time

    store.insert_readings(device_id, [make_reading(int(time.time()) - 30)], 60)
    h = queries.health()
    assert h["ok"] and h["devices"][0]["age_s"] < 100
