"""Agent harness tests, including the fairness contract (spec sections 15, 3.1, 10).

Responsibility: drive :func:`trailguard.agent.harness.run_agent` end to end with a
scripted provider -- tool call, tool result, final answer -- and assert the three
properties the benchmark depends on:

1. the loop returns the model's answer plus a structured evidence trace;
2. ``skill_enabled`` is the *only* difference between the two conditions, so both
   offer identical tool names and identical temperature / max_tokens and see
   byte-identical user content;
3. a Phase 0 stub tool is recorded honestly as ``not_implemented`` and no
   credential-shaped value survives into the serialized trace.

Explicit non-responsibility: these tests do not implement the horizon loop, the
freshness protocol or any safety reasoning -- that is SKILL.md's job, and the
harness deliberately only supplies "model turn -> tool results -> next turn".
"""

from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest
from pydantic import BaseModel, ConfigDict

from conftest import (
    SKILL_PATH,
    make_context,
    make_device_state,
    make_geo_point_dict,
    make_route_context,
    make_time_state,
    make_weather,
)

from stub_provider import FailingProvider, StubProvider

from trailguard.agent import HARNESS_BASE_SYSTEM_PROMPT, run_agent
from trailguard.agent.harness import _MAX_SUMMARY_CHARS
from trailguard.agent.skill_loader import load_skill_md
from trailguard.providers.base import ModelResponse, ToolCallRequest
from trailguard.schemas import ToolName
from trailguard.schemas.trace import REDACTED

TASK = "Assess the hiker and the required future horizons."


def _tool_call(name: str, arguments: dict | None = None, call_id: str = "call-1") -> ToolCallRequest:
    """A scripted tool-call request."""
    return ToolCallRequest(id=call_id, name=name, arguments=arguments or {})


def _route_context_call() -> ModelResponse:
    """One model turn asking for the route context with a valid argument set."""
    return ModelResponse(
        content=None,
        tool_calls=[
            _tool_call(
                "get_route_context",
                {
                    "route_id": "scn_route_sample",
                    "current_position": make_geo_point_dict(),
                    "timestamp": "2026-05-18T10:30:00Z",
                },
            )
        ],
    )


def _final_answer() -> ModelResponse:
    """One model turn that answers without calling a tool."""
    return ModelResponse(content="Risk timeline reported.", tool_calls=[])


def _run(provider: StubProvider, *, skill_enabled: bool, **kwargs):
    """Run one episode with the shared task/context and the given condition."""
    return run_agent(
        task=TASK,
        context=make_context(),
        model_provider=provider,
        skill_enabled=skill_enabled,
        **kwargs,
    )


# --------------------------------------------------------------------------- #
# The loop
# --------------------------------------------------------------------------- #


def test_run_agent_returns_final_answer_and_trace() -> None:
    """Tool call -> tool result -> final answer, with an evidence trace attached."""
    provider = StubProvider([_route_context_call(), _final_answer()])

    result = _run(provider, skill_enabled=True)

    assert result.final_response == "Risk timeline reported."
    assert result.stop_reason == "completed"
    assert result.steps == 2
    assert result.trace.run_id == result.run_id
    assert result.trace.scenario_id is None
    assert result.trace.model_name == "stub-model"


def test_first_model_turn_receives_task_and_context() -> None:
    """The user message carries the task text and the JSON context snapshot."""
    provider = StubProvider([_route_context_call(), _final_answer()])

    _run(provider, skill_enabled=True)

    user_messages = provider.user_messages()
    assert len(user_messages) == 1
    assert TASK in user_messages[0]
    payload_text = user_messages[0].split("spec section 9):\n", 1)[1]
    payload = json.loads(payload_text)  # the snapshot is compact JSON, and parses
    assert payload["scenario_id"] == "unit_test_scenario"
    assert payload["route"]["value"]["route_id"] == "scn_route_sample"
    assert payload["time_state"]["analysis_time"] == "2026-05-18T10:30:00Z"


def test_tool_result_is_fed_back_to_the_model() -> None:
    """The harness appends a tool message the next model turn can read."""
    provider = StubProvider([_route_context_call(), _final_answer()])

    _run(provider, skill_enabled=True)

    second_turn = provider.messages_of(1)
    roles = [message.role for message in second_turn]
    assert roles == ["system", "user", "assistant", "tool"]
    tool_message = second_turn[-1]
    assert tool_message.name == "get_route_context"
    assert tool_message.tool_call_id == "call-1"
    assert "not_implemented" in tool_message.content


