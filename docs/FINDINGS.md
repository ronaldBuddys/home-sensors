# Engineering notes

Facts about the hardware, the Bluetooth protocol, the libraries and performance, learned while building and running this project. They are grouped by topic.

## Hardware and host

- **Host:** development PC (x86_64, Ubuntu, BlueZ). It had **no Bluetooth** until a USB dongle was added.
- **Dongle:** TP-Link **UB5A** (USB id `2357:0604`, Realtek RTL8761BU). The in-kernel `btusb`/`btrtl` driver loads it with firmware `rtl8761bu_fw.bin`, and it shows up as `hci0`.
- **Permissions:** BLE scanning through bleak/BlueZ D-Bus works as a **normal user**, with no root and no extra capabilities.

## Aranet4 BLE advertisements (passive mode)

- **Format:** the sensor advertises manufacturer data for company id **`0x0702`** (SAF Tehnika) plus service UUID `0xFCE0`.
- **Smart Home integrations.** When this is enabled in the Aranet Home app (flag bit 5 of byte 0), the 22-byte payload carries the full current reading.
- **Layout for Aranet4.** The library prepends a 0 byte; offsets below are into the raw 22 bytes.

  | Bytes | Field |
  |---|---|
  | `[0]` | flags (bit 0 disconnected, bits 2–3 calibration state, bit 4 DFU, bit 5 integrations) |
  | `[1..3]` | version |
  | `[4..7]` | unknown |
  | `[8..9]` | CO₂ (ppm, u16 LE) |
  | `[10..11]` | temperature (×0.05 °C) |
  | `[12..13]` | pressure (×0.1 hPa) |
  | `[14]` | RH % |
  | `[15]` | battery % |
  | `[16]` | status colour (1 green, 2 yellow, 3 red) |
  | `[17..18]` | interval s |
  | `[19..20]` | seconds since last measurement (`ago`) |
  | `[21]` | rolling **counter** |

- **Counter.** It **increments by exactly 1 per logged measurement**; observed 77 → 78 → 79 → 80 at 60 s intervals. `aranet4` 2.6.0 does **not** decode it for Aranet4 (its struct stops at byte 20), so `ble/parse.py` reads `raw[21]` itself.
  - **Not used for dedupe.** The measurement timestamp alone (re-adverts are within ±1 s) is a sufficient and safer "is this new?" test. Relying on an undocumented byte could silently drop every reading on firmware that doesn't increment it, and it wraps at 256.
- **Advert cadence.** BlueZ delivered an advert roughly every **3–13 s** (27 in 300 s at RSSI around −84 dBm). The same measurement is re-advertised about 5–6 times with increasing `ago`.
- **Measurement time.** `now − ago` is stable to within **±1 s** across re-adverts. That jitter is why dedupe uses a ±interval/2 window rather than exact timestamp equality.
- **Firmware string.** The advert's version bytes decode (via the library) to `v2.0.12`. Not yet cross-checked against the GATT `SW_REV` characteristic.
- **Battery.** Passive collection makes **no connections**, so it adds zero battery cost. GATT connections are kept to start-up and gap backfills.
- **Temperature precision.** Raw resolution is 0.05 °C, but `aranet4` rounds to 0.1 °C. For example, raw 443 is 22.15 °C, stored as 22.2.

## GATT / history download

- **On-device history is a full ring buffer of 5,040 records.** At 60 s that's 3.5 days, not the 2,016 points (~33 h) that older docs and library comments suggest. Once full, `total` stays at 5,040, and every new measurement evicts the oldest record and **shifts every index down by one**.
  - A full download takes 40–140 s here, so a shift almost always happens mid-download.
  - `ble/history.py` keeps a rolling `(seconds-since-last-log, wall time)` anchor and checks it after every chunk. On a shift it re-anchors the timestamps and re-reads that chunk one index lower.
  - A *growing* (not yet full) log only appends, so indices stay stable and nothing needs re-reading.
