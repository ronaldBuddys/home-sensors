from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from aranet_monitor.db.queries import Series
from aranet_monitor.web import figures
from aranet_monitor.web.dash_app import _parse_relayout, co2_status


def series(n=10, bucket=0):
    t = list(range(0, 60 * n, 60))
    vals = {
        "co2": [800 + i for i in range(n)],
        "temperature": [22.1] * (n - 1) + [22.2],
        "humidity": [54.0] * n,
        "pressure": [1025.8] * n,
    }
    return Series(t=t, mean=vals, lo=vals, hi=vals, bucket_s=bucket, count=n)


def test_min_span_keeps_small_changes_small():
    fig = figures.timeseries_figure(series())
    yr = fig.layout.yaxis2.range  # temperature panel
    assert yr[1] - yr[0] >= 1.0 - 1e-9
    assert yr[0] < 22.1 and yr[1] > 22.2


def test_panels_without_data_are_dropped():
    s = series()
    s.mean["pressure"] = [None] * 10
    fig = figures.timeseries_figure(s)
    titles = [a.text for a in fig.layout.annotations if "(" in (a.text or "")]
    assert all("Pressure" not in t for t in titles)


def test_empty_series_gives_placeholder():
    s = Series(t=[], mean={}, lo={}, hi={}, bucket_s=0, count=0)
    fig = figures.timeseries_figure(s)
    assert "No data" in fig.layout.annotations[0].text


def test_dark_theme_colours():
    fig = figures.timeseries_figure(series(), theme="dark")
    assert fig.layout.paper_bgcolor == figures.THEMES["dark"]["surface"]


def test_co2_status_labels():
    assert co2_status(None, 1000, 1400)[2] == "No data"
    assert co2_status(999, 1000, 1400)[0] == "good"
    assert co2_status(1000, 1000, 1400)[0] == "warning"
    assert co2_status(1400, 1000, 1400)[0] == "critical"


def test_parse_relayout():
    assert _parse_relayout(None) is None
    assert _parse_relayout({"autosize": True}) is None
    assert _parse_relayout({"xaxis.autorange": True}) == "reset"
    assert _parse_relayout(
        {"xaxis3.range[0]": "2026-01-01 00:00", "xaxis3.range[1]": "2026-01-02"}
    ) == ("2026-01-01 00:00", "2026-01-02")
    assert _parse_relayout({"xaxis.range": ["a", "b"]}) == ("a", "b")


def _london_fall_back_series(bucket):
    tz = ZoneInfo("Europe/London")
    t0 = int(datetime(2026, 10, 25, 0, 30, tzinfo=tz).timestamp())
    n = 16
    t = [t0 + 600 * k for k in range(n)]
    vals = {
        "co2": [800 + i for i in range(n)],
        "temperature": [22.1] * n,
        "humidity": [54.0] * n,
        "pressure": [1025.8] * n,
    }
    return tz, Series(t=t, mean=vals, lo=vals, hi=vals, bucket_s=bucket, count=n)


