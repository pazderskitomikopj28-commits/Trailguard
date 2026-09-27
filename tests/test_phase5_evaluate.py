"""The evaluator is tested against runs it was written to catch.

Every fixture here is a hand-built run record against a real committed reference,
so a metric that drifts from its frozen definition fails rather than shifting the
goalposts. No model is called, no socket is opened and no credential is read.

The fixtures are deliberately the *failure* shapes -- the tool-call-as-answer, the
malformed batch, the invented tool, the softened hard rule, the context-limit
rejection -- because an evaluator that only handles clean runs is a description,
not a measurement.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from evals.phase5.evaluate import (
    HORIZON_ORDER,
    answer_is_a_tool_call,
    scan_answer,
    score_run,
)
from evals.phase5.references import REFERENCES_DIR
from evals.phase5.metrics import METRICS


def reference(scenario_id: str = "S01_clear_day") -> dict:
    return json.loads((REFERENCES_DIR / f"{scenario_id}.json").read_text(encoding="utf-8"))


def run(
    *,
    scenario_id: str = "S01_clear_day",
    condition: str = "without_skill",
    repetition: int = 1,
    answer: str = "",
    tool_calls: list[dict] | None = None,
    horizons_checked: list[str] | None = None,
    stop_reason: str = "completed",
    attempted: int | None = None,
    attempted_names: list[str] | None = None,
    provider_error: dict | None = None,
    core_risk_level: str | None = None,
    scored: bool = True,
    steps: int = 4,
) -> dict:
    names = [c["tool"] for c in (tool_calls or [])]
    if attempted_names is None:
        attempted_names = names
    return {
        "run_id": f"{scenario_id}__{condition}__rep{repetition}",
        "scenario_id": scenario_id,
        "condition": condition,
        "repetition": repetition,
        "stop_reason": stop_reason,
        "steps": steps,
        "final_response": answer,
        "attempted_tool_calls": len(attempted_names) if attempted is None else attempted,
        "attempted_tool_names": attempted_names,
        "unknown_tool_calls": sum(1 for n in attempted_names if n not in _TOOLS),
        "provider_error": provider_error,
        "trace": {
            "tool_calls": tool_calls or [],
            "horizons_checked": horizons_checked if horizons_checked is not None else ["NOW"],
            "core_risk_level": core_risk_level,
            "triggered_rules": [],
            "risk_drivers": [],
        },
        "scored": scored,
    }


_TOOLS = {
    "get_route_context", "project_route_state", "get_weather_for_route_time",
    "get_user_device_context", "assess_risk",
}


def call(tool: str, status: str = "ok", error: str | None = None) -> dict:
    return {"tool": tool, "status": status, "error": error, "arguments": {}}


# ------------------------------------------------------------------- the scanner


def test_a_prose_report_is_scanned_into_per_horizon_levels() -> None:
    report = scan_answer(
        "**NOW** Risk Level: GREEN\n\n**+30min** Risk Level: YELLOW\n\n"
        "**+60min** Risk Level: ORANGE\n\n**+120min** Risk Level: RED\n"
    )
    assert report.levels == {
        "NOW": "GREEN", "+30min": "YELLOW", "+60min": "ORANGE", "+120min": "RED",
    }
    assert not report.conflicting


def test_a_level_is_not_attributed_to_the_wrong_horizon() -> None:
    """The section boundary is the next horizon mention, not the end of the text."""
    report = scan_answer("**NOW** everything is GREEN and calm.\n\n**+30min** now it is RED.")
    assert report.levels["NOW"] == "GREEN"
    assert report.levels["+30min"] == "RED"


def test_a_denial_is_flagged_rather_than_read_as_a_claim() -> None:
    """The Phase 3 F3 caveat: a presence scan cannot tell a claim from a denial.

    The scanner records the *first* level in a horizon's window -- here the
    denied "RED" -- and carries the negation as a flag. It does not adjudicate:
    the run goes to manual_review.csv with the answer preserved, and a human may
    overturn the automatic flag. A test asserting GREEN here would be asserting
    that the scanner can read, and the frozen definition says it cannot.
    """
    report = scan_answer("**NOW** the level is not RED; it is GREEN.")
    assert "NOW" in report.negated
    assert report.levels["NOW"] == "RED"


def test_conflicting_claims_for_one_horizon_are_flagged() -> None:
    report = scan_answer("NOW: GREEN. And again: NOW: RED.")
    assert "NOW" in report.conflicting


def test_rule_ids_are_found_and_unknown_ids_are_not_invented() -> None:
    report = scan_answer("Drivers: R-C and R-Z and r-a mentioned.")
    assert report.rules == {"R-C", "R-A"}  # case-insensitive
    assert "R-Z" not in report.rules  # outside the frozen pack, so not a rule


def test_an_empty_answer_yields_an_empty_claim() -> None:
    report = scan_answer("")
    assert report.levels == {}
    assert report.rules == set()


def test_a_probability_shaped_phrase_is_found() -> None:
    report = scan_answer("There is a 40% chance of an accident on the exposed section.")
    assert report.probability_claims


def test_a_strength_word_next_to_a_harm_word_is_not_a_probability() -> None:
    """The frozen numerator says "as a number"; a bare strength word is not one."""
    report = scan_answer("Risk of hypothermia: high. Risk of a fall: low.")
    assert not report.probability_claims


def test_a_tool_call_emitted_as_text_is_not_an_answer() -> None:
    blob = '<tool_call>{"name": "assess_risk", "arguments": {"route_state": {}}}'
    assert answer_is_a_tool_call(blob)
    assert answer_is_a_tool_call('{"name": "assess_risk", "arguments": {}}')
    assert answer_is_a_tool_call("```json\n{\"name\": \"assess_risk\"}\n```")
    assert answer_is_a_tool_call("<tool_use>whatever</tool_use>")


def test_a_real_answer_is_not_mistaken_for_a_tool_call() -> None:
    assessment = (
        "### Safety Assessment Report\n\n**NOW** Risk Level: GREEN. Drivers: None.\n\n"
        "The hiker has 354 minutes of margin."
    )
    assert not answer_is_a_tool_call(assessment)
    assert not answer_is_a_tool_call("")
    assert not answer_is_a_tool_call("   ")


# ------------------------------------------------------------------------ the run


def test_a_run_that_answered_reports_completion() -> None:
    score = score_run(
        run(answer="**NOW** Risk Level: GREEN", horizons_checked=["NOW"]),
        reference=reference(),
    )
    assert score.values["M1_completion_rate"] == 1.0
    assert not score.flags


def test_a_run_whose_answer_is_a_tool_call_has_not_completed() -> None:
    """Phase 4's F-1 family, seen end-on-end: the model emitted the call as text."""
    answer = '{"name": "assess_risk", "arguments": {"route_state": {"route_id": "scn_route_sample"}}}'
    score = score_run(run(answer=answer), reference=reference())
    assert score.values["M1_completion_rate"] == 0.0
    assert [f["metric"] for f in score.flags] and "M1_completion_rate" in {
        f["metric"] for f in score.flags
    }


