"""Offline SVG charts.

No chart library and no tile server, for three reasons that are all about the
competition room rather than about taste:

1. A venue with no internet is a venue where a slippy map does not render. Nothing
   in this file fetches anything, so the demo looks the same on a plane.
2. The charts must be *legible about the specific thing this project is being
   judged on* -- that a level is assembled from a score band, an escalation and a
   hard-rule floor -- and a generic plotting API is a longer way to a less
   specific picture.
3. Every value drawn comes from a presenter field. There is no transformation in
   this file beyond scaling to pixels, so the picture cannot disagree with the
   table beneath it.

``render`` returns an SVG string for ``st.markdown(..., unsafe_allow_html=True)``.
Every string that reaches an attribute has been XML-escaped.
"""

from __future__ import annotations

from html import escape
from typing import Sequence

from demo.presenters.benchmark_view import BenchmarkView
from demo.presenters.route_view import RouteProfile
from demo.presenters.timeline import LEVEL_STYLE, Timeline

#: The four levels in rank order, so a y coordinate is a level.
LEVELS = ("GREEN", "YELLOW", "ORANGE", "RED")

GRID = "#e2e6ea"
INK = "#1c2530"
MUTED = "#6b7785"
TRACK = "#8b97a3"
TERRAIN = "#2f6fa8"
GOOD = "#1b7f3b"
BAD = "#b00020"


def _esc(text: object) -> str:
    return escape(str(text), quote=True)


def _num(value: float, digits: int = 1) -> str:
    """A number with no trailing zeros, so a table of deltas has ragged digits
    only where the evidence itself had them."""
    text = f"{value:.{digits}f}"
    return text.rstrip("0").rstrip(".") if "." in text else text


def _empty(message: str) -> str:
    return (
        f'<div style="color:{MUTED};font-family:system-ui,-apple-system,Segoe UI,sans-serif;'
        f'font-size:13px;padding:10px 2px">{_esc(message)}</div>'
    )


def _level_y(rank: int, *, pad_t: int = 28, plot_h: int = 220, top: int = 3) -> float:
    """A level's row, as a y coordinate.

    Extracted from the drawing code and named so the one transformation the charts
    do perform is testable on its own: a higher rank must never be drawn above a
    lower one, or the picture would say the opposite of the table.
    """
    return pad_t + plot_h * (1 - rank / top)


def _open(width: int, height: int, label: str) -> list[str]:
    return [
        f'<svg viewBox="0 0 {width} {height}" width="100%" '
        f'preserveAspectRatio="xMidYMid meet" role="img" aria-label="{_esc(label)}">'
    ]


# --------------------------------------------------------------------------- #
# The risk timeline: the soft-score band against the level it became
# --------------------------------------------------------------------------- #


