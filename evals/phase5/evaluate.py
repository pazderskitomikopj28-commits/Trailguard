"""The Phase 5 evaluator: M1..M18 computed from a preserved run and its reference.

WHAT THIS MODULE IS ALLOWED TO READ
===================================
Exactly three things, and the brief is specific about all of them:

* the run's **evidence trace** -- which tools the model actually called, what
  statuses the harness recorded, which horizons the deterministic core was run
  for, and the verdicts the tools themselves returned;
* the run's **final answer text** -- read by a deterministic scanner that can
  find a level word, a rule id and a hedge, and cannot tell an assertion from a
  denial;
* the run's **raw record** -- stop reason, the provider's error class, the
  attempted-call count, wall-clock cost.

It does not ask a model what it thinks of a run. ``manual_review.csv`` is where
ambiguity goes, and the original trace is never edited to resolve it.

WHAT THE WARM-UP CHANGED IN THE DESIGN
======================================
Two facts, both measured rather than assumed, and both recorded here because they
change what a metric means:

1. A run can end with ``stop_reason='completed'`` and a final answer that is a
   **tool call emitted as text**. The harness only reads structured
   ``response.tool_calls``; a model that writes ``{"name": "assess_risk", ...}``
   into the content channel gets no tool execution and its run ends there. That
   is the Phase 4 F-1 family, seen end-on-end. M1 therefore requires that the
   answer be an answer: a run whose entire response is a tool-call invocation has
   not completed anything, and counting it as a completion would let the
   benchmark measure the harness's tolerance of a formatting failure as if it
   were an assessment. The classifier is in :func:`answer_is_a_tool_call` and it
   *classifies only* -- it never parses the arguments, never repairs them and
   never feeds them to a tool.

2. Rule ids and driver ids in an answer are found by regex, so a run that says
   "future weather hazard" in words rather than "R-C" scores as a miss on M8/M9.
   Both metrics are therefore **lower bounds**, and the report says so.
"""

from __future__ import annotations

import argparse
import csv
import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Sequence

from evals.phase5.manifest import REPO_ROOT
from evals.phase5.metrics import (
    CONTEXT_LIMIT_SUBSTRINGS,
    HAZARD_LEVELS,
    HORIZON_ORDER,
    MALFORMED_TOOL_CALL_PREFIX,
    METRICS,
    RISK_LEVELS,
    definition,
)

RAW_DIR = REPO_ROOT / "runs" / "phase5" / "raw"
MANUAL_REVIEW_PATH = REPO_ROOT / "runs" / "phase5" / "manual_review.csv"
SUMMARY_PATH = REPO_ROOT / "runs" / "phase5" / "summary.csv"
PAIRED_PATH = REPO_ROOT / "runs" / "phase5" / "paired_results.csv"
METRICS_PATH = REPO_ROOT / "runs" / "phase5" / "metrics.json"

CONDITIONS = ("without_skill", "with_skill")

# --------------------------------------------------------------------------
# The frozen answer scanner.
# --------------------------------------------------------------------------

#: How each horizon may be referred to. The frozen labels are the canonical ones;
#: the others are what a model actually writes, taken from the runs this module
#: was calibrated against rather than guessed at.
HORIZON_ALIASES: dict[str, tuple[str, ...]] = {
    "NOW": ("now", "current time", "at risk now", "t=0", "0 min"),
    "+30min": ("+30min", "+30 min", "30 minutes", "30-minute", "in 30 min"),
    "+60min": ("+60min", "+60 min", "60 minutes", "60-minute", "in 60 min"),
    "+120min": ("+120min", "+120 min", "120 minutes", "120-minute", "two hours", "in 2 hours"),
}

#: The four frozen levels, plus INSUFFICIENT_DATA, matched on word boundaries.
_LEVEL_PATTERN = re.compile(
    r"\b(GREEN|YELLOW|ORANGE|RED|INSUFFICIENT_DATA|INSUFFICIENT DATA)\b", re.IGNORECASE
)

#: A rule id, e.g. ``R-C``. The frozen pack is R-A..R-F; an id outside that range
#: is not a rule, so it is not matched. Case-insensitive because the model is
#: entitled to write ``r-c`` and the frozen pack is not case-sensitive.
_RULE_PATTERN = re.compile(r"\bR-([A-F])\b", re.IGNORECASE)

#: Words that turn a level mention into a denial or a hedge. Used to flag a claim
#: as ambiguous for manual review, never to rewrite it.
_NEGATION_WINDOW = 40
_NEGATIONS = ("not ", "no ", "isn't", "is not", "never", "cannot", "neither", "without ")
_HEDGES = (
    "might", "may be", "could be", "possibly", "probably", "unclear", "uncertain",
    "cannot determine", "unable to", "not computable", "unknown",
)

