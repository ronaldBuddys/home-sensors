import time

import pytest
from fastapi.testclient import TestClient

from aranet_monitor.api.app import create_app
from aranet_monitor.config import Settings
from aranet_monitor.db.store import Store, init_db, make_engine
from aranet_monitor.models import DeviceInfo
from aranet_monitor.sim.simulator import Aranet4Simulator


@pytest.fixture(scope="module")
def client(tmp_path_factory):
    url = f"sqlite:///{tmp_path_factory.mktemp('api')}/api.sqlite"
    eng = make_engine(url)
    init_db(eng)
    st = Store(eng)
    sim = Aranet4Simulator()
    dev = st.upsert_device(DeviceInfo(sim.address, "Aranet4 SIM", "Aranet4", "sim", 60))
    now = int(time.time())
    st.bulk_insert_raw(dev, sim.generate(now - 10 * 86400, now - 30))
    st.log_event("start", "test", dev)
    return TestClient(create_app(Settings(db_url=url)))


def test_devices_and_latest(client):
    devs = client.get("/api/v1/devices").json()
    assert len(devs) == 1 and devs[0]["name"] == "Aranet4 SIM"
    lat = client.get(f"/api/v1/devices/{devs[0]['id']}/latest").json()
    assert 400 <= lat["co2"] < 5000 and lat["age_s"] < 120


def test_readings_ranges(client):
    raw = client.get("/api/v1/devices/1/readings?range=1h&metrics=co2").json()
    assert raw["bucket_s"] == 0 and 55 <= len(raw["t"]) <= 61
    assert list(raw["mean"]) == ["co2"] and raw["units"]["co2"] == "ppm"
    week = client.get("/api/v1/devices/1/readings?range=7d").json()
    assert week["bucket_s"] == 600 and len(week["t"]) <= 1500
    assert set(week["mean"]) >= {"co2", "temperature", "humidity", "pressure"}


def test_readings_validation(client):
    assert client.get("/api/v1/devices/1/readings?range=2w").status_code == 422
    assert client.get("/api/v1/devices/1/readings?metrics=nope").status_code == 422
    assert client.get("/api/v1/devices/1/readings?start=10&end=5").status_code == 422
    assert client.get("/api/v1/devices/99/readings").status_code == 404


def test_stats_health_events(client):
    st = client.get("/api/v1/devices/1/stats?range=7d").json()
    assert st["n"] > 9000 and st["co2_min"] < st["co2_mean"] < st["co2_max"]
    h = client.get("/api/v1/health").json()
    assert h["ok"] is True
    assert client.get("/api/v1/events").json()[0]["kind"] == "start"


def test_csv_export_streams(client):
    r = client.get("/api/v1/devices/1/export.csv?range=24h")
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/csv")
    lines = r.text.strip().splitlines()
    assert lines[0].startswith("time_utc,ts,source,co2")
    assert 1400 <= len(lines) - 1 <= 1441


def test_dash_is_mounted(client):
    assert client.get("/").status_code == 200
    layout = client.get("/_dash-layout")
    assert layout.status_code == 200 and "kpis" in layout.text
    assert client.get("/api/docs").status_code == 200
    assert client.get("/healthz").json() == {"ok": True}


def test_basic_auth(tmp_path):
    import base64

    url = f"sqlite:///{tmp_path}/auth.sqlite"
    init_db(make_engine(url))
    c = TestClient(create_app(Settings(db_url=url, web_password="s3cret")))
    assert c.get("/api/v1/devices").status_code == 401
    assert c.get("/").status_code == 401
    assert c.get("/healthz").status_code == 200
    bad = base64.b64encode(b"aranet:nope").decode()
    assert c.get("/api/v1/devices", headers={"Authorization": f"Basic {bad}"}).status_code == 401
    good = base64.b64encode(b"aranet:s3cret").decode()
    assert c.get("/api/v1/devices", headers={"Authorization": f"Basic {good}"}).status_code == 200
    assert c.get("/", headers={"Authorization": f"Basic {good}"}).status_code == 200
    assert "s3cret" not in repr(Settings(web_password="s3cret"))


def test_web_app_does_not_create_tables(tmp_path):
    from sqlalchemy import inspect

    url = f"sqlite:///{tmp_path}/empty.sqlite"
    with pytest.raises(RuntimeError, match="db-init"):
        create_app(Settings(db_url=url))
    assert inspect(make_engine(url)).get_table_names() == []