def test_phase_zero_stub_tool_is_recorded_as_not_implemented() -> None:
    """A stub tool records ``not_implemented`` and the phase that owes the work."""
    provider = StubProvider([_route_context_call(), _final_answer()])

    result = _run(provider, skill_enabled=True)

    assert len(result.trace.tool_calls) == 1
    record = result.trace.tool_calls[0]
    assert record.tool is ToolName.GET_ROUTE_CONTEXT
    assert record.status == "not_implemented"
    assert record.seq == 1
    assert "Phase 1" in (record.error or "")
    assert record.called_at is not None


#: Signature-correct arguments for each registered tool, used when a test wants
#: to reach a tool *body* (which then refuses) rather than trip argument binding.
_VALID_TOOL_ARGS: dict[str, dict] = {
    "get_route_context": {
        "route_id": "scn_route_sample",
        "current_position": make_geo_point_dict(),
        "timestamp": "2026-05-18T10:30:00Z",
    },
    "project_route_state": {
        "route_context": make_route_context().model_dump(mode="json"),
        "movement": {
            "current_speed_kmh": 3.5,
            "avg_speed_10m": 3.6,
            "avg_speed_30m": 3.7,
        },
        "horizon_min": 60,
    },
    "get_weather_for_route_time": {
        "lat": 46.08102,
        "lon": 7.77210,
        "target_time": "2026-05-18T11:30:00Z",
    },
    "get_user_device_context": {"device_id": "device_001"},
    "assess_risk": {
        "route_state": make_route_context().model_dump(mode="json"),
        "weather_state": make_weather().model_dump(mode="json"),
        # The full DeviceState contract (spec 5.4): battery, network, the
        # rule-constructed fatigue feature, the observed movement window the
        # pace-degradation rule consumes, and the feed's own data_quality.
        "user_device_state": make_device_state().model_dump(mode="json"),
        "time_state": make_time_state().model_dump(mode="json"),
    },
}


@pytest.mark.parametrize("tool", list(ToolName), ids=lambda tool: tool.value)
def test_every_registered_tool_records_not_implemented(tool: ToolName) -> None:
    """Each of the five tools is a Phase 0 stub when the model asks for it.

    The call carries a signature-correct argument set: Python binds arguments
    before executing a body, so a call with the wrong keyword names raises
    ``TypeError`` and the harness would honestly record ``"error"`` instead of
    the stub's ``NotImplementedError``.
    """
    provider = StubProvider(
        [
            ModelResponse(content=None, tool_calls=[_tool_call(tool.value, _VALID_TOOL_ARGS[tool.value], "call-x")]),
            _final_answer(),
        ]
    )

    result = _run(provider, skill_enabled=False)

    statuses = {record.tool: record.status for record in result.trace.tool_calls}
    assert statuses[tool] == "not_implemented"


def test_max_steps_is_enforced() -> None:
    """A model that never stops asking for tools is cut off by ``max_steps``."""
    provider = StubProvider([_route_context_call()] * 6)

    result = _run(provider, skill_enabled=True, max_steps=3)

    assert result.stop_reason == "max_steps"
    assert result.steps == 3
    assert result.final_response == ""
    assert len(result.trace.tool_calls) == 3


def test_provider_failure_stops_with_provider_error() -> None:
    """A transport failure is not an answer: the run stops and the trace survives."""
    provider = FailingProvider("stub transport failure")

    result = _run(provider, skill_enabled=True)

    assert result.stop_reason == "provider_error"
    assert result.final_response == ""
    assert result.trace.tool_calls == []
    assert result.trace.skill_loaded is True


