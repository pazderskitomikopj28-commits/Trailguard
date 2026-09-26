"""Deterministic route geometry (GPX parsing, geodesic measurement, projection).

This package is the *geometry* half of TrailGuard's deterministic core. It turns a
GPX asset into measurements a rule can be evaluated against -- progress along the
route, distance still to walk, ascent still to climb, how far the hiker is from
the planned line -- and it walks the polyline to answer "where would the hiker be
after N more minutes?".

It deliberately depends on neither the tool layer (``trailguard/tools/``) nor the
risk core (``trailguard/risk_core/``): both consume it, and it imports neither, so
a change in a tool contract can never quietly change a metre.

Why the three libraries are imported lazily
-------------------------------------------
``gpxpy``, ``shapely`` and ``pyproj`` are declared as the ``route`` extra of
``pyproject.toml`` (spec 23). Importing them at module import time would make
``trailguard.tools`` -- and therefore the harness and the whole contract layer --
unimportable on a machine that only installed the base dependencies, which is
exactly the footprint ``tests/test_packaging.py`` polices. Every import therefore
happens inside the function that needs it, and a missing extra raises
:class:`RouteAssetError` naming the install command instead of an opaque
``ModuleNotFoundError``.

Coordinates are not a plane
---------------------------
Latitude/longitude pairs are angles on an ellipsoid, not ``(x, y)`` coordinates, so
this module never measures them with Pythagoras. Two different tools are used for
two different jobs, and the split is documented here because it is the one place a
reviewer could reasonably suspect a hidden flat-earth approximation:

* **every reported metre is a geodesic.** ``pyproj.Geod`` (WGS-84) gives the
  distance between the hiker's fix and the snapped point, the length of each
  polyline segment, and the point reached when walking ``d`` metres from a
  trackpoint (``Geod.fwd``). Total route length, ``remaining_distance_km``,
  ``off_route_distance_m`` and the along-route distances are all geodesic.
* **the projection is used only to answer "which point of the polyline is
  closest?".** Finding the nearest point on a polyline is a planar operation, so
  the route and the fix are transformed into a local azimuthal-equidistant
  projection centred on the fix (exact from its centre, and sub-centimetre in
  error at the scale of a hiking route) and ``shapely``'s ``LineString`` solves
  the nearest-point and along-the-line arithmetic there. The *result* is
  converted back and re-measured geodesically, so the projection never leaks into
  a reported number -- it only decides which trackpoint pair to interpolate in.

No DEM, no raster, no elevation service: elevation comes from the GPX itself and
``remaining_ascent_m`` is a sum of positive elevation deltas along the polyline,
which is the honest meaning of "ascent still to climb" for a *planned* route.
"""

from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

__all__ = [
    "NearestRoutePoint",
    "RouteAssetError",
    "RouteGeometry",
    "TrackPoint",
    "load_route_geometry",
]


class RouteAssetError(RuntimeError):
    """A route asset could not be read, parsed or resolved.

    Raised instead of returning an empty geometry: a route that cannot be parsed
    is missing data (spec 10.3), and the honest answer to "where am I on this
    route?" is a named failure, never a fabricated zero-length route.
    """


@dataclass(frozen=True)
class TrackPoint:
    """One GPX trackpoint: WGS-84 degrees plus an optional elevation in metres."""

    lat: float
    lon: float
    elevation_m: float | None = None


@dataclass(frozen=True)
class NearestRoutePoint:
    """Where a GPS fix snaps onto the planned polyline.

    ``along_m`` is the distance from the route start to that snapped point,
    ``off_route_m`` the geodesic distance from the fix to it, ``progress`` the
    fraction of the route it represents. ``progress`` is ``None`` only for a
    degenerate route whose total length is zero, where no fraction is defined.
    """

    lat: float
    lon: float
    elevation_m: float | None
    along_m: float
    off_route_m: float
    segment_index: int
    progress: float | None


@lru_cache(maxsize=1)
def _route_libraries() -> tuple[object, object, object]:
    """Import the ``route`` extra once, with an actionable error when missing."""
    try:
        import gpxpy  # type: ignore[import-not-found]
        import pyproj  # type: ignore[import-not-found]
        import shapely  # type: ignore[import-not-found]
    except ImportError as exc:  # pragma: no cover - exercised only without the extra
        raise RouteAssetError(
            "route geometry needs the 'route' extra (gpxpy, shapely, pyproj): "
            "install it with `pip install -e '.[route]'`"
        ) from exc
    return gpxpy, shapely, pyproj


