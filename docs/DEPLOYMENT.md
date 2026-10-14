# Deployment

## 1. Prerequisites

- **Linux with BlueZ** (`bluetoothd` running) and a BLE adapter.
  - On the development host that's a TP-Link UB5A USB dongle (`hci0`).
  - On a Raspberry Pi 3, 4, 5 or Zero 2 W the built-in radio works.
- **[uv](https://docs.astral.sh/uv/)**. It installs Python 3.12 itself if needed.
- **No root needed.** BLE scanning through BlueZ D-Bus works as a normal user.

```bash
git clone <repo> aranet4-monitor && cd aranet4-monitor
uv sync --frozen --no-dev            # add --extra postgres for Postgres
cp .env.example .env                 # edit as needed
```

## 2. Sensor setup (one-time, Aranet Home phone app)

**Settings → Smart Home integrations → On.** Without this the adverts carry no readings, and the collector logs a warning event saying so. Check with:

```bash
uv run aranet scan
# AA:BB:CC:DD:EE:FF  Aranet4 12345  model=Aranet4  fw=v2.0.12  interval=60s
#     {"co2": 831, "temperature": 22.2, "humidity": 54.0, "pressure": 1026.4, "battery": 28, ...}
```

The logging interval (1, 2, 5 or 10 min) is set in the app as well. Shorter intervals give finer data and drain the battery slightly faster.

## 3. Pair the sensor (one-time; needed for history backfill)

Passive collection does **not** need pairing. Downloading the on-device history after downtime **does**. The Aranet4 demands passkey authentication for GATT: without it, `bluetoothd` logs `No agent available for request type 2`, and the backfill fails with a timeout or `failed to discover services`.

Pair once using `bluetoothctl`. You'll need to see the sensor's display, because it shows a 6-digit PIN:

```text
$ bluetoothctl
[bluetooth]# agent KeyboardOnly
[bluetooth]# default-agent
[bluetooth]# scan on                      # wait until "Aranet4 xxxxx" appears, then:
[bluetooth]# scan off
[bluetooth]# pair AA:BB:CC:DD:EE:FF
[agent] Enter passkey (number in 0-999999): 123456   <- PIN shown on the Aranet display
[bluetooth]# trust AA:BB:CC:DD:EE:FF
[bluetooth]# quit
```

Verify with a small download:

```bash
uv run aranet backfill AA:BB:CC:DD:EE:FF --last 10
# 9 new rows of 10 fetched; device holds 2016 records; 6.3s
```

**After the first pairing, import the history recorded before the collector existed.** Automatic gap detection only fills gaps *after* the newest stored reading, so the ~3.5 days (5,040 records) held on the sensor before your first stored row has to be fetched once by hand. Stop the collector for this one-off, to avoid connect-while-scanning; the short gap while it's stopped is backfilled automatically when it restarts:

```bash
./scripts/devctl.sh stop collector          # or: systemctl --user stop aranet-collector
uv run aranet backfill AA:BB:CC:DD:EE:FF --all    # ~5040 records, 1-3 min; resumable
./scripts/devctl.sh start collector         # or: systemctl --user start aranet-collector
```

If a download fails partway (weak signal), just run `aranet backfill AA:BB:CC:DD:EE:FF`, without `--all`. It resumes from the first gap in the stored data, and rows already stored are never duplicated. A USB extension cable that brings the dongle closer to the sensor makes downloads much more reliable.

The bond is stored by BlueZ on that host. You need to pair again if you move the collector to a different host (for example, a Pi), or if you "forget" the device.

## 4. Run continuously (systemd user services)

```bash
./deploy/install-user-services.sh          # installs + starts aranet-collector and aranet-web
loginctl enable-linger "$USER"             # keep running when logged out and start at boot
journalctl --user -u aranet-collector -f   # logs
systemctl --user restart aranet-web        # after changing .env or upgrading
./deploy/install-user-services.sh uninstall
```

Both units read `.env` from the repo directory (the working directory), restart on failure, and stop the collector with SIGINT, which shuts it down gracefully.

For development without systemd, use `./scripts/devctl.sh start|stop|status [collector|web|web-sim]`. It works from pid files, and logs go to `data/*.log`.

## 5. Postgres

### One-time admin step

Run this as a Postgres admin (e.g. `postgres`) in the target database. On Postgres 15+, ordinary roles can't create tables in `public` by default, so give the app its own schema:

```sql
-- psql -h <host> -p <port> -U postgres -d <database>
CREATE SCHEMA IF NOT EXISTS aranet AUTHORIZATION app_role;
```

This puts the app's tables in `aranet`, apart from anything else stored in the database: `aranet.devices`, `aranet.readings`, `aranet.readings_hourly` and `aranet.collector_events`.

If you'd rather use `public`, the alternative is `GRANT CREATE ON SCHEMA public TO app_role;`; in that case leave `ARANET_DB_SCHEMA` unset.

### Configure

In `.env` (git-ignored):

```bash
POSTGRES_HOST=…  POSTGRES_PORT=…  POSTGRES_DB=<database>  POSTGRES_USER=app_role
POSTGRES_PASSWORD=…              # special characters are fine (escaped automatically)
ARANET_DB_SCHEMA=aranet
```

Use `ARANET_DB_URL=postgresql+psycopg://…` instead of the `POSTGRES_*` parts if you prefer a single URL; it takes precedence.

**TLS.** If the server is not on the same machine, enable TLS on it and set `POSTGRES_SSLMODE=verify-full` (plus `POSTGRES_SSLROOTCERT=/path/to/ca.pem` for a private CA), or `require` if you can't verify the certificate. Without it libpq uses `prefer`: encryption is opportunistic and the server is not authenticated. With `ARANET_DB_URL`, append `?sslmode=verify-full&sslrootcert=…` instead.

**Who creates tables.** Only writers run DDL: `collect`, `db-init`, `seed`/`simulate`, `rollup`, `backfill` and the `copy-db` target. `serve`, `status` and `export` only read and fail with "run `aranet db-init`" if the tables are missing. So the dashboard can optionally use its own read-only role (set its `POSTGRES_USER`/`POSTGRES_PASSWORD` in the web service's environment):

```sql
CREATE ROLE dashboard_ro LOGIN PASSWORD '…';
GRANT USAGE ON SCHEMA aranet TO dashboard_ro;
GRANT SELECT ON ALL TABLES IN SCHEMA aranet TO dashboard_ro;
ALTER DEFAULT PRIVILEGES FOR ROLE app_role IN SCHEMA aranet GRANT SELECT ON TABLES TO dashboard_ro;
```

### Create the tables and move the SQLite data

```bash
uv sync --frozen --no-dev --extra postgres
uv run aranet db-init                                        # creates the 4 tables (idempotent)
uv run aranet copy-db --source sqlite:///data/aranet.sqlite  # target = the configured DB
```

`copy-db` is idempotent: rows that already exist are skipped, and the hourly rollup is refreshed for the copied range.

Then restart the collector and web app; both read `.env` at start-up:

```bash
./scripts/devctl.sh restart collector && ./scripts/devctl.sh restart web
# or, once installed as services:
systemctl --user restart aranet-collector aranet-web
```

To catch readings logged between the copy and the restart, run `copy-db` once more afterwards. Readings already copied are skipped, so this is safe.

### Notes

- **Migrations:** there is no migration tool yet; the schema is created with `create_all`. Add Alembic when the schema needs to change.
- **Size:** a year of 60 s data for one sensor is about 63 MB in `readings` plus 2.5 MB of rollup.

## 6. Raspberry Pi (collector next to the sensor)

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
git clone <repo> && cd aranet4-monitor
uv sync --frozen --no-dev --extra postgres
echo 'ARANET_DB_URL=postgresql+psycopg://aranet:...@db-host:5432/aranet' > .env
uv run aranet scan
# pair as in section 3, then:
./deploy/install-user-services.sh
systemctl --user disable --now aranet-web      # optional: run the web app elsewhere
loginctl enable-linger "$USER"
```

Tips:

- Keep the Pi within a few metres of the sensor. At about −85 dBm (the signal at the desk test) adverts still arrive, but GATT connections become less reliable.
- If the Pi's onboard Wi-Fi and Bluetooth share an antenna, a USB BLE dongle can be more reliable.

## 7. Docker

Compose runs Postgres and the web app, bound to localhost:

```bash
POSTGRES_PASSWORD=change-me docker compose up -d --build
# demo data:   POSTGRES_PASSWORD=change-me docker compose --profile sim up -d
# collector in a container (uses the host's BlueZ over D-Bus, host networking):
#              POSTGRES_PASSWORD=change-me docker compose --profile collector up -d
```

- **Configurable ports:** `ARANET_WEB_PORT` (default 8050) and `POSTGRES_PORT` (default 5432).
- **Collector placement:** running the collector on the host (systemd) is simpler than in Docker, because BLE in containers needs the D-Bus socket mount and an AppArmor exception.

## 8. Exposing the dashboard on your LAN

By default the web app binds to `127.0.0.1`. To reach it from other machines:

```bash
ARANET_WEB_HOST=0.0.0.0
ARANET_WEB_PASSWORD=<something long>   # turns on HTTP Basic auth for every page and API route
```

Basic auth sends credentials on every request. Only use it on a trusted LAN, or put a TLS reverse proxy (Caddy or nginx) in front. See [SECURITY.md](SECURITY.md).
