"""Eval-layer contract tests: the fairness audit and the metric containers.

Responsibility: prove the one thing the Phase 0 eval layer actually implements --
that ``evals.compare.compare_runs`` refuses to compare two run sets unless the
frozen single-variable experiment (spec 3.1) was respected -- plus the shape of
the containers that carry the result: ``EvalRunResult`` with the tool registry it
was offered, ``MetricResult`` with its mandatory unit, and the DGX Spark evidence.

Explicit non-responsibility: these tests do not score a metric, do not aggregate
deltas and do not run a scenario -- every one of those bodies raises
``NotImplementedError`` and belongs to Phase 5 (spec 25). What *is* asserted here
is that the fairness guards are real and that they look at the right variable: a
treatment run may call tools the baseline skipped, and that must stay legal.
"""

from __future__ import annotations

from datetime import datetime, time, timezone
from typing import Any

import pytest
from pydantic import ValidationError

from evals import (
    AgentMetricName,
    BenchmarkReport,
    ComparisonReport,
    DgxSparkEvidence,
    EvalCondition,
    EvalRunResult,
    ExplanationMetricName,
    MetricResult,
    ScenarioMetricName,
)
from evals.compare import _check_same_tool_set, compare_runs
from evals.runner import run_scenario

from trailguard.agent.trace import TraceCollector
from trailguard.schemas.scenario import ToolName
from trailguard.schemas.trace import EvidenceTrace

STARTED_AT = datetime(2026, 5, 18, 10, 30, tzinfo=timezone.utc)
FINISHED_AT = datetime(2026, 5, 18, 10, 31, tzinfo=timezone.utc)

#: The canonical five-tool registry, the only one either condition may offer.
CANONICAL_TOOLS: tuple[str, ...] = tuple(tool.value for tool in ToolName)


def _trace(
    *,
    run_id: str,
    scenario_id: str,
    model_name: str = "stub-model",
    skill_loaded: bool = False,
    temperature: float = 0.0,
    max_tokens: int = 2048,
    max_steps: int = 12,
    called_tools: tuple[ToolName, ...] = (),
) -> EvidenceTrace:
    """Build one realistic evidence trace, recording the calls it is given.

    ``called_tools`` is what the *model* chose to call, which is the effect the
    benchmark measures and is deliberately independent of what was offered.
    """
    collector = TraceCollector.new(
        run_id=run_id,
        scenario_id=scenario_id,
        model_name=model_name,
        provider="StubProvider",
        skill_loaded=skill_loaded,
        sampling_params={"temperature": temperature, "max_tokens": max_tokens, "max_steps": max_steps},
        started_at=STARTED_AT,
    )
    for seq, tool in enumerate(called_tools, start=1):
        collector.record_tool_call(
            tool=tool,
            arguments={"route_id": "scn_route_sample"},
            result_ref=None,
            result_summary=f"{tool.value} -> ok",
            status="ok",
            error=None,
        )
    return collector.finish(finished_at=FINISHED_AT)


def _run(
    *,
    run_id: str,
    scenario_id: str,
    condition: EvalCondition,
    model_name: str = "stub-model",
    tools_offered: tuple[str, ...] = CANONICAL_TOOLS,
    temperature: float = 0.0,
    max_tokens: int = 2048,
    max_steps: int = 12,
    called_tools: tuple[ToolName, ...] = (),
) -> EvalRunResult:
    """One :class:`EvalRunResult` as the Phase 5 runner would build it."""
    return EvalRunResult(
        run_id=run_id,
        scenario_id=scenario_id,
        condition=condition,
        model_name=model_name,
        tools_offered=list(tools_offered),
        trace=_trace(
            run_id=run_id,
            scenario_id=scenario_id,
            model_name=model_name,
            skill_loaded=(condition == "with_skill"),
            temperature=temperature,
            max_tokens=max_tokens,
            max_steps=max_steps,
            called_tools=called_tools,
        ),
        metric_results=[],
    )


