"""Deterministic risk engine interface and pipeline order (spec sections 7, 8, 5.5).

The safety core is the reason TrailGuard does not let a language model feel
its way to a safety verdict: risk level, score, drivers and confidence come
from rules and arithmetic, and the LLM may only explain them (spec section 2,
"LLM 不能 ... 自行修改 Safety Core risk level / 覆盖 hard rule").

This module is Phase 0: it defines the engine *boundary* -- the abstract
:class:`RiskEngine` and how one is built from a :class:`RulePack`. Evaluating
the pack is Phase 1.

Pipeline order (fixed, and the order Phase 1 must implement):

1. **Feature extraction** -- flatten the caller-supplied inputs (route state,
   weather state, device state, time state, and the horizon's projection when
   the caller passed one) into the named features the rule pack refers to,
   plus the derived features the rules name (``time_margin_min``,
   ``pace_drop_percent``, ``projected_battery_percent``).
2. **Hard rules set a severity floor** -- each triggered hard rule (R-A route
   deviation, R-B sunset margin) raises the level floor; a level may never be
   lowered below the floor, which is what makes hard rules non-overridable.
3. **Soft score** -- triggered soft rules (R-C future weather, R-D pace
   degradation, R-E battery x remaining route) contribute to the aggregate
   soft score in [0, 100] (see ``risk_core/scoring.py``).
4. **Level mapping** -- the score maps onto the calibration bands.
5. **R-F compound escalation** -- two or more independent soft rules at
   severity >= MEDIUM at the same horizon escalate the verdict by exactly one
   level, as configured in the pack (never hardcoded in the prompt).
6. **Confidence** -- data quality, weather freshness and projection confidence
   combine into ``confidence`` (see ``risk_core/confidence.py``).

Step 6 is the one step of this pipeline that is *not* Phase 1 work: steps 1-5
are the deterministic core of Phase 1, while the confidence model is Phase 2
(spec section 25). The order above is the full, final pipeline, so a Phase 1
implementation must leave step 6 out rather than invent a confidence number, and
the phase label in the error message below names Phase 1 for exactly that
reason. ``_PHASE1_MESSAGE`` therefore stops at step 5.

Non-responsibilities of the engine, all of which belong to the skill or the
tools: fetching inputs, projecting future positions, fetching future weather,
running the 30/60/120 horizon loop, computing the final confidence number, and
writing the user-facing explanation.
"""

from abc import ABC, abstractmethod

from ..schemas.context import DeviceState, RouteContext, TimeState
from ..schemas.risk import RiskResult
from ..schemas.route import RouteProjection
from ..schemas.weather import WeatherForRouteTime
from .hard_rules import RulePack

#: Phase label used by every not-yet-implemented engine entry point, so the
#: failure names the phase that owes the work (Phase 0 writes no risk logic).
#: Stops at step 5 of the documented pipeline on purpose: confidence is Phase 2
#: (``risk_core/confidence.py``), so naming it here would promise Phase 1 work it
#: does not own.
_PHASE1_MESSAGE = (
    "Phase 1: deterministic safety core (feature extraction -> hard rules set severity "
    "floor -> soft score -> level mapping -> R-F compound escalation)"
)


class RiskEngine(ABC):
    """Deterministic safety core: rules in, explainable risk verdict out.

    Responsibility: execute the loaded rule pack against one horizon's inputs
    and return a :class:`RiskResult` -- level, integer score, drivers, hard
    rules triggered, confidence, evidence refs and physical-unit safety
    margins. Not responsible for: acquiring its own inputs, projecting future
    positions, fetching weather for a projected time, iterating horizons, or
    phrasing the explanation. ``assess_risk`` is one atomic call for one
    horizon (spec 4.2, 5.5).

    Phase 0 provides only the boundary: the concrete evaluation is Phase 1.
    """

    def __init__(self, pack: RulePack | None = None) -> None:
        """Store the rule pack this engine evaluates.

        Args:
            pack: Rule pack to evaluate. Concrete Phase 1 implementations are
                expected to accept and require it; the Phase 0 placeholder
                accepts ``None`` so it can be constructed before the pack is
                loaded.
        """
        self._pack = pack

    @property
    def pack(self) -> RulePack | None:
        """The rule pack this engine evaluates, if one was supplied."""
        return self._pack

    @abstractmethod
    def assess_risk(
        self,
        route_state: RouteContext,
        weather_state: WeatherForRouteTime,
        user_device_state: DeviceState,
        time_state: TimeState,
        projected_state: RouteProjection | None = None,
    ) -> RiskResult:
        """Assess one horizon and return an explainable verdict.

        Args:
            route_state: Current route position facts (spec 5.1).
            weather_state: Weather valid for the assessed position/time (spec 5.3).
            user_device_state: Device snapshot including battery/network (spec 5.4).
            time_state: Analysis clock and sunset instant.
            projected_state: The horizon's projection, when the caller made one
                (never for NOW). Supplies ``projected_arrival_time`` and
                ``projection_confidence`` for the projection-derived safety
                margins of spec 8; ``None`` means those margins stay unknown.

        Returns:
            A :class:`RiskResult` for this horizon.

        Raises:
            NotImplementedError: always, in Phase 0 -- the pipeline is Phase 1.
        """
        raise NotImplementedError(_PHASE1_MESSAGE)

    @classmethod
    def from_rule_pack(cls, pack: RulePack) -> "RiskEngine":
        """Build an engine bound to ``pack``.

        The pack is data, not code: the engine keeps thresholds out of its own
        source so they can be re-tuned without touching engine or prompt text.

        Called on the abstract base it returns the Phase 0 placeholder, which
        carries the pack and raises ``NotImplementedError`` on assessment. Called
        on a concrete subclass (``Phase1Engine.from_rule_pack(pack)``) it returns
        that subclass: the base cannot be instantiated -- it has an abstract
        method -- so a hardcoded ``_DeferredRiskEngine(pack)`` here would
        silently hand back the placeholder no matter which engine a caller asked
        for, defeating the factory entirely. Phase 1 replaces the placeholder.

        Args:
            pack: The validated rule pack to evaluate.

        Returns:
            An engine bound to ``pack``.
        """
        if cls is RiskEngine:
            return _DeferredRiskEngine(pack)
        return cls(pack)


class _DeferredRiskEngine(RiskEngine):
    """Placeholder engine returned by :meth:`RiskEngine.from_rule_pack`.

    Responsibility: hold the loaded pack so callers can wire the interface now
    and get an honest, phase-labelled failure instead of a silent wrong answer.
    Not responsible for: any evaluation. Phase 1 implements the real engine.
    """

    def __init__(self, pack: RulePack | None) -> None:
        """Store the pack this placeholder carries on behalf of Phase 1."""
        super().__init__(pack)

    def assess_risk(
        self,
        route_state: RouteContext,
        weather_state: WeatherForRouteTime,
        user_device_state: DeviceState,
        time_state: TimeState,
        projected_state: RouteProjection | None = None,
    ) -> RiskResult:
        """Refuse to assess and say which phase owes the implementation."""
        raise NotImplementedError(_PHASE1_MESSAGE)


__all__ = ["RiskEngine"]
