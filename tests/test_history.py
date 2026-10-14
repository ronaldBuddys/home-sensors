import asyncio
import time

import pytest

from aranet_monitor.ble import history


class FakeClient:
    def __init__(self, *a, **k):
        pass

    async def disconnect(self):
        return True


class FakeMonitor:
    """Simulates an Aranet4 whose log grows by one point after `grow_after` record reads."""

    def __init__(self, address, total=10, ago=20, grow_after=None):
        self.total, self.ago, self.grow_after, self.reads = total, ago, grow_after, 0
        self.device = None

    async def connect(self):
        pass

    async def get_name(self):
        return "Aranet4 12345"

    async def get_version(self):
        return "v1.4.19"

    async def get_interval(self):
        return 60

    async def get_seconds_since_update(self):
        return self.ago

    async def get_total_readings(self):
        return self.total

    async def get_records(self, param, log_size, start, end):
        self.reads += 1
        if self.grow_after is not None and self.reads == self.grow_after:
            self.total += 1
            self.ago = 1
            self.grow_after = None
        return [-1] * (start - 1) + [
            400 + i + int(param) * 1000 for i in range(start - 1, log_size)
        ]


@pytest.fixture
def fake(monkeypatch):
    holder = {}

    def factory(address, **kw):
        m = FakeMonitor(address, **holder.get("kw", {}))
        holder["m"] = m
        return m

    monkeypatch.setattr(history, "Aranet4", factory)
    monkeypatch.setattr(history, "BleakClient", FakeClient)
    return holder


def test_downloads_records_after_since(fake):
    now = int(time.time())
    newest = now - 20
    since = newest - 3 * 60  # we have everything up to 3 intervals ago
    res = asyncio.run(history.fetch_history("AA:BB:CC:00:11:22", since, timeout=5))
    got = [r.ts for r in res.readings]
    want = [newest - 120, newest - 60, newest]
    assert all(abs(a - b) <= 1 for a, b in zip(got, want, strict=True))
    assert res.readings[-1].co2 == 4000 + 400 + 9 and res.readings[-1].temperature == 1000 + 409
    assert res.total_on_device == 10 and res.requested_from_index == 8


def test_retries_when_log_advances_mid_download(fake):
    fake["kw"] = {"grow_after": 2}
    res = asyncio.run(history.fetch_history("AA:BB:CC:00:11:22", None, timeout=5))
    assert res.total_on_device == 11 and len(res.readings) == 11
    assert fake["m"].reads == 4 + 4  # first attempt discarded, second complete


def test_nothing_new(fake):
    res = asyncio.run(history.fetch_history("AA:BB:CC:00:11:22", int(time.time()), timeout=5))
    assert res.readings == [] and res.requested_from_index == 0
