import csv
import io

from aranet_monitor import cli
from aranet_monitor.sim.simulator import Aranet4Simulator


def test_generate_accepts_float_bounds():
    rows = list(Aranet4Simulator().generate(0.0, 600.5))
    assert len(rows) == 11 and rows[0].ts == 0


def test_seed_export_status(db, monkeypatch, capsys):
    db.use_env(monkeypatch)
    assert cli.main(["seed", "--days", "0.5"]) == 0
    assert "inserted 721 rows" in capsys.readouterr().out
    assert cli.main(["export"]) == 0
    rows = list(csv.reader(io.StringIO(capsys.readouterr().out)))
    assert rows[0][:4] == ["time_utc", "ts", "source", "co2"] and len(rows) == 722
    assert rows[1][0].endswith("+00:00")  # the header says UTC, so the values are UTC
    assert cli.main(["status"]) == 0
    assert '"devices"' in capsys.readouterr().out
    assert cli.main(["rollup"]) == 0
    assert "rebuilt 13 hourly rows" in capsys.readouterr().out


def test_db_init_hides_password(monkeypatch, capsys, db):
    db.use_env(monkeypatch)
    assert cli.main(["db-init"]) == 0
    assert cli._safe_url("postgresql+psycopg://u:secret@h/db") == "postgresql+psycopg://u:***@h/db"


def test_copy_db_is_idempotent(tmp_path, db, capsys):
    # --source takes a URL only (no schema), so the source is a SQLite file; the target is
    # the engine under test (a SQLite file, or a fresh Postgres schema).
    a = f"sqlite:///{tmp_path}/a.sqlite"
    assert cli.main(["seed", "--db", a, "--days", "1"]) == 0
    capsys.readouterr()
    assert cli.main(["copy-db", "--source", a, *db.target_args()]) == 0
    assert "1441 new reading(s)" in capsys.readouterr().out
    assert cli.main(["copy-db", "--source", a, *db.target_args()]) == 0
    assert "0 new reading(s)" in capsys.readouterr().out
    from aranet_monitor.db.queries import Queries
    from aranet_monitor.db.store import make_engine

    qa, qb = Queries(make_engine(a)), Queries(db.engine())
    sa = qa.stats(1, 0, 2**40)
    sb = qb.stats(1, 0, 2**40)
    assert sa["n"] == sb["n"] and sa["co2_mean"] == sb["co2_mean"]


def test_copy_db_keeps_rows_after_interval_change(tmp_path, db):
    from aranet_monitor.db.store import Store, copy_db, init_db, make_engine
    from aranet_monitor.models import DeviceInfo, Reading

    a = f"sqlite:///{tmp_path}/a.sqlite"
    eng = make_engine(a)
    init_db(eng)
    st = Store(eng)
    dev = st.upsert_device(DeviceInfo("AA:BB:CC:00:11:22", "Aranet4 12345", interval_s=60))
    st.bulk_insert_raw(
        dev, [Reading("AA:BB:CC:00:11:22", t * 60, "adv", co2=500) for t in range(1440)]
    )
    st.upsert_device(DeviceInfo("AA:BB:CC:00:11:22", interval_s=600))  # interval changed later
    assert copy_db(a, db.url, target_schema=db.schema)["readings"] == 1440


def test_copy_db_target_defaults_to_configured_db(tmp_path, db, monkeypatch, capsys):
    a = f"sqlite:///{tmp_path}/a.sqlite"
    assert cli.main(["seed", "--db", a, "--days", "0.1"]) == 0
    db.use_env(monkeypatch)
    capsys.readouterr()
    assert cli.main(["copy-db", "--source", a]) == 0
    assert "new reading(s)" in capsys.readouterr().out
    assert cli.main(["copy-db"]) == 2


def test_copy_db_rejects_same_source_and_target(tmp_path, monkeypatch):
    # SQLite-only: --source cannot carry a schema, so on Postgres it is never "the same".
    b = f"sqlite:///{tmp_path}/b.sqlite"
    monkeypatch.setenv("ARANET_DB_URL", b)
    assert cli.main(["copy-db", "--source", b]) == 2  # same as configured target


def test_postgres_url_escapes_password(monkeypatch):
    from aranet_monitor.config import Settings

    monkeypatch.delenv("ARANET_DB_URL", raising=False)
    monkeypatch.setenv("POSTGRES_HOST", "db")
    monkeypatch.setenv("POSTGRES_PASSWORD", "p@ss:w/rd#1")
    monkeypatch.setenv("POSTGRES_DB", "sensors")
    from sqlalchemy.engine import make_url

    u = make_url(Settings.from_env().db_url)
    assert u.password == "p@ss:w/rd#1" and u.host == "db" and u.database == "sensors"


def test_settings_repr_hides_db_password(monkeypatch):
    from aranet_monitor.config import Settings

    monkeypatch.setenv("POSTGRES_HOST", "db")
    monkeypatch.setenv("POSTGRES_PASSWORD", "hunter2-secret")
    monkeypatch.setenv("ARANET_WEB_PASSWORD", "web-secret")
    text = repr(Settings.from_env())
    assert "hunter2-secret" not in text and "web-secret" not in text


def test_postgres_port_must_be_numeric(monkeypatch):
    import pytest

    from aranet_monitor.config import Settings

    monkeypatch.setenv("POSTGRES_HOST", "db")
    monkeypatch.setenv("POSTGRES_PORT", "54x")
    with pytest.raises(ValueError, match="POSTGRES_PORT"):
        Settings.from_env()


