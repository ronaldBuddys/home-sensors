"""Plotly figure builders (pure functions of data + theme, so they are unit-testable)."""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

import plotly.graph_objects as go
from plotly.subplots import make_subplots

from aranet_monitor.db.queries import Series

# Heatmap ramps, low -> high CO2: green (outdoor air) -> amber (elevated) -> red (high).
# Derived in OKLCH with a fixed lightness schedule (0.75 -> 0.49 light, 0.46 -> 0.74 dark, steps
# of 0.065 / 0.07), hue 140 -> 112 -> 75 -> 42 -> 25 and the highest in-gamut chroma, then checked
# with the dataviz validator (--ordinal, surfaces #fcfcfb / #1a1a19). Lightness is monotone, every
# adjacent step is >= 0.06 L and the low end clears 2:1 against the surface. The one deliberate
# exception is "Single hue": a green -> red ramp spans ~115 degrees by design, so colour-vision
# safety rests on the monotone lightness plus the labelled thresholds on the colour bar.
# Light darkens toward high CO2; dark lightens, so low values recede toward the dark surface.
_HEATMAP_LIGHT = ["#7cc36c", "#9da200", "#b37900", "#bf4700", "#ac262a"]
_HEATMAP_DARK = ["#2f6633", "#637510", "#a97500", "#e76c2b", "#ff7e77"]