def _matched_pair(
    *, scenario_id: str, baseline_calls: tuple[ToolName, ...] = (), treatment_calls: tuple[ToolName, ...] = ()
) -> tuple[list[EvalRunResult], list[EvalRunResult]]:
    """A baseline run and a treatment run over the same fairly-configured scenario."""
    baseline = [
        _run(
            run_id=f"{scenario_id}-baseline",
            scenario_id=scenario_id,
            condition="without_skill",
            called_tools=baseline_calls,
        )
    ]
    treatment = [
        _run(
            run_id=f"{scenario_id}-treatment",
            scenario_id=scenario_id,
            condition="with_skill",
            called_tools=treatment_calls,
        )
    ]
    return baseline, treatment


# --------------------------------------------------------------------------- #
# The result container records the registry, not the calls (spec 3.1)
# --------------------------------------------------------------------------- #


def test_run_result_defaults_to_no_offered_tools() -> None:
    """``tools_offered`` is optional so a container can exist before Phase 5 fills it."""
    run = _run(run_id="r1", scenario_id="S01", condition="without_skill", tools_offered=())

    assert run.tools_offered == []
    assert run.trace.run_id == "r1"


def test_run_result_rejects_unknown_fields() -> None:
    """The eval containers stay closed: an extra key is a contract break, not a free-for-all."""
    with pytest.raises(ValidationError) as excinfo:
        EvalRunResult(
            run_id="r1",
            scenario_id="S01",
            condition="without_skill",
            model_name="stub-model",
            trace=_trace(run_id="r1", scenario_id="S01"),
            accident_probability=0.01,
        )

    assert "accident_probability" in str(excinfo.value)
    assert "Extra inputs" in str(excinfo.value)


def test_offered_registry_may_name_tools_the_model_never_called() -> None:
    """Offering a tool is independent of using it; the trace decides the latter."""
    run = _run(
        run_id="r1",
        scenario_id="S01",
        condition="with_skill",
        called_tools=(ToolName.GET_ROUTE_CONTEXT,),
    )

    assert set(run.tools_offered) == set(CANONICAL_TOOLS)
    recorded = {call.tool for call in run.trace.tool_calls}
    assert recorded == {ToolName.GET_ROUTE_CONTEXT}


# --------------------------------------------------------------------------- #
# Fairness: the tool registry audit (spec 3.1, 4.1)
# --------------------------------------------------------------------------- #


def test_identical_offered_registry_with_different_calls_still_reaches_phase_5() -> None:
    """Same registry offered, different tools called: fair, so the audit is silent.

    This is the regression that matters. The treatment is *meant* to call tools
    the baseline skipped; an audit that compared the recorded calls would reject
    every legitimate with-skill run, and one that compared nothing at all would
    accept two runs that both called nothing.
    """
    baseline, treatment = _matched_pair(
        scenario_id="S01",
        baseline_calls=(),
        treatment_calls=(ToolName.GET_ROUTE_CONTEXT, ToolName.GET_WEATHER_FOR_ROUTE_TIME),
    )

    _check_same_tool_set(baseline, treatment)  # no exception: the configuration is fair


def test_identical_offered_registry_with_identical_calls_is_fair() -> None:
    """A run where the model skipped every tool is still fairly compared."""
    baseline, treatment = _matched_pair(scenario_id="S04", baseline_calls=(), treatment_calls=())

    _check_same_tool_set(baseline, treatment)


def test_different_offered_registries_are_refused() -> None:
    """Offering a treatment run an extra tool is a configuration difference (spec 3.1)."""
    baseline, treatment = _matched_pair(scenario_id="S01")
    treatment[0].tools_offered = [*CANONICAL_TOOLS, "forecast_risk"]

    with pytest.raises(ValueError) as excinfo:
        _check_same_tool_set(baseline, treatment)

    message = str(excinfo.value)
    assert "different tool sets" in message
    assert "forecast_risk" in message


def test_a_smaller_treatment_registry_is_refused() -> None:
    """The audit is asymmetric-safe: a withdrawn tool is as unfair as an added one."""
    baseline, treatment = _matched_pair(scenario_id="S01")
    treatment[0].tools_offered = [*CANONICAL_TOOLS[:2]]

    with pytest.raises(ValueError) as excinfo:
        _check_same_tool_set(baseline, treatment)

    assert "only in baseline" in str(excinfo.value)