def test_fall_back_hour_has_no_backward_segment():
    for bucket in (0, 600):
        tz, s = _london_fall_back_series(bucket)
        n_t, co2 = len(s.t), list(s.mean["co2"])
        fig = figures.timeseries_figure(s, tz=tz)
        assert len(s.t) == n_t and s.mean["co2"] == co2
        for tr in fig.data:
            paths = [list(zip(tr.x, tr.y, strict=True))]
            if tr.fill:  # polygons close by retracing; only the outbound (hi) half is a path
                paths = [piece[: len(piece) // 2] for piece in _pieces(tr.y, tr.x)]
            for pts in paths:
                for (x0, y0), (x1, y1) in zip(pts, pts[1:], strict=False):
                    if y0 is not None and y1 is not None:
                        assert x1 >= x0, (bucket, x0, x1)
        mean = [tr for tr in fig.data if tr.name == "CO₂"][0]
        assert len(mean.x) == 17
        assert sum(y is None for y in mean.y) == 1


def test_no_breaks_without_dst_change():
    s = series()
    fig = figures.timeseries_figure(s)
    assert all(len(tr.x) == len(s.t) for tr in fig.data)


def _gap_series(n=9, k=4):
    t = list(range(0, 600 * n, 600))
    mean = {
        m: [100.0 + i if i != k else None for i in range(n)] for m in [p[0] for p in figures.PANELS]
    }
    lo = {
        m: [None if v is None else v - 5 - i for i, v in enumerate(col)] for m, col in mean.items()
    }
    hi = {
        m: [None if v is None else v + 7 + i for i, v in enumerate(col)] for m, col in mean.items()
    }
    return Series(t=t, mean=mean, lo=lo, hi=hi, bucket_s=600, count=n)


def _pieces(values, xs):
    """Split parallel ``xs``/``values`` lists on ``None`` separators in ``xs``."""
    out, cur = [], []
    for x, v in zip(xs, values, strict=True):
        if x is None:
            out.append(cur)
            cur = []
        else:
            cur.append((x, v))
    return [*out, cur]


def test_envelope_never_spans_a_gap():
    n, k = 9, 4
    s = _gap_series(n, k)
    fig = figures.timeseries_figure(s)
    x = figures.to_local(s.t, None)
    fills = [tr for tr in fig.data if tr.fill]
    assert fills
    for tr in fills:
        for piece in _pieces(tr.y, tr.x):
            xs = [p[0] for p in piece]
            assert all(v <= x[k - 1] for v in xs) or all(v >= x[k + 1] for v in xs)


def test_envelope_covers_each_segment():
    n, k = 9, 4
    s = _gap_series(n, k)
    fig = figures.timeseries_figure(s)
    fills = [tr for tr in fig.data if tr.fill]
    assert len(fills) == len(figures.PANELS)
    for tr, (metric, *_rest) in zip(fills, figures.PANELS, strict=True):
        pieces = _pieces(tr.y, tr.x)
        assert len(pieces) == 2
        for piece, seg in zip(pieces, (range(0, k), range(k + 1, n)), strict=True):
            ys = [p[1] for p in piece]
            assert min(ys) == min(s.lo[metric][i] for i in seg)
            assert max(ys) == max(s.hi[metric][i] for i in seg)


def _heatmap_data(fill=None):
    mean = [[fill(d, h) if fill else None for h in range(24)] for d in range(7)]
    count = [[0 if v is None else 60 for v in row] for row in mean]
    days = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
    return {"weekdays": days, "hours": list(range(24)), "mean": mean, "count": count}


def _oklab_l(hex_color: str) -> float:
    def lin(c):
        c /= 255
        return c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4

    r, g, b = (lin(int(hex_color[i : i + 2], 16)) for i in (1, 3, 5))
    ls = (0.4122214708 * r + 0.5363325363 * g + 0.0514459929 * b) ** (1 / 3)
    ms = (0.2119034982 * r + 0.6806995451 * g + 0.1073969566 * b) ** (1 / 3)
    ss = (0.0883024619 * r + 0.2817188376 * g + 0.6299787005 * b) ** (1 / 3)
    return 0.2104542553 * ls + 0.7936177850 * ms - 0.0040720468 * ss


def test_co2_heatmap_figure():
    data = _heatmap_data(lambda d, h: None if (d, h) == (6, 3) else 500 + 40 * h + d)
    fig = figures.co2_heatmap_figure(data)
    assert [tr.type for tr in fig.data] == ["heatmap"]
    hm = fig.data[0]
    assert hm.zmin == 400 and hm.zmax == pytest.approx(1610)  # alert * 1.15 beats the data max
    light = figures.THEMES["light"]["heatmap"]
    assert hm.colorscale[0][1].lower() == light[0] and hm.colorscale[-1][1].lower() == light[-1]
    assert hm.xgap == 2 and hm.ygap == 2
    assert fig.layout.yaxis.autorange == "reversed" and list(hm.y)[0] == "Mon"
    assert hm.z[6][3] is None and "no data" in hm.text[6][3]
    assert "Tue 14:00–15:00" in hm.text[1][14] and "1,061 ppm" in hm.text[1][14]

    dark_ramp = figures.THEMES["dark"]["heatmap"]
    dark = figures.co2_heatmap_figure(data, theme="dark").data[0]
    assert dark_ramp != light  # the dark theme picks its own stops
    assert [c.lower() for _, c in dark.colorscale] == dark_ramp


def test_co2_heatmap_figure_scale_and_empty():
    calm = figures.co2_heatmap_figure(_heatmap_data(lambda d, h: 600 if h == 9 else 450))
    assert calm.data[0].zmax == pytest.approx(1610)  # a well-ventilated home doesn't fill the ramp
    hot = figures.co2_heatmap_figure(_heatmap_data(lambda d, h: 2000)).data[0]
    assert hot.zmax == 2000
    empty = figures.co2_heatmap_figure(_heatmap_data())
    assert not [tr for tr in empty.data if tr.type == "heatmap"]
    assert "Not enough data" in empty.layout.annotations[0].text


@pytest.mark.parametrize(
    ("warn", "alert"), [(1000, 1400), (800, 1200)], ids=["defaults", "custom-800-1200"]
)
@pytest.mark.parametrize("peak", [450, 3000], ids=["data-below", "data-above"])
@pytest.mark.parametrize("theme", ["light", "dark"])
def test_co2_heatmap_colour_stops_follow_thresholds(warn, alert, peak, theme):
    data = _heatmap_data(lambda d, h: peak if (d, h) == (2, 12) else 500)
    hm = figures.co2_heatmap_figure(data, theme, co2_warn=warn, co2_alert=alert).data[0]
    zmin, zmax = hm.zmin, hm.zmax
    assert zmax == pytest.approx(max(alert * 1.15, peak))
    pos = [p for p, _ in hm.colorscale]
    assert pos[0] == 0 and pos[-1] == 1 and pos == sorted(set(pos))  # Plotly's requirements
    ramp = figures.THEMES[theme]["heatmap"]
    assert [c.lower() for _, c in hm.colorscale] == ramp
    value = lambda p: zmin + p * (zmax - zmin)  # noqa: E731
    assert value(pos[1]) == pytest.approx((zmin + warn) / 2)
    assert value(pos[2]) == pytest.approx(warn)  # amber at "elevated"
    assert value(pos[3]) == pytest.approx(alert)  # orange-red at "high"
    # Same story on the colour bar, in words as well as colour.
    cb = hm.colorbar
    assert list(cb.tickvals) == [400, warn, alert]
    assert "elevated" in cb.ticktext[1] and f"{warn:,}" in cb.ticktext[1]
    assert "high" in cb.ticktext[2] and f"{alert:,}" in cb.ticktext[2]
    assert cb.nticks is None


@pytest.mark.parametrize(("warn", "alert"), [(1400, 1000), (1000, 1000), (300, 1400)])
def test_co2_heatmap_misconfigured_thresholds_space_stops_evenly(warn, alert):
    data = _heatmap_data(lambda d, h: 700)
    hm = figures.co2_heatmap_figure(data, co2_warn=warn, co2_alert=alert).data[0]
    pos = [p for p, _ in hm.colorscale]
    assert pos == pytest.approx([i / (len(pos) - 1) for i in range(len(pos))])


def test_heatmap_ramps_are_monotonic_in_lightness():
    for theme, sign in (("light", -1), ("dark", 1)):
        # light: darker toward high CO2; dark: lighter (low values recede toward the surface)
        ls = [_oklab_l(c) for c in figures.THEMES[theme]["heatmap"]]
        steps = [b - a for a, b in zip(ls, ls[1:], strict=False)]
        assert all(sign * d >= 0.06 for d in steps), (theme, ls)
