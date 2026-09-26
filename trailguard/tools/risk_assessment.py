"""``assess_risk`` tool -- deterministic risk verdict for one horizon (spec 5.5, Phase 1).

The tool is a thin, honest seam: it builds (or accepts) the deterministic engine
and calls it once for one horizon. All the logic lives in
:mod:`trailguard.risk_core.engine`, and the engine reads no clock, calls no model
and touches no network -- so this function runs with ``STEPFUN_API_KEY`` unset and
offline, which is what the Phase 1 tests assert.
"""

from __future__ import annotations

from ..risk_core.calibration import Calibration, load_calibration
from ..risk_core.engine import DeterministicRiskEngine
from ..risk_core.hard_rules import RulePack, load_rule_pack
from ..risk_core.movement import MovementModel
from ..schemas.context import DataQualityReport, DeviceState, RouteContext, TimeState
from ..schemas.risk import RiskResult
from ..schemas.route import RouteProjection
from ..schemas.weather import WeatherForRouteTime

from ._arguments import binds_input_contract
from .contracts import AssessRiskInput

__all__ = ["assess_risk"]

_ENGINE_CACHE: dict[tuple[str, str, str, str], DeterministicRiskEngine] = {}


def _engine(
    pack: RulePack | None,
    calibration: Calibration | None,
    movement: MovementModel | None,
) -> DeterministicRiskEngine:
    """The engine for this call, cached on the inputs that define it.

    Cached because an engine is immutable once built (a pack, a calibration and a
    movement model are all validated data) and a horizon loop builds one engine
    and assesses four horizons with it. The cache key is the identity of all
    three, and a caller that passes none gets the same engine every time -- which
    is what makes four horizons in one run comparable.
    """
    resolved_pack = pack if pack is not None else load_rule_pack()
    resolved_calibration = calibration if calibration is not None else load_calibration()
    resolved_movement = (
        movement
        if movement is not None
        else MovementModel(
            base_speed_weights=resolved_calibration.base_speed_weights,
            fatigue_penalty_cap=resolved_calibration.fatigue_penalty_cap,
        )
    )
    key = (
        f"{resolved_pack.pack_id}@{resolved_pack.version}",
        f"{resolved_calibration.pack_id}@{resolved_calibration.version}",
        tuple(sorted((name, weight) for name, weight in resolved_movement.base_speed_weights.items())),
        f"{resolved_movement.fatigue_penalty_cap}",
    )
    if key not in _ENGINE_CACHE:
        _ENGINE_CACHE[key] = DeterministicRiskEngine(resolved_pack, resolved_calibration, resolved_movement)
    return _ENGINE_CACHE[key]


@binds_input_contract(AssessRiskInput)
def assess_risk(
    route_state: RouteContext,
    weather_state: WeatherForRouteTime | None,
    user_device_state: DeviceState,
    time_state: TimeState,
    projected_state: RouteProjection | None = None,
    *,
    horizon_min: int | None = None,
    weather_freshness_min: float | None = None,
    data_quality: DataQualityReport | float | None = None,
    engine: DeterministicRiskEngine | None = None,
    pack: RulePack | None = None,
    calibration: Calibration | None = None,
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

    Args:
        route_state: Route context for the horizon under assessment (projected
            when the horizon is future).
        weather_state: Weather for the same horizon, with provenance and
            freshness. ``None`` is accepted: every weather feature is then not
            computable and the future-weather rule is disclosed as not evaluated
            rather than being silently skipped.
        user_device_state: Device snapshot for the same horizon.
        time_state: Analysis clock and sunset instant.
        projected_state: The horizon's projection, or ``None`` for a horizon that
            was not projected (the honest state for NOW).
        horizon_min: Offset this verdict refers to. Defaults to the projection's
            own horizon, or 0 when there is no projection.
        weather_freshness_min: Age of the weather snapshot, when the caller
            computed it. Defaults to ``weather_state.freshness_minutes``.
        data_quality: Caller-disclosed data-quality report (or a bare overall
            score), which fills the confidence's reported-overall term and charges
            the disclosed missing/stale fields.
        engine: A pre-built engine, for a caller that already holds one.
        pack: Rule pack to evaluate. Defaults to ``rules_v1.yaml``.
        calibration: Demo-calibration parameters. Defaults to
            ``calibration_v1.yaml``.

    Returns:
        A :class:`~trailguard.schemas.risk.RiskResult` -- level, score, drivers,
        triggered hard rules, confidence, evidence references and safety margins
        for that single horizon.

    Raises:
        ValueError: a clause names a feature the extractor does not produce, or a
            clause names a threshold the pack does not define.
    """
    return (
        engine if engine is not None else _engine(pack, calibration, None)
    ).assess_risk(
        route_state,
        weather_state,
        user_device_state,
        time_state,
        projected_state,
        horizon_min=horizon_min,
        weather_freshness_min=weather_freshness_min,
        data_quality=data_quality,
    )
