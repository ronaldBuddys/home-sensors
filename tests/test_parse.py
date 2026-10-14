from aranet_monitor.ble.parse import parse_advert
from conftest import ADDR, REAL_ADVERT


def test_real_aranet4_advert_decodes():
    p = parse_advert("AA:BB:CC:00:11:22", "Aranet4 12345", REAL_ADVERT, -80, now=10_000.0)
    assert p is not None and p.integrations
    r = p.reading
    assert (r.co2, r.humidity, r.pressure, r.battery, r.status) == (831, 54.0, 1026.4, 28, 1)
    assert abs(r.temperature - 22.15) < 0.06
    assert p.device.interval_s == 60 and p.ago == 14 and p.counter == 0x44
    assert r.ts == 10_000 - 14  # measurement time, not receive time
    assert r.rssi == -80 and r.source == "adv"
    assert p.device.model == "Aranet4"


def test_nameless_advert_still_decodes():
    # Passive scans can arrive before the scan response carrying the name.
    p = parse_advert("X", None, REAL_ADVERT, None, now=0)
    assert p.reading.co2 == 831


def test_integrations_disabled_has_no_reading():
    raw = bytearray(REAL_ADVERT)
    raw[0] &= ~(1 << 5)  # clear the "integrations" flag bit
    p = parse_advert("X", "Aranet4 12345", bytes(raw), None, now=0)
    assert p is not None and p.reading is None and not p.integrations


def test_garbage_is_rejected():
    assert parse_advert("X", "Aranet4 1", b"\x01\x02", None) is None
    assert parse_advert("X", "Aranet4 1", b"", None) is None


def test_implausible_values_are_rejected():
    from aranet_monitor.ble.parse import parse_advert

    raw = bytearray(REAL_ADVERT)
    assert parse_advert(ADDR, "Aranet4 12345", bytes(raw)) is not None
    raw[8:10] = (20000).to_bytes(2, "little")  # decodes, but beyond the sensor range
    assert parse_advert(ADDR, "Aranet4 12345", bytes(raw)) is None
