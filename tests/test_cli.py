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
