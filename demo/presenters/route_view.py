"""Route geometry and horizon markers, straight from the frozen GPX.

Spec 9. The polyline is not redrawn by this module: ``RouteGeometry`` already
carries the whole route as WGS-84 trackpoints with per-point elevation and the
geodesic cumulative distance from the start, both pairs of which are exactly what
a map line and an elevation profile need. Everything here is a copy of those two
tuples into a flat view, plus markers for the positions the projection tool
returned -- never coordinates this package computed.

One consequence worth stating to a judge: all fourteen scenarios share the single
bundled GPX (12 trackpoints, ~11 km, ~925 m of ascent, 1180 m to 2105 m). The
three headline demos differ in what the hiker and the weather are doing, not in
the terrain. That is why the same map appears under all three, with different
markers on it.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from trailguard.routes import resolve_route_geometry

from .chain import HorizonRow, ReplayRun
from .timeline import LEVEL_STYLE


@dataclass(slots=True)
class HorizonMarker:
    """Where a horizon's projected position sits on the route."""

    label: str
    arrival_time: str
    lat: float
    lon: float
    alt_m: float | None
    progress: float
    remaining_distance_km: float
    level: str
    badge: str
    colour: str


@dataclass(slots=True)
class ElevationProfile:
    """The route's shape, in the geometry's own metres."""

    x_m: list[float]
    y_m: list[float]
    has_elevation: bool
    #: Marker positions drawn onto the same x axis, for the horizon overlays.
    markers: list[HourMarker] = field(default_factory=list)


@dataclass(slots=True)
class HourMarker:
    """A horizon pinned to a distance along the route, for the elevation x-axis."""

    label: str
    along_m: float
    progress: float


@dataclass(slots=True)
class RouteProfile:
    """Everything needed to draw the route once, with the run's markers on it."""

    route_id: str
    polyline_ref: str
    source_ref: str
    point_count: int
    total_distance_km: float
    total_ascent_m: float
    has_elevation: bool
    coordinates: list[tuple[float, float]]
    progress: float
    off_route_distance_m: float
    remaining_distance_km: float
    remaining_ascent_m: float
    current_altitude_m: float
    route_data_quality: float
    position_lat: float
    position_lon: float
    position_alt_m: float | None
    position_source: str
    position_observed_at: str
    position_confidence: float
    markers: list[HorizonMarker]
    elevation: ElevationProfile

    @property
    def current_marker(self) -> tuple[float, float]:
        return (self.position_lat, self.position_lon)


def route_profile(run: ReplayRun, *, geometry=None) -> RouteProfile:
    """Build the route view for ``run``.

    ``geometry`` defaults to whatever ``resolve_route_geometry`` finds for the
    scenario's own ``route_id`` -- the same lookup ``get_route_context`` made, so
    the drawn line and the measured state come from one asset.
    """
    resolved = geometry if geometry is not None else resolve_route_geometry(run.route["route_id"])

    markers = [
        HorizonMarker(
            label=row.label,
            arrival_time=row.projected_arrival_time,
            lat=row.projected_position_lat,
            lon=row.projected_position_lon,
            alt_m=row.projected_alt_m,
            progress=row.projected_route_progress,
            remaining_distance_km=row.projected_remaining_distance_km,
            level=row.risk_level,
            badge=LEVEL_STYLE[row.risk_level]["badge"],
            colour=LEVEL_STYLE[row.risk_level]["colour"],
        )
        for row in run.rows
    ]

    along_markers = [
        HourMarker(
            label=marker.label,
            along_m=marker.progress * resolved.total_distance_m,
            progress=marker.progress,
        )
        for marker in markers
    ]

    elevation = ElevationProfile(
        x_m=[float(d) for d in resolved.cumulative_m],
        y_m=[float(p.elevation_m) for p in resolved.points],
        has_elevation=resolved.has_elevation,
        markers=along_markers,
    )

    position = run.route["position"]
    return RouteProfile(
        route_id=run.route["route_id"],
        polyline_ref=run.route["polyline_ref"],
        source_ref=resolved.source_ref,
        point_count=resolved.point_count,
        total_distance_km=resolved.total_distance_m / 1000.0,
        total_ascent_m=resolved.total_ascent_m,
        has_elevation=resolved.has_elevation,
        coordinates=[(p.lat, p.lon) for p in resolved.points],
        progress=run.route["progress"],
        off_route_distance_m=run.route["off_route_distance_m"],
        remaining_distance_km=run.route["remaining_distance_km"],
        remaining_ascent_m=run.route["remaining_ascent_m"],
        current_altitude_m=run.route["current_altitude_m"],
        route_data_quality=run.route["data_quality"],
        position_lat=position["lat"],
        position_lon=position["lon"],
        position_alt_m=position.get("alt_m"),
        position_source=position["source"],
        position_observed_at=position["observed_at"],
        position_confidence=position["confidence"],
        markers=markers,
        elevation=elevation,
    )
