"""Agent-runtime tests: what a *real* run puts into the evidence trace (spec 15, 18, 3.1).

``tests/test_harness.py`` covers the loop mechanics with a scripted provider.
This module covers the Phase 3 question those tests could not: when the model
actually calls the real tools and the real deterministic core runs, does the
trace carry facts or theatre?

Three properties are asserted here:

1. **The trace is populated from real execution.** A successful ``assess_risk``
   call writes the horizon, the rules that fired, the drivers and the core's own
   verdict into the trace -- with the values verified against a direct call to the
   same tool, so a harness that invented them would fail here.
2. **The trace stays silent about what the model merely said.** ``final_risk_level``,
   ``confidence`` and ``uncertainty_disclosed`` describe the agent's claim, and the
   harness has no opinion about English: a completed run leaves them empty until
   the Phase 5 evaluator reads the answer.
3. **The harness does not do the skill's job.** It records the horizons the model
   *chose* to assess; it never iterates them itself, never re-orders calls, never
   fills in a tool the model skipped, and never rewrites the model's answer. The
   structural guards at the bottom of this file are the regression fence.

Every run here is offline: a scripted provider drives a real harness, real tools,
a real replay fixture and a real rule pack (spec 13).
"""

from __future__ import annotations

import ast
import copy
import inspect
import json
from typing import Any

import pytest

from conftest import (
    make_context,
    make_context_dict,
    make_geo_point,
    make_risk_result_dict,
    make_route_context,
    make_time_state,
    make_weather,
)

from stub_provider import FailingProvider, StubProvider

from trailguard.agent import run_agent
from trailguard.agent.harness import HARNESS_BASE_SYSTEM_PROMPT, AgentRunResult
from trailguard.providers.base import ModelProvider, ModelResponse, ToolCallRequest
from trailguard.schemas import ToolName
from trailguard.schemas.risk import RiskResult
from trailguard.tools import TOOL_FUNCTIONS

TASK = "Assess the hiker and the required future horizons."

#: Every tool of the frozen registry, by name. Used by the structural guards: a
#: harness that names a tool in a string literal is one step away from calling it
#: on the model's behalf.
TOOL_NAME_STRINGS: tuple[str, ...] = tuple(tool.value for tool in ToolName)


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #


def _projection(horizon_minutes: int) -> dict[str, Any]:
    """A contract-correct ``RouteProjection`` for one frozen horizon."""
    from datetime import datetime, timedelta, timezone

    labels = {0: "NOW", 30: "+30min", 60: "+60min", 120: "+120min"}
    analysis = datetime(2026, 5, 18, 10, 30, tzinfo=timezone.utc)
    arrival = analysis + timedelta(minutes=horizon_minutes)
    return {
        "horizon": labels[horizon_minutes],
        "horizon_min": horizon_minutes,
        "projected_route_progress": 0.35 + horizon_minutes / 400.0,
        "projected_position": make_geo_point().model_dump(mode="json"),
        "projected_arrival_time": arrival.isoformat().replace("+00:00", "Z"),
        "projected_remaining_distance_km": 7.15 - horizon_minutes / 60.0,
        "projection_confidence": 0.9,
    }


def _assess_call(
    *,
    freshness_min: float | None = 30.0,
    weather: bool = True,
    horizon_minutes: int | None = None,
    call_id: str = "call-risk",
) -> ToolCallRequest:
    """One scripted ``assess_risk`` call with contract-correct arguments.

    ``horizon_minutes`` is expressed the only way the frozen input contract
    allows -- through ``projected_state`` -- because ``AssessRiskInput`` carries
    neither ``horizon_min`` nor ``weather_freshness_min``. A model that tried to
    pass either would be refused at the boundary, which is exactly the behaviour
    the contract exists to produce (spec 5.5).
    """
    arguments: dict[str, Any] = {
        "route_state": make_route_context().model_dump(mode="json"),
        "user_device_state": {
            "battery_percent": 78.0,
            "network_available": True,
            "fatigue_index": 0.12,
            "movement": {
                "current_speed_kmh": 3.5,
                "avg_speed_10m": 3.6,
                "avg_speed_30m": 3.7,
                "pace_drop_percent": 2.7,
            },
            "data_quality": 0.95,
            "last_network_time": "2026-05-18T10:28:00Z",
        },
        "time_state": make_time_state().model_dump(mode="json"),
    }
    if weather:
        arguments["weather_state"] = make_weather(freshness_minutes=freshness_min).model_dump(mode="json")
    if horizon_minutes is not None:
        arguments["projected_state"] = _projection(horizon_minutes)
    return ToolCallRequest(id=call_id, name="assess_risk", arguments=arguments)


