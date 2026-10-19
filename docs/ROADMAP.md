# Roadmap

Ideas and planned work are tracked as [GitHub issues](https://github.com/ronaldBuddys/home-sensors/issues); comments and pull requests are welcome.

## Next

| Idea | Issue |
|---|---|
| **Alerts.** Notify on stale data, CO₂ above the alert threshold for N minutes, or a low sensor battery, e.g. through ntfy.sh, a Home Assistant webhook or e-mail. | [#5](https://github.com/ronaldBuddys/home-sensors/issues/5) |
| **Annotations.** Mark "window opened" or "guests" on the chart. This would be the API's first write endpoint: a small `annotations` table (the first Alembic migration after the baseline), auth and CSRF. | [#13](https://github.com/ronaldBuddys/home-sensors/issues/13) |
| **Several rooms.** Small multiples per device and a comparison view. | [#10](https://github.com/ronaldBuddys/home-sensors/issues/10) |
| **MQTT / Home Assistant.** Publish the decoded adverts. | [#11](https://github.com/ronaldBuddys/home-sensors/issues/11) |
| **Other sensors.** Generalise naming, the data source and the data model beyond Aranet. | [#9](https://github.com/ronaldBuddys/home-sensors/issues/9) |
| **Native temperature resolution.** Store 0.05 °C instead of the library's 0.1 °C. | [#15](https://github.com/ronaldBuddys/home-sensors/issues/15) |
| **Pi 3 performance.** Measure the dashboard on a Pi 3, and consider a Plotly partial bundle to cut the cold page load. | [#14](https://github.com/ronaldBuddys/home-sensors/issues/14) |
| **Upstream fixes to Aranet4-Python:** `get_all_records()` returns nothing for Aranet4; the counter byte isn't decoded; `find_nearby()` crashes on `name=None`; the history loops have no timeouts. | [#16](https://github.com/ronaldBuddys/home-sensors/issues/16) |

## Known limitations

- **History backfill needs pairing.** Until the sensor is paired with the collector's host, detected gaps stay pending. Connection attempts back off to at most once every 6 h.
- **Long-window stats are hour-aligned.** Stats for windows longer than 7 days come from the hourly rollup, so the window edges are rounded to the hour.
- **Basic authentication.** The dashboard uses one shared user, and there's no logout. See the accepted risks in [SECURITY.md](SECURITY.md#accepted-risks).
- **Half-hour time zones.** In the weekly heatmap, each UTC hour counts towards the local hour in which it starts. For time zones with a half-hour offset, cells are therefore shifted by 30 minutes.