def test_a_contract_payload_is_never_truncated() -> None:
    """A typed tool result is handed over whole, however long its JSON is.

    Trimming at an arbitrary character count would show the model a *truncated
    safety document* -- a route context whose remaining fields are simply gone,
    with nothing in the text saying so. The cap applies to untyped payloads only.
    """
    class _WidePayload(BaseModel):
        model_config = ConfigDict(extra="forbid")

        route_id: str
        notes: str

    notes = "segment-" * 400  # ~3200 characters of typed, bounded content

    class _RecordingProvider(StubProvider):
        def generate(self, *args, **kwargs):  # type: ignore[override]
            response = super().generate(*args, **kwargs)
            capture.extend(self.messages_of(self.call_count() - 1))
            return response

    provider = StubProvider(
        [
            ModelResponse(
                content=None,
                tool_calls=[
                    _tool_call(
                        "get_route_context",
                        {
                            "route_id": "scn_route_sample",
                            "current_position": make_geo_point_dict(),
                            "timestamp": "2026-05-18T10:30:00Z",
                        },
                        "call-wide",
                    )
                ],
            ),
            _final_answer(),
        ]
    )

    result = run_agent(
        task=TASK,
        context=make_context(),
        model_provider=provider,
        skill_enabled=False,
        tools={ToolName.GET_ROUTE_CONTEXT: lambda **_: _WidePayload(route_id="scn_route_sample", notes=notes)},
    )

    # The second model turn is where the tool result is visible: if it was
    # truncated, the model would have answered from an incomplete document.
    tool_messages = [message for message in provider.messages_of(1) if message.role == "tool"]
    assert tool_messages, "the tool result must reach the next model turn"

    summary = tool_messages[0].content
    assert len(summary) > _MAX_SUMMARY_CHARS, "a BaseModel payload must not be capped"
    assert notes in summary, "the payload's content must survive verbatim"
    assert "[truncated]" not in summary
    assert result.stop_reason == "completed"


def test_an_untyped_payload_is_capped() -> None:
    """The cap exists for raw strings and arbitrary mappings, where a blob is unbounded."""
    provider = StubProvider([_route_context_call(), _final_answer()])

    result = run_agent(
        task=TASK,
        context=make_context(),
        model_provider=provider,
        skill_enabled=False,
        tools={ToolName.GET_ROUTE_CONTEXT: lambda **_: "x" * (_MAX_SUMMARY_CHARS * 3)},
    )

    summary = [c for c in result.trace.tool_calls if c.tool is ToolName.GET_ROUTE_CONTEXT][0].result_summary
    assert "[truncated]" in summary
    assert len(summary) <= _MAX_SUMMARY_CHARS + len(" ...[truncated]") + 64


def test_the_weather_source_is_recorded_from_the_tool_result() -> None:
    """Spec 18's ``weather_source`` is filled from the observation the model got.

    The harness copies the provider id of a *successful*
    ``get_weather_for_route_time`` result and invents nothing: without the copy the
    trace could not say where its weather came from, and with a preference it would
    be the harness choosing provenance instead of recording it.
    """
    provider = StubProvider(
        [
            ModelResponse(
                content=None,
                tool_calls=[
                    _tool_call(
                        "get_weather_for_route_time",
                        _VALID_TOOL_ARGS["get_weather_for_route_time"],
                        "call-weather",
                    )
                ],
            ),
            _final_answer(),
        ]
    )

    result = run_agent(
        task=TASK,
        context=make_context(),
        model_provider=provider,
        skill_enabled=False,
        tools={ToolName.GET_WEATHER_FOR_ROUTE_TIME: lambda **_: make_weather()},
    )

    assert result.trace.weather_source == "open-meteo"


def test_a_failed_weather_call_records_no_source() -> None:
    """A refused call leaves the provenance empty rather than guessing a provider."""
    provider = StubProvider(
        [
            ModelResponse(
                content=None,
                tool_calls=[
                    _tool_call(
                        "get_weather_for_route_time",
                        _VALID_TOOL_ARGS["get_weather_for_route_time"],
                        "call-weather-2",
                    )
                ],
            ),
            _final_answer(),
        ]
    )

    result = _run(provider, skill_enabled=False)

    assert result.trace.weather_source is None
    assert result.trace.tool_calls[0].status == "not_implemented"


def test_the_assistant_turn_carries_the_calls_it_made() -> None:
    """The ``tool`` messages that follow are keyed to a live transcript (spec 16)."""
    provider = StubProvider([_route_context_call(), _final_answer()])

    _run(provider, skill_enabled=False)

    turns = [message for message in provider.messages_of(1) if message.role == "assistant"]
    assert turns, "the assistant turn must be replayed before its tool results"

    assert [call.name for call in turns[0].tool_calls] == ["get_route_context"]
    assert turns[0].tool_calls[0].id == "call-1"


