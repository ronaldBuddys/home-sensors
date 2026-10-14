"""Dash dashboard. Callbacks call :class:`~aranet_monitor.db.queries.Queries` in-process."""

from __future__ import annotations

import re
import time
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import dash
from dash import ClientsideFunction, Input, Output, State, ctx, dcc, html, no_update

from aranet_monitor.config import Settings
from aranet_monitor.db.queries import RANGES, Queries
from aranet_monitor.web import figures

ASSETS = Path(__file__).parent / "assets"
PRESETS = ["1h", "6h", "24h", "7d", "30d", "90d", "1y"]
DEFAULT_PRESET = "24h"
# ~1 point per 2 px of chart width. Also picks daily buckets for 1 y (finer buckets alias the
# day/night CO₂ cycle into a solid block) and 15 min for 7 d.
CHART_POINTS = 700
GRAPH_CONFIG = {
    "displaylogo": False,
    "responsive": True,
    "modeBarButtonsToRemove": ["select2d", "lasso2d", "autoScale2d", "toggleSpikelines"],
    "toImageButtonOptions": {"format": "png", "filename": "aranet", "scale": 2},
}
_RANGE_KEY = re.compile(r"^xaxis\d*\.range\[(0|1)\]$")


def co2_status(co2: int | None, warn: int, alert: int) -> tuple[str, str, str]:
    """(css class, icon, label) for a CO₂ level; never colour alone."""
    if co2 is None:
        return "unknown", "?", "No data"
    if co2 >= alert:
        return "critical", "▲", "High — ventilate"
    if co2 >= warn:
        return "warning", "■", "Elevated"
    return "good", "●", "Good"


def _fmt(v, dec: int) -> str:
    if v is None:
        return "–"
    return f"{v:,.{dec}f}"


def _delta(cur, prev, dec: int, unit: str) -> html.Span:
    if cur is None or prev is None:
        return html.Span("", className="kpi-delta")
    d = cur - prev
    if round(d, dec) == 0:
        return html.Span(f"±0 {unit} vs 1 h ago", className="kpi-delta")
    arrow = "▲" if d > 0 else "▼"
    return html.Span(f"{arrow} {abs(d):,.{dec}f} {unit} vs 1 h ago", className="kpi-delta")


def _parse_relayout(relayout: dict | None) -> tuple[str, str] | str | None:
    """Return (x0, x1) for a zoom, "reset" for autorange, None for anything else."""
    if not relayout:
        return None
    if any(k.endswith("autorange") for k in relayout):
        return "reset"
    lo = hi = None
    for k, v in relayout.items():
        m = _RANGE_KEY.match(k)
        if m:
            if m.group(1) == "0":
                lo = v
            else:
                hi = v
        elif re.match(r"^xaxis\d*\.range$", k) and isinstance(v, list) and len(v) == 2:
            lo, hi = v
    if lo and hi:
        return str(lo), str(hi)
    return None


def _local_to_epoch(s: str, tz: ZoneInfo | None) -> int:
    dt = datetime.fromisoformat(s.replace(" ", "T")[:19])
    if tz is not None:
        dt = dt.replace(tzinfo=tz)
    return int(dt.timestamp())


