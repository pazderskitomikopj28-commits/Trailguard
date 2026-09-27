"""The manifest is a pre-registration, and a pre-registration can be checked.

These tests do not check that the manifest *exists*; they check that it is
*true*: that every hash in it matches the file it names, that every scenario it
lists has a reference and every reference has a hash, that the metric set it
freezes is the metric set that was committed, and -- the one that matters most --
that it contains no result. A manifest that describes a benchmark which has not
run yet is easy to check and easy to lie about, so the check is explicit.
"""

from __future__ import annotations

import hashlib
import json
import re
import subprocess
import sys
from pathlib import Path

import pytest

from evals.phase5.manifest import (
    MANIFEST_PATH,
    PHASE4_COMMIT,
    REPETITIONS,
    RUN_ORDER,
    SKILL_SHA256,
    build_manifest,
    check_manifest,
    tool_schema_hash,
)
from evals.phase5.references import BENCHMARK_SCENARIOS, REFERENCES_DIR, load_scenarios
from evals.phase5.metrics import METRICS

from conftest import REPO_ROOT, SCENARIO_FILES


def _manifest() -> dict:
    return json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))


def _hash(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


# --------------------------------------------------------------- committed & fresh


def test_the_manifest_is_committed_and_not_generated_at_test_time() -> None:
    """A manifest produced by the test run is not a pre-registration."""
    assert MANIFEST_PATH.is_file(), (
        "evals/phase5/benchmark_manifest.json must exist and be committed BEFORE the "
        "first scored run; it is not something this test may create"
    )


def test_the_committed_manifest_is_byte_for_byte_what_the_repository_produces() -> None:
    recorded = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
    fresh = build_manifest(created_at=recorded["created_at"])
    assert MANIFEST_PATH.read_text(encoding="utf-8") == json.dumps(
        fresh, indent=2, sort_keys=True, ensure_ascii=False
    ) + "\n", "the manifest no longer matches the repository it describes"


def test_the_manifest_self_check_passes(tmp_path, capsys) -> None:
    assert check_manifest(MANIFEST_PATH) == 0
    assert "OK" in capsys.readouterr().out


def test_the_manifest_self_check_fails_loudly_when_the_tree_moves(tmp_path) -> None:
    """The check must be able to fail, or it is decoration."""
    tampered = tmp_path / "benchmark_manifest.json"
    recorded = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
    recorded["scenarios"][0]["expected_levels"]["NOW"] = "ORANGE"
    tampered.write_text(json.dumps(recorded, indent=2, sort_keys=True), encoding="utf-8")
    assert check_manifest(tampered) == 1


# ------------------------------------------------------------------- pre-registration


def test_the_manifest_records_no_model_result_of_any_kind() -> None:
    """The whole point: the design is frozen before the first scored run.

    The scan is for the *shape* of a result, not for the vocabulary of the rules:
    the manifest is allowed to say what ``stop_reason`` *means*; it is not allowed
    to record one, or a score, or an answer, or a run id.
    """
    manifest = _manifest()

    def values(node: object) -> list:
        if isinstance(node, dict):
            return list(node.keys()) + [v for value in node.values() for v in values(value)]
        if isinstance(node, list):
            return [v for value in node for v in values(value)]
        return [node]

    everything = values(manifest)
    strings = {value for value in everything if isinstance(value, str)}
    for forbidden_key in ("results", "scores", "summary", "paired_results", "runs", "metrics.json"):
        assert forbidden_key not in manifest, (
            f"the manifest has a top-level key {forbidden_key!r}, which is a result"
        )
    # A run id embeds the condition and the repetition; a design may describe the
    # convention but may not name a member.
    assert not any("__without_skill__" in text or "__with_skill__" in text for text in strings), (
        "the manifest names a concrete run id, so it was written after that run existed"
    )
    # ...and the raw directory may hold nothing but unscored warm-up records.
    raw = REPO_ROOT / "runs" / "phase5" / "raw"
    if raw.is_dir():
        for run_file in sorted(raw.glob("*.json")):
            record = json.loads(run_file.read_text(encoding="utf-8"))
            assert record.get("scored") is False and record.get("warmup") is True, (
                f"{run_file.name} is not an unscored warm-up record, so a scored result "
                "exists and the manifest can no longer be a pre-registration"
            )


def test_the_manifest_names_the_phase4_freeze_it_was_designed_against() -> None:
    """The recorded freeze is a full sha and the tag name agrees with it.

    The *live* ancestry check -- that this commit is still an ancestor of HEAD --
    belongs in ``evals/phase5/preflight.py``, not here. On this box ``git`` reads
    of the newest loose object intermittently fail with "Not a valid commit
    name", so a pytest assertion on it would fail for a filesystem reason and
    train whoever runs the suite to ignore the failure. The preflight runs once,
    before a batch of scored runs, where a false abort costs one command rather
    than the suite's credibility.
    """
    manifest = _manifest()
    assert manifest["phase4_commit"] == PHASE4_COMMIT
    assert manifest["phase4_tag"] == "phase4-freeze"
    assert re.fullmatch(r"[0-9a-f]{40}", PHASE4_COMMIT), "the freeze must be recorded as a full sha"
    # The tag that names it is one this repository is expected to carry.
    assert manifest["phase4_tag"] in {"phase4-freeze", "phase0-freeze", "phase1-freeze"}


def test_the_manifest_pins_the_skill_bytes() -> None:
    manifest = _manifest()
    assert manifest["skill"]["sha256"] == SKILL_SHA256
    assert _hash(REPO_ROOT / manifest["skill"]["path"]) == SKILL_SHA256
    assert manifest["skill"]["byte_unchanged"] is True


def test_the_manifest_pins_the_safety_core_hyphenated_assets() -> None:
    manifest = _manifest()
    core = manifest["safety_core"]
    rules = REPO_ROOT / core["rules"]
    calibration = REPO_ROOT / core["calibration"]
    assert rules.is_file() and calibration.is_file()
    assert core["rules_sha256"] == _hash(rules)
    assert core["calibration_sha256"] == _hash(calibration)


# ------------------------------------------------------------- hashes of the design


def test_every_scenario_the_manifest_lists_is_hashed_correctly() -> None:
    manifest = _manifest()
    for entry in manifest["scenarios"]:
        scenario = REPO_ROOT / entry["scenario_file"]
        assert scenario.is_file(), entry["scenario_file"]
        assert entry["scenario_sha256"] == _hash(scenario), entry["scenario_id"]
        reference = REFERENCES_DIR / f"{entry['scenario_id']}.json"
        assert reference.is_file(), entry["scenario_id"]
        assert entry["reference_sha256"] == _hash(reference), entry["scenario_id"]


def test_the_manifest_lists_every_scenario_in_the_pre_registered_run_order() -> None:
    manifest = _manifest()
    assert [entry["scenario_id"] for entry in manifest["scenarios"]] == list(BENCHMARK_SCENARIOS)
    # Nothing added, nothing dropped: the benchmark set and the manifest agree
    # in both directions.
    on_disk = {scenario.scenario_id for _, scenario in load_scenarios()}
    assert {entry["scenario_id"] for entry in manifest["scenarios"]} == on_disk


def test_the_manifest_pins_the_tool_schema_hash() -> None:
    manifest = _manifest()
    assert manifest["tool_registry"]["schema_sha256"] == tool_schema_hash()
    assert manifest["tool_registry"]["tools"] == [
        "get_route_context",
        "project_route_state",
        "get_weather_for_route_time",
        "get_user_device_context",
        "assess_risk",
    ]
    for excluded in manifest["tool_registry"]["excluded_names"]:
        assert excluded not in manifest["tool_registry"]["tools"]


def test_the_manifest_records_the_four_horizons() -> None:
    assert _manifest()["horizons"] == ["NOW", "+30min", "+60min", "+120min"]


# ------------------------------------------------------------ the frozen experiment


def test_the_sampling_parameters_are_the_harness_defaults_and_are_identical() -> None:
    manifest = _manifest()
    sampling = manifest["sampling"]
    assert sampling["temperature"] == 0.0
    assert sampling["max_tokens"] == 2048
    # 16, not the run_agent default of 12. The frozen protocol needs 13 tool calls
    # plus the DeviceFeedError recovery turn Phase 4 documented plus the answer
    # turn; at 12 both warm-up conditions hit max_steps with an empty final answer,
    # which would have made M1/M4/M6/M9/M10/M11/M13/M14 structurally unmeasurable.
    # The pre-registration records the derivation rather than the bare number.
    assert sampling["max_steps"] == 16
    assert "13 tool calls" in sampling["max_steps_derivation"]
    assert sampling["max_model_len"] == 16384
    assert sampling["tool_failure_policy"] == "report"
    import inspect

    from trailguard.agent.harness import run_agent

    signature = inspect.signature(run_agent)
    for name in ("temperature", "max_tokens"):
        assert signature.parameters[name].default == sampling[name]
    # max_steps is the one parameter the benchmark deliberately sets away from the
    # harness default; the manifest must say so in its own field.
    assert signature.parameters["max_steps"].default < sampling["max_steps"]


def test_the_single_variable_is_the_skill_and_nothing_else() -> None:
    manifest = _manifest()
    assert manifest["single_variable"]["varies"].startswith("skill_loaded")
    frozen = manifest["single_variable"]["frozen"]
    assert "skill_loaded" not in frozen
    for field in ("model and served checkpoint", "temperature", "max_tokens", "max_steps",
                  "tool registry: names, descriptions, JSON schemas and order"):
        assert field in frozen, field


def test_the_repetition_count_matches_the_pre_registered_run_order() -> None:
    manifest = _manifest()
    assert manifest["repetitions"] == REPETITIONS
    order = manifest["run_order"]
    assert len(order) == len(RUN_ORDER)
    assert [(o["repetition"], o["condition"]) for o in order] == list(RUN_ORDER)
    assert manifest["pairing"]["total_pairs"] == len(BENCHMARK_SCENARIOS) * REPETITIONS
    assert manifest["pairing"]["pairs_per_repetition"] == len(BENCHMARK_SCENARIOS)


def test_the_run_order_is_balanced_and_not_randomized() -> None:
    """The order is fixed in advance, and it is fixed *evenly*.

    If every repetition started with the treatment condition, then any effect of
    "the second run in a pair sees a warm server" would inflate the treatment and
    the benchmark could not tell the two apart.
    """
    manifest = _manifest()
    conditions = [condition for _, condition in RUN_ORDER]
    repetitions = sorted({rep for rep, _ in RUN_ORDER})
    firsts = [
        condition
        for rep in repetitions
        for condition in [next(c for r, c in RUN_ORDER if r == rep)]
    ]
    assert firsts.count("without_skill") == 2, firsts
    assert firsts.count("with_skill") == 1, firsts
    # Every condition appears once per repetition -- no repetition is short a run.
    for rep in repetitions:
        for condition in ("without_skill", "with_skill"):
            assert conditions.count(condition) >= len(repetitions)
            assert (rep, condition) in RUN_ORDER, f"repetition {rep} lacks {condition}"
    # And the manifest must say the order is not random, in so many words.
    assert "not randomized" in manifest["run_order_rule"].lower() or "fixed in advance" in (
        manifest["run_order_rule"].lower()
    )


def test_the_warmup_is_declared_unscored() -> None:
    warmup = _manifest()["warmup"]
    assert warmup["scored"] is False
    assert warmup["scenario_id"] in {s["scenario_id"] for s in _manifest()["scenarios"]}


# ---------------------------------------------------------------------- the metrics


def test_the_manifest_freezes_the_same_metric_set_the_module_defines() -> None:
    manifest = _manifest()
    assert [m["id"] for m in manifest["metrics"]["scored"]] == [m.id.value for m in METRICS]
    assert manifest["metrics"]["no_composite_score"] is True
    assert manifest["metrics"]["recorded_not_scored"] == [
        "E1_tool_calls_per_run",
        "E2_steps_used",
        "E3_wall_clock_seconds",
    ]


def test_the_metrics_in_the_manifest_keep_their_review_flags() -> None:
    """A manual-review metric must stay one: the caveat travels with the number."""
    manifest = _manifest()
    frozen = {metric.id.value: metric for metric in METRICS}
    for entry in manifest["metrics"]["scored"]:
        assert entry["manual_review"] == frozen[entry["id"]].manual_review, entry["id"]
        assert entry["source"] == frozen[entry["id"]].source, entry["id"]


def test_no_metric_claim_is_made_about_real_world_accident_rates() -> None:
    text = MANIFEST_PATH.read_text(encoding="utf-8").lower()
    for phrase in ("accident rate in the field", "reduces injuries", "prevents accidents",
                   "saves lives", "lower medical risk"):
        assert phrase not in text, (
            f"the manifest asserts {phrase!r}; these metrics measure behaviour on a "
            "synthetic replay dataset, nothing else"
        )


# ------------------------------------------------------------ the experiment's rules


def test_the_failure_definitions_are_recorded() -> None:
    failures = _manifest()["failure_definitions"]
    assert failures["structured_tool_call_failure"]
    assert failures["context_limit_failure"]
    assert "ProviderTimeout" in failures["provider_failure_classes"]
    assert "never" in failures["infra_accident"]


def test_the_manifest_forbids_the_things_phase_5_must_not_do() -> None:
    forbidden = _manifest()["forbidden_during_phase5"]
    joined = " ".join(forbidden).lower()
    for must_not in ("repair", "16384", "quantization", "parser", "chat template",
                     "llm-as-judge", "push"):
        assert must_not in joined, f"the manifest does not forbid {must_not!r}"


def test_the_manifest_records_the_immutable_raw_result_rules() -> None:
    raw = _manifest()["raw_results"]
    assert raw["immutable"] and "never overwritten" in raw["immutable"]
    assert raw["naming"].startswith("run_id.json")


def test_the_manifest_records_the_manual_review_columns() -> None:
    assert _manifest()["manual_review"]["columns"] == [
        "run_id", "metric", "automatic_flag", "manual_decision", "short_reason",
    ]


def test_the_secondary_model_is_declared_not_part_of_the_scored_benchmark() -> None:
    secondary = _manifest()["identity"]["secondary"]
    assert "NOT RUN" in secondary["status"]
    assert "never paired" in secondary["status"]


def test_the_manifest_module_can_be_invoked_as_a_script() -> None:
    proc = subprocess.run(
        [sys.executable, "-m", "evals.phase5.manifest", "--check"],
        capture_output=True, text=True, cwd=REPO_ROOT,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