def test_env_example_does_not_override_postgres_parts():
    from pathlib import Path

    example = Path(__file__).resolve().parents[1] / ".env.example"
    active = [ln for ln in example.read_text().splitlines() if ln.startswith("ARANET_DB_URL=")]
    assert active == []


def test_serve_refuses_lan_bind_without_password(monkeypatch, db, capsys):
    db.use_env(monkeypatch)
    assert cli.main(["serve", "--host", "0.0.0.0"]) == 2
    assert "refusing" in capsys.readouterr().err
    assert cli._is_loopback("127.0.0.1") and cli._is_loopback("::1")
    assert cli._is_loopback("localhost") and not cli._is_loopback("0.0.0.0")


def test_postgres_sslmode_from_env(monkeypatch):
    from sqlalchemy.engine import make_url

    from aranet_monitor.config import Settings

    monkeypatch.setenv("POSTGRES_HOST", "db")
    monkeypatch.setenv("POSTGRES_PASSWORD", "x")
    assert "sslmode" not in make_url(Settings.from_env().db_url).query
    monkeypatch.setenv("POSTGRES_SSLMODE", "verify-full")
    monkeypatch.setenv("POSTGRES_SSLROOTCERT", "/etc/ssl/home-ca.pem")
    q = make_url(Settings.from_env().db_url).query
    assert q == {"sslmode": "verify-full", "sslrootcert": "/etc/ssl/home-ca.pem"}


def test_read_only_commands_do_not_create_tables(db, monkeypatch, capsys):
    import pytest
    from sqlalchemy import inspect

    db.use_env(monkeypatch)
    for cmd in (["status"], ["export"]):
        with pytest.raises(RuntimeError, match="db-init"):
            cli.main(cmd)
    assert inspect(db.engine()).get_table_names(schema=db.schema) == []


def test_serve_needs_both_tls_files(monkeypatch, db, capsys):
    db.use_env(monkeypatch)
    monkeypatch.setenv("ARANET_WEB_TLS_CERT", "/nonexistent/web.crt")
    assert cli.main(["serve"]) == 2
    assert "ARANET_WEB_TLS_KEY" in capsys.readouterr().err


def test_grant_read_checks_backend_and_role(monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("ARANET_DB_URL", f"sqlite:///{tmp_path}/x.sqlite")
    assert cli.main(["grant-read", "dashboard_ro"]) == 2
    assert "Postgres" in capsys.readouterr().err
    monkeypatch.setenv("ARANET_DB_URL", "postgresql+psycopg://u:p@localhost:1/db")
    assert cli.main(["grant-read", 'x"; DROP TABLE readings; --']) == 2
    assert "invalid role" in capsys.readouterr().err


def test_settings_wait_clock_sync_from_env(monkeypatch):
    from aranet_monitor.config import Settings

    assert Settings().wait_clock_sync_s == 0
    monkeypatch.setenv("ARANET_WAIT_CLOCK_SYNC_S", "120")
    assert Settings.from_env().wait_clock_sync_s == 120


def _ts(store, dev):
    from sqlalchemy import text

    with store.engine.connect() as c:
        return [
            r[0]
            for r in c.execute(
                text("select ts from readings where device_id=:d order by ts"), {"d": dev}
            )
        ]


def test_sim_device_seeds_then_tops_up(db, monkeypatch):
    store = cli._store(db.settings())
    now = 1_700_000_000
    monkeypatch.setattr(cli.time, "time", lambda: now)
    dev, sim = cli._sim_device(store, 60, 0.5)
    assert store.last_ts(dev) is not None
    assert abs(len(_ts(store, dev)) - 0.5 * 86400 / 60) <= 2
    monkeypatch.setattr(cli.time, "time", lambda: now + 3600)
    dev2, _ = cli._sim_device(store, 60, 0.5)
    assert dev2 == dev
    ts = _ts(store, dev)
    assert abs(len(ts) - (0.5 * 86400 + 3600) / 60) <= 3
    assert max(b - a for a, b in zip(ts, ts[1:], strict=False)) <= 120


def _stub_web(monkeypatch):
    seen = []
    monkeypatch.setattr(
        cli, "_run_web", lambda settings, host, port: seen.append((settings, host, port))
    )
    return seen


def test_demo_never_uses_configured_db(db, tmp_path, monkeypatch):
    monkeypatch.setenv("ARANET_DB_URL", f"sqlite:///{tmp_path}/real.sqlite")
    monkeypatch.setenv("ARANET_WEB_PASSWORD", "secret")
    seen = _stub_web(monkeypatch)
    b = f"sqlite:///{tmp_path}/demo_b.sqlite"
    assert cli.main(["demo", "--db", b, "--days", "0.01", "--port", "8123"]) == 0
    settings, host, port = seen[-1]
    assert settings.db_url == b and settings.web_password is None
    assert (host, port) == ("127.0.0.1", 8123)
    assert not (tmp_path / "real.sqlite").exists()

    monkeypatch.chdir(tmp_path)
    (tmp_path / "data").mkdir()
    assert cli.main(["demo", "--days", "0.01"]) == 0
    assert seen[-1][0].db_url == "sqlite:///data/demo.sqlite"
    assert (tmp_path / "data" / "demo.sqlite").exists()
    assert not (tmp_path / "real.sqlite").exists()


def test_demo_parser_defaults():
    args = cli.build_parser().parse_args(["demo"])
    assert args.days == 30 and args.interval == 60 and args.db is None and args.port is None
