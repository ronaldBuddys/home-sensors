"""Plotly figure builders (pure functions of data + theme, so they are unit-testable)."""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

import plotly.graph_objects as go
from plotly.subplots import make_subplots

from aranet_monitor.db.queries import Series

# Reference palette (dataviz skill, references/palette.md). Categorical slots in fixed order.
THEMES = {
    "light": {
        "surface": "#fcfcfb",
        "ink": "#0b0b0b",
        "ink2": "#52514e",
        "muted": "#898781",
        "grid": "#e1e0d9",
        "axis": "#c3c2b7",
        "series": ["#2a78d6", "#eb6834", "#1baf7a", "#eda100", "#e87ba4", "#008300"],
    },
    "dark": {
        "surface": "#1a1a19",
        "ink": "#f0efec",
        "ink2": "#c3c2b7",
        "muted": "#898781",
        "grid": "#2c2c2a",
        "axis": "#383835",
        "series": ["#3987e5", "#d95926", "#199e70", "#c98500", "#d55181", "#008300"],
    },
}
# Smallest y-axis span per panel, so quantisation steps (0.1 °C, 1 %RH) and sensor noise
# don't fill the whole panel and look dramatic.
MIN_SPAN = {"temperature": 1.0, "humidity": 6.0, "pressure": 4.0}

# Status palette (fixed across themes); only used where colour *means* good/warn/critical.
STATUS = {"good": "#0ca30c", "warning": "#fab219", "critical": "#d03b3b"}

PANELS: tuple[tuple[str, str, str, int], ...] = (
    # metric, title, unit, decimals
    ("co2", "CO₂", "ppm", 0),
    ("temperature", "Temperature", "°C", 1),
    ("humidity", "Relative humidity", "%", 0),
    ("pressure", "Pressure", "hPa", 1),
)


def _hex_to_rgba(hex_color: str, alpha: float) -> str:
    h = hex_color.lstrip("#")
    r, g, b = (int(h[i : i + 2], 16) for i in (0, 2, 4))
    return f"rgba({r},{g},{b},{alpha})"


def to_local(ts: list[int], tz: ZoneInfo | None) -> list[str]:
    """Epoch seconds -> naive local ISO strings (Plotly renders them without shifting)."""
    out = []
    for t in ts:
        out.append(datetime.fromtimestamp(t, tz).strftime("%Y-%m-%dT%H:%M:%S"))
    return out


def empty_figure(message: str, theme: str = "light") -> go.Figure:
    """Placeholder with a centred message (no data yet, etc.)."""
    th = THEMES[theme]
    fig = go.Figure()
    fig.add_annotation(
        text=message,
        showarrow=False,
        font={"size": 15, "color": th["ink2"]},
        xref="paper",
        yref="paper",
        x=0.5,
        y=0.5,
    )
    fig.update_layout(
        paper_bgcolor=th["surface"],
        plot_bgcolor=th["surface"],
        height=320,
        xaxis={"visible": False},
        yaxis={"visible": False},
        margin={"l": 10, "r": 10, "t": 10, "b": 10},
    )
    return fig


def timeseries_figure(
    s: Series,
    *,
    theme: str = "light",
    tz: ZoneInfo | None = None,
    co2_warn: int = 1000,
    co2_alert: int = 1400,
    panels: tuple = PANELS,
    x_range: tuple[str, str] | None = None,
    uirevision: str = "keep",
) -> go.Figure:
    """Stacked, x-linked small multiples: one single-series panel per metric.

    Each panel has its own y-axis (never two y-scales on one plot). Aggregated windows draw a
    min–max envelope as a 10 % wash behind the mean line. The CO₂ panel shades the
    good/elevated/high bands with the status palette and labels the thresholds.
    """
    th = THEMES[theme]
    panels = tuple(p for p in panels if p[0] in s.mean and any(v is not None for v in s.mean[p[0]]))
    if not panels:
        return empty_figure("No data in this time range", theme)

    n = len(panels)
    fig = make_subplots(
        rows=n,
        cols=1,
        shared_xaxes=True,
        vertical_spacing=0.045,
        subplot_titles=[f"{title} ({unit})" for _, title, unit, _ in panels],
    )
    # Style subplot titles now, before any other annotations (threshold labels) are added.
    fig.update_annotations(font={"size": 13, "color": th["ink2"]}, xanchor="left", x=0)
    x = to_local(s.t, tz)
    aggregated = s.bucket_s > 0
    for row, (metric, title, unit, dec) in enumerate(panels, start=1):
        color = th["series"][(row - 1) % len(th["series"])]
        fmt = f",.{dec}f"
        if aggregated:
            fig.add_trace(
                go.Scatter(
                    x=x,
                    y=s.hi[metric],
                    mode="lines",
                    line={"width": 0},
                    hoverinfo="skip",
                    showlegend=False,
                    connectgaps=False,
                ),
                row=row,
                col=1,
            )
            fig.add_trace(
                go.Scatter(
                    x=x,
                    y=s.lo[metric],
                    mode="lines",
                    line={"width": 0},
                    fill="tonexty",
                    fillcolor=_hex_to_rgba(color, 0.14),
                    hoverinfo="skip",
                    showlegend=False,
                    connectgaps=False,
                ),
                row=row,
                col=1,
            )
            custom = list(zip(s.lo[metric], s.hi[metric], strict=True))
            hover = (
                f"<b>%{{y:{fmt}}} {unit}</b> {title}"
                f" <span style='opacity:.7'>(%{{customdata[0]:{fmt}}}"
                f"–%{{customdata[1]:{fmt}}})</span>"
                "<extra></extra>"
            )
        else:
            custom = None
            hover = f"<b>%{{y:{fmt}}} {unit}</b> {title}<extra></extra>"
        fig.add_trace(
            go.Scatter(
                x=x,
                y=s.mean[metric],
                mode="lines",
                name=title,
                line={"width": 2, "color": color, "shape": "linear"},
                customdata=custom,
                hovertemplate=hover,
                showlegend=False,
                connectgaps=False,
            ),
            row=row,
            col=1,
        )
        if metric == "co2":
            _co2_bands(fig, row, s, co2_warn, co2_alert, th)
        elif metric in MIN_SPAN:
            yr = _padded_range(s.lo[metric], s.hi[metric], MIN_SPAN[metric])
            if yr:
                fig.update_yaxes(range=yr, row=row, col=1)

    grid = {
        "showgrid": True,
        "gridcolor": th["grid"],
        "gridwidth": 1,
        "zeroline": False,
        "linecolor": th["axis"],
        "tickfont": {"color": th["muted"], "size": 11},
    }
    fig.update_xaxes(
        **grid,
        showspikes=True,
        spikemode="across",
        spikesnap="cursor",
        spikecolor=th["muted"],
        spikethickness=1,
        spikedash="solid",
    )
    fig.update_yaxes(**grid, nticks=5, separatethousands=True, fixedrange=True)
    if x_range:
        fig.update_xaxes(range=list(x_range))
    fig.update_layout(
        height=190 * n + 40,
        margin={"l": 56, "r": 16, "t": 28, "b": 28},
        paper_bgcolor=th["surface"],
        plot_bgcolor=th["surface"],
        font={"family": 'system-ui, -apple-system, "Segoe UI", sans-serif', "color": th["ink"]},
        hovermode="x unified",
        hoversubplots="axis",
        hoverlabel={
            "bgcolor": th["surface"],
            "bordercolor": th["axis"],
            "font": {"color": th["ink"], "size": 12},
        },
        uirevision=uirevision,
        dragmode="zoom",
    )
    return fig


