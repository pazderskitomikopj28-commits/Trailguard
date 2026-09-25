"""``get_route_context`` tool -- route position facts (spec 5.1).

Phase 0 scope: contract only. The function body is a stub that raises
``NotImplementedError`` naming the phase that owns the implementation.
"""

from datetime import datetime

from ..schemas.context import GeoPoint, RouteContext


def get_route_context(route_id: str, current_position: GeoPoint, timestamp: datetime) -> RouteContext:
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

    Inputs:
      * ``route_id`` -- identifier of the planned route (GPX asset reference).
      * ``current_position`` -- :class:`~trailguard.schemas.context.GeoPoint`
        GPS fix the position facts are computed against.
      * ``timestamp`` -- ISO8601 instant the fix refers to.

    Outputs:
      A :class:`~trailguard.schemas.context.RouteContext` carrying
      ``route_progress``, ``off_route_distance_m``, ``remaining_distance_km``,
      ``remaining_ascent_m``, ``current_altitude_m``, ``route_polyline_ref``
      and ``data_quality``.

    Phase 0 stub: the deterministic implementation (GPX math) belongs to
    Phase 1 (deterministic core) and therefore raises ``NotImplementedError``.
    """
    raise NotImplementedError("Phase 1: deterministic route context (GPX parsing, progress, off-route distance)")