# Palette validated for colour-vision deficiency. Categorical slots are used in fixed order.
THEMES = {
    "light": {
        "surface": "#fcfcfb",
        "ink": "#0b0b0b",
        "ink2": "#52514e",
        "muted": "#898781",
        "grid": "#e1e0d9",
        "axis": "#c3c2b7",
        "series": ["#2a78d6", "#eb6834", "#1baf7a", "#eda100", "#e87ba4", "#008300"],
        "heatmap": _HEATMAP_LIGHT,
    },
    "dark": {
        "surface": "#1a1a19",
        "ink": "#f0efec",
        "ink2": "#c3c2b7",
        "muted": "#898781",
        "grid": "#2c2c2a",
        "axis": "#383835",
        "series": ["#3987e5", "#d95926", "#199e70", "#c98500", "#d55181", "#008300"],
        "heatmap": _HEATMAP_DARK,
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


def _break_backward_jumps(x: list[str], s: Series) -> tuple[list, dict, dict, dict]:
    """Insert a ``None`` point wherever local time goes backwards (DST fall-back).

    The repeated hour would otherwise be drawn as a stroke running back across the plot. Returns
    new ``x`` and per-metric ``mean``/``lo``/``hi`` lists; ``s`` is not mutated.
    """
    nx: list = []
    mean: dict = {m: [] for m in s.mean}
    lo: dict = {m: [] for m in s.lo}
    hi: dict = {m: [] for m in s.hi}
    for i, xi in enumerate(x):
        if i and xi < x[i - 1]:
            nx.append(xi)
            for d in (mean, lo, hi):
                for col in d.values():
                    col.append(None)
        nx.append(xi)
        for d, src in ((mean, s.mean), (lo, s.lo), (hi, s.hi)):
            for m, col in d.items():
                col.append(src[m][i])
    return nx, mean, lo, hi


def _envelope_polygons(x: list, lo: list, hi: list) -> tuple[list, list]:
    """Closed lo–hi polygons, one per contiguous run, separated by ``None``.

    ``fill="tonexty"`` ignores ``None`` gaps and draws a wedge across them; disjoint ``toself``
    shapes keep the wash to the segments that have data. Single-point runs have no area.
    """
    px: list = []
    py: list = []
    run: list[int] = []
    for i in (*range(len(x)), None):
        if i is not None and x[i] is not None and lo[i] is not None and hi[i] is not None:
            run.append(i)
            continue
        if len(run) > 1:
            if px:
                px.append(None)
                py.append(None)
            px += [x[j] for j in run] + [x[j] for j in reversed(run)]
            py += [hi[j] for j in run] + [lo[j] for j in reversed(run)]
        run = []
    return px, py


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
    x, mean, lo, hi = _break_backward_jumps(to_local(s.t, tz), s)
    aggregated = s.bucket_s > 0
    for row, (metric, title, unit, dec) in enumerate(panels, start=1):
        color = th["series"][(row - 1) % len(th["series"])]
        fmt = f",.{dec}f"
        if aggregated:
            env_x, env_y = _envelope_polygons(x, lo[metric], hi[metric])
            fig.add_trace(
                go.Scatter(
                    x=env_x,
                    y=env_y,
                    mode="lines",
                    line={"width": 0},
                    fill="toself",
                    fillcolor=_hex_to_rgba(color, 0.14),
                    hoverinfo="skip",
                    showlegend=False,
                    connectgaps=False,
                ),
                row=row,
                col=1,
            )
            custom = list(zip(lo[metric], hi[metric], strict=True))
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
                y=mean[metric],
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


def _heatmap_colorscale(
    ramp: list[str], zmin: float, zmax: float, warn: float, alert: float
) -> list[list]:
    """Colour stops placed by value: amber at ``warn``, orange-red at ``alert``.

    Falls back to evenly spaced stops when the thresholds don't fit inside the scale (for
    example ``warn >= alert``), since Plotly needs strictly increasing positions.
    """
    values = [zmin, (zmin + warn) / 2, warn, alert, zmax]
    if not zmin < warn < alert < zmax:
        return [[i / (len(ramp) - 1), c] for i, c in enumerate(ramp)]
    pos = [(v - zmin) / (zmax - zmin) for v in values]
    pos[0], pos[-1] = 0.0, 1.0
    return [[p, c] for p, c in zip(pos, ramp, strict=True)]


def co2_heatmap_figure(
    data: dict, theme: str = "light", co2_warn: float = 1000, co2_alert: float = 1400
) -> go.Figure:
    """Mean CO₂ by weekday (rows, Monday on top) and local hour (columns).

    Parameters
    ----------
    data
        ``Queries.co2_heatmap`` output: ``weekdays``, ``hours`` and 7 × 24 ``mean``/``count``.
    theme
        ``"light"`` or ``"dark"``.
    co2_warn, co2_alert
        The "elevated" and "high" thresholds (ppm); the colour scale is amber at the first and
        orange-red at the second, and the colour bar labels both.

    Returns
    -------
    go.Figure
        One heatmap on the theme's green -> amber -> red ramp. The scale starts at the outdoor
        baseline (400 ppm) and tops out no lower than 1.15 x ``co2_alert``, so a well-ventilated
        home doesn't look alarming and the alert colour is never at the very top. Empty cells
        stay blank; every cell has its own hover.
    """
    th = THEMES[theme]
    days, hours, mean, count = data["weekdays"], data["hours"], data["mean"], data["count"]
    values = [v for row in mean for v in row if v is not None]
    if not values:
        return empty_figure("Not enough data yet for the weekly pattern", theme)

    text = []
    for day, row_mean, row_n in zip(days, mean, count, strict=True):
        cells = []
        for h, v, n in zip(hours, row_mean, row_n, strict=True):
            when = f"{day} {h:02d}:00–{(h + 1) % 24:02d}:00"
            cells.append(
                f"{when} · no data" if v is None else f"{when} · <b>{v:,} ppm</b> mean (n={n:,})"
            )
        text.append(cells)
    zmin, zmax = 400, max(co2_alert * 1.15, max(values))
    fig = go.Figure(
        go.Heatmap(
            x=hours,
            y=days,
            z=mean,
            text=text,
            hovertemplate="%{text}<extra></extra>",
            hoverongaps=True,
            colorscale=_heatmap_colorscale(th["heatmap"], zmin, zmax, co2_warn, co2_alert),
            zmin=zmin,
            zmax=zmax,
            xgap=2,
            ygap=2,
            colorbar={
                "title": {"text": "ppm", "font": {"size": 11, "color": th["muted"]}},
                "thickness": 8,
                "outlinewidth": 0,
                "tickfont": {"size": 10, "color": th["muted"]},
                "ticks": "",
                "tickmode": "array",
                "tickvals": [zmin, co2_warn, co2_alert],
                "ticktext": [f"{zmin}", f"{co2_warn:,} elevated", f"{co2_alert:,} high"],
                "separatethousands": True,
                "xpad": 12,
            },
        )
    )
    axis = {
        "showgrid": False,
        "zeroline": False,
        "showline": False,
        "ticks": "",
        "fixedrange": True,
    }
    fig.update_xaxes(
        **axis,
        tickvals=list(range(0, 24, 3)),
        ticktext=[f"{h:02d}" for h in range(0, 24, 3)],
        tickfont={"color": th["muted"], "size": 11},
    )
    fig.update_yaxes(**axis, autorange="reversed", tickfont={"color": th["ink2"], "size": 11})
    fig.update_layout(
        height=260,
        # l + card padding lines the plot up with the chart card above (8 + 56 = 16 + 48).
        margin={"l": 48, "r": 16, "t": 8, "b": 22},
        paper_bgcolor=th["surface"],
        plot_bgcolor=th["surface"],  # shows through the cell gaps
        font={"family": 'system-ui, -apple-system, "Segoe UI", sans-serif', "color": th["ink"]},
        hoverlabel={
            "bgcolor": th["surface"],
            "bordercolor": th["axis"],
            "font": {"color": th["ink"], "size": 12},
        },
        uirevision="heatmap",
    )
    return fig


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
