"""Skill-eval metric vocabulary and result container (spec sections 11, 12).

Layer B of the frozen benchmark protocol (spec 11.1) scores the *agent skill*:
whether it called the right tools, covered the required horizons, checked
freshness, refused to invent data, overrode (or faithfully reported) the
deterministic safety core, and explained its verdict from the evidence. Layer A
tests the deterministic core itself with ordinary unit tests and never uses the
metric names in this module, so the two layers cannot be used to grade each
other (spec 11.1: 禁止循環論證).

Three families of metric names exist because they answer different questions:

* :class:`AgentMetricName` -- protocol behaviour of the agent (Layer B).
* :class:`ExplanationMetricName` -- grounding and disclosure of the explanation
  (Layer B).
* :class:`ScenarioMetricName` -- scenario-level safety behaviour of a run over
  the synthetic dataset (Layer B).

NAMING PROHIBITION -- read before quoting any number produced with this module
============================================================================
The scenario-level names (``SCENARIO_HAZARD_MISS_RATE``,
``SCENARIO_FALSE_ALARM_RATE``, ``PROJECTED_RISK_DETECTION_RATE``) describe what
happened on a *synthetic, hand-authored scenario dataset* (spec 13, 27). They
are NEVER to be reported -- in a slide, a README, a paper or a conversation --
as real-world accident rates, real-world miss rates, real-world false-alarm
rates or any other epidemiological claim. TrailGuard is a prototype of an
outdoor-safety decision aid, not a medical device, a rescue system, an official
warning system or an accident-probability predictor (spec 22, 27). Say
"scenario hazard miss rate on the v1 synthetic dataset", never "miss rate".
The banned identifier ``accident_probability`` is not used anywhere in this
package.

Phase 0 scope: vocabulary and the result container only. Computing any metric
value is Phase 5 work (see ``evals/runner.py``, ``evals/report.py``).
"""

from enum import Enum

from pydantic import BaseModel, ConfigDict, Field


class AgentMetricName(str, Enum):
    """Agent protocol metrics of Layer B (spec 12, "Agent Behavior").

    Each name measures one observable property of a run's evidence trace
    (spec 18): which tools were called, which horizons were analysed, whether
    freshness was checked, whether missing data was disclosed rather than
    invented, and whether the agent tried to rewrite the deterministic core's
    verdict.
    """

    TOOL_CALL_COMPLETENESS = "TOOL_CALL_COMPLETENESS"
    REQUIRED_HORIZON_COVERAGE = "REQUIRED_HORIZON_COVERAGE"
    WRONG_TOOL_RATE = "WRONG_TOOL_RATE"
    MISSING_DATA_COMPLIANCE = "MISSING_DATA_COMPLIANCE"
    FRESHNESS_COMPLIANCE = "FRESHNESS_COMPLIANCE"
    SAFETY_CORE_OVERRIDE_RATE = "SAFETY_CORE_OVERRIDE_RATE"
    HALLUCINATED_FACT_RATE = "HALLUCINATED_FACT_RATE"


class ExplanationMetricName(str, Enum):
    """Explanation-quality metrics of Layer B (spec 12, "Explanation").

    These score the *text* of the final answer against the evidence trace: is
    every claim traceable to a tool output, are the reported drivers the ones the
    core actually triggered, and is uncertainty disclosed when the data was
    incomplete.
    """

    EXPLANATION_GROUNDING = "EXPLANATION_GROUNDING"
    DRIVER_CONSISTENCY = "DRIVER_CONSISTENCY"
    UNCERTAINTY_DISCLOSURE = "UNCERTAINTY_DISCLOSURE"


class ScenarioMetricName(str, Enum):
    """Scenario-level safety behaviour metrics (spec 12, "Scenario-level Safety Behavior").

    Scenario-level ONLY. These compare a run against the expectations pinned in
    the synthetic scenario file (spec 13, ``expected_safety_core``) and must
    never be relabelled as real-world accident or miss rates -- see the naming
    prohibition in the module docstring and spec 12's explicit note
    ("比赛阶段只能稱 Scenario Hazard Miss Rate，不得宣傳為真實世界事故漏報率").
    """

    SCENARIO_HAZARD_MISS_RATE = "SCENARIO_HAZARD_MISS_RATE"
    SCENARIO_FALSE_ALARM_RATE = "SCENARIO_FALSE_ALARM_RATE"
    PROJECTED_RISK_DETECTION_RATE = "PROJECTED_RISK_DETECTION_RATE"
    LEAD_TIME = "LEAD_TIME"


class MetricResult(BaseModel):
    """One scored metric for one run (or one aggregate over several runs).

    Responsibility: carry the metric name, its value, the unit the value is
    expressed in and how many observations it aggregates, plus free-form
    details (per-scenario breakdown, matched evidence references) so a number
    is never quoted without its provenance. Not responsible for: computing
    itself, or deciding whether a value is good -- interpretation belongs to
    ``evals/report.py`` and to the reviewer.

    ``unit`` is mandatory because these metrics are not interchangeable
    quantities: rates and ratios are ``"ratio"``, a lead time is ``"minutes"``.
    """

    model_config = ConfigDict(extra="forbid")

    name: str = Field(
        ...,
        description=(
            "Metric name; one of AgentMetricName, ExplanationMetricName or "
            "ScenarioMetricName (kept as a plain string so the three families can "
            "share one container)."
        ),
    )
    value: float = Field(..., description="Metric value in the unit given below.")
    unit: str = Field(..., description="Unit of the value, e.g. 'ratio', 'percent', 'minutes', 'count'.")
    n: int = Field(default=1, ge=1, description="Number of observations aggregated into this value.")
    details: dict = Field(
        default_factory=dict,
        description="Free-form provenance: per-scenario breakdown, evidence refs, scorer notes.",
    )


__all__ = [
    "AgentMetricName",
    "ExplanationMetricName",
    "MetricResult",
    "ScenarioMetricName",
]
