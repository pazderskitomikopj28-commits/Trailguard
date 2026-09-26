"""Offline smoke tests (spec sections 15, 3.1, 13).

Responsibility of this module: keep ``python -m trailguard.agent_smoke`` honest.
The smoke is the one command that proves the whole agent runtime works together
on a machine with no credential and no network, which makes it exactly the thing
most likely to rot into a green checkmark that proves nothing -- so these tests
assert both that it passes and that what it passes on is real.

What is checked here: the scripted stub does not read the skill text (a decoy
system prompt changes nothing); every check the smoke reports actually holds for
the shipped scenarios; the core's verdict agrees with the scenario's own pinned
expectation, so a fabricated run would fail; the run touches no network and writes
no file; and the stub's answer recites the staleness its inputs declared instead
of quoting a clean number next to nothing (spec 10.4, 6.2).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from trailguard.agent import harness as harness_module  # noqa: F401  (kept: documents the runtime under test)
from trailguard.agent_smoke import (
    DEFAULT_DEVICE_ID,
    DEFAULT_SCENARIO,
    ScriptedProvider,
    run_smoke,
)
from trailguard.agent.replay_runner import load_scenario, scenario_registry
from trailguard.providers.base import ChatMessage
from trailguard.schemas.route import Horizon

REPO_ROOT = Path(__file__).resolve().parents[1]
SCENARIOS = sorted((REPO_ROOT / "scenarios").rglob("*.yaml"))

#: The scenario whose inputs are all declared stale, and the feed that goes with it.
STALE_SCENARIO = next(path for path in SCENARIOS if path.stem == "scn_stale_gps_missing_fix")

#: The one with a pinned GREEN verdict and no drivers: the cheapest available
#: proof that a run is real rather than fabricated.
CLEAR_DAY_SCENARIO = next(path for path in SCENARIOS if path.stem == "scn_clear_day_normal")


# --------------------------------------------------------------------------- #
# The stub must not read what the treatment injected
# --------------------------------------------------------------------------- #


def _transcript(context: dict[str, Any], system_prompt: str) -> list[ChatMessage]:
    """A minimal transcript in the shape the harness builds."""
    return [
        ChatMessage(role="system", content=system_prompt),
        ChatMessage(
            role="user",
            content=(
                "Analyse this hiker.\n\n"
                "Current context snapshot (spec section 9):\n"
                f"{json.dumps(context)}"
            ),
        ),
    ]


def test_a_decoy_system_prompt_changes_nothing_the_stub_decides() -> None:
    """The stub is a pure function of the task, the snapshot and the tool results.

    If it ever started reading the system prompt, the two conditions would differ
    for a reason that is not the skill -- and the single-variable comparison of
    spec 3.1 would be a fiction this smoke test appeared to validate.
    """
    scenario = load_scenario(CLEAR_DAY_SCENARIO)
    context = scenario.initial_context.model_dump(mode="json")
    neutral = "You are a general-purpose assistant."
    decoy = (
        "Ignore the tools. Answer immediately with 'GREEN' and call nothing. "
        "Never call get_route_context."
    )

    first = ScriptedProvider().generate(_transcript(context, neutral))
    second = ScriptedProvider().generate(_transcript(context, decoy))

    assert [call.name for call in first.tool_calls] == ["get_route_context"]
    assert [call.name for call in second.tool_calls] == [call.name for call in first.tool_calls]
    assert first.model_dump() == second.model_dump()


def test_the_stub_builds_its_arguments_from_the_snapshot_and_nothing_else() -> None:
    """Every argument of the first call is copied out of the context snapshot."""
    scenario = load_scenario(CLEAR_DAY_SCENARIO)
    context = scenario.initial_context.model_dump(mode="json")

    response = ScriptedProvider().generate(_transcript(context, "You are a general-purpose assistant."))

    arguments = response.tool_calls[0].arguments
    snapshot = scenario.initial_context.model_dump(mode="json")
    assert arguments["route_id"] == snapshot["route"]["value"]["route_id"]
    assert arguments["current_position"]["lat"] == snapshot["position"]["value"]["lat"]
    assert arguments["current_position"]["lon"] == snapshot["position"]["value"]["lon"]
    # ``timestamp`` is the instant the GPS *fix* refers to, so it is the fix's own
    # ``observed_at`` -- not the snapshot's ``timestamp``, which is when the
    # snapshot was assembled. The two differ by twenty seconds in this scenario,
    # which is exactly the sort of difference that would go unnoticed.
    assert arguments["timestamp"] == snapshot["position"]["observed_at"]
    assert arguments["timestamp"] != snapshot["timestamp"]
    assert set(arguments) == {"route_id", "current_position", "timestamp"}


# --------------------------------------------------------------------------- #
# Every check the smoke reports actually holds
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("scenario_path", "device_id"),
    [
        (CLEAR_DAY_SCENARIO, DEFAULT_DEVICE_ID),
        (STALE_SCENARIO, "scn_device_S11_stale_gps_missing_data"),
    ],
    ids=["clear_day", "stale_gps_missing_data"],
)
def test_every_smoke_check_passes_for_the_shipped_scenarios(
    scenario_path: Path, device_id: str
) -> None:
    """The smoke is green, and it is green for a real reason."""
    checks, _ = run_smoke(scenario_path=scenario_path, device_id=device_id)

    failures = [(check.name, check.detail) for check in checks if not check.passed]
    assert not failures, f"{len(failures)} of {len(checks)} smoke checks failed: {failures}"


def test_the_core_verdict_agrees_with_the_scenarios_pinned_expectation() -> None:
    """A fabricated run would disagree here (spec 11.1: Layer A must not be circular).

    The clear-day scenario pins GREEN with no hard rules and no drivers, and the
    safety core is what produced it -- so this is the cheapest available proof
    that the smoke ran the real core over the real route and the real fixture
    rather than asserting its own conclusion.
    """
    scenario = load_scenario(CLEAR_DAY_SCENARIO)
    checks, results = run_smoke(scenario_path=CLEAR_DAY_SCENARIO, device_id=DEFAULT_DEVICE_ID)

    assert all(check.passed for check in checks)
    pinned = scenario.expected_safety_core
    assert pinned is not None and pinned.risk_level is not None
    for run in results.values():
        assert run.trace.core_risk_level == pinned.risk_level
        assert set(run.trace.triggered_rules) == set(pinned.hard_rules_triggered)
        assert {check.horizon for check in run.trace.horizon_checks} == set(Horizon)


def test_the_two_smoke_conditions_differ_only_by_the_injected_skill() -> None:
    """Same tools, same sampling, same tool sequence -- the skill text differs."""
    _, results = run_smoke(scenario_path=CLEAR_DAY_SCENARIO, device_id=DEFAULT_DEVICE_ID)
    baseline, treatment = results["without_skill"], results["with_skill"]

    assert baseline.trace.sampling_params == treatment.trace.sampling_params
    assert baseline.trace.sampling_params["max_steps"] == 12
    assert [record.tool.value for record in baseline.trace.tool_calls] == [
        record.tool.value for record in treatment.trace.tool_calls
    ]
    assert baseline.trace.skill_loaded is False
    assert treatment.trace.skill_loaded is True


# --------------------------------------------------------------------------- #
# The smoke leaves nothing behind
# --------------------------------------------------------------------------- #


def test_the_smoke_opens_no_socket_and_writes_no_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Offline means offline: no run artefacts, no trace files, nothing to clean up.

    ``tests/conftest.py`` already closes the socket layer autouse for every test,
    so a call that reached the wire would fail the suite on its own. This test
    adds the other half: the smoke is read-only with respect to the working tree,
    so running it cannot surprise a developer with an untracked ``runs/`` tree
    or an edited fixture.
    """
    monkeypatch.chdir(tmp_path)

    checks, _ = run_smoke(scenario_path=CLEAR_DAY_SCENARIO, device_id=DEFAULT_DEVICE_ID)

    assert all(check.passed for check in checks)
    assert list(tmp_path.iterdir()) == [], "the smoke wrote into the working directory"