def _final_answer(content: str = "Risk timeline reported.") -> ModelResponse:
    """One model turn that answers without calling a tool."""
    return ModelResponse(content=content, tool_calls=[])


def _run(
    provider: ModelProvider,
    *,
    skill_enabled: bool = False,
    context_override: dict[str, Any] | None = None,
    **kwargs: Any,
) -> AgentRunResult:
    """Run one episode against a (possibly modified) context snapshot."""
    overrides = copy.deepcopy(context_override or {})
    context = make_context(**overrides) if overrides else make_context()
    return run_agent(
        task=TASK,
        context=context,
        model_provider=provider,
        skill_enabled=skill_enabled,
        **kwargs,
    )


def _direct_verdict(**tool_arguments: Any) -> RiskResult:
    """Call the real tool directly, so a test can compare the trace against truth."""
    call = _assess_call(**tool_arguments)
    return TOOL_FUNCTIONS[ToolName.ASSESS_RISK](**call.arguments)


# --------------------------------------------------------------------------- #
# The trace carries what the core actually produced (spec 18)
# --------------------------------------------------------------------------- #


def test_a_real_risk_call_populates_the_horizon_check() -> None:
    """The horizon recorded is the one the core computed, not one the harness picked."""
    direct = _direct_verdict()
    provider = StubProvider([ModelResponse(content=None, tool_calls=[_assess_call()]), _final_answer()])

    result = _run(provider)

    assert result.stop_reason == "completed"
    assert [check.horizon for check in result.trace.horizon_checks] == [direct.horizon]
    assert result.trace.horizons_checked == [direct.horizon]


def test_the_core_verdict_is_the_ones_the_core_returned() -> None:
    """Verified against a direct call to the same tool: no rounding, no relabelling."""
    direct = _direct_verdict()
    provider = StubProvider([ModelResponse(content=None, tool_calls=[_assess_call()]), _final_answer()])

    result = _run(provider)

    assert result.trace.core_risk_level == direct.risk_level
    assert result.trace.core_confidence == pytest.approx(direct.confidence)
    assert result.trace.triggered_rules == direct.hard_rules_triggered
    assert result.trace.risk_drivers == direct.drivers


def test_freshness_is_judged_against_the_snapshots_own_budget() -> None:
    """``freshness_ok`` is a comparison against the budget the snapshot declares."""
    fresh = StubProvider([ModelResponse(content=None, tool_calls=[_assess_call(freshness_min=30.0)]), _final_answer()])
    stale = StubProvider([ModelResponse(content=None, tool_calls=[_assess_call(freshness_min=240.0)]), _final_answer()])

    fresh_result = _run(fresh)
    stale_result = _run(
        stale,
        context_override={"weather_policy": {"max_freshness_min": 180}},
    )

    assert fresh_result.trace.horizon_checks[0].freshness_ok is True
    assert fresh_result.trace.weather_freshness_min == pytest.approx(30.0)
    assert stale_result.trace.horizon_checks[0].freshness_ok is False
    assert "STALE" in stale_result.trace.freshness_checks[0]


def test_a_weather_age_the_core_could_not_compute_is_not_checked() -> None:
    """``None`` means "not checkable", never "fresh".

    The frozen input contract requires a ``weather_state`` with a declared
    ``freshness_minutes``, so a model cannot currently make the core report an
    unknown age through the harness -- which is a finding about the contract, not
    a gap this test may paper over. The recording path is therefore driven with a
    real :class:`RiskResult` whose age is ``None``: the unit under test is the
    harness's freshness verdict, and it must not invent one for a verdict that
    did not carry an age.
    """
    verdict = RiskResult.model_validate(
        make_risk_result_dict(safety_margins={"weather_freshness_min": None})
    )

    def _verdict_without_an_age(**_arguments: object) -> RiskResult:
        return verdict

    provider = StubProvider([ModelResponse(content=None, tool_calls=[_assess_call()]), _final_answer()])

    result = _run(provider, tools={**TOOL_FUNCTIONS, ToolName.ASSESS_RISK: _verdict_without_an_age})

    check = result.trace.horizon_checks[0]
    assert check.weather_freshness_min is None
    assert check.freshness_ok is None
    assert "not reported" in result.trace.freshness_checks[0]
    assert result.trace.weather_freshness_min is None