@dataclass(frozen=True)
class RouteGeometry:
    """A parsed polyline plus the measurements that can be read off it.

    Immutable and cacheable: every field is derived once from the GPX, so the same
    route file yields the same measurements in every run (spec 19) and a horizon
    loop does not re-parse the file four times.
    """

    route_id: str
    source_ref: str
    points: tuple[TrackPoint, ...]
    cumulative_m: tuple[float, ...]
    total_distance_m: float
    total_ascent_m: float

    # ------------------------------------------------------------------ basics

    @property
    def point_count(self) -> int:
        """How many trackpoints the polyline holds."""
        return len(self.points)

    @property
    def has_elevation(self) -> bool:
        """Whether every trackpoint carries an elevation."""
        return bool(self.points) and all(point.elevation_m is not None for point in self.points)

    def _segment_lengths(self) -> tuple[float, ...]:
        """Geodesic length of each segment, in metres."""
        return _segment_geodesic_lengths(self.points)

    # ------------------------------------------------------------- along-route

    def _segment_index(self, along_m: float) -> int:
        """Index of the segment containing ``along_m`` (clamped to the polyline)."""
        cumulative = self.cumulative_m
        if len(cumulative) < 2:
            return 0
        if along_m <= 0.0:
            return 0
        if along_m >= self.total_distance_m:
            return len(self.points) - 2
        for index in range(len(cumulative) - 1):
            if cumulative[index] <= along_m <= cumulative[index + 1]:
                return index
        return len(self.points) - 2  # pragma: no cover - defensive

    def position_at(self, along_m: float) -> TrackPoint:
        """The point of the polyline ``along_m`` metres from the start.

        Between two trackpoints the position is the exact geodesic point at the
        remaining distance along the geodesic joining them (``Geod.fwd``), not a
        straight line in degree space; elevation is interpolated linearly between
        the two endpoint elevations, which is the only thing a polyline can claim.
        """
        clamped = min(max(float(along_m), 0.0), self.total_distance_m)
        if len(self.points) == 1:
            return self.points[0]
        index = self._segment_index(clamped)
        start, end = self.points[index], self.points[index + 1]
        segment_length = self.cumulative_m[index + 1] - self.cumulative_m[index]
        if segment_length <= 0.0:
            return TrackPoint(start.lat, start.lon, start.elevation_m)

        fraction = (clamped - self.cumulative_m[index]) / segment_length
        _, _, pyproj = _route_libraries()
        geod = pyproj.Geod(ellps="WGS84")
        azimuth, _, _ = geod.inv(start.lon, start.lat, end.lon, end.lat)
        longitude, latitude, _ = geod.fwd(
            start.lon,
            start.lat,
            azimuth,
            segment_length * fraction,
        )
        elevation = _interpolated_elevation(start, end, fraction)
        return TrackPoint(float(latitude), float(longitude), elevation)

    def elevation_at(self, along_m: float) -> float | None:
        """Polyline elevation ``along_m`` metres along, or ``None`` if absent."""
        if not self.has_elevation:
            return None
        return self.position_at(along_m).elevation_m

    def nearest(self, lat: float, lon: float) -> NearestRoutePoint:
        """Snap ``(lat, lon)`` onto the polyline and measure the snap.

        The nearest point is found in a local azimuthal-equidistant projection
        centred on the fix (see the module docstring for why a projection is
        allowed to solve *this* sub-problem and not to report metres); the
        returned ``off_route_m`` and ``along_m`` are geodesic.
        """
        _, shapely, pyproj = _route_libraries()
        geod = pyproj.Geod(ellps="WGS84")

        if len(self.points) == 1:
            only = self.points[0]
            _, _, off_route = geod.inv(lon, lat, only.lon, only.lat)
            return NearestRoutePoint(
                lat=only.lat,
                lon=only.lon,
                elevation_m=only.elevation_m,
                along_m=0.0,
                off_route_m=max(float(off_route), 0.0),
                segment_index=0,
                progress=None if self.total_distance_m <= 0 else 0.0,
            )

        projection = pyproj.CRS.from_proj4(
            f"+proj=aeqd +lat_0={lat} +lon_0={lon} +datum=WGS84 +units=m +type=crs"
        )
        to_planar = pyproj.Transformer.from_crs("EPSG:4326", projection, always_xy=True)
        to_geographic = pyproj.Transformer.from_crs(projection, "EPSG:4326", always_xy=True)

        xs, ys = to_planar.transform([point.lon for point in self.points], [point.lat for point in self.points])
        line = shapely.geometry.LineString(list(zip(xs, ys)))
        fix_x, fix_y = to_planar.transform(lon, lat)
        planar_along = float(line.project(shapely.geometry.Point(fix_x, fix_y)))
        snapped = line.interpolate(planar_along)
        snap_lon, snap_lat = to_geographic.transform(snapped.x, snapped.y)

        # pyproj.Geod.inv takes (lon, lat) pairs -- longitudes first. Swapping the
        # pair here would silently return a distance of millions of metres, so the
        # call is deliberately spelled out rather than passed as a tuple.
        _, _, off_route = geod.inv(lon, lat, float(snap_lon), float(snap_lat))

        planar_cumulative = _planar_cumulative(list(xs), list(ys))
        segment_index = _index_for_planar_along(planar_cumulative, planar_along)
        fraction = _fraction_within(
            planar_along,
            planar_cumulative[segment_index],
            planar_cumulative[segment_index + 1],
        )
        segment_length = self.cumulative_m[segment_index + 1] - self.cumulative_m[segment_index]
        along_m = self.cumulative_m[segment_index] + fraction * segment_length

        elevation = _interpolated_elevation(
            self.points[segment_index],
            self.points[segment_index + 1],
            fraction,
        )
        progress = None if self.total_distance_m <= 0 else min(along_m / self.total_distance_m, 1.0)
        return NearestRoutePoint(
            lat=float(snap_lat),
            lon=float(snap_lon),
            elevation_m=elevation,
            along_m=float(along_m),
            off_route_m=max(float(off_route), 0.0),
            segment_index=segment_index,
            progress=progress,
        )

    # ----------------------------------------------------------------- ascent

    def remaining_ascent(self, along_m: float) -> float:
        """Positive elevation still to be gained from ``along_m`` to the end."""
        if not self.has_elevation or len(self.points) < 2:
            return 0.0
        clamped = min(max(float(along_m), 0.0), self.total_distance_m)
        start_index = self._segment_index(clamped)
        ascent = 0.0
        for index in range(start_index, len(self.points) - 1):
            start, end = self.points[index], self.points[index + 1]
            from_elevation = start.elevation_m
            if index == start_index:
                from_elevation = self.elevation_at(clamped)
            if from_elevation is None or end.elevation_m is None:
                continue
            if end.elevation_m > from_elevation:
                ascent += end.elevation_m - from_elevation
        return ascent

    # ------------------------------------------------------------ quality score

    def geometry_quality(self, *, min_trackpoints: int, trackpoint_weight: float, elevation_weight: float) -> float:
        """A transparent geometry-derived route data quality in [0, 1].

        Two measurable properties of a polyline are combined: how finely it is
        sampled (a 12-point route is a plan, a 200-point route is a recorded
        track) and how much of it carries elevation. Both weights are
        demo-calibration parameters from ``calibration_v1.yaml`` -- there is no
        published "route data quality" scale to honour, and this score is a
        disclosure aid for the confidence channel, not a claim of survey accuracy.
        """
        if not self.points or min_trackpoints <= 0:
            return 0.0
        density = min(1.0, len(self.points) / min_trackpoints)
        covered = sum(1 for point in self.points if point.elevation_m is not None) / len(self.points)
        score = trackpoint_weight * density + elevation_weight * covered
        return _clamp01(score)


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #


