"""Replay-driver tests (spec sections 13, 3.1, 17).

Responsibility of this module: prove that
:mod:`trailguard.agent.replay_runner` is a *driver* and not a sixth tool. It loads
a scenario, hands the same :func:`~trailguard.agent.harness.run_agent` a registry
that honours the scenario's own weather policy, writes the evidence trace to
disk, and reports structural facts about the answer. It never calls a tool, never
scores a run, and never lets one scenario's replay mode leak into the next.

Explicit non-responsibility: no network, no credential, no real model. The driver
is exercised through the scripted :class:`~tests.stub_provider.StubProvider` and
the shipped scenario fixtures, so the whole batch runs inside ``python -m
pytest`` the way spec 13 requires.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from tests.stub_provider import StubProvider

from trailguard.agent import replay_runner
from trailguard.agent.replay_runner import (
    DEFAULT_OUTPUT_DIR,
    final_answer_findings,
    load_scenario,
    main,
    replay_dataset,
    replay_scenario,
    scenario_registry,
)
from trailguard.providers.base import ModelResponse, ProviderUnavailable, ToolCallRequest
from trailguard.schemas.scenario import ToolName
from trailguard.tools import TOOL_FUNCTIONS
from trailguard.tools.contracts import GetWeatherForRouteTimeInput

#: The frozen dataset, in the stable order the fixtures declare.
SCENARIO_FILES: list[Path] = sorted(Path(__file__).resolve().parents[1].joinpath("scenarios").rglob("*.yaml"))

#: The one scenario every driver test replays: the clear-day control case, whose
#: pinned verdict (GREEN, no hard rules, no drivers) is the cheapest way to tell a
#: real run from a fabricated one.
CLEAR_DAY = next(path for path in SCENARIO_FILES if path.stem == "scn_clear_day_normal")


# --------------------------------------------------------------------------- #
# final_answer_findings: facts about the text, never a verdict on it
# --------------------------------------------------------------------------- #


def test_findings_measure_the_text_and_nothing_else() -> None:
    """Length, emptiness and horizon coverage -- nothing that needs an opinion."""
    findings = final_answer_findings(
        "At +30min the level is MEDIUM; at +60min HIGH; now GREEN. Weather is 30 min old."
    )

    assert findings.is_empty is False
    assert findings.length_chars > 0
    assert findings.horizons_mentioned == ("NOW", "+30min", "+60min")


def test_findings_recognise_the_other_spellings_a_model_actually_uses() -> None:
    """The canonical labels are the contract's; the spellings are the model's."""
    findings = final_answer_findings("current GREEN, 30 minutes away MEDIUM, 1 hour HIGH, two hours RED")

    assert set(findings.horizons_mentioned) == {"NOW", "+30min", "+60min", "+120min"}


def test_an_empty_answer_is_reported_as_empty() -> None:
    """An empty final response means the run produced no answer (spec 18)."""
    findings = final_answer_findings("   \n  ")

    assert findings.is_empty is True
    assert findings.horizons_mentioned == ()
    assert findings.mentions_banned_claim is False


def test_the_banned_phrase_is_reported_as_present_even_when_it_is_denied() -> None:
    """A presence scan cannot tell a claim from a denial, and says so in its name.

    This test pins a *limitation* rather than a feature. "There is no accident
    probability to report" and "the accident probability is 3%" both contain the
    banned phrase, and no substring test can separate them -- so the field is
    named ``mentions_banned_claim`` and both sentences set it. An earlier revision
    named it ``makes_banned_claim``, which scored a careful denial as a protocol
    violation: a benchmark that punishes disclosure is worse than no benchmark.
    """
    denial = final_answer_findings("there is no accident probability to report for this route")
    claim = final_answer_findings("the accident probability of this route is 3%")

    assert denial.mentions_banned_claim is True
    assert claim.mentions_banned_claim is True


def test_the_banned_phrase_limitation_is_documented_on_the_findings_type() -> None:
    """The limitation a reader must know is written where they will meet it.

    ``FinalAnswerFindings`` carries the per-field contract, so its docstring is
    where the negation limitation belongs -- a reader inspecting the type (or the
    saved JSON beside it) has to learn that ``mentions_banned_claim`` is a
    presence scan before they treat it as a verdict.
    """
    from trailguard.agent.replay_runner import FinalAnswerFindings

    doc = FinalAnswerFindings.__doc__ or ""

    assert "presence" in doc
    assert "denial" in doc


# --------------------------------------------------------------------------- #
# scenario_registry: the scenario's weather mode, bound without mutating anything
# --------------------------------------------------------------------------- #


def _weather_arguments(scenario: Any) -> dict[str, Any]:
    """A valid ``get_weather_for_route_time`` argument set for a scenario."""
    position = scenario.initial_context.position.value
    return {
        "lat": position.lat,
        "lon": position.lon,
        "target_time": scenario.initial_context.time_state.analysis_time,
    }


def test_the_scenario_registry_holds_exactly_the_frozen_five_tools() -> None:
    """No tool is added and none is dropped; the order is the frozen one (spec 4.1)."""
    registry = scenario_registry(load_scenario(CLEAR_DAY))

    assert list(registry) == list(ToolName)


def test_binding_the_policy_leaves_the_canonical_registry_untouched() -> None:
    """One scenario's replay mode must not leak into the run that follows it."""
    before = TOOL_FUNCTIONS[ToolName.GET_WEATHER_FOR_ROUTE_TIME]
    scenario = load_scenario(SCENARIO_FILES[-1])

    scenario_registry(scenario)

    after = TOOL_FUNCTIONS[ToolName.GET_WEATHER_FOR_ROUTE_TIME]
    assert after is before, "the canonical registry was mutated in place"
    assert not hasattr(after, "func"), "the shared registry entry is now a functools.partial"