#: Harm words that could carry a probability, and the number that would carry it.
_HARM_WORD = re.compile(r"\b(chance|probability|likelihood|risk of|odds)\b", re.IGNORECASE)
_NUMBER_PATTERN = re.compile(r"\d+(?:\.\d+)?\s*(?:%|percent|per cent)", re.IGNORECASE)
_PROBABILITY_WINDOW = 40

#: Aliases that are ordinary English words as well as horizon names. Matching one
#: mid-sentence ("we now turn to the gusts") would attribute the next level to the
#: wrong horizon and silently corrupt M6, M7, M10, M11, M13 and M14. Such an alias
#: only counts as a horizon heading when both signals agree: it does not follow a
#: word, and a colon or a level word sits just after it. The literal ``+30min``
#: forms need neither -- nothing in prose spells them.
_BARE_ALIASES: frozenset[str] = frozenset({
    "now", "current time", "at risk now", "t=0", "0 min",
    "30 minutes", "60 minutes", "30-minute", "60-minute", "120-minute", "two hours",
})
_HEADING_SPAN = 60
#: Markdown furniture a heading may sit behind: bold, heading marks, bullets,
#: table pipes, leading digits, a list bracket.
_LINE_PREFIX = re.compile(r"^[\s>*_#\-|:0-9\[\]]*$")
#: Mid-sentence, the only safe reading of a bare alias is an explicit label --
#: "NOW:" with nothing but punctuation between. "it is now RED" must not be one.
_INLINE_LABEL = re.compile(r"[^\w\n]{0,8}:")

#: Shapes whose only content is a tool-call invocation. A response that begins
#: with one of these asked for a tool and got none, and is not an answer. The
#: tags cover both the OpenAI/``<tool_call>`` spelling and the Anthropic-style
#: ``<tool_use>`` spelling, because a malformed batch is a result whichever
#: dialect the model reached for.
_TOOL_CALL_START = re.compile(
    r"""^\s*(\{\s*"name"\s*:|\{\s*'name'\s*:|\[\s*\{\s*"name"\s*:"""
    r"""|<tool_call|<tool_use|<function_call"""
    r"""|```(?:json|tool_code)?\s*\n\s*\{\s*"name")""",
    re.IGNORECASE,
)
_TOOL_CALL_TAG = re.compile(r"</?(?:tool_call|tool_use|function_call)", re.IGNORECASE)


def normalise_level(text: str) -> str:
    """Map a stray ``INSUFFICIENT DATA`` spelling onto the frozen enum spelling."""
    return text.strip().upper().replace(" ", "_")


def answer_is_a_tool_call(answer: str) -> bool:
    """True when the response is a tool-call invocation rather than an answer.

    Classification only. The text is checked for *shape* -- it begins with a
    JSON object carrying a ``name`` key, an XML-ish ``<tool_call>`` tag, or a fenced
    block holding such an object. It is never parsed for arguments, never repaired
    and never handed to a tool: that is the boundary the Phase 5 rules draw
    between measuring a failure and rescuing it.
    """
    if not answer or not answer.strip():
        return False
    return bool(_TOOL_CALL_START.search(answer) or _TOOL_CALL_TAG.search(answer[:200]))


@dataclass
class ClaimReport:
    """What the answer text says, as far as a deterministic scanner can see."""

    #: horizon -> the risk level claimed for it, when the text gave one.
    levels: dict[str, str] = field(default_factory=dict)
    #: horizons whose claim was contradicted by a second mention of the same horizon.
    conflicting: list[str] = field(default_factory=list)
    #: horizons whose claim was preceded by a negation ("not RED", "no hazard").
    negated: list[str] = field(default_factory=list)
    #: hedge phrases found anywhere in the answer.
    hedges: list[str] = field(default_factory=list)
    #: rule ids the answer names.
    rules: set[str] = field(default_factory=set)
    #: horizons whose claim came with a hedge nearby.
    hedged_levels: list[str] = field(default_factory=list)
    #: numbers presented as a probability of harm.
    probability_claims: list[str] = field(default_factory=list)
    #: horizons the answer mentions at all, whether or not it gave them a level.
    mentions: set[str] = field(default_factory=set)

    def as_dict(self) -> dict[str, Any]:
        return {
            "levels": dict(self.levels),
            "conflicting": list(self.conflicting),
            "negated": list(self.negated),
            "hedges": list(self.hedges),
            "rules": sorted(self.rules),
            "hedged_levels": list(self.hedged_levels),
            "probability_claims": list(self.probability_claims),
            "mentions": sorted(self.mentions),
        }