def create_dash_app(queries: Queries, settings: Settings) -> dash.Dash:
    """Build the Dash app bound to a query layer."""
    tz = ZoneInfo(settings.display_tz) if settings.display_tz else None
    app = dash.Dash(
        __name__,
        title="Aranet Monitor",
        assets_folder=str(ASSETS),
        update_title=None,
        compress=False,  # FastAPI's GZipMiddleware compresses everything
        meta_tags=[{"name": "viewport", "content": "width=device-width, initial-scale=1"}],
    )

    def serve_layout():
        devices = queries.list_devices()
        options = [
            {"label": d["name"] or d["address"], "value": d["id"]}
            for d in sorted(devices, key=lambda d: -(d["latest_ts"] or 0))
        ]
        default = options[0]["value"] if options else None
        return html.Div(
            className="page",
            children=[
                dcc.Store(id="theme", storage_type="local"),
                dcc.Store(id="latest-ts"),
                dcc.Store(id="window", data={"preset": DEFAULT_PRESET}),
                dcc.Interval(id="poll", interval=settings.refresh_s * 1000),
                dcc.Interval(id="tick", interval=5000),
                html.Header(
                    className="topbar",
                    children=[
                        html.Div(
                            className="brand",
                            children=[
                                html.Span("●", className="brand-dot"),
                                html.H1("Aranet Monitor"),
                            ],
                        ),
                        html.Div(
                            className="topbar-right",
                            children=[
                                html.Span(id="freshness", className="pill"),
                                dcc.Dropdown(
                                    id="device",
                                    options=options,
                                    value=default,
                                    clearable=False,
                                    searchable=False,
                                    className="device-select",
                                    placeholder="No devices yet",
                                ),
                                html.Button(
                                    "Theme",
                                    id="theme-toggle",
                                    className="icon-btn",
                                    title="Toggle light/dark",
                                    n_clicks=0,
                                ),
                            ],
                        ),
                    ],
                ),
                html.Main(
                    children=[
                        html.Section(id="kpis", className="kpis"),
                        html.Div(
                            className="filters",
                            children=[
                                dcc.RadioItems(
                                    id="preset",
                                    options=[{"label": p, "value": p} for p in PRESETS],
                                    value=DEFAULT_PRESET,
                                    className="segmented",
                                    inputClassName="seg-input",
                                    labelClassName="seg-label",
                                ),
                                dcc.DatePickerRange(
                                    id="dates",
                                    display_format="YYYY-MM-DD",
                                    start_date_placeholder_text="Custom from",
                                    end_date_placeholder_text="to",
                                    clearable=True,
                                    minimum_nights=0,
                                    className="dates",
                                ),
                                html.Span(id="window-label", className="window-label"),
                                html.A("Download CSV", id="csv", className="btn", href="#"),
                            ],
                        ),
                        html.Section(
                            className="card chart-card",
                            children=[
                                dcc.Graph(
                                    id="chart",
                                    config=GRAPH_CONFIG,
                                    figure=figures.empty_figure("Loading…"),
                                    className="chart",
                                ),
                                html.P(
                                    "Drag to zoom (detail is reloaded for the zoomed window) · "
                                    "double-click to reset · shaded band = min–max within each "
                                    "time bucket",
                                    className="hint",
                                ),
                            ],
                        ),
                        html.Section(
                            className="card",
                            children=[html.H2("Summary for this window"), html.Div(id="summary")],
                        ),
                    ]
                ),
                html.Footer(
                    children=[
                        html.Div(id="events", className="events"),
                        html.Div(
                            [
                                html.A("API docs", href="/api/docs"),
                                " · ",
                                html.A("health", href="/api/v1/health"),
                            ],
                            className="links",
                        ),
                    ]
                ),
            ],
        )

    app.layout = serve_layout

    # ---------------------------------------------------------------- theme (client side)
    app.clientside_callback(
        ClientsideFunction("aranet", "toggleTheme"),
        Output("theme", "data"),
        Input("theme-toggle", "n_clicks"),
        State("theme", "data"),
    )

    # ---------------------------------------------------------------- polling
    @app.callback(
        Output("latest-ts", "data"),
        Input("poll", "n_intervals"),
        Input("device", "value"),
        State("latest-ts", "data"),
    )
    def poll(_n, device_id, current):
        if device_id is None:
            return no_update
        ts = queries.latest_ts(device_id)
        new = {"device": device_id, "ts": ts}
        return no_update if new == current else new

    # Relative "updated 23 s ago" runs in the browser every 5 s: no server round-trip.
    app.clientside_callback(
        ClientsideFunction("aranet", "freshness"),
        Output("freshness", "children"),
        Output("freshness", "className"),
        Input("tick", "n_intervals"),
        Input("latest-ts", "data"),
    )

    # ---------------------------------------------------------------- KPI tiles
    @app.callback(
        Output("kpis", "children"),
        Input("latest-ts", "data"),
        Input("theme", "data"),
    )
    def kpis(latest_info, theme):
        theme = theme if theme in figures.THEMES else "light"
        if not latest_info or latest_info.get("ts") is None:
            return html.Div(
                "Waiting for the first reading… start the collector with `aranet collect`.",
                className="card empty",
            )
        device_id = latest_info["device"]
        cur = queries.latest(device_id) or {}
        ago = queries.value_at(device_id, cur["ts"] - 3600) or {}
        spark = queries.series(device_id, cur["ts"] - 6 * 3600, cur["ts"], max_points=72)
        th = figures.THEMES[theme]
        tiles = []
        cls, icon, label = co2_status(cur.get("co2"), settings.co2_warn, settings.co2_alert)
        tiles.append(
            html.Div(
                className="card kpi kpi-hero",
                children=[
                    html.Div("CO₂", className="kpi-label"),
                    html.Div(
                        [
                            html.Span(_fmt(cur.get("co2"), 0), className="kpi-value"),
                            html.Span("ppm", className="kpi-unit"),
                        ]
                    ),
                    html.Div(
                        [html.Span(icon, className="status-icon"), label],
                        className=f"status status-{cls}",
                    ),
                    _delta(cur.get("co2"), ago.get("co2"), 0, "ppm"),
                    dcc.Graph(
                        figure=figures.sparkline(spark.mean.get("co2", []), th["series"][0], theme),
                        config={"staticPlot": True},
                        className="spark",
                    ),
                ],
            )
        )
        for i, (metric, title, unit, dec) in enumerate(figures.PANELS[1:], start=1):
            tiles.append(
                html.Div(
                    className="card kpi",
                    children=[
                        html.Div(title, className="kpi-label"),
                        html.Div(
                            [
                                html.Span(_fmt(cur.get(metric), dec), className="kpi-value"),
                                html.Span(unit, className="kpi-unit"),
                            ]
                        ),
                        _delta(cur.get(metric), ago.get(metric), dec, unit),
                        dcc.Graph(
                            figure=figures.sparkline(
                                spark.mean.get(metric, []), th["series"][i], theme
                            ),
                            config={"staticPlot": True},
                            className="spark",
                        ),
                    ],
                )
            )
        batt = cur.get("battery")
        batt_cls = (
            "critical"
            if batt is not None and batt < 15
            else ("warning" if batt is not None and batt < 30 else "good")
        )
        tiles.append(
            html.Div(
                className="card kpi kpi-small",
                children=[
                    html.Div("Battery", className="kpi-label"),
                    html.Div(
                        [
                            html.Span(_fmt(batt, 0), className="kpi-value"),
                            html.Span("%", className="kpi-unit"),
                        ]
                    ),
                    html.Div(
                        className=f"meter meter-{batt_cls}",
                        children=html.Div(style={"width": f"{batt or 0}%"}),
                    ),
                    html.Div(
                        f"Signal {cur.get('rssi')} dBm" if cur.get("rssi") is not None else "",
                        className="kpi-delta",
                    ),
                ],
            )
        )
        return tiles

    # ---------------------------------------------------------------- window selection
    @app.callback(
        Output("window", "data"),
        Output("preset", "value"),
        Output("dates", "start_date"),
        Output("dates", "end_date"),
        Input("preset", "value"),
        Input("dates", "start_date"),
        Input("dates", "end_date"),
        Input("chart", "relayoutData"),
        Input("device", "value"),
        State("window", "data"),
        prevent_initial_call=True,
    )
    def choose_window(preset, d0, d1, relayout, _device, window):
        trig = ctx.triggered_id
        if trig == "device":
            # A zoomed/custom window belongs to the previous device; go back to a live preset.
            base = (window or {}).get("base") or window or {}
            p = base.get("preset") or preset or DEFAULT_PRESET
            return {"preset": p}, p, None, None
        if trig == "preset" and preset:
            return {"preset": preset}, no_update, None, None
        if trig == "dates":
            if d0 and d1:
                start = _local_to_epoch(f"{d0[:10]}T00:00:00", tz)
                end = _local_to_epoch(f"{d1[:10]}T23:59:59", tz)
                return {"start": start, "end": end, "custom": True}, None, no_update, no_update
            if not d0 and not d1:
                return {"preset": DEFAULT_PRESET}, DEFAULT_PRESET, no_update, no_update
            return no_update, no_update, no_update, no_update
        if trig == "chart":
            parsed = _parse_relayout(relayout)
            if parsed == "reset":
                base = window.get("base") or {"preset": DEFAULT_PRESET}
                return base, base.get("preset") or no_update, no_update, no_update
            if isinstance(parsed, tuple):
                start, end = (_local_to_epoch(p, tz) for p in parsed)
                base = window.get("base") or window
                return {"start": start, "end": end, "zoom": True, "base": base}, *([no_update] * 3)
        return no_update, no_update, no_update, no_update

    # ---------------------------------------------------------------- chart + summary
    @app.callback(
        Output("chart", "figure"),
        Output("summary", "children"),
        Output("csv", "href"),
        Output("window-label", "children"),
        Input("window", "data"),
        Input("latest-ts", "data"),
        Input("theme", "data"),
    )
    def chart(window, latest_info, theme):
        theme = theme if theme in figures.THEMES else "light"
        if not latest_info or latest_info.get("ts") is None:
            return figures.empty_figure("No readings yet", theme), "", "#", ""
        is_live = "preset" in (window or {})
        if ctx.triggered_id == "latest-ts" and not is_live:
            return no_update, no_update, no_update, no_update  # zoomed/custom: hold the frame
        device_id = latest_info["device"]
        dev = queries.device(device_id) or {}
        if is_live:
            end = int(time.time())
            start = end - RANGES[window["preset"]]
            uirev = f"{device_id}-{window['preset']}"
            x_range = None
        else:
            start, end = int(window["start"]), int(window["end"])
            base = window.get("base", {})
            uirev = (
                f"{device_id}-{base.get('preset', 'custom')}"
                if window.get("zoom")
                else (f"{device_id}-{start}-{end}")
            )
            x_range = tuple(figures.to_local([start, end], tz))
        s = queries.series(
            device_id,
            start,
            end,
            # Up to 24 h stays raw (1440 pts at 60 s); longer windows aggregate.
            max_points=1500 if end - start <= 86400 + 60 else CHART_POINTS,
            raw_interval_s=dev.get("interval_s") or 60,
        )
        fig = figures.timeseries_figure(
            s,
            theme=theme,
            tz=tz,
            co2_warn=settings.co2_warn,
            co2_alert=settings.co2_alert,
            x_range=x_range,
            uirevision=uirev,
        )
        st = queries.stats(device_id, start, end, settings.co2_warn, settings.co2_alert)
        csv = f"/api/v1/devices/{device_id}/export.csv?start={start}&end={end}"
        res = "raw" if s.bucket_s == 0 else f"{_human(s.bucket_s)} averages"
        label = f"{_when(start, tz)} → {_when(end, tz)} · {res}"
        return fig, _summary_table(st, settings), csv, label

    # ---------------------------------------------------------------- events
    @app.callback(Output("events", "children"), Input("latest-ts", "data"))
    def recent_events(_):
        evs = queries.recent_events(4)
        if not evs:
            return ""
        return [
            html.Span(f"{_when(e['ts'], tz)} {e['kind']}: {e['detail'] or ''}", className="event")
            for e in evs
        ]

    return app


