"""Callback wiring for the chart window (preset / dates / device / zoom)."""

from aranet_monitor.config import Settings
from aranet_monitor.web.dash_app import create_dash_app


def _callbacks(queries):
    """(outputs, inputs, runs on the server) for every callback of the dashboard."""
    app = create_dash_app(queries, Settings(db_url="sqlite://"))
    for key, cb in app.callback_map.items():
        outputs = {o.split("@")[0] for o in key.strip(".").split("...")}
        inputs = {f"{i['id']}.{i['property']}" for i in cb["inputs"]}
        yield outputs, inputs, "callback" in cb


def test_no_server_callback_sees_raw_relayouts(queries):
    # Dash drops the in-flight request of a callback when the same callback fires again.
    # Plotly sends {"autosize": true} relayouts on its own (first draw), so a server callback
    # on chart.relayoutData shared with the preset buttons let one discard a click (#25).
    # The browser filters relayouts down to real zooms first.
    for outputs, inputs, server in _callbacks(queries):
        if "chart.relayoutData" in inputs:
            assert not server and outputs == {"zoom-req.data"}


def test_one_callback_owns_the_window(queries):
    # A second writer (allow_duplicate) would race: a stale zoom response could land after a
    # newer preset click. With one owner Dash keeps only the newest request.
    owners = [inputs for outputs, inputs, _ in _callbacks(queries) if "window.data" in outputs]
    assert len(owners) == 1
    assert {"preset.value", "zoom-req.data", "device.value"} <= owners[0]