def test_the_registry_reaches_the_scenarios_own_replay_snapshot() -> None:
    """In replay mode the snapshot answers -- no socket is opened (spec 13).

    ``tests/conftest.py`` closes the socket layer autouse, so a run that reached
    for the live endpoint here would fail the suite rather than silently pass.
    """
    scenario = load_scenario(CLEAR_DAY)
    registry = scenario_registry(scenario)

    result = registry[ToolName.GET_WEATHER_FOR_ROUTE_TIME](**_weather_arguments(scenario))

    assert "synthetic fixture" in result.source
    assert result.freshness_minutes == 30.0


def test_the_frozen_weather_contract_has_no_field_for_the_scenarios_mode() -> None:
    """The Phase 3 finding, stated as a test that will fail when the gap is closed.

    ``WeatherPolicy`` (mode + snapshot_ref) is how a scenario says where weather
    may come from, and the *Python* tool signature accepts it as ``policy=`` --
    but the contract the harness validates the model's arguments against does
    not. A model can therefore never ask for replay mode, which is why
    :func:`scenario_registry` has to bind the policy from outside the call. This
    test records the gap instead of widening a Phase 1 contract inside Phase 3;
    when a later phase adds the field, this assertion is the thing that should be
    deleted.
    """
    fields = set(GetWeatherForRouteTimeInput.model_fields)

    assert "policy" not in fields
    assert "mode" not in fields
    assert fields == {"lat", "lon", "target_time"}


def test_both_conditions_of_a_scenario_get_the_same_registry() -> None:
    """Fairness (spec 3.1): the registry is scenario-derived, never skill-derived.

    Compared by what the bound entry *is* rather than by ``==``:
    ``functools.partial`` falls back to identity equality, so two bindings of the
    same function with the same policy would compare unequal and the assertion
    would report a fairness violation that did not happen.
    """
    scenario = load_scenario(CLEAR_DAY)
    baseline = scenario_registry(scenario)
    treatment = scenario_registry(scenario)

    assert list(baseline) == list(treatment)
    for name in ToolName:
        if name is ToolName.GET_WEATHER_FOR_ROUTE_TIME:
            baseline_entry = baseline[name]
            treatment_entry = treatment[name]
            assert baseline_entry.func is treatment_entry.func
            assert baseline_entry.keywords == treatment_entry.keywords
        else:
            assert baseline[name] == treatment[name]


# --------------------------------------------------------------------------- #
# replay_scenario: writes the run, and only the run
# --------------------------------------------------------------------------- #


def _script(*, route_call: ToolCallRequest, weather_call: ToolCallRequest) -> list[ModelResponse]:
    """A three-turn scripted model: two tool calls, then an answer."""
    return [
        ModelResponse(tool_calls=[route_call]),
        ModelResponse(tool_calls=[weather_call]),
        ModelResponse(content="Assessment: GREEN at all four horizons.", tool_calls=[]),
    ]


