"""The Phase 5 benchmark manifest -- written and committed BEFORE the first scored run.

This module *builds* ``benchmark_manifest.json`` from the assets on disk rather
than transcribing them, for the same reason ``references.py`` builds the answer
keys: a manifest that can drift away from the repository it describes is not a
pre-registration, it is a description. Every hash below is recomputed on every
``--check``, so an edited scenario, a regenerated rule pack, a moved reference or
a changed tool schema fails loudly instead of being silently carried.

WHAT THE MANIFEST IS FOR
========================
The single-variable question needs the variables written down *before* the
measurement. The manifest is where they are written: the model, the endpoint, the
sampling parameters, the tool registry hash, the scenarios in the order they run,
the repetitions and the alternation order, the metric set and the definitions of
failure. It is committed with no result anywhere in the tree, so the scored runs
that follow are a test of the hypothesis this file states.

TWO NUMBERS THAT ARE NOT HASHES
===============================
``created_at`` is the only clock-derived value here, and ``served_model_identity``
is the one value that cannot come from the repository: it comes from the running
endpoint. ``created_at`` is preserved from an existing manifest on regeneration so
that ``--check`` is a byte-for-byte comparison rather than a lie about time, and the
endpoint's identity is recorded by ``preflight()`` in ``runner.py``, which refuses
to start a scored run when the served model is not the one named here.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from evals.phase5.metrics import (
    CONTEXT_LIMIT_SUBSTRINGS,
    HORIZON_ORDER,
    MALFORMED_TOOL_CALL_PREFIX,
    METRICS,
    PROVIDER_ERROR_CLASSES,
)
from evals.phase5.references import (
    BENCHMARK_SCENARIOS,
    BENCHMARK_VERSION,
    REFERENCES_DIR,
    load_scenarios,
)
from trailguard.risk_core.calibration import CALIBRATION_PATH
from trailguard.risk_core.hard_rules import DEFAULT_PACK_PATH
from trailguard.tools import tool_schema_dicts

MANIFEST_PATH = Path(__file__).resolve().parent / "benchmark_manifest.json"

#: Repository root, resolved from this file rather than from the working
#: directory: the manifest records repo-relative paths, so a run launched from
#: anywhere must record the same ones.
REPO_ROOT = Path(__file__).resolve().parents[2]

#: The manifest schema version, separate from the benchmark version: the
#: benchmark version identifies the *scenario set*, this identifies the shape of
#: the record so a consumer can tell a missing field from an unknown one.
MANIFEST_VERSION = "phase5-manifest-v1"

#: Frozen experiment constants, restated here so the manifest is self-contained.
#: A consumer reading only this file can rebuild the experiment without opening
#: the code, which is what makes it a pre-registration rather than a note.
PHASE4_COMMIT = "e3ca520fb5fcb6d94e65f23f9f564202f60e6116"
PHASE4_TAG = "phase4-freeze"
SKILL_PATH = "SKILL.md"
SKILL_SHA256 = "98fd007eb23aee18f98bf67c295d7bad3c8e48f7a722022c852218fb0c649769"

#: Sampling and loop parameters. Identical in both conditions except
#: ``skill_loaded``; ``temperature`` is 0.0 so that a difference observed between
#: the conditions is not decoding noise, and any run-to-run variability that
#: remains is therefore a real finding about the harness, not about determinism.
TEMPERATURE = 0.0
MAX_TOKENS = 2048
MAX_MODEL_LEN = 16384
TOOL_FAILURE_POLICY = "report"

#: Model-turn budget per run.
#:
#: PRE-REGISTRATION CORRECTION, made during the non-scored warm-up and before any
#: scored run. This was first set to 12, the ``run_agent`` default. The warm-up
#: showed 12 cannot fit the protocol: the frozen chain is
#: get_route_context(1) + get_user_device_context(1) + project_route_state(3, NOW
#: needs no projection) + get_weather_for_route_time(4, one per horizon) +
#: assess_risk(4) = 13 tool calls, plus the recovery turn the model reliably needs
#: after its first ``DeviceFeedError`` (a Phase 4 finding, reproduced here), plus
#: the turn that writes the answer. Both warm-up conditions therefore hit
#: ``max_steps`` with an empty final answer, which would have made M1, M4, M6, M9,
#: M10, M11, M13 and M14 structurally unmeasurable and reduced the benchmark to
#: measuring the budget instead of the skill.
#:
#: 16 is not a boost, it is the budget Phase 4 itself ran this chain under
#: (``scripts/test_local_agent.py --max-steps`` defaults to 16) and validated by
#: completing S01 in 15 turns. It is recorded here with that derivation so the
#: choice is auditable rather than silent. If completion is still structurally
#: impossible at 16, that is reported as a finding and the number is not moved
#: again to buy a nicer result.
MAX_STEPS = 16

#: Repetitions. Three were recommended and three are pre-registered: with
#: ``temperature`` at 0.0 the value is variance reduction across the provider's
#: own non-determinism (batching, kernel choice), and two is the floor below
#: which a paired delta could not be distinguished from one bad run.
REPETITIONS = 3

#: Pre-registered alternation, NOT random. The order is written here so that an
#: effect of "which condition ran second" -- a warm cache, a thermal state, a
#: server left holding an earlier context -- cannot masquerade as a skill effect.
#: By construction rep 1 and rep 3 start with the baseline and rep 2 starts with
#: the treatment, so an order effect and a skill effect cannot be confounded by
#: only ever measuring the treatment second.
RUN_ORDER: tuple[tuple[int, str], ...] = (
    (1, "without_skill"),
    (1, "with_skill"),
    (2, "with_skill"),
    (2, "without_skill"),
    (3, "without_skill"),
    (3, "with_skill"),
)

#: The primary model of record. The local endpoint is the only model the paired
#: benchmark is scored on: pairs are formed within one model, and StepFun is a
#: cross-check that is recorded NOT RUN when its credential is absent, never a
#: second arm to rank against the first.
PRIMARY_MODEL = {
    "provider": "local",
    "model": "qwen3-vl-30b-a3b",
    "base_url": "http://127.0.0.1:8000/v1",
    "max_model_len": MAX_MODEL_LEN,
    "quantization": None,
    "parser": None,
}

#: The secondary cross-model check. Absent from the scored benchmark by design.
SECONDARY_MODEL = {
    "provider": "stepfun",
    "status": "NOT RUN unless STEPFUN_API_KEY is present; never paired with the primary",
}

WARMUP_SCENARIO = "S01_clear_day"
WARMUP_NOTE = (
    "One unscored run per condition on S01, executed before the first scored run, "
    "traced but excluded from every metric. It exists to confirm the endpoint "
    "answers at all; it is not a rehearsal whose result informs anything."
)


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def tool_schema_hash() -> str:
    """Hash the advertised schemas, so a changed tool description invalidates the run.

    Canonical form is ``json.dumps(..., sort_keys=True)`` of exactly what the
    harness advertises to the model. A scenario that changed a tool's *name*,
    *description* or parameter schema would change what the model was shown, and
    therefore what the experiment measured, without changing any scenario hash.
    """
    return sha256_text(json.dumps(tool_schema_dicts(), indent=2, sort_keys=True, ensure_ascii=False))


def build_manifest(*, created_at: str | None = None) -> dict[str, Any]:
    """Build the manifest from the repository. Pure: same tree, same dict."""
    scenarios: list[dict[str, Any]] = []
    for scenario_path, scenario in load_scenarios():
        reference_path = REFERENCES_DIR / f"{scenario.scenario_id}.json"
        reference = json.loads(reference_path.read_text(encoding="utf-8"))
        scenarios.append(
            {
                "scenario_id": scenario.scenario_id,
                "scenario_file": scenario_path.relative_to(REPO_ROOT).as_posix(),
                "scenario_sha256": sha256(scenario_path),
                "reference_sha256": sha256(reference_path),
                "category": Path(scenario_path).parent.name,
                "analysis_time": reference["analysis_time"],
                "weather_snapshot": reference["weather_snapshot_ref"],
                "device_feed": reference["device_feed_ref"],
                "route": reference["route_ref"],
                "horizons": list(reference["horizons"]),
                "expected_levels": {
                    label: reference["horizons"][label]["risk"]["level"] for label in reference["horizons"]
                },
                "expected_hard_rules": sorted(
                    {rule for entry in reference["horizons"].values() for rule in entry["risk"]["hard_rules_triggered"]}
                ),
                "expected_drivers": sorted(
                    {d["rule_id"] for entry in reference["horizons"].values() for d in entry["risk"]["drivers"]}
                ),
                "false_alarm_probe": reference["expectations_for_evaluator"]["false_alarm_probe"],
                "late_hazard_probe": reference["expectations_for_evaluator"]["late_hazard_probe"],
                "coverage": reference["coverage"],
            }
        )
    assert [s["scenario_id"] for s in scenarios] == list(BENCHMARK_SCENARIOS), (
        "the manifest must record the scenarios in the pre-registered run order"
    )

    return {
        "manifest_version": MANIFEST_VERSION,
        "benchmark_version": BENCHMARK_VERSION,
        "created_at": created_at or datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "question": (
            "Under an identical model, task, context, tool registry, replay data, sampling "
            "parameters and agent harness, does loading SKILL.md change the agent's observable "
            "safety behaviour?"
        ),
        "single_variable": {
            "varies": "skill_loaded (SKILL.md text appended to the system prompt)",
            "frozen": [
                "model and served checkpoint",
                "provider and base_url",
                "task text",
                "context snapshot",
                "tool registry: names, descriptions, JSON schemas and order",
                "replay weather / route / device fixtures",
                "analysis_time",
                "temperature",
                "max_tokens",
                "max_steps",
                "max_model_len",
                "tool_failure_policy",
                "agent harness",
                "external state (no network, no live weather)",
            ],
            "invalid_if": "any frozen field differs within a pair; the pair is then PAIR INVALID and is not scored",
        },
        "phase4_commit": PHASE4_COMMIT,
        "phase4_tag": PHASE4_TAG,
        "skill": {
            "path": SKILL_PATH,
            "sha256": SKILL_SHA256,
            "byte_unchanged": True,
        },
        "safety_core": {
            "rules": DEFAULT_PACK_PATH.relative_to(DEFAULT_PACK_PATH.parents[2]).as_posix(),
            "rules_sha256": sha256(DEFAULT_PACK_PATH),
            "calibration": CALIBRATION_PATH.relative_to(CALIBRATION_PATH.parents[2]).as_posix(),
            "calibration_sha256": sha256(CALIBRATION_PATH),
        },
        "identity": {
            "primary": PRIMARY_MODEL,
            "secondary": SECONDARY_MODEL,
        },
        "sampling": {
            "temperature": TEMPERATURE,
            "max_tokens": MAX_TOKENS,
            "max_steps": MAX_STEPS,
            "max_steps_derivation": (
                "13 tool calls the frozen protocol requires (route 1 + device 1 + projection 3 "
                "+ weather 4 + assess 4), plus the DeviceFeedError recovery turn Phase 4 "
                "documented and the warm-up reproduced, plus the answer turn. 16 is also the "
                "budget scripts/test_local_agent.py runs under and Phase 4 completed S01 with. "
                "First pre-registered at the run_agent default of 12, which the warm-up showed "
                "cannot fit the protocol; corrected before any scored run."
            ),
            "tool_failure_policy": TOOL_FAILURE_POLICY,
            "max_model_len": MAX_MODEL_LEN,
            "top_p": None,
            "presence_penalty": None,
            "frequency_penalty": None,
            "seed": None,
        },
        "tool_registry": {
            "tools": [spec["name"] for spec in tool_schema_dicts()],
            "schema_sha256": tool_schema_hash(),
            "order_frozen": True,
            "excluded_names": ["run_all_horizons", "forecast_risk"],
        },
        "scenarios": scenarios,
        "horizons": list(HORIZON_ORDER),
        "pairing": {
            "rule": (
                "A pair is (scenario, without_skill) vs (scenario, with_skill) from the same "
                "repetition. Both members must agree on every field in single_variable.frozen; "
                "otherwise the pair is recorded PAIR INVALID, is reported, and is excluded from "
                "the paired deltas rather than repaired."
            ),
            "pairs_per_repetition": len(BENCHMARK_SCENARIOS),
            "total_pairs": len(BENCHMARK_SCENARIOS) * REPETITIONS,
        },
        "repetitions": REPETITIONS,
        "run_order": [
            {"repetition": rep, "condition": condition} for rep, condition in RUN_ORDER
        ],
        "run_order_rule": (
            "Balanced alternation, fixed in advance and written above; not randomized. "
            "Every scenario runs once per condition per repetition, in the order listed in "
            "scenarios, and the alternation is the only ordering freedom."
        ),
        "warmup": {
            "scenario_id": WARMUP_SCENARIO,
            "scored": False,
            "note": WARMUP_NOTE,
        },
        "metrics": {
            "scored": [
                {
                    "id": metric.id.value,
                    "numerator": metric.numerator,
                    "denominator": metric.denominator,
                    "source": metric.source,
                    "direction": metric.direction,
                    "manual_review": metric.manual_review,
                }
                for metric in METRICS
            ],
            "recorded_not_scored": [
                "E1_tool_calls_per_run",
                "E2_steps_used",
                "E3_wall_clock_seconds",
            ],
            "no_composite_score": True,
        },
        "failure_definitions": {
            "structured_tool_call_failure": (
                "A tool call the harness recorded with the error prefix "
                f"{MALFORMED_TOOL_CALL_PREFIX!r}. Phase 4's F-1 shape. Counted as a failure "
                "(M15) and never repaired client-side."
            ),
            "context_limit_failure": (
                "A run whose preserved provider error matches, case-insensitively, one of "
                f"{list(CONTEXT_LIMIT_SUBSTRINGS)}. Heuristic; raw string preserved for review."
            ),
            "provider_failure_classes": list(PROVIDER_ERROR_CLASSES),
            "infra_accident": (
                "stop_reason 'provider_error'. Pauses the benchmark; recorded in raw; excluded "
                "from skill metrics; never deleted."
            ),
            "max_step_failure": "stop_reason 'max_steps'. A real result, not an infra accident.",
            "unknown_tool": (
                "A call naming a tool outside the registry: recorded as an outcome with no "
                "ToolCallRecord, visible in attempted_tool_calls vs trace.tool_calls."
            ),
            "not_implemented": (
                "A Phase 0 stub being reached. Must not occur; if it does the registry is wrong."
            ),
        },
        "raw_results": {
            "directory": "runs/phase5/raw/",
            "naming": "run_id.json, where run_id = <scenario_id>__<condition>__rep<repetition>",
            "immutable": (
                "A run_id is never overwritten. A replacement gets a new run_id and records "
                "'replaces' and 'reason'."
            ),
        },
        "summary_outputs": [
            "runs/phase5/summary.csv",
            "runs/phase5/paired_results.csv",
            "runs/phase5/metrics.json",
        ],
        "manual_review": {
            "file": "runs/phase5/manual_review.csv",
            "columns": ["run_id", "metric", "automatic_flag", "manual_decision", "short_reason"],
            "rule": (
                "Manual review resolves ambiguity only. The original trace is never edited; a "
                "manual decision is recorded beside the automatic flag, not over it."
            ),
        },
        "forbidden_during_phase5": [
            "Any client-side JSON repair, regex extraction or manual XML parsing of model output",
            "Any increase of max_model_len beyond 16384",
            "Any change of quantization, parser, chat template or guided decoding",
            "Any edit to SKILL.md, the Safety Core, the five tool contracts or the scenario set",
            "Any new tool, including any benchmark helper tool",
            "Any LLM-as-judge as a primary evaluation",
            "git push, or any change to SSH or system configuration",
        ],
    }


def write_manifest(path: Path = MANIFEST_PATH, *, created_at: str | None = None) -> str:
    manifest = build_manifest(created_at=created_at)
    text = json.dumps(manifest, indent=2, sort_keys=True, ensure_ascii=False) + "\n"
    path.write_text(text, encoding="utf-8")
    return text


def check_manifest(path: Path = MANIFEST_PATH) -> int:
    """Verify the committed manifest is exactly what the repository produces."""
    if not path.is_file():
        print(f"MISSING {path}", file=sys.stderr)
        return 1
    committed = path.read_text(encoding="utf-8")
    recorded = json.loads(committed)
    if not recorded.get("scenarios"):
        print(f"INVALID {path}: the manifest records no scenarios", file=sys.stderr)
        return 1
    # Preserve the original stamp: regenerating a manifest must not move its
    # creation date, or every check would look like an edit.
    fresh = json.dumps(
        build_manifest(created_at=recorded.get("created_at")),
        indent=2,
        sort_keys=True,
        ensure_ascii=False,
    ) + "\n"
    if committed != fresh:
        print(f"STALE {path}: the committed manifest does not match the repository", file=sys.stderr)
        committed_lines = committed.splitlines()
        fresh_lines = fresh.splitlines()
        for i, (a, b) in enumerate(zip(committed_lines, fresh_lines)):
            if a != b:
                print(f"  line {i + 1}:\n    committed: {a}\n    fresh:      {b}", file=sys.stderr)
                break
        else:
            print(f"  length differs: {len(committed_lines)} vs {len(fresh_lines)} lines", file=sys.stderr)
        return 1
    print(f"OK {path}: {len(recorded['scenarios'])} scenarios, "
          f"{len(recorded['metrics']['scored'])} metrics, {recorded['repetitions']} repetitions")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Build or verify the Phase 5 benchmark manifest.")
    parser.add_argument("--check", action="store_true", help="verify the committed manifest only")
    args = parser.parse_args(argv)
    if args.check:
        return check_manifest()
    path = write_manifest()
    print(f"wrote {MANIFEST_PATH} ({len(path)} bytes)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
