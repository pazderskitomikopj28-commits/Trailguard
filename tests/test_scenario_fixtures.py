"""Scenario dataset tests for the Replay Mode fixtures (spec sections 13, 17).

Responsibility: prove that every YAML file under ``scenarios/`` parses into the
frozen :class:`Scenario` contract, that its expectations use valid registry and
horizon values, that each fixture is explicitly synthetic with a
``simulation_note``, and that the declared ``category`` matches the directory the
file lives in.

Explicit non-responsibility: nothing here runs a scenario, scores it or asserts
what risk level it deserves. The pinned ``expected_safety_core`` values are
data for the Phase 5 benchmark and the Phase 1 engine, and these tests only
check that they are well-formed rule identifiers.
"""

from __future__ import annotations

import re
from datetime import datetime
from pathlib import Path

import pytest
import yaml

from conftest import (
    EXPECTED_RULE_IDS,
    HORIZON_VALUES,
    REPO_ROOT,
    SCENARIOS_DIR,
    SCENARIO_FILES,
    make_context_dict,
)

from trailguard.schemas import (
    DataFreshnessStatus,
    ForbiddenBehavior,
    Horizon,
    Scenario,
    ScenarioCategory,
    ToolName,
    WeatherForRouteTime,
)

#: Directory name -> dataset partition (spec section 24 layout).
CATEGORY_BY_DIRECTORY: dict[str, ScenarioCategory] = {
    "baseline": ScenarioCategory.BASELINE,
    "edge_cases": ScenarioCategory.EDGE_CASES,
    "compound": ScenarioCategory.COMPOUND,
}


def _load_scenario(path: Path) -> Scenario:
    """Parse one scenario YAML file into the frozen contract."""
    payload = yaml.safe_load(path.read_text(encoding="utf-8"))
    return Scenario.model_validate(payload)


def _header_comment(path: Path) -> str:
    """The fixture's leading comment block, joined into one narrative line.

    YAML wraps a long comment across lines and prefixes every line with ``#``;
    joining them (and dropping the markers) lets a test search the *sentence* a
    fixture author actually wrote instead of the wrapping.
    """
    lines: list[str] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped.startswith("#"):
            if lines:  # the header block ended at the first non-comment line
                break
            continue
        lines.append(stripped.lstrip("#").strip())
    return " ".join(part for part in lines if part)


# --------------------------------------------------------------------------- #
# Dataset shape
# --------------------------------------------------------------------------- #


def test_scenario_dataset_is_not_empty() -> None:
    """The v1 dataset ships at least one scenario per partition (spec 13)."""
    assert SCENARIO_FILES, "no scenario YAML files found under scenarios/"
    covered = {path.parent.name for path in SCENARIO_FILES}
    assert set(CATEGORY_BY_DIRECTORY) <= covered, f"missing partitions: {set(CATEGORY_BY_DIRECTORY) - covered}"


def test_scenario_ids_are_unique() -> None:
    """Scenario ids are the join key of the benchmark; they must not collide."""
    ids = [_load_scenario(path).scenario_id for path in SCENARIO_FILES]

    assert len(ids) == len(set(ids))


@pytest.mark.parametrize("path", SCENARIO_FILES, ids=lambda path: str(path.relative_to(SCENARIOS_DIR.parent)))
def test_scenario_parses_into_the_contract(path: Path) -> None:
    """Every fixture parses into a ``Scenario`` with a usable initial context."""
    scenario = _load_scenario(path)

    assert scenario.scenario_id
    assert scenario.description.strip()
    assert scenario.task_prompt.strip()
    assert -90.0 <= scenario.initial_context.position.value.lat <= 90.0
    assert -180.0 <= scenario.initial_context.position.value.lon <= 180.0
    assert 0.0 <= scenario.initial_context.route.value.route_progress <= 1.0


# --------------------------------------------------------------------------- #
# Expectations use valid registry / horizon / behaviour values
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("path", SCENARIO_FILES, ids=lambda path: path.name)
def test_expected_tools_are_valid_tool_names(path: Path) -> None:
    """``expected_required_tools`` holds only the five registered tools."""
    scenario = _load_scenario(path)

    assert scenario.expected_required_tools, "a scenario must declare its required tools"
    for tool in scenario.expected_required_tools:
        assert isinstance(tool, ToolName)
        assert tool in ToolName


@pytest.mark.parametrize("path", SCENARIO_FILES, ids=lambda path: path.name)
def test_expected_horizons_are_valid(path: Path) -> None:
    """``expected_required_horizons`` holds only the four frozen horizons."""
    scenario = _load_scenario(path)

    assert scenario.expected_required_horizons, "a scenario must declare its required horizons"
    for horizon in scenario.expected_required_horizons:
        assert isinstance(horizon, Horizon)
        assert horizon.value in HORIZON_VALUES


