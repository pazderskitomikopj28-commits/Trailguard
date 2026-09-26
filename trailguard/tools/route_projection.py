"""``project_route_state`` tool -- one conditional projection (spec 5.2, 6).

The tool answers exactly one question for exactly one horizon: *if the hiker keeps
moving at the currently observable trend along the current route, where would they
be, and when?* It never answers it for more than one horizon in a call, and it
never claims to know what happens after the horizon it was given (spec 4.2, 6.2):
the 30/60/120 loop belongs to the Skill, which calls this once per horizon.

The arithmetic is deliberately the simplest honest one (spec 6.3, 6.4):

* the frozen movement model weights the three observed speeds into a base speed
  and applies the fatigue penalty once, so a projection and an ETA can never
  disagree about how fast the hiker walks;
* the hiker is walked that many metres along the *planned polyline* from where
  they are now, geodesically, and the walk is clamped at the route's end -- a
  projection beyond the last trackpoint is "at the end", not "off the map";
* ``projected_arrival_time`` is the instant the projection *refers to*, not a
  predicted arrival: the analysis instant plus the elapsed time, truncated at the
  horizon the caller asked for. A hiker whose ETA is 400 minutes still has a
  +30-minute projection thirty minutes from now, and that is the instant its
  weather and its safety margins must be evaluated at.

No clock is read (spec 19): every instant comes from the caller.
"""

from __future__ import annotations

from datetime import datetime, timedelta

from ..risk_core.calibration import Calibration, load_calibration
from ..risk_core.movement import MovementModel, estimate_minutes
from ..routes.geometry import RouteAssetError, RouteGeometry, TrackPoint
from ..routes.registry import resolve_route_geometry
from ..schemas.context import GeoPoint, RouteContext
from ..schemas.route import Horizon, MovementTrend, RouteProjection

from ._arguments import binds_input_contract
from .contracts import ProjectRouteStateInput

__all__ = ["project_route_state"]

#: The frozen horizon set, keyed by its own offset in minutes.
_HORIZON_BY_OFFSET: dict[int, Horizon] = {horizon.minutes: horizon for horizon in Horizon.all_horizons()}