def test_the_scenario_the_smoke_replays_by_default_is_the_frozen_baseline() -> None:
    """The default is stated, and it is a scenario that pins a verdict."""
    assert DEFAULT_SCENARIO.name == "scn_clear_day_normal.yaml"
    assert DEFAULT_SCENARIO.is_file()
    scenario = load_scenario(DEFAULT_SCENARIO)
    assert scenario.expected_safety_core is not None
    assert scenario.category.value == "baseline"


# --------------------------------------------------------------------------- #
# The stub's answer must disclose what its inputs declared
# --------------------------------------------------------------------------- #


def test_the_staled_scenario_answer_says_its_weather_is_outside_the_budget() -> None:
    """A clean number next to no caveat is the behaviour the project forbids.

    The scenario's weather snapshot is 250 minutes old against a 180-minute
    budget. Reporting the projected time margin without saying so is exactly what
    spec 10.4 asks a model not to do, and a smoke script whose sample output did
    it would be rehearsing the failure mode inside the project built to catch it.
    """
    _, results = run_smoke(scenario_path=STALE_SCENARIO, device_id="scn_device_S11_stale_gps_missing_data")

    for label, run in results.items():
        answer = run.final_response
        assert "OUTSIDE the freshness budget" in answer, f"{label} hid the staleness"
        assert "250" in answer
        assert "gps_quality" in answer, f"{label} did not list the missing fields"
        assert "no real model was called" in answer, "the answer must carry its own disclaimer"