def level_chart(timeline: Timeline, notes: Sequence[str] = (), *, width: int = 760,
                height: int = 300) -> str:
    """Two lines on level rows: the band the soft score alone produced, and the
    level the core reported.

    The gap between them *is* the finding. It is a hard rule or a compound
    escalation lifting a level above what the arithmetic said, and it is invisible
    on a single-line chart. The vertical distance is exactly the number of levels
    the escalation moved, so the picture and the assembly table cannot disagree.
    """
    points = timeline.points
    if not points:
        return _empty("No horizons to draw.")

    pad_l, pad_r, pad_t, pad_b = 54, 20, 28, 52
    plot_w, plot_h = width - pad_l - pad_r, height - pad_t - pad_b
    n = len(points)
    top = len(LEVELS) - 1
    xs = [pad_l + plot_w * (i + 0.5) / n for i in range(n)]
    ys = [_level_y(p.rank, pad_t=pad_t, plot_h=plot_h, top=top) for p in points]
    band_ys = [
        _level_y(LEVELS.index(p.score_band), pad_t=pad_t, plot_h=plot_h, top=top)
        for p in points
    ]

    parts = _open(width, height, "Risk level at each horizon, against the soft-score band")

    for index, level in enumerate(LEVELS):
        y = pad_t + plot_h * (1 - index / top)
        style = LEVEL_STYLE[level]
        parts.append(
            f'<line x1="{pad_l}" y1="{y:.1f}" x2="{width - pad_r}" y2="{y:.1f}" '
            f'stroke="{GRID}" stroke-width="1"/>'
        )
        parts.append(
            f'<text x="{pad_l - 10}" y="{y + 4:.1f}" text-anchor="end" font-size="11" '
            f'fill="{style["colour"]}" font-weight="600">{_esc(level)}</text>'
        )

    band = " ".join(f"{x:.1f},{y:.1f}" for x, y in zip(xs, band_ys))
    levels = " ".join(f"{x:.1f},{y:.1f}" for x, y in zip(xs, ys))
    parts.append(
        f'<polyline points="{band}" fill="none" stroke="{MUTED}" stroke-width="1.6" '
        f'stroke-dasharray="5 4" opacity="0.8"/>'
    )
    parts.append(
        f'<polyline points="{levels}" fill="none" stroke="{INK}" stroke-width="2.6" '
        f'stroke-linejoin="round"/>'
    )

    for index, point in enumerate(points):
        x, y = xs[index], ys[index]
        if point.escalated_above_score:
            # The escalation, drawn as the distance between the two lines.
            parts.append(
                f'<line x1="{x:.1f}" y1="{band_ys[index]:.1f}" x2="{x:.1f}" y2="{y:.1f}" '
                f'stroke="{point.colour}" stroke-width="2.2" stroke-dasharray="3 3" opacity="0.7"/>'
            )
        parts.append(
            f'<circle cx="{x:.1f}" cy="{y:.1f}" r="7.5" fill="{point.colour}" '
            f'stroke="#ffffff" stroke-width="2"/>'
        )
        parts.append(
            f'<text x="{x:.1f}" y="{y - 14:.1f}" text-anchor="middle" font-size="12.5" '
            f'font-weight="700" fill="{point.colour}">{_esc(point.level)}</text>'
        )
        parts.append(
            f'<text x="{x:.1f}" y="{height - 28}" text-anchor="middle" font-size="12" '
            f'fill="{INK}" font-weight="600">{_esc(point.label)}</text>'
        )
        note = notes[index] if index < len(notes) else ""
        parts.append(
            f'<text x="{x:.1f}" y="{height - 13}" text-anchor="middle" font-size="10.5" '
            f'fill="{MUTED}">{_esc(note)}</text>'
        )

    parts.append(f'<line x1="{pad_l}" y1="13" x2="{pad_l + 26}" y2="13" stroke="{INK}" stroke-width="2.6"/>')
    parts.append(f'<text x="{pad_l + 32}" y="17" font-size="11" fill="{INK}">level the core reported</text>')
    parts.append(
        f'<line x1="{pad_l + 200}" y1="13" x2="{pad_l + 226}" y2="13" stroke="{MUTED}" '
        f'stroke-width="1.6" stroke-dasharray="5 4"/>'
    )
    parts.append(f'<text x="{pad_l + 232}" y="17" font-size="11" fill="{MUTED}">soft-score band alone</text>')
    parts.append("</svg>")
    return "".join(parts)


# --------------------------------------------------------------------------- #
# The route, with where the hiker will be when each verdict applies
# --------------------------------------------------------------------------- #


