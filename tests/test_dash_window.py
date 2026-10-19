"""Callback wiring for the chart window (preset / dates / device / zoom)."""

from aranet_monitor.config import Settings
from aranet_monitor.web.dash_app import create_dash_app

USER_CHOICES = {"preset.value", "dates.start_date", "dates.end_date", "device.value"}


def _callbacks(queries):
    app = create_dash_app(queries, Settings(db_url="sqlite://"))
    for key, cb in app.callback_map.items():
        inputs = {f"{i['id']}.{i['property']}" for i in cb["inputs"]}
        yield key, inputs


def test_chart_relayout_has_its_own_callback(queries):
    # Dash drops the in-flight request of a callback when the same callback fires again.
    # Plotly sends {"autosize": true} relayouts on its own (first draw, resize), so if they
    # shared a callback with the preset buttons, one landing mid-request would discard the
    # user's click and leave the old window on screen (#25).
    for key, inputs in _callbacks(queries):
        if "chart.relayoutData" in inputs:
            assert not inputs & USER_CHOICES, key


def test_zoom_still_sets_the_window(queries):
    assert any(
        "chart.relayoutData" in inputs and "window.data" in key
        for key, inputs in _callbacks(queries)
    )