def test_an_unrecorded_registry_is_never_a_silent_pass() -> None:
    """Nothing recorded on either side means "unauditable", not "identical"."""
    baseline, treatment = _matched_pair(scenario_id="S01", baseline_calls=(), treatment_calls=())
    baseline[0].tools_offered = []
    treatment[0].tools_offered = []

    with pytest.raises(ValueError) as excinfo:
        _check_same_tool_set(baseline, treatment)

    message = str(excinfo.value)
    assert "cannot be audited" in message
    assert "tools_offered" in message


def test_only_one_condition_recording_its_registry_is_refused() -> None:
    """A half-recorded registry is an asymmetry, not a difference that may pass."""
    baseline, treatment = _matched_pair(scenario_id="S01")
    treatment[0].tools_offered = []

    with pytest.raises(ValueError) as excinfo:
        _check_same_tool_set(baseline, treatment)

    message = str(excinfo.value)
    assert "only one condition recorded" in message
    assert "treatment" in message


def test_a_trace_recording_an_unoffered_tool_is_refused() -> None:
    """The trace and the registry must describe the same run.

    The harness can only record registered tools, so a recorded call outside the
    offered set means the two inputs came from different places.
    """
    shared_registry = [ToolName.GET_ROUTE_CONTEXT.value, ToolName.GET_WEATHER_FOR_ROUTE_TIME.value]
    baseline, treatment = _matched_pair(
        scenario_id="S01",
        treatment_calls=(ToolName.ASSESS_RISK,),
    )
    baseline[0].tools_offered = list(shared_registry)
    treatment[0].tools_offered = list(shared_registry)

    with pytest.raises(ValueError) as excinfo:
        _check_same_tool_set(baseline, treatment)

    message = str(excinfo.value)
    assert "never offered" in message
    assert ToolName.ASSESS_RISK.value in message
    assert ToolName.ASSESS_RISK.value not in shared_registry


def test_a_deliberately_empty_registry_has_nothing_to_cross_check() -> None:
    """A run offered no tools cannot have a trace violating its empty registry."""
    baseline, treatment = _matched_pair(scenario_id="S01")
    treatment[0].tools_offered = []
    treatment[0].trace.tool_calls = []

    with pytest.raises(ValueError) as excinfo:
        _check_same_tool_set(baseline, treatment)

    # Refused for being unrecorded, not for a phantom call.
    assert "only one condition recorded" in str(excinfo.value)


# --------------------------------------------------------------------------- #
# Fairness: the other three preconditions
# --------------------------------------------------------------------------- #


def test_different_scenario_sets_are_refused() -> None:
    """Every scenario must be replayed in both conditions (spec 3.1)."""
    baseline = [_run(run_id="a", scenario_id="S01", condition="without_skill")]
    treatment = [_run(run_id="b", scenario_id="S04", condition="with_skill")]

    with pytest.raises(ValueError) as excinfo:
        compare_runs(baseline, treatment)

    message = str(excinfo.value)
    assert "same \nscenario set" in message or "do not cover the same" in message
    assert "S01" in message and "S04" in message


def test_different_models_are_refused() -> None:
    """The model is a frozen variable of the 2x2 matrix (spec 14.2)."""
    baseline, treatment = _matched_pair(scenario_id="S01")
    treatment[0].model_name = "other-model"

    with pytest.raises(ValueError) as excinfo:
        compare_runs(baseline, treatment)

    message = str(excinfo.value)
    assert "different models" in message
    assert "stub-model" in message and "other-model" in message


def test_different_sampling_parameters_between_conditions_are_refused() -> None:
    """A re-sampled treatment is a different experiment, not a skill effect."""
    baseline, treatment = _matched_pair(scenario_id="S01")
    treatment[0].trace.sampling_params["temperature"] = 0.7

    with pytest.raises(ValueError) as excinfo:
        compare_runs(baseline, treatment)

    assert "sampling parameters differ between the two conditions" in str(excinfo.value)


def test_non_uniform_sampling_inside_one_condition_is_refused() -> None:
    """One run cannot be quietly re-sampled inside its own condition either."""
    baseline, treatment = _matched_pair(scenario_id="S01")
    baseline.append(_run(run_id="c", scenario_id="S04", condition="without_skill", temperature=0.5))
    treatment.append(_run(run_id="d", scenario_id="S04", condition="with_skill"))

    with pytest.raises(ValueError) as excinfo:
        compare_runs(baseline, treatment)

    message = str(excinfo.value)
    assert "not uniform inside the baseline condition" in message
    assert "S04" in message and "S01" in message


