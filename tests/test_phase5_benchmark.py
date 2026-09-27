"""The Phase 5 benchmark's pre-registered design is pinned here, before any model runs.

Nothing in this module runs a model, opens a socket or needs a credential. What it
pins is the *design*: that the committed references exist, that they regenerate
byte-for-byte from the frozen assets, that each one agrees with the expectations
its own scenario file declares, and that the coverage the benchmark was built for
is actually present in the set. A benchmark whose design can drift silently cannot
be a measurement of anything, so the drift is a test failure rather than a
footnote in a report.

The ordering matters more than the assertions: this module is written and passing
*before* the first scored model run, which is what makes the results that follow a
test of a pre-registered hypothesis rather than a description of what happened.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from evals.phase5.references import (
    BENCHMARK_SCENARIOS,
    BENCHMARK_VERSION,
    REFERENCES_DIR,
    build_reference,
    load_scenarios,
)
from trailguard.risk_core.calibration import CALIBRATION_PATH
from trailguard.risk_core.hard_rules import DEFAULT_PACK_PATH
from trailguard.routes.geometry import load_route_geometry

from conftest import REPO_ROOT, SCENARIO_FILES

#: The pre-registered coverage floor the brief requires (spec 25, Phase 5).
#: A scenario slot maps to the minimum number of scenarios exercising it; the
#: benchmark is rejected if any row is thin, because a thin row cannot support the
#: claim it was designed to support.
COVERAGE_FLOOR: dict[str, int] = {
    "safe_control": 2,
    "R-A": 1,
    "R-B": 2,
    "R-C": 1,
    "R-D": 1,
    "R-E": 1,
    "R-F": 1,
    "multi_driver": 1,
    "missing_or_degraded": 1,
    "late_hazard": 1,
    "stress": 1,
}

#: Every rule in the frozen pack must be reachable by at least one scenario,
#: otherwise a whole rule is quietly untested and its recall metric is vacuous.
FROZEN_RULES = ("R-A", "R-B", "R-C", "R-D", "R-E", "R-F")

#: The five tools, which must be the entire registry: no helper that answers a
#: hazard directly, and no tool a run could use to skip the horizon loop.
EXPECTED_TOOLS = (
    "get_route_context",
    "project_route_state",
    "get_weather_for_route_time",
    "get_user_device_context",
    "assess_risk",
)


def _references() -> dict[str, dict]:
    out: dict[str, dict] = {}
    for path in sorted(REFERENCES_DIR.glob("*.json")):
        out[path.stem] = json.loads(path.read_text(encoding="utf-8"))
    return out


# --------------------------------------------------------------------- design


def test_the_benchmark_names_a_scenario_that_exists() -> None:
    scenarios = load_scenarios()
    assert [s.scenario_id for _, s in scenarios] == list(BENCHMARK_SCENARIOS)


def test_every_committed_reference_regenerates_identically(tmp_path: Path) -> None:
    """Determinism is checked, not claimed: the same assets give the same bytes."""
    for scenario_path, scenario in load_scenarios():
        fresh = build_reference(scenario_path, scenario)
        committed = REFERENCES_DIR / f"{scenario.scenario_id}.json"
        assert committed.is_file(), f"{scenario.scenario_id} has no committed reference"
        assert committed.read_text(encoding="utf-8") == json.dumps(
            fresh, indent=2, sort_keys=True, ensure_ascii=False
        ) + "\n", f"{scenario.scenario_id} does not regenerate from the committed assets"


def test_no_reference_was_produced_by_a_language_model() -> None:
    for name, reference in _references().items():
        assert reference["llm_involved"] is False
        assert reference["deterministic"] is True
        assert reference["generator"] == "evals/phase5/references.py"


def test_every_reference_covers_the_frozen_four_horizons() -> None:
    for name, reference in _references().items():
        assert sorted(reference["horizons"]) == ["+120min", "+30min", "+60min", "NOW"]
        for label, entry in reference["horizons"].items():
            assert entry["horizon_min"] in (0, 30, 60, 120)
            assert entry["risk"]["level"] in ("GREEN", "YELLOW", "ORANGE", "RED", "INSUFFICIENT_DATA")
            assert isinstance(entry["risk"]["score"], int)
            assert entry["weather"]["freshness_ok"] is not None


def test_the_references_asset_hashes_match_the_files_on_disk() -> None:
    """A rule pack edited without regenerating the references is a broken benchmark."""
    for name, reference in _references().items():
        assert reference["rules_sha256"] == _hash(DEFAULT_PACK_PATH)
        assert reference["calibration_sha256"] == _hash(CALIBRATION_PATH)
        scenario_path = REPO_ROOT / reference["scenario_file"]
        assert reference["scenario_sha256"] == _hash(scenario_path)
        snapshot = REPO_ROOT / reference["weather_snapshot_ref"]
        assert reference["weather_snapshot_sha256"] == _hash(snapshot)
        feed = REPO_ROOT / reference["device_feed_ref"]
        assert reference["device_feed_sha256"] == _hash(feed)
        route = REPO_ROOT / reference["route_ref"]
        assert reference["route_sha256"] == _hash(route)
        # The GPX must still parse: a truncated polyline is a missing route.
        assert load_route_geometry(str(route), route_id="scn_route_sample").point_count > 0


def _hash(path: Path) -> str:
    import hashlib

    return hashlib.sha256(path.read_bytes()).hexdigest()


# ------------------------------------------------------- reference vs scenario


def test_each_reference_agrees_with_the_expectations_its_own_scenario_declares() -> None:
    """The reference and the scenario file must not disagree.

    The frozen suite already pins that every scenario's ``expected_safety_core``
    holds at every horizon through the real core. The benchmark scores runs
    against the reference, so if the two ever diverge the benchmark would be
    grading against an answer the frozen suite rejects -- an inconsistency that
    has to fail here rather than be discovered in the report.
    """
    scenarios = {scenario.scenario_id: scenario for _, scenario in load_scenarios()}
    for name, reference in _references().items():
        scenario = scenarios[name]
        expected = scenario.expected_safety_core
        assert expected is not None, f"{name} declares no expectation"
        for label, entry in reference["horizons"].items():
            risk = entry["risk"]
            if expected.risk_level is not None:
                assert risk["level"] == expected.risk_level.value, f"{name} at {label}"
            assert sorted(risk["hard_rules_triggered"]) == sorted(expected.hard_rules_triggered)
            assert set(d["rule_id"] for d in risk["drivers"]) >= set(expected.drivers_expected)


def test_the_reference_declares_the_five_tool_registry_and_nothing_else() -> None:
    for name, reference in _references().items():
        assert tuple(reference["required_tools"]) == EXPECTED_TOOLS
        # A registry that grew a hazard-answering tool would make the measured
        # capability a property of the harness rather than of the skill.
        assert "run_all_horizons" not in reference["required_tools"]
        assert "forecast_risk" not in reference["required_tools"]


def test_every_reference_records_the_frozen_freshness_budget() -> None:
    for name, reference in _references().items():
        assert reference["freshness_budget_min"] == 180
        for label, entry in reference["horizons"].items():
            assert entry["weather"]["freshness_budget_min"] == 180


# ------------------------------------------------------------------- coverage


def test_the_pre_registered_coverage_floor_is_met() -> None:
    """The benchmark runs the coverage it was designed to run -- no thinner."""
    references = _references()
    assert len(references) == len(BENCHMARK_SCENARIOS)

    measured = {
        "safe_control": _count_safe_control(references),
        "R-A": _count_rule(references, "R-A"),
        "R-B": _count_rule(references, "R-B"),
        "R-C": _count_rule(references, "R-C"),
        "R-D": _count_rule(references, "R-D"),
        "R-E": _count_rule(references, "R-E"),
        "R-F": _count_rule(references, "R-F"),
        "multi_driver": _count_multi_driver(references),
        "missing_or_degraded": _count_degraded(references),
        "late_hazard": _count_settles(references),
        "stress": _count_stress(references),
    }
    for slot, floor in COVERAGE_FLOOR.items():
        assert measured[slot] >= floor, (
            f"coverage slot {slot!r} has {measured[slot]}, the pre-registered floor is {floor}"
        )


def test_every_rule_in_the_pack_is_exercised_by_at_least_one_scenario() -> None:
    references = _references()
    covered = {rule for reference in references.values() for rule in reference["coverage"]["drivers"]}
    covered |= {rule for reference in references.values() for rule in reference["coverage"]["hard_rules"]}
    assert covered == set(FROZEN_RULES), (
        f"rules with no scenario at all: {sorted(set(FROZEN_RULES) - covered)}"
    )


def test_the_benchmark_contains_at_least_one_scenario_where_nothing_fires() -> None:
    """Without a false-alarm probe, a run that invents hazards scores perfectly."""
    references = _references()
    probes = [name for name, r in references.items() if r["expectations_for_evaluator"]["false_alarm_probe"]]
    assert probes, "no scenario has an expectation that nothing fires, so M-false-alarm is unmeasurable"


def test_one_scenario_is_the_documented_stress_case() -> None:
    references = _references()
    stress = [name for name in references if name == "S04_sunset_margin_pace_drop"]
    assert stress, "S04, the pre-registered stress scenario, is missing from the benchmark"


def _count_rule(references: dict[str, dict], rule: str) -> int:
    return sum(1 for r in references.values() if rule in r["coverage"]["drivers"]
               or rule in r["coverage"]["hard_rules"])


def _count_safe_control(references: dict[str, dict]) -> int:
    return sum(1 for r in references.values()
               if r["expectations_for_evaluator"]["false_alarm_probe"]
               and not r["coverage"]["weather_is_stale_at"]
               and not r["coverage"]["missing_data"])


def _count_multi_driver(references: dict[str, dict]) -> int:
    return sum(1 for r in references.values() if len(r["coverage"]["drivers"]) >= 3)


def _count_degraded(references: dict[str, dict]) -> int:
    return sum(1 for r in references.values()
               if r["coverage"]["missing_data"] or r["coverage"]["stale_fields"]
               or r["coverage"]["weather_is_stale_at"])


def _count_settles(references: dict[str, dict]) -> int:
    return sum(1 for r in references.values() if r["expectations_for_evaluator"]["late_hazard_probe"])


def _count_stress(references: dict[str, dict]) -> int:
    return sum(1 for r in references.values()
               if len(r["coverage"]["hard_rules"]) >= 1 and len(r["coverage"]["drivers"]) >= 3)


# ------------------------------------------------------------------ integrity


def test_the_benchmark_scenario_set_is_a_subset_of_the_frozen_scenario_set() -> None:
    """The benchmark runs frozen scenarios; it does not fork the dataset."""
    frozen = {path.name for path in SCENARIO_FILES}
    for reference in _references().values():
        assert Path(reference["scenario_file"]).name in frozen


def test_the_benchmark_version_is_declared_identically_everywhere() -> None:
    for reference in _references().values():
        assert reference["benchmark_version"] == BENCHMARK_VERSION


def test_no_reference_records_a_wall_clock_instant() -> None:
    """Every instant in a reference comes from the scenario, never from the clock."""
    import re

    stamps = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}")
    for name, reference in _references().items():
        for label, entry in reference["horizons"].items():
            arrival = entry["projected_arrival_time"]
            assert stamps.match(arrival), f"{name} at {label}: {arrival!r}"
            analysis = reference["analysis_time"]
            assert arrival >= analysis, (
                f"{name} at {label}: arrival {arrival} precedes analysis {analysis}"
            )


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-v"]))