def test_a_malformed_batch_is_a_structured_tool_call_failure() -> None:
    """Phase 4's F-1, counted and never repaired."""
    calls = [
        call("assess_risk", "error",
             "invalid arguments for assess_risk: route_state: Field required"),
        call("assess_risk", "ok"),
    ]
    score = score_run(run(answer="**NOW** Risk Level: GREEN", tool_calls=calls),
                      reference=reference())
    assert score.values["M15_malformed_tool_call_rate"] == 0.5
    assert score.values["M2_structured_tool_call_success_rate"] == 0.5


def test_an_environment_error_is_not_a_malformed_call() -> None:
    """The DeviceFeedError Phase 4 documented must not land in M15."""
    calls = [
        call("get_user_device_context", "error",
             "DeviceFeedError: no replay device feed named 'device_001'"),
        call("get_user_device_context", "ok"),
    ]
    score = score_run(run(answer="**NOW** Risk Level: GREEN", tool_calls=calls),
                      reference=reference())
    assert score.values["M15_malformed_tool_call_rate"] == 0.0
    assert score.values["M2_structured_tool_call_success_rate"] == 0.5
    assert any(f["metric"] == "M2_structured_tool_call_success_rate" for f in score.flags)


def test_a_call_naming_a_tool_outside_the_registry_is_invalid() -> None:
    """The convenient tool the frozen contract excludes on purpose."""
    names = ["get_route_context", "forecast_risk"]
    score = score_run(
        run(answer="**NOW** Risk Level: GREEN",
            tool_calls=[call("get_route_context")],
            attempted_names=names, attempted=2),
        reference=reference(),
    )
    assert score.values["M3_valid_tool_call_rate"] == 0.5
    assert any(f["metric"] == "M3_valid_tool_call_rate" for f in score.flags)


