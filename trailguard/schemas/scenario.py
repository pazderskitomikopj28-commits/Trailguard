"""Scenario dataset contracts (spec sections 13, 17).

A :class:`Scenario` is a fully reproducible benchmark unit: fixed GPX route
reference, fixed weather snapshot reference, fixed movement and device
timelines, and the expectations the harness will be compared against.

Every scenario in this dataset is synthetic. ``simulation_note`` exists so no
scenario can ever be mistaken for a real incident record, which matters because
the scenario-level metrics derived from this data must never be reported as
real-world accident rates (spec 12).
"""

from enum import Enum
from typing import Generic, TypeVar

from pydantic import BaseModel, ConfigDict, Field

from .context import DeviceState, MovementState, TrailGuardContext
from .risk import RiskLevel
from .route import Horizon

T = TypeVar("T")


class ToolName(str, Enum):
    """Canonical Phase 0 tool registry (spec 4.1).

    Exactly five atomic tools exist in Phase 0. ``find_safe_points`` and
    ``find_escape_option`` are deliberately NOT in this registry -- they are
    optional later-phase capabilities (spec 4.1) and implementing them here
    would blur the atomic-tool contract (spec 4.2 forbids one-shot tools such
    as ``forecast_risk`` that answer all three horizons at once).
    """

    GET_ROUTE_CONTEXT = "get_route_context"
    PROJECT_ROUTE_STATE = "project_route_state"
    GET_WEATHER_FOR_ROUTE_TIME = "get_weather_for_route_time"
    GET_USER_DEVICE_CONTEXT = "get_user_device_context"
    ASSESS_RISK = "assess_risk"


class ForbiddenBehavior(str, Enum):
    """Behaviours the benchmark treats as protocol violations (spec 10).

    Captured per scenario so the agent's behaviour can be scored against them,
    e.g. hallucinating weather or overriding a hard rule.
    """

    HALLUCINATE_WEATHER = "HALLUCINATE_WEATHER"
    HALLUCINATE_ROUTE = "HALLUCINATE_ROUTE"
    GUESS_MISSING_DATA = "GUESS_MISSING_DATA"
    MODIFY_RISK_LEVEL = "MODIFY_RISK_LEVEL"
    OVERRIDE_HARD_RULE = "OVERRIDE_HARD_RULE"
    SKIP_FRESHNESS_CHECK = "SKIP_FRESHNESS_CHECK"
    SKIP_REQUIRED_HORIZON = "SKIP_REQUIRED_HORIZON"
    SKIP_PROJECTED_LOCATION = "SKIP_PROJECTED_LOCATION"
    CLAIM_ACCIDENT_PROBABILITY = "CLAIM_ACCIDENT_PROBABILITY"


class ScenarioCategory(str, Enum):
    """Dataset partition a scenario belongs to (spec 13, 17)."""

    BASELINE = "baseline"
    EDGE_CASES = "edge_cases"
    COMPOUND = "compound"


class TimelinePoint(BaseModel, Generic[T]):
    """One value on a replay timeline, offset from the scenario start.

    Responsibility: replay data only (fixed movement / device timelines for
    Replay Mode, spec 17). Not responsible for: interpolation or evaluation --
    driving the timeline is runtime work.
    """

    model_config = ConfigDict(extra="forbid")

    t_offset_min: float = Field(..., description="Minutes after scenario start when this value becomes active.")
    value: T


class ExpectedSafetyCoreOutcome(BaseModel):
    """Expected deterministic-core verdict for a scenario, when applicable.

    Responsibility: pin the level, hard rules and drivers the deterministic
    safety core must produce, so Layer A tests are not circular (spec 11.1).
    Not responsible for: describing the agent's behaviour; ``None`` fields mean
    "not pinned for this scenario".
    """

    model_config = ConfigDict(extra="forbid")

    risk_level: RiskLevel | None = None
    hard_rules_triggered: list[str] = []
    drivers_expected: list[str] = []


class Scenario(BaseModel):
    """A reproducible benchmark scenario for Replay Mode (spec 13, 17).

    Responsibility: bundle the starting context, the replay timelines, the
    route and weather references and all expectations (required tools, required
    horizons, forbidden behaviours, expected safety-core outcome) so the same
    run can be repeated byte-for-byte.
    Not responsible for: executing itself -- running it belongs to the eval
    runner, and reasoning about it belongs to the agent.
    """

    model_config = ConfigDict(extra="forbid")

    scenario_id: str = Field(..., description="Stable identifier, e.g. 'S04'.")
    description: str = Field(..., description="What the scenario exercises.")
    category: ScenarioCategory = Field(..., description="Dataset partition this scenario belongs to.")
    is_synthetic: bool = Field(default=True, description="Always True for the v1 dataset; never a real incident record.")
    simulation_note: str = Field(
        ..., description="Disclosure that this is simulated input data, not a real-world event record."
    )
    task_prompt: str = Field(..., description="Task text handed to the agent (identical in both skill conditions).")
    route_ref: str = Field(..., description="Path to the fixed GPX route under replay/routes/.")
    weather_snapshot_ref: str | None = Field(
        default=None, description="Replay weather snapshot reference, or None when weather is unavailable/stale."
    )
    initial_context: TrailGuardContext = Field(..., description="Context snapshot at scenario start (spec 9).")
    movement_timeline: list[TimelinePoint[MovementState]] = Field(
        default=[], description="Fixed movement timeline for replay."
    )
    device_timeline: list[TimelinePoint[DeviceState]] = Field(
        default=[], description="Fixed device timeline (battery / network / fatigue) for replay."
    )
    expected_required_tools: list[ToolName] = Field(..., description="Tools the agent is expected to call.")
    expected_required_horizons: list[Horizon] = Field(..., description="Horizons the agent is expected to analyse.")
    forbidden_behaviors: list[ForbiddenBehavior] = Field(..., description="Behaviours that would be protocol violations.")
    expected_safety_core: ExpectedSafetyCoreOutcome | None = Field(
        default=None, description="Expected deterministic-core verdict, when the scenario pins one."
    )
