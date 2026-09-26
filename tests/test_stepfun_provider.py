"""StepFun transport tests: wire translation, status mapping and the retry budget.

Responsibility (spec 16, 12, 13): prove that
:class:`~trailguard.providers.stepfun.StepFunProvider` sends exactly what the
harness handed it, parses exactly what the endpoint sent back, and fails in a way
a benchmark can classify -- with **no network involved**.

Every test here drives a real ``openai.OpenAI`` client over
``httpx.MockTransport``. That is deliberate: the object under test is the real
SDK's request construction, its header handling, its response validation and its
exception hierarchy. Stubbing the client instead would test a fake and leave the
actual translation -- the part that breaks when an endpoint changes -- untested.
Conftest's autouse socket guard makes a real connection impossible, so the
guarantee is structural rather than promised (spec 13).

What is *not* tested here: anything about the agent loop, the tools or the safety
core. This module is transport only, and it asserts nothing about risk.
"""

from __future__ import annotations

import json
from typing import Any

import httpx
import openai
import pytest

from trailguard.providers.base import (
    ChatMessage,
    ProviderAuthenticationError,
    ProviderError,
    ProviderRateLimited,
    ProviderResponseError,
    ProviderServerError,
    ProviderTimeout,
    ProviderUnavailable,
    ToolCallRequest,
)
from trailguard.providers.stepfun import StepFunProvider

#: Obviously-fake credential, so a leaked one is a test failure and not a real
#: secret. The repository-wide secret scan reads every committable file, so it is
#: held here as an obviously-not-a-key string.
FAKE_CREDENTIAL = "not-a-real-credential"

#: Endpoint stand-in. Nothing resolves it; the transport never reaches a socket.
STUB_BASE_URL = "https://stepfun.invalid/v1"
STUB_MODEL = "step-5-preview"

TRANSCRIPT: list[ChatMessage] = [
    ChatMessage(role="system", content="You are a general-purpose assistant."),
    ChatMessage(role="user", content="Assess this hiker."),
]