def test_a_tight_budget_flips_the_same_verdict_to_stale() -> None:
    """The budget comes from the context, so it is the run's policy, not a constant."""
    provider = StubProvider([ModelResponse(content=None, tool_calls=[_assess_call(freshness_min=30.0)]), _final_answer()])

    result = _run(provider, context_override={"weather_policy": {"max_freshness_min": 10}})

    assert result.trace.horizon_checks[0].freshness_ok is False


@pytest.mark.parametrize("horizon_minutes", [0, 30, 60, 120], ids=["NOW", "PLUS30", "PLUS60", "PLUS120"])
def test_every_horizon_the_model_assesses_is_recorded(horizon_minutes: int) -> None:
    """One real call per horizon, and the horizon on the record is the core's."""
    direct = _direct_verdict(horizon_minutes=horizon_minutes)
    provider = StubProvider(
        [ModelResponse(content=None, tool_calls=[_assess_call(horizon_minutes=horizon_minutes)]), _final_answer()]
    )

    result = _run(provider)

    assert [check.horizon for check in result.trace.horizon_checks] == [direct.horizon]
    assert result.trace.horizon_checks[0].weather_freshness_min == pytest.approx(30.0)


def test_a_run_without_a_risk_call_records_no_verdict_at_all() -> None:
    """No assessment happened, and the trace says so instead of defaulting to GREEN."""
    route_call = ModelResponse(
        content=None,
        tool_calls=[
            ToolCallRequest(
                id="call-route",
                name="get_route_context",
                arguments={
                    "route_id": "scn_route_sample",
                    "current_position": make_geo_point().model_dump(mode="json"),
                    "timestamp": "2026-05-18T10:30:00Z",
                },
            )
        ],
    )
    provider = StubProvider([route_call, _final_answer()])

    result = _run(provider)

    assert result.trace.horizon_checks == []
    assert result.trace.core_risk_level is None
    assert result.trace.core_confidence is None
    assert result.trace.triggered_rules == []


# --------------------------------------------------------------------------- #
# The worst horizon is a selection, never a re-computation
# --------------------------------------------------------------------------- #


def _assess_call_variant(*, weather_kwargs: dict[str, Any], call_id: str) -> ToolCallRequest:
    """One scripted ``assess_risk`` call whose *weather* is deliberately varied.

    ``_assess_call`` builds a fixed clear-day reading, so every variant it can
    produce lands on the same risk level and no test over them can observe a
    selection. This helper is the one place a test varies the input that moves the
    verdict, so "the trace reports the worst horizon" is a statement about two
    genuinely different horizons rather than twice the same one.
    """
    call = _assess_call(call_id=call_id)
    arguments = dict(call.arguments)
    arguments["weather_state"] = make_weather(**weather_kwargs).model_dump(mode="json")
    return ToolCallRequest(id=call_id, name="assess_risk", arguments=arguments)


def _direct_verdict_variant(**weather_kwargs: Any) -> RiskResult:
    """The real tool's verdict for one weather variant, called directly.

    Same contract as ``_assess_call_variant``: the test discovers what the core
    does with the input rather than asserting a level this file invented.
    """
    call = _assess_call_variant(weather_kwargs=weather_kwargs, call_id="direct")
    return TOOL_FUNCTIONS[ToolName.ASSESS_RISK](**call.arguments)