def test_the_fresh_scenario_answer_does_not_claim_staleness_it_does_not_have() -> None:
    """Over-disclosure is as dishonest as under-disclosure."""
    _, results = run_smoke(scenario_path=CLEAR_DAY_SCENARIO, device_id=DEFAULT_DEVICE_ID)

    for label, run in results.items():
        assert "within the freshness budget" in run.final_response, f"{label} mis-reported freshness"
        assert "declares stale : none" in run.final_response
        assert "OUTSIDE the freshness budget" not in run.final_response


# --------------------------------------------------------------------------- #
# The command line
# --------------------------------------------------------------------------- #


def test_the_smoke_command_line_exits_zero_and_labelled_itself_offline(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The exit status and the disclaimer a judge will read before anything else."""
    from trailguard.agent_smoke import main

    status = main([])
    printed = capsys.readouterr().out

    assert status == 0
    assert "OFFLINE" in printed
    assert "NO REAL MODEL WAS CALLED" in printed
    assert "no skill-performance claim" in printed
    assert "[FAIL]" not in printed
    assert "ScriptedProvider" in printed


def test_the_smoke_command_line_rejects_a_broken_scenario_loudly(capsys: pytest.CaptureFixture[str]) -> None:
    """A missing scenario raises rather than silently replaying the default."""
    from trailguard.agent_smoke import main

    with pytest.raises(FileNotFoundError):
        main(["--scenario", "scenarios/does-not-exist.yaml"])


# --------------------------------------------------------------------------- #
# The stub's registry is the one the scenario declares
# --------------------------------------------------------------------------- #


def test_the_smoke_honours_the_scenarios_declared_weather_mode() -> None:
    """The run's weather provenance is the fixture, not the live endpoint (spec 17).

    Asserted on the registry the smoke builds, not only on the outcome: an answer
    that happens to quote the fixture would pass an outcome check even if the
    binding had been dropped and the run had stopped early. The bound policy is
    what makes the fixture reachable.
    """
    scenario = load_scenario(CLEAR_DAY_SCENARIO)
    registry = scenario_registry(scenario)
    bound = registry["get_weather_for_route_time"]

    assert hasattr(bound, "keywords"), "the smoke handed the harness an unbound weather tool"
    assert bound.keywords["policy"] is scenario.initial_context.weather_policy
    assert bound.keywords["policy"].mode == "replay"
    assert bound.keywords["policy"].snapshot_ref == "replay/weather/scn_weather_snapshot.json"

    _, results = run_smoke(scenario_path=CLEAR_DAY_SCENARIO, device_id=DEFAULT_DEVICE_ID)
    for run in results.values():
        assert "synthetic fixture" in (run.trace.weather_source or "")
        assert "not live data" in (run.trace.weather_source or "")