TOOL_SPEC: dict[str, Any] = {
    "name": "assess_risk",
    "description": "Run the deterministic safety core once for one horizon.",
    "input_schema": {
        "type": "object",
        "properties": {"horizon": {"type": "string", "enum": ["NOW", "PLUS30", "PLUS60", "PLUS120"]}},
        "required": ["horizon"],
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
) -> dict[str, Any]:
    """A minimal OpenAI-compatible chat-completion payload."""
    message: dict[str, Any] = {"role": "assistant", "content": content}
    if tool_calls is not None:
        message["tool_calls"] = tool_calls
    payload: dict[str, Any] = {
        "id": "cmpl-stub",
        "object": "chat.completion",
        "created": 0,
        "model": STUB_MODEL,
    }
    if not drop_choices:
        payload["choices"] = [{"index": 0, "message": message, "finish_reason": finish_reason}]
    else:
        payload["choices"] = []
    if usage is not None:
        payload["usage"] = usage
    return payload


def _tool_call_payload(call_id: str, name: str, arguments: dict[str, Any] | str) -> dict[str, Any]:
    """One OpenAI-shaped tool call; ``arguments`` as the endpoint sends it (a JSON string)."""
    encoded = arguments if isinstance(arguments, str) else json.dumps(arguments)
    return {"id": call_id, "type": "function", "function": {"name": name, "arguments": encoded}}


def _provider(handler: Any, **kwargs: Any) -> tuple[StepFunProvider, list[httpx.Request]]:
    """Return a provider whose client sends through ``handler``, and what it sent.

    The client is injected, which is the documented dependency-injection point
    (spec 13): no credential is needed to build the provider, no socket is opened,
    and the handler can count requests -- which is how the retry budget is
    verified rather than trusted.
    """
    seen: list[httpx.Request] = []

    def _recording(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return handler(request)

    client = openai.OpenAI(
        api_key=FAKE_CREDENTIAL,
        base_url=STUB_BASE_URL,
        max_retries=0,
        http_client=httpx.Client(transport=httpx.MockTransport(_recording)),
    )
    provider = StepFunProvider(model=STUB_MODEL, base_url=STUB_BASE_URL, client=client, **kwargs)
    return provider, seen


def _always(payload: dict[str, Any]) -> Any:
    return lambda request: httpx.Response(200, json=payload)


def _status(status_code: int, body: str = "boom") -> Any:
    return lambda request: httpx.Response(status_code, json={"error": {"message": body}})


def _body(request: httpx.Request) -> dict[str, Any]:
    """The JSON body of one recorded request."""
    return json.loads(request.content.decode("utf-8"))


# --------------------------------------------------------------------------- #
# What goes out on the wire (spec 16)
# --------------------------------------------------------------------------- #


def test_a_text_turn_comes_back_with_its_finish_reason_and_usage() -> None:
    provider, seen = _provider(
        _always(_completion(content="Risk timeline reported.", finish_reason="stop", usage={"prompt_tokens": 11, "completion_tokens": 7, "total_tokens": 18}))
    )

    response = provider.generate(TRANSCRIPT, tools=None, temperature=0.0, max_tokens=2048)

    assert response.content == "Risk timeline reported."
    assert response.tool_calls == []
    assert response.finish_reason == "stop"
    assert response.usage["total_tokens"] == 18
    assert len(seen) == 1


def test_the_request_carries_the_model_the_temperature_and_the_token_cap() -> None:
    provider, seen = _provider(_always(_completion()))

    provider.generate(TRANSCRIPT, tools=None, temperature=0.25, max_tokens=512)

    body = _body(seen[0])
    assert body["model"] == STUB_MODEL
    assert body["temperature"] == 0.25
    assert body["max_tokens"] == 512
    assert "tools" not in body, "no tool spec was offered, so none may be advertised"


def test_sampling_parameters_are_forwarded_verbatim() -> None:
    """Spec 3.1: the harness's sampling parameters reach the endpoint unchanged.

    A provider that rounded, clamped or defaulted them would give the two skill
    conditions different decoding parameters without anyone noticing -- the
    single-variable experiment would be broken inside the transport.
    """
    provider, seen = _provider(_always(_completion()))

    provider.generate(TRANSCRIPT, tools=None, temperature=0.0, max_tokens=2048)

    body = _body(seen[0])
    assert body["temperature"] == 0.0
    assert body["max_tokens"] == 2048


def test_the_transcript_reaches_the_endpoint_in_openai_shape() -> None:
    """The assistant and tool halves must both survive translation.

    The endpoint rejects a tool result that is not keyed to a call, so the
    assistant turn has to carry its calls and the tool message its
    ``tool_call_id``. Provider order is preserved because the pairing is
    positional.
    """
    transcript = TRANSCRIPT + [
        ChatMessage(role="assistant", content="", tool_calls=[_call("call-1", "assess_risk", {"horizon": "NOW"})]),
        ChatMessage(role="tool", content="assess_risk -> {...}", tool_call_id="call-1", name="assess_risk"),
        ChatMessage(role="assistant", content="Now the final answer.", tool_calls=[]),
    ]
    provider, seen = _provider(_always(_completion()))

    provider.generate(transcript, tools=None, temperature=0.0, max_tokens=64)

    messages = _body(seen[0])["messages"]
    assert [message["role"] for message in messages] == ["system", "user", "assistant", "tool", "assistant"]
    assistant = messages[2]
    assert assistant["tool_calls"][0]["id"] == "call-1"
    assert assistant["tool_calls"][0]["type"] == "function"
    assert json.loads(assistant["tool_calls"][0]["function"]["arguments"]) == {"horizon": "NOW"}
    tool_message = messages[3]
    assert tool_message["tool_call_id"] == "call-1"
    assert tool_message["name"] == "assess_risk"


def test_tool_specs_are_wrapped_in_the_endpoints_function_shape() -> None:
    """``input_schema`` (TrailGuard) and ``parameters`` (OpenAI) name the same thing."""
    provider, seen = _provider(_always(_completion()))

    provider.generate(TRANSCRIPT, tools=[TOOL_SPEC], temperature=0.0, max_tokens=64)

    tools = _body(seen[0])["tools"]
    assert tools[0]["type"] == "function"
    assert tools[0]["function"]["name"] == "assess_risk"
    assert tools[0]["function"]["parameters"] == TOOL_SPEC["input_schema"]
    assert "input_schema" not in tools[0]["function"], "the registry's key name is not the endpoint's"


def test_a_tool_spec_without_an_argument_schema_is_refused_loudly() -> None:
    """Advertising a tool with no contract is worse than not advertising it."""
    provider, _ = _provider(_always(_completion()))

    with pytest.raises(ProviderResponseError) as excinfo:
        provider.generate(TRANSCRIPT, tools=[{"name": "assess_risk"}], temperature=0.0, max_tokens=64)

    assert "input_schema" in str(excinfo.value)


def test_the_credential_travels_as_a_header_and_never_in_the_body() -> None:
    """A credential pasted into a payload would land in logs and in a replay."""
    provider, seen = _provider(_always(_completion()))

    provider.generate(TRANSCRIPT, tools=None, temperature=0.0, max_tokens=64)

    assert seen[0].headers["authorization"] == f"Bearer {FAKE_CREDENTIAL}"
    assert FAKE_CREDENTIAL not in seen[0].content.decode("utf-8")


# --------------------------------------------------------------------------- #
# What comes back (spec 16)
# --------------------------------------------------------------------------- #


def test_tool_calls_come_back_in_provider_order() -> None:
    """Order and ids are what the harness pairs tool results with (spec 15)."""
    payload = _completion(
        content=None,
        finish_reason="tool_calls",
        tool_calls=[
            _tool_call_payload("call-2", "assess_risk", {"horizon": "PLUS120"}),
            _tool_call_payload("call-1", "get_route_context", {"route_id": "scn_route_sample"}),
        ],
    )
    provider, _ = _provider(_always(payload))

    response = provider.generate(TRANSCRIPT, tools=[TOOL_SPEC], temperature=0.0, max_tokens=64)

    assert [call.id for call in response.tool_calls] == ["call-2", "call-1"], "provider order, not sorted"
    assert [call.name for call in response.tool_calls] == ["assess_risk", "get_route_context"]
    assert response.tool_calls[0].arguments == {"horizon": "PLUS120"}


def _call(call_id: str, name: str, arguments: dict[str, Any]) -> ToolCallRequest:
    return ToolCallRequest(id=call_id, name=name, arguments=arguments)


def test_a_call_with_an_unparseable_argument_string_is_preserved_as_raw() -> None:
    """A malformed call must fail validation, not sail through as "no arguments"."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_completion(content=None, finish_reason="tool_calls", tool_calls=[{"id": "call-1", "type": "function", "function": {"name": "assess_risk", "arguments": '{"horizon": '}}]))

    provider, _ = _provider(handler)

    response = provider.generate(TRANSCRIPT, tools=[TOOL_SPEC], temperature=0.0, max_tokens=64)

    assert response.tool_calls[0].arguments == {"_raw": '{"horizon": '}
    assert response.tool_calls[0].name == "assess_risk", "the name is still readable"


def test_a_call_without_an_id_gets_a_positional_stand_in() -> None:
    """The transcript needs a key for the tool result, so one is invented and disclosed."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json=_completion(
                content=None,
                finish_reason="tool_calls",
                tool_calls=[{"id": "", "type": "function", "function": {"name": "assess_risk", "arguments": "{}"}}],
            ),
        )

    provider, _ = _provider(handler)

    response = provider.generate(TRANSCRIPT, tools=[TOOL_SPEC], temperature=0.0, max_tokens=64)

    assert response.tool_calls[0].id == "stepfun_call_1"


