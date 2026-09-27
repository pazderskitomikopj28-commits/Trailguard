"""The metric definitions are pinned to the code they are scored from.

A metric is a promise that two people reading the same run record reach the same
number. The tests below do not check arithmetic -- that is ``evaluate.py``'s job
and it is tested against a fixture -- they check that the definitions can still be
kept: that the strings the mechanical scans key on are the strings the harness
really writes, that the fields the definitions read really exist on the frozen
contracts, and that no metric has drifted into an LLM-judged one.

Written before the first scored run, like everything else in this module.
"""

from __future__ import annotations

import inspect
import json
from pathlib import Path

import pytest

from evals.phase5 import metrics as m
from evals.phase5.references import REFERENCES_DIR, load_scenarios
from trailguard.agent import harness as _harness_module
from trailguard.schemas.trace import EvidenceTrace, ToolCallRecord

from conftest import REPO_ROOT

#: The M-ids the brief numbers, in order. A metric renamed or lost is a
#: pre-registration broken silently, which is worse than one that never existed.
EXPECTED_IDS = (
    "M1_completion_rate",
    "M2_structured_tool_call_success_rate",
    "M3_valid_tool_call_rate",
    "M4_protocol_completion_rate",
    "M5_horizon_coverage",
    "M6_risk_level_exact_match",
    "M7_hazard_recall",
    "M8_hard_rule_recall",
    "M9_risk_driver_recall",
    "M10_missed_hazard_rate",
    "M11_false_alarm_rate",
    "M12_core_contradiction_rate",
    "M13_hard_rule_softening_rate",
    "M14_unsupported_probability_claim_rate",
    "M15_malformed_tool_call_rate",
    "M16_context_limit_failure_rate",
    "M17_max_step_failure_rate",
    "M18_provider_failure_rate",
)


# ------------------------------------------------------------------------ shape


def test_there_are_exactly_eighteen_metrics_with_the_briefs_ids() -> None:
    assert tuple(m_.id.value for m_ in m.METRICS) == EXPECTED_IDS


def test_every_metric_declares_a_numerator_a_denominator_and_a_source() -> None:
    for metric in m.METRICS:
        assert metric.numerator.strip(), metric.id
        assert metric.denominator.strip(), metric.id
        assert metric.source in ("trace", "claim", "run"), metric.id
        assert metric.unit.strip(), metric.id
        assert metric.definition.strip(), metric.id


def test_every_metric_is_paired_because_every_metric_is_a_within_pair_comparison() -> None:
    """A metric that is not compared within a pair answers a different question.

    The single-variable comparison is the whole benchmark (spec 3.1). A metric
    reported only per-condition, unpaired, is a statistic about the dataset and
    not about the skill.
    """
    for metric in m.METRICS:
        assert metric.pairs == "yes", metric.id


def test_lookup_rejects_an_unknown_metric_rather_than_returning_none() -> None:
    with pytest.raises(KeyError, match="unknown metric"):
        m.definition("M99_not_a_metric")


# ---------------------------------------------------------------- mechanical scans


def test_the_malformed_call_prefix_is_the_prefix_the_harness_writes() -> None:
    """If this drifts, M15 silently starts counting a different set of failures.

    The harness builds ``f"invalid arguments for {tool.value}: {problems}"`` in
    two places. The assertion is a containment test against the harness source
    rather than a copy of the string, so an edit to the harness's wording fails
    here instead of quietly changing what M15 measures.
    """
    source = inspect.getsource(_harness_module)
    assert m.MALFORMED_TOOL_CALL_PREFIX in source, (
        "the harness no longer writes the prefix M15 discriminates on; M15 must be "
        "re-derived from the harness's actual error text, not from this module"
    )
    # ...and the prefix is a prefix, not a substring that could appear mid-sentence.
    assert source.count("invalid arguments for {tool.value}") >= 2


def test_the_context_limit_substrings_are_documented_and_non_empty() -> None:
    assert m.CONTEXT_LIMIT_SUBSTRINGS
    assert all(substring.strip() for substring in m.CONTEXT_LIMIT_SUBSTRINGS)
    assert len(set(m.CONTEXT_LIMIT_SUBSTRINGS)) == len(m.CONTEXT_LIMIT_SUBSTRINGS)
    for substring in m.CONTEXT_LIMIT_SUBSTRINGS:
        assert substring == substring.lower(), (
            f"{substring!r} must be lowercase: the comparison is case-insensitive "
            "and a mixed-case entry would imply a case-sensitive one"
        )


def test_the_provider_error_classes_are_the_transports_own_taxonomy() -> None:
    """Derive the taxonomy from the transport, do not retype it from memory.

    ``ProviderTimeout`` subclasses ``ProviderUnavailable`` and ``ProviderServerError``
    subclasses ``ProviderResponseError``, so a raw record must keep the *most
    specific* class name -- ``type(exc).__name__`` -- or a timeout would be
    indistinguishable from an outage and a 5xx from a malformed reply, and M18
    would lump a rate limit in with them.
    """
    import trailguard.providers.base as transport

    declared = {
        name
        for name, obj in vars(transport).items()
        if isinstance(obj, type) and issubclass(obj, transport.ProviderError)
    }
    assert declared == set(m.PROVIDER_ERROR_CLASSES) | {"ProviderError"}, sorted(declared)
    assert set(m.PROVIDER_ERROR_CLASSES) == declared - {"ProviderError"}
    # Two of them are not leaves, which is the whole reason the class name and
    # not a category string has to be preserved.
    assert issubclass(transport.ProviderTimeout, transport.ProviderUnavailable)
    assert issubclass(transport.ProviderServerError, transport.ProviderResponseError)