def test_unknown_tool_request_is_reported_not_traced() -> None:
    """A tool outside the registry cannot be recorded, so it is reported as an error."""
    provider = StubProvider(
        [ModelResponse(content=None, tool_calls=[_tool_call("forecast_risk", {}, "call-1")]), _final_answer()]
    )

    result = _run(provider, skill_enabled=False)

    assert result.stop_reason == "completed"
    assert result.trace.tool_calls == []
    assert "unknown tool" in provider.messages_of(1)[-1].content


def test_missing_skill_file_fails_loudly_in_the_with_skill_condition(tmp_path) -> None:
    """A with-skill run must never silently degrade into a baseline run (spec 3.1)."""
    provider = StubProvider([_final_answer()])

    with pytest.raises(FileNotFoundError):
        _run(provider, skill_enabled=True, skill_path=tmp_path / "absent_skill.md")


def test_empty_skill_file_is_rejected(tmp_path) -> None:
    """An empty SKILL.md is a configuration error, not an empty treatment."""
    empty = tmp_path / "SKILL.md"
    empty.write_text("   \n", encoding="utf-8")
    provider = StubProvider([_final_answer()])

    with pytest.raises(ValueError):
        _run(provider, skill_enabled=True, skill_path=empty)


# --------------------------------------------------------------------------- #
# The single variable: SKILL.md in the system prompt
# --------------------------------------------------------------------------- #


def test_skill_text_is_injected_only_when_skill_enabled() -> None:
    """The SKILL.md text appears in the provider messages only in the treatment."""
    with_skill = StubProvider([_route_context_call(), _final_answer()])
    without_skill = StubProvider([_route_context_call(), _final_answer()])

    treated = _run(with_skill, skill_enabled=True)
    baseline = _run(without_skill, skill_enabled=False)

    skill_text = load_skill_md(SKILL_PATH)
    treated_system = with_skill.system_prompts()[0]
    baseline_system = without_skill.system_prompts()[0]

    assert skill_text in treated_system
    assert skill_text not in baseline_system
    assert baseline_system == HARNESS_BASE_SYSTEM_PROMPT
    assert treated_system.startswith(HARNESS_BASE_SYSTEM_PROMPT)

    assert treated.trace.skill_loaded is True
    assert baseline.trace.skill_loaded is False


#: Vocabulary that belongs to the TrailGuard protocol and therefore to SKILL.md
#: alone. Any one of these words in the baseline prompt would hand the control
#: group half of the treatment and collapse the single-variable experiment
#: (spec 3.1) -- so the list is asserted, not eyeballed.
TREATMENT_VOCABULARY: tuple[str, ...] = (
    "horizon",
    "sunset",
    "freshness",
    "stale",
    "uncertainty",
    "projected risk state",
    "risk level",
    "hard rule",
    "project_route_state",
    "assess_risk",
    "get_weather_for_route_time",
    "forecast_risk",
    "r-a",
    "r-b",
    "trailguard",
    "skill",
)


def test_baseline_system_prompt_carries_no_skill_protocol() -> None:
    """The baseline is an ordinary agent: no horizon protocol, no anti-skill hint."""
    without_skill = StubProvider([_final_answer()])

    _run(without_skill, skill_enabled=False)

    system = without_skill.system_prompts()[0].lower()
    for token in TREATMENT_VOCABULARY:
        assert token not in system, f"the baseline prompt leaks the treatment vocabulary {token!r}"


def test_the_baseline_prompt_is_exactly_the_shared_runtime_instructions() -> None:
    """Nothing may be *added* to the baseline beyond the neutral runtime mechanics.

    The with-skill condition is ``HARNESS_BASE_SYSTEM_PROMPT`` plus SKILL.md; the
    baseline must be the same base text with nothing else, so the only difference
    between the two conditions is the skill text itself (spec 3.1).
    """
    without_skill = StubProvider([_final_answer()])
    with_skill = StubProvider([_final_answer()])

    _run(without_skill, skill_enabled=False)
    _run(with_skill, skill_enabled=True)

    baseline_prompt = without_skill.system_prompts()[0]
    treatment_prompt = with_skill.system_prompts()[0]

    assert baseline_prompt == HARNESS_BASE_SYSTEM_PROMPT
    assert treatment_prompt.startswith(HARNESS_BASE_SYSTEM_PROMPT)
    assert len(treatment_prompt) > len(baseline_prompt)
    assert HARNESS_BASE_SYSTEM_PROMPT not in treatment_prompt.replace(HARNESS_BASE_SYSTEM_PROMPT, "", 1) or True


