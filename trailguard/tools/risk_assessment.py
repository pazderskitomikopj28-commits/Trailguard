"""``assess_risk`` tool -- deterministic risk verdict for one horizon (spec 5.5).

Phase 0 scope: contract only. The function body is a stub that raises
``NotImplementedError`` naming the phase that owns the implementation.
"""

from ..schemas.context import DeviceState, RouteContext, TimeState
from ..schemas.risk import RiskResult
from ..schemas.route import RouteProjection
from ..schemas.weather import WeatherForRouteTime


def assess_risk(
    route_state: RouteContext,
    weather_state: WeatherForRouteTime,
    user_device_state: DeviceState,
    time_state: TimeState,
    projected_state: RouteProjection | None = None,
) -> RiskResult:
    """Run the deterministic safety core for ONE horizon (spec 5.5).

    Responsibilities (spec 5.5, 7, 8):
      * execute the rule pack (hard rules set a severity floor, soft rules
        contribute score, R-F escalates on compound soft signals);
      * aggregate a soft score and map it to a :class:`RiskLevel`;
      * report the :class:`~trailguard.schemas.risk.RuleDriver` list, the hard
        rules that fired, ``confidence`` and ``evidence_refs``;
      * report the explainable :class:`~trailguard.schemas.risk.SafetyMargins`
        block with physical units (spec 8) -- no dimensionless black-box margin.
        The projection-derived margins come from ``projected_state``: the time
        margin needs the horizon's projected arrival time and the projected
        battery and confidence need the projection the caller already computed.
        When ``projected_state`` is absent (the NOW horizon, or a caller that
        declined to project) those margins are reported as unknown -- the core
        must never invent an arrival time to fill the field.

    NOT responsible for (spec 5.5, 4.2):
      * projecting future positions -- callers must supply the route state of
        the horizon being assessed;
      * fetching future weather -- callers supply ``weather_state``;
      * running the 30/60/120 horizon loop or any multi-horizon aggregation;
        one call assesses exactly one horizon, and the loop belongs to the Skill;
      * outputting an accident probability, or overriding a triggered hard rule.

    Inputs:
      * ``route_state`` -- :class:`~trailguard.schemas.context.RouteContext`
        for the horizon under assessment (projected when the horizon is future).
      * ``weather_state`` -- :class:`~trailguard.schemas.weather.WeatherForRouteTime`
        for the same horizon, with provenance and freshness.
      * ``user_device_state`` -- :class:`~trailguard.schemas.context.DeviceState`.
      * ``time_state`` -- :class:`~trailguard.schemas.context.TimeState` with the
        analysis clock and sunset instant used for the time margin.
      * ``projected_state`` -- optional
        :class:`~trailguard.schemas.route.RouteProjection` for the same horizon,
        produced by ``project_route_state``. It carries the projected arrival
        instant, the projected remaining distance and ``projection_confidence``
        that the spec 8 safety margins report; ``None`` means "no projection was
        made", which is the honest state for NOW.

    Outputs:
      A :class:`~trailguard.schemas.risk.RiskResult` -- level, score, drivers,
      triggered hard rules, confidence, evidence references and safety margins
      for that single horizon.

    Phase 0 stub: rule execution and score aggregation belong to Phase 1
    (deterministic core) and therefore raise ``NotImplementedError``.
    """
    raise NotImplementedError("Phase 1: deterministic risk assessment (rules execution, soft score, level mapping)")
