"""Baseline vs treatment comparison contract (spec sections 3.1, 3.2, 11).

The comparison layer turns two matched lists of :class:`EvalRunResult` (one
baseline, one with-skill) into a report. Two things are implemented in
Phase 0:

1. the FAIRNESS preconditions, checked for real before any metric is looked at;
2. nothing else -- metric aggregation and the verdict raise
   ``NotImplementedError``.

Why the preconditions are checked first (spec 3.1, 3.2)
========================================================
The only permitted difference between the two conditions is the presence of the
SKILL.md text in the system prompt. If the scenario set, the model, the tool
registry or the sampling parameters differ, then any measured delta is a
configuration artefact rather than a skill effect, and the comparison must be
refused rather than reported.

Note on the tool registry specifically: what is compared is the registry each
run was *offered* (``EvalRunResult.tools_offered``), never the tools it called.
A treatment run calling more tools than a baseline run is the effect being
measured, not a fairness violation. Spec 3.2 also requires that the benchmark not
assume the skill is better: this module raises instead of guessing, so a
mis-configured run can never be dressed up as a verdict.
"""

from pydantic import BaseModel, ConfigDict, Field

from trailguard.schemas.trace import ToolCallRecord

from .runner import EvalRunResult

__all__ = ["ComparisonReport", "compare_runs"]


class ComparisonReport(BaseModel):
    """Aggregated baseline-vs-treatment comparison over one scenario set.

    Responsibility: hold the two matched run lists, the per-metric deltas, a
    human-readable verdict and reviewer notes, so a claim about the skill's
    effect is always backed by the runs that produced it. Not responsible for:
    computing the deltas (Phase 5), or deciding that the skill is better --
    spec 3.2 forbids pre-supposing the outcome.
    """

    model_config = ConfigDict(extra="forbid")

    baseline: list[EvalRunResult] = Field(..., description="Runs with skill_enabled=False, one per scenario.")
    treatment: list[EvalRunResult] = Field(..., description="Runs with skill_enabled=True, one per scenario.")
    metric_deltas: dict = Field(
        default_factory=dict,
        description="Metric name -> delta (treatment minus baseline), plus per-scenario detail.",
    )
    verdict: str = Field(default="", description="Human-readable conclusion, written only from the measured deltas.")
    notes: list[str] = Field(
        default=[],
        description="Caveats, fairness audit results and anything a reader must know before quoting the delta.",
    )


def _scenario_ids(runs: list[EvalRunResult]) -> set[str]:
    """Scenario identifiers covered by ``runs`` (duplicates collapse)."""
    return {run.scenario_id for run in runs}


def _model_names(runs: list[EvalRunResult]) -> set[str]:
    """Model identifiers recorded by ``runs``."""
    return {run.model_name for run in runs}


def _offered_tool_names(runs: list[EvalRunResult]) -> set[str]:
    """Distinct tool names the runs in ``runs`` were offered by the harness.

    Responsibility: expose the configuration variable spec 3.1 freezes -- the
    registry the caller handed to ``run_scenario``/``run_agent`` -- so the audit
    compares what the model was *given* rather than what it chose to use.
    Not responsible for: reconstructing a registry that was not recorded; an
    empty result means "not recorded", which the audit reports as such.
    """
    names: set[str] = set()
    for run in runs:
        names.update(run.tools_offered or [])
    return names


def _observed_tool_names(runs: list[EvalRunResult]) -> set[str]:
    """Distinct tool names appearing in the evidence traces of ``runs``.

    Responsibility: give the integrity cross-check in :func:`_check_same_tool_set`
    something the trace itself states. Not responsible for: being compared
    between conditions -- a treatment run legitimately calls tools the baseline
    skipped, so that difference is the measured effect, not a configuration
    violation.
    """
    names: set[str] = set()
    for run in runs:
        for call in run.trace.tool_calls:
            names.add(_recorded_tool_name(call))
    return names


def _recorded_tool_name(call: ToolCallRecord) -> str:
    """Tool name of a recorded call as a plain string."""
    tool = call.tool
    return tool.value if hasattr(tool, "value") else str(tool)


