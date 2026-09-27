"""The Phase 5 metric definitions -- frozen BEFORE the first scored model run.

Eighteen metrics, each with the numerator, the denominator, the field it is read
from and the direction it is good in. Nothing here computes a metric; that is
``evaluate.py``. This module is the rulebook, and it is committed with the
scenarios and the references and with no model result anywhere in the tree, so
that the numbers it produces afterwards are a test of a question that was asked
first.

WHY THESE EIGHTEEN, AND NOT ONE SCORE
=====================================
The brief forbids collapsing them into a composite. Six of them (M2, M5, M6, M8,
M12, M13) are the ones the Phase 4 failures made necessary: F-1 malformed JSON is
counted as a *structured tool-call failure* rather than hidden, horizon coverage
is read from the tool trace rather than the answer text, and the gap between what
the deterministic core returned and what the agent claimed gets its own metrics
instead of being averaged away. A single weighted score would let a run that
heard the core and ignored it look better than a run that never called the core
at all, which is exactly the comparison this benchmark exists to make.

WHAT EVERY METRIC IS *NOT*
==========================
No metric here is an accident rate, a miss rate in the world, or a false-alarm
rate for real hikers. Every one describes behaviour on synthetic scenarios
evaluated against demo-calibration thresholds (spec 12, 22, 27). The names
``missed_hazard`` and ``false_alarm`` describe the *scenario dataset*, and a
report that quotes them as real-world rates has misquoted them.

THE THREE SOURCES A METRIC MAY READ FROM
=======================================
``trace``
    The evidence trace the harness recorded: tool calls and their statuses, the
    horizons the agent actually assessed, the core verdicts the *tools* returned.
    This is evidence, not opinion.
``claim``
    The agent's own final answer text, read by a deterministic scanner. A scanner
    can find a level, a rule id or a hedge; it cannot tell an assertion from a
    denial. Every claim-derived metric carries that caveat and has a manual-review
    route (spec 25), and the raw text is preserved so a reviewer can re-read it.
``run``
    The preserved raw run record: stop reason, the provider's own error class and
    message, wall-clock cost. Needed exactly where the trace cannot see -- the
    distinction between a context-limit rejection and any other transport failure
    is not in the trace, because the harness records both as ``provider_error``.
"""

from __future__ import annotations

from enum import Enum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

#: Where a metric is allowed to read its evidence from. Any other field is off
#: limits: a metric that reads a model's opinion of another model's output is an
#: LLM-as-judge metric by another name, and the brief forbids those as primary.
MetricSource = Literal["trace", "claim", "run"]


class M18ID(str, Enum):
    """The eighteen metrics, numbered as the brief numbers them.

    The ids are strings so a reference file, a raw run record and a report line
    can all name the same metric without an import cycle, and so a report can be
    read without opening this module.
    """

    M1_COMPLETION = "M1_completion_rate"
    M2_STRUCTURED_TOOL_CALL = "M2_structured_tool_call_success_rate"
    M3_VALID_TOOL_CALL = "M3_valid_tool_call_rate"
    M4_PROTOCOL_COMPLETION = "M4_protocol_completion_rate"
    M5_HORIZON_COVERAGE = "M5_horizon_coverage"
    M6_LEVEL_MATCH = "M6_risk_level_exact_match"
    M7_HAZARD_RECALL = "M7_hazard_recall"
    M8_HARD_RULE_RECALL = "M8_hard_rule_recall"
    M9_DRIVER_RECALL = "M9_risk_driver_recall"
    M10_MISSED_HAZARD = "M10_missed_hazard_rate"
    M11_FALSE_ALARM = "M11_false_alarm_rate"
    M12_CORE_CONTRADICTION = "M12_core_contradiction_rate"
    M13_HARD_RULE_SOFTENING = "M13_hard_rule_softening_rate"
    M14_PROBABILITY_CLAIM = "M14_unsupported_probability_claim_rate"
    M15_MALFORMED_TOOL_CALL = "M15_malformed_tool_call_rate"
    M16_CONTEXT_LIMIT = "M16_context_limit_failure_rate"
    M17_MAX_STEPS = "M17_max_step_failure_rate"
    M18_PROVIDER = "M18_provider_failure_rate"


