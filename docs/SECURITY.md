# Security model

To report a vulnerability, see the [security policy](../SECURITY.md).

## Threat model

The app runs on a home network: a collector next to the sensor and a dashboard on the LAN.

- **The data.** Indoor CO₂, temperature and humidity are low-sensitivity, but they reveal occupancy: when people are home, asleep or away. Treat them as personal data.
- **The main threats:**
  - someone on the LAN or Wi-Fi reading the dashboard or sniffing its password;
  - someone in Bluetooth range injecting fake readings;
  - a leaked credential being used to change or delete data.

## What the code does

### Web app

- **Localhost by default.** The server binds to `127.0.0.1`. `aranet serve` refuses a non-loopback bind without `ARANET_WEB_PASSWORD`, unless you pass `--allow-no-auth`.
- **Authentication.** With a password set, HTTP Basic auth covers every route, including the dashboard's websockets.
  - The comparison is constant-time (`secrets.compare_digest`).
  - Only `/healthz` stays open, for health checks.
- **Lockout.** After 5 failed logins from one address within 10 minutes, that address gets `429` until the 10 minutes are up.
- **HTTPS.** Optional, with `ARANET_WEB_TLS_CERT`/`ARANET_WEB_TLS_KEY`. `scripts/make-cert.sh` runs a small private CA. A LAN bind over plain HTTP logs a warning.
- **Security headers** on every response, including 401s:
  - a Content-Security-Policy with everything from `'self'` and `frame-ancestors 'none'`;
  - `X-Frame-Options: DENY`;
  - `X-Content-Type-Options: nosniff`;
  - `Referrer-Policy: no-referrer`.
- **Read-only API.** There are no write or control endpoints, and device settings are never changed.
- **Bounded input:**
  - `start`/`end` must be epoch seconds within `[0, 2^32]`;
  - an API window may span at most about 2 years (export longer histories in several requests, or with `aranet export`);
  - malformed dashboard state is tolerated rather than raising an error.
- **SQL injection.** Metric names from query strings are checked against a fixed allow-list before being used as column references. Everything else is bound parameters through SQLAlchemy. CSV filenames are sanitised.
- **No secrets in logs.** The web password and the DB URL (which carries the DB password) never appear in `repr()` or logs; URLs are logged with the password masked.

### Database

- **Least privilege on Postgres:**
  - the app gets its own schema;
  - the dashboard can log in as a role granted `SELECT` only (`aranet grant-read`, which also covers future tables);
  - the web app opens every transaction read-only;
  - read-only commands (`serve`, `status`, `export`) never run DDL.
- **TLS to Postgres.** `POSTGRES_SSLMODE=verify-full` with the private CA authenticates the server as well as encrypting the connection.

### Collector and Bluetooth

- **Allow-list.** With `ARANET_DEVICES` set, only the listed sensors are accepted. Without it, at most 16 devices are tracked, and only devices with readings stored before the current session are backfilled over GATT. So a spoofed advert can't make the collector pause scanning to connect to it.
- **Plausibility checks.** Physically implausible values (e.g. CO₂ above 10,000 ppm) are dropped.
- **Backfill limits.** At most one GATT session per device per 15 minutes, with exponential back-off up to 6 h after failures.
- **Listen-only scanning.** Passive scanning only listens. The pairing bond is stored by BlueZ (`/var/lib/bluetooth`), and the app stores no keys.

### Deployment

- **Sandboxed services.** The systemd user units:
  - forbid privilege escalation;
  - allow only Unix and IP sockets;
  - see a read-only filesystem except `data/`;
  - get a private `/tmp` and `UMask=0077`.
- **File permissions.** The installer sets `.env` to `600` and `data/` to `700`.
- **No secrets in the repo.** `.env`, `data/` and database files are git-ignored, and `.env.example` holds placeholders only.

### Supply chain

- **Locked dependencies.** They are pinned in `uv.lock`, and Dependabot proposes updates for Python packages and GitHub Actions.
- **Pinned actions.** CI pins every action to a commit SHA and runs with `contents: read`.
- **Weekly audit.** A scheduled job runs `pip-audit --strict` on the exported lock file.

## Accepted risks

These are known limits, accepted for a single-household deployment. Revisit them if the exposure changes, for example more users or access from outside the home.

### Web authentication

- **Basic auth has no logout.** Browsers cache the credentials, there is one shared user, and the password sits in plain text in `.env` (mode `600`).
- **The lockout is per address and in memory.** An attacker with several LAN addresses gets more guesses. A legitimate device behind the same NAT or VPN address can be locked out. A restart clears the state.
- **The CSP needs `'unsafe-inline'` and `'unsafe-eval'`** for the Dash renderer and Plotly. Dash's CSP-hash support (`app.csp_hashes()`) could remove `'unsafe-inline'`; `'unsafe-eval'` is needed by Plotly.
- **The Swagger UI** (`ARANET_API_DOCS=1`, off by default) loads JavaScript from a CDN and is exempt from the CSP. Self-host its assets if you need the docs on a network.
- **The API returns BLE addresses and collector error strings.** This is fine behind authentication, but it matters if authentication is turned off.
- **Recommended for remote access:** don't port-forward. Use a VPN such as Tailscale or WireGuard, or put Caddy or nginx in front to handle TLS and stronger authentication (forward-auth or SSO).

### Bluetooth spoofing

- **Adverts are unauthenticated.** Someone in range can spoof the allow-listed address and inject plausible readings, or win the dedupe race against a real reading. Implausible values are dropped, but plausible fakes are stored.
- **A spoofed advert showing a large gap can trigger a backfill attempt.** This is limited to one session per 15 minutes per device with back-off; there is no global rate limit.
- **A possible mitigation:** cross-check advert readings against the paired device's history, and flag a reading that a later backfill doesn't confirm.

## Exposing the dashboard beyond localhost

- Set a long random `ARANET_WEB_PASSWORD` **and** a TLS certificate ([DEPLOYMENT.md §7](DEPLOYMENT.md#7-the-dashboard-on-your-lan)). Basic auth over plain HTTP sends the password with every request.
- Use a VPN rather than port forwarding to reach it from outside the home.
- Never enable Dash debug mode on a network interface. It is off here.