def elevation_chart(profile: RouteProfile, *, width: int = 760, height: int = 260) -> str:
    """Altitude against distance, each horizon marked at the point the projection
    put the hiker.

    The x positions are the projection's own ``progress`` values and the y values
    are the GPX's own elevations, so this is a picture of two numbers that already
    exist, not a third number.
    """
    xs_all, ys_all = profile.elevation.x_m, profile.elevation.y_m
    if not xs_all or not ys_all or not profile.has_elevation:
        return _empty("This route carries no elevation, so no profile can be drawn.")

    pad_l, pad_r, pad_t, pad_b = 58, 20, 30, 42
    plot_w, plot_h = width - pad_l - pad_r, height - pad_t - pad_b
    max_x = max(xs_all) or 1.0
    min_y, max_y = min(ys_all), max(ys_all)
    span_y = (max_y - min_y) or 1.0

    def px(x: float) -> float:
        return pad_l + plot_w * (x / max_x)

    def py(y: float) -> float:
        return pad_t + plot_h * (1 - (y - min_y) / span_y)

    line = " ".join(f"{px(x):.1f},{py(y):.1f}" for x, y in zip(xs_all, ys_all))
    area = f"{pad_l:.1f},{pad_t + plot_h} " + line + f" {pad_l + plot_w},{pad_t + plot_h}"

    parts = _open(
        width, height, "Route elevation with the projected position at each horizon"
    )
    for fraction in (0.0, 0.25, 0.5, 0.75, 1.0):
        y = pad_t + plot_h * fraction
        parts.append(
            f'<line x1="{pad_l}" y1="{y:.1f}" x2="{width - pad_r}" y2="{y:.1f}" '
            f'stroke="{GRID}" stroke-width="1"/>'
        )
        parts.append(
            f'<text x="{pad_l - 10}" y="{y + 4:.1f}" text-anchor="end" font-size="11" '
            f'fill="{MUTED}">{_num(max_y - span_y * fraction, 0)} m</text>'
        )
    for fraction in (0.0, 0.5, 1.0):
        x = pad_l + plot_w * fraction
        parts.append(
            f'<text x="{x:.1f}" y="{height - 18}" text-anchor="middle" font-size="11" '
            f'fill="{MUTED}">{_num(max_x * fraction / 1000)} km</text>'
        )

    parts.append(f'<polygon points="{area}" fill="{TERRAIN}" opacity="0.10"/>')
    parts.append(
        f'<polyline points="{line}" fill="none" stroke="{TERRAIN}" stroke-width="2.2" '
        f'stroke-linejoin="round"/>'
    )

    total_m = (profile.total_distance_km or 1.0) * 1000
    for marker in profile.markers:
        reached = total_m * marker.progress
        # The elevation at the reached distance is the last sample at or before it.
        sample = 0
        for index, x_m in enumerate(xs_all):
            if x_m <= reached:
                sample = index
        x, y = px(reached), py(ys_all[sample])
        parts.append(
            f'<line x1="{x:.1f}" y1="{pad_t - 4}" x2="{x:.1f}" y2="{pad_t + plot_h}" '
            f'stroke="{marker.colour}" stroke-width="1.4" stroke-dasharray="3 3" opacity="0.55"/>'
        )
        parts.append(
            f'<circle cx="{x:.1f}" cy="{y:.1f}" r="6" fill="{marker.colour}" '
            f'stroke="#ffffff" stroke-width="2"/>'
        )
        parts.append(
            f'<text x="{x:.1f}" y="{pad_t - 12}" text-anchor="middle" font-size="11" '
            f'font-weight="700" fill="{marker.colour}">{_esc(f"{marker.label} {marker.level}")}</text>'
        )
    parts.append("</svg>")
    return "".join(parts)


def route_sketch(profile: RouteProfile, *, width: int = 420, height: int = 420) -> str:
    """The track itself, north up, with the four projected positions on it.

    Not a map: there is no basemap, and the only reference frame the SVG states is
    "north is up". It exists so a judge can see that the four verdicts are four
    *places* on one line, rather than four rows in a table.
    """
    coords = profile.coordinates
    if not coords:
        return _empty("This scenario's route has no geometry to draw.")

    lons = [lon for _lat, lon in coords]
    lats = [lat for lat, _lon in coords]
    pad = 26
    span = max((max(lons) - min(lons)) or 1e-6, (max(lats) - min(lats)) or 1e-6) * 1.12

    def px(lon: float) -> float:
        return pad + (width - 2 * pad) * ((lon - min(lons)) / span)

    def py(lat: float) -> float:
        return height - pad - (height - 2 * pad) * ((lat - min(lats)) / span)

    path = " ".join(f"{px(lon):.1f},{py(lat):.1f}" for lat, lon in coords)
    legend = " &#183; ".join(_esc(m.label) for m in profile.markers)

    parts = _open(
        width, height, "The route track with the projected position at each horizon"
    )
    parts.append(
        f'<polyline points="{path}" fill="none" stroke="{TRACK}" stroke-width="3" '
        f'stroke-linecap="round" stroke-linejoin="round"/>'
    )
    for marker in profile.markers:
        x, y = px(marker.lon), py(marker.lat)
        parts.append(
            f'<circle cx="{x:.1f}" cy="{y:.1f}" r="8" fill="{marker.colour}" '
            f'stroke="#ffffff" stroke-width="2.5"/>'
        )
        parts.append(
            f'<text x="{x + 13:.1f}" y="{y + 4:.1f}" font-size="12" font-weight="700" '
            f'fill="{marker.colour}">{_esc(marker.label)}</text>'
        )
    start_lat, start_lon = coords[0]
    parts.append(f'<circle cx="{px(start_lon):.1f}" cy="{py(start_lat):.1f}" r="3.5" fill="{INK}"/>')
    parts.append(
        f'<text x="{px(start_lon) + 9:.1f}" y="{py(start_lat) - 9:.1f}" font-size="10.5" '
        f'fill="{MUTED}">start</text>'
    )
    parts.append(
        f'<text x="{pad}" y="{height - 8}" font-size="10.5" fill="{MUTED}">'
        f'north is up &#183; {legend}</text>'
    )
    parts.append("</svg>")
    return "".join(parts)


