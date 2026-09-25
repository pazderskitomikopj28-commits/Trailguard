"""TrailGuard tool contract layer (spec sections 4, 5).

Five atomic tools exist in Phase 0 -- see
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

Phase 0 scope: contracts, descriptions and stubs only. Every implementation
body raises ``NotImplementedError`` naming the phase that owns it -- Phase 1 for
the deterministic core (route context, projection, device feed, risk
assessment) and Phase 2 for route-aware weather.
"""

from datetime import datetime
from typing import Any, Callable

from pydantic import BaseModel, ConfigDict, Field, field_validator

from ..schemas.context import DeviceState, GeoPoint, RouteContext, TimeState
from ..schemas.risk import RiskResult
from ..schemas.route import Horizon, MovementTrend, RouteProjection
from ..schemas.scenario import ToolName
from ..schemas.weather import WeatherForRouteTime

from .risk_assessment import assess_risk
from .route_context import get_route_context
from .route_projection import project_route_state
from .user_device import get_user_device_context
from .weather_route_time import get_weather_for_route_time


# --------------------------------------------------------------------------- #
# Tool input contracts
# --------------------------------------------------------------------------- #
# One pydantic model per tool so the harness can expose a JSON schema and
# validate arguments before a stub or a real implementation is invoked. These
# mirror the tool signatures one-to-one; the tool bodies own the semantics.


class GetRouteContextInput(BaseModel):
    """Input contract for :func:`get_route_context` (spec 5.1)."""

    model_config = ConfigDict(extra="forbid")

    route_id: str = Field(..., description="Identifier of the planned route (GPX asset reference).")
    current_position: GeoPoint = Field(..., description="GPS fix the route position facts are computed against.")
    timestamp: datetime = Field(..., description="ISO8601 instant the fix refers to.")


class ProjectRouteStateInput(BaseModel):
    """Input contract for :func:`project_route_state` (spec 5.2)."""

    model_config = ConfigDict(extra="forbid")

    route_context: RouteContext = Field(..., description="Current route position facts.")
    movement: MovementTrend = Field(..., description="Movement trend the frozen projection model weights.")
    horizon_min: int = Field(..., description="Horizon offset in minutes (0/30/60/120).")

    @field_validator("horizon_min")
    @classmethod
    def _frozen_horizon_set(cls, value: int) -> int:
        """Reject any offset outside the frozen horizon set (spec 6.1).

        The horizon set (NOW / +30min / +60min / +120min) is frozen for the MVP,
        and every horizon-keyed consumer -- the skill's loop, the scenario's
        ``expected_required_horizons``, the trace's ``horizons_checked`` -- is
        keyed on exactly those four values. An unconstrained ``int`` would let a
        model ask for ``horizon_min=15`` and get a projection nothing downstream
        can record or compare, so the contract refuses it here instead of
        silently answering a question the benchmark cannot score.
        """
        allowed = sorted(member.minutes for member in Horizon)
        if value not in allowed:
            raise ValueError(f"horizon_min must be one of the frozen horizons {allowed} minutes (spec 6.1), got {value}")
        return value


class GetWeatherForRouteTimeInput(BaseModel):
    """Input contract for :func:`get_weather_for_route_time` (spec 5.3)."""

    model_config = ConfigDict(extra="forbid")

    lat: float = Field(..., ge=-90, le=90, description="Query latitude in decimal degrees.")
    lon: float = Field(..., ge=-180, le=180, description="Query longitude in decimal degrees.")
    target_time: datetime = Field(..., description="ISO8601 instant the forecast must describe.")


class GetUserDeviceContextInput(BaseModel):
    """Input contract for :func:`get_user_device_context` (spec 5.4)."""

    model_config = ConfigDict(extra="forbid")

    device_id: str = Field(..., description="Identifier of the hiker device / feed.")


class AssessRiskInput(BaseModel):
    """Input contract for :func:`assess_risk` (spec 5.5).

    The three required states are the caller's *inputs*: spec 5.5 forbids the
    core from projecting or fetching, so the Skill projects and then hands the
    horizon's route state over. ``projected_state`` is the optional mirror of
    that hand-off for the horizon's *projected* route state, without which the
    core could not report the projection-derived safety margins of spec 8
    (``time_margin_min`` from ``projected_arrival_time``,
    ``projection_confidence`` from ``RouteProjection.projection_confidence``) for
    any horizon after NOW. It is optional precisely because the NOW horizon has
    no projection to pass.
    """

    model_config = ConfigDict(extra="forbid")

    route_state: RouteContext = Field(..., description="Route state for the horizon under assessment.")
    weather_state: WeatherForRouteTime = Field(..., description="Weather for the same horizon, with provenance.")
    user_device_state: DeviceState = Field(..., description="Device state for the hiker.")
    time_state: TimeState = Field(..., description="Analysis clock and sunset instant for the time margin.")
    projected_state: RouteProjection | None = Field(
        default=None,
        description=(
            "Projected route state for this horizon, from project_route_state, when one exists (not for NOW). "
            "Supplies the projected arrival time and projection confidence the safety margins of spec 8 report."
        ),
    )


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

#: Name -> implementation. Phase 0 bodies are stubs raising ``NotImplementedError``.
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
