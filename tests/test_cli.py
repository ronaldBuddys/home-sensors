import csv
import io

from aranet_monitor import cli
from aranet_monitor.sim.simulator import Aranet4Simulator


def test_generate_accepts_float_bounds():
    rows = list(Aranet4Simulator().generate(0.0, 600.5))
    assert len(rows) == 11 and rows[0].ts == 0


def test_seed_export_status(tmp_path, monkeypatch, capsys):
    url = f"sqlite:///{tmp_path}/cli.sqlite"
    monkeypatch.setenv("ARANET_DB_URL", url)
    assert cli.main(["seed", "--days", "0.5"]) == 0
    assert "inserted 721 rows" in capsys.readouterr().out
    assert cli.main(["export"]) == 0
    rows = list(csv.reader(io.StringIO(capsys.readouterr().out)))
    assert rows[0][:4] == ["time_utc", "ts", "source", "co2"] and len(rows) == 722
    assert cli.main(["status"]) == 0
    assert '"devices"' in capsys.readouterr().out
    assert cli.main(["rollup"]) == 0
    assert "rebuilt 13 hourly rows" in capsys.readouterr().out


def test_db_init_hides_password(monkeypatch, capsys, tmp_path):
    monkeypatch.setenv("ARANET_DB_URL", f"sqlite:///{tmp_path}/x.sqlite")
    assert cli.main(["db-init"]) == 0
    assert cli._safe_url("postgresql+psycopg://u:secret@h/db") == "postgresql+psycopg://u:***@h/db"


def test_copy_db_is_idempotent(tmp_path, capsys):
    a, b = f"sqlite:///{tmp_path}/a.sqlite", f"sqlite:///{tmp_path}/b.sqlite"
    assert cli.main(["seed", "--db", a, "--days", "1"]) == 0
    capsys.readouterr()
    assert cli.main(["copy-db", "--source", a, "--target", b]) == 0
    assert "1441 new reading(s)" in capsys.readouterr().out
    assert cli.main(["copy-db", "--source", a, "--target", b]) == 0
    assert "0 new reading(s)" in capsys.readouterr().out
    from aranet_monitor.db.queries import Queries
    from aranet_monitor.db.store import make_engine

    qa, qb = Queries(make_engine(a)), Queries(make_engine(b))
    sa = qa.stats(1, 0, 2**40)
    sb = qb.stats(1, 0, 2**40)
    assert sa["n"] == sb["n"] and sa["co2_mean"] == sb["co2_mean"]


def test_copy_db_keeps_rows_after_interval_change(tmp_path):
    from aranet_monitor.db.store import Store, copy_db, init_db, make_engine
    from aranet_monitor.models import DeviceInfo, Reading

    a, b = f"sqlite:///{tmp_path}/a.sqlite", f"sqlite:///{tmp_path}/b.sqlite"
    eng = make_engine(a)
    init_db(eng)
    st = Store(eng)
    dev = st.upsert_device(DeviceInfo("AA:BB:CC:00:11:22", "Aranet4 12345", interval_s=60))
    st.bulk_insert_raw(
        dev, [Reading("AA:BB:CC:00:11:22", t * 60, "adv", co2=500) for t in range(1440)]
    )
    st.upsert_device(DeviceInfo("AA:BB:CC:00:11:22", interval_s=600))  # interval changed later
    assert copy_db(a, b)["readings"] == 1440


def test_copy_db_target_defaults_to_configured_db(tmp_path, monkeypatch, capsys):
    a, b = f"sqlite:///{tmp_path}/a.sqlite", f"sqlite:///{tmp_path}/b.sqlite"
    assert cli.main(["seed", "--db", a, "--days", "0.1"]) == 0
    monkeypatch.setenv("ARANET_DB_URL", b)
    capsys.readouterr()
    assert cli.main(["copy-db", "--source", a]) == 0
    assert "new reading(s)" in capsys.readouterr().out
    assert cli.main(["copy-db"]) == 2
    assert cli.main(["copy-db", "--source", b]) == 2  # same as configured target


def test_postgres_url_escapes_password(monkeypatch):
    from aranet_monitor.config import Settings

    monkeypatch.delenv("ARANET_DB_URL", raising=False)
    monkeypatch.setenv("POSTGRES_HOST", "db")
    monkeypatch.setenv("POSTGRES_PASSWORD", "p@ss:w/rd#1")
    monkeypatch.setenv("POSTGRES_DB", "sensors_db")
    from sqlalchemy.engine import make_url

    u = make_url(Settings.from_env().db_url)
    assert u.password == "p@ss:w/rd#1" and u.host == "db" and u.database == "sensors_db"