def test_the_core_verdict_is_the_worst_horizon_the_agent_assessed() -> None:
    """A projected-risk answer is only as safe as its worst horizon.

    The two calls are driven through the real tool first, so the expected level
    and confidence are *discovered* rather than hard-coded: this file does not know
    what the rule pack makes of a 60 km/h gust, and it does not need to. What it
    does assert is the ordering -- the trace reports the higher-ranked of the two
    verdicts the core actually returned, with the confidence of that same verdict.
    """
    green = _direct_verdict_variant()
    yellow = _direct_verdict_variant(wind_gust_kmh=60.0)
    assert green.risk_level is not yellow.risk_level, "fixture must give two different levels"
    assert yellow.risk_level.rank > green.risk_level.rank

    calls = [
        _assess_call_variant(weather_kwargs={}, call_id="call-1"),
        _assess_call_variant(weather_kwargs={"wind_gust_kmh": 60.0}, call_id="call-2"),
    ]
    provider = StubProvider([ModelResponse(content=None, tool_calls=calls), _final_answer()])

    result = _run(provider)

    assert result.trace.core_risk_level is yellow.risk_level
    assert result.trace.core_confidence == pytest.approx(yellow.confidence)


def test_the_worst_horizon_wins_regardless_of_the_order_the_model_asked_in() -> None:
    """The selection is by rank, not by recency: GREEN last must not win.

    Without this the previous test is satisfied by "reports the last verdict",
    which is exactly the regression a worst-case selection exists to prevent.
    """
    yellow = _direct_verdict_variant(wind_gust_kmh=60.0)

    calls = [
        _assess_call_variant(weather_kwargs={"wind_gust_kmh": 60.0}, call_id="call-1"),
        _assess_call_variant(weather_kwargs={}, call_id="call-2"),
    ]
    provider = StubProvider([ModelResponse(content=None, tool_calls=calls), _final_answer()])

    result = _run(provider)

    assert result.trace.core_risk_level is yellow.risk_level


def test_a_tie_on_rank_reports_the_first_seen_verdict_and_its_confidence() -> None:
    """The documented tie-break, pinned so it cannot drift into "last one wins".

    Two assessments that tie on rank carry *different* confidences (a stale weather
    reading costs confidence without changing the level), so the reported number
    identifies which of the two was selected. ``max`` returns the first maximal
    element; a stable ``sorted(...)[-1]`` would have returned the last.
    """
    first = _direct_verdict_variant(freshness_minutes=240.0)
    second = _direct_verdict_variant(confidence=0.4)
    assert first.risk_level is second.risk_level, "fixture must tie on rank"
    assert first.confidence != pytest.approx(second.confidence), "fixture must differ on confidence"

    calls = [
        _assess_call_variant(weather_kwargs={"freshness_minutes": 240.0}, call_id="call-1"),
        _assess_call_variant(weather_kwargs={"confidence": 0.4}, call_id="call-2"),
    ]
    provider = StubProvider([ModelResponse(content=None, tool_calls=calls), _final_answer()])

    result = _run(provider)

    assert result.trace.core_risk_level is first.risk_level
    assert result.trace.core_confidence == pytest.approx(first.confidence)


def test_the_reported_confidence_belongs_to_the_same_verdict() -> None:
    """Level and confidence must describe one assessment, never a blend."""
    direct = _direct_verdict()
    provider = StubProvider([ModelResponse(content=None, tool_calls=[_assess_call()]), _final_answer()])

    result = _run(provider)

    assert result.trace.core_confidence == pytest.approx(direct.confidence)


# --------------------------------------------------------------------------- #
# The harness has no opinion about the model's prose (spec 10.5, 18)
# --------------------------------------------------------------------------- #


def test_a_completed_run_leaves_the_claimed_verdict_to_the_evaluator() -> None:
    """The model's own level claim is not the harness's to decide."""
    provider = StubProvider(
        [
            ModelResponse(content=None, tool_calls=[_assess_call()]),
            _final_answer("I am RED-level confident: 0.99, uncertainty is high."),
        ]
    )

    result = _run(provider)

    assert result.stop_reason == "completed"
    assert result.final_response.startswith("I am RED-level confident")
    assert result.trace.final_risk_level is None, "the harness must not read a level out of prose"
    assert result.trace.confidence is None
    assert result.trace.uncertainty_disclosed is False
    # The core's own verdict, by contrast, is there -- from the tool, not the text.
    assert result.trace.core_risk_level is not None