@pytest.mark.parametrize("path", SCENARIO_FILES, ids=lambda path: path.name)
def test_forbidden_behaviors_are_valid(path: Path) -> None:
    """``forbidden_behaviors`` holds only values from the frozen vocabulary."""
    scenario = _load_scenario(path)

    assert scenario.forbidden_behaviors
    for behaviour in scenario.forbidden_behaviors:
        assert isinstance(behaviour, ForbiddenBehavior)


@pytest.mark.parametrize("path", SCENARIO_FILES, ids=lambda path: path.name)
def test_expected_safety_core_references_real_rules(path: Path) -> None:
    """Pinned hard rules and drivers are ids from the v1 rule pack."""
    scenario = _load_scenario(path)
    expected = scenario.expected_safety_core

    if expected is None:
        return

    for rule_id in expected.hard_rules_triggered:
        assert rule_id in EXPECTED_RULE_IDS
    for rule_id in expected.drivers_expected:
        assert rule_id in EXPECTED_RULE_IDS


# --------------------------------------------------------------------------- #
# Synthetic labelling (spec 27) and partition metadata
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("path", SCENARIO_FILES, ids=lambda path: path.name)
def test_every_fixture_is_synthetic_with_a_simulation_note(path: Path) -> None:
    """Every fixture declares itself synthetic and discloses the simulation."""
    scenario = _load_scenario(path)

    assert scenario.is_synthetic is True
    note = scenario.simulation_note.strip()
    assert note, "simulation_note is the disclosure that keeps this fixture honest"
    assert "synthetic" in note.lower()
    assert "not a real" in note.lower()


@pytest.mark.parametrize("path", SCENARIO_FILES, ids=lambda path: path.name)
def test_category_matches_the_directory(path: Path) -> None:
    """The declared partition equals the directory the fixture lives in."""
    scenario = _load_scenario(path)
    partition = CATEGORY_BY_DIRECTORY.get(path.parent.name)

    assert partition is not None, f"{path} lives outside the frozen partitions"
    assert scenario.category is partition


@pytest.mark.parametrize("path", SCENARIO_FILES, ids=lambda path: path.name)
def test_scenario_context_id_matches_the_scenario(path: Path) -> None:
    """The embedded context snapshot belongs to the scenario that carries it."""
    scenario = _load_scenario(path)

    assert scenario.initial_context.scenario_id == scenario.scenario_id


# --------------------------------------------------------------------------- #
# Replay Mode references (spec 17)
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("path", SCENARIO_FILES, ids=lambda path: path.name)
def test_route_ref_points_at_a_replay_asset(path: Path) -> None:
    """``route_ref`` resolves to a GPX under ``replay/routes/``."""
    scenario = _load_scenario(path)
    route = REPO_ROOT / scenario.route_ref

    assert route.is_file(), f"{scenario.scenario_id}: missing route asset {route}"
    assert route.suffix == ".gpx"
    assert route.parent == REPO_ROOT / "replay" / "routes"


@pytest.mark.parametrize("path", SCENARIO_FILES, ids=lambda path: path.name)
def test_weather_snapshot_ref_resolves_when_present(path: Path) -> None:
    """A weather snapshot reference points at an existing replay asset."""
    scenario = _load_scenario(path)

    if scenario.weather_snapshot_ref is None:
        pytest.skip("scenario declares no weather snapshot (unavailable/stale weather)")
    snapshot = REPO_ROOT / scenario.weather_snapshot_ref
    assert snapshot.is_file(), f"{scenario.scenario_id}: missing snapshot {snapshot}"


@pytest.mark.parametrize("path", SCENARIO_FILES, ids=lambda path: path.name)
def test_replay_scenarios_use_replay_weather_policy(path: Path) -> None:
    """``WeatherPolicy.mode`` is ``replay`` for the frozen dataset (spec 17)."""
    scenario = _load_scenario(path)
    policy = scenario.initial_context.weather_policy

    assert policy.mode == "replay"
    assert policy.max_freshness_min > 0


#: Everything a fixture writes about its weather snapshot: from the phrase to the
#: next bullet in the comment block. A YAML header lists facts with ``;`` and
#: ``*``, so the passage is bounded and scoped to the snapshot alone -- the same
#: header legitimately calls a *GPS* fix stale, which says nothing about weather.
_WEATHER_PASSAGE = re.compile(r"weather snapshot\b(?P<passage>(?:(?!\*\s).)*)", re.IGNORECASE)

#: "180-minute freshness budget" or "(budget 180 min)".
_BUDGET_CLAIM = re.compile(
    r"(\d+)\s*-?\s*min(?:ute)?s?\s*(?:freshness\s+)?budget|budget\s+(\d+)\s*min"
)


