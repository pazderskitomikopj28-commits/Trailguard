"""``get_route_context`` tool -- route position facts (spec 5.1, Phase 1).

The tool is the seam between "a GPS fix and a route name" and "measurements".
It resolves the route name to a GPX asset through
:mod:`trailguard.routes.registry`, snaps the fix onto the planned polyline with
:mod:`trailguard.routes.geometry`, and reports what it measured. It evaluates no
rule, judges no hazard and projects nothing: those belong to the safety core and
to ``project_route_state``.

Determinism: every number below is a function of (route asset, fix, route data
quality calibration). No clock is read, no network is touched, and the same
inputs give byte-identical output in every run (spec 19).
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

from ..risk_core.calibration import Calibration, load_calibration
from ..routes.geometry import NearestRoutePoint, RouteAssetError, RouteGeometry
from ..routes.registry import REPO_ROOT, resolve_route_geometry
from ..schemas.context import GeoPoint, RouteContext

from ._arguments import binds_input_contract
from .contracts import GetRouteContextInput

__all__ = ["get_route_context"]


@binds_input_contract(GetRouteContextInput)
def get_route_context(
    route_id: str,
    current_position: GeoPoint,
    timestamp: datetime,
    *,
    geometry: RouteGeometry | None = None,
    calibration: Calibration | None = None,
) -> RouteContext:
    """Resolve where the hiker is on a known route (spec 5.1).

    Responsibilities (spec 5.1):
      * GPX / polyline parsing for ``route_id``;
      * route progress and the nearest point on the planned polyline;
      * remaining distance and remaining ascent along the planned route;
      * off-route distance in meters from the planned polyline;
      * a route data-quality score in [0, 1].

    NOT responsible for (spec 5.1):
      * risk levels, safety verdicts or any rule evaluation;
      * weather, freshness or confidence decisions;
      * future projection -- predicting where the hiker *will* be is
        ``project_route_state``'s job, not this tool's.

    Args:
        route_id: Identifier of the planned route: a logical name
            (``scn_route_sample``) or a repo-root-relative GPX reference.
        current_position: The GPS fix the position facts are computed against.
            Its ``alt_m`` is *not* used as the reported altitude: the route's own
            elevation is preferred (spec 5.1), so a fix that lost altitude does
            not produce a zero, and a fix with a better altitude does not
            silently rewrite the planned line.
        timestamp: ISO8601 instant the fix refers to. Recorded for provenance
            only -- the measurements do not depend on it.
        geometry: Pre-parsed route geometry, for a caller that already holds it.
        calibration: Route-quality parameters. Defaults to
            ``calibration_v1.yaml``.

    Returns:
        A :class:`~trailguard.schemas.context.RouteContext` carrying
        ``route_progress``, ``off_route_distance_m``, ``remaining_distance_km``,
        ``remaining_ascent_m``, ``current_altitude_m``, ``route_polyline_ref``
        and ``data_quality``.

    Raises:
        RouteAssetError: ``route_id`` names no GPX asset, or the asset cannot be
            parsed or is degenerate. A route that cannot be read is missing data
            (spec 10.3), and the honest answer is a named failure -- never a
            fabricated zero-length route.
    """
    route_geometry = geometry if geometry is not None else resolve_route_geometry(route_id)
    parameters = (calibration if calibration is not None else load_calibration()).route_geometry_quality_params()

    nearest: NearestRoutePoint = route_geometry.nearest(current_position.lat, current_position.lon)
    along_m = nearest.along_m
    remaining_distance_km = max(route_geometry.total_distance_m - along_m, 0.0) / 1000.0
    elevation = nearest.elevation_m
    if elevation is None:
        # The frozen contract requires a float, and the GPX is the only honest
        # source of altitude the tool has: a polyline without elevation reports
        # 0.0 rather than an invented number, and the route's own quality score
        # is the disclosure that its elevation is absent.
        elevation = 0.0

    return RouteContext(
        route_id=route_geometry.route_id or route_id,
        route_progress=nearest.progress if nearest.progress is not None else 0.0,
        off_route_distance_m=nearest.off_route_m,
        remaining_distance_km=remaining_distance_km,
        remaining_ascent_m=route_geometry.remaining_ascent(along_m),
        current_altitude_m=elevation,
        route_polyline_ref=_asset_reference(route_geometry),
        data_quality=route_geometry.geometry_quality(**parameters),
    )


def _asset_reference(route_geometry: RouteGeometry) -> str:
    """Report the polyline reference relative to the repository root when it is inside it.

    An absolute path is machine-specific, and an evidence trace that records one
    cannot be compared between two checkouts of the same benchmark run. Every
    in-repo asset therefore reports the same reference on every machine
    (``replay/routes/scn_route_sample.gpx``); only an asset genuinely outside the
    repository keeps its absolute location, because a relative one would not
    resolve.
    """
    source = Path(route_geometry.source_ref)
    try:
        return source.relative_to(REPO_ROOT).as_posix()
    except ValueError:
        return str(source)
