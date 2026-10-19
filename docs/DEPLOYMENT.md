# Deployment

## 1. Prerequisites

- **Linux with BlueZ** (`bluetoothd` running) and a BLE adapter.
  - On the development host that's a TP-Link UB5A USB dongle (`hci0`).
  - On a Raspberry Pi 3, 4, 5 or Zero 2 W the built-in radio works.
- **[uv](https://docs.astral.sh/uv/)**. It installs Python 3.12 itself if needed.
- **No root needed.** BLE scanning through BlueZ D-Bus works as a normal user.

```bash
git clone <repo> aranet4-monitor && cd aranet4-monitor
uv sync --frozen --no-dev
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
# 9 new rows of 10 fetched; device holds 5040 records; 6.3s
```

**After the first pairing, import the history recorded before the collector existed.** Automatic gap detection only fills gaps *after* the newest stored reading, so the ~3.5 days (5,040 records) held on the sensor before your first stored row has to be fetched once by hand. Stop the collector for this one-off, to avoid connect-while-scanning; the short gap while it's stopped is backfilled automatically when it restarts:

```bash
systemctl --user stop aranet-collector
uv run aranet backfill AA:BB:CC:DD:EE:FF --all    # ~5040 records, 1-3 min; resumable
systemctl --user start aranet-collector
```

If a download fails partway (weak signal), just run `aranet backfill AA:BB:CC:DD:EE:FF`, without `--all`. It resumes from the first gap in the stored data, and rows already stored are never duplicated. A USB extension cable that brings the dongle closer to the sensor makes downloads much more reliable.

**Set the allow-list.** Put your sensor's address in `.env` as `ARANET_DEVICES=AA:BB:CC:DD:EE:FF` (comma-separate several). Without it the collector accepts adverts from any device using Aranet's manufacturer id (anyone nearby can spoof one), tracks at most 16 devices, and only backfills devices that already had stored readings before the collector started — a brand-new device needs the manual `aranet backfill` above. Adverts with physically implausible values (e.g. CO₂ above 10,000 ppm) are dropped.

The bond is stored by BlueZ on that host. You need to pair again if you move the collector to a different host (for example, a Pi), or if you "forget" the device.

## 4. Run continuously (systemd user services)

```bash
./deploy/install-user-services.sh          # installs + starts aranet-collector and aranet-web
loginctl enable-linger "$USER"             # keep running when logged out and start at boot
journalctl --user -u aranet-collector -f   # logs
systemctl --user restart aranet-web        # after changing .env or upgrading
./deploy/install-user-services.sh uninstall
```

Both units read `.env` from the repo root (python-dotenv searches upward from the installed package, which is the repo for this editable install), restart on failure (forever, 5–15 s apart), and stop the collector with SIGINT, which shuts it down gracefully.

The units are sandboxed: no privilege escalation, only Unix/IP sockets, a read-only view of the filesystem except `data/`, a private `/tmp`, and `UMask=0077`. This was checked on systemd 249: BLE scanning over the system D-Bus and Postgres both work. `PrivateUsers=yes` (a user namespace) is what makes the filesystem rules take effect in a *user* unit; if a host refuses user namespaces, remove the `PrivateUsers`, `ProtectSystem`, `ProtectHome` and `PrivateTmp` lines. After editing the unit templates, re-run the install script; it reinstalls the units and restarts both services. The script also sets `.env` to `600` and `data/` to `700`, because `.env` holds the DB and web passwords.

For development without systemd, run `uv run aranet collect` and `uv run aranet serve` in a terminal.

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

**TLS.** If the server is not on the same machine, enable TLS on it. Without TLS, libpq falls back to `prefer`: the connection is encrypted only if the server offers it, and the server is never authenticated. A self-run server usually has `ssl = off`. To turn it on:

1. Issue a server certificate from the project's private CA. List every name or IP that clients use for the server:

   ```bash
   ./scripts/make-cert.sh postgres <server IP> [<server hostname>]
   ```

2. Copy `data/tls/postgres.crt` and `data/tls/postgres.key` into the server's data directory. The key must be owned by the `postgres` user with mode `600`. If the server runs in Docker, mount both files into the container.

3. In `postgresql.conf` (or as `-c` options on the container), set:

   ```
   ssl = on
   ssl_cert_file = 'postgres.crt'
   ssl_key_file = 'postgres.key'
   ```

   Then reload the server: `SELECT pg_reload_conf();` as an admin.

4. Optionally, make the server refuse unencrypted logins: in `pg_hba.conf`, change this app's `host` lines to `hostssl`.

5. In `.env`, set:

   ```
   POSTGRES_SSLMODE=verify-full
   POSTGRES_SSLROOTCERT=data/tls/ca.crt
   ```

6. Check the connection with `uv run aranet status`, then restart both services.

If you use `ARANET_DB_URL` instead of the `POSTGRES_*` parts, append `?sslmode=verify-full&sslrootcert=data/tls/ca.crt` to the URL.

**Who creates tables.** Only writers run DDL: `collect`, `db-init`, `seed`/`simulate`, `rollup`, `backfill` and the `copy-db` target. `serve`, `status` and `export` only read and fail with "run `aranet db-init`" if the tables are missing. Because the dashboard never writes, it can log in as a **read-only role**. A leaked dashboard password then exposes data, but can't change or delete it. Setup:

1. As a DB admin, create the role (only an admin can create roles):

   ```sql
   CREATE ROLE dashboard_ro LOGIN PASSWORD '<openssl rand -base64 24>';
   ```

2. As the app role, which owns the tables, grant read access. This also covers tables added later:

   ```bash
   uv run aranet grant-read dashboard_ro
   ```

3. In `.env`, add `ARANET_WEB_DB_USER=dashboard_ro` and `ARANET_WEB_DB_PASSWORD=…`. The web app connects with these. The host, database, schema and TLS settings stay shared with the collector.

4. Restart the web service with `systemctl --user restart aranet-web`.

The web app also opens every Postgres transaction read-only, which guards against bugs even without the separate role.

### Create the tables and move the SQLite data

```bash
uv sync --frozen --no-dev
uv run aranet db-init                                        # creates the 4 tables (idempotent)
uv run aranet copy-db --source sqlite:///data/aranet.sqlite  # target = the configured DB
```

`copy-db` is idempotent: rows that already exist are skipped, and the hourly rollup is refreshed for the copied range.

Then restart the collector and web app; both read `.env` at start-up:

```bash
systemctl --user restart aranet-collector aranet-web
```

To catch readings logged between the copy and the restart, run `copy-db` once more afterwards. Readings already copied are skipped, so this is safe.

### Notes

- **Migrations:** there is no migration tool yet; the schema is created with `create_all`. Add Alembic when the schema needs to change.
- **Size:** a year of 60 s data for one sensor is about 63 MB in `readings` plus 2.5 MB of rollup.

## 6. Raspberry Pi (collector next to the sensor)

### Quick proof of concept (Pi already running, repo already cloned)

The goal is to show the Pi can hear the sensor and write to Postgres, without pairing, certificates or services. Run each block on the machine named in its comment.

```bash
# on the Pi: check the basics
uname -m                      # want aarch64 (64-bit OS); armv7l = 32-bit, see the note below
groups                        # should include "bluetooth" (if not: sudo usermod -aG bluetooth "$USER", log in again)
systemctl is-active bluetooth # "active": BlueZ ships with Raspberry Pi OS
```

```bash
# on the Pi: get the code and its dependencies
cd ~/home-sensors
git pull origin main
command -v uv || { curl -LsSf https://astral.sh/uv/install.sh | sh; exec "$SHELL" -l; }
uv sync --frozen --no-dev     # first run downloads Python 3.12 and the wheels: a few minutes
```

```bash
# on the current host: copy the settings (they contain passwords, so only over SSH)
scp .env <pi>:~/home-sensors/.env
ssh <pi> chmod 600 '~/home-sensors/.env'
```

```bash
# on the Pi: can it hear the sensor, and reach the database?
cd ~/home-sensors
uv run aranet scan            # lists the sensor and its readings
uv run aranet status          # JSON with "ok": true = Postgres reachable (read-only)
```

```bash
# trial run: hand recording over to the Pi for a few minutes
# on the current host:
systemctl --user stop aranet-collector
# on the Pi (no history download: the Pi isn't paired yet):
ARANET_BACKFILL_ON_START=0 uv run aranet collect     # a reading line every ~minute; Ctrl-C to stop
# back on the current host (it is paired, so it fills the gap from the sensor's memory):
systemctl --user start aranet-collector
```

If all that works, follow the full steps below to make the Pi permanent: pair it, install the services, and retire the old collector.

**32-bit OS (`armv7l`).** Several dependencies, including `psycopg[binary]`, have no 32-bit ARM wheels on PyPI, so they would have to compile. Re-flashing with the 64-bit Lite image is the easier path.

### Full setup

These steps were written for a Pi 3 (1 GB, built-in Bluetooth) running **Raspberry Pi OS Lite 64-bit**. The Pi runs the collector, and optionally the dashboard; Postgres stays on its server. Every dependency has an ARM64 wheel, so nothing heavy compiles on the Pi.

**1. Flash and boot.** Use Raspberry Pi Imager:
- choose *Raspberry Pi OS Lite (64-bit)*;
- set a hostname, a user, SSH with your public key, and Wi-Fi if needed;
- prefer Ethernet: on a Pi 3, Wi-Fi and Bluetooth share one radio chip.

**2. Base setup over SSH:**

```bash
sudo apt update && sudo apt full-upgrade -y
sudo apt install -y git bluez
sudo usermod -aG bluetooth "$USER"           # BlueZ D-Bus access for headless logins; log out and in
timedatectl                                  # "System clock synchronized: yes" (the Pi has no RTC)
curl -LsSf https://astral.sh/uv/install.sh | sh && exec "$SHELL" -l
```

**3. Get the code.** For a private repo, use a read-only deploy key:

```bash
ssh-keygen -t ed25519 -N "" -f ~/.ssh/home-sensors -C "pi deploy key"
cat ~/.ssh/home-sensors.pub    # GitHub: repo > Settings > Deploy keys > Add (read-only)
printf 'Host github.com\n  IdentityFile ~/.ssh/home-sensors\n' >> ~/.ssh/config
git clone git@github.com:<user>/<repo>.git ~/home-sensors && cd ~/home-sensors
uv sync --frozen --no-dev                    # downloads Python 3.12 + wheels; a few minutes
```

**4. Configure.** Copy `.env` from the current host over SSH. It holds secrets, so never commit or e-mail it:

```bash
# on the current host:
scp .env pi@<pi>:~/home-sensors/.env
# and, for HTTPS and Postgres TLS, the CA plus a web certificate for the Pi:
./scripts/make-cert.sh web-pi <pi IP> <pi hostname>.local
ssh pi@<pi> 'mkdir -p ~/home-sensors/data/tls && chmod 700 ~/home-sensors/data'
scp data/tls/ca.crt data/tls/web-pi.crt data/tls/web-pi.key pi@<pi>:~/home-sensors/data/tls/
```

On the Pi, set `ARANET_WEB_TLS_CERT=data/tls/web-pi.crt` and `ARANET_WEB_TLS_KEY=data/tls/web-pi.key` in `.env`. Leave the CA key (`ca.key`) on the host that issues certificates.

**5. Check the radio and pair** (the bond is per host, so the Pi needs its own):

```bash
uv run aranet scan                          # the sensor and its readings should be listed
bluetoothctl                                # pair + trust as in section 3 (PIN on the display)
uv run aranet backfill AA:BB:CC:DD:EE:FF --last 10
```

If pairing fails while the old host is still bonded, stop that host's collector first. If it still fails, run `bluetoothctl remove AA:BB:CC:DD:EE:FF` on the old host (unverified whether the Aranet4 keeps more than one bond).

**6. Run it as services, and retire the old collector:**

```bash
./deploy/install-user-services.sh
loginctl enable-linger "$USER"              # start at boot without logging in
systemctl --user disable --now aranet-web   # optional: keep the dashboard elsewhere
journalctl --user -u aranet-collector -f    # "device allow-list", then readings every minute
```

On the old host, run `systemctl --user disable --now aranet-collector`. Two collectors wouldn't duplicate rows, but each would open its own GATT sessions, which costs sensor battery.

Tips:

- **Distance.** Keep the Pi within a few metres of the sensor. At about −85 dBm (the signal at the desk test) adverts still arrive, but GATT connections become less reliable.
- **Radio.** If the onboard Bluetooth is flaky, typically because Wi-Fi shares the chip, a USB BLE dongle helps; the TP-Link UB5A works.
- **Clock.** Readings are timestamped with the Pi's clock. After a power cut the clock can be stale until NTP syncs, normally within seconds of the network coming up. Check `timedatectl` if timestamps look wrong.
- **Power and SD card.** Use a proper 2.5 A supply; under-voltage causes Bluetooth dropouts. Writes to the SD card are only the journal, because the database is remote.
- **Sandboxing.** If the units fail with namespace errors, remove the four `PrivateUsers`/`Protect*`/`PrivateTmp` lines from the unit templates (section 4) and re-run the installer.

## 7. Exposing the dashboard on your LAN

By default the web app binds to `127.0.0.1`. To reach it from other machines:

```bash
ARANET_WEB_HOST=0.0.0.0
ARANET_WEB_PASSWORD=<openssl rand -base64 24>   # HTTP Basic auth for every page and API route
ARANET_WEB_TLS_CERT=data/tls/web.crt            # HTTPS, so the password isn't sent in clear
ARANET_WEB_TLS_KEY=data/tls/web.key
```

- **Password required.** `aranet serve` refuses a non-loopback host without a password. Override this with `--allow-no-auth`.
- **Lockout.** After 5 wrong passwords from the same address, that address gets `429` for 10 minutes.
- **Plain-HTTP warning.** Without TLS, `serve` logs a warning, because Basic auth then sends the password readably to anyone sniffing the LAN or Wi-Fi.

To set up HTTPS:

1. Issue a certificate for every name or IP you type into the browser:

   ```bash
   ./scripts/make-cert.sh web <LAN IP> <hostname>.local
   ```

2. Set the two `ARANET_WEB_TLS_*` variables shown above.

3. Restart the web service with `systemctl --user restart aranet-web`, and open `https://<LAN IP>:8050/`.

4. To get rid of the browser warning, install `data/tls/ca.crt` as a trusted CA on each device:
   - **iOS:** open the file, install the profile, then turn on Settings → General → About → Certificate Trust Settings.
   - **Android:** Settings → Security → Install certificate → CA certificate.
   - **Desktop:** import the CA into the browser or OS store.

A VPN such as Tailscale is an alternative if you want the dashboard reachable away from home. See [SECURITY.md](SECURITY.md).
