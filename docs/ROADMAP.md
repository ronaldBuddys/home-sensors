# Roadmap and next steps

## Needs you (manual and one-time)

1. **Pair the sensor** so history backfill works after downtime. It needs the PIN on the Aranet display; see [DEPLOYMENT.md §3](DEPLOYMENT.md#3-pair-the-sensor-one-time-needed-for-history-backfill). Then verify with `uv run aranet backfill <MAC> --last 10`.
2. **Install as services** with `./deploy/install-user-services.sh`, and optionally run `loginctl enable-linger $USER`. At the moment the collector and web app run from `scripts/devctl.sh`, so they won't survive a reboot or logout.
3. **Point at the network Postgres** when ready: `uv sync --extra postgres`, set `ARANET_DB_URL` in `.env`, then `aranet copy-db --source sqlite:///data/aranet.sqlite --target <pg-url>`.
4. **Before publishing**, work through [SECURITY.md](SECURITY.md): add a LICENSE, run a secret scan, and check the author email.

## Good next features

- **Alerts.** Notify when CO₂ stays above the alert threshold for more than N minutes, or when data goes stale (collector or sensor down). Options include ntfy.sh, Home Assistant webhooks and email. This fits as a small loop in the collector, or as a separate `aranet alert` service reading the DB.
- **Daily and weekly patterns.** An hour-of-day × weekday heatmap of mean CO₂, built from `readings_hourly` (cheap). It's the best view for "when do I need to ventilate?".
- **Annotations.** Mark events such as "window opened" or "guests" on the chart, using a small `annotations` table and a Dash modal.
- **Comparing devices.** With more sensors, add small multiples per room, and a device column in the API's `/latest`.
- **MQTT / Home Assistant publishing** from the collector, since the passive adverts are already decoded.
- **Retention.** Optionally drop raw rows older than N years and keep `readings_hourly` forever.
- **Alembic migrations** once the schema needs to change. At the moment it's `create_all` only.
- **Plotly partial bundle** to cut the cold page load from 1.8 s to about 0.6 s, if the dashboard is used on slow phones. It needs a Dash override; see FINDINGS.
- **Temperature resolution.** Store the native 0.05 °C instead of the library's 0.1 °C rounding. Adverts are easy to decode directly; history would need to bypass `CurrentReading._set`.
- **Upstream contributions to Aranet4-Python:**
  - `get_all_records()` returns empty for Aranet4 (model branch)
  - the Aranet4 counter byte isn't decoded
  - `find_nearby()` crashes on `name=None`
  - history loops have no timeouts

## Known limitations

- **Daylight-saving fall-back.** On the night clocks go back, the repeated hour is drawn doubled back on itself, because the chart's x-axis uses naive local time (Plotly has no tz-aware axis). Storage is UTC and unaffected. A fix would be an option to display UTC.
- **History needs pairing.** Until the sensor is paired, detected gaps stay pending. A connection attempt is made at a backed-off rate of at most once every 6 h after a few failures.
- **Rollup stats are hour-aligned.** Stats for windows longer than 7 days come from the hourly rollup, so the window edges are aligned to the hour (accurate to about 1 hour of data at each end).
