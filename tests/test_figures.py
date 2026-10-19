from datetime import datetime
from zoneinfo import ZoneInfo

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
            pts = [(x, y) for x, y in zip(tr.x, tr.y, strict=True)]
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