def test_the_trace_records_tool_output_not_the_models_description_of_it() -> None:
    """A claim in the transcript cannot overwrite what the tool returned."""
    direct = _direct_verdict()
    provider = StubProvider(
        [
            ModelResponse(content=None, tool_calls=[_assess_call()]),
            ModelResponse(
                content=None,
                tool_calls=[
                    ToolCallRequest(
                        id="call-claim",
                        name="assess_risk",
                        arguments={"risk_level": "RED", "horizon": "NOW"},
                    )
                ],
            ),
            _final_answer(),
        ]
    )

    result = _run(provider)

    # The second call cannot satisfy the tool's input contract, so it is recorded
    # as a failed call. The first one's verdict survives untouched.
    statuses = [record.status for record in result.trace.tool_calls]
    assert statuses == ["ok", "error"]
    assert result.trace.core_risk_level == direct.risk_level
    assert len(result.trace.horizon_checks) == 1, "a rejected call is not a horizon that was assessed"


def test_the_model_cannot_rewrite_the_trace_after_the_fact() -> None:
    """Mutating the request it sent must not change what was recorded."""
    call = _assess_call()
    provider = StubProvider([ModelResponse(content=None, tool_calls=[call]), _final_answer()])

    result = _run(provider)
    recorded = result.trace.tool_calls[0]
    before = json.dumps(recorded.arguments, sort_keys=True)

    call.arguments["route_state"] = {"route_id": "rewritten"}
    call.arguments["horizon_min"] = 999

    assert json.dumps(recorded.arguments, sort_keys=True) == before


# --------------------------------------------------------------------------- #
# A failed call and the stop vocabulary (spec 15, 18)
# --------------------------------------------------------------------------- #


def _exploding_tool(**_arguments: object) -> None:
    raise RuntimeError("the deterministic core is unavailable")


def test_the_default_policy_reports_a_failure_and_lets_the_model_recover() -> None:
    """Pre-Phase-3 behaviour, unchanged: a failure is fed back, not fatal."""
    provider = StubProvider(
        [
            ModelResponse(content=None, tool_calls=[_assess_call()]),
            _final_answer("The risk assessment could not be completed."),
        ]
    )

    result = _run(provider, tools={**TOOL_FUNCTIONS, ToolName.ASSESS_RISK: _exploding_tool})

    assert result.stop_reason == "completed"
    assert result.steps == 2
    assert "could not be completed" in result.final_response
    # The failure the model was handed, not just the answer it wrote afterwards.
    second_turn = provider.messages_of(1)
    tool_message = [message for message in second_turn if message.role == "tool"][0]
    assert "the deterministic core is unavailable" in tool_message.content
    record = result.trace.tool_calls[0]
    assert record.status == "error"
    assert "deterministic core is unavailable" in (record.error or "")
    assert result.trace.core_risk_level is None, "a failed call is not a verdict"


def test_stop_on_failure_ends_the_run_with_tool_error() -> None:
    provider = StubProvider(
        [
            ModelResponse(content=None, tool_calls=[_assess_call()]),
            _final_answer("never reached"),
        ]
    )

    result = _run(
        provider,
        tools={**TOOL_FUNCTIONS, ToolName.ASSESS_RISK: _exploding_tool},
        tool_failure_policy="stop",
    )

    assert result.stop_reason == "tool_error"
    assert result.final_response == "", "no answer exists, so none is reported"
    assert result.steps == 1
    assert provider.call_count == 1, "the model is not consulted again"
    assert len(result.trace.tool_calls) == 1


def test_stop_on_an_unknown_tool_ends_the_run_with_invalid_tool_call() -> None:
    """A hallucinated one-shot tool is the model's failure, not the tool's."""
    provider = StubProvider(
        [
            ModelResponse(
                content=None,
                tool_calls=[ToolCallRequest(id="call-1", name="forecast_risk", arguments={"horizons": [0, 30]})],
            ),
            _final_answer("never reached"),
        ]
    )

    result = _run(provider, tool_failure_policy="stop")

    assert result.stop_reason == "invalid_tool_call"
    assert result.steps == 1
    assert result.trace.tool_calls == [], "an unregistered name cannot be recorded as a tool call"