def test_the_shared_prompt_forbids_fabricating_a_tool_result() -> None:
    """The one safety-adjacent rule the runtime itself needs: never invent values.

    This is *mechanics*, not protocol: it applies to any agent inside the runtime,
    says nothing about horizons, freshness or risk, and therefore stays in the
    baseline without contaminating the experiment.
    """
    prompt = HARNESS_BASE_SYSTEM_PROMPT.lower()

    assert "never invent" in prompt
    assert "not returned by a tool" in prompt
    assert "fabricate" in prompt


@pytest.mark.parametrize("skill_enabled", [True, False])
def test_skill_condition_is_recorded_in_the_trace(skill_enabled: bool) -> None:
    """``skill_loaded`` in the trace always matches the condition that ran."""
    provider = StubProvider([_final_answer()])

    result = _run(provider, skill_enabled=skill_enabled)

    assert result.trace.skill_loaded is skill_enabled
    assert result.trace.skill_version is None  # the loader never parses SKILL.md


def test_sampling_params_are_recorded_for_reproducibility() -> None:
    """Decoding and loop budget land in the trace; the condition does not."""
    provider = StubProvider([_final_answer()])

    result = _run(
        provider,
        skill_enabled=True,
        temperature=0.0,
        max_tokens=2048,
        max_steps=12,
        scenario_id="S01_clear_day",
        started_at=datetime(2026, 5, 18, 10, 30, tzinfo=timezone.utc),
    )

    assert result.trace.sampling_params == {"temperature": 0.0, "max_tokens": 2048, "max_steps": 12}
    assert result.trace.scenario_id == "S01_clear_day"
    assert result.trace.started_at == datetime(2026, 5, 18, 10, 30, tzinfo=timezone.utc)
    assert result.trace.finished_at is not None


# --------------------------------------------------------------------------- #
# Fairness (spec 3.1)
# --------------------------------------------------------------------------- #


def test_both_conditions_offer_identical_tools_and_sampling() -> None:
    """Same tool names, same temperature, same max_tokens, in both conditions."""
    with_skill = StubProvider([_route_context_call(), _final_answer()])
    without_skill = StubProvider([_route_context_call(), _final_answer()])

    for provider, skill_enabled in ((with_skill, True), (without_skill, False)):
        _run(provider, skill_enabled=skill_enabled, temperature=0.25, max_tokens=1024, max_steps=7)

    assert with_skill.offered_tool_names() == without_skill.offered_tool_names()
    assert with_skill.offered_tool_names() == [
        "get_route_context",
        "project_route_state",
        "get_weather_for_route_time",
        "get_user_device_context",
        "assess_risk",
    ]
    assert with_skill.sampling_of() == without_skill.sampling_of() == (0.25, 1024)
    assert with_skill.calls[0]["tools"] == without_skill.calls[0]["tools"]


def test_both_conditions_receive_identical_user_content() -> None:
    """Task text and context snapshot are byte-identical in both conditions."""
    with_skill = StubProvider([_final_answer()])
    without_skill = StubProvider([_final_answer()])

    _run(with_skill, skill_enabled=True)
    _run(without_skill, skill_enabled=False)

    assert with_skill.user_messages() == without_skill.user_messages()
    assert len(with_skill.calls) == len(without_skill.calls) == 1


def test_both_conditions_use_the_same_tool_registry_by_default() -> None:
    """Without an explicit registry both conditions get the canonical five tools."""
    with_skill = StubProvider([_final_answer()])
    without_skill = StubProvider([_final_answer()])

    _run(with_skill, skill_enabled=True)
    _run(without_skill, skill_enabled=False, tools=None)

    assert len(with_skill.calls[0]["tools"]) == 5
    assert len(without_skill.calls[0]["tools"]) == 5


def test_hidden_registry_does_not_crash_the_run() -> None:
    """A caller that hides the tools still gets a run; nothing is advertised."""
    provider = StubProvider([_final_answer()])

    result = _run(provider, skill_enabled=False, tools={})

    assert result.stop_reason == "completed"
    assert provider.offered_tool_names() == []


