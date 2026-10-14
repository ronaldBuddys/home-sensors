# Development log

This is a dated, step-by-step record of what was done, what broke and how it was resolved. It's newest-last.

## 2026-10-14 — Planning and hardware bring-up

1. **Bluetooth.** I surveyed the host and found no Bluetooth adapter at all. The user plugged in a TP-Link UB5A dongle, which came up immediately as `hci0` with `bluetoothd` active.
2. **Device found.** A 15 s `bluetoothctl scan` found `Aranet4 xxxxx` (random static address, unpaired). Its advert manufacturer data showed Smart Home integrations already enabled, so passive collection was possible.
3. **Library review.** I read the `aranet4` 2.6.0 source and found the `get_all_records` Aranet4 bug and the missing timeouts (see FINDINGS).
4. **Conventions.** I reviewed the user's existing projects (uv, src layout, ruff, frozen-dataclass settings, Dash) and matched them.
5. **Plan approved.** The plan is FastAPI + Dash in one process, a separate collector process, and SQLAlchemy Core over SQLite or Postgres.

## 2026-10-14 — M0/M1: scaffold, storage, collector

- **Scaffold.** `git init`, a uv project (Python 3.12, hatchling, `src/aranet_monitor`), ruff (E,F,W,I,UP,B,SIM,D; numpy docstrings) and pytest.
- **Live advert capture.** A 300 s capture showed adverts every 3–13 s and a counter that increments once per measurement. The derived timestamp is stable to ±1 s.
- **`ble/parse.py`.** Wraps the library's `Aranet4Advertisement`, maps `-1` sentinels to `None`, computes the measurement time and decodes the counter byte.
- **`db/`.** `devices`, `readings` (PK `(device_id, ts)`, SQLite `WITHOUT ROWID`) and `collector_events`. SQLite runs in WAL with a busy timeout. Inserts use window dedupe.
- **`ble/collector.py`.**
  - A passive scan feeds an asyncio queue; DB writes happen in threads.
  - The counter decides whether an advert is a new measurement.
  - Backfill runs at start-up and on gaps, with the scanner paused, a cooldown after failure, and a watchdog.
- **GATT backfill blocked on pairing.** Backfill against the real device failed because the device demands passkey pairing. The collector logs a `backfill_error` event, keeps collecting passively, and won't retry for an hour.
- **Real collector running.** It writes to `data/aranet.sqlite`; output is in `data/collector.log`. It records one row per 60 s.
- **Tests.** 23 pytest tests cover:
  - parsing, using real advert bytes
  - dedupe, with jitter and history merges
  - bucketing and gaps
  - stats
  - the collector, with a fake scanner and fake history fetcher, covering counter dedupe, startup backfill, failure cooldown and the allow-list

## 2026-10-14 — M2/M3: API and dashboard

- **FastAPI under Dash: spike first.** Mounting Dash 4.4.1's Flask server under FastAPI 0.143 via `a2wsgi.WSGIMiddleware` worked first time: layout, callbacks and FastAPI routes all live in one uvicorn process. Routes registered before `app.mount("/")` win.
- **API.** Endpoints: `/api/v1/{health,devices,devices/{id},devices/{id}/latest,…/readings,…/stats,…/export.csv,events}`. They use Pydantic response models and are documented at `/api/docs`. Dependencies (`Queries`, `Settings`) come in through `Annotated[..., Depends()]`.
- **Dataviz guidance applied:**
  - one single-series panel per metric (no dual axes), linked x with a unified crosshair (`hovermode="x unified"`, `hoversubplots="axis"`)
  - a 10–14 % min–max envelope
  - status colours only for the CO₂ bands, always with an icon and label
  - light and dark themes, each selected (not auto-flipped)
  - a summary table as the "table view"