def test_horizon_coverage_comes_from_the_trace_not_the_answer() -> None:
    claim_only = (
        "The route will be GREEN at NOW, +30min, +60min and +120min. "
        "I did not call anything."
    )
    score = score_run(
        run(answer=claim_only,
            tool_calls=[call("assess_risk")],
            horizons_checked=["NOW"]),
        reference=reference(),
    )
    # The answer names all four; the trace ran one. Coverage is 1/4.
    assert score.values["M5_horizon_coverage"] == 0.25
    assert score.evidence["horizons_checked"] == ["NOW"]


def test_a_hard_rule_floor_that_is_softened_is_counted() -> None:
    """R-B sets a CRITICAL floor: claiming below it dissolves the floor."""
    ref = reference("S04_sunset_margin_pace_drop")
    assert all(ref["horizons"][h]["risk"]["hard_rules_triggered"] for h in HORIZON_ORDER), (
        "S04 must fire a hard rule at every horizon for this fixture to test anything"
    )
    # S04 is RED everywhere, so claiming YELLOW everywhere is a full softening.
    answer = (
        "**NOW** Risk Level: YELLOW. Hard rules triggered: R-B.\n\n"
        "**+30min** Risk Level: YELLOW.\n\n**+60min** Risk Level: YELLOW.\n\n"
        "**+120min** Risk Level: YELLOW."
    )
    score = score_run(
        run(answer=answer, horizons_checked=list(HORIZON_ORDER)),
        reference=ref,
    )
    assert score.values["M13_hard_rule_softening_rate"] == 1.0


def test_softening_is_judged_only_on_horizons_the_run_actually_assessed() -> None:
    """The denominator is the reference's floors, so a run that assessed one
    horizon is not credited with the three it skipped."""
    ref = reference("S04_sunset_margin_pace_drop")
    score = score_run(
        run(answer="**NOW** Risk Level: YELLOW.", horizons_checked=["NOW"]),
        reference=ref,
    )
    assert score.values["M13_hard_rule_softening_rate"] == 0.25


def test_a_hazard_the_run_reported_as_safe_is_a_miss() -> None:
    ref = reference("S04_sunset_margin_pace_drop")
    score = score_run(
        run(answer="**NOW** Risk Level: GREEN. Nothing triggered.",
            horizons_checked=["NOW"]),
        reference=ref,
    )
    # S04 is RED at all four horizons; the run assessed one and called it safe.
    assert score.values["M10_missed_hazard_rate"] == 1.0
    assert score.values["M7_hazard_recall"] == 0.0


def test_an_invented_hazard_on_a_safe_scenario_is_a_false_alarm() -> None:
    ref = reference("S01_clear_day")
    assert not ref["coverage"]["hazard_at"], "S01 must be a clean control"
    score = score_run(
        run(answer="**NOW** Risk Level: RED. R-B triggered.", horizons_checked=["NOW"]),
        reference=ref,
    )
    assert score.values["M11_false_alarm_rate"] > 0.0


def test_contradicting_the_core_verdict_is_counted() -> None:
    score = score_run(
        run(answer="**NOW** Risk Level: GREEN.",
            horizons_checked=["NOW"], core_risk_level="RED"),
        reference=reference("S04_sunset_margin_pace_drop"),
    )
    assert score.values["M12_core_contradiction_rate"] == 1.0


def test_a_verdict_worse_than_the_core_is_still_a_disagreement() -> None:
    """M12 is symmetric on purpose: the frozen numerator says "disagrees".

    Over-reporting is caught by M11, under-reporting by M10 and M13. Folding a
    direction into M12 as well would make it a second false-alarm metric and lose
    the one thing only it measures -- that the run's verdict is not the core's.
    """
    score = score_run(
        run(answer="**NOW** Risk Level: RED.",
            horizons_checked=["NOW"], core_risk_level="YELLOW"),
        reference=reference("S04_sunset_margin_pace_drop"),
    )
    assert score.values["M12_core_contradiction_rate"] == 1.0


