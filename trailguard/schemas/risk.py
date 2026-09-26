"""Risk contracts (spec sections 7, 8, 5.5, 6.2).

Everything here is explainable by construction: a risk result is a level, an
integer score, the rules that fired, evidence references and a *Safety Margins*
block with physical units (spec section 8). There is deliberately no
dimensionless "safety margin = 0.37" black box, and the banned identifier
``accident_probability`` is never used as an attribute, field or function name
anywhere in this package -- the domain concept is a *Projected Risk State*
(spec 6.2).
"""

from datetime import datetime
from enum import Enum
from typing import ClassVar

from pydantic import BaseModel, ConfigDict, Field

from .route import Horizon, RouteProjection


class RiskLevel(str, Enum):
    """Four-level safety scale.

    ``rank`` gives the comparable ordering GREEN=0 .. RED=3, used by the
    compound-escalation rule R-F.
    """

    GREEN = "GREEN"
    YELLOW = "YELLOW"
    ORANGE = "ORANGE"
    RED = "RED"

    @property
    def rank(self) -> int:
        """Comparable severity rank: GREEN=0, YELLOW=1, ORANGE=2, RED=3."""
        return RiskLevel.LEVELS.index(self)


#: Canonical severity order, low to high. Declared as a class constant on the
#: enum rather than an enum member; it is assigned right after the class body
#: because members do not exist while the body is still executing.
RiskLevel.LEVELS: "ClassVar[list[RiskLevel]]" = [
    RiskLevel.GREEN,
    RiskLevel.YELLOW,
    RiskLevel.ORANGE,
    RiskLevel.RED,
]


class Severity(str, Enum):
    """Rule severity vocabulary used by rule packs and drivers.

    ``rank`` gives the comparable ordering LOW=0 .. CRITICAL=3. The ordering is
    deliberately NOT the string ordering: ``"HIGH" < "MEDIUM"`` alphabetically, so
    a comparison such as "severity at or above MEDIUM" performed on the raw member
    would silently drop HIGH and CRITICAL rules -- which is exactly the filter the
    compound rule R-F applies. Ordering therefore goes through :attr:`rank`, the
    way :class:`RiskLevel` does it.
    """

    LOW = "LOW"
    MEDIUM = "MEDIUM"
    HIGH = "HIGH"
    CRITICAL = "CRITICAL"

    @property
    def rank(self) -> int:
        """Comparable severity rank: LOW=0, MEDIUM=1, HIGH=2, CRITICAL=3."""
        return Severity.LEVELS.index(self)


#: Canonical severity order, low to high. Assigned right after the class body
#: because members do not exist while the body is still executing.
Severity.LEVELS: "ClassVar[list[Severity]]" = [
    Severity.LOW,
    Severity.MEDIUM,
    Severity.HIGH,
    Severity.CRITICAL,
]


class RuleDriver(BaseModel):
    """One rule that contributed to a risk result.

    Responsibility: identify which rule fired, its kind, its severity and an
    optional human-readable detail. Not responsible for: scoring (see
    ``risk_core/scoring.py``) or choosing the final level.
    """

    model_config = ConfigDict(extra="forbid")

    rule_id: str = Field(..., description="Rule pack identifier, e.g. 'R-B'.")
    type: str = Field(..., description="Rule family, e.g. 'sunset_margin' or 'future_weather'.")
    severity: Severity = Field(..., description="Severity contributed by this rule.")
    detail: str | None = Field(default=None, description="Short, quotable explanation of what fired.")


class SafetyMargins(BaseModel):
    """Explainable safety margins with physical units (spec section 8).

    Responsibility: report how much headroom remains -- in minutes, percent
    battery, meters, minutes of weather age and projection confidence.
    Not responsible for: collapsing these into a single opaque score. ``None``
    means "not computable from the available data", which must be disclosed
    rather than defaulted.
    """

    model_config = ConfigDict(extra="forbid")

    time_margin_min: float | None = Field(
        default=None, description="minutes_to_sunset - estimated_minutes_to_finish; negative means finish after sunset."
    )
    projected_battery_percent: float | None = Field(
        default=None, description="Projected battery percentage at route finish."
    )
    route_deviation_m: float | None = Field(default=None, description="Off-route distance in meters.")
    weather_freshness_min: float | None = Field(default=None, description="Age of the weather snapshot in minutes.")
    projection_confidence: float | None = Field(default=None, description="Confidence of the projection used.")


class RiskResult(BaseModel):
    """Outcome of one ``assess_risk`` call for one horizon (spec 5.5).

    Responsibility: level, score, drivers, hard rules that fired, confidence,
    evidence references and safety margins -- a fully explainable verdict.
    Not responsible for: fetching its own inputs, projecting future positions
    or weather, or running the 30/60/120 horizon loop; all of those are the
    skill's job (spec 4.2, 5.5).
    """

    model_config = ConfigDict(extra="forbid")

    risk_level: RiskLevel
    risk_score: int = Field(..., ge=0, le=100, description="Aggregate soft score in [0, 100].")
    drivers: list[RuleDriver] = []
    hard_rules_triggered: list[str] = Field(
        default=[], description="Rule ids of triggered hard rules (these set a severity floor)."
    )
    confidence: float = Field(..., ge=0, le=1, description="Confidence in this verdict, given data quality and freshness.")
    evidence_refs: list[str] = Field(default=[], description="Pointers to the evidence backing this verdict.")
    safety_margins: SafetyMargins = Field(default_factory=SafetyMargins)
    horizon: Horizon = Field(default=Horizon.NOW, description="Horizon this verdict was computed for.")
    computed_at: datetime = Field(..., description="ISO8601 instant the verdict was produced.")


class HorizonRiskAssessment(BaseModel):
    """Skill-level record for one horizon: project -> weather -> assess.

    Responsibility: bundle the conditional projection, the weather freshness
    used with it and the resulting per-horizon risk verdict, plus the list of
    fields that were missing at that horizon.
    Not responsible for: being a tool output. This is assembled by the TrailGuard
    Skill's horizon loop (spec 10.2); no single tool may return all horizons
    at once (spec 4.2).
    """

    model_config = ConfigDict(extra="forbid")

    horizon: Horizon = Field(..., description="Which frozen horizon this record covers.")
    projected_state: RouteProjection | None = Field(
        default=None, description="Conditional projection used, if it could be computed."
    )
    weather_freshness_min: float | None = Field(default=None, description="Age of the weather snapshot used, in minutes.")
    risk: RiskResult = Field(..., description="Risk verdict for this horizon.")
    insufficient_data: list[str] = Field(
        default=[], description="Field dot-paths that were missing or stale and disclosed as such."
    )