- **The link is flaky at about −84 dBm.** Long GATT transfers regularly fail with `BleakGATTProtocolError: Unlikely Error`. Reconnects sometimes fail with `failed to discover services, device disconnected`. Even the first connect and the metadata reads can fail.
  - Every GATT operation now goes through one helper that reconnects and retries, up to 8 times with growing pauses.
  - Downloads run in 500-record chunks, oldest first. Verified chunks are stored immediately, so a failure keeps its progress.
- **Real-device numbers.** A full 5,040-record download took 135 s with 9 recovered link drops and 2 ring shifts. The gap-only re-run took 36 s with 1 drop.
- **`aranet backfill ADDR` default.** It starts at the *first gap* in stored data within the device's horizon (`Store.first_gap_start`), so a re-run only fetches what's missing. `--all` re-reads everything.
- **Pairing.** It worked with `bluetoothctl` (`agent KeyboardOnly`, then `pair` and `trust`). `--last 10` succeeded straight after pairing.
- **Pairing is required.** Connecting without pairing fails during service discovery (`failed to discover services, device disconnected` or `GATT Protocol Error: Unlikely Error`). `bluetoothd` logs `new_auth() No agent available for request type 2`, which means the sensor requests **passkey authentication**. This is a one-time pairing step where you enter the PIN shown on the Aranet's display; see `docs/DEPLOYMENT.md`.
- **`get_all_records()` bug in aranet4 2.6.0.** It returns an **empty** history for Aranet4. Its model check only handles names starting with `Aranet2`, `Aranet☢` and `AranetRn`; everything else, including `Aranet4…`, is treated as "unknown model" and returns early. `ble/history.py` therefore drives `Aranet4.get_records()` directly.
- **No timeouts in the library.** Its history loops (`_get_records_v1` while-loop on notifications, `_get_records_v2` polling loop) have none. Every GATT step here is wrapped in `asyncio.wait_for`.
- **Other library issues:**
  - `find_nearby()` raises a `TypeError` when a scanned device has `name=None` (`"Aranet" in device.name`).
  - `remove_empty=True` slices `[begin-1:end+1]`, which looks like an off-by-one.
- **Timestamps.** The library computes history timestamps on the host as `now − ago − (total−1−i)·interval`, exactly as `ble/history.py:log_times` does. Like adverts, they jitter by about ±1 s.
- **Scanner conflicts.** BlueZ connect-while-scanning is unreliable, so the collector **stops the scanner during a backfill** and restarts it afterwards.
- **D-Bus noise.** After a failed or cancelled connect, `dbus-fast` leaves orphaned futures that raise `BrokenPipeError` ("Future exception was never retrieved"). It's harmless; the collector's loop exception handler downgrades it to debug.

## Libraries and frameworks

- **Dash 4.4** (released 2026) replaced the old React-Select/react-dates based components:
  - `RadioItems` renders `label.dash-options-list-option` (`.selected` / `aria-selected`) containing the input
  - `Dropdown` is a `button.dash-dropdown` with a portal popover
  - `DatePickerRange` uses `.dash-datepicker-*` classes
  - theming is through CSS custom properties (`--Dash-Fill-Interactive-Strong`, `--Dash-Text-Primary`, `--Dash-Stroke-Weak`, …)
  - a hidden `.dash-dropdown-focus-target` can overflow narrow viewports; clip it with `overflow-x: clip` on a container
- **Dash under FastAPI.** Dash 4 mounts cleanly under FastAPI with `a2wsgi.WSGIMiddleware(dash_app.server)`. Set Dash `compress=False` and let `GZipMiddleware` compress everything once.
- **Starlette TestClient** warns that `httpx` is deprecated in favour of **`httpx2`** (the pydantic-maintained fork); the dev dependency is now `httpx2`.
- **SQLAlchemy 2.x** `/` on integer columns is true division; use `//` for SQL integer bucket maths (`(ts // bucket) * bucket`). This works the same on SQLite and Postgres.
- **SQLite `INSERT … SELECT … ON CONFLICT DO UPDATE`** (used for the rollup upsert) needs a `WHERE` in the SELECT to avoid a parser ambiguity. The rollup's SELECT always has one.
- **Postgres numbers.** Postgres returns `numeric` (`Decimal`) for `avg`/sums of integers, so the query layer converts to float before JSON.
- **Plotly.** `hoversubplots="axis"` with `hovermode="x unified"` gives one tooltip across stacked subplots that share an x-axis.
- **Playwright** can drive the system Google Chrome with `channel="chrome"`; no `playwright install` download is needed.