def test_stop_on_arguments_the_contract_rejects_ends_the_run_with_invalid_tool_call() -> None:
    call = _assess_call()
    call.arguments["horizon_min"] = 7  # outside the frozen horizon set
    provider = StubProvider([ModelResponse(content=None, tool_calls=[call]), _final_answer("never reached")])

    result = _run(provider, tool_failure_policy="stop")

    assert result.stop_reason == "invalid_tool_call"
    assert result.trace.tool_calls[0].status == "error"
    assert "invalid arguments" in (result.trace.tool_calls[0].error or "")


def test_the_failure_policy_is_the_same_in_both_skill_conditions() -> None:
    """A harness policy may never be derived from the experimental variable."""
    outcomes = []
    for skill_enabled in (False, True):
        provider = StubProvider(
            [
                ModelResponse(content=None, tool_calls=[_assess_call()]),
                _final_answer("recovered"),
            ]
        )
        result = _run(
            provider,
            skill_enabled=skill_enabled,
            tools={**TOOL_FUNCTIONS, ToolName.ASSESS_RISK: _exploding_tool},
            tool_failure_policy="stop",
        )
        outcomes.append((result.stop_reason, result.steps, len(result.trace.tool_calls)))

    assert outcomes[0] == outcomes[1] == (("tool_error"), 1, 1)


def test_a_provider_failure_still_means_provider_error() -> None:
    """A transport failure is not an answer, whatever happened before it."""
    provider = FailingProvider("stub transport failure")

    result = _run(provider)

    assert result.stop_reason == "provider_error"
    assert result.final_response == ""
    assert provider.calls == 1
    assert result.trace.tool_calls == []


# --------------------------------------------------------------------------- #
# Several calls in one turn (spec 15, 16)
# --------------------------------------------------------------------------- #


def test_two_calls_in_one_turn_keep_provider_order_and_pairing() -> None:
    """Order and ids are how a provider pairs results back to calls."""
    route_call = ToolCallRequest(
        id="call-route",
        name="get_route_context",
        arguments={
            "route_id": "scn_route_sample",
            "current_position": make_geo_point().model_dump(mode="json"),
            "timestamp": "2026-05-18T10:30:00Z",
        },
    )
    provider = StubProvider(
        [
            ModelResponse(content=None, tool_calls=[route_call, _assess_call(call_id="call-risk")]),
            _final_answer(),
        ]
    )

    result = _run(provider)

    assert [record.seq for record in result.trace.tool_calls] == [1, 2]
    assert [record.tool for record in result.trace.tool_calls] == [
        ToolName.GET_ROUTE_CONTEXT,
        ToolName.ASSESS_RISK,
    ]
    second_turn = provider.messages_of(1)
    tool_messages = [message for message in second_turn if message.role == "tool"]
    assert [message.tool_call_id for message in tool_messages] == ["call-route", "call-risk"]
    assert [message.name for message in tool_messages] == ["get_route_context", "assess_risk"]
    assistant = [message for message in second_turn if message.role == "assistant"][0]
    assert [call.id for call in assistant.tool_calls] == ["call-route", "call-risk"]


# --------------------------------------------------------------------------- #
# Structural guards: the harness does not become the skill (spec 3.1, 10)
# --------------------------------------------------------------------------- #


def _harness_module() -> Any:
    """The harness module, imported once for the structural guards."""
    return __import__("trailguard.agent.harness", fromlist=["x"])


def _code_strings(tree: ast.AST) -> set[str]:
    """Every string literal the module *executes*, docstrings excluded.

    A docstring is documentation and may name a tool freely (these modules
    document what they refuse to do, which requires naming it). A non-docstring
    literal is data: it reaches the interpreter, and it is the only kind that
    could select a tool to call. Comments are gone by this point, which is fine --
    a comment has never called a tool.
    """
    documented: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Module | ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef):
            body = getattr(node, "body", [])
            if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant):
                if isinstance(body[0].value.value, str):
                    documented.add(id(body[0].value))
    return {
        node.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant) and isinstance(node.value, str) and id(node) not in documented
    }


def test_the_harness_never_names_a_tool_in_a_string_literal() -> None:
    """Naming a tool is how "fill in the missing call" and "fix the order" start.

    The harness resolves the registry by key and executes whatever the model
    asked for. If it knew a tool's name as a literal, it could call it on the
    model's behalf -- which would silently hand the baseline a plan and turn the
    measured effect into the harness's plan. Enum members are fine: naming the
    tool whose provenance the harness copies is not naming a tool to call.
    """
    executed = _code_strings(ast.parse(inspect.getsource(_harness_module())))

    for name in TOOL_NAME_STRINGS:
        assert name not in executed, f"the harness names the tool {name!r} in executable code"


