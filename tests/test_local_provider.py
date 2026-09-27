"""LocalModelProvider tests: the DGX Spark transport, offline (spec 16, 13, 3.1).

``tests/test_providers.py`` asserts that the two providers of the frozen
evaluation matrix are *interchangeable*. This module covers what is specific to
the local backend: that it forwards the transcript, the five tool schemas and
the sampling parameters faithfully; that structured ``tool_calls`` survive the
round trip with their ids intact; that a malformed arguments payload becomes a
visible ``invalid_tool_call`` rather than an invented empty dict; that every
transport failure kind is classified instead of being answered; and -- the one
that matters most -- that a full agent loop driven by the real local provider
produces exactly the run a scripted double produces, through the *same*
``run_agent()``, with no Qwen-specific branch anywhere in the harness.

Nothing here needs a GPU, a Docker container, a local server or a network. Every
response is served by ``httpx.MockTransport`` wrapped around a real
``openai.OpenAI`` client, which is what makes the timeout, 429, 400, 5xx and
malformed-payload paths real tests instead of promises. Conftest's autouse socket
guard closes the wire for the whole suite, so the guarantee is structural
(spec 13).

Read together with ``scripts/test_tool_calling.py`` on the Spark box: that probe
proves the *server* returns native structured calls, and this module proves the
*client* neither adds to nor rescues them. Neither is a substitute for the
other, and neither is allowed to make the other's evidence.
"""

from __future__ import annotations

import ast
import hashlib
import inspect
import json
import os
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import httpx
import openai
import pytest

from conftest import (
    make_context,
    make_route_context,
    make_time_state,
    make_weather,
)
from stub_provider import StubProvider

from trailguard.agent import run_agent
from trailguard.agent.harness import HARNESS_BASE_SYSTEM_PROMPT
from trailguard.agent.skill_loader import load_skill_md
from trailguard.providers.base import (
    ChatMessage,
    ModelResponse,
    ProviderAuthenticationError,
    ProviderError,
    ProviderRateLimited,
    ProviderResponseError,
    ProviderServerError,
    ProviderTimeout,
    ProviderUnavailable,
    ToolCallRequest,
)
from trailguard.providers.local import LocalModelProvider
from trailguard.schemas import ToolName
from trailguard.tools import TOOL_FUNCTIONS, tool_schema_dicts

REPO_ROOT = Path(__file__).resolve().parents[1]
SKILL_PATH = REPO_ROOT / "SKILL.md"
RULES_PATH = REPO_ROOT / "trailguard" / "rules" / "rules_v1.yaml"

#: Byte identity of the two frozen assets this phase must not touch. Pinned so a
#: skill or rule pack edited "just for the local model" fails here rather than
#:: silently becoming the treatment of a benchmark (spec 3.1, 3.2).
FROZEN_SHA256: dict[str, str] = {
    "SKILL.md": "98fd007eb23aee18f98bf67c295d7bad3c8e48f7a722022c852218fb0c649769",
    "trailguard/rules/rules_v1.yaml": (
        "b2630f3d32e50d6533a01172fbdd4f5db670fcdc474a2c8ebbf1bcebda71a5d4"
    ),
}

TASK = "Assess the hiker and the required future horizons."

#: Stand-in credential. A local vLLM deployment authenticates nobody, but the SDK
#: requires *something* in the header; this is an obviously-not-a-key string so a
#: leaked real one would be a test failure rather than a silent pass.
FAKE_CREDENTIAL = "not-a-real-credential"

#: Never resolved, never connected to: the MockTransport intercepts everything.
STUB_BASE_URL = "http://spark.invalid:8000/v1"
STUB_MODEL = "qwen3-vl-30b-a3b"

#: One transcript in the provider-agnostic shape, exactly as the harness builds
#: it. Every test drives the real translator, so this must be real messages and
#: not raw dicts: a dict transcript would fail on attribute access and be caught
#: by the broad ``except`` in ``generate``, silently exercising the wrong path.
TRANSCRIPT: list[ChatMessage] = [
    ChatMessage(role="system", content="You are a general-purpose assistant."),
    ChatMessage(role="user", content="Assess this hiker."),
]

#: The Hermes envelope, assembled from fragments. Held as fragments for the same
#: reason the probe script holds them: a literal here would make this file part of
#: the pattern-matching surface instead of a witness to it.
ENVELOPE_OPEN: str = "<" + "tool" + "_call>"
ENVELOPE_CLOSE: str = "<" + "/tool" + "_call>"
ENVELOPE_RESPONSE_OPEN: str = "<" + "tool" + "_response>"
ENVELOPE_RESPONSE_CLOSE: str = "<" + "/tool" + "_response>"

#: One hand-written completion payload, shaped exactly as the endpoint sends it.
TOOL_CALL_ON_WIRE: dict[str, Any] = {
    "id": "chatcmpl-tool-local",
    "type": "function",
    "function": {
        "name": "get_test_temperature",
        "arguments": json.dumps({"city": "Reykjavik"}),
    },
}


# --------------------------------------------------------------------------- #
# Harness helpers
# --------------------------------------------------------------------------- #


def _completion(
    *,
    content: str | None = "Answer.",
    tool_calls: list[dict[str, Any]] | None = None,
    finish_reason: str = "stop",
    usage: dict[str, int] | None = None,
    drop_choices: bool = False,
    drop_message: bool = False,
    response_id: str = "cmpl-local-stub",
) -> dict[str, Any]:
    """A minimal OpenAI-compatible chat-completion payload."""
    message: dict[str, Any] = {"role": "assistant", "content": content}
    if tool_calls is not None:
        message["tool_calls"] = tool_calls
    payload: dict[str, Any] = {
        "id": response_id,
        "object": "chat.completion",
        "created": 0,
        "model": STUB_MODEL,
    }
    if drop_choices:
        payload["choices"] = []
    elif drop_message:
        payload["choices"] = [{"index": 0, "finish_reason": finish_reason}]
    else:
        payload["choices"] = [{"index": 0, "message": message, "finish_reason": finish_reason}]
    if usage is not None:
        payload["usage"] = usage
    return payload