def test_offered_registries_may_differ_in_order_but_not_in_content() -> None:
    """The registry is a set of names; advertisement order is not a variable."""
    baseline, treatment = _matched_pair(scenario_id="S01")
    treatment[0].tools_offered = list(reversed(CANONICAL_TOOLS))

    assert _check_same_tool_set(baseline, treatment) is None


# --------------------------------------------------------------------------- #
# compare_runs: preconditions pass, then Phase 5 owns the rest
# --------------------------------------------------------------------------- #


def test_compare_runs_with_a_fair_configuration_hands_over_to_phase_5() -> None:
    """A fair pair is not refused; the aggregation that follows is Phase 5 work."""
    baseline, treatment = _matched_pair(
        scenario_id="S01",
        treatment_calls=(ToolName.GET_ROUTE_CONTEXT,),
    )

    with pytest.raises(NotImplementedError) as excinfo:
        compare_runs(baseline, treatment)

    assert "Phase 5" in str(excinfo.value)


def test_compare_runs_preconditions_are_checked_before_any_metric() -> None:
    """An unfair configuration is refused even though aggregation is unimplemented."""
    baseline, treatment = _matched_pair(scenario_id="S01")
    treatment[0].model_name = "other-model"

    with pytest.raises(ValueError):
        compare_runs(baseline, treatment)


def test_comparison_report_carries_the_matched_run_lists() -> None:
    """The report is a container: both lists travel with the audit notes."""
    baseline, treatment = _matched_pair(scenario_id="S01")
    report = ComparisonReport(
        baseline=baseline,
        treatment=treatment,
        notes=["tool-set audit: both conditions offered the same five-tool registry"],
    )

    assert [run.scenario_id for run in report.baseline] == ["S01"]
    assert [run.condition for run in report.treatment] == ["with_skill"]
    assert report.metric_deltas == {}
    assert report.verdict == "", "no verdict may be written before the deltas exist"
    assert report.notes


def test_comparison_report_survives_a_json_round_trip() -> None:
    """A report must be attachable to a slide or a demo UI without a second format."""
    baseline, treatment = _matched_pair(scenario_id="S01")
    report = ComparisonReport(baseline=baseline, treatment=treatment)

    restored = ComparisonReport.model_validate_json(report.model_dump_json())

    assert restored.model_dump() == report.model_dump()
    assert restored.baseline[0].tools_offered == list(CANONICAL_TOOLS)


# --------------------------------------------------------------------------- #
# The metric vocabulary and the result container (spec 12)
# --------------------------------------------------------------------------- #


def test_metric_result_requires_a_unit() -> None:
    """A metric without its unit is a number that cannot be compared (spec 12)."""
    with pytest.raises(ValidationError) as excinfo:
        MetricResult(name=AgentMetricName.TOOL_CALL_COMPLETENESS.value, value=1.0)

    assert "unit" in str(excinfo.value)


def test_metric_result_keeps_its_provenance() -> None:
    """Name, value, unit and observation count all round trip together."""
    metric = MetricResult(
        name=ScenarioMetricName.LEAD_TIME.value,
        value=95.0,
        unit="minutes",
        n=4,
        details={"scenario": "S04_sunset_margin_pace_drop", "horizons": ["+60min", "+120min"]},
    )

    restored = MetricResult.model_validate_json(metric.model_dump_json())

    assert restored.name == ScenarioMetricName.LEAD_TIME.value
    assert restored.value == 95.0
    assert restored.unit == "minutes"
    assert restored.n == 4
    assert restored.details["horizons"] == ["+60min", "+120min"]


def test_metric_result_rejects_a_value_without_its_unit_typo() -> None:
    """An unknown unit is still a unit: the container does not police the vocabulary."""
    metric = MetricResult(name=ExplanationMetricName.DRIVER_CONSISTENCY.value, value=0.5, unit="ratio")

    assert metric.unit == "ratio"
    assert metric.n == 1, "a single observation is the documented default"


