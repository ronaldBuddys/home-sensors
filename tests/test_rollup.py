import pytest
from sqlalchemy import func, select

from aranet_monitor.db.queries import pick_bucket
from aranet_monitor.db.schema import readings_hourly
from aranet_monitor.sim.simulator import Aranet4Simulator
from conftest import make_reading

DAY = 86400


@pytest.fixture
def seeded(store, device_id):
    sim = Aranet4Simulator(seed=1)
    start = 1_700_000_000 - 1_700_000_000 % DAY
    rows = [
        r.__class__(**{**r.__dict__, "address": "x"})
        for r in sim.generate(start, start + 120 * DAY)
    ]
    store.bulk_insert_raw(device_id, rows)
    return start


def test_rollup_matches_raw_aggregation(store, queries, device_id, seeded):
    start, end = seeded, seeded + 120 * DAY
    assert pick_bucket(end - start, 1500) >= 3600
    via_rollup = queries.series(device_id, start, end, ["co2", "temperature"])
    # Force the raw path by asking for a metric that is not rolled up alongside.
    via_raw = queries.series(device_id, start, end, ["co2", "temperature", "status"])
    assert via_rollup.t == via_raw.t
    for m in ("co2", "temperature"):
        assert via_rollup.lo[m] == via_raw.lo[m]
        assert via_rollup.hi[m] == via_raw.hi[m]
        assert via_rollup.mean[m] == pytest.approx(via_raw.mean[m], abs=0.011)


def test_rollup_stats_match_raw(queries, device_id, seeded):
    start, end = seeded, seeded + 30 * DAY - 1
    fast = queries.stats(device_id, start, end)  # > 7 d -> rollup
    slow = queries.stats(device_id, start, end, co2_warn=1001, co2_alert=1401)  # raw path
    assert fast["n"] == slow["n"]
    assert fast["co2_max"] == slow["co2_max"]
    assert fast["co2_mean"] == pytest.approx(slow["co2_mean"], abs=0.01)
    assert fast["pct_alert"] == pytest.approx(slow["pct_alert"], abs=0.2)


def test_incremental_insert_updates_rollup(store, engine, device_id):
    store.insert_readings(device_id, [make_reading(7200, co2=500)], 60)
    store.insert_readings(device_id, [make_reading(7260, co2=1500)], 60)
    with engine.connect() as c:
        row = (
            c.execute(select(readings_hourly).where(readings_hourly.c.hour == 7200))
            .mappings()
            .one()
        )
    assert row["n"] == 2 and row["co2_min"] == 500 and row["co2_max"] == 1500
    assert row["co2_ge_1400"] == 1 and row["co2_sum"] == 2000


def test_rebuild_rollup(store, engine, device_id):
    store.insert_readings(device_id, [make_reading(t) for t in range(0, 4 * 3600, 60)], 60)
    with engine.begin() as c:
        c.execute(readings_hourly.delete())
    assert store.rebuild_rollup() == 4
    with engine.connect() as c:
        assert c.execute(select(func.sum(readings_hourly.c.n))).scalar() == 240