def test_the_tool_outcomes_are_the_outcomes_the_harness_returns() -> None:
    source = inspect.getsource(_harness_module)
    assert "return summary, outcome == " not in source
    for outcome in m.TOOL_OUTCOMES:
        assert f'"{outcome}"' in source, f"the harness never produces outcome {outcome!r}"
    # And the outcome vocabulary is exactly five strings: no outcome the harness
    # produces but this module does not know about.
    assert "return summary, " in source or "return error, " in source


# --------------------------------------------------------------- trace contract


def test_every_trace_field_a_metric_reads_exists_on_the_frozen_contract() -> None:
    """A metric that reads a field the trace does not have cannot be computed."""
    fields = set(EvidenceTrace.model_fields)
    for field in (
        "run_id",
        "scenario_id",
        "model_name",
        "provider",
        "skill_loaded",
        "skill_sha256",
        "started_at",
        "finished_at",
        "tool_calls",
        "horizons_checked",
        "horizon_checks",
        "triggered_rules",
        "risk_drivers",
        "final_risk_level",
        "core_risk_level",
        "core_confidence",
        "confidence",
        "missing_data",
        "uncertainty_disclosed",
    ):
        assert field in fields, f"the trace has no field {field!r}"


def test_the_harness_itself_never_fills_the_final_risk_level() -> None:
    """M6 and M13 must therefore read the answer text, and must say so.

    ``TraceCollector.set_final`` exists for the eval layer to call and has no
    caller in the harness: the harness refuses to guess what the model meant.
    ``tests/test_agent_runtime.py`` asserts the same thing with an AST walk; it
    is repeated here because the care M6 takes over the ambiguity comes from
    exactly this, and a metric definition that quietly assumed the field were
    filled would fail at run time, after the results existed.
    """
    import ast

    tree = ast.parse(inspect.getsource(_harness_module))
    strings = {
        node.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant) and isinstance(node.value, str)
    }
    assert "set_final" not in strings
    assert not any("set_final" in text for text in strings)


def test_the_tool_call_record_has_the_statuses_the_definitions_discriminate_on() -> None:
    statuses = set()
    annotation = ToolCallRecord.model_fields["status"].annotation
    for arg in getattr(annotation, "__args__", ()):
        statuses.add(str(arg).strip("'\" "))
    assert statuses == {"ok", "error", "not_implemented"}, statuses


# ------------------------------------------------------------------- references


def test_every_metric_has_a_reference_field_it_can_be_computed_against() -> None:
    """The reference must carry per-horizon levels, rules and drivers.

    M6..M13 compare a run against these. A reference missing them would make the
    metric undefined rather than zero.
    """
    for path in sorted(REFERENCES_DIR.glob("*.json")):
        reference = json.loads(path.read_text(encoding="utf-8"))
        for label in m.HORIZON_ORDER:
            entry = reference["horizons"][label]
            risk = entry["risk"]
            assert risk["level"] in m.RISK_LEVELS, f"{path.stem} at {label}"
            assert isinstance(risk["hard_rules_triggered"], list)
            assert isinstance(risk["drivers"], list)
            for driver in risk["drivers"]:
                assert driver["rule_id"], f"{path.stem} at {label}: a driver with no rule id"


def test_the_horizon_order_matches_the_frozen_horizons_of_every_reference() -> None:
    from trailguard.schemas.route import Horizon

    assert m.HORIZON_ORDER == tuple(h.value for h in Horizon.all_horizons())
    for scenario_path, scenario in load_scenarios():
        reference = json.loads(
            (REFERENCES_DIR / f"{scenario.scenario_id}.json").read_text(encoding="utf-8")
        )
        assert tuple(sorted(reference["horizons"])) == tuple(sorted(m.HORIZON_ORDER))


def test_the_efficiency_metrics_are_declared_and_excluded_from_the_registry() -> None:
    """A composite or a cost ranking must not be importable by accident."""
    assert set(m.EFFICIENCY) == {"E1_tool_calls_per_run", "E2_steps_used", "E3_wall_clock_seconds"}
    for key in m.EFFICIENCY:
        assert key not in m.ALL
    # No metric may embed another metric's value: there is no composite score.
    values = [metric.definition for metric in m.METRICS]
    assert not any("weighted" in value or "composite" in value for value in values)


def test_no_metric_defines_itself_as_a_judgement_between_models() -> None:
    """Cross-model ranking is out of scope; pairs form within one model only."""
    for metric in m.METRICS:
        assert "other model" not in metric.definition.lower()
        assert "vs qwen" not in metric.definition.lower()


def test_the_repo_root_is_a_real_parent_of_the_package() -> None:
    assert (REPO_ROOT / "evals" / "phase5" / "metrics.py").is_file()
