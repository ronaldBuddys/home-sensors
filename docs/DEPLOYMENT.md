# Deployment

## 1. Prerequisites

- **Linux with BlueZ** (`bluetoothd` running) and a BLE adapter.
  - On `devhost` that's a TP-Link UB5A USB dongle (`hci0`).
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

### Create the database

Run this on your Postgres server:

```sql
CREATE ROLE aranet LOGIN PASSWORD 'change-me';
CREATE DATABASE aranet OWNER aranet;
```

### Point the app at it

```bash
uv sync --frozen --no-dev --extra postgres
# in .env (never commit it):
ARANET_DB_URL=postgresql+psycopg://aranet:change-me@db-host:5432/aranet
uv run aranet db-init
```

### Move existing SQLite data

```bash
uv run aranet copy-db --source sqlite:///data/aranet.sqlite \
                      --target postgresql+psycopg://aranet:change-me@db-host:5432/aranet
```

`copy-db` is idempotent: rows that already exist are skipped. It rebuilds the hourly rollup at the end.

### Notes

- The schema is created automatically (`metadata.create_all`). There's no migration tool yet. If the schema changes later, add Alembic.
- A year of 60 s data for one sensor is about 63 MB in `readings` plus 2.5 MB of rollup on Postgres, and about 27 MB on SQLite.

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