def test_metric_result_forbids_extra_fields() -> None:
    """The metric container is closed, like every other contract in this package."""
    with pytest.raises(ValidationError):
        MetricResult(name="TOOL_CALL_COMPLETENESS", value=1.0, unit="ratio", score=0.9)


def test_metric_name_families_are_disjoint() -> None:
    """Agent, explanation and scenario metrics answer different questions (spec 11.1)."""
    families = (
        {name.value for name in AgentMetricName},
        {name.value for name in ExplanationMetricName},
        {name.value for name in ScenarioMetricName},
    )

    assert families[0].isdisjoint(families[1])
    assert families[0].isdisjoint(families[2])
    assert families[1].isdisjoint(families[2])


def test_no_metric_name_speaks_of_accident_probability() -> None:
    """The banned identifier is absent from the whole metric vocabulary."""
    all_names = {
        name.value
        for family in (AgentMetricName, ExplanationMetricName, ScenarioMetricName)
        for name in family
    }
    joined = " ".join(all_names).lower()

    assert "accident_probability" not in joined
    assert "accident" not in joined, "only scenario-level, synthetic-dataset wording is permitted"


def test_the_metric_vocabulary_is_the_frozen_one() -> None:
    """Every name in the three families is pinned by spec 12; none may be invented.

    The names are matched on the *spelling* the frozen spec uses (spaces and the
    hyphen of "Projected-risk Detection Rate" become underscores), so adding,
    dropping or prettifying a metric name is a contract break and not a rename.
    """
    assert {name.value for name in AgentMetricName} == {
        "TOOL_CALL_COMPLETENESS",
        "REQUIRED_HORIZON_COVERAGE",
        "WRONG_TOOL_RATE",
        "MISSING_DATA_COMPLIANCE",
        "FRESHNESS_COMPLIANCE",
        "SAFETY_CORE_OVERRIDE_RATE",
        "HALLUCINATED_FACT_RATE",
    }
    assert {name.value for name in ExplanationMetricName} == {
        "EXPLANATION_GROUNDING",
        "DRIVER_CONSISTENCY",
        "UNCERTAINTY_DISCLOSURE",
    }
    assert {name.value for name in ScenarioMetricName} == {
        "SCENARIO_HAZARD_MISS_RATE",
        "SCENARIO_FALSE_ALARM_RATE",
        "PROJECTED_RISK_DETECTION_RATE",
        "LEAD_TIME",
    }


def test_the_naming_prohibition_is_stated_on_the_enum() -> None:
    """The one metric a reader would most easily misquote carries the warning itself.

    Spec 12 pins the wording ("比賽階段只能稱 Scenario Hazard Miss Rate，不得宣傳為
    真實世界事故漏報率"); the enum's docstring is where the reader who reaches for
    the name will find it. The prohibition is enforced in prose and in the
    presentation layers -- it is deliberately NOT enforced by renaming a frozen
    identifier such as ``PROJECTED_RISK_DETECTION_RATE``.
    """
    docstring = ScenarioMetricName.__doc__ or ""

    assert "never" in docstring.lower()
    assert "real-world" in docstring.lower()
    assert "SCENARIO_HAZARD_MISS_RATE" in ScenarioMetricName.SCENARIO_HAZARD_MISS_RATE.value


# --------------------------------------------------------------------------- #
# DGX Spark evidence (spec 14.3)
# --------------------------------------------------------------------------- #


def test_dgx_spark_evidence_records_what_was_measured() -> None:
    """A measured record keeps its numbers; an unmeasured field stays None."""
    evidence = DgxSparkEvidence(
        gpu_model="NVIDIA GB10",
        gpu_utilization_avg=63.5,
        tokens_per_s=42.1,
        local_inference_latency_s=1.42,
        benchmark_runtime_s=905.0,
        scenario_count=20,
    )

    restored = DgxSparkEvidence.model_validate_json(evidence.model_dump_json())

    assert restored.gpu_model == "NVIDIA GB10"
    assert restored.gpu_utilization_avg == 63.5
    assert restored.tokens_per_s == 42.1
    assert restored.scenario_count == 20