def _sampling_params(run: EvalRunResult) -> dict:
    """Sampling parameters recorded in a run's trace (temperature/max_tokens/max_steps)."""
    return dict(run.trace.sampling_params or {})


def _check_same_scenarios(baseline: list[EvalRunResult], treatment: list[EvalRunResult]) -> None:
    """Both conditions must cover exactly the same scenario identifiers (spec 3.1)."""
    baseline_ids = _scenario_ids(baseline)
    treatment_ids = _scenario_ids(treatment)
    if baseline_ids != treatment_ids:
        missing_in_treatment = sorted(baseline_ids - treatment_ids)
        missing_in_baseline = sorted(treatment_ids - baseline_ids)
        raise ValueError(
            "fairness precondition violated (spec 3.1): the two conditions do not cover the same "
            f"scenario set -- only in baseline: {missing_in_treatment}; only in treatment: "
            f"{missing_in_baseline}. Each scenario must be replayed in both conditions."
        )


def _check_same_model(baseline: list[EvalRunResult], treatment: list[EvalRunResult]) -> None:
    """Both conditions must be produced by the same model (spec 14.2, 3.1)."""
    models = _model_names(baseline) | _model_names(treatment)
    if len(models) > 1:
        baseline_models = sorted(_model_names(baseline))
        treatment_models = sorted(_model_names(treatment))
        raise ValueError(
            "fairness precondition violated (spec 3.1): the two conditions were produced by "
            f"different models -- baseline: {baseline_models}; treatment: {treatment_models}. "
            "The model is a frozen variable of the frozen 2-models x 2-conditions matrix."
        )


def _check_same_tool_set(baseline: list[EvalRunResult], treatment: list[EvalRunResult]) -> None:
    """Audit that both conditions were offered the same tool registry (spec 3.1, 4.1).

    What is compared: :attr:`EvalRunResult.tools_offered`, the registry the
    harness advertised to the model in each run. That is the frozen variable,
    and it is the only thing this audit looks at.

    What is deliberately NOT compared: the tools the runs actually called. The
    with-skill condition is *supposed* to call tools the baseline skipped -- that
    difference is the effect under measurement, and treating it as a
    configuration violation would make the benchmark refuse every legitimate
    result. An earlier revision compared the observed call sets instead, which
    had two failure modes: it fired on the very signal the benchmark exists to
    measure, and it passed two runs that had called nothing at all (two empty
    sets are equal, so a pair of tool-less runs looked perfectly fair).

    An unrecorded registry is a hard error rather than a skip: "nothing was
    recorded, so nothing can be compared" is precisely the silent pass this
    module exists to prevent.
    """
    baseline_offered = _offered_tool_names(baseline)
    treatment_offered = _offered_tool_names(treatment)

    if not baseline_offered and not treatment_offered:
        raise ValueError(
            "fairness precondition cannot be audited (spec 3.1): neither condition recorded the tool "
            "registry it was offered. EvalRunResult.tools_offered must be filled from the registry "
            "handed to run_scenario(tools=...) before a comparison may be attempted; skipping the "
            "tool-set check would let a registry difference pass as a skill effect."
        )

    if bool(baseline_offered) != bool(treatment_offered):
        recorded = "baseline" if baseline_offered else "treatment"
        missing = "treatment" if baseline_offered else "baseline"
        raise ValueError(
            "fairness precondition violated (spec 3.1): only one condition recorded its offered tool "
            f"registry ({recorded} did, {missing} did not) -- baseline: {sorted(baseline_offered)}; "
            f"treatment: {sorted(treatment_offered)}. Both conditions must report the registry they "
            "were given."
        )

    if baseline_offered != treatment_offered:
        only_baseline = sorted(baseline_offered - treatment_offered)
        only_treatment = sorted(treatment_offered - baseline_offered)
        raise ValueError(
            "fairness precondition violated (spec 3.1): the two conditions were offered different tool "
            f"sets -- only in baseline: {only_baseline}; only in treatment: {only_treatment}. "
            "The tool registry is a frozen variable; pass the same tools to run_scenario in both conditions."
        )

    # Integrity cross-check: every tool a run recorded calling must be one that
    # run was offered. The harness can only record registered tools, so a
    # mismatch means the trace and the registry came from different runs.
    for label, runs in (("baseline", baseline), ("treatment", treatment)):
        offered = _offered_tool_names(runs)
        if not offered:
            continue  # nothing was advertised: nothing to cross-check against
        unoffered = sorted(_observed_tool_names(runs) - offered)
        if unoffered:
            raise ValueError(
                "fairness precondition violated (spec 3.1): the "
                f"{label} traces record tool calls that were never offered to the model: {unoffered}. "
                "A trace and its tools_offered registry must describe the same run."
            )


