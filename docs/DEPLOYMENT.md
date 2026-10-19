# Deployment

This guide sets up a permanent installation with a real sensor. The usual set-up is a **Raspberry Pi next to the sensor**, running the collector and the dashboard. Any Linux machine with BlueZ and a Bluetooth LE adapter works the same way.

To try the project without hardware, `uv run aranet demo` is enough; see the [README](../README.md#try-it-in-60-seconds-no-sensor-needed).

1. [What you need](#1-what-you-need)
2. [Prepare the Raspberry Pi](#2-prepare-the-raspberry-pi)
3. [Set up the sensor](#3-set-up-the-sensor)
4. [Pair the sensor (for history backfill)](#4-pair-the-sensor-for-history-backfill)
5. [Configure](#5-configure)
6. [Run as services](#6-run-as-services)
7. [The dashboard on your LAN](#7-the-dashboard-on-your-lan)
8. [Postgres (optional)](#8-postgres-optional)
9. [Upgrading](#9-upgrading)
10. [Troubleshooting](#10-troubleshooting)

## 1. What you need

- **An Aranet4** (or another Aranet BLE sensor) and the Aranet Home phone app.
- **A Raspberry Pi** 3, 4, 5 or Zero 2 W with **Raspberry Pi OS Lite (64-bit)**. The built-in Bluetooth works. Any Linux with BlueZ also works; on a PC a cheap USB dongle such as the TP-Link UB5A will do.
- **[uv](https://docs.astral.sh/uv/).** It installs a suitable Python by itself.
- **No root.** BLE scanning through BlueZ's D-Bus API works as a normal user, and the services run as systemd *user* units.

Every dependency has an ARM64 wheel, so nothing heavy compiles on the Pi. A **32-bit OS** (`uname -m` prints `armv7l`) is not supported: some dependencies, `psycopg[binary]` among them, have no 32-bit ARM wheels.

## 2. Prepare the Raspberry Pi

**Flash** with Raspberry Pi Imager:

- choose *Raspberry Pi OS Lite (64-bit)*;
- set a hostname, a user, and SSH with your public key;
- prefer Ethernet: on a Pi 3, Wi-Fi and Bluetooth share one radio chip.

**Base set-up** over SSH:

```bash
sudo apt update && sudo apt full-upgrade -y
sudo apt install -y git bluez
sudo usermod -aG bluetooth "$USER"  # BlueZ access for headless logins; log out and in again
curl -LsSf https://astral.sh/uv/install.sh | sh && exec "$SHELL" -l
```

**Get the code:**

```bash
git clone https://github.com/ronaldBuddys/home-sensors.git ~/home-sensors
cd ~/home-sensors
uv sync --frozen --no-dev  # first run downloads Python and the wheels: a few minutes
cp .env.example .env && chmod 600 .env
```

## 3. Set up the sensor

In the Aranet Home app, turn on **Settings → Smart Home integrations**. The sensor then includes each reading in its Bluetooth adverts. Without it the adverts carry no readings, and the collector logs a warning saying so. The logging interval (1, 2, 5 or 10 min) is also set in the app.

Check the Pi can hear the sensor:

```console
$ uv run --no-dev aranet scan
AA:BB:CC:DD:EE:FF  Aranet4 12345  model=Aranet4  fw=v2.0.12  interval=60s
    {"co2": 831, "temperature": 22.2, "humidity": 54.0, "pressure": 1026.4, "battery": 85, ...}
```

## 4. Pair the sensor (for history backfill)

Collecting readings does **not** need pairing: the collector only listens. Pairing is needed to **download the sensor's on-board history** (about 3.5 days at a 60 s interval) and fill gaps after downtime. The Aranet4 requires passkey authentication for that. Without pairing, `bluetoothd` logs `No agent available for request type 2` and the backfill fails.

Pair once with `bluetoothctl`. You need to see the sensor's display, which shows a 6-digit PIN:

```text
$ bluetoothctl
[bluetooth]# agent KeyboardOnly
[bluetooth]# default-agent
[bluetooth]# scan on  # wait until "Aranet4 xxxxx" appears, then:
[bluetooth]# scan off
[bluetooth]# pair AA:BB:CC:DD:EE:FF
[agent] Enter passkey (number in 0-999999): 123456   <- PIN shown on the Aranet display
[bluetooth]# trust AA:BB:CC:DD:EE:FF
[bluetooth]# quit
```

Check it with a small download:

```console
$ uv run --no-dev aranet backfill AA:BB:CC:DD:EE:FF --last 10
9 new rows of 10 fetched; device holds 5040 records; 6.3s
```

**Optional: import the history the sensor already holds.** Automatic gap detection only fills gaps *after* the newest stored reading. To import the days recorded before you started, run this once while the collector isn't running:

```bash
uv run --no-dev aranet backfill AA:BB:CC:DD:EE:FF --all  # ~5,040 records, 1-3 min; resumable
```

If a download stops partway (weak signal), run `aranet backfill AA:BB:CC:DD:EE:FF` without `--all`. It resumes from the first gap, and stored rows are never duplicated.

The bond belongs to this host. Pair again if you move the collector to another machine.

## 5. Configure

Settings come from `.env` in the repo root; every option is documented in [`.env.example`](../.env.example). For a Pi, the important ones are:

```bash
ARANET_DEVICES=AA:BB:CC:DD:EE:FF  # accept only your sensor (comma-separate several)
ARANET_WAIT_CLOCK_SYNC_S=300      # the Pi has no RTC: wait for NTP before recording
ARANET_TZ=Europe/London           # dashboard time zone (default: the system's)
```

- **Why the allow-list:** without `ARANET_DEVICES`, the collector accepts adverts from any device using Aranet's manufacturer id, and anyone nearby can spoof one. It then tracks at most 16 devices and only backfills devices it already knew about.
- **Why the clock wait:** after a power cut a Pi boots with the time saved at shutdown. `ARANET_WAIT_CLOCK_SYNC_S` makes the collector wait, up to that many seconds, until NTP has set the clock, so early readings aren't stamped hours in the past.
- **Where data goes:** to `data/aranet.sqlite` by default. To use Postgres instead, see [section 8](#8-postgres-optional).

## 6. Run as services

```bash
./deploy/install-user-services.sh         # installs, enables and (re)starts aranet-collector and aranet-web
loginctl enable-linger "$USER"            # start at boot and keep running while logged out
journalctl --user -u aranet-collector -f  # "device allow-list", then a reading every interval
uv run --no-dev aranet status             # freshness of the newest reading, as JSON
```

The installer:

- syncs the virtual environment;
- sets `.env` to `600` and `data/` to `700`;
- installs both units and restarts them, so changed units and code take effect.

Both services restart on failure. The collector also waits in-process for an unreachable database: one log line per retry, backing off to once a minute.

The units are **sandboxed**:

- no privilege escalation;
- only Unix and IP sockets;
- a read-only view of the filesystem except `data/`;
- a private `/tmp`;
- `UMask=0077`.

`PrivateUsers=yes` is what makes the filesystem rules effective in a *user* unit. If a host refuses user namespaces (the units fail with namespace errors), delete the `PrivateUsers`, `ProtectSystem`, `ProtectHome` and `PrivateTmp` lines from `deploy/systemd/*.service`, then re-run the installer.

To remove the services: `./deploy/install-user-services.sh uninstall`. To run the dashboard on another machine, disable it on the Pi with `systemctl --user disable --now aranet-web`.

## 7. The dashboard on your LAN

The web app binds to `127.0.0.1` by default. To open it from a phone or laptop, add these to `.env`:

```bash
ARANET_WEB_HOST=0.0.0.0
ARANET_WEB_PASSWORD=...               # HTTP Basic auth for every page and API route; generate with: openssl rand -base64 24
ARANET_WEB_TLS_CERT=data/tls/web.crt  # HTTPS, so the password isn't sent in clear
ARANET_WEB_TLS_KEY=data/tls/web.key
```

`aranet serve` refuses a LAN bind without a password. After 5 wrong passwords from one address within 10 minutes, that address gets `429` until the 10 minutes are up.

**HTTPS with a private CA:**

1. Issue a certificate for every name and IP you'll type into the browser. The script creates a small CA in `data/tls/` on first use:

   ```bash
   ./scripts/make-cert.sh web <Pi IP> <hostname>.local
   ```

2. Restart the dashboard (`systemctl --user restart aranet-web`) and open `https://<Pi IP>:8050/`.

3. To get rid of the browser warning, install `data/tls/ca.crt` as a trusted CA on each device:
   - **iOS:** open the file and install the profile, then turn it on under Settings → General → About → Certificate Trust Settings.
   - **Android:** Settings → Security → Install certificate → CA certificate.
   - **Desktop:** import it into the browser or OS certificate store.

For access away from home, use a VPN such as Tailscale or WireGuard; never forward the port. See [SECURITY.md](SECURITY.md).

## 8. Postgres (optional)

SQLite is enough for one sensor: a year of 60 s data is about 27 MB. Postgres is useful if you already run one, want the data off the SD card, or want the dashboard on another machine.

**One-time admin step.** On Postgres 15+ ordinary roles can't create tables in `public`, so give the app its own schema. Run this as an admin in the target database:

```sql
CREATE ROLE aranet LOGIN PASSWORD '<a long random password>';
CREATE SCHEMA IF NOT EXISTS aranet AUTHORIZATION aranet;
```

**Configure** in `.env`:

```bash
POSTGRES_HOST=<host>
POSTGRES_PORT=5432
POSTGRES_DB=<database>
POSTGRES_USER=aranet
POSTGRES_PASSWORD=...  # special characters are fine (escaped automatically)
ARANET_DB_SCHEMA=aranet
```

A single `ARANET_DB_URL=postgresql+psycopg://…` also works, and takes precedence over the `POSTGRES_*` parts.

**Create the tables, and optionally copy existing SQLite data:**

```bash
uv run --no-dev aranet db-upgrade                                     # creates the tables (idempotent)
uv run --no-dev aranet copy-db --source sqlite:///data/aranet.sqlite  # optional; target = the configured DB
systemctl --user restart aranet-collector aranet-web
```

`copy-db` is idempotent: rows already present are skipped. Running it again after the restart catches readings logged in between.

**TLS.** If Postgres runs on another machine, turn TLS on, so the connection is encrypted and the server is authenticated:

1. `./scripts/make-cert.sh postgres <server IP> [<server hostname>]`.
2. Copy `data/tls/postgres.crt` and `postgres.key` into the server's data directory. The key must be owned by `postgres` with mode `600`.
3. In `postgresql.conf`, set `ssl = on`, `ssl_cert_file = 'postgres.crt'` and `ssl_key_file = 'postgres.key'`, then run `SELECT pg_reload_conf();`. Optionally change this app's `host` lines in `pg_hba.conf` to `hostssl`.
4. In `.env` on the Pi, set `POSTGRES_SSLMODE=verify-full` and `POSTGRES_SSLROOTCERT=data/tls/ca.crt`. With `ARANET_DB_URL`, append `?sslmode=verify-full&sslrootcert=data/tls/ca.crt` instead.
5. Check with `uv run --no-dev aranet status`, then restart both services.

**A read-only role for the dashboard.** Only writers run DDL: `collect`, `backfill`, `db-upgrade`, `rollup`, `seed`/`simulate`/`demo` and the `copy-db` target. The dashboard never writes, so it can log in as a role that can only `SELECT`. A leaked dashboard password then exposes data, but can't change it.

1. As an admin, create the role:

   ```sql
   CREATE ROLE dashboard_ro LOGIN PASSWORD '<a long random password>';
   ```

2. As the app role, grant read access. This also covers tables added later:

   ```bash
   uv run --no-dev aranet grant-read dashboard_ro
   ```

3. In `.env`, set `ARANET_WEB_DB_USER=dashboard_ro` and `ARANET_WEB_DB_PASSWORD=…`, then restart the dashboard.

The web app also opens every Postgres transaction read-only, which guards against bugs even without the separate role.

## 9. Upgrading

```bash
cd ~/home-sensors
git pull
./deploy/install-user-services.sh  # syncs the venv (new dependencies), reinstalls the units, restarts both
uv run --no-dev aranet status
```

- **Re-running the installer is the safe way to upgrade.** Restarting the services alone isn't enough when a release adds a dependency: the old virtual environment fails at start-up.
- **Schema changes are applied automatically.** Alembic migrations run when the collector starts, or run `uv run --no-dev aranet db-upgrade` yourself.
- **Pre-migration databases** (created before Alembic was added) are recognised, and stamped at the baseline revision with no data change.

## 10. Troubleshooting

- **Distance.** Keep the Pi within a few metres of the sensor. At about −85 dBm adverts still arrive, but history downloads become unreliable; they retry and resume, but slowly.
- **Radio.** If the onboard Bluetooth is flaky, typically because Wi-Fi shares the chip on a Pi 3, use Ethernet or a USB BLE dongle.
- **Power.** Use a proper 2.5 A (Pi 3) or 3 A (Pi 4/5) supply. Under-voltage causes Bluetooth dropouts.
- **No readings in the adverts.** Smart Home integrations are off; see [section 3](#3-set-up-the-sensor).
- **The backfill fails with `failed to discover services`.** The sensor isn't paired with this host; see [section 4](#4-pair-the-sensor-for-history-backfill).
- **Timestamps look wrong after a power cut.** Set `ARANET_WAIT_CLOCK_SYNC_S` and check `timedatectl` shows "System clock synchronized: yes".
- **The services fail with namespace errors.** Remove the four sandbox lines; see [section 6](#6-run-as-services).
- **"database not reachable … retrying".** Postgres is down or unreachable. The collector keeps retrying and continues once the database is back, and backfill fills the gap on paired sensors.

Design details are in [ARCHITECTURE.md](ARCHITECTURE.md). Notes on the sensor, BlueZ and the libraries are in [FINDINGS.md](FINDINGS.md).