def _alias_positions(text: str) -> list[tuple[int, str]]:
    """Every horizon mention, in document order, with the horizon it names."""
    low = text.lower()
    found: list[tuple[int, str]] = []
    for horizon, aliases in HORIZON_ALIASES.items():
        for alias in aliases:
            start = 0
            needle = alias.lower()
            while True:
                index = low.find(needle, start)
                if index < 0:
                    break
                if alias in _BARE_ALIASES and not _reads_as_a_heading(text, index, len(needle)):
                    # "we now turn to the gusts" is not the NOW heading.
                    start = index + max(len(needle), 1)
                    continue
                found.append((index, horizon))
                start = index + max(len(needle), 1)
    return sorted(found)


def _reads_as_a_heading(text: str, index: int, length: int) -> bool:
    """True when a bare alias is being used as a horizon heading.

    A heading opens a line -- bold, a bullet, a number, or the start of one -- and
    carries a colon or a level word within ``_HEADING_SPAN`` characters. A bare
    alias in the middle of a sentence is a heading only when it is written as a
    label (``And again: NOW: RED``); "the gusts ease and it is now RED" is prose
    about the next horizon and must not be read as a claim about NOW. Both operands
    are checked, and the cost of being wrong is asymmetric: a missed heading makes
    a metric vacuous and says so, a spurious one corrupts it silently.
    """
    line_start = text.rfind("\n", 0, index) + 1
    after = text[index + length:index + length + _HEADING_SPAN]
    if not _LINE_PREFIX.match(text[line_start:index]):
        return _INLINE_LABEL.match(after) is not None
    return ":" in after or _LEVEL_PATTERN.search(after) is not None


def scan_answer(answer: str) -> ClaimReport:
    """Read the answer text. Deterministic, and honest about its limits."""
    report = ClaimReport()
    if not answer or not answer.strip():
        return report

    report.rules = {
        f"R-{match.group(1).upper()}" for match in _RULE_PATTERN.finditer(answer)
    }
    low = answer.lower()
    report.hedges = sorted({hedge for hedge in _HEDGES if hedge in low})

    positions = _alias_positions(answer)
    report.mentions = {horizon for _, horizon in positions}

    # Bound each horizon's claim by the next mention of ANY horizon, so a level
    # belonging to the next section is not attributed to this one.
    claims: dict[str, list[tuple[str, bool, bool]]] = {horizon: [] for horizon in HORIZON_ORDER}
    for index, (position, horizon) in enumerate(positions):
        end = positions[index + 1][0] if index + 1 < len(positions) else len(answer)
        window = answer[position:end]
        match = _LEVEL_PATTERN.search(window)
        if match is None:
            continue
        level = normalise_level(match.group(1))
        # Only the words immediately before the level count as hedging or denying
        # it. A wider window reads a report's confidence disclaimers ("projection
        # confidence: not computable") as hedging the level, which would flood the
        # manual-review queue with ambiguity that is not there.
        immediate = window[max(0, match.start() - _NEGATION_WINDOW): match.start()].lower()
        negated = any(negation in immediate for negation in _NEGATIONS)
        hedged = any(hedge in immediate for hedge in _HEDGES)
        claims[horizon].append((level, negated, hedged))

    for horizon in HORIZON_ORDER:
        hori_claims = claims[horizon]
        if not hori_claims:
            continue
        distinct = {level for level, _, _ in hori_claims}
        if len(distinct) > 1:
            report.conflicting.append(horizon)
        # The last mention wins: a run that gives a level per horizon and then a
        # summary paragraph is most likely restating, not revising -- and where it
        # is revising, the conflict is flagged above and goes to manual review.
        level, negated, hedged = hori_claims[-1]
        report.levels[horizon] = level
        if negated:
            report.negated.append(horizon)
        if hedged:
            report.hedged_levels.append(horizon)

    # A probability of harm: a number adjacent to a harm word, within a short
    # span. This is written as two windows rather than one pattern with an
    # optional numeric group, because an optional group is satisfied by matching
    # nothing: "There is a 40% chance of an accident" would let the gap swallow
    # the 40% and the phrase would never be counted. The frozen numerator says
    # "as a number", so a bare "high likelihood" is a strength word, not a
    # probability, and is not matched.
    for match in _HARM_WORD.finditer(answer):
        before = answer[max(0, match.start() - _PROBABILITY_WINDOW):match.start()]
        after = answer[match.end():match.end() + _PROBABILITY_WINDOW]
        number_before = _NUMBER_PATTERN.search(before)
        number_after = _NUMBER_PATTERN.search(after)
        if number_before is None and number_after is None:
            continue
        window_start = max(0, match.start() - _PROBABILITY_WINDOW)
        span_start = window_start + (number_before.start() if number_before else _PROBABILITY_WINDOW)
        span_end = match.end() + (number_after.end() if number_after else 0)
        report.probability_claims.append(answer[span_start:span_end].strip()[:80])

    return report


