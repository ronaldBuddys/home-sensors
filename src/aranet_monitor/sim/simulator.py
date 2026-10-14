"""Deterministic synthetic Aranet4 data with realistic indoor dynamics.

CO₂ follows a single-zone mass balance: occupants emit, ventilation decays the excess
towards the outdoor baseline. Occupancy follows a weekday/weekend schedule (a bedroom at night
with the door shut gives the classic overnight ramp). Temperature and humidity have diurnal
cycles; pressure is a slow random walk like passing weather systems.
"""

from __future__ import annotations

import math
import random
from collections.abc import Iterator
from datetime import datetime

from aranet_monitor.models import Reading

SIM_ADDRESS = "SI:MU:LA:TE:D0:01"
SIM_NAME = "Aranet4 SIM01"


class Aranet4Simulator:
    """Generate a stream of readings at a fixed interval."""

    def __init__(self, seed: int = 4, interval_s: int = 60, address: str = SIM_ADDRESS):
        self.rng = random.Random(seed)
        self.interval_s = interval_s
        self.address = address
        self.co2 = 650.0
        self.battery = 85.0
        self.counter = 0
        # Weather: a few slow sinusoids (periods 1.5-9 days) -> smooth, realistic swings.
        self._weather = [
            (
                self.rng.uniform(2.0, 8.0),
                self.rng.uniform(1.5, 9.0) * 86400,
                self.rng.uniform(0, 6.3),
            )
            for _ in range(4)
        ]

    def _occupants(self, dt: datetime) -> tuple[float, float]:
        """Return (people, air changes per hour) for a local datetime."""
        h = dt.hour + dt.minute / 60
        weekend = dt.weekday() >= 5
        if h < 7 or h >= 23:  # asleep, door mostly closed
            return 2.0, 0.65
        if 7 <= h < 8.5:
            return 2.0, 2.0  # window opened in the morning
        if not weekend and 9 <= h < 17.5:
            return (1.0 if dt.weekday() in (1, 3) else 0.0), 0.8  # WFH some days
        if 18 <= h < 22:
            return (3.0 if weekend and 19 <= h < 21 else 2.0), 1.1
        return 1.0, 1.0

    def step(self, ts: int) -> Reading:
        """Advance one interval and return the reading at ``ts``."""
        dt = datetime.fromtimestamp(ts)
        people, ach = self._occupants(dt)
        if self.rng.random() < 0.002:  # occasional window-open purge
            ach += 4.0
        hours = self.interval_s / 3600
        outdoor = 425.0
        volume_m3 = 35.0
        gen_ppm_h = people * 0.018 * 1e6 / volume_m3 * 1.0  # ~18 L/h CO2 per person
        excess = self.co2 - outdoor
        excess += (gen_ppm_h - ach * excess) * hours
        self.co2 = max(outdoor - 20, outdoor + excess + self.rng.gauss(0, 4))

        hod = dt.hour + dt.minute / 60
        heating = 1.2 if 6 <= hod < 9 or 17 <= hod < 22 else 0.0
        temp = 19.6 + 1.3 * math.sin((hod - 9) / 24 * 2 * math.pi) + heating
        temp += 0.15 * people + self.rng.gauss(0, 0.05)
        hum = 52 - 2.2 * (temp - 20.5) + 1.8 * people / 3 + self.rng.gauss(0, 0.4)

        pressure = 1013.0 + sum(
            a * math.sin(2 * math.pi * ts / p + ph) for a, p, ph in self._weather
        )
        pressure += self.rng.gauss(0, 0.05)
        # ~2-year battery life.
        self.battery = max(1.0, self.battery - 100 / (2 * 365 * 24 * 3600 / self.interval_s))
        self.counter = (self.counter + 1) % 256

        co2 = int(round(self.co2))
        status = 1 if co2 < 1000 else 2 if co2 < 1400 else 3
        return Reading(
            address=self.address,
            ts=ts,
            source="sim",
            co2=co2,
            temperature=round(temp * 20) / 20,
            humidity=float(round(min(max(hum, 20), 85))),
            pressure=round(pressure, 1),
            battery=int(self.battery),
            status=status,
            rssi=int(-70 + self.rng.gauss(0, 3)),
        )

    def generate(self, start: int, end: int) -> Iterator[Reading]:
        """Yield readings from ``start`` to ``end`` (epoch seconds) inclusive."""
        start, end = int(start), int(end)
        start -= start % self.interval_s
        for ts in range(start, end + 1, self.interval_s):
            yield self.step(ts)