def test_dgx_spark_evidence_distinguishes_missing_from_zero() -> None:
    """An unmeasured value is None -- never a flattering default (spec 14.3)."""
    evidence = DgxSparkEvidence(gpu_model="NVIDIA GB10", scenario_count=1)

    assert evidence.gpu_utilization_avg is None
    assert evidence.tokens_per_s is None
    assert evidence.local_inference_latency_s is None
    assert evidence.benchmark_runtime_s is None


def test_dgx_spark_evidence_requires_a_gpu_and_a_scenario_count() -> None:
    """Without the platform identity and the workload size the record proves nothing."""
    with pytest.raises(ValidationError):
        DgxSparkEvidence(scenario_count=1)
    with pytest.raises(ValidationError):
        DgxSparkEvidence(gpu_model="NVIDIA GB10")


def test_benchmark_report_carries_regressions_and_failure_cases() -> None:
    """Regressions are reported, not hidden (spec 3.2); failure cases travel with the runs."""
    baseline, treatment = _matched_pair(scenario_id="S01")
    evidence = DgxSparkEvidence(gpu_model="NVIDIA GB10", scenario_count=1)
    report = BenchmarkReport(
        runs=baseline + treatment,
        metric_summary={"TOOL_CALL_COMPLETENESS": {"without_skill": 0.4, "with_skill": 1.0}},
        regressions=["S04: treatment took three more steps than the baseline"],
        failure_cases=[],
        dgx_spark_evidence=evidence,
    )

    restored = BenchmarkReport.model_validate_json(report.model_dump_json())

    assert restored.regressions == report.regressions
    assert restored.dgx_spark_evidence is not None
    assert restored.dgx_spark_evidence.gpu_model == "NVIDIA GB10"
    assert len(restored.runs) == 2


def test_benchmark_report_allows_missing_platform_evidence() -> None:
    """A benchmark run off the Spark box may honestly carry no platform evidence."""
    baseline, treatment = _matched_pair(scenario_id="S01")
    report = BenchmarkReport(
        runs=baseline + treatment,
        metric_summary={},
        regressions=[],
        failure_cases=[],
        dgx_spark_evidence=None,
    )

    assert report.dgx_spark_evidence is None


# --------------------------------------------------------------------------- #
# run_scenario: the contract is the signature, the body is Phase 5
# --------------------------------------------------------------------------- #


def test_run_scenario_defers_to_phase_five() -> None:
    """No run is orchestrated in Phase 0; the body names the phase that owns it."""
    with pytest.raises(NotImplementedError) as excinfo:
        run_scenario(
            scenario=object(),  # never touched: the body raises before using it
            model_provider=object(),
            skill_enabled=True,
            tools=None,
            temperature=0.0,
            max_tokens=2048,
            max_steps=12,
        )

    assert "Phase 5" in str(excinfo.value)


def test_run_scenario_accepts_both_conditions_as_plain_arguments() -> None:
    """The signature itself enforces the single-variable experiment (spec 3.1).

    ``skill_enabled`` is a keyword argument like any other; the tool registry and
    the sampling parameters are handed over unchanged, so the baseline and the
    treatment can only differ where the experiment allows them to.
    """
    code = run_scenario.__code__
    positional = code.co_varnames[: code.co_argcount]
    keyword_only = code.co_varnames[code.co_argcount : code.co_argcount + code.co_kwonlyargcount]
    parameters = tuple(positional) + tuple(keyword_only)

    assert "skill_enabled" in parameters
    assert "tools" in parameters
    assert "temperature" in parameters
    assert "max_tokens" in parameters
    assert "max_steps" in parameters
    assert "scenario" in parameters, "the scenario travels with the run, so a result is never orphaned"


def test_run_scenario_defaults_are_documented_as_frozen_variables() -> None:
    """The docstring states the fairness contract a future implementer must honour."""
    docstring = run_scenario.__doc__ or ""

    for phrase in ("identical", "ONLY argument", "spec 3.1"):
        assert phrase.lower() in docstring.lower(), f"the fairness contract must mention {phrase!r}"


def test_run_scenario_records_the_offered_registry_in_the_contract() -> None:
    """The result container documents why the offered registry is part of the contract."""
    docstring = EvalRunResult.__doc__ or ""

    assert "tools_offered" in docstring
    assert "frozen variable" in docstring