# --------------------------------------------------------------------------
# Scoring one run.
# --------------------------------------------------------------------------


def _reference_for(scenario_id: str, references_dir: Path) -> dict[str, Any]:
    path = Path(references_dir) / f"{scenario_id}.json"
    if not path.is_file():
        raise FileNotFoundError(f"no reference for {scenario_id!r} at {path}")
    return json.loads(path.read_text(encoding="utf-8"))


def _reference_levels(reference: dict[str, Any]) -> dict[str, str]:
    return {label: reference["horizons"][label]["risk"]["level"] for label in HORIZON_ORDER}


def _reference_rules(reference: dict[str, Any]) -> dict[str, set[str]]:
    out = {label: set() for label in HORIZON_ORDER}
    for label in HORIZON_ORDER:
        out[label] = set(reference["horizons"][label]["risk"]["hard_rules_triggered"])
    return out


def _reference_drivers(reference: dict[str, Any]) -> dict[str, set[str]]:
    out = {label: set() for label in HORIZON_ORDER}
    for label in HORIZON_ORDER:
        out[label] = {d["rule_id"] for d in reference["horizons"][label]["risk"]["drivers"]}
    return out


@dataclass
class RunScore:
    """The eighteen metrics for one run, plus the flags a human must settle."""

    run_id: str
    scenario_id: str
    condition: str
    repetition: int
    scored: bool
    exclusion_reason: str | None
    values: dict[str, float | None]
    evidence: dict[str, Any]
    flags: list[dict[str, str]] = field(default_factory=list)

    def as_row(self) -> dict[str, Any]:
        row: dict[str, Any] = {
            "run_id": self.run_id,
            "scenario_id": self.scenario_id,
            "condition": self.condition,
            "repetition": self.repetition,
            "scored": self.scored,
        }
        if self.exclusion_reason:
            row["exclusion_reason"] = self.exclusion_reason
        for metric in METRICS:
            value = self.values.get(metric.id.value)
            row[metric.id.value] = "" if value is None else f"{value:.6f}"
        return row


def _rate(numerator: int, denominator: int) -> float | None:
    """A rate, or None when the denominator is zero -- never 0, never 1."""
    return None if denominator == 0 else numerator / denominator


