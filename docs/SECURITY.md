# Security notes and pre-publication checklist

## Threat model (MVP)

The app runs on a home network. The data, indoor CO₂/temperature/humidity, is low sensitivity but reveals occupancy patterns: when people are home, asleep or away. Treat it as personal data.

## What the code already does

- **Localhost by default.** The web server binds to `127.0.0.1`, and the docker-compose ports are bound to `127.0.0.1` too.
- **Optional authentication.** When `ARANET_WEB_PASSWORD` is set, HTTP Basic auth covers every route, including the Dash UI. The comparison is constant-time (`secrets.compare_digest`), and `/healthz` stays open for health checks. The password never appears in the settings `repr()` or in logs: DB URLs are logged with the password masked.
- **Read-only API.** There are no write or control endpoints. Device settings (interval, integrations) are never changed by this app.
- **SQL injection.** Metric names in query strings are validated against a fixed allow-list before they're used as column references. Everything else is bound parameters through SQLAlchemy.
- **Safe CSV filenames.** CSV export filenames are sanitised to alphanumerics.
- **Containers.** The image runs as a non-root user (uid 1000).
- **No secrets in the repo.** `.env`, `.env.*` (except `.env.example`), editor swap files (`*.swp`), `data/` and `*.sqlite` are in `.gitignore`, and `.env.example` contains placeholders only. An editor lock file of `.env` (`..env.swp`, header only: user, host name and path, no values) was committed once and removed; it is purged from history before publishing.
- **No real host, database or role names** in docs and tests; placeholders are used instead.
- **Real identifiers scrubbed.** Tests and docs use fake BLE addresses (`AA:BB:CC:…`) and device names (`Aranet4 12345`).

## Before pushing to a remote (private)

- [ ] `git status` shows no `.env`, `data/`, `*.sqlite` or logs.
- [ ] `git log -p | grep -iE "password|secret|token|<your sensor MAC>"` finds nothing sensitive in history. (The real MAC should only ever appear in `data/` and logs, which are ignored.)
- [ ] **Decided:** remove the `Co-Authored-By: Claude <noreply@anthropic.com>` trailers from every commit message before the first push. They were added during the initial build; rewrite them out with `git filter-repo --message-callback` or an interactive rebase.
- [ ] **To decide later:** the commit author email. Every commit so far uses the personal Gmail; pick the final address (e.g. a GitHub noreply address) and rewrite author/committer with `git filter-repo --mailmap` before pushing.

## Before making the repo public

- [ ] Run a secret scanner over the full history, for example `gitleaks detect` or `trufflehog git file://.`.
- [ ] Re-check the screenshots in `docs/img/`. They come from **simulated** data (`Aranet4 SIM01`) and contain no real readings or identifiers.
- [ ] Decide whether real-data examples should ever be published: occupancy can be inferred from CO₂.
- [ ] Choose a licence. MIT is assumed in the README and `pyproject.toml`, and is compatible with `aranet4` (MIT). Add a `LICENSE` file.
- [ ] Pin dependency versions (`uv.lock` is committed) and enable Dependabot or Renovate for updates.
- [ ] Run `pip-audit` (e.g. `uvx pip-audit`) for known CVEs in dependencies.

## If exposing the dashboard beyond localhost

- Set `ARANET_WEB_PASSWORD`. Prefer a reverse proxy with TLS (Caddy gets this almost for free), because Basic auth over plain HTTP sends the password on every request.
- Don't port-forward this to the internet. Use a VPN (Tailscale or WireGuard) instead.
- Dash runs with `debug=False`, which is the default here. Never enable Dash debug mode on a network interface.
- `/api/docs` (OpenAPI) is public when no password is set. That's fine on localhost; it sits behind auth otherwise.

## Bluetooth

- Pairing creates a bond stored by BlueZ (`/var/lib/bluetooth`). The app itself stores no keys.
- Passive scanning only listens. The collector connects to a sensor only to read history.
