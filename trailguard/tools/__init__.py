"""TrailGuard tool contract layer (spec sections 4, 5).

Five atomic tools exist -- see
:class:`~trailguard.schemas.scenario.ToolName` for why. Each tool is a single
capability with its own input and output contract; the registry below is the
single source of truth for what the agent harness may expose to a model.

Atomicity rules enforced here (spec 4.1, 4.2):

* a tool answers ONE question, and there is deliberately no ``forecast_risk``
  that returns ``{"30m": ..., "60m": ..., "120m": ...}`` in one call;
* the 30/60/120 horizon loop, the tool combination and the freshness and
  missing-data protocols are coordinated by the TrailGuard Skill (SKILL.md),
  never by a tool;
* every tool lists its forbidden uses, so a reviewer can check a proposed call
  against the spec without re-reading it.

Phase ownership: all five tools now have real bodies -- four implemented in Phase 1
(route context, route projection, device context, risk assessment) and
``get_weather_for_route_time`` in Phase 2 (route-aware weather providers, replay and
live). Five real bodies do not mean five decisions: the tools stay atomic, the
30/60/120 horizon loop still belongs to the Skill, and the deterministic core still
runs with no LLM, no key and no network (``mode = "replay"`` needs none, and
``mode = "live"`` needs only the public Open-Meteo endpoint).

The input contracts live in :mod:`trailguard.tools.contracts` so the
implementations can bind their arguments to them without importing this package;
they are re-exported below for callers that only want the contract.
"""

from datetime import datetime
from typing import Any, Callable

from pydantic import BaseModel, ConfigDict, Field

from ..schemas.context import DeviceState, RouteContext, TimeState
from ..schemas.risk import RiskResult
from ..schemas.route import Horizon, MovementTrend, RouteProjection
from ..schemas.scenario import ToolName
from ..schemas.weather import WeatherForRouteTime

from .contracts import (
    AssessRiskInput,
    GetRouteContextInput,
    GetUserDeviceContextInput,
    GetWeatherForRouteTimeInput,
    ProjectRouteStateInput,
)
from .risk_assessment import assess_risk
from .route_context import get_route_context
from .route_projection import project_route_state
from .user_device import get_user_device_context
from .weather_route_time import get_weather_for_route_time


# --------------------------------------------------------------------------- #
# Registry
# --------------------------------------------------------------------------- #


class ToolSpec(BaseModel):
    """Contract, phase and boundary of one atomic tool.

    Responsibility: describe what a tool may be used for, which schemas bound
    it, when it is implemented and what it must never be used for.
    Not responsible for: implementing anything, or orchestrating several tools
    -- composition is the Skill's job (spec 4.2).
    """

    model_config = ConfigDict(extra="forbid")

    name: ToolName = Field(..., description="Canonical tool identifier.")
    description: str = Field(..., description="One-paragraph capability description handed to the model.")
    input_model: type[BaseModel] = Field(..., description="Pydantic model bounding the tool arguments.")
    output_model: type[BaseModel] = Field(..., description="Pydantic model bounding the tool result.")
    implemented_in_phase: str = Field(
        ..., description="Development phase that implements the body, e.g. 'Phase 1' or 'Phase 2'."
    )
    forbidden_uses: list[str] = Field(
        default=[], description="Spec boundaries this tool must never be used for (spec 4, 5)."
    )