def score_run(
    run: dict[str, Any],
    *,
    reference: dict[str, Any],
    references_dir: Path | None = None,
    include_unscored: bool = False,
) -> RunScore:
    """Score one preserved run record against one reference.

    ``include_unscored`` is for validating this evaluator against the warm-up
    traces, which are recorded with ``scored: false`` and excluded from every real
    aggregation. It changes none of the numbers -- it only lets a run the ``scored``
    flag protects be measured at all. The batch path leaves it False.
    """
    trace = run["trace"]
    stop_reason = run["stop_reason"]
    final_answer = run.get("final_response", "") or ""
    claim = scan_answer(final_answer)
    tool_calls = trace.get("tool_calls", [])
    horizons_checked = list(trace.get("horizons_checked", []))
    provider_error = run.get("provider_error")

    def flag(metric: str, automatic_flag: bool, reason: str) -> None:
        if automatic_flag:
            flags.append(
                {
                    "run_id": run["run_id"],
                    "metric": metric,
                    "automatic_flag": "yes",
                    "manual_decision": "",
                    "short_reason": reason,
                }
            )

    flags: list[dict[str, str]] = []

    # --- M18 / M16 first: an infra accident is not a skill failure ------------- #
    provider_class = (provider_error or {}).get("class_name")
    provider_message = ((provider_error or {}).get("message") or "").lower()
    is_context_limit = any(sub in provider_message for sub in CONTEXT_LIMIT_SUBSTRINGS)
    infra_accident = stop_reason == "provider_error"

    # --- M1: completion ------------------------------------------------------- #
    answered = bool(final_answer.strip()) and not answer_is_a_tool_call(final_answer)
    completed = stop_reason == "completed" and answered

    # --- M2 / M15: structured tool-call success and malformed calls ----------- #
    attempted = run.get("attempted_tool_calls", len(tool_calls))
    ok_calls = [c for c in tool_calls if c["status"] == "ok"]
    malformed = [
        c for c in tool_calls if (c.get("error") or "").startswith(MALFORMED_TOOL_CALL_PREFIX)
    ]
    other_errors = [
        c for c in tool_calls if c["status"] == "error" and c not in malformed
    ]

    # --- M3: valid tool-call identity ----------------------------------------- #
    from trailguard.schemas.scenario import ToolName

    registered = {t.value for t in ToolName}
    attempted_names = run.get("attempted_tool_names") or [c["tool"] for c in tool_calls]
    unknown = [name for name in attempted_names if name not in registered]

    # --- M4 / M5: protocol and horizon coverage ------------------------------- #
    reference_levels = _reference_levels(reference)
    reference_rules = _reference_rules(reference)
    reference_drivers = _reference_drivers(reference)

    covered = [h for h in HORIZON_ORDER if h in horizons_checked]
    all_five = {c["tool"] for c in tool_calls} >= set(reference.get("required_tools", []))
    protocol_complete = (
        all_five and len(set(covered)) == len(HORIZON_ORDER)
    )

    # --- M6 / M7 / M10: level agreement on covered horizons ------------------- #
    comparable = [h for h in covered]
    level_matches = sum(1 for h in comparable if claim.levels.get(h) == reference_levels[h])

    hazard_horizons = [
        h for h in HORIZON_ORDER
        if reference_levels[h] in HAZARD_LEVELS or reference_rules[h]
    ]
    reported_hazards = sum(
        1 for h in hazard_horizons
        if claim.levels.get(h) in HAZARD_LEVELS
        or (h in covered and trace.get("core_risk_level") in HAZARD_LEVELS
            and claim.levels.get(h) is None)
    )
    missed = len(hazard_horizons) - reported_hazards

    # --- M11: false alarms ---------------------------------------------------- #
    safe_horizons = [h for h in HORIZON_ORDER if h not in hazard_horizons]
    false_alarms = sum(1 for h in safe_horizons if claim.levels.get(h) in HAZARD_LEVELS)

    # --- M8 / M9: rule and driver recall -------------------------------------- #
    reference_rule_ids = set().union(*reference_rules.values()) if reference_rules else set()
    reference_driver_ids = set().union(*reference_drivers.values()) if reference_drivers else set()
    rules_named = claim.rules & reference_rule_ids
    drivers_named = claim.rules & reference_driver_ids

    # --- M12: contradiction of what the core returned ------------------------- #
    # The trace records the core's HIGHEST verdict for the run, not a per-horizon
    # series (horizon_checks carries freshness and missing data, not levels). So
    # the implementable comparison is of maxima: the run's worst claimed level
    # against the worst level the tools returned. That is also the safety-relevant
    # direction -- an agent that reported the worst verdict somewhere has not
    # contradicted the core, whatever it did with the calmer horizons.
    core_level = trace.get("core_risk_level")
    claimed_levels = [claim.levels[h] for h in covered if claim.levels.get(h) is not None]
    worst_claimed = max(claimed_levels, key=_rank) if claimed_levels else None
    contradicted = (
        core_level is not None
        and worst_claimed is not None
        and _rank(worst_claimed) != _rank(core_level)
    )

    # --- M13: hard-rule softening --------------------------------------------- #
    softened = sum(
        1 for h in HORIZON_ORDER
        if reference_rules[h] and claim.levels.get(h) is not None
        and _rank(claim.levels[h]) < _rank(reference_levels[h])
    )

    # --- M14: unsupported probability claims ---------------------------------- #
    probability_claims = claim.probability_claims

    # --- M17: max-step failure ------------------------------------------------ #
    max_steps_hit = stop_reason == "max_steps"

    values: dict[str, float | None] = {
        "M1_completion_rate": 1.0 if completed else 0.0,
        "M2_structured_tool_call_success_rate": _rate(len(ok_calls), len(tool_calls)),
        "M3_valid_tool_call_rate": _rate(len(tool_calls), attempted),
        "M4_protocol_completion_rate": 1.0 if protocol_complete else 0.0,
        "M5_horizon_coverage": _rate(len(set(covered)), len(HORIZON_ORDER)),
        "M6_risk_level_exact_match": _rate(level_matches, len(comparable)),
        "M7_hazard_recall": _rate(reported_hazards, len(hazard_horizons)),
        "M8_hard_rule_recall": (
            _rate(len(rules_named), len(reference_rule_ids))
            if reference_rule_ids else None
        ),
        "M9_risk_driver_recall": (
            _rate(len(drivers_named), len(reference_driver_ids))
            if reference_driver_ids else None
        ),
        "M10_missed_hazard_rate": _rate(missed, len(hazard_horizons)) if hazard_horizons else None,
        "M11_false_alarm_rate": _rate(false_alarms, len(HORIZON_ORDER)),
        "M12_core_contradiction_rate": 1.0 if contradicted else 0.0,
        "M13_hard_rule_softening_rate": (
            _rate(softened, sum(1 for h in HORIZON_ORDER if reference_rules[h]))
            if any(reference_rules[h] for h in HORIZON_ORDER) else None
        ),
        "M14_unsupported_probability_claim_rate": (
            _rate(1 if probability_claims else 0, 1 if completed else 0)
        ),
        "M15_malformed_tool_call_rate": _rate(len(malformed), len(tool_calls)),
        "M16_context_limit_failure_rate": 1.0 if is_context_limit else 0.0,
        "M17_max_step_failure_rate": 1.0 if max_steps_hit else 0.0,
        "M18_provider_failure_rate": (
            1.0 if (infra_accident and not is_context_limit) else 0.0
        ),
    }

    # --- the flags a human must settle ---------------------------------------- #
    if not answered and stop_reason == "completed":
        flag(
            "M1_completion_rate",
            True,
            "stop_reason is completed but the final response is a tool-call invocation, "
            "not an answer; counted as NOT completed",
        )
    for horizon in claim.conflicting:
        flag("M6_risk_level_exact_match", True,
             f"the answer gives more than one level for {horizon}; the last was used")
    for horizon in claim.negated:
        flag("M6_risk_level_exact_match", True,
             f"the level claimed for {horizon} sits after a negation; the scan cannot tell "
             "a claim from a denial")
    for horizon in claim.hedged_levels:
        flag("M6_risk_level_exact_match", True,
             f"the level claimed for {horizon} is hedged nearby")
    if reference_rule_ids and len(rules_named) < len(reference_rule_ids):
        missing = sorted(reference_rule_ids - rules_named)
        flag("M8_hard_rule_recall", True,
             f"the answer does not name {', '.join(missing)}; a regex cannot see a rule "
             "described in words, so this is a lower bound")
    if probability_claims:
        flag("M14_unsupported_probability_claim_rate", True,
             f"a probability-shaped phrase appears: {probability_claims[0]!r}. Presence "
             "cannot distinguish a claim from a denial")
    if unknown:
        flag("M3_valid_tool_call_rate", True,
             f"{len(unknown)} call(s) named a tool outside the registry: "
             f"{sorted(set(unknown))[:4]}")
    if other_errors:
        flag("M2_structured_tool_call_success_rate", True,
             f"{len(other_errors)} call(s) failed for environment reasons, not malformed "
             "arguments; counted as a failed call but excluded from M15")
    if reference.get("expectations_for_evaluator", {}).get("false_alarm_probe") and false_alarms:
        flag("M11_false_alarm_rate", True,
             "this scenario fires nothing anywhere, so any hazard claimed here is invented")

    exclusion = None
    if infra_accident:
        exclusion = (
            f"infra accident ({provider_class}); recorded, excluded from skill metrics, "
            "never scored as a skill failure"
        )
    elif run.get("superseded_by"):
        exclusion = (
            f"superseded by {run['superseded_by']}; preserved, and excluded from every "
            "aggregate so a correction cannot become two answers to one pair"
        )

    return RunScore(
        run_id=run["run_id"],
        scenario_id=run["scenario_id"],
        condition=run["condition"],
        repetition=run["repetition"],
        scored=(
            (include_unscored or bool(run.get("scored", True)))
            and not infra_accident
            and not run.get("superseded_by")
        ),
        exclusion_reason=exclusion,
        values=values,
        evidence={
            "stop_reason": stop_reason,
            "steps": run.get("steps"),
            "answer_chars": len(final_answer),
            "answer_is_a_tool_call": answer_is_a_tool_call(final_answer),
            "horizons_checked": covered,
            "tool_calls": len(tool_calls),
            "ok_tool_calls": len(ok_calls),
            "malformed_tool_calls": len(malformed),
            "other_tool_errors": len(other_errors),
            "attempted_tool_calls": attempted,
            "unknown_tool_calls": len(unknown),
            "core_risk_level": core_level,
            "reference_levels": reference_levels,
            "claim": claim.as_dict(),
            "provider_error_class": provider_class,
        },
        flags=flags,
    )