def _scripted_scenario_provider(scenario: Any, *, provider_name: str = "stub") -> StubProvider:
    """A scripted provider that calls the route and weather tools for a scenario."""
    position = scenario.initial_context.position.value
    return StubProvider(
        _script(
            route_call=ToolCallRequest(
                id="call-route",
                name="get_route_context",
                arguments={
                    "route_id": scenario.initial_context.route.value.route_id,
                    "current_position": {"lat": position.lat, "lon": position.lon},
                    "timestamp": scenario.initial_context.timestamp,
                },
            ),
            weather_call=ToolCallRequest(
                id="call-weather",
                name="get_weather_for_route_time",
                arguments=_weather_arguments(scenario),
            ),
        ),
        provider_name=provider_name,
    )


def test_a_replayed_scenario_writes_its_trace_to_the_named_directory(tmp_path: Path) -> None:
    """One file per (scenario, condition), holding the run and its evidence."""
    scenario = load_scenario(CLEAR_DAY)

    summary = replay_scenario(
        scenario=scenario,
        model_provider=_scripted_scenario_provider(scenario),
        skill_enabled=False,
        output_dir=tmp_path,
    )

    expected = tmp_path / scenario.scenario_id / "without_skill.json"
    assert summary.output_path == expected
    assert expected.is_file()
    saved = json.loads(expected.read_text(encoding="utf-8"))
    assert saved["run_id"] == summary.run_id
    assert saved["condition"] == "without_skill"
    assert saved["scenario_id"] == scenario.scenario_id
    assert saved["stop_reason"] == "completed"
    assert saved["trace"]["skill_loaded"] is False
    # ``as_dict`` serialises the tuple field as a JSON array, so the saved form
    # lists horizons rather than carrying a tuple.
    assert saved["findings"]["horizons_mentioned"] == []
    assert saved["findings"]["length_chars"] > 0


def test_the_treatment_run_records_the_skill_identity_the_baseline_does_not(tmp_path: Path) -> None:
    """The single variable, visible in both saved traces (spec 3.1)."""
    scenario = load_scenario(CLEAR_DAY)

    baseline = replay_scenario(
        scenario=scenario,
        model_provider=_scripted_scenario_provider(scenario),
        skill_enabled=False,
        output_dir=tmp_path,
    )
    treatment = replay_scenario(
        scenario=scenario,
        model_provider=_scripted_scenario_provider(scenario),
        skill_enabled=True,
        output_dir=tmp_path,
    )

    assert baseline.trace.skill_sha256 is None
    assert baseline.trace.skill_version is None
    assert treatment.trace.skill_sha256
    assert treatment.trace.skill_version
    assert (tmp_path / scenario.scenario_id / "with_skill.json").is_file()


def test_replay_dataset_writes_a_batch_summary_beside_the_traces(tmp_path: Path) -> None:
    """The summary is an index: run ids and stop reasons, not a second opinion."""
    scenario = load_scenario(CLEAR_DAY)
    provider = _scripted_scenario_provider(scenario)

    summaries = replay_dataset(
        scenario_paths=[CLEAR_DAY],
        model_provider=provider,
        output_dir=tmp_path,
    )

    assert len(summaries) == 2, "both conditions ran"
    summaries_files = sorted(tmp_path.glob("summary_*.json"))
    assert len(summaries_files) == 1
    index = json.loads(summaries_files[0].read_text(encoding="utf-8"))
    assert [run["condition"] for run in index["runs"]] == ["without_skill", "with_skill"]
    assert index["sampling"] == {"temperature": 0.0, "max_tokens": 2048, "max_steps": 12}
    assert index["model_name"] == "stub-model"


def test_a_scenario_file_is_validated_before_it_can_reach_the_harness(tmp_path: Path) -> None:
    """A typo in the YAML fails at the contract boundary, not mid-run (spec 17)."""
    broken = tmp_path / "broken.yaml"
    broken.write_text("scenario_id: BROKEN\n", encoding="utf-8")

    with pytest.raises(Exception) as excinfo:
        load_scenario(broken)

    assert "description" in str(excinfo.value) or "Field required" in str(excinfo.value)


# --------------------------------------------------------------------------- #
# The driver is not a tool, and never becomes one
# --------------------------------------------------------------------------- #


def _replay_runner_names() -> set[str]:
    """Every identifier the replay driver executes (docstrings excluded)."""
    import ast
    import inspect

    from trailguard.agent import replay_runner

    tree = ast.parse(inspect.getsource(replay_runner))
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Name):
            names.add(node.id)
        elif isinstance(node, ast.Attribute):
            names.add(node.attr)
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names.add(node.name)
    return names