def test_the_harness_does_not_iterate_the_horizons() -> None:
    """The horizon loop is the skill's; the harness only records what was asked.

    Comprehensions are checked alongside ``for`` statements on purpose: an
    ``ast.For``-only guard is satisfied by the idiomatic
    ``[check.horizon for check in trace.horizon_checks]``, so a horizon loop written
    the natural way would sail straight through it.

    Note *where* the check looks. An ``ast.comprehension`` node carries only the
    comprehension's own ``target`` / ``iter`` / ``ifs`` -- not the element
    expression -- so dumping it would miss ``check.horizon`` entirely and pass. The
    dump is therefore taken over the enclosing comprehension *expression*
    (``ListComp`` / ``SetComp`` / ``DictComp`` / ``GeneratorExp``), which holds both
    the element and the generators.
    """
    module = _harness_module()
    tree = ast.parse(inspect.getsource(module))

    assert not hasattr(module, "Horizon"), "importing Horizon invites a loop over it"
    for node in ast.walk(tree):
        if isinstance(node, ast.For):
            assert "horizon" not in ast.dump(node.iter).lower(), (
                "the harness iterates horizons itself; that is SKILL.md's job"
            )
        if isinstance(node, ast.ListComp | ast.SetComp | ast.DictComp | ast.GeneratorExp):
            assert "horizon" not in ast.dump(node).lower(), (
                "the harness iterates horizons in a comprehension itself; that is SKILL.md's job"
            )
        if isinstance(node, ast.Attribute):
            assert node.attr not in {"all_horizons", "horizons"}, "the harness enumerates the horizon set"


def test_the_harness_does_not_write_the_agents_claimed_verdict() -> None:
    """``set_final`` is the eval layer's call, not the harness's (Phase 5)."""
    assert "set_final" not in _code_strings(ast.parse(inspect.getsource(_harness_module())))


def test_the_harness_resolves_the_registry_without_importing_a_tool_body() -> None:
    """The registry is looked up by key, so no tool is reachable by name in code."""
    module = _harness_module()
    registry, specs = module._resolve_registry(None)

    assert set(registry) == set(TOOL_FUNCTIONS), "the canonical registry is the default"
    assert [spec["name"] for spec in specs] == [tool.value for tool in ToolName], (
        "the advertised specs are the frozen ones, in registry order"
    )
    assert module._resolve_registry({}) == ({}, []), "an explicit empty registry advertises nothing"


def test_the_model_receives_the_skill_text_verbatim_or_nothing_at_all() -> None:
    """The single variable: SKILL.md text appended, or nothing appended."""
    with_skill = StubProvider([_final_answer()])
    without_skill = StubProvider([_final_answer()])

    _run(with_skill, skill_enabled=True)
    _run(without_skill, skill_enabled=False)

    treatment = with_skill.system_prompts()[0]
    baseline = without_skill.system_prompts()[0]

    assert treatment.startswith(HARNESS_BASE_SYSTEM_PROMPT)
    assert baseline == HARNESS_BASE_SYSTEM_PROMPT
    assert len(treatment) > len(baseline)


def test_the_context_snapshot_the_model_sees_is_the_one_it_was_given() -> None:
    """Byte-identical JSON in both conditions (spec 3.1)."""
    overrides = {"scenario_id": "S04_edge", "time_state": {"analysis_time": "2026-05-18T14:00:00Z", "sunset_time": "2026-05-18T18:40:00Z", "minutes_to_sunset": 280.0}}
    payload = json.dumps(make_context_dict(**overrides), sort_keys=True)

    for skill_enabled in (False, True):
        provider = StubProvider([_final_answer()])
        _run(provider, skill_enabled=skill_enabled, context_override=overrides)

        user_message = provider.user_messages()[0]
        assert payload == json.dumps(
            json.loads(user_message.split("spec section 9):\n", 1)[1]), sort_keys=True
        ), "the snapshot is serialized unchanged, not re-shaped per condition"