def test_two_calls_sharing_an_id_are_refused() -> None:
    """Two results keyed to one call cannot be paired back; the answer is unusable."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json=_completion(
                content=None,
                finish_reason="tool_calls",
                tool_calls=[
                    _tool_call_payload("call-1", "assess_risk", {"horizon": "NOW"}),
                    _tool_call_payload("call-1", "assess_risk", {"horizon": "PLUS30"}),
                ],
            ),
        )

    provider, _ = _provider(handler)

    with pytest.raises(ProviderResponseError) as excinfo:
        provider.generate(TRANSCRIPT, tools=[TOOL_SPEC], temperature=0.0, max_tokens=64)

    assert "call-1" in str(excinfo.value)


def test_a_completion_with_no_choices_is_refused() -> None:
    """An empty choice list is not "the model chose to say nothing"."""
    provider, _ = _provider(_always(_completion(drop_choices=True)))

    with pytest.raises(ProviderResponseError) as excinfo:
        provider.generate(TRANSCRIPT, tools=None, temperature=0.0, max_tokens=64)

    assert "no choices" in str(excinfo.value)


def test_a_missing_usage_block_is_an_empty_mapping_not_a_guess() -> None:
    provider, _ = _provider(_always(_completion()))

    response = provider.generate(TRANSCRIPT, tools=None, temperature=0.0, max_tokens=64)

    assert response.usage == {}


# --------------------------------------------------------------------------- #
# Failure taxonomy and the retry budget (spec 12)
# --------------------------------------------------------------------------- #


def test_a_429_is_retried_exactly_once_and_then_reported() -> None:
    """One retry is the whole budget: the endpoint that refuses twice loses."""
    calls = {"count": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["count"] += 1
        return _status(429, "slow down")(request)

    provider, _ = _provider(handler)

    with pytest.raises(ProviderRateLimited):
        provider.generate(TRANSCRIPT, tools=None, temperature=0.0, max_tokens=64)

    assert calls["count"] == 2, "exactly one retry, not one per attempt"


def test_a_500_is_retried_exactly_once_and_then_reported() -> None:
    """A 5xx is the one unusable answer a retry can fix -- once."""
    calls = {"count": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["count"] += 1
        return _status(503, "upstream exploded")(request)

    provider, _ = _provider(handler)

    with pytest.raises(ProviderServerError):
        provider.generate(TRANSCRIPT, tools=None, temperature=0.0, max_tokens=64)

    assert calls["count"] == 2


def test_a_retry_that_succeeds_returns_the_answer() -> None:
    """The retry is not decoration: a flapping endpoint can still produce a run."""
    calls = {"count": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["count"] += 1
        if calls["count"] == 1:
            return _status(429, "slow down")(request)
        return httpx.Response(200, json=_completion(content="Recovered."))

    provider, _ = _provider(handler)

    response = provider.generate(TRANSCRIPT, tools=None, temperature=0.0, max_tokens=64)

    assert response.content == "Recovered."
    assert calls["count"] == 2


def test_a_400_is_not_retried() -> None:
    """A malformed request is deterministic; retrying only burns the budget."""
    calls = {"count": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["count"] += 1
        return _status(400, "bad request")(request)

    provider, _ = _provider(handler)

    with pytest.raises(ProviderResponseError) as excinfo:
        provider.generate(TRANSCRIPT, tools=None, temperature=0.0, max_tokens=64)

    assert calls["count"] == 1
    assert "400" in str(excinfo.value)


def test_a_401_is_not_retried_and_names_no_credential() -> None:
    """A rejected credential is a refusal, not a hiccup -- and the key stays out."""
    calls = {"count": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["count"] += 1
        return _status(401, "invalid api key")(request)

    provider, _ = _provider(handler)

    with pytest.raises(ProviderAuthenticationError) as excinfo:
        provider.generate(TRANSCRIPT, tools=None, temperature=0.0, max_tokens=64)

    assert calls["count"] == 1
    assert FAKE_CREDENTIAL not in str(excinfo.value), "the rejected key is never echoed"


def test_a_timeout_is_mapped_and_retried_once() -> None:
    """A timeout is a missing answer; a second identical attempt is the whole budget."""
    calls = {"count": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["count"] += 1
        raise httpx.ReadTimeout("read timed out", request=request)

    provider, _ = _provider(handler)

    with pytest.raises(ProviderTimeout):
        provider.generate(TRANSCRIPT, tools=None, temperature=0.0, max_tokens=64)

    assert calls["count"] == 2


def test_a_connection_failure_is_mapped_and_retried_once() -> None:
    calls = {"count": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["count"] += 1
        raise httpx.ConnectError("no route to host", request=request)

    provider, _ = _provider(handler)
    failure = _catch(provider)

    assert isinstance(failure, ProviderUnavailable)
    assert not isinstance(failure, ProviderTimeout), "an unreachable endpoint is not a timeout"
    assert calls["count"] == 2


def _catch(provider: StepFunProvider) -> Exception:
    """Run a turn and return the failure, so a test can assert on the type."""
    try:
        provider.generate(TRANSCRIPT, tools=None, temperature=0.0, max_tokens=64)
    except Exception as exc:  # noqa: BLE001 - the test asserts on the type itself
        return exc
    raise AssertionError("expected the provider to fail")


def test_an_unrecognised_failure_is_still_reported_as_a_missing_answer() -> None:
    """No exception may escape as an opaque RuntimeError, or as a fabricated turn."""
    calls = {"count": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["count"] += 1
        raise ValueError("something nobody predicted")

    provider, _ = _provider(handler)

    with pytest.raises(ProviderUnavailable) as excinfo:
        provider.generate(TRANSCRIPT, tools=None, temperature=0.0, max_tokens=64)

    assert "ValueError" in str(excinfo.value)
    assert calls["count"] == 2


def test_the_retry_budget_cannot_be_widened() -> None:
    """Spec 12: at most one retry, so the bound is enforced at construction."""
    for bad in (0, 3, 100):
        with pytest.raises(ProviderUnavailable):
            StepFunProvider(model=STUB_MODEL, base_url=STUB_BASE_URL, api_key=FAKE_CREDENTIAL, max_attempts=bad)


def test_the_timeout_must_be_finite() -> None:
    with pytest.raises(ProviderUnavailable):
        StepFunProvider(model=STUB_MODEL, base_url=STUB_BASE_URL, api_key=FAKE_CREDENTIAL, timeout_s=0)


def test_a_max_attempts_of_one_sends_exactly_one_request() -> None:
    """The configured budget is what happens, not what the docstring says."""
    calls = {"count": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["count"] += 1
        return _status(429, "slow down")(request)

    provider, _ = _provider(handler, max_attempts=1)

    with pytest.raises(ProviderRateLimited):
        provider.generate(TRANSCRIPT, tools=None, temperature=0.0, max_tokens=64)

    assert calls["count"] == 1


def test_the_client_is_built_once_and_reused_across_turns() -> None:
    """A per-turn client would open a connection pool per model turn.

    Built here rather than injected, so the caching path is the one exercised.
    Building a client opens no socket, so the offline guarantee is untouched.
    """
    provider = StepFunProvider(model=STUB_MODEL, base_url=STUB_BASE_URL, api_key=FAKE_CREDENTIAL)

    first = provider._client()
    second = provider._client()

    assert first is second
    assert isinstance(first, openai.OpenAI)


def test_a_transport_failure_never_becomes_an_answer() -> None:
    """The harness stops the run on a ProviderError; this is the other half."""
    provider, _ = _provider(_status(500))

    with pytest.raises(ProviderError):
        provider.generate(TRANSCRIPT, tools=None, temperature=0.0, max_tokens=64)