def _clamp01(value: float) -> float:
    """Clamp to the [0, 1] interval every quality/confidence field is bounded by."""
    return min(max(float(value), 0.0), 1.0)


def _segment_geodesic_lengths(points: tuple[TrackPoint, ...] | list[TrackPoint]) -> tuple[float, ...]:
    """Geodesic length of every segment of the polyline, in metres."""
    if len(points) < 2:
        return ()
    _, _, pyproj = _route_libraries()
    geod = pyproj.Geod(ellps="WGS84")
    lengths: list[float] = []
    for start, end in zip(points, points[1:]):
        _, _, distance = geod.inv(start.lon, start.lat, end.lon, end.lat)
        lengths.append(max(float(distance), 0.0))
    return tuple(lengths)


def _interpolated_elevation(start: TrackPoint, end: TrackPoint, fraction: float) -> float | None:
    """Linear elevation between two trackpoints, or ``None`` if either is missing."""
    if start.elevation_m is None or end.elevation_m is None:
        return None
    return start.elevation_m + (end.elevation_m - start.elevation_m) * fraction


def _planar_cumulative(xs: list[float], ys: list[float]) -> list[float]:
    """Planar cumulative distances of the projected polyline (metres)."""
    cumulative = [0.0]
    for (x0, y0), (x1, y1) in zip(zip(xs, ys), zip(xs[1:], ys[1:])):
        cumulative.append(cumulative[-1] + ((x1 - x0) ** 2 + (y1 - y0) ** 2) ** 0.5)
    return cumulative