# --------------------------------------------------------------------------- #
# The benchmark: every metric's recorded movement, both directions shown
# --------------------------------------------------------------------------- #


def metric_chart(view: BenchmarkView, *, width: int = 760) -> str:
    """One signed bar per metric, taken from ``mean_paired_delta`` as committed.

    The colour is the view's own classification -- ``improved`` / ``worsened`` /
    ``unchanged`` -- and not a second opinion on the sign, so the bars cannot say
    something the tables beside them do not. Red bars are red Phase 5 numbers:
    four of them are the finding, and hiding them is the one thing this tab must
    never do. There is no bar for a combined score, because Phase 5 recorded that
    no composite exists.
    """
    rows = [m for m in view.metrics if m.mean_paired_delta is not None]
    if not rows:
        return _empty("No paired deltas are recorded in the committed evidence.")

    improved = {m.metric_id for m in view.improved}
    worsened = {m.metric_id for m in view.worsened}

    row_h, pad_t, pad_l, pad_r = 24, 26, 96, 132
    height = pad_t + row_h * len(rows) + 12
    plot_w = width - pad_l - pad_r
    largest = max(abs(m.mean_paired_delta) for m in rows) or 1.0
    zero_x = pad_l + plot_w / 2

    parts = _open(
        width,
        height,
        "Paired movement per metric, positive where the metric's own direction says better",
    )
    parts.append(
        f'<line x1="{zero_x:.1f}" y1="{pad_t - 8}" x2="{zero_x:.1f}" y2="{height - 6}" '
        f'stroke="{GRID}" stroke-width="1.5"/>'
    )
    parts.append(f'<text x="{pad_l}" y="13" font-size="11" fill="{BAD}">&#9664; worse</text>')
    parts.append(
        f'<text x="{width - pad_r + 8}" y="13" font-size="11" fill="{GOOD}">better &#9654;</text>'
    )

    for index, metric in enumerate(rows):
        y = pad_t + index * row_h
        delta = metric.mean_paired_delta
        bar = plot_w / 2 * (abs(delta) / largest)
        if metric.metric_id in worsened:
            colour = BAD
        elif metric.metric_id in improved:
            colour = GOOD
        else:
            colour = MUTED
        left = zero_x if delta >= 0 else zero_x - bar
        parts.append(
            f'<rect x="{left:.1f}" y="{y + 3:.1f}" width="{max(bar, 1.4):.1f}" '
            f'height="{row_h - 10}" rx="2" fill="{colour}" opacity="0.88"/>'
        )
        parts.append(
            f'<text x="{pad_l - 8}" y="{y + row_h / 2 + 1:.1f}" text-anchor="end" '
            f'font-size="11.5" fill="{INK}">{_esc(metric.metric_id)}</text>'
        )
        sign = "+" if delta > 0 else ""
        parts.append(
            f'<text x="{width - 6}" y="{y + row_h / 2 + 1:.1f}" text-anchor="end" '
            f'font-size="11.5" fill="{INK}" font-weight="600">{_esc(sign + _num(delta, 3))}</text>'
        )
    parts.append("</svg>")
    return "".join(parts)
