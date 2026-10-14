from sqlalchemy import func, select

from aranet_monitor.db.schema import readings
from aranet_monitor.models import DeviceInfo
from conftest import ADDR, make_reading


def count(engine):
    with engine.connect() as c:
        return c.execute(select(func.count()).select_from(readings)).scalar()


def test_upsert_device_is_idempotent(store):
    a = store.upsert_device(DeviceInfo(ADDR, "n1"), seen=1)
    b = store.upsert_device(DeviceInfo(ADDR.lower(), "n2", firmware="v9"), seen=2)
    assert a == b
    assert store.interval_for(ADDR) == 60


def test_window_dedupe_skips_jittered_duplicates(store, engine, device_id):
    assert store.insert_readings(device_id, [make_reading(1000)], 60) == 1
    # Same measurement heard again with +-1..2 s jitter (advert vs history clock maths).
    assert store.insert_readings(device_id, [make_reading(1001), make_reading(998)], 60) == 0
    # Next real measurement 60 s later is kept.
    assert store.insert_readings(device_id, [make_reading(1060)], 60) == 1
    assert count(engine) == 2


def test_history_batch_merges_with_adverts(store, engine, device_id):
    store.insert_readings(device_id, [make_reading(t) for t in (1000, 1060, 1120)], 60)
    # History says the same points with +1 s skew, plus two older ones and one gap-fill.
    hist = [make_reading(t + 1, source="history") for t in (880, 940, 1000, 1060, 1120, 1180)]
    assert store.insert_readings(device_id, hist, 60) == 3
    with engine.connect() as c:
        ts = c.execute(select(readings.c.ts).order_by(readings.c.ts)).scalars().all()
    assert ts == [881, 941, 1000, 1060, 1120, 1181]


def test_batch_internal_duplicates_collapse(store, engine, device_id):
    assert store.insert_readings(device_id, [make_reading(500), make_reading(501)], 60) == 1


def test_large_batch_chunks(store, engine, device_id):
    batch = [make_reading(i * 60) for i in range(2100)]
    assert store.insert_readings(device_id, batch, 60) == 2100
    assert count(engine) == 2100


def test_last_ts_and_events(store, device_id, queries):
    assert store.last_ts(device_id) is None
    store.insert_readings(device_id, [make_reading(100), make_reading(200)], 60)
    assert store.last_ts(device_id) == 200
    store.log_event("start", "hello", device_id)
    assert queries.recent_events(1)[0]["kind"] == "start"
