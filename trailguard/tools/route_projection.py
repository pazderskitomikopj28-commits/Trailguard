"""``project_route_state`` tool -- conditional future route state (spec 5.2, 6).

Phase 0 scope: contract only. The function body is a stub that raises
``NotImplementedError`` naming the phase that owns the implementation.
"""

from ..schemas.context import RouteContext
from ..schemas.route import MovementTrend, RouteProjection


def project_route_state(route_context: RouteContext, movement: MovementTrend, horizon_min: int) -> RouteProjection:
    """Project the hiker's future route state at one horizon (spec 5.2, 6.2).

    Responsibilities (spec 5.2):
      * predict a future route position and arrival instant from the current
        movement trend, conditional on "the user keeps moving at the currently
        observable trend along the current route" (spec 6.2);
      * project route progress and remaining distance at that horizon;
      * return a ``projection_confidence`` derived from the input data quality.

    NOT responsible for (spec 5.2, 6.2):
      * querying weather (that is ``get_weather_for_route_time``);
      * risk judgement -- a projection is a state, never a risk level and never
        an accident probability; the domain concept is a *Projected Risk State*;
      * the 30/60/120 loop, which belongs to the Skill (spec 4.2).

    Frozen Phase 1 movement model (spec 6.3) -- documented here so the contract
    is stable even though Phase 0 implements nothing::

        base_speed      = 0.5 * avg_speed_10m + 0.3 * avg_speed_30m + 0.2 * current_speed
        effective_speed = base_speed * (1 - fatigue_penalty)
        distance        = effective_speed * horizon

    The weights (0.5/0.3/0.2) and the fatigue penalty range are configurable
    **demo-calibration values**, not an industry standard, and must be disclosed
    as such wherever they are reported (spec 6.3). No gradient-aware pace,
    Naismith adjustment or ML ETA is used in the MVP.
    Note on the frozen signature: :class:`~trailguard.schemas.route.MovementTrend`
    deliberately carries no device state, so the fatigue penalty term of the
    model above is supplied by the deterministic core rather than by this
    contract, and the penalty actually applied must be reported rather than
    silently folded into the numbers.

    Inputs:
      * ``route_context`` -- current :class:`~trailguard.schemas.context.RouteContext`.
      * ``movement`` -- :class:`~trailguard.schemas.route.MovementTrend` speed trend.
      * ``horizon_min`` -- horizon offset in minutes (0/30/60/120).

    Outputs:
      A :class:`~trailguard.schemas.route.RouteProjection` for the requested
      horizon: projected progress, projected position, projected arrival time,
      projected remaining distance and projection confidence.

    Phase 0 stub: the projection math belongs to Phase 1 (deterministic core)
    and therefore raises ``NotImplementedError``.
    """
    raise NotImplementedError("Phase 1: deterministic route projection (frozen movement model, spec 6.3)")
