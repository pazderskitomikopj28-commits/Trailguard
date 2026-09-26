"""TrailGuard route geometry layer (GPX parsing, geodesic measurement, projection).

Deterministic, offline and dependency-light: everything here is arithmetic over a
fixed GPX file, so the same route and the same GPS fix produce the same
measurements in every run (spec 19). See :mod:`trailguard.routes.geometry` for the
module docstring explaining why reported metres are geodesic while the nearest
point is solved in a local projection.
"""

from .geometry import (
    NearestRoutePoint,
    RouteAssetError,
    RouteGeometry,
    TrackPoint,
    load_route_geometry,
)
from .registry import REPO_ROOT, ROUTE_SEARCH_PATHS, known_route_ids, resolve_route_geometry

__all__ = [
    "REPO_ROOT",
    "ROUTE_SEARCH_PATHS",
    "NearestRoutePoint",
    "RouteAssetError",
    "RouteGeometry",
    "TrackPoint",
    "known_route_ids",
    "load_route_geometry",
    "resolve_route_geometry",
]