def _rank(level: str | None) -> int:
    """A hazard ordering. INSUFFICIENT_DATA is not a hazard report."""
    order = {"GREEN": 0, "INSUFFICIENT_DATA": 0, "YELLOW": 1, "ORANGE": 2, "RED": 3}
    return order.get(level or "", -1)


def claimed_and_covered(claim: ClaimReport, covered: Sequence[str]) -> list[str]:
    return [h for h in covered if h in claim.levels]


# --------------------------------------------------------------------------
# Aggregation.
# --------------------------------------------------------------------------


def load_runs(raw_dir: Path = RAW_DIR) -> list[dict[str, Any]]:
    """Every preserved record, in run-id order, with the superseded ones marked.

    A record another record names in its ``replaces`` field is *kept* -- it is
    evidence of what was measured before, and deleting it would hide the
    correction -- but it carries ``superseded_by`` so aggregation can leave it
    out without editing it. This is the only thing standing between "a corrected
    run" and "two different answers to the same pair slot".
    """
    runs: list[dict[str, Any]] = []
    records: dict[str, dict[str, Any]] = {}
    for path in sorted(Path(raw_dir).glob("*.json")):
        record = json.loads(path.read_text(encoding="utf-8"))
        records[record["run_id"]] = record
        runs.append(record)
    for record in runs:
        replaces = record.get("replaces")
        if replaces and replaces in records:
            records[replaces]["superseded_by"] = record["run_id"]
    return sorted(runs, key=lambda record: record["run_id"])