@pytest.mark.parametrize("path", SCENARIO_FILES, ids=lambda path: path.name)
def test_replay_snapshot_age_and_freshness_are_disclosed_consistently(path: Path) -> None:
    """The snapshot's age, and the verdict that follows from it, are disclosed.

    The fixture's header must state the weather snapshot's age *and* the freshness
    budget it is judged against, and both must be the real ones: the age has to
    equal what ``generated_at`` and ``analysis_time`` imply, and the budget has to
    equal the scenario's own ``max_freshness_min``. The verdict is then derived by
    the contract itself (``WeatherForRouteTime.freshness_status`` on the derived
    age), so a fixture that calls a stale snapshot fresh -- or the reverse --
    contradicts its own replay assets.
    """
    scenario = _load_scenario(path)
    snapshot_ref = scenario.weather_snapshot_ref or scenario.initial_context.weather_policy.snapshot_ref
    if snapshot_ref is None:
        pytest.skip("scenario declares no weather snapshot")

    snapshot = yaml.safe_load((REPO_ROOT / snapshot_ref).read_text(encoding="utf-8"))
    analysis_time = scenario.initial_context.time_state.analysis_time
    budget = scenario.initial_context.weather_policy.max_freshness_min
    generated_at = datetime.fromisoformat(snapshot["generated_at"])
    age_minutes = (analysis_time - generated_at).total_seconds() / 60

    weather = WeatherForRouteTime.model_validate(
        {
            "temperature_c": 12.0,
            "precipitation_probability": 0.05,
            "wind_speed_kmh": 8.4,
            "wind_gust_kmh": 17.5,
            "weather_code": 1,
            "source": "replay",
            "forecast_generated_at": generated_at.isoformat(),
            "target_time": analysis_time.isoformat(),
            "freshness_minutes": age_minutes,
            "confidence": 0.9,
        }
    )
    status = weather.freshness_status(budget)

    passage = _WEATHER_PASSAGE.search(_header_comment(path))
    assert passage is not None, f"{scenario.scenario_id}: header never mentions the weather snapshot"
    about_snapshot = passage.group("passage")

    claimed = re.search(r"(\d+)\s*min(?:ute)?s?\s+old", about_snapshot)
    assert claimed is not None, f"{scenario.scenario_id}: header must state the weather snapshot's age"
    assert int(claimed.group(1)) == int(round(age_minutes)), (
        f"{scenario.scenario_id}: header claims a {claimed.group(1)}-minute-old weather "
        f"snapshot but generated_at implies {int(round(age_minutes))} minutes"
    )

    claimed_budget = _BUDGET_CLAIM.search(about_snapshot)
    assert claimed_budget is not None, (
        f"{scenario.scenario_id}: header must state the freshness budget the snapshot is judged against"
    )
    assert int(claimed_budget.group(1) or claimed_budget.group(2)) == budget, (
        f"{scenario.scenario_id}: header quotes a different freshness budget than the "
        f"scenario's max_freshness_min of {budget}"
    )

    if status is DataFreshnessStatus.STALE:
        assert "stale" in about_snapshot.lower(), (
            f"{scenario.scenario_id}: a {int(round(age_minutes))}-minute-old snapshot is STALE "
            f"against the {budget}-minute budget, but the header does not say so"
        )
    else:
        assert "stale" not in about_snapshot.lower(), (
            f"{scenario.scenario_id}: the snapshot is {status.value} against the {budget}-minute "
            f"budget, but the header calls it stale"
        )


def test_factory_context_is_replay_ready() -> None:
    """The shared factory payload is itself a valid replay context."""
    context = make_context_dict()

    assert context["weather_policy"]["mode"] == "replay"
    assert context["weather_policy"]["snapshot_ref"] == "replay/weather/scn_weather_snapshot.json"
    assert context["route"]["value"]["route_polyline_ref"] == "replay/routes/scn_route_sample.gpx"
    assert (SCENARIOS_DIR.parent / context["route"]["value"]["route_polyline_ref"]).is_file()


@pytest.mark.parametrize("path", SCENARIO_FILES, ids=lambda path: path.name)
def test_no_field_is_declared_both_missing_and_stale(path: Path) -> None:
    """``missing_fields`` and ``stale_fields`` are mutually exclusive (spec 17).

    A field is either absent (nothing to age) or present-but-old; it cannot be
    both. WeatherForRouteTime.freshness_status can only ever return
    FRESH/STALE/UNKNOWN, so a scenario that also lists its (existing, dated)
    snapshot under ``missing_fields`` asks the protocol to disclose a gap that
    does not exist -- and an agent that reports the snapshot as missing instead
    of stale is following the fixture, not the protocol.
    """
    payload = yaml.safe_load(path.read_text(encoding="utf-8"))
    quality = payload["initial_context"]["data_quality"]
    overlap = sorted(set(quality.get("missing_fields") or []) & set(quality.get("stale_fields") or []))

    assert overlap == [], f"{path.name}: fields declared both missing and stale: {overlap}"