def _assess_arguments() -> dict[str, Any]:
    """Contract-correct ``assess_risk`` arguments, built from the frozen factories."""
    return {
        "route_state": make_route_context().model_dump(mode="json"),
        "weather_state": make_weather(freshness_minutes=30.0).model_dump(mode="json"),
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


def _client_for(handler: Any, **kwargs: Any) -> tuple[LocalModelProvider, list[httpx.Request]]:
    """Return a local provider over ``handler``, and every request it sent.

    The client is injected through the documented dependency-injection point
    (spec 13): no credential, no socket, and a handler that can count requests --
    which is how the retry budget is verified rather than trusted.
    """
    seen: list[httpx.Request] = []

    def _recording(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return handler(request)

    client = openai.OpenAI(
        api_key=FAKE_CREDENTIAL,
        base_url=STUB_BASE_URL,
        max_retries=0,
        timeout=5.0,
        http_client=httpx.Client(transport=httpx.MockTransport(_recording)),
    )
    provider = LocalModelProvider(
        model=STUB_MODEL, base_url=STUB_BASE_URL, client=client, **kwargs
    )
    return provider, seen


def _body(request: httpx.Request) -> dict[str, Any]:
    """The JSON body of one recorded request."""
    return json.loads(request.content.decode("utf-8"))


def _always(payload: dict[str, Any]) -> Any:
    return lambda request: httpx.Response(200, json=payload)


def _status(status_code: int, body: str = "boom") -> Any:
    return lambda request: httpx.Response(status_code, json={"error": {"message": body}})


def _catch(provider: LocalModelProvider) -> Exception:
    """Return the exception one failing call raises, instead of asserting on it."""
    try:
        provider.generate(TRANSCRIPT, tools=None)
    except Exception as exc:  # the taxonomy test classifies what this returns
        return exc
    raise AssertionError("the provider returned an answer where it must fail")


# --------------------------------------------------------------------------- #
# Configuration (spec 16, 13)
# --------------------------------------------------------------------------- #


def test_the_default_endpoint_is_loopback_and_the_served_model_id() -> None:
    """No host, IP or port is baked in: the default is this machine's own server."""
    provider = LocalModelProvider()

    assert provider.base_url == "http://127.0.0.1:8000/v1"
    assert provider.model == "qwen3-vl-30b-a3b"
    assert provider.name == "local"

    host = urlparse(provider.base_url).hostname
    assert host in {"127.0.0.1", "localhost", "::1"}, (
        f"the default endpoint must be loopback, got {host!r}"
    )


def test_the_default_base_url_carries_no_remote_address() -> None:
    """A hardcoded Spark IP here would be a deployment frozen into the source."""
    provider = LocalModelProvider()

    for remote in ("dgx", "spark", "192.168.", "10.0.", "172.16."):
        assert remote not in provider.base_url.lower()
    assert "localhost" not in provider.base_url or "127.0.0.1" in provider.base_url


def test_the_environment_overrides_the_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TRAILGUARD_LOCAL_MODEL", "some-other-checkpoint")
    monkeypatch.setenv("TRAILGUARD_LOCAL_BASE_URL", "http://127.0.0.1:9000/v1")

    provider = LocalModelProvider()

    assert provider.model == "some-other-checkpoint"
    assert provider.base_url == "http://127.0.0.1:9000/v1"


def test_the_legacy_names_still_work_and_lose_to_the_new_ones(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Phase 0 shipped ``TG_LOCAL_*``; dropping it would break a working operator."""
    monkeypatch.setenv("TG_LOCAL_MODEL", "legacy-checkpoint")
    monkeypatch.setenv("TG_LOCAL_BASE_URL", "http://127.0.0.1:7777/v1")
    assert LocalModelProvider().model == "legacy-checkpoint"
    assert LocalModelProvider().base_url == "http://127.0.0.1:7777/v1"

    monkeypatch.setenv("TRAILGUARD_LOCAL_MODEL", "current-checkpoint")
    monkeypatch.setenv("TRAILGUARD_LOCAL_BASE_URL", "http://127.0.0.1:8888/v1")

    assert LocalModelProvider().model == "current-checkpoint"
    assert LocalModelProvider().base_url == "http://127.0.0.1:8888/v1"


def test_an_explicit_argument_beats_the_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TRAILGUARD_LOCAL_MODEL", "from-environment")
    monkeypatch.setenv("TRAILGUARD_LOCAL_BASE_URL", "http://127.0.0.1:1/v1")

    provider = LocalModelProvider(model="explicit", base_url="http://127.0.0.1:2/v1")

    assert provider.model == "explicit"
    assert provider.base_url == "http://127.0.0.1:2/v1"


def test_an_empty_environment_variable_is_not_a_value(monkeypatch: pytest.MonkeyPatch) -> None:
    """``VAR=`` in a checked-out .env must not configure an endpoint of empty string."""
    monkeypatch.setenv("TRAILGUARD_LOCAL_MODEL", "")
    monkeypatch.setenv("TRAILGUARD_LOCAL_BASE_URL", "")

    provider = LocalModelProvider()

    assert provider.model == "qwen3-vl-30b-a3b"
    assert provider.base_url == "http://127.0.0.1:8000/v1"


def test_the_documented_names_all_appear_in_the_env_example() -> None:
    """The third leg of the configuration contract: code, docs, and this file.

    ``test_providers.py`` checks the code reads only documented names. This
    closes the loop the other way: an operator who reads the example file must
    find every name the code honours, including the legacy ones, or a variable
    they are told to set would do nothing.
    """
    from trailguard.providers import local as local_module

    declared = {
        line.strip()
        for line in (REPO_ROOT / ".env.example").read_text(encoding="utf-8").splitlines()
    }

    documented = set(local_module._MODEL_ENV) | set(local_module._BASE_URL_ENV) | set(
        local_module._API_KEY_ENV
    )
    missing = sorted(
        name
        for name in documented
        if not any(line.startswith(f"{name}=") for line in declared)
    )
    assert not missing, (
        f".env.example does not declare {missing}; these names are honoured by the code"
    )

    # And the placeholder must ship empty, so the example carries no value at all.
    for line in (REPO_ROOT / ".env.example").read_text(encoding="utf-8").splitlines():
        name, sep, value = line.strip().partition("=")
        if sep and "LOCAL_API_KEY" in name:
            assert value == "", "the placeholder credential stays empty in the example"


def test_configuration_is_captured_at_construction_not_per_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider = LocalModelProvider(model="first", base_url="http://127.0.0.1:1/v1")

    monkeypatch.setenv("TRAILGUARD_LOCAL_MODEL", "second")
    monkeypatch.setenv("TRAILGUARD_LOCAL_BASE_URL", "http://127.0.0.1:2/v1")

    assert provider.model == "first", "the endpoint must not change mid-run"
    assert provider.base_url == "http://127.0.0.1:1/v1"


def test_building_the_provider_builds_no_client_and_reads_no_file() -> None:
    """No socket, no SDK import and no file at construction time (spec 13, 16)."""
    provider = LocalModelProvider(model="m", base_url="http://127.0.0.1:1/v1")

    assert provider._client_override is None, "the client is lazy, built on first use"


def test_a_nonpositive_timeout_is_refused_at_construction() -> None:
    """An infinite timeout is a hang wearing a provider (spec 12)."""
    with pytest.raises(ProviderUnavailable) as excinfo:
        LocalModelProvider(model="m", base_url="u", timeout_s=0.0)

    assert "timeout_s must be positive" in str(excinfo.value)


def test_a_retry_budget_above_two_is_refused_at_construction() -> None:
    for budget in (0, 3, 10):
        with pytest.raises(ProviderUnavailable) as excinfo:
            LocalModelProvider(model="m", base_url="u", max_attempts=budget)
        assert "max_attempts must be 1 or 2" in str(excinfo.value)


def test_the_repr_names_no_credential() -> None:
    """There is no secret here, and the repr must not pretend there is one.

    It also must not print one if an operator pointed this transport at a
    remote endpoint that insists on a real key.
    """
    provider = LocalModelProvider(
        model="m", base_url="http://127.0.0.1:8000/v1", api_key=FAKE_CREDENTIAL
    )

    rendered = repr(provider)

    assert "model='m'" in rendered
    assert "base_url='http://127.0.0.1:8000/v1'" in rendered
    assert FAKE_CREDENTIAL not in rendered
    assert "api_key" not in rendered
    assert "key" not in rendered.lower().replace("monkey", "")


def test_the_api_key_is_not_reachable_from_an_instance() -> None:
    """One private attribute, masked, and no accessor anywhere (spec 16)."""
    provider = LocalModelProvider(api_key=FAKE_CREDENTIAL)

    public = {name for name in dir(provider) if not name.startswith("_")}
    assert "api_key" not in public
    assert not any("key" in name.lower() for name in public)


# --------------------------------------------------------------------------- #
# What goes out on the wire (spec 16)
# --------------------------------------------------------------------------- #


def test_the_request_carries_the_model_the_temperature_and_the_token_cap() -> None:
    provider, seen = _client_for(_always(_completion()))

    provider.generate(TRANSCRIPT, tools=None, temperature=0.0, max_tokens=2048)

    body = _body(seen[0])
    assert body["model"] == STUB_MODEL
    assert body["temperature"] == 0.0
    assert body["max_tokens"] == 2048
    assert "tools" not in body, "no tool spec was offered, so none may be advertised"
    assert "tool_choice" not in body


def test_sampling_parameters_are_forwarded_verbatim() -> None:
    """Spec 3.1: the harness's decoding parameters reach the endpoint unchanged.

    A provider that rounded, clamped or defaulted them would give the two skill
    conditions different sampling without anyone noticing -- the single-variable
    experiment would be broken inside the transport.
    """
    provider, seen = _client_for(_always(_completion()))

    provider.generate(TRANSCRIPT, tools=None, temperature=0.37, max_tokens=777)

    body = _body(seen[0])
    assert body["temperature"] == 0.37
    assert body["max_tokens"] == 777


def test_the_transcript_is_forwarded_in_order_and_unedited() -> None:
    provider, seen = _client_for(_always(_completion()))

    provider.generate(TRANSCRIPT, tools=None)

    body = _body(seen[0])
    assert [m["role"] for m in body["messages"]] == ["system", "user"]
    assert body["messages"][0]["content"] == TRANSCRIPT[0].content
    assert body["messages"][1]["content"] == TRANSCRIPT[1].content


def test_the_five_registered_tools_reach_the_wire_in_the_endpoints_shape() -> None:
    """The advertised contract is the registry's, translated -- never rewritten."""
    provider, seen = _client_for(_always(_completion()))
    registered = tool_schema_dicts()

    provider.generate(TRANSCRIPT, tools=registered)

    tools = _body(seen[0])["tools"]
    assert len(tools) == len(registered) == 5
    for advertised, spec in zip(tools, registered, strict=True):
        assert advertised["type"] == "function"
        assert advertised["function"]["name"] == spec["name"]
        assert advertised["function"]["description"] == spec["description"]
        assert advertised["function"]["parameters"] == spec["input_schema"], (
            "the arguments schema must arrive byte-identical to the registry's"
        )
    assert _body(seen[0])["tool_choice"] == "auto"


def test_tool_choice_auto_is_a_property_of_the_endpoint_not_the_skill_condition() -> None:
    """Both skill conditions must send the identical request shape (spec 3.1)."""
    provider, seen = _client_for(_always(_completion()))
    tools = tool_schema_dicts()

    provider.generate(TRANSCRIPT, tools=tools, temperature=0.0, max_tokens=2048)

    body = _body(seen[0])
    assert body["tool_choice"] == "auto"
    # The skill text lives in the transcript, not in a request knob: nothing
    # skill-conditional may appear in the request envelope.
    assert not any("skill" in key.lower() for key in body)
    assert body["temperature"] == 0.0 and body["max_tokens"] == 2048


def test_a_tool_spec_without_an_arguments_contract_is_refused_loudly() -> None:
    provider, _ = _client_for(_always(_completion()))

    with pytest.raises(ProviderResponseError) as excinfo:
        provider.generate(TRANSCRIPT, tools=[{"name": "assess_risk"}])

    assert "input_schema" in str(excinfo.value)
    assert "tool_schema_dicts" in str(excinfo.value)


def test_a_tool_spec_in_the_endpoints_own_shape_is_also_accepted() -> None:
    """``parameters`` instead of ``input_schema`` -- the same contract, other name.

    Both providers face both shapes, and a run that crashed on one of them would
    be a configuration accident recorded as a model failure.
    """
    provider, seen = _client_for(_always(_completion()))

    provider.generate(
        TRANSCRIPT,
        tools=[{
            "name": "assess_risk",
            "description": "Run the core once.",
            "parameters": {"type": "object", "properties": {"horizon": {"type": "string"}}},
        }],
    )

    advertised = _body(seen[0])["tools"][0]
    assert advertised["function"]["name"] == "assess_risk"
    assert advertised["function"]["parameters"] == {
        "type": "object", "properties": {"horizon": {"type": "string"}}
    }


def test_an_assistant_turn_carries_its_calls_and_the_result_answers_by_id() -> None:
    """The transcript the endpoint needs to pair a result back to its call."""
    provider, seen = _client_for(_always(_completion()))
    transcript = [
        ChatMessage(role="system", content="base"),
        ChatMessage(
            role="assistant",
            content="",
            tool_calls=[
                ToolCallRequest(
                    id="chatcmpl-tool-local",
                    name="assess_risk",
                    arguments={"horizon": "NOW"},
                )
            ],
        ),
        ChatMessage(
            role="tool",
            content="risk_level=RED confidence=0.8",
            tool_call_id="chatcmpl-tool-local",
            name="assess_risk",
        ),
    ]

    provider.generate(transcript, tools=None)

    sent = _body(seen[0])["messages"]
    assistant = sent[1]
    assert assistant["role"] == "assistant"
    assert assistant["tool_calls"][0]["id"] == "chatcmpl-tool-local", (
        "the id the server assigned is the id handed back, verbatim"
    )
    assert assistant["tool_calls"][0]["function"]["name"] == "assess_risk"
    assert json.loads(assistant["tool_calls"][0]["function"]["arguments"]) == {"horizon": "NOW"}

    tool_message = sent[2]
    assert tool_message["role"] == "tool"
    assert tool_message["tool_call_id"] == "chatcmpl-tool-local"
    assert tool_message["name"] == "assess_risk"
    assert tool_message["content"] == "risk_level=RED confidence=0.8"


def test_provider_order_is_preserved_so_results_answer_the_right_call() -> None:
    """Reordering would pair a result to a different call (positional keying)."""
    provider, seen = _client_for(_always(_completion()))
    transcript = [
        ChatMessage(
            role="assistant",
            content="",
            tool_calls=[
                ToolCallRequest(id="call-a", name="assess_risk", arguments={"horizon": "NOW"}),
                ToolCallRequest(id="call-b", name="assess_risk", arguments={"horizon": "PLUS30"}),
            ],
        ),
        ChatMessage(role="tool", content="first", tool_call_id="call-a", name="assess_risk"),
        ChatMessage(role="tool", content="second", tool_call_id="call-b", name="assess_risk"),
    ]

    provider.generate(transcript, tools=None)

    sent = _body(seen[0])["messages"]
    assert [c["id"] for c in sent[0]["tool_calls"]] == ["call-a", "call-b"]
    assert [m["tool_call_id"] for m in sent[1:]] == ["call-a", "call-b"]
    assert [m["content"] for m in sent[1:]] == ["first", "second"]


# --------------------------------------------------------------------------- #
# What comes back: the structured calls, and nothing manufactured
# --------------------------------------------------------------------------- #


def test_a_structured_tool_call_survives_with_its_id_name_and_arguments() -> None:
    provider, _ = _client_for(_always(_completion(
        content=None, tool_calls=[TOOL_CALL_ON_WIRE], finish_reason="tool_calls",
    )))

    response = provider.generate(TRANSCRIPT, tools=tool_schema_dicts())

    assert len(response.tool_calls) == 1
    call = response.tool_calls[0]
    assert call.id == "chatcmpl-tool-local"
    assert call.name == "get_test_temperature"
    assert call.arguments == {"city": "Reykjavik"}, "arguments arrive decoded, as an object"
    assert response.content is None
    assert response.finish_reason == "tool_calls"


def test_parallel_structured_calls_come_back_in_order_with_distinct_ids() -> None:
    payload = _completion(
        content=None,
        tool_calls=[
            {**TOOL_CALL_ON_WIRE, "id": "call-1"},
            {
                "id": "call-2",
                "type": "function",
                "function": {"name": "get_test_temperature", "arguments": '{"city": "Akureyri"}'},
            },
        ],
        finish_reason="tool_calls",
    )
    provider, _ = _client_for(_always(payload))

    response = provider.generate(TRANSCRIPT, tools=tool_schema_dicts())

    assert [c.id for c in response.tool_calls] == ["call-1", "call-2"]
    assert [c.name for c in response.tool_calls] == ["get_test_temperature"] * 2
    assert [c.arguments["city"] for c in response.tool_calls] == ["Reykjavik", "Akureyri"]


def test_a_text_answer_comes_back_with_its_finish_reason_and_usage() -> None:
    payload = _completion(
        content="The hiker is at RED risk for NOW.",
        finish_reason="stop",
        usage={"prompt_tokens": 812, "completion_tokens": 96, "total_tokens": 908},
    )
    provider, _ = _client_for(_always(payload))

    response = provider.generate(TRANSCRIPT, tools=tool_schema_dicts())

    assert response.tool_calls == []
    assert response.finish_reason == "stop"
    assert response.usage["total_tokens"] == 908
    assert response.usage["prompt_tokens"] == 812
    assert response.usage["completion_tokens"] == 96


def test_usage_is_absent_rather_than_invented() -> None:
    provider, _ = _client_for(_always(_completion()))

    response = provider.generate(TRANSCRIPT, tools=None)

    assert response.usage == {}, "no counters were reported, so none may appear"


def test_prose_that_mimics_the_envelope_is_not_treated_as_a_tool_call() -> None:
    """THE Phase 4 failure mode, tested from the client side.

    A vLLM started without ``--tool-call-parser`` answers a tool request with the
    envelope still inside ``content`` and ``tool_calls`` null. That is a
    capability failure, and a provider that rescued the call out of the prose
    would manufacture the evidence that the capability works. This endpoint
    returns exactly that shape, and the provider must report zero tool calls and
    hand the prose back untouched.
    """
    envelope = (
        "I will call the tool.\n"
        + ENVELOPE_OPEN + "\n"
        + json.dumps({"name": "assess_risk", "arguments": {"horizon": "NOW"}})
        + "\n" + ENVELOPE_CLOSE
    )
    provider, _ = _client_for(_always(_completion(content=envelope, finish_reason="stop")))

    response = provider.generate(TRANSCRIPT, tools=tool_schema_dicts())

    assert response.tool_calls == [], "an unparsed envelope is not a structured call"
    assert response.content == envelope, "and the prose is not rewritten or stripped"
    assert response.finish_reason == "stop"


def test_the_same_shape_with_an_empty_tool_calls_list_is_also_not_a_call() -> None:
    provider, _ = _client_for(_always(_completion(content=None, tool_calls=[])))

    response = provider.generate(TRANSCRIPT, tools=tool_schema_dicts())

    assert response.tool_calls == []
    assert response.content is None


def test_malformed_arguments_are_preserved_not_replaced_by_an_empty_dict() -> None:
    """An empty dict would validate as 'called with no arguments';

    ``_raw`` makes the call fail validation, so the harness records an honest
    ``invalid_tool_call`` and hands the failure back to the model.
    """
    payload = _completion(
        content=None,
        tool_calls=[{
            "id": "call-bad",
            "type": "function",
            "function": {"name": "assess_risk", "arguments": '{"horizon": "NOW"'},
        }],
        finish_reason="tool_calls",
    )
    provider, _ = _client_for(_always(payload))

    response = provider.generate(TRANSCRIPT, tools=tool_schema_dicts())

    assert response.tool_calls[0].arguments == {"_raw": '{"horizon": "NOW"'}
    assert "horizon" not in response.tool_calls[0].arguments, (
        "a truncated payload must not be partially trusted"
    )


def test_arguments_that_decode_to_an_array_are_not_smuggled_through_as_arguments() -> None:
    payload = _completion(
        content=None,
        tool_calls=[{
            "id": "call-array",
            "type": "function",
            "function": {"name": "assess_risk", "arguments": '["NOW"]'},
        }],
        finish_reason="tool_calls",
    )
    provider, _ = _client_for(_always(payload))

    response = provider.generate(TRANSCRIPT, tools=tool_schema_dicts())

    assert list(response.tool_calls[0].arguments) == ["_raw"]


def test_a_missing_call_id_gets_a_disclosed_positional_stand_in() -> None:
    """The endpoint being sloppy must not corrupt the transcript's pairing."""
    payload = _completion(
        content=None,
        tool_calls=[
            {"type": "function", "function": {"name": "assess_risk", "arguments": "{}"}},
            {"type": "function", "function": {"name": "assess_risk", "arguments": "{}"}},
        ],
        finish_reason="tool_calls",
    )
    provider, _ = _client_for(_always(payload))

    response = provider.generate(TRANSCRIPT, tools=tool_schema_dicts())

    assert [c.id for c in response.tool_calls] == ["local_call_1", "local_call_2"]


def test_two_calls_sharing_an_id_raise_rather_than_mispairing() -> None:
    payload = _completion(
        content=None,
        tool_calls=[{**TOOL_CALL_ON_WIRE, "id": "duplicate"}] * 2,
        finish_reason="tool_calls",
    )
    provider, _ = _client_for(_always(payload))

    with pytest.raises(ProviderResponseError) as excinfo:
        provider.generate(TRANSCRIPT, tools=tool_schema_dicts())

    assert "duplicate" in str(excinfo.value)
    assert "paired back" in str(excinfo.value)


def test_a_completion_with_no_choices_raises_instead_of_answering() -> None:
    """An empty answer must not look like the model chose to say nothing (spec 10.5)."""
    provider, _ = _client_for(_always(_completion(drop_choices=True)))

    with pytest.raises(ProviderResponseError) as excinfo:
        provider.generate(TRANSCRIPT, tools=None)

    assert "no choices" in str(excinfo.value)


def test_a_choice_with_no_message_raises() -> None:
    provider, _ = _client_for(_always(_completion(drop_message=True)))

    with pytest.raises(ProviderResponseError) as excinfo:
        provider.generate(TRANSCRIPT, tools=None)

    assert "no message" in str(excinfo.value)


def test_a_tool_call_with_no_function_name_is_reported_as_the_empty_string() -> None:
    """Not invented, not dropped: the harness refuses an unknown tool by name."""
    payload = _completion(
        content=None,
        tool_calls=[{"id": "call-noname", "type": "function", "function": {"arguments": "{}"}}],
        finish_reason="tool_calls",
    )
    provider, _ = _client_for(_always(payload))

    response = provider.generate(TRANSCRIPT, tools=tool_schema_dicts())

    assert response.tool_calls[0].name == ""
    assert response.tool_calls[0].id == "call-noname"


# --------------------------------------------------------------------------- #
# Failure taxonomy: every kind classified, none of them answered (spec 12, 17)
# --------------------------------------------------------------------------- #


def test_a_timeout_is_a_timeout() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("read timed out", request=request)

    provider, _ = _client_for(handler)

    with pytest.raises(ProviderTimeout):
        provider.generate(TRANSCRIPT, tools=None)


def test_a_stopped_local_server_is_unavailable_and_names_no_fallback() -> None:
    """The Spark box with vLLM down: no answer, and nothing rerouted.

    A rerouted run would be a benchmark row for a run that did not happen
    (spec 17), so the message must say a fallback was *not* attempted.
    """

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    provider, _ = _client_for(handler)

    with pytest.raises(ProviderUnavailable) as excinfo:
        provider.generate(TRANSCRIPT, tools=None)

    message = str(excinfo.value)
    assert "no fallback" in message
    assert "unreachable" in message


def test_a_429_is_rate_limited_and_consumes_exactly_one_retry() -> None:
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(429, json={"error": {"message": "slow down"}})

    provider, _ = _client_for(handler)

    with pytest.raises(ProviderRateLimited):
        provider.generate(TRANSCRIPT, tools=None)

    assert calls["n"] == 2, "one readable retry budget, not an unbounded loop (spec 12)"


def test_a_500_is_a_server_error_and_consumes_exactly_one_retry() -> None:
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(500, json={"error": {"message": "boom"}})

    provider, _ = _client_for(handler)

    with pytest.raises(ProviderServerError):
        provider.generate(TRANSCRIPT, tools=None)

    assert calls["n"] == 2


def test_a_400_is_not_retried_because_a_refusal_is_deterministic() -> None:
    """This is where a misconfigured --tool-call-parser surfaces (vLLM answers 400)."""
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(400, json={"error": {"message": "tool_choice unsupported"}})

    provider, _ = _client_for(handler)

    with pytest.raises(ProviderResponseError) as excinfo:
        provider.generate(TRANSCRIPT, tools=tool_schema_dicts())

    assert calls["n"] == 1, "retrying a refusal wastes the budget"
    assert "400" in str(excinfo.value)


def test_a_401_and_a_403_are_authentication_errors_and_not_retried() -> None:
    for status in (401, 403):
        provider, seen = _client_for(_status(status))

        with pytest.raises(ProviderAuthenticationError):
            provider.generate(TRANSCRIPT, tools=None)

        assert len(seen) == 1


def test_a_retry_budget_of_one_does_not_retry_at_all() -> None:
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(503, json={"error": {"message": "unavailable"}})

    provider, _ = _client_for(handler, max_attempts=1)

    with pytest.raises(ProviderError):
        provider.generate(TRANSCRIPT, tools=None)

    assert calls["n"] == 1


def test_an_unclassifiable_failure_still_leaves_the_caller_without_an_answer() -> None:
    """Something nobody predicted must not escape as a raw exception.

    A ``RuntimeError`` raised by a transport must be classified, because the
    benchmark records ``stop_reason`` from the ``ProviderError`` subclasses --
    an unclassified escape is a crash, not an honest record of a failed run.
    """

    class _ExplodingClient:
        """A client that fails in a way no SDK exception class describes."""

        class chat:  # noqa: N801 - mirrors the SDK's nested attribute path
            class completions:
                @staticmethod
                def create(**kwargs: Any) -> Any:
                    raise RuntimeError("something nobody predicted")

    provider = LocalModelProvider(model="m", base_url="http://127.0.0.1:1/v1",
                                  client=_ExplodingClient())

    with pytest.raises(ProviderError) as excinfo:
        provider.generate(TRANSCRIPT, tools=None)

    assert type(excinfo.value) is ProviderUnavailable
    assert "RuntimeError" in str(excinfo.value)
    assert "no fallback" in str(excinfo.value) or "failed" in str(excinfo.value)


def test_no_failure_message_carries_the_credential_or_the_endpoint_secret() -> None:
    """A trace or a log must never be where a credential surfaces (spec 16)."""
    secretish = "not-a-real-credential"
    provider = LocalModelProvider(
        model="m", base_url="http://127.0.0.1:1/v1", api_key=secretish
    )

    for kwargs in ({"timeout_s": 0.0}, {"max_attempts": 9}, {"timeout_s": -1.0}):
        with pytest.raises(ProviderUnavailable) as excinfo:
            LocalModelProvider(model="m", base_url="http://127.0.0.1:1/v1", **kwargs)
        assert secretish not in str(excinfo.value), (
            "the placeholder never belongs in a message, whatever the endpoint"
        )

    assert secretish not in repr(provider)
    assert "key" not in repr(provider).lower()


def test_an_unreachable_endpoint_is_not_something_to_retry_past_the_budget() -> None:
    """The message must name the refusal, and the flag must agree with it."""
    provider, _ = _client_for(_status(400))

    with pytest.raises(ProviderResponseError) as excinfo:
        provider.generate(TRANSCRIPT, tools=tool_schema_dicts())

    assert excinfo.value.retryable is False, (
        "a deterministic refusal must not be retried, and must not claim to be retryable"
    )


@pytest.mark.parametrize("status", [400, 401, 403, 429, 500, 503])
def test_no_failure_ever_produces_a_synthesised_turn(status: int) -> None:
    """The one substitution that would fake a local run is a fallback.

    Every HTTP failure kind must raise, never return an empty or optimistic
    :class:`ModelResponse`: a provider that "failed gracefully" would hand the
    harness a turn no model ever produced, and the run would be recorded as a
    real one.
    """
    provider, _ = _client_for(_status(status))

    with pytest.raises(ProviderError) as excinfo:
        provider.generate(TRANSCRIPT, tools=tool_schema_dicts())

    assert not hasattr(excinfo.value, "content")
    assert excinfo.value.retryable in (True, False), "retryable is data, not prose"


def test_the_retryable_flag_travels_with_the_error_as_data() -> None:
    """Spec 12: a caller classifies from the flag, not from the message text."""
    retryable = _catch(_client_for(_status(503))[0])
    fatal = _catch(_client_for(_status(400))[0])

    assert isinstance(retryable, ProviderServerError)
    assert retryable.retryable is True
    assert isinstance(fatal, ProviderResponseError)
    assert not isinstance(fatal, ProviderServerError)
    assert fatal.retryable is False


# --------------------------------------------------------------------------- #
# Through the frozen harness: one run_agent(), whichever backend is loaded
# --------------------------------------------------------------------------- #


class _ScriptedEndpoint:
    """Serve a fixed sequence of completions, recording every request body.

    This is the offline stand-in for the real endpoint the Phase 4 probe spoke
    to: the same request/response shape, so the loop under test is the harness's
    real loop over the real provider, not a mock of either.
    """

    def __init__(self, *payloads: dict[str, Any]) -> None:
        self.payloads = list(payloads)
        self.bodies: list[dict[str, Any]] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.bodies.append(json.loads(request.content.decode("utf-8")))
        index = min(len(self.bodies) - 1, len(self.payloads) - 1)
        return httpx.Response(200, json=self.payloads[index])


def _assess_turn(call_id: str = "chatcmpl-tool-risk") -> dict[str, Any]:
    """A structured ``assess_risk`` call as the local endpoint sends it."""
    return _completion(
        content=None,
        tool_calls=[{
            "id": call_id,
            "type": "function",
            "function": {
                "name": "assess_risk",
                "arguments": json.dumps(_assess_arguments()),
            },
        }],
        finish_reason="tool_calls",
    )


def _answer_turn(content: str = "RED for NOW; the hiker should turn back.") -> dict[str, Any]:
    return _completion(content=content, finish_reason="stop")


def test_a_full_agent_loop_runs_over_the_real_local_transport() -> None:
    """The Phase 3 capability gates, replayed offline against the real provider.

    Model asks for the tool -> the harness runs the real TrailGuard tool -> the
    result goes back keyed by the call id -> the model answers. If any link in
    that chain is broken, this test fails rather than the local run silently
    producing prose.
    """
    endpoint = _ScriptedEndpoint(_assess_turn(), _answer_turn())
    provider, _ = _client_for(endpoint)

    result = run_agent(
        task=TASK,
        context=make_context(),
        model_provider=provider,
        skill_enabled=False,
    )

    assert result.stop_reason == "completed", result.trace.model_dump_json()
    assert result.final_response == "RED for NOW; the hiker should turn back."
    assert result.steps == 2
    assert endpoint.bodies, "no request reached the transport"

    first, second = endpoint.bodies
    assert [t["function"]["name"] for t in first["tools"]] == [
        spec["name"] for spec in tool_schema_dicts()
    ]
    assert first["tool_choice"] == "auto"

    # The second request is a continuation, not a fresh transcript: it carries the
    # assistant's call and the tool result keyed back to that exact id.
    roles = [m["role"] for m in second["messages"]]
    assert roles == ["system", "user", "assistant", "tool"]
    assert second["messages"][2]["tool_calls"][0]["id"] == "chatcmpl-tool-risk"
    assert second["messages"][3]["tool_call_id"] == "chatcmpl-tool-risk"
    assert second["messages"][3]["name"] == "assess_risk"

    # And the tool result that went back is what the deterministic core returned.
    assert "risk_level" in second["messages"][3]["content"]

    trace = result.trace
    assert trace.provider == "local"
    assert trace.model_name == STUB_MODEL
    assert len(trace.tool_calls) == 1
    assert trace.tool_calls[0].tool is ToolName.ASSESS_RISK
    assert trace.core_risk_level is not None, "the core ran, so its verdict is recorded"
    assert trace.sampling_params == {"temperature": 0.0, "max_tokens": 2048, "max_steps": 12}


def test_the_local_backend_produces_the_run_a_scripted_double_produces() -> None:
    """``run_agent`` is provably backend-neutral: the same run, twice over.

    One run goes through the real local transport, the other through the scripted
    double the Phase 3 tests use. They must agree on everything except the
    provider label, because the harness owns the loop, the tool order and the
    risk recording -- the transport only moves messages.
    """
    over_transport = run_agent(
        task=TASK,
        context=make_context(),
        model_provider=_client_for(_ScriptedEndpoint(_assess_turn(), _answer_turn()))[0],
        skill_enabled=False,
    )
    scripted = run_agent(
        task=TASK,
        context=make_context(),
        model_provider=StubProvider([
            ModelResponse(
                content=None,
                tool_calls=[ToolCallRequest(
                    id="chatcmpl-tool-risk",
                    name="assess_risk",
                    arguments=_assess_arguments(),
                )],
                finish_reason="tool_calls",
            ),
            ModelResponse(
                content="RED for NOW; the hiker should turn back.", finish_reason="stop"
            ),
        ]),
        skill_enabled=False,
    )

    assert over_transport.final_response == scripted.final_response
    assert over_transport.stop_reason == scripted.stop_reason
    assert over_transport.steps == scripted.steps
    assert over_transport.trace.provider == "local"
    assert scripted.trace.provider == "stub"
    # Everything the deterministic core produced is identical, verbatim.
    assert over_transport.trace.core_risk_level == scripted.trace.core_risk_level
    assert over_transport.trace.core_confidence == scripted.trace.core_confidence
    assert over_transport.trace.horizons_checked == scripted.trace.horizons_checked
    assert over_transport.trace.triggered_rules == scripted.trace.triggered_rules
    assert [c.tool for c in over_transport.trace.tool_calls] == [
        c.tool for c in scripted.trace.tool_calls
    ]


def test_a_provider_error_stops_the_run_without_an_answer() -> None:
    """A stopped server must not become a plausible-looking answer (spec 17)."""
    provider, _ = _client_for(_status(500))

    result = run_agent(
        task=TASK,
        context=make_context(),
        model_provider=provider,
        skill_enabled=True,
    )

    assert result.stop_reason == "provider_error"
    assert result.final_response == "", "the harness never writes an answer for the model"
    assert result.trace.provider == "local"
    assert result.trace.skill_loaded is True, "the trace still records the condition"


def test_the_skill_text_reaches_the_local_endpoint_unchanged() -> None:
    """The treatment is the same bytes whichever backend is loaded (spec 3.2)."""
    endpoint = _ScriptedEndpoint(_answer_turn())
    provider, _ = _client_for(endpoint)
    expected_system = f"{HARNESS_BASE_SYSTEM_PROMPT}\n\n{load_skill_md()}"

    run_agent(task=TASK, context=make_context(), model_provider=provider, skill_enabled=True)

    assert endpoint.bodies[0]["messages"][0]["role"] == "system"
    assert endpoint.bodies[0]["messages"][0]["content"] == expected_system


def test_the_skill_hash_the_trace_records_is_the_file_on_disk() -> None:
    """A skill tuned for the local model would change this byte string (spec 3.2)."""
    result = run_agent(
        task=TASK,
        context=make_context(),
        model_provider=_client_for(_ScriptedEndpoint(_answer_turn()))[0],
        skill_enabled=True,
    )

    assert result.trace.skill_sha256 == FROZEN_SHA256["SKILL.md"]
    assert result.trace.skill_version, "the version its own frontmatter declares"


def test_the_baseline_prompt_carries_nothing_skill_related() -> None:
    endpoint = _ScriptedEndpoint(_answer_turn())
    provider, _ = _client_for(endpoint)

    result = run_agent(
        task=TASK, context=make_context(), model_provider=provider, skill_enabled=False
    )

    assert endpoint.bodies[0]["messages"][0]["content"] == HARNESS_BASE_SYSTEM_PROMPT
    assert result.trace.skill_loaded is False
    assert result.trace.skill_sha256 is None


def test_the_run_advertises_the_registrys_own_five_tools() -> None:
    """No tool is added for the local model, and none is dropped (spec 3.1, 4.1)."""
    endpoint = _ScriptedEndpoint(_answer_turn())
    provider, _ = _client_for(endpoint)

    run_agent(task=TASK, context=make_context(), model_provider=provider, skill_enabled=False)

    advertised = [t["function"]["name"] for t in endpoint.bodies[0]["tools"]]
    assert advertised == [tool.value for tool in ToolName]
    assert set(advertised) == {name for name in TOOL_FUNCTIONS}


# --------------------------------------------------------------------------- #
# Structural guards: the client never rescues, the harness never special-cases
# --------------------------------------------------------------------------- #


def _code_only_source(module: Any) -> str:
    """Return a module's source with every docstring and comment removed.

    Docstrings name what a module deliberately *avoids* -- ``local.py`` explains
    that it does not use ``ast.literal_eval`` -- so scanning the raw text would
    fail a module for documenting its own restraint. Stripping docstrings first
    means the guard judges code, not prose.
    """

    class _StripDocstrings(ast.NodeTransformer):
        def _strip(self, node: Any) -> Any:
            body = getattr(node, "body", None)
            if (body and isinstance(body[0], ast.Expr)
                    and isinstance(body[0].value, ast.Constant)
                    and isinstance(body[0].value.value, str)):
                node.body = body[1:] or [ast.Pass()]
            return node

        def visit_Module(self, node: ast.Module) -> ast.Module:
            self.generic_visit(node)
            return self._strip(node)

        def visit_FunctionDef(self, node: ast.FunctionDef) -> ast.FunctionDef:
            self.generic_visit(node)
            return self._strip(node)

        def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> ast.AsyncFunctionDef:
            self.generic_visit(node)
            return self._strip(node)

        def visit_ClassDef(self, node: ast.ClassDef) -> ast.ClassDef:
            self.generic_visit(node)
            return self._strip(node)

    tree = ast.parse(inspect.getsource(module))
    return ast.unparse(_StripDocstrings().visit(tree))


def test_the_provider_module_contains_no_tool_call_parser() -> None:
    """No regex, no XML reader, no split on the envelope, no interpreter.

    This is the client-side half of spec section 9: if the server hands back the
    envelope inside ``content``, the honest outcome is an empty ``tool_calls``
    list, and a provider that parsed the prose would be manufacturing the
    evidence that the capability works.
    """
    from trailguard.providers import local as local_module

    source = _code_only_source(local_module)

    for banned in (
        "import re",
        "re.compile",
        "re.search",
        "re.match",
        "re.findall",
        "re.split",
        "import xml",
        "etree",
        "minidom",
        "BeautifulSoup",
        "literal_eval",
        "eval(",
        "exec(",
    ):
        assert banned not in source, (
            f"{banned!r} in trailguard/providers/local.py would parse or execute model text"
        )
    for rescue in (".split(", ".find(", ".partition(", ".startswith("):
        assert rescue not in source, f"{rescue!r} could strip an envelope out of content"


def _string_constants(tree: ast.AST) -> set[str]:
    """Every string the code can produce, with ``+`` concatenation folded.

    ``ast.unparse`` renders ``"<" + "tool" + "_call>"`` verbatim rather than
    folding it, so a source scan alone misses exactly the fragment-assembled
    form a rescue would be written in -- and that is the form this whole guard
    exists to catch. Folding the constant ``+`` chains turns both spellings into
    one value.
    """

    def fold(node: ast.AST) -> str | None:
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            return node.value
        if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
            left, right = fold(node.left), fold(node.right)
            if left is not None and right is not None:
                return left + right
        if isinstance(node, ast.JoinedStr):
            parts = []
            for value in node.values:
                folded = fold(value)
                if folded is None:
                    return None  # an interpolated value: cannot be resolved statically
                parts.append(folded)
            return "".join(parts)
        return None

    found: set[str] = set()
    for node in ast.walk(tree):
        folded = fold(node)
        if folded is not None:
            found.add(folded)
    return found


def test_the_provider_never_names_the_envelope_at_all() -> None:
    """The strongest form of the guard: the envelope exists nowhere in the code.

    A client that cannot even name the envelope cannot parse one out of prose, so
    no future refactor can quietly reintroduce that rescue without failing here.
    Constant concatenation is folded first, because ``"<" + "tool" + "_call>"``
    is the same string as ``"<tool_call>"`` and a rescue written that way must
    not pass by being spelled in pieces.
    """
    from trailguard.providers import local as local_module

    tree = ast.parse(_code_only_source(local_module))
    constants = _string_constants(tree)

    for fragment in (ENVELOPE_OPEN, ENVELOPE_CLOSE, ENVELOPE_RESPONSE_OPEN,
                     ENVELOPE_RESPONSE_CLOSE):
        assert fragment not in constants, (
            f"the client can build {fragment!r}; parsing it back out of content is forbidden"
        )
        assert fragment not in _code_only_source(local_module)


def test_no_model_supplied_text_reaches_an_interpreter() -> None:
    """``function.arguments`` is decoded with json.loads and nothing else."""
    from trailguard.providers.local import _decode_arguments

    assert _decode_arguments('{"city": "Reykjavik"}') == {"city": "Reykjavik"}
    assert _decode_arguments('__import__("os")') == {"_raw": '__import__("os")'}
    assert _decode_arguments("{'city': 'Reykjavik'}") == {
        "_raw": "{'city': 'Reykjavik'}"
    }, "single quotes are not JSON and must not be evaluated as Python"
    assert _decode_arguments(None) == {}


def test_the_harness_has_no_branch_on_a_backend_identity() -> None:
    """Qwen adapts to the frozen harness, never the harness to Qwen (spec 15).

    ``local`` is in the list with the remote names for the same reason: the
    provider label is *data* the harness records in the trace, and a loop that
    asked which backend is loaded would already be a second, unequal path. The
    harness has no legitimate reason to mention any of these seven names in a
    condition, and the assertion below is what keeps that true.
    """
    from trailguard.agent import harness as harness_module

    tree = ast.parse(inspect.getsource(harness_module))
    banned = ("qwen", "hermes", "stepfun", "dgx", "spark", "nvidia", "local")

    for node in ast.walk(tree):
        if isinstance(node, (ast.If, ast.IfExp)):
            dumped = ast.dump(node.test).lower()
            for name in banned:
                assert name not in dumped, (
                    f"the harness branches on {name!r}; a backend-specific path would "
                    "break the single-variable comparison of spec 3.1"
                )
        if isinstance(node, ast.Compare):
            dumped = ast.dump(node).lower()
            for name in banned:
                assert name not in dumped, f"the harness tests for {name!r}"

    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id in {
                    "local_model", "qwen_model", "stepfun_model"
                }:
                    raise AssertionError(f"backend-specific binding {target.id!r}")


def test_there_is_no_run_qwen_agent_entry_point() -> None:
    """One harness entry point, for every backend (spec 15, 3.1)."""
    from trailguard.agent import harness as harness_module

    public = {name for name in dir(harness_module) if not name.startswith("_")}
    for banned in ("run_qwen_agent", "run_local_agent", "run_stepfun_agent"):
        assert banned not in public, f"{banned}() would be a second, unequal entry point"


def test_the_frozen_assets_are_byte_unchanged() -> None:
    """The skill and the rule pack must not be tuned for the local model (spec 3.2)."""
    for relative, expected in FROZEN_SHA256.items():
        actual = hashlib.sha256((REPO_ROOT / relative).read_bytes()).hexdigest()
        assert actual == expected, (
            f"{relative} changed during Phase 4. If this is a deliberate change, record it "
            "as a Phase 5 proposal -- a skill edited for one backend is a different experiment."
        )


def test_the_tool_registry_is_still_the_frozen_five() -> None:
    """The five tool responsibilities are not widened for the local model."""
    assert list(TOOL_FUNCTIONS) == list(ToolName)
    assert len(tool_schema_dicts()) == 5
    assert "forecast_risk" not in {tool.value for tool in ToolName}, (
        "the banned one-shot tool must not reappear (spec 4.1)"
    )
