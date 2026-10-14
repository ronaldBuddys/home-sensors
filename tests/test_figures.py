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