def _check_same_sampling_params(baseline: list[EvalRunResult], treatment: list[EvalRunResult]) -> None:
    """Identical sampling parameters everywhere (spec 3.1).

    Two levels: the parameters must be uniform *inside* each condition (so one
    run cannot be quietly re-sampled), and the two conditions must agree with
    each other for every shared scenario.
    """
    for label, runs in (("baseline", baseline), ("treatment", treatment)):
        reference: dict | None = None
        reference_id: str | None = None
        for run in runs:
            params = _sampling_params(run)
            if reference is None:
                reference, reference_id = params, run.scenario_id
                continue
            if params != reference:
                differing = sorted(
                    key
                    for key in set(params) | set(reference)
                    if params.get(key) != reference.get(key)
                )
                raise ValueError(
                    "fairness precondition violated (spec 3.1): sampling parameters are not uniform "
                    f"inside the {label} condition -- scenario {run.scenario_id!r} differs from "
                    f"scenario {reference_id!r} on {differing}. Every run must be sampled the same way."
                )

    baseline_by_id = {run.scenario_id: _sampling_params(run) for run in baseline}
    for run in treatment:
        other = baseline_by_id.get(run.scenario_id)
        if other is None:  # pragma: no cover - guarded by _check_same_scenarios
            continue
        params = _sampling_params(run)
        if params != other:
            differing = sorted(key for key in set(params) | set(other) if params.get(key) != other.get(key))
            raise ValueError(
                "fairness precondition violated (spec 3.1): sampling parameters differ between the two "
                f"conditions for scenario {run.scenario_id!r} -- baseline: {other}; treatment: {params}; "
                f"differing keys: {differing}."
            )


def compare_runs(baseline: list[EvalRunResult], treatment: list[EvalRunResult]) -> ComparisonReport:
    """Compare a baseline run list with a with-skill run list over one scenario set.

    Responsibility: enforce the fairness preconditions of spec 3.1 -- same
    scenario identifier set, same model, same tool set, same sampling
    parameters -- and raise ``ValueError`` naming the violation as soon as one is
    broken, so a configuration difference can never be reported as a skill
    effect. Then hand the matched runs to the Phase 5 aggregator.

    Not responsible for: deciding that the skill is better (spec 3.2 explicitly
    forbids pre-supposing the outcome), computing any metric value, or
    re-running failed episodes.

    Args:
        baseline: Runs with ``skill_enabled=False``.
        treatment: Runs with ``skill_enabled=True``.

    Returns:
        A :class:`ComparisonReport` with the run lists, per-metric deltas and a
        verdict written only from the measured deltas.

    Raises:
        ValueError: A fairness precondition is violated; the message names the
            violated precondition and the offending values.
        NotImplementedError: always, in Phase 0 -- metric aggregation, delta
            computation and verdict wording are Phase 5 work (spec 25).
    """
    _check_same_scenarios(baseline, treatment)
    _check_same_model(baseline, treatment)
    _check_same_tool_set(baseline, treatment)
    _check_same_sampling_params(baseline, treatment)

    raise NotImplementedError(
        "Phase 5: metric aggregation -- compute per-metric baseline/treatment deltas, per-scenario "
        "regressions and the verdict from the matched EvalRunResult lists (spec 11, 12)"
    )