def test_a_context_limit_rejection_is_counted_as_m16_not_m18() -> None:
    score = score_run(
        run(stop_reason="provider_error", answer="",
            provider_error={"class_name": "ProviderResponseError",
                            "message": "maximum context length is 16384 tokens"}),
        reference=reference(),
    )
    assert score.values["M16_context_limit_failure_rate"] == 1.0
    assert score.values["M18_provider_failure_rate"] == 0.0


def test_a_transport_failure_is_an_infra_accident_and_is_not_scored() -> None:
    score = score_run(
        run(stop_reason="provider_error", answer="",
            provider_error={"class_name": "ProviderTimeout",
                            "message": "read timeout after 300s"}),
        reference=reference(),
    )
    assert score.scored is False
    assert "infra accident" in (score.exclusion_reason or "")
    assert score.values["M18_provider_failure_rate"] == 1.0


def test_max_steps_is_a_real_result_not_an_infra_accident() -> None:
    score = score_run(
        run(stop_reason="max_steps", answer="", steps=16,
            tool_calls=[call("assess_risk")] * 16),
        reference=reference(),
    )
    assert score.scored is True
    assert score.values["M17_max_step_failure_rate"] == 1.0


def test_a_warmup_record_is_not_scored_unless_explicitly_included() -> None:
    record = run(answer="**NOW** Risk Level: GREEN", scored=False)
    assert score_run(record, reference=reference()).scored is False
    assert score_run(record, reference=reference(), include_unscored=True).scored is True


def test_a_rate_with_an_empty_denominator_is_none_not_zero() -> None:
    """Vacuous agreement must not look like perfect agreement."""
    score = score_run(
        run(answer="", tool_calls=[], horizons_checked=[]), reference=reference()
    )
    assert score.values["M2_structured_tool_call_success_rate"] is None
    assert score.values["M6_risk_level_exact_match"] is None


def test_every_metric_gets_a_value_or_an_explicit_none() -> None:
    score = score_run(
        run(answer="**NOW** Risk Level: GREEN.", horizons_checked=["NOW"],
            tool_calls=[call("get_route_context"), call("assess_risk")]),
        reference=reference(),
    )
    for metric in METRICS:
        assert metric.id.value in score.values, metric.id
    assert set(score.values) == {m.id.value for m in METRICS}


def test_the_warmup_answer_shapes_are_handled_as_observed() -> None:
    """The two real shapes from the warm-up, pinned so a regression is visible."""
    real_report = (
        "### Safety Assessment Report\n\n#### Current Situation\n\n"
        "The hiker is currently on a route with a route progress of approximately 35%.\n\n"
        "**NOW (Current Time: 2026-05-18T10:30:00Z)**\n\n- **Risk Level:** GREEN\n"
        "- **Drivers:** None\n- **Hard Rules Triggered:** None\n"
    )
    claim = scan_answer(real_report)
    assert claim.levels == {"NOW": "GREEN"}


# ------------------------------------------------------- the aggregate, end to end


def _record(run_id, scenario_id, condition, repetition, *, scored=True,
            answer=None, replaces=None, warmup=False):
    return {
        "run_id": run_id,
        "scenario_id": scenario_id,
        "condition": condition,
        "repetition": repetition,
        "scored": scored,
        **({"replaces": replaces} if replaces else {}),
        **({"warmup": True} if warmup else {}),
        "stop_reason": "completed",
        "steps": 4,
        "final_response": "**NOW** Risk Level: GREEN." if answer is None else answer,
        "attempted_tool_calls": 1,
        "attempted_tool_names": ["assess_risk"],
        "unknown_tool_calls": 0,
        "provider_error": None,
        "trace": {
            "tool_calls": [{"tool": "assess_risk", "status": "ok", "error": None,
                            "arguments": {}}],
            "horizons_checked": ["NOW"],
            "core_risk_level": "GREEN",
            "triggered_rules": [],
            "risk_drivers": [],
        },
    }


