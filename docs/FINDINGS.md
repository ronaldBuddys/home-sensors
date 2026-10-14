# Findings

These are useful facts I discovered while building this project, kept for future development. They're grouped by topic, and the newest findings are added at the end of each section.

## Hardware and host

- **Host:** `devhost` (x86_64, Ubuntu, BlueZ). It had **no Bluetooth** until a USB dongle was added.
- **Dongle:** TP-Link **UB5A** (USB id `2357:0604`, Realtek RTL8761BU). The in-kernel `btusb`/`btrtl` driver loads it with firmware `rtl8761bu_fw.bin`, and it shows up as `hci0`.
- **Permissions:** BLE scanning through bleak/BlueZ D-Bus works as a **normal user**, with no root and no extra capabilities.

## Aranet4 BLE advertisements (passive mode)

- **Format:** the sensor advertises manufacturer data for company id **`0x0702`** (SAF Tehnika) plus service UUID `0xFCE0`.
- **Smart Home integrations.** When this is enabled in the Aranet Home app (flag bit 5 of byte 0), the 22-byte payload carries the full current reading. Here it was already enabled.
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

- **Counter.** It **increments by exactly 1 per logged measurement**; I observed 77 → 78 → 79 → 80 at 60 s intervals. `aranet4` 2.6.0 does **not** decode it for Aranet4 (its struct stops at byte 20), so `ble/parse.py` reads `raw[21]` itself. It's a clean "is this a new measurement?" key.
- **Advert cadence.** BlueZ delivered an advert roughly every **3–13 s** (27 in 300 s at RSSI around −84 dBm). The same measurement is re-advertised about 5–6 times with increasing `ago`.
- **Measurement time.** `now − ago` is stable to within **±1 s** across re-adverts. That jitter is why dedupe uses a ±interval/2 window rather than exact timestamp equality.
- **Firmware string.** The advert's version bytes decode (via the library) to `v2.0.12`. This still needs confirming against GATT `SW_REV` once the device is paired.
- **Battery.** The sensor reported **28 %**. Passive collection makes **no connections**, so it adds zero battery cost. GATT connections are kept to start-up and gap backfills.
- **Temperature precision.** Raw resolution is 0.05 °C, but `aranet4` rounds to 0.1 °C. For example, raw 443 is 22.15 °C, stored as 22.2.

## GATT / history download

- **Pairing is required.** Connecting without pairing fails during service discovery (`failed to discover services, device disconnected` or `GATT Protocol Error: Unlikely Error`). `bluetoothd` logs `new_auth() No agent available for request type 2`, which means the sensor requests **passkey authentication**. This is a one-time pairing step where you enter the PIN shown on the Aranet's display; see `docs/DEPLOYMENT.md`.
- **`get_all_records()` bug in aranet4 2.6.0.** It returns an **empty** history for Aranet4. Its model check only handles names starting with `Aranet2`, `Aranet☢` and `AranetRn`; everything else, including `Aranet4…`, is treated as "unknown model" and returns early. `ble/history.py` therefore drives `Aranet4.get_records()` directly.
- **No timeouts in the library.** Its history loops (`_get_records_v1` while-loop on notifications, `_get_records_v2` polling loop) have none. Every GATT step here is wrapped in `asyncio.wait_for`.
- **Other library issues:**
  - `find_nearby()` raises a `TypeError` when a scanned device has `name=None` (`"Aranet" in device.name`).
  - `remove_empty=True` slices `[begin-1:end+1]`, which looks like an off-by-one.
- **Timestamps.** The library computes history timestamps on the host as `now − ago − (total−1−i)·interval`, exactly as `ble/history.py:log_times` does. Like adverts, they jitter by about ±1 s.
- **Scanner conflicts.** BlueZ connect-while-scanning is unreliable, so the collector **stops the scanner during a backfill** and restarts it afterwards.
- **D-Bus noise.** After a failed or cancelled connect, `dbus-fast` leaves orphaned futures that raise `BrokenPipeError` ("Future exception was never retrieved"). It's harmless; the collector's loop exception handler downgrades it to debug.
