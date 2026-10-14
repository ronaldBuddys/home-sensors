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