def test_the_replay_driver_never_executes_a_tool_itself() -> None:
    """The driver hands the registry to ``run_agent``; it does not call through it.

    A driver that called tools directly would be handing the baseline a plan --
    the measured difference between the conditions would then be the driver's
    tool choices, not the skill's. What it *may* do is name the tools when it
    builds the registry, which is what ``scenario_registry`` does.
    """
    executed = _replay_runner_names()

    assert "_execute_tool_call" not in executed
    assert "record_core_verdict" not in executed
    assert "assess_risk" not in executed, "a tool name reached the driver's executable code"


def test_the_driver_never_reads_the_skill_text() -> None:
    """Only the harness injects SKILL.md (spec 3.1); the driver must not peek."""
    executed = _replay_runner_names()

    assert "load_skill_md" not in executed
    assert "skill_identity" not in executed


def test_the_default_output_directory_is_the_gitignored_runs_tree() -> None:
    """Traces are evidence, not source (spec 13, 24)."""
    assert DEFAULT_OUTPUT_DIR == DEFAULT_OUTPUT_DIR.parent.parent / "runs" / "phase3"
    assert DEFAULT_OUTPUT_DIR.name == "phase3"
    gitignore = (DEFAULT_OUTPUT_DIR.parents[1] / ".gitignore").read_text(encoding="utf-8")
    assert "runs/" in gitignore


# --------------------------------------------------------------------------- #
# The command line never fabricates a live run
# --------------------------------------------------------------------------- #