def _human(seconds: int) -> str:
    if seconds % 86400 == 0:
        return f"{seconds // 86400} d"
    if seconds % 3600 == 0:
        return f"{seconds // 3600} h"
    return f"{seconds // 60} min"


def _when(ts: int, tz: ZoneInfo | None) -> str:
    return datetime.fromtimestamp(ts, tz).strftime("%d %b %H:%M")


def _summary_table(st: dict, settings: Settings) -> html.Table | html.P:
    if not st.get("n"):
        return html.P("No readings in this window.", className="muted")
    rows = []
    for metric, title, unit, dec in figures.PANELS:
        rows.append(
            html.Tr(
                [
                    html.Th(title, scope="row"),
                    html.Td(_fmt(st.get(f"{metric}_min"), dec)),
                    html.Td(_fmt(st.get(f"{metric}_mean"), dec)),
                    html.Td(_fmt(st.get(f"{metric}_max"), dec)),
                    html.Td(unit, className="muted"),
                ]
            )
        )
    table = html.Table(
        className="summary",
        children=[
            html.Thead(
                html.Tr([html.Th(""), html.Th("min"), html.Th("mean"), html.Th("max"), html.Th("")])
            ),
            html.Tbody(rows),
        ],
    )
    pw, pa = st.get("pct_warn"), st.get("pct_alert")
    exposure = html.P(
        [
            html.Span("CO₂ time above thresholds: ", className="muted"),
            html.Span(["■ ", f"{pw:.1f}% elevated"], className="status status-warning")
            if pw is not None
            else "",
            " ",
            html.Span(["▲ ", f"{pa:.1f}% high"], className="status status-critical")
            if pa is not None
            else "",
            html.Span(f" · {st['n']:,} readings", className="muted"),
        ],
        className="exposure",
    )
    _ = settings
    return html.Div([table, exposure])