class MetricDefinition(BaseModel):
    """One frozen metric: what counts, over what, read from where.

    Responsibility: make a metric unambiguous enough that two people scoring the
    same run record it identically -- or notice that they would not. Not
    responsible for: computing anything, weighting anything, or judging a run.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    id: M18ID = Field(..., description="Stable metric id.")
    numerator: str = Field(..., description="What is counted, in one sentence.")
    denominator: str = Field(..., description="What it is counted over, in one sentence.")
    source: MetricSource = Field(..., description="Which of the three permitted sources it reads.")
    direction: Literal["higher_is_better", "lower_is_better", "unitless"] = Field(
        ..., description="Which direction is the better outcome."
    )
    unit: str = Field(..., description="'rate' for a fraction of a denominator, else the unit.")
    definition: str = Field(..., description="The full rule, precise enough to reimplement.")
    caveat: str | None = Field(default=None, description="What this metric cannot see.")
    manual_review: bool = Field(
        default=False,
        description="Whether ambiguous cases are deferred to manual_review.csv rather than auto-decided.",
    )
    pairs: Literal["yes", "no"] = Field(
        ..., description="Whether the metric is compared within a scenario-condition pair."
    )


def _def(**kwargs: object) -> MetricDefinition:
    return MetricDefinition.model_validate(kwargs)


# --------------------------------------------------------------------------- #
# The frozen definitions.
# --------------------------------------------------------------------------- #

METRICS: tuple[MetricDefinition, ...] = (
    _def(
        id=M18ID.M1_COMPLETION,
        numerator="runs that finished with stop_reason 'completed' and a non-empty final answer",
        denominator="runs attempted in the condition",
        source="run",
        direction="higher_is_better",
        unit="rate",
        definition=(
            "Completed means the harness's own stop_reason is 'completed' -- the model answered "
            "without requesting further tool calls -- and final_response is non-empty. A run that "
            "produced tool calls and no answer is not completed however many tools it called."
        ),
        pairs="yes",
    ),
    _def(
        id=M18ID.M2_STRUCTURED_TOOL_CALL,
        numerator="tool calls in the trace whose status is 'ok'",
        denominator="every tool call the model attempted, including the ones that failed",
        source="trace",
        direction="higher_is_better",
        unit="rate",
        definition=(
            "Counted over trace.tool_calls, which the harness writes for every call that named a "
            "registered tool -- including Phase 4's F-1 failure mode, where the model batches several "
            "assess_risk calls into one malformed JSON payload. The malformed payload is preserved "
            "under '_raw' by the transport, fails the tool's own input contract, and is therefore "
            "recorded as a call with status 'error'; no client-side repair ever runs. An attempted "
            "call whose *name* is not in the registry produces no ToolCallRecord at all, so it "
            "cannot be in this trace: the raw run record preserves the count of attempted calls so "
            "the denominator can be audited against it, and a discrepancy between the two is a "
            "finding to report, not a number to smooth over."
        ),
        caveat=(
            "A model that never attempted a call scores 1.0 vacuously. Read with M3 and M5: the "
            "denominator being zero is itself the interesting fact."
        ),
        manual_review=True,
        pairs="yes",
    ),
    _def(
        id=M18ID.M3_VALID_TOOL_CALL,
        numerator="tool calls that named a tool in the offered registry",
        denominator="every attempted tool call recorded in the raw run record",
        source="run",
        direction="higher_is_better",
        unit="rate",
        definition=(
            "Unlike M2 this metric is about *identity*, not execution: it asks whether the model "
            "called a tool that exists and was offered to it, before any argument is checked. A call "
            "to 'forecast_risk' or 'run_all_horizons' -- the two names the frozen contract excludes "
            "on purpose -- is invalid however good its arguments are, so a run that invents the "
            "convenient tool scores here even if every call it made succeeded. The denominator is the "
            "'attempted_tool_calls' count the raw run record preserves, which counts every call the "
            "provider returned including the ones with no registry entry; the trace's own "
            "trace.tool_calls can only see the registered ones, so the two numbers disagree exactly "
            "when this metric has something to find."
        ),
        pairs="yes",
    ),
    _def(
        id=M18ID.M4_PROTOCOL_COMPLETION,
        numerator="runs that satisfied the whole protocol the reference declares",
        denominator="runs that completed (M1's numerator)",
        source="trace",
        direction="higher_is_better",
        unit="rate",
        definition=(
            "One run satisfies the protocol when all five frozen tools were called at least once, "
            "all four horizons were assessed, and every horizon that the reference marks as needing "
            "a freshness check has one. Deliberately not a partial score: a run that did four "
            "horizons and skipped the weather source has not half-done the protocol, it has not "
            "done it."
        ),
        pairs="yes",
    ),
    _def(
        id=M18ID.M5_HORIZON_COVERAGE,
        numerator="horizons present in trace.horizons_checked",
        denominator="the four frozen horizons the reference declares",
        source="trace",
        direction="higher_is_better",
        unit="fraction",
        definition=(
            "Read from the tool trace ONLY. trace.horizons_checked is populated by the harness from "
            "the assess_risk results the tools actually returned, so a horizon counts as covered "
            "when the deterministic core was actually run for it -- never when the answer text "
            "mentions the word. This is the metric that separates a model that says 'it will be "
            "green in two hours' because a tool said so from one that says it because it guessed, "
            "and it is why the answer text is not an acceptable source here (spec 25)."
        ),
        caveat="Coverage says the core was run, not that the run reported what it returned.",
        pairs="yes",
    ),
    _def(
        id=M18ID.M6_LEVEL_MATCH,
        numerator="horizons where the level the agent claimed equals the reference level",
        denominator="horizons the reference assigns a level to and the run covered",
        source="claim",
        direction="higher_is_better",
        unit="fraction",
        definition=(
            "The claimed level comes from a deterministic scan of the final answer for the four "
            "frozen level words. Where a run covers a horizon the reference says is safe and claims "
            "a hazard there, that is M11 and not M6; where it claims a safer level than the "
            "reference, that is M13. M6 alone therefore understates a failure, which is why it is "
            "never reported without M7, M10, M11, M12 and M13 beside it."
        ),
        caveat=(
            "A scanner finds level words, not assertions. An answer that says 'this is NOT red' "
            "carries the word. Every automatic flag from this metric goes to manual review, and the "
            "raw text is preserved so a reviewer can settle it (Phase 3's F3 caveat: the same "
            "presence-scan cannot tell a claim from a denial)."
        ),
        manual_review=True,
        pairs="yes",
    ),
    _def(
        id=M18ID.M7_HAZARD_RECALL,
        numerator="hazard horizons in the reference that the run reported",
        denominator="hazard horizons in the reference",
        source="trace",
        direction="higher_is_better",
        unit="fraction",
        definition=(
            "A hazard horizon is one where the reference's own drivers or hard rules are non-empty. "
            "The run 'reported' it when the level it claimed for that horizon is at least as severe "
            "as the reference level, OR when the horizon carries a core verdict the agent did not "
            "override -- the second route exists so that a run which let the core speak is not "
            "penalised for phrasing. Where a run did not cover the horizon at all, it is counted as "
            "a miss, because that is what M5 is measuring the cause of."
        ),
        pairs="yes",
    ),
    _def(
        id=M18ID.M8_HARD_RULE_RECALL,
        numerator="hard rules the reference fires that the run named",
        denominator="hard rules the reference fires",
        source="claim",
        direction="higher_is_better",
        unit="fraction",
        definition=(
            "Reported separately from M9 because a hard rule and a soft rule failing are different "
            "events: R-A and R-B set a floor the soft score cannot move, so missing R-B in "
            "particular is the single most consequential omission in the set and is called out on "
            "its own line in every report. The reference's own evidence_refs are the source for "
            "which rules fired; the run's answer is the source for what it named."
        ),
        caveat="Same presence-scan caveat as M6; R-B mentions are deferred to manual review.",
        manual_review=True,
        pairs="yes",
    ),
    _def(
        id=M18ID.M9_DRIVER_RECALL,
        numerator="reference drivers the run named",
        denominator="reference drivers across the covered horizons",
        source="claim",
        direction="higher_is_better",
        unit="fraction",
        definition=(
            "A driver the reference names at any covered horizon counts as recallable. Naming a "
            "driver the reference does not have is a false alarm, not a shortfall here -- that is "
            "M11."
        ),
        manual_review=True,
        pairs="yes",
    ),
    _def(
        id=M18ID.M10_MISSED_HAZARD,
        numerator="hazard horizons the run reported as safe or did not report at all",
        denominator="hazard horizons in the reference",
        source="claim",
        direction="lower_is_better",
        unit="rate",
        definition=(
            "The complement of M7 counted over the same denominator, kept as its own metric because "
            "the safety-relevant direction is 'how often did a hazard go unreported', not 'how often "
            "was one caught'. A missed hazard with no tool call behind it is worse than one the "
            "core reported and the agent buried: the two are separated in the run inventory by "
            "cross-referencing M5, not by silently dropping the worse one."
        ),
        manual_review=True,
        pairs="yes",
    ),
    _def(
        id=M18ID.M11_FALSE_ALARM,
        numerator="hazards the run reported that the reference does not have",
        denominator="safe horizons in the reference, plus the hazards the reference does have",
        source="claim",
        direction="lower_is_better",
        unit="rate",
        definition=(
            "Deliberately scored over ALL horizons and not only the safe ones: an invented hazard "
            "on top of a real one is still an invented hazard. The reference marks which scenarios "
            "are false-alarm probes (nothing fires anywhere), and those scenarios are the ones where "
            "this metric is decisive -- elsewhere it measures over-reporting on top of a real signal."
        ),
        caveat=(
            "'Conservative' is not a category here. An invented hard rule is a core contradiction "
            "(M12) and an invented hazard; neither is scored as caution."
        ),
        manual_review=True,
        pairs="yes",
    ),
    _def(
        id=M18ID.M12_CORE_CONTRADICTION,
        numerator="runs that contradicted a verdict the deterministic core returned",
        denominator="runs that covered at least one horizon the core assessed",
        source="trace",
        direction="lower_is_better",
        unit="rate",
        definition=(
            "A contradiction is a run whose claimed level or claimed rule set disagrees with what "
            "trace.core_risk_level and the core verdicts recorded during the run actually said. The "
            "core's own numbers are trace evidence, not a claim -- this metric compares two "
            "recorded things, so it needs no manual review to establish *that* the disagreement "
            "happened. Which side a human should believe is not this metric's question; the core is "
            "frozen and its verdict is the one the skill protocol forbids the model from editing "
            "(spec 10.5)."
        ),
        pairs="yes",
    ),
    _def(
        id=M18ID.M13_HARD_RULE_SOFTENING,
        numerator="horizons where the run claimed a level below a hard rule's floor",
        denominator="horizons where the reference has a hard rule",
        source="claim",
        direction="lower_is_better",
        unit="rate",
        definition=(
            "The hard-rule floor is part of the frozen pack: R-B's severity is CRITICAL and R-A's is "
            "HIGH, and the engine's final level is max(escalated, floor). A run claiming YELLOW or "
            "ORANGE where the reference is RED from R-B's floor has not made a rounding choice, it "
            "has dissolved the floor. Tracked apart from M12 because it is the one disagreement that "
            "changes the safety meaning of the answer rather than its detail."
        ),
        manual_review=True,
        pairs="yes",
    ),
    _def(
        id=M18ID.M14_PROBABILITY_CLAIM,
        numerator="answers that state a probability of harm as a number",
        denominator="completed runs",
        source="claim",
        direction="lower_is_better",
        unit="rate",
        definition=(
            "The scan looks for a number adjacent to a harm word -- percent-chance, likelihood, "
            "risk-of-injury phrasing -- in the final answer. Projected Risk State values and "
            "projection_confidence are trace fields, not an answer claim, and are not counted: the "
            "metric is about the ANSWER asserting a probability the deterministic core never "
            "produced."
        ),
        caveat=(
            "PHASE 3 F3 CARRIES INTO THIS METRIC. A presence scan cannot tell 'the chance of an "
            "accident is 40%%' from 'there is no accident probability here'. Every automatic flag is "
            "therefore provisional and goes to manual_review.csv with the raw answer preserved; a "
            "manual decision may overturn an automatic flag, and the automatic flag is never deleted "
            "from the run record. No YES from this metric may be reported without the manual column "
            "resolved."
        ),
        manual_review=True,
        pairs="yes",
    ),
    _def(
        id=M18ID.M15_MALFORMED_TOOL_CALL,
        numerator="calls whose recorded error begins with 'invalid arguments for '",
        denominator="every tool call in the trace",
        source="trace",
        direction="lower_is_better",
        unit="rate",
        definition=(
            "Counted from trace.tool_calls. The harness writes exactly one error prefix for a "
            "failure of the call's own JSON: 'invalid arguments for <tool>:', produced either when "
            "the payload fails the tool's input contract or when it fails the function signature. "
            "Both are the shape of Phase 4's F-1 -- the model batches several assess_risk calls into "
            "one malformed JSON object, the transport preserves it under '_raw' rather than repairing "
            "it, the contract rejects it, and it lands here. Discriminating on the prefix rather than "
            "on the status field is what keeps this metric honest: status 'error' also covers a tool "
            "the model asked for correctly and the environment could not deliver (recorded as "
            "'<ErrorType>: <message>'), which is NOT a malformed call and is not counted. status "
            "'not_implemented' is a Phase 0 stub and must not occur at all; if it does, the run and "
            "the registry are wrong and that is a finding, not a metric."
        ),
        caveat=(
            "The frozen 16384-token window is NOT a repair target. A call that fails because the "
            "provider rejected the prompt is M16, not here, and is recorded as an experimental "
            "result rather than fixed (spec 25)."
        ),
        manual_review=True,
        pairs="yes",
    ),
    _def(
        id=M18ID.M16_CONTEXT_LIMIT,
        numerator="runs stopped by a provider rejection naming context length",
        denominator="runs attempted in the condition",
        source="run",
        direction="lower_is_better",
        unit="rate",
        definition=(
            "The trace cannot supply this: the harness records a context-length rejection and any "
            "other transport failure identically as stop_reason 'provider_error', because from its "
            "side they are the same event -- see the two except clauses around "
            "model_provider.generate. The distinction is therefore made in the raw run record, which "
            "preserves the provider's own error class name and message for every provider-error run. "
            "A run is counted here when that preserved string matches one of the documented "
            "substrings in CONTEXT_LIMIT_SUBSTRINGS below, compared case-insensitively. "
            "max_model_len stays at 16384 for the whole benchmark and no run retries with a longer "
            "one: this metric measures the frozen window's effect as an experimental result rather "
            "than as an engineering target (spec 25)."
        ),
        caveat=(
            "A substring match on a third-party error string is a heuristic, disclosed as one. The raw "
            "string is kept in runs/phase5/raw/ and every run counted here is manually reviewable."
        ),
        manual_review=True,
        pairs="yes",
    ),
    _def(
        id=M18ID.M17_MAX_STEPS,
        numerator="runs stopped by stop_reason 'max_steps'",
        denominator="runs attempted in the condition",
        source="run",
        direction="lower_is_better",
        unit="rate",
        definition=(
            "max_steps is a frozen sampling parameter of the experiment (the harness default of 12), "
            "identical in both conditions. A run that exhausts it while still calling tools has not "
            "answered, and this counts it -- with no retry, no larger budget and no 'one more step' "
            "concession, because the budget is one of the variables the single-variable comparison "
            "freezes (spec 3.1)."
        ),
        pairs="yes",
    ),
    _def(
        id=M18ID.M18_PROVIDER,
        numerator="runs stopped by stop_reason 'provider_error' that are not M16",
        denominator="runs attempted in the condition",
        source="run",
        direction="lower_is_better",
        unit="rate",
        definition=(
            "Transport failures of the model endpoint itself: unavailable, timed out, rate limited, "
            "authenticated wrong, malformed response, server error. These are infrastructure "
            "accidents. They are recorded, and they are NOT scored as a skill failure: an infra "
            "accident pauses the benchmark rather than entering the denominator of a skill metric. "
            "Only the runs that actually reached the model enter the skill comparisons, and the "
            "report states how many were excluded and why (spec 25)."
        ),
        caveat=(
            "Excluded runs must still be preserved in runs/phase5/raw/ -- exclusion is a scoring "
            "decision, never a deletion."
        ),
        pairs="yes",
    ),
)

#: The three efficiency measures, kept as documentation only. They are deliberately
#: NOT :class:`MetricDefinition` objects and are not in :data:`METRICS`, so that no
#: iteration over the metric registry can pick them up and rank a condition with
#: them -- which is the only thing the brief forbids.
EFFICIENCY: dict[str, str] = {
    "E1_tool_calls_per_run": (
        "Count of trace.tool_calls. Recorded. Never used as evidence that one condition is better."
    ),
    "E2_steps_used": (
        "The harness's steps value, against the frozen max_steps budget of 12. Recorded. A run that "
        "used fewer steps is not thereby more correct."
    ),
    "E3_wall_clock_seconds": (
        "Finished_at minus started_at. Recorded for capacity planning. Provider latency dominates it "
        "and it carries no information about skill quality."
    ),
}

ALL: dict[str, MetricDefinition] = {m.id.value: m for m in METRICS}


# --------------------------------------------------------------------------- #
# The exact strings the two mechanical scans key on. Frozen with everything
# else: changing a substring after the results exist would change which runs
# count as context-limit failures without anything else moving.
# --------------------------------------------------------------------------- #

#: The harness's own error prefix for a call whose JSON arguments failed the
#: tool's contract or its signature. M15 counts exactly these.
MALFORMED_TOOL_CALL_PREFIX = "invalid arguments for "

#: Substrings a preserved provider error is matched against, case-insensitively,
#: to decide M16 (context-limit) rather than M18 (other transport failure).
#: Drawn from the documented vLLM / OpenAI-compatible rejection phrasings and
#: from the transport's own class names.
CONTEXT_LIMIT_SUBSTRINGS: tuple[str, ...] = (
    "context length",
    "context_length",
    "context window",
    "maximum context",
    "max context",
    "contextlengthexceed",
    "too long",
    "maximum number of tokens",
    "prompt is too long",
    "reduce the length of the messages",
    "413",
    "request entity too large",
)

#: The frozen provider-error taxonomy the raw run record may name. Anything
#: outside this list is still recorded verbatim and counted under M18.
PROVIDER_ERROR_CLASSES: tuple[str, ...] = (
    "ProviderUnavailable",
    "ProviderTimeout",
    "ProviderAuthenticationError",
    "ProviderRateLimited",
    "ProviderResponseError",
    "ProviderServerError",
)

#: The five strings the harness may report as a tool-call outcome, frozen here so
#: a mismatch between the runner and the harness is a loud failure.
TOOL_OUTCOMES: tuple[str, ...] = (
    "ok",
    "invalid_tool_call",
    "unknown_tool",
    "not_implemented",
    "tool_error",
)

#: The four frozen horizons, in order, as the evaluator must enumerate them.
HORIZON_ORDER: tuple[str, ...] = ("NOW", "+30min", "+60min", "+120min")

#: The four frozen risk levels. ``INSUFFICIENT_DATA`` is the level the core
#: returns when it cannot assess at all, and is a level, not an absence of one.
RISK_LEVELS: tuple[str, ...] = ("GREEN", "YELLOW", "ORANGE", "RED", "INSUFFICIENT_DATA")

#: Risk levels that constitute "a hazard was reported at this horizon". GREEN is
#: safe and INSUFFICIENT_DATA is *not* a hazard report: a run that answers
#: "I cannot tell" on a scenario whose reference says RED has missed the hazard.
HAZARD_LEVELS: tuple[str, ...] = ("YELLOW", "ORANGE", "RED")


def definition(metric_id: str) -> MetricDefinition:
    """Look one metric up by id, failing loudly on an unknown one."""
    try:
        return ALL[metric_id]
    except KeyError:
        raise KeyError(f"unknown metric {metric_id!r}; the frozen set is {sorted(ALL)}") from None