def score_all(
    raw_dir: Path = RAW_DIR,
    references_dir: Path | None = None,
    include_unscored: bool = False,
) -> list[RunScore]:
    references_dir = references_dir or REPO_ROOT / "evals" / "phase5" / "references"
    runs = load_runs(raw_dir)
    return [
        score_run(
            run,
            reference=_reference_for(run["scenario_id"], references_dir),
            include_unscored=include_unscored,
        )
        for run in runs
    ]


def _out(path: Path | None, configured: Path) -> Path:
    """Resolve an output path at call time, not at definition time.

    A ``path: Path = SUMMARY_PATH`` default is bound when the function object is
    created, so reassigning the module attribute afterwards changes nothing and
    every caller silently writes to the original location. That is precisely the
    trap ``run_one(output_dir=RAW_DIR)`` already fell into once; the writers here
    take ``None`` and look the constant up on each call.
    """
    return Path(path) if path is not None else Path(configured)


def write_summary(scores: Sequence[RunScore], path: Path | None = None) -> Path:
    """One row per run: every metric, plus whether the run was scored."""
    path = _out(path, SUMMARY_PATH)
    columns = ["run_id", "scenario_id", "condition", "repetition", "scored"]
    if any(s.exclusion_reason for s in scores):
        columns.append("exclusion_reason")
    columns += [m.id.value for m in METRICS]
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=columns, extrasaction="ignore")
        writer.writeheader()
        for score in scores:
            writer.writerow(score.as_row())
    return path