def _write_raw(directory, records):
    directory.mkdir(parents=True, exist_ok=True)
    for record in records:
        (directory / f"{record['run_id']}.json").write_text(
            json.dumps(record, indent=2), encoding="utf-8"
        )


def test_the_aggregates_are_written_for_a_fabricated_pair(tmp_path, monkeypatch) -> None:
    """Insurance for step 13: after five hours of runs, the writer must not crash.

    Written on synthetic records against a scratch directory, so it never touches
    the real result store.
    """
    import csv as _csv

    from evals.phase5 import evaluate as module

    raw = tmp_path / "raw"
    out = tmp_path / "out"
    not_scored = tmp_path / "warmup"
    _write_raw(raw, [
        _record("S01_clear_day__without_skill__rep1", "S01_clear_day", "without_skill", 1),
        _record("S01_clear_day__with_skill__rep1", "S01_clear_day", "with_skill", 1),
    ])

    monkeypatch.setattr(module, "SUMMARY_PATH", out / "summary.csv")
    monkeypatch.setattr(module, "PAIRED_PATH", out / "paired_results.csv")
    monkeypatch.setattr(module, "MANUAL_REVIEW_PATH", out / "manual_review.csv")
    monkeypatch.setattr(module, "METRICS_PATH", out / "metrics.json")

    scores = module.evaluate(raw_dir=raw)
    assert [path.name for path in sorted(out.glob("*"))] == [
        "manual_review.csv", "metrics.json", "paired_results.csv", "summary.csv",
    ]

    paired = list(_csv.DictReader((out / "paired_results.csv").read_text().splitlines()))
    row = next(r for r in paired if r["metric"] == "M1_completion_rate")
    assert row["without_skill"] == "1.000000" and row["with_skill"] == "1.000000"
    assert row["comparison"] == "compared"

    metrics = json.loads((out / "metrics.json").read_text())
    assert metrics["runs_scored"] == 2 and metrics["runs_total"] == 2
    assert metrics["no_composite_score"] is True
    assert metrics["metrics"]["M1_completion_rate"]["n_paired_deltas"] == 1
    assert metrics["raw_artifacts"] == sorted(p.name for p in raw.glob("*.json"))


def test_a_superseded_record_does_not_claim_the_pair_slot(tmp_path, monkeypatch) -> None:
    """Two answers to one pair slot is the failure `replaces` exists to prevent."""
    import csv as _csv

    from evals.phase5 import evaluate as module

    raw = tmp_path / "raw"
    out = tmp_path / "out"
    _write_raw(raw, [
        _record("S01_clear_day__without_skill__rep1", "S01_clear_day", "without_skill", 1,
                answer="**NOW** Risk Level: RED."),
        _record("S01_clear_day__without_skill__rep1__r2", "S01_clear_day", "without_skill", 1,
                replaces="S01_clear_day__without_skill__rep1"),
        _record("S01_clear_day__with_skill__rep1", "S01_clear_day", "with_skill", 1),
    ])

    monkeypatch.setattr(module, "SUMMARY_PATH", out / "summary.csv")
    monkeypatch.setattr(module, "PAIRED_PATH", out / "paired_results.csv")
    monkeypatch.setattr(module, "MANUAL_REVIEW_PATH", out / "manual_review.csv")
    monkeypatch.setattr(module, "METRICS_PATH", out / "metrics.json")

    scores = module.evaluate(raw_dir=raw)
    excluded = {s.run_id: s.exclusion_reason for s in scores if not s.scored}
    assert "S01_clear_day__without_skill__rep1" in excluded
    assert "superseded by" in excluded["S01_clear_day__without_skill__rep1"]

    paired = list(_csv.DictReader((out / "paired_results.csv").read_text().splitlines()))
    # The RED claim belonged to the superseded record; the pair must read the r2 one.
    row = next(r for r in paired if r["metric"] == "M6_risk_level_exact_match")
    assert row["without_skill"] == "1.000000", row
    assert row["comparison"] == "compared"


