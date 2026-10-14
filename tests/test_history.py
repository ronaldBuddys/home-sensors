import asyncio
import time

import pytest
from bleak.exc import BleakError, BleakGATTProtocolError

from aranet_monitor.ble import history

INTERVAL = 60


class FakeClient:
    def __init__(self, *a, **k):
        pass

    async def disconnect(self):
        return True


class FakeMonitor:
    """An Aranet4 history log modelled as a ring buffer of absolute record ids.

    Record ``id`` stores ``param * 1000 + id`` for every parameter, so a test can recover the
    id from any value and check timestamps and cross-parameter alignment exactly.

    logged      ids ever logged (newest id == logged)
    capacity    ring size; when ``logged > capacity`` the log is full and indices shift
    log_at      record-read numbers after which a new point is logged
    fail_reads  record-read numbers that raise "Unlikely Error" (link drop)
    fail_connects  connect() call numbers that fail ("failed to discover services")
    """

    def __init__(
        self,
        address,
        logged=10,
        capacity=10_000,
        ago=20,
        log_at=(),
        fail_reads=(),
        fail_connects=(),
    ):
        self.logged, self.capacity, self.ago = logged, capacity, ago
        self.log_at, self.fail_reads, self.fail_connects = (
            set(log_at),
            set(fail_reads),
            set(fail_connects),
        )
        self.reads = self.connects = 0
        self.device = FakeClient()
        self.clock = float(int(time.time()))  # virtual wall clock shared with the code under test

    @property
    def total(self):
        return min(self.logged, self.capacity)

    async def connect(self):
        self.connects += 1
        if self.connects in self.fail_connects:
            raise BleakError("failed to discover services, device disconnected")

    async def get_name(self):
        return "Aranet4 12345"

    async def get_version(self):
        return "v1.4.19"

    async def get_interval(self):
        return INTERVAL

    async def get_seconds_since_update(self):
        return self.ago

    async def get_total_readings(self):
        return self.total

    async def get_records(self, param, log_size, start, end):
        self.reads += 1
        if self.reads in self.fail_reads:
            raise BleakGATTProtocolError(14)  # "Unlikely Error", as seen on the real device
        vals = [-1] * log_size
        first_id = self.logged - self.total + 1
        for i in range(start, min(end, self.total) + 1):  # 1-based index
            vals[i - 1] = int(param) * 1000 + first_id + i - 1
        self.clock += 4  # reading takes a few (virtual) seconds
        self.ago += 4
        if self.reads in self.log_at:
            # The next point is logged exactly one interval after the previous one.
            self.clock += INTERVAL - self.ago
            self.logged += 1
            self.ago = 0
        return vals


@pytest.fixture(autouse=True)
def _fast_reconnect(monkeypatch):
    monkeypatch.setattr(history, "RECONNECT_PAUSE_S", 0)


@pytest.fixture
def fake(monkeypatch):
    holder = {}

    def factory(address, **kw):
        m = FakeMonitor(address, **holder.get("kw", {}))
        holder["m"] = m
        return m

    class VirtualTime:
        @staticmethod
        def time():
            return holder["m"].clock if "m" in holder else time.time()

        monotonic = staticmethod(__import__("time").monotonic)

    monkeypatch.setattr(history, "time", VirtualTime)
    monkeypatch.setattr(history, "Aranet4", factory)
    monkeypatch.setattr(history, "BleakClient", FakeClient)
    return holder


def run(**kw):
    chunks = []
    res = asyncio.run(
        history.fetch_history(
            "AA:BB:CC:00:11:22",
            kw.pop("since", None),
            timeout=5,
            on_chunk=lambda info, rows: chunks.append(rows),
            **kw,
        )
    )
    return res, chunks


def assert_consistent(readings, newest_id):
    """Every reading: params agree on the id, and ts matches the id's position in time."""
    ids = []
    anchors = set()
    for r in readings:
        rid = r.co2 - 4000
        assert r.temperature - 1000 == rid and r.humidity - 2000 == rid
        assert r.pressure - 3000 == pytest.approx(rid)
        ids.append(rid)
        anchors.add(r.ts + (newest_id - rid) * INTERVAL)
    # All readings imply the same "newest record time" (+-2 s of wall-clock drift).
    assert max(anchors) - min(anchors) <= 2, anchors
    assert ids == sorted(ids) and len(ids) == len(set(ids)), "duplicates or disorder"
    return ids


def test_downloads_records_after_since(fake):
    now = int(time.time())
    newest = now - 20
    res, _ = run(since=newest - 3 * 60)
    assert [r.co2 - 4000 for r in res.readings] == [8, 9, 10]
    assert all(abs(r.ts - (newest - (10 - (r.co2 - 4000)) * 60)) <= 1 for r in res.readings)
    assert res.total_on_device == 10 and res.requested_from_index == 8


def test_chunks_are_streamed_and_consistent(fake):
    fake["kw"] = {"logged": 25}
    res, chunks = run(chunk_size=10)
    assert [len(c) for c in chunks] == [10, 10, 5]
    assert assert_consistent(res.readings, 25) == list(range(1, 26))


def test_growing_log_needs_no_reread(fake):
    fake["kw"] = {"logged": 25, "log_at": {6}}  # new point appended mid-chunk-2
    res, chunks = run(chunk_size=10)
    assert assert_consistent(res.readings, 25) == list(range(1, 26))
    assert fake["m"].reads == 12  # 3 chunks x 4 params, nothing re-read


def test_full_ring_buffer_shift_is_reanchored(fake):
    # Full log of 25; a new point logged while chunk 2 is read shifts every index by one.
    fake["kw"] = {"logged": 30, "capacity": 25, "log_at": {6}}
    res, chunks = run(chunk_size=10)
    ids = assert_consistent(res.readings, fake["m"].logged)
    # Oldest id 6 was evicted after chunk 1 was read; nothing missing from there on.
    assert ids == list(range(6, 32))
    assert fake["m"].reads == 16  # chunk 2 re-read once


def test_link_drop_reconnects_and_resumes(fake):
    fake["kw"] = {"logged": 25, "fail_reads": {6, 7}}
    res, chunks = run(chunk_size=10)
    assert assert_consistent(res.readings, 25) == list(range(1, 26))
    assert fake["m"].connects == 3  # initial + 2 reconnects


def test_failed_reconnect_counts_as_a_retry(fake):
    fake["kw"] = {"logged": 25, "fail_reads": {6}, "fail_connects": {2}}
    res, _ = run(chunk_size=10)
    assert len(res.readings) == 25 and fake["m"].connects == 3


def test_persistent_link_failure_raises_but_keeps_verified_chunks(fake):
    fake["kw"] = {"logged": 25, "fail_reads": set(range(5, 100))}
    stored = []
    with pytest.raises(BleakGATTProtocolError):
        asyncio.run(
            history.fetch_history(
                "AA:BB:CC:00:11:22",
                None,
                timeout=5,
                chunk_size=10,
                on_chunk=lambda info, rows: stored.extend(rows),
            )
        )
    assert [r.co2 - 4000 for r in stored] == list(range(1, 11))


def test_nothing_new(fake):
    res, chunks = run(since=int(time.time()))
    assert res.readings == [] and res.requested_from_index == 0 and chunks == []


def test_initial_connect_failure_is_retried(fake):
    fake["kw"] = {"logged": 5, "fail_connects": {1, 2}}
    res, _ = run()
    assert len(res.readings) == 5 and fake["m"].connects == 3
