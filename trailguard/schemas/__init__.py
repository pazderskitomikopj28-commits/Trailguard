"""TrailGuard frozen schema contracts.

One import surface for every layer::

    from trailguard.schemas import TrailGuardContext, Horizon, RiskLevel

Phase 0 exports contracts only. Tool implementations, the risk engine, the
agent harness, providers and evals import from here; nothing in this package
executes business logic.
"""

from .context import (
    DataFreshnessStatus,
    DataQualityReport,
    DeviceState,
    GeoPoint,
    MovementState,
    Observed,
    RouteContext,
    TimeState,
    TrailGuardContext,
    WeatherPolicy,
)
from .route import Horizon, MovementTrend, RouteProjection
from .risk import (
    HorizonRiskAssessment,
    RuleDriver,
    RiskLevel,
    RiskResult,
    SafetyMargins,
    Severity,
)
from .scenario import (
    ExpectedSafetyCoreOutcome,
    ForbiddenBehavior,
    Scenario,
    ScenarioCategory,
    TimelinePoint,
    ToolName,
)
from .trace import (
    REDACTED,
    SECRET_KEY_PATTERNS,
    EvidenceTrace,
    HorizonCheckRecord,
    ToolCallRecord,
)
from .weather import WeatherForRouteTime

__all__ = [
    # context
    "DataFreshnessStatus",
    "DataQualityReport",
    "DeviceState",
    "GeoPoint",
    "MovementState",
    "Observed",
    "RouteContext",
    "TimeState",
    "TrailGuardContext",
    "WeatherPolicy",
    # route
    "Horizon",
    "MovementTrend",
    "RouteProjection",
    # risk
    "HorizonRiskAssessment",
    "RuleDriver",
    "RiskLevel",
    "RiskResult",
    "SafetyMargins",
    "Severity",
    # scenario
    "ExpectedSafetyCoreOutcome",
    "ForbiddenBehavior",
    "Scenario",
    "ScenarioCategory",
    "TimelinePoint",
    "ToolName",
    # trace
    "REDACTED",
    "SECRET_KEY_PATTERNS",
    "EvidenceTrace",
    "HorizonCheckRecord",
    "ToolCallRecord",
    # weather
    "WeatherForRouteTime",
]