@binds_input_contract(ProjectRouteStateInput)
def project_route_state(
    route_context: RouteContext,
    movement: MovementTrend,
    horizon_min: int,
    *,
    analysis_time: datetime | None = None,
    fatigue_penalty: float | None = None,
    geometry: RouteGeometry | None = None,
    movement_model: MovementModel | None = None,
    calibration: Calibration | None = None,
) -> RouteProjection:
    """Project the hiker's route state ``horizon_min`` minutes ahead (spec 5.2, 6).

    Args:
        route_context: The current route position facts. The projection continues
            *this* state: its remaining distance and its route name supply both
            where the walk starts and which polyline it follows, so the tool never
            re-derives where the hiker is and a rounding difference cannot move
            them.
        movement: The speed trend the frozen model weights (spec 6.3).
        horizon_min: Which horizon to project to. One of the frozen offsets
            0/30/60/120; the tool refuses any other value rather than pretending a
            fifth horizon exists (spec 6.1, 4.2).
        analysis_time: Instant the projection is anchored to, and the origin of
            ``projected_arrival_time``. Passed in rather than read from the system
            clock so a replay reproduces byte-for-byte (spec 19). It is optional
            only because the frozen three-field tool contract of spec 5.2 names
            ``route_context``, ``movement`` and ``horizon_min`` and nothing else;
            the anchor is therefore supplied by the caller that owns a clock (the
            Skill, which holds ``time_state.analysis_time``), and its absence is a
            loud error rather than a silent read of the wall clock.
        fatigue_penalty: Fatigue index in [0, 1] to apply, when the caller knows
            it. ``None`` projects an *unfatigued* trend, which is a stronger
            assumption than applying one, and it is charged against the projection
            confidence rather than hidden.
        geometry: Pre-parsed route geometry, for a caller that already holds it.
        movement_model: Frozen movement model. Defaults to the calibration's.
        calibration: Demo-calibration parameters. Defaults to
            ``calibration_v1.yaml``.

    Returns:
        A :class:`~trailguard.schemas.route.RouteProjection` for exactly this
        horizon: progress, position, the instant it refers to, the distance still
        remaining and a trust score.

    Raises:
        ValueError: ``horizon_min`` is not one of the frozen offsets, or
            ``analysis_time`` was not supplied -- a projection has no arrival
            instant to report and the tool will not invent one from the clock.
        RouteAssetError: the route asset behind ``route_context`` cannot be
            resolved, so the projection has no polyline to walk along.
    """
    offset = int(horizon_min)
    if offset not in _HORIZON_BY_OFFSET:
        raise ValueError(
            f"horizon_min={horizon_min} is not one of the frozen horizons "
            f"{sorted(_HORIZON_BY_OFFSET)}: one call projects one horizon (spec 4.2, 6.1)"
        )
    if analysis_time is None:
        raise ValueError(
            "project_route_state needs an analysis_time to anchor projected_arrival_time; "
            "the frozen tool contract of spec 5.2 does not carry one, so the caller that owns "
            "the clock (the Skill's time_state.analysis_time) must pass it. The system clock is "
            "never read instead (spec 19)."
        )

    resolved_calibration = calibration if calibration is not None else load_calibration()
    resolved_model = (
        movement_model
        if movement_model is not None
        else MovementModel(
            base_speed_weights=resolved_calibration.base_speed_weights,
            fatigue_penalty_cap=resolved_calibration.fatigue_penalty_cap,
        )
    )
    route = geometry if geometry is not None else resolve_route_geometry(route_context.route_id)

    base_speed = resolved_model.base_speed_kmh(
        current_speed_kmh=movement.current_speed_kmh,
        avg_speed_10m=movement.avg_speed_10m,
        avg_speed_30m=movement.avg_speed_30m,
    )
    effective_speed = resolved_model.effective_speed_kmh(base_speed, fatigue_penalty)

    remaining_m = max(float(route_context.remaining_distance_km), 0.0) * 1000.0
    along_m = max(route.total_distance_m - remaining_m, 0.0)

    # How far the hiker can actually get: the horizon's worth of walking, capped
    # by what is left of the route. A projection that overshoots the end stops at
    # the end rather than leaving the polyline and reporting a position nobody
    # planned.
    horizon_distance_m = (offset / 60.0) * effective_speed * 1000.0
    walked_m = max(min(horizon_distance_m, remaining_m), 0.0)
    projected_point: TrackPoint = route.position_at(along_m + walked_m)
    projected_along_m = along_m + walked_m

    eta_minutes = estimate_minutes(remaining_m / 1000.0, effective_speed)
    travel_minutes = float(min(offset, eta_minutes)) if eta_minutes is not None else float(offset)
    arrival_time = analysis_time + timedelta(minutes=travel_minutes)

    return RouteProjection(
        horizon=_HORIZON_BY_OFFSET[offset],
        horizon_min=offset,
        projected_route_progress=(
            projected_along_m / route.total_distance_m if route.total_distance_m > 0 else 0.0
        ),
        projected_position=GeoPoint(lat=projected_point.lat, lon=projected_point.lon, alt_m=projected_point.elevation_m),
        projected_arrival_time=arrival_time,
        projected_remaining_distance_km=max(remaining_m - walked_m, 0.0) / 1000.0,
        projection_confidence=_projection_confidence(
            route_context=route_context,
            calibration=resolved_calibration,
            horizon_min=offset,
            fatigue_penalty=fatigue_penalty,
        ),
    )


def _projection_confidence(
    *,
    route_context: RouteContext,
    calibration: Calibration,
    horizon_min: int,
    fatigue_penalty: float | None,
) -> float:
    """Trust in this projection, in [0, 1], from three disclosed components.

    ``route data quality`` x ``horizon reliability`` x ``fatigue assumption``: a
    projection along a well-known route at NOW with a real fatigue index is
    trusted most, and a missing component is charged rather than ignored. The
    numbers are demo calibration (``competition_demo_calibration``), not a
    published projection-accuracy model, and the result is not a likelihood of
    anything -- only a statement about the inputs behind it.
    """
    route_term = float(route_context.data_quality)
    horizon_term = float(calibration.horizon_reliability_for(horizon_min))
    fatigue_term = 1.0 if fatigue_penalty is not None else 0.5
    return max(0.0, min(route_term * horizon_term * fatigue_term, 1.0))