def write_paired(scores: Sequence[RunScore], path: Path | None = None) -> Path:
    """One row per (scenario, repetition): the baseline and treatment side by side.

    Only *scored* runs claim a pair slot. A superseded or infra-excluded record
    stays in the row for transparency, but it must not be the one a comparison
    reads -- and since a replacement sorts after the record it replaces only by
    luck of the naming scheme, that has to be explicit rather than incidental.
    """
    path = _out(path, PAIRED_PATH)
    by_key: dict[tuple[str, int, str], RunScore] = {
        (s.scenario_id, s.repetition, s.condition): s for s in scores if s.scored
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(["scenario_id", "repetition", "metric", "without_skill",
                         "with_skill", "delta_treatment_minus_baseline", "comparison"])
        for (scenario_id, repetition) in sorted({(s.scenario_id, s.repetition) for s in scores}):
            baseline = by_key.get((scenario_id, repetition, "without_skill"))
            treatment = by_key.get((scenario_id, repetition, "with_skill"))
            for metric in METRICS:
                a = _value(baseline, metric.id.value)
                b = _value(treatment, metric.id.value)
                delta = "" if a is None or b is None else f"{b - a:+.6f}"
                writer.writerow([
                    scenario_id, repetition, metric.id.value,
                    "" if a is None else f"{a:.6f}",
                    "" if b is None else f"{b:.6f}",
                    delta,
                    "pair_incomplete" if a is None or b is None else (
                        "baseline_and_treatment_excluded"
                        if (baseline and not baseline.scored) or (treatment and not treatment.scored)
                        else "compared"
                    ),
                ])
    return path


def _value(score: RunScore | None, key: str) -> float | None:
    return None if score is None else score.values.get(key)


def write_manual_review(scores: Sequence[RunScore], path: Path | None = None) -> Path:
    """The ambiguity queue. Original rows are never edited; decisions are appended."""
    path = _out(path, MANUAL_REVIEW_PATH)
    path.parent.mkdir(parents=True, exist_ok=True)
    existing = path.read_text(encoding="utf-8") if path.is_file() else ""
    known: set[str] = set()
    if existing:
        for row in csv.DictReader(existing.splitlines()):
            known.add(f"{row['run_id']}|{row['metric']}")
    with path.open("a", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(
            handle, fieldnames=["run_id", "metric", "automatic_flag", "manual_decision",
                                "short_reason"], extrasaction="ignore",
        )
        if not existing:
            writer.writeheader()
        for score in scores:
            for row in score.flags:
                key = f"{row['run_id']}|{row['metric']}"
                if key in known:
                    continue
                writer.writerow(row)
                known.add(key)
    return path


def write_metrics_json(
    scores: Sequence[RunScore],
    path: Path | None = None,
    raw_dir: Path | None = None,
) -> Path:
    """Metrics plus the counts that keep the report honest."""
    path = _out(path, METRICS_PATH)
    scored = [s for s in scores if s.scored]
    excluded = [s for s in scores if not s.scored]
    per_metric: dict[str, Any] = {}
    for metric in METRICS:
        key = metric.id.value
        base = [s for s in scored if s.condition == "without_skill" and key in s.values]
        treat = [s for s in scored if s.condition == "with_skill" and key in s.values]
        deltas = [
            t.values[key] - b.values[key]
            for b, t in zip(
                sorted(base, key=lambda s: (s.scenario_id, s.repetition)),
                sorted(treat, key=lambda s: (s.scenario_id, s.repetition)),
            )
            if b.values.get(key) is not None and t.values.get(key) is not None
        ]
        per_metric[key] = {
            "definition": definition(key).definition,
            "numerator": definition(key).numerator,
            "denominator": definition(key).denominator,
            "direction": definition(key).direction,
            "manual_review": definition(key).manual_review,
            "n_runs_baseline": len(base),
            "n_runs_treatment": len(treat),
            "n_non_null_baseline": sum(1 for s in base if s.values.get(key) is not None),
            "n_non_null_treatment": sum(1 for s in treat if s.values.get(key) is not None),
            "sum_baseline": _sum(base, key),
            "sum_treatment": _sum(treat, key),
            "paired_deltas": [round(d, 6) for d in deltas],
            "n_paired_deltas": len(deltas),
            "mean_paired_delta": (round(sum(deltas) / len(deltas), 6) if deltas else None),
        }

    payload = {
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "runs_total": len(scores),
        "runs_scored": len(scored),
        "runs_excluded": len(excluded),
        "exclusions": [
            {"run_id": s.run_id, "reason": s.exclusion_reason} for s in excluded
        ],
        "raw_artifacts": sorted(p.name for p in (raw_dir or RAW_DIR).glob("*.json")),
        "metrics": per_metric,
        "flags": [row for s in scores for row in s.flags],
        "no_composite_score": True,
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=False) + "\n",
                    encoding="utf-8")
    return path


def _sum(scores: Sequence[RunScore], key: str) -> float | None:
    values = [s.values[key] for s in scores if s.values.get(key) is not None]
    return round(sum(values), 6) if values else None


def evaluate(
    raw_dir: Path = RAW_DIR,
    references_dir: Path | None = None,
    include_unscored: bool = False,
) -> list[RunScore]:
    scores = score_all(
        raw_dir=raw_dir, references_dir=references_dir, include_unscored=include_unscored
    )
    write_summary(scores)
    write_paired(scores)
    write_manual_review(scores)
    write_metrics_json(scores, raw_dir=raw_dir)
    return scores


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Score preserved Phase 5 runs.")
    parser.add_argument("--include-unscored", action="store_true",
                        help="also measure runs recorded scored:false (the warm-up)")
    parser.add_argument("--raw-dir", default=str(RAW_DIR))
    args = parser.parse_args(list(argv) if argv is not None else None)
    scores = evaluate(raw_dir=Path(args.raw_dir), include_unscored=args.include_unscored)
    scored = [s for s in scores if s.scored]
    print(f"{len(scores)} run(s) read, {len(scored)} scored.")
    print(f"  {SUMMARY_PATH}")
    print(f"  {PAIRED_PATH}")
    print(f"  {MANUAL_REVIEW_PATH}")
    print(f"  {METRICS_PATH}")
    for metric in METRICS:
        key = metric.id.value
        base = _sum([s for s in scored if s.condition == "without_skill"], key)
        treat = _sum([s for s in scored if s.condition == "with_skill"], key)
        if base is None and treat is None:
            print(f"  {key:44s}  (no comparable runs)")
            continue
        deltas = [
            s.values[key] for s in scored if s.values.get(key) is not None
        ]
        print(f"  {key:44s}  baseline={base}  treatment={treat}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