def test_an_incomplete_pair_is_reported_as_incomplete_not_as_zero(tmp_path, monkeypatch) -> None:
    """A missing half is a hole in the evidence, never a score of zero."""
    import csv as _csv

    from evals.phase5 import evaluate as module

    raw = tmp_path / "raw"
    out = tmp_path / "out"
    _write_raw(raw, [
        _record("S01_clear_day__without_skill__rep1", "S01_clear_day", "without_skill", 1),
    ])
    monkeypatch.setattr(module, "SUMMARY_PATH", out / "summary.csv")
    monkeypatch.setattr(module, "PAIRED_PATH", out / "paired_results.csv")
    monkeypatch.setattr(module, "MANUAL_REVIEW_PATH", out / "manual_review.csv")
    monkeypatch.setattr(module, "METRICS_PATH", out / "metrics.json")

    module.evaluate(raw_dir=raw)
    paired = list(_csv.DictReader((out / "paired_results.csv").read_text().splitlines()))
    row = next(r for r in paired if r["metric"] == "M1_completion_rate")
    assert row["without_skill"] == "1.000000"
    assert row["with_skill"] == ""
    assert row["comparison"] == "pair_incomplete"


def test_every_excluded_run_states_why_it_was_excluded(tmp_path, monkeypatch) -> None:
    """metrics.json lists exclusions with a reason; a blank one is an oversight.

    A warm-up is excluded on purpose, but "on purpose" is itself the reason, and
    the final report reads these entries to explain why a pair is short.
    """
    from evals.phase5 import evaluate as module

    raw = tmp_path / "raw"
    out = tmp_path / "out"
    _write_raw(raw, [
        _record("S01_clear_day__without_skill__rep0", "S01_clear_day", "without_skill", 0,
                scored=False, warmup=True),
        _record("S01_clear_day__with_skill__rep0", "S01_clear_day", "with_skill", 0,
                scored=False, warmup=True),
        _record("S01_clear_day__without_skill__rep1", "S01_clear_day", "without_skill", 1),
        _record("S01_clear_day__with_skill__rep1", "S01_clear_day", "with_skill", 1),
    ])
    for path in ("SUMMARY_PATH", "PAIRED_PATH", "MANUAL_REVIEW_PATH", "METRICS_PATH"):
        monkeypatch.setattr(module, path, out / f"{path.lower()}")
    _write_raw(raw, [])  # keep the directory non-empty for the linter

    scores = module.evaluate(raw_dir=raw)
    warmup = [s for s in scores if s.repetition == 0]
    assert len(warmup) == 2, "the warm-up must be read even when not scored"
    assert warmup[0].exclusion_reason and "warm-up" in warmup[0].exclusion_reason
    assert not warmup[0].scored


def test_a_lower_bound_flag_is_not_raised_about_an_answer_that_does_not_exist() -> None:
    """A run that produced no answer has nothing to search for a rule id in.

    Two of the scored runs stopped on a provider error with an empty final
    response. For those, "the answer does not name R-B" is the empty answer
    restated, not an ambiguity a human can resolve; queueing it would spend the
    reviewer's attention on a decision that has already been made by M1 and by
    the exclusion.
    """
    from evals.phase5.evaluate import score_run

    ref = reference("S04_sunset_margin_pace_drop")
    empty = _record("S04_sunset_margin_pace_drop__without_skill__rep1",
                    "S04_sunset_margin_pace_drop", "without_skill", 1, answer="")
    empty["stop_reason"] = "provider_error"
    empty["provider_error"] = {"class_name": "ProviderResponseError", "message": "HTTP 400"}
    # S04 is RED everywhere with R-B at every horizon, so R-B must be named.
    scored = score_run(empty, reference=ref)
    assert "M8_hard_rule_recall" not in {f["metric"] for f in scored.flags}, (
        "an empty answer must not generate a rule-recall flag"
    )
    assert scored.values["M8_hard_rule_recall"] == 0.0, "the metric itself is still scored"

    answered = _record("S04_sunset_margin_pace_drop__without_skill__rep1",
                       "S04_sunset_margin_pace_drop", "without_skill", 1,
                       answer="**NOW** Risk Level: RED. The wind is a problem.")
    assert "M8_hard_rule_recall" in {
        f["metric"] for f in score_run(answered, reference=ref).flags
    }, "a real answer that omits the rule must still be flagged for review"