def _padded_range(lo: list, hi: list, min_span: float) -> list[float] | None:
    """Y range covering the data with 6 % padding, widened to at least ``min_span``."""
    lows = [v for v in lo if v is not None]
    highs = [v for v in hi if v is not None]
    if not lows or not highs:
        return None
    a, b = min(lows), max(highs)
    span = b - a
    if span < min_span:
        mid = (a + b) / 2
        return [mid - min_span / 2, mid + min_span / 2]
    pad = span * 0.06
    return [a - pad, b + pad]


def _co2_bands(fig: go.Figure, row: int, s: Series, warn: int, alert: int, th: dict) -> None:
    vals = [v for v in s.hi["co2"] if v is not None]
    top = max(vals) if vals else alert
    yref = "y" if row == 1 else f"y{row}"
    # Faint status washes behind the data; only drawn where the data reaches them.
    fig.add_hrect(
        y0=0,
        y1=warn,
        fillcolor=STATUS["good"],
        opacity=0.05,
        line_width=0,
        layer="below",
        row=row,
        col=1,
    )
    if top >= warn * 0.9:
        fig.add_hrect(
            y0=warn,
            y1=alert,
            fillcolor=STATUS["warning"],
            opacity=0.08,
            line_width=0,
            layer="below",
            row=row,
            col=1,
        )
    if top >= alert * 0.9:
        fig.add_hrect(
            y0=alert,
            y1=max(top * 1.05, alert * 1.1),
            fillcolor=STATUS["critical"],
            opacity=0.07,
            line_width=0,
            layer="below",
            row=row,
            col=1,
        )
    for level, label in ((warn, "elevated"), (alert, "high")):
        if top >= level * 0.9:
            fig.add_hline(
                y=level, line={"color": th["muted"], "width": 1, "dash": "dot"}, row=row, col=1
            )
            fig.add_annotation(
                x=1,
                xref="paper",
                y=level,
                yref=yref,
                text=f"{level} {label}",
                showarrow=False,
                xanchor="right",
                yanchor="bottom",
                font={"size": 10, "color": th["ink2"]},
                bgcolor=th["surface"],
                opacity=0.9,
                borderpad=1,
            )
    lows = [v for v in s.lo["co2"] if v is not None]
    lo = min(lows) if lows else 400
    fig.update_yaxes(range=[max(0, min(lo, 400) - 30), max(top * 1.05, warn * 0.8)], row=row, col=1)


def sparkline(values: list[float | None], color: str, theme: str = "light") -> go.Figure:
    """Tiny trend line for a KPI tile (no axes, no hover)."""
    th = THEMES[theme]
    pts = [v for v in values if v is not None]
    fig = go.Figure(
        go.Scatter(
            y=values,
            mode="lines",
            line={"width": 1.5, "color": color},
            hoverinfo="skip",
            connectgaps=True,
        )
    )
    if pts:
        fig.add_trace(
            go.Scatter(
                x=[len(values) - 1],
                y=[values[-1]],
                mode="markers",
                marker={"size": 5, "color": color},
                hoverinfo="skip",
            )
        )
    fig.update_layout(
        height=36,
        margin={"l": 0, "r": 4, "t": 2, "b": 2},
        showlegend=False,
        paper_bgcolor="rgba(0,0,0,0)",
        plot_bgcolor="rgba(0,0,0,0)",
        xaxis={"visible": False},
        yaxis={"visible": False},
    )
    _ = th
    return fig