#: Exactly five entries, in the canonical order of spec 4.1.
TOOL_SPECS: dict[ToolName, ToolSpec] = {
    ToolName.GET_ROUTE_CONTEXT: ToolSpec(
        name=ToolName.GET_ROUTE_CONTEXT,
        description=(
            "Get where the hiker currently is on a known route: progress, nearest point on the planned "
            "polyline, remaining distance and ascent, off-route distance in meters and route data quality."
        ),
        input_model=GetRouteContextInput,
        output_model=RouteContext,
        implemented_in_phase="Phase 1",
        forbidden_uses=[
            "must not output risk levels or safety verdicts (spec 5.1)",
            "must not query weather or judge freshness",
            "must not project a future position; that is project_route_state",
        ],
    ),
    ToolName.PROJECT_ROUTE_STATE: ToolSpec(
        name=ToolName.PROJECT_ROUTE_STATE,
        description=(
            "Project the hiker's future route state at one horizon, conditional on continuing at the "
            "currently observable movement trend: projected progress, position, arrival time, remaining "
            "distance and projection confidence. The result is a kinematic projected route state -- "
            "never a risk verdict and never a probability; interpreting it is assess_risk's job."
        ),
        input_model=ProjectRouteStateInput,
        output_model=RouteProjection,
        implemented_in_phase="Phase 1",
        forbidden_uses=[
            "must not query weather; that is get_weather_for_route_time (spec 5.2)",
            "must not judge risk or return a risk level",
            "must not answer the 30/60/120 horizon set in one call; the horizon loop belongs to the Skill (spec 4.2)",
            "must not apply medical or injury thresholds; the fatigue penalty is a configurable demo-calibration parameter (spec 6.3)",
        ],
    ),
    ToolName.GET_WEATHER_FOR_ROUTE_TIME: ToolSpec(
        name=ToolName.GET_WEATHER_FOR_ROUTE_TIME,
        description=(
            "Get weather for a target position and target time -- typically the projected position and "
            "projected arrival time of a horizon -- with provider provenance, freshness in minutes and "
            "confidence."
        ),
        input_model=GetWeatherForRouteTimeInput,
        output_model=WeatherForRouteTime,
        implemented_in_phase="Phase 2",
        forbidden_uses=[
            "must not output safety levels; hazard interpretation is rule R-C's job (spec 5.3, 7)",
            "must not fabricate, interpolate or best-guess weather when no snapshot exists; report the gap instead",
            "must not decide whether stale weather is acceptable; that is the Skill's freshness protocol (spec 10.4)",
        ],
    ),
    ToolName.GET_USER_DEVICE_CONTEXT: ToolSpec(
        name=ToolName.GET_USER_DEVICE_CONTEXT,
        description=(
            "Get the hiker device state (spec 5.4): battery percent, network availability, last network "
            "contact, the device feed's own data quality, and the observed movement window -- current, "
            "10-minute and 30-minute speeds plus the pace drop -- together with the rule-constructed "
            "fatigue feature the deterministic rules consume."
        ),
        input_model=GetUserDeviceContextInput,
        output_model=DeviceState,
        implemented_in_phase="Phase 1",
        forbidden_uses=[
            "must not present fatigue_index as a medical model output; it is a rule-constructed simulated feature (spec 5.4)",
            "must not infer health, exhaustion or injury",
            "must not output risk levels or decide whether the remaining battery is sufficient",
        ],
    ),
    ToolName.ASSESS_RISK: ToolSpec(
        name=ToolName.ASSESS_RISK,
        description=(
            "Run the deterministic safety core for ONE horizon over the supplied route, weather, device and "
            "time states: hard rules set a severity floor, soft rules aggregate into a score, compound soft "
            "signals escalate one level. Returns level, score, drivers, triggered hard rules, confidence, "
            "evidence references and explainable safety margins with physical units. The projection-derived "
            "margins (projected arrival time, projection confidence) are read from the optional projected "
            "state the caller attaches; without it those margins are reported as unknown rather than guessed."
        ),
        input_model=AssessRiskInput,
        output_model=RiskResult,
        implemented_in_phase="Phase 1",
        forbidden_uses=[
            "must not project future positions; callers supply the horizon's route state (spec 5.5)",
            "must not fetch weather; callers supply the horizon's weather state",
            "must not run the 30/60/120 loop or aggregate horizons (spec 4.2, 5.5)",
            "must not override or downgrade a triggered hard rule (spec 10.5)",
            "must not return an accident probability; the concept is a Projected Risk State (spec 6.2)",
        ],
    ),
}

#: Name -> implementation. Every body is real: Phase 1 implemented four of them and
#: Phase 2 implemented ``get_weather_for_route_time``, so no entry here raises
#: ``NotImplementedError``.
TOOL_FUNCTIONS: dict[ToolName, Callable[..., Any]] = {
    ToolName.GET_ROUTE_CONTEXT: get_route_context,
    ToolName.PROJECT_ROUTE_STATE: project_route_state,
    ToolName.GET_WEATHER_FOR_ROUTE_TIME: get_weather_for_route_time,
    ToolName.GET_USER_DEVICE_CONTEXT: get_user_device_context,
    ToolName.ASSESS_RISK: assess_risk,
}


def all_tool_names() -> list[ToolName]:
    """Canonical tool names in registry order (spec 4.1).

    Responsibility: one ordered list the harness and the benchmark share, so
    tool-set equality between skill conditions is checkable.
    Not responsible for: describing capabilities -- see :data:`TOOL_SPECS`.
    """
    return list(TOOL_SPECS)


def get_tool_spec(name: ToolName) -> ToolSpec:
    """Return the :class:`ToolSpec` for ``name``.

    Raises ``KeyError`` for any name outside the frozen five-tool registry, so
    an unknown tool fails loudly instead of being silently invented by a caller.
    """
    try:
        return TOOL_SPECS[name]
    except KeyError:
        raise KeyError(f"unknown tool {name!r}; Phase 0 registry holds exactly {len(TOOL_SPECS)} tools") from None


def tool_schema_dicts() -> list[dict[str, Any]]:
    """JSON-schema-shaped tool specs for the agent harness (spec 4.1, 15).

    Responsibility: hand the model the five tool contracts -- name, description
    and the JSON schema of the arguments -- so tool calling is bounded by the
    same schemas the implementations use.
    Not responsible for: execution, permissioning or horizon orchestration.
    """
    return [
        {
            "name": spec.name.value,
            "description": spec.description,
            "input_schema": spec.input_model.model_json_schema(),
        }
        for spec in TOOL_SPECS.values()
    ]


__all__ = [
    "TOOL_FUNCTIONS",
    "TOOL_SPECS",
    "AssessRiskInput",
    "GetRouteContextInput",
    "GetUserDeviceContextInput",
    "GetWeatherForRouteTimeInput",
    "ProjectRouteStateInput",
    "ToolSpec",
    "all_tool_names",
    "get_tool_spec",
    "tool_schema_dicts",
]