- **Dash 4 changed the core components.** `RadioItems`, `Dropdown` and `DatePickerRange` render new markup (`label.dash-options-list-option.selected`, `.dash-dropdown`, `.dash-datepicker-*`) and are themed through `--Dash-*` CSS variables. Old React-Select and react-dates CSS selectors no longer apply.
- **Visual QA with headless Chrome.** Playwright (`channel="chrome"`, using the system Google Chrome, so no browser download) drove screenshots and found:
  - the selected preset wasn't highlighted (Dash 4 markup change)
  - the date picker had a stray filler box
  - threshold labels were mis-anchored, because `update_annotations` also restyled them
  - the 1 y view rendered as a solid block (diurnal aliasing at 6 h buckets)
  - the simulator's pressure random-walk drifted to 741 hPa
  - mobile overflowed by 9 px (a Dash dropdown focus target)

  All fixed and re-verified.
- **Restart mistake.** `pkill -f "aranet serve"` matched my own shell's command line and killed it, twice. I replaced it with `scripts/devctl.sh` (pid files) and later systemd units.

## 2026-10-14 — M5: performance and Postgres

- **Benchmark.** I seeded 1 year (525,601 rows) of simulated 60 s data, which took 9–11 s on SQLite and 44 s on Postgres.
- **Raw aggregation was too slow.** It cost about 0.4 µs per row on SQLite:

  | Window | Series | Stats |
  |---|---|---|
  | 1 y | 432 ms | 203 ms |
  | 90 d | 116 ms | 50 ms |

  The target was under 200 ms.
- **Fix: hourly rollup table** (`readings_hourly`, sum/cnt/min/max per metric plus CO₂ level counts).
  - It is maintained in the same transaction as each insert, by recomputing only the touched hours.
  - Buckets of 1 h or more and stats over more than 7 days read from it.
  - Results are exactly equal to raw aggregation; this is tested.
- **API timings after the rollup** (SQLite / Postgres 16):

  | Window | SQLite | Postgres 16 |
  |---|---|---|
  | 1 h | 21 ms | 27 ms |
  | 24 h | 30 ms | 13 ms |
  | 7 d | 35 ms | 26 ms |
  | 30 d | 75 ms | 49 ms |
  | 90 d | 35 ms | 24 ms |
  | 1 y | 55 ms | 33 ms |

  Stats take under 10 ms everywhere.
- **Page load.** About 1.8 s cold (1.82 MB gzipped, of which plotly.js is 1.47 MB) and 0.25–0.5 s warm (ETag 304s). Switching presets takes about 0.35 s. I decided against swapping in a partial Plotly bundle, because it would mean patching a private Dash hook.
- **Fewer chart points.** The dashboard asks for at most 700 points beyond 24 h. That gives daily buckets for 1 y and avoids day/night aliasing.
- **Postgres.** The same unit tests ran unchanged on `postgres:16-alpine` (Docker, port 55432) via `ARANET_TEST_PG_URL`. The full stack (seed, serve, browser) was verified against Postgres too.
- **Compose test found a bug.** The `simulator` service crashed on float `--history-days` in `range()`. Fixed, with a regression test.

## 2026-10-14 — M6: ops, security, docs

- **Deployment files:**
  - systemd user units plus `deploy/install-user-services.sh` (not installed yet; lingering is off on this host)
  - a Dockerfile (python:3.12-slim + uv, non-root, healthcheck)
  - docker-compose with Postgres, web, and optional `sim` and `collector` profiles, all bound to localhost
- **Security:**
  - optional HTTP Basic auth (pure ASGI middleware, so it also guards the mounted Dash app)
  - the password is hidden from the settings `repr` and DB URLs are masked in logs
  - git history scanned: no real MAC or serial
- **`aranet copy-db`** copies SQLite to Postgres (or any pair), idempotently, for moving the PoC data onto the network Postgres.
- **Real-device data at this point.** One row every 60 s, with zero missed or duplicated measurements across collector restarts. Spacing histogram: `{60 s: all}`.