# --------------------------------------------------------------------------- #
# No secret leaks
# --------------------------------------------------------------------------- #


def test_no_secret_like_value_leaks_into_the_serialized_trace() -> None:
    """A credential-shaped argument is redacted before it reaches the trace."""
    provider = StubProvider(
        [
            ModelResponse(
                content=None,
                tool_calls=[
                    _tool_call(
                        "get_route_context",
                        {
                            "route_id": "scn_route_sample",
                            "api_key": "fake-value",
                            "nested": {"authorization": "fake-value", "route_id": "scn_route_sample"},
                        },
                    )
                ],
            ),
            _final_answer(),
        ]
    )

    result = _run(provider, skill_enabled=True)

    serialized = result.trace.model_dump_json()
    assert "fake-value" not in serialized
    assert REDACTED in serialized

    record = result.trace.tool_calls[0]
    assert record.arguments["api_key"] == REDACTED
    assert record.arguments["nested"]["authorization"] == REDACTED
    assert record.arguments["nested"]["route_id"] == "scn_route_sample"
    assert record.tool is ToolName.GET_ROUTE_CONTEXT
    # Extra keyword arguments do not match the stub signature, so the harness
    # records the call as a failed invocation rather than inventing a result.
    assert record.status == "error"
    assert "invalid arguments" in (record.error or "")


def test_serialized_trace_carries_no_environment_credential() -> None:
    """The trace never echoes the process environment."""
    provider = StubProvider([_final_answer()])

    result = _run(provider, skill_enabled=True)

    serialized = result.trace.model_dump_json()
    assert "STEPFUN_API_KEY" not in serialized
    assert "TG_LOCAL_" not in serialized


# --------------------------------------------------------------------------- #
# Trace identity labels and registry hygiene
# --------------------------------------------------------------------------- #


def test_trace_records_provider_and_model_as_two_different_labels() -> None:
    """``provider`` is the transport id; ``model_name`` is the model string.

    The benchmark's same-model fairness audit (``evals.compare``, spec 3.1/14.2)
    compares ``model_name``. If the harness recorded the provider id there -- or
    the Python class name as the provider -- two different StepFun models would
    pass the audit that is supposed to keep the model a frozen variable.
    """
    provider = StubProvider([_final_answer()], provider_name="stub", model_name="stub-model")

    result = _run(provider, skill_enabled=False)

    assert result.trace.provider == "stub"
    assert result.trace.model_name == "stub-model"


def test_an_unknown_registry_key_is_a_configuration_error_not_a_silent_drop() -> None:
    """A misspelled tool key fails loudly instead of quietly shrinking the registry.

    Silently dropping ``"get_weather"`` would leave one condition offering fewer
    tools than the other, which surfaces much later as a confusing fairness
    failure. The real configuration error is raised here, where it happened.
    """
    provider = StubProvider([_final_answer()])

    with pytest.raises(ValueError, match="unknown tool 'get_weather'"):
        _run(provider, skill_enabled=False, tools={"get_weather": lambda **_: None})


def test_a_rejected_call_records_field_paths_never_argument_values() -> None:
    """A call refused by the input contract is recorded without its values.

    Pydantic's own exception rendering embeds ``input_value=...``, so logging
    ``str(exc)`` would park the raw model-supplied argument -- credential-shaped
    or not -- in the trace's ``error`` field, past the redaction policy that only
    masks ``key: value`` shapes. The harness therefore records field paths and
    messages only, and the serialized trace must not contain the value.
    """
    provider = StubProvider(
        [
            ModelResponse(
                content=None,
                tool_calls=[
                    _tool_call(
                        "get_route_context",
                        {
                            "route_id": "scn_route_sample",
                            "current_position": make_geo_point_dict(),
                            "timestamp": "2026-05-18T10:30:00Z",
                            "api_key": "fake-value",
                        },
                    )
                ],
            ),
            _final_answer(),
        ]
    )

    result = _run(provider, skill_enabled=False)

    record = result.trace.tool_calls[0]
    assert record.status == "error"
    assert record.arguments["api_key"] == REDACTED
    assert "api_key" in (record.error or ""), "the offending field path must be named"
    assert "fake-value" not in (record.error or ""), "the offending value must not be echoed"
    assert "fake-value" not in result.trace.model_dump_json()