def _index_for_planar_along(cumulative: list[float], along: float) -> int:
    """Index of the planar segment containing ``along`` (clamped)."""
    if along <= 0.0:
        return 0
    if along >= cumulative[-1]:
        return len(cumulative) - 2
    for index in range(len(cumulative) - 1):
        if cumulative[index] <= along <= cumulative[index + 1]:
            return index
    return len(cumulative) - 2  # pragma: no cover - defensive


def _fraction_within(along: float, start: float, end: float) -> float:
    """Fraction of a segment that ``along`` covers, safe on zero-length segments."""
    if end <= start:
        return 0.0
    return min(max((along - start) / (end - start), 0.0), 1.0)


def _parse_gpx_points(text: str) -> list[TrackPoint]:
    """Extract the polyline of a GPX document.

    Track segments come first (a recorded or planned track), then ``<rte>``
    routes, then waypoints: a route asset that only carries waypoints still has a
    geometry, and refusing it would be worse than stating the preference here.
    Elevations are read when present and left ``None`` when not, so a missing
    elevation is disclosed as missing rather than filled with zero.
    """
    gpxpy, _, _ = _route_libraries()
    parsed = gpxpy.parse(text)

    coordinates: list[tuple[float, float, float | None]] = []
    for track in parsed.tracks:
        for segment in track.segments:
            for point in segment.points:
                coordinates.append((point.latitude, point.longitude, point.elevation))
    if not coordinates:
        for route in parsed.routes:
            for point in route.points:
                coordinates.append((point.latitude, point.longitude, point.elevation))
    if not coordinates:
        for point in parsed.waypoints:
            coordinates.append((point.latitude, point.longitude, point.elevation))

    if len(coordinates) < 1:
        raise RouteAssetError("GPX asset holds no trackpoints, route points or waypoints")
    return [TrackPoint(lat, lon, elevation) for lat, lon, elevation in coordinates]


@lru_cache(maxsize=32)
def _geometry_from_path(path_key: str, route_id: str) -> RouteGeometry:
    """Parse (and remember) the GPX at ``path_key``.

    Cached by resolved path: a horizon loop asks the same geometry four times and
    the parse is deterministic, so caching changes no result -- it only removes
    the temptation to parse once and hand the object around.
    """
    path = Path(path_key)
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise RouteAssetError(f"route asset {path} could not be read: {exc}") from exc

    try:
        points = _parse_gpx_points(text)
    except RouteAssetError:
        raise
    except Exception as exc:  # gpxpy raises its own parser errors
        raise RouteAssetError(f"route asset {path} is not parseable GPX: {exc}") from exc

    if len(points) == 1:
        raise RouteAssetError(f"route asset {path} holds a single point; a route needs at least two")

    segment_lengths = _segment_geodesic_lengths(points)
    cumulative = [0.0]
    for length in segment_lengths:
        cumulative.append(cumulative[-1] + length)

    ascent = 0.0
    for start, end in zip(points, points[1:]):
        if start.elevation_m is None or end.elevation_m is None:
            continue
        if end.elevation_m > start.elevation_m:
            ascent += end.elevation_m - start.elevation_m

    return RouteGeometry(
        route_id=route_id,
        source_ref=str(path),
        points=tuple(points),
        cumulative_m=tuple(cumulative),
        total_distance_m=cumulative[-1],
        total_ascent_m=ascent,
    )


def load_route_geometry(path: str | Path, *, route_id: str | None = None) -> RouteGeometry:
    """Load the GPX at ``path`` into a :class:`RouteGeometry`.

    Args:
        path: Filesystem path of the GPX asset.
        route_id: Identity to record on the geometry. Defaults to the file stem,
            so a geometry loaded by path still answers "which route is this?".

    Raises:
        RouteAssetError: the file is unreadable, unparseable, or degenerate (a
            single point cannot form a route).
    """
    resolved = Path(path).expanduser().resolve()
    return _geometry_from_path(str(resolved), route_id or resolved.stem)