## Data and visualisation

- **CO₂ day/night cycle.** Indoor CO₂ has a strong day/night cycle (bedroom overnight ramps). Any bucket shorter than a day (6 h, 12 h) aliases into a sawtooth "solid block" on a 1-year chart, so long windows use daily buckets.
- **Integer humidity.** The Aranet4 reports RH as an integer %, so raw RH charts look stepped. This is expected.

## Performance (1 year at 60 s ≈ 525 k rows per sensor)

- **SQLite size:** about 27 MB for a year; Postgres `readings` is about 63 MB.
- **Raw aggregation** costs about 0.4 µs/row on SQLite: 1 y takes about 430 ms for 4 metrics.
- **Hourly rollup** (8.8 k rows per year) brings 1 y down to about 50 ms end-to-end through the API, and rebuilding it takes 0.7 s.
- **Startup.** The web process imports Dash/Plotly, which takes about 1–2 s.
- **Page weight (measured).** A cold page load transfers **1.82 MB** (gzip) in 23 requests. `plotly.min.js` alone is 1.47 MB gzipped (4.8 MB raw).
  - Cold load and render: **about 1.8 s**, with about 1 s of script execution.
  - Warm reload: **0.25–0.54 s**. The bundle is revalidated by ETag (a 300 B 304 response).
  - Switching presets: about 0.35 s.
- **Rejected optimisation.** A Plotly *cartesian* partial bundle (about 0.4 MB gzipped) would cut the cold load. However, Dash registers plotly.js through the private `Dash._setup_plotlyjs()`, so swapping it means patching Dash internals. Not worth it, given the warm-cache numbers; revisit if the dashboard is served to slow mobile clients.

## Bugs found in review

Each of these was fixed with a regression test.

- **Mid-download shift.** If the sensor logs a point during a multi-parameter history download, the per-parameter columns shift relative to each other. When the log is full this silently mis-pairs values; when it isn't full the library raises `IndexError`. Fix: re-read `total`/`ago` after the download and retry.
- **Copying with the current interval.** `copy_db` window-deduplicated old 60 s data using the device's *current* interval (600 s), turning 1440 rows into 288. It now uses insert-or-ignore on the primary key.
- **Scanner leak on shutdown.** Stopping during a backfill left a scanner running, because the backfill's `finally` restarted it. Shutdown now cancels backfill tasks and never restarts the scanner while stopping.
- **Gaps that were never healed.** Three cases were missed:
  - points missed while the scanner was paused;
  - a failed start-up backfill;
  - a backfill skipped during cooldown.

  Gap detection is now based on measurement timestamps, and pending gaps persist across failures, with exponential back-off.
- **Thresholds ignored long intervals.** The stale and gap thresholds ignored the device interval, which mattered at 600 s logging. Both now scale with the interval.
- **The DST fall-back drew backwards.** Plotly has no time-zone-aware axis, so local time is drawn naive. On the night clocks go back, the repeated hour was drawn as a stroke running backwards. The figure now breaks the line wherever x goes backwards.
- **Min–max envelopes bridged gaps.** Plotly's `fill="tonexty"` ignores `None` gaps: the first segment's wash vanished and a wedge was drawn across the gap. The envelope is now one closed `fill="toself"` polygon per contiguous segment.
- **A preset click could be silently lost.** dash-renderer keeps only the **newest** request when a callback is re-triggered while a request is in flight, and discards the older response. Plotly emits an `{"autosize": true}` relayout about 200 ms after the first draw. Because the window callback also listened to `relayoutData`, that autosize could cancel a preset click: the button showed 30 d while the chart stayed on 24 h.
  - **Fix:** relayouts are filtered in the browser, so only real zooms and resets reach the server, and one callback owns the chart window.
  - **Lesson:** never let a framework-generated event share a callback with user input whose response must not be dropped.
  - **How it was found:** it showed up only as a flaky end-to-end test on CI runners. It was reproduced locally by holding requests with Playwright's `page.route`.
