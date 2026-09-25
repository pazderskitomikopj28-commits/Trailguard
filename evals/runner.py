"""One scenario, one run: the eval runner contract (spec sections 11.1, 13, 3.1).

``EvalRunResult`` is the atomic unit of the benchmark: exactly one agent
episode, its evidence trace (spec 18) and the metric results scored from that
trace. :func:`run_scenario` is the only sanctioned way to produce one.

Phase 0 scope: the result container and the call contract. Scoring, tool
execution bookkeeping and metric computation are Phase 5 work -- the body raises
``NotImplementedError`` so nothing here silently computes a metric.

FAIRNESS (spec 3.1) -- the reason this module exists
====================================================
``run_scenario`` must call :func:`trailguard.agent.harness.run_agent` with
parameters that are IDENTICAL in both conditions; the only permitted difference
between a ``"without_skill"`` run and a ``"with_skill"`` run is the
``skill_enabled`` flag. Therefore the sampling parameters, the max-step budget,
the tool registry and the context/task text are plain arguments of
``run_scenario`` and are never derived from ``skill_enabled``, and the condition
label is recorded on the result so a comparison can never silently mix the two.
"""

from typing import Any, Callable, Literal

from pydantic import BaseModel, ConfigDict, Field

from trailguard.providers.base import ModelProvider
from trailguard.schemas.scenario import Scenario, ToolName
from trailguard.schemas.trace import EvidenceTrace

from .metrics import MetricResult

#: Benchmark condition label. ``without_skill`` is the baseline (no SKILL.md text
#: in the system prompt), ``with_skill`` the treatment. Exactly two conditions
#: exist, so the comparison is a single-variable experiment (spec 3.1).
EvalCondition = Literal["without_skill", "with_skill"]

__all__ = ["EvalCondition", "EvalRunResult", "run_scenario"]


class EvalRunResult(BaseModel):
    """Result of running one scenario once, with its evidence trace.

    Responsibility: bundle the run identity, the scenario it replayed, the
    condition it ran in, the model that produced it, the tool registry the run
    was offered, the frozen evidence trace and the metric results scored from
    that trace -- everything a comparison needs, and nothing that has to be
    re-derived from a log.
    Not responsible for: judging the run (``evals/compare.py`` and
    ``evals/report.py`` aggregate results) or for holding more than one episode.

    ``condition`` is part of the contract on purpose: a result whose condition
    is unknown cannot be placed in a fair baseline/treatment comparison.

    ``tools_offered`` is part of it for the same reason. The tool registry is a
    frozen variable of the single-variable experiment (spec 3.1), and the
    evidence trace only records the tools the model *chose to call* -- which is
    the effect being measured, not the configuration. Without this field the
    comparison layer cannot audit the registry at all: it can only compare two
    sets of *observed* calls, which legitimately differ between a baseline and a
    treatment, and which are equal -- misleadingly so -- whenever both sides
    called nothing. Phase 5 fills it from the registry handed to
    :func:`trailguard.agent.harness.run_agent`.
    """

    model_config = ConfigDict(extra="forbid")

    run_id: str = Field(..., description="Unique identifier of the agent run (from the harness trace).")
    scenario_id: str = Field(..., description="Scenario this run replayed.")
    condition: EvalCondition = Field(
        ..., description="'without_skill' (baseline) or 'with_skill' (treatment); see spec 3.1."
    )
    model_name: str = Field(..., description="Model identifier reported by the provider.")
    tools_offered: list[str] = Field(
        default=[],
        description=(
            "Names of the tools the harness advertised to the model in this run, i.e. the registry "
            "the caller passed to run_scenario(tools=...). Frozen variable of the spec 3.1 "
            "comparison; recorded by the Phase 5 runner. Deliberately NOT the tools the model "
            "called -- that difference is the effect under measurement."
        ),
    )
    trace: EvidenceTrace = Field(..., description="Structured evidence trace recorded during the run (spec 18).")
    metric_results: list[MetricResult] = Field(
        default=[], description="Metrics scored from this run, once Phase 5 scoring exists."
    )


def run_scenario(
    scenario: Scenario,
    *,
    model_provider: ModelProvider,
    skill_enabled: bool,
    tools: dict[ToolName, Callable[..., Any]] | None,
    temperature: float,
    max_tokens: int,
    max_steps: int,
) -> EvalRunResult:
    """Replay one scenario once through the shared harness and score the run.

    Fairness contract (spec 3.1): calls
    :func:`trailguard.agent.harness.run_agent` with ``task=scenario.task_prompt``
    and ``context=scenario.initial_context`` (both byte-identical in both
    conditions), the same ``model_provider``, the same ``tools`` registry, the
    same ``temperature`` / ``max_tokens`` / ``max_steps`` and
    ``scenario_id=scenario.scenario_id``. ``skill_enabled`` is the ONLY argument
    whose value differs between the baseline and the treatment condition, and it
    maps to the recorded condition label as
    ``False -> "without_skill"`` / ``True -> "with_skill"``.

    Responsibility: produce one :class:`EvalRunResult` per (scenario, condition)
    pair so the comparison layer receives matched, identically-sampled runs --
    including the ``tools_offered`` registry the run was actually given, without
    which the fairness audit of ``evals/compare.py`` has nothing to compare and
    must refuse the comparison.
    Not responsible for: implementing the skill, executing tools, scoring the
    run or aggregating across scenarios.

    Args:
        scenario: Synthetic scenario to replay (spec 13).
        model_provider: Transport backend; identical in both conditions.
        skill_enabled: Whether SKILL.md text is injected. The single variable.
        tools: Tool registry handed to the harness; identical in both conditions.
            ``None`` means the canonical ``trailguard.tools.TOOL_FUNCTIONS``.
        temperature: Sampling temperature; identical in both conditions.
        max_tokens: Output cap; identical in both conditions.
        max_steps: Maximum model turns; identical in both conditions.

    Returns:
        The :class:`EvalRunResult` for this episode.

    Raises:
        NotImplementedError: always, in Phase 0 -- run orchestration and metric
            scoring are Phase 5 work (spec 25).
    """
    raise NotImplementedError(
        "Phase 5: benchmark run orchestration -- call trailguard.agent.harness.run_agent with "
        "identical parameters for both skill conditions, then score the returned EvidenceTrace "
        "into EvalRunResult.metric_results (spec 11, 12)"
    )