def test_the_command_line_reports_not_run_without_a_credential(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """No credential, no run, no trace -- and a non-zero exit (spec 16, 17)."""
    monkeypatch.delenv("STEPFUN_API_KEY", raising=False)

    status = main(["--scenario", str(CLEAR_DAY), "--out", str(tmp_path)])

    assert status == 2
    printed = capsys.readouterr().out
    assert "NOT RUN" in printed
    assert "STEPFUN_API_KEY" in printed, "the missing variable is named so the run can be re-run"
    assert "credential unavailable" in printed
    assert not list(tmp_path.rglob("*.json")), "no trace was fabricated"


def test_the_command_line_reports_not_run_when_the_dataset_is_empty(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """An empty dataset is reported, not silently treated as a zero-run batch.

    The credential has to be *present* for this test to test anything: ``main``
    checks the credential first and returns ``2`` before it builds the path list,
    so a test that deleted the key would pass on the sibling branch instead of
    this one -- it would assert "NOT RUN" while proving the empty-dataset code
    path dead. The key is a placeholder that is never used, because with an empty
    path list ``main`` never constructs a provider.
    """
    monkeypatch.setenv("STEPFUN_API_KEY", "test-placeholder-not-a-real-credential")
    # Point the driver at a dataset directory that holds no scenario files, so
    # the ``if not paths`` branch below is the only one this run can reach.
    monkeypatch.setattr(replay_runner, "REPO_ROOT", tmp_path)
    status = main(["--out", str(tmp_path / "out")])

    assert status == 2
    printed = capsys.readouterr().out
    assert "no scenario files found" in printed
    assert "credential unavailable" not in printed, "this run had a credential"
    assert not list((tmp_path / "out").rglob("*.json")), "no trace was fabricated"


def test_the_command_line_reports_a_transport_failure_by_class_name_only(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A provider failure is reported by class name, never as a traceback.

    The SDK error chains the raw HTTP failure as ``__cause__``, and an endpoint
    that echoes the request it refused would carry the credential into a printed
    traceback. This test puts a decoy key in the environment and a provider that
    fails, then asserts the console and the report carry neither the key nor a
    traceback -- the guarantee ``scripts/test_stepfun_agent.py`` documents for its
    own entry point, now held for this one.
    """
    decoy = "sk-decoy-should-never-be-printed-000"
    monkeypatch.setenv("STEPFUN_API_KEY", decoy)

    def _failing_provider() -> ModelProvider:
        raise ProviderUnavailable("decoy transport failure for the CLI test")

    monkeypatch.setattr(replay_runner, "_provider_from_environment", _failing_provider)

    status = main(["--scenario", str(CLEAR_DAY), "--out", str(tmp_path)])

    assert status == 3
    printed = capsys.readouterr().out
    assert "PROVIDER FAILURE" in printed
    assert "ProviderUnavailable" in printed, "the failure kind is reported"
    assert decoy not in printed
    assert "Traceback" not in printed
    assert not list(tmp_path.rglob("*.json")), "a failed run writes no trace"


def test_a_saved_run_file_carries_the_answer_through_the_traces_redaction(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A credential-shaped string in the model's answer does not reach disk.

    The run file is free text, so no contract validator sees it: the answer has to
    pass through the same redaction the evidence trace applies at its own
    boundary, which is what ``StepFunProvider``'s docstring already promises.

    The credential-shaped string is assembled at runtime rather than written as a
    literal: ``tests/test_secrets_git.py`` scans every committable file for an
    ``api_key: <value>`` assignment, and that scanner is the reason this test can
    trust its own decoy.
    """
    scenario = load_scenario(CLEAR_DAY)
    secret = "sk-" + "decoy-should-never-be-printed-000"
    provider = StubProvider(
        [
            ModelResponse(
                content=f"Analysis done. api_key: {secret} ok",
                tool_calls=[],
            )
        ]
    )

    summary = replay_scenario(
        scenario=scenario,
        model_provider=provider,
        skill_enabled=False,
        output_dir=tmp_path,
    )

    saved = summary.output_path.read_text(encoding="utf-8")
    assert "sk-decoy-should-never-be-printed-000" not in saved
    assert "<redacted>" in saved


# --------------------------------------------------------------------------- #
# The live smoke's report line, exercised before a live run can rely on it
# --------------------------------------------------------------------------- #


def _live_smoke_module() -> Any:
    """Import ``scripts/test_stepfun_agent.py`` without pytest collecting it as tests.

    ``tests/conftest.py::collect_ignore`` keeps the file out of collection, which is
    what stops a suite run from reaching the network -- but the file's *helpers* are
    pure functions over a :class:`ReplayRunSummary`, and the console line they build
    is what a judge reads during a live run. Testing them here means a formatting
    error or a leak cannot wait for a live run to be discovered.
    """
    import importlib.util

    path = Path(__file__).resolve().parents[1] / "scripts" / "test_stepfun_agent.py"
    spec = importlib.util.spec_from_file_location("live_stepfun_agent_smoke", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _one_run(tmp_path: Path) -> Any:
    """A single completed replayed run, built offline with the scripted provider."""
    scenario = load_scenario(CLEAR_DAY)
    return replay_scenario(
        scenario=scenario,
        model_provider=_scripted_scenario_provider(scenario),
        skill_enabled=True,
        output_dir=tmp_path,
    )


def test_the_live_report_line_says_what_the_run_actually_did(tmp_path: Path) -> None:
    """Scenario, condition, stop reason, counts, core verdict, skill identity."""
    module = _live_smoke_module()
    summary = _one_run(tmp_path)

    line = module._report(summary)

    assert summary.scenario_id in line
    assert "with_skill" in line
    assert "completed" in line
    assert "steps= 3" in line, "the scripted stub makes three turns"
    assert "calls= 2" in line
    assert "horizons=0" in line, "this stub assesses no horizon, and the line says so"
    assert "skill=True" in line
    assert summary.trace.skill_sha256[:12] in line
    assert "synthetic fixture" in line, "the weather provenance is part of what a run did"
    assert "FAILED" not in line


def test_the_call_sequence_line_lists_the_calls_in_the_models_order(tmp_path: Path) -> None:
    """A count is not a record: the order and the outcomes are what a judge needs."""
    module = _live_smoke_module()

    sequence = module._call_sequence(_one_run(tmp_path))

    assert sequence == "get_route_context[ok] -> get_weather_for_route_time[ok]"


def test_the_live_report_and_trace_carry_no_credential(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A decoy key in the environment must not appear in the console or the file.

    The check is worth making even though the report line never receives the key
    directly: a redaction that only holds because the value happens not to be in
    scope is not a redaction.
    """
    monkeypatch.setenv("STEPFUN_API_KEY", "sk-decoy-should-never-be-printed-000")
    module = _live_smoke_module()
    summary = _one_run(tmp_path)

    printed = module._report(summary) + module._call_sequence(summary)
    saved = summary.output_path.read_text(encoding="utf-8")

    for leak in ("sk-decoy-should-never-be-printed-000", "STEPFUN_API_KEY"):
        assert leak not in printed
        assert leak not in saved
