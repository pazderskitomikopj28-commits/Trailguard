"""StepFun Step 5 Preview provider: the remote model of the frozen matrix (spec 16).

Step 5 Preview is the strong, tool-capable reference model of the frozen
``2 models x 2 skill conditions`` evaluation matrix (spec 14.2). This module is
its transport, and only its transport: it turns one list of
:class:`~trailguard.providers.base.ChatMessage` into one
:class:`~trailguard.providers.base.ModelResponse` through the OpenAI-compatible
chat-completions route, and reports the tool calls the model asked for. It does
not execute them, does not decide which horizons to fetch, and holds no safety
logic of any kind.

Configuration is read **once, at construction time** (spec 16), from the
arguments and then from the environment:

* ``STEPFUN_MODEL`` -- model id (``.env.example`` ships ``step-5-preview``);
* ``STEPFUN_BASE_URL`` -- OpenAI-compatible base URL;
* ``STEPFUN_API_KEY`` -- credential.

No other environment variable is read, and no file is ever opened for
configuration: ``tests/test_providers.py`` asserts that the identifiers this
module *executes* contain neither ``open`` nor ``Path`` nor ``dotenv``.

Secret handling
---------------
The credential lives in one private attribute, is masked by :meth:`__repr__`,
is never returned by :attr:`name`, and is never written into a trace, a log or
an exception message. Every failure this module raises names the *kind* of
failure and the endpoint -- never the key that was rejected. Provider responses
are not filtered: if a model echoes a credential-shaped string into its own
content, the trace contract's redaction layer
(:mod:`trailguard.schemas.trace`) is what keeps it out of a saved trace.

Retries
-------
At most one immediate retry, and only for the failures a retry can fix (spec
12): a timeout, a connection failure, a 429 or a 5xx. A 400, a 403/401 or a
payload that cannot be parsed is never retried -- those are deterministic, and
retrying them would only burn the budget and delay the honest failure. The
decision is read off the exception's :attr:`retryable` class attribute rather
than pattern-matched from a message, and the SDK's own retry loop is disabled
(``max_retries=0``) so the budget is exactly what this module documents: two
attempts, no more. There is no backoff sleep between them, because a benchmark
run must not stall on a flapping endpoint.

Offline testability
-------------------
``openai`` is imported lazily, inside :meth:`_client`, so importing this module
never requires the SDK and never binds it as a module attribute. A test injects
a client built over ``httpx.MockTransport`` (spec 13), which is how the
timeout, 429, 400, 5xx and malformed-response paths are exercised for real with
no network. Importing the module also never reads the environment, so a
credential-free interpreter can import the provider.
"""

from __future__ import annotations

import json
import os
from typing import Any

from .base import (
    ChatMessage,
    ModelProvider,
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

__all__ = ["DEFAULT_BASE_URL", "DEFAULT_MAX_ATTEMPTS", "DEFAULT_TIMEOUT_S", "StepFunProvider"]

#: StepFun's OpenAI-compatible entry point. A mirror or a gateway can replace it
#: through ``STEPFUN_BASE_URL`` or the constructor argument; nothing else reads it.
DEFAULT_BASE_URL: str = "https://api.stepfun.com/v1"

#: Read timeout in seconds, finite by construction (spec 12): an unbounded read
#: would hang a horizon loop on a black-holed endpoint instead of failing it.
#: A tool-calling turn with a long transcript is slower than a plain chat turn,
#: so this is deliberately generous rather than tight.
DEFAULT_TIMEOUT_S: float = 120.0

#: Total attempts including the first. Spec 12 allows at most one retry, so the
#: default is 2 and no caller can configure more.
DEFAULT_MAX_ATTEMPTS: int = 2

#: The SDK's own retry loop, disabled. TrailGuard owns the retry budget (see the
#: module docstring); leaving the default in place would make "one retry" a
#: number nobody could audit.
_SDK_MAX_RETRIES: int = 0


def _decode_arguments(payload: Any) -> dict[str, Any]:
    """Return the tool-call arguments as a dict, without ever inventing content.

    OpenAI-compatible endpoints send ``function.arguments`` as a JSON *string*.
    A malformed string is preserved under ``_raw`` rather than replaced by an
    empty dict: an empty dict would look like "the model called the tool with no
    arguments" and would sail through contract validation, whereas ``_raw`` makes
    the tool call fail validation, which the harness records as an error and hands
    back to the model so it can correct itself.
    """
    if payload is None:
        return {}
    if isinstance(payload, dict):
        return dict(payload)
    if isinstance(payload, str):
        try:
            decoded = json.loads(payload)
        except (TypeError, ValueError):
            return {"_raw": payload}
        return _decode_arguments(decoded)
    return {"_raw": repr(payload)}


class StepFunProvider(ModelProvider):
    """OpenAI-compatible chat-completions transport for Step 5 Preview.

    Args:
        model: Model id; falls back to ``STEPFUN_MODEL``.
        base_url: OpenAI-compatible base URL; falls back to ``STEPFUN_BASE_URL``.
        api_key: Credential; falls back to ``STEPFUN_API_KEY``. Stored privately
            and never logged.
        timeout_s: Read timeout in seconds. Finite and positive, or the
            construction fails (spec 12).
        max_attempts: Total attempts including the first; 1 or 2 only. The upper
            bound is enforced rather than trusted, because an unbounded retry
            loop inside a benchmark run is indistinguishable from a hang.
        client: An already-built OpenAI-compatible client. Takes precedence over
            everything else, which is what lets the offline tests inject a client
            over ``httpx.MockTransport`` (spec 13) -- and what lets an operator
            supply a shared, pre-configured client.

    Raises:
        ProviderAuthenticationError: No credential is available at call time.
            Raised when the first turn is attempted, not at construction, so a
            credential-free interpreter can still build and inspect the provider
            (and so the failure appears in the run's evidence trace instead of
            in an import).
        ProviderUnavailable: The ``openai`` package is missing (install the
            ``providers`` extra), or a client could not be built.
    """

    def __init__(
        self,
        model: str | None = None,
        base_url: str | None = None,
        api_key: str | None = None,
        *,
        timeout_s: float = DEFAULT_TIMEOUT_S,
        max_attempts: int = DEFAULT_MAX_ATTEMPTS,
        client: Any | None = None,
    ) -> None:
        if timeout_s <= 0:
            raise ProviderUnavailable(
                f"timeout_s must be positive, got {timeout_s}; an infinite timeout is not a "
                "configuration this provider accepts (spec 12)"
            )
        if max_attempts not in (1, 2):
            raise ProviderUnavailable(
                f"max_attempts must be 1 or 2, got {max_attempts}; spec 12 allows at most one "
                "retry and forbids an unbounded retry loop"
            )

        self.model: str | None = model or os.environ.get("STEPFUN_MODEL")
        self.base_url: str | None = base_url or os.environ.get("STEPFUN_BASE_URL") or DEFAULT_BASE_URL
        self._api_key: str | None = api_key or os.environ.get("STEPFUN_API_KEY")
        self._timeout_s = float(timeout_s)
        self._max_attempts = int(max_attempts)
        # Deliberately NOT named ``_client``: an instance attribute of that name
        # would shadow the ``_client()`` method and make every call fail with a
        # ``TypeError`` that looks like a transport failure.
        self._client_override: Any | None = client

    @property
    def name(self) -> str:
        """Stable provider id ``"stepfun"`` (recorded in the evidence trace)."""
        return "stepfun"

    def __repr__(self) -> str:
        """Mask the credential so no accidental log or traceback leaks it."""
        key_state = "<set>" if self._api_key else None
        return f"StepFunProvider(model={self.model!r}, base_url={self.base_url!r}, api_key={key_state})"

    # ------------------------------------------------------------------ #
    # ModelProvider
    # ------------------------------------------------------------------ #

    def generate(
        self,
        messages: list[ChatMessage],
        tools: list[dict[str, Any]] | None = None,
        temperature: float = 0.0,
        max_tokens: int = 2048,
    ) -> ModelResponse:
        """Produce exactly one model turn for ``messages``.

        Responsibility: one request, one response, with the sampling parameters
        the harness fixed (identical in both skill conditions, spec 3.1). Not
        responsible for: retrying an arbitrary number of times (bounded by
        ``max_attempts``), executing the tool calls it reports, or deciding what
        the model should have asked for.

        Args:
            messages: Full transcript, starting with the harness system prompt.
            tools: Tool schema dicts from ``trailguard.tools.tool_schema_dicts``,
                translated here into the endpoint's ``function`` shape.
            temperature: Sampling temperature fixed by the harness (spec 3.1).
            max_tokens: Output cap fixed by the harness (spec 3.1).

        Returns:
            A :class:`ModelResponse` with either text content or tool calls, plus
            the raw finish reason and usage counters when the endpoint reports
            them.

        Raises:
            ProviderError: Any transport failure, with a subclass naming the
                kind. A failure is never answered with a synthesised turn.
        """
        request = {
            "model": self.model,
            "messages": self._translate_messages(messages),
        }
        if tools:
            request["tools"] = [self._translate_tool(spec) for spec in tools]
        request["temperature"] = temperature
        request["max_tokens"] = max_tokens

        last_error: Exception | None = None
        for attempt in range(1, self._max_attempts + 1):
            try:
                completion = self._client().chat.completions.create(**request)
            except Exception as exc:
                mapped = self._map_error(exc)
                last_error = mapped
                if not getattr(mapped, "retryable", False) or attempt == self._max_attempts:
                    raise mapped from exc
                continue
            return self._translate_response(completion)

        # Unreachable while max_attempts >= 1, but a loop that can fall through
        # without either returning or raising would return None and hand the
        # harness a fabricated empty turn.
        raise ProviderUnavailable(
            f"stepfun: no request was made ({last_error!r}); the retry budget is misconfigured"
        )

    # ------------------------------------------------------------------ #
    # Wire translation (pure, no I/O -- unit-tested without a client)
    # ------------------------------------------------------------------ #

    @staticmethod
    def _translate_messages(messages: list[ChatMessage]) -> list[dict[str, Any]]:
        """Return the transcript in the endpoint's chat-completions shape.

        The assistant and ``tool`` roles are the interesting ones: an assistant
        turn must carry the calls it made (otherwise the tool results that
        follow are orphans and the endpoint rejects the request), and a ``tool``
        result must carry the ``tool_call_id`` of the call it answers so the
        endpoint can pair them. Provider order is preserved exactly -- the
        pairing is positional, so reordering would answer a different call.
        """
        payload: list[dict[str, Any]] = []
        for message in messages:
            if message.role == "assistant" and message.tool_calls:
                payload.append(
                    {
                        "role": "assistant",
                        "content": message.content or None,
                        "tool_calls": [
                            {
                                "id": call.id,
                                "type": "function",
                                "function": {
                                    "name": call.name,
                                    "arguments": json.dumps(call.arguments or {}, default=str),
                                },
                            }
                            for call in message.tool_calls
                        ],
                    }
                )
                continue
            if message.role == "tool":
                entry: dict[str, Any] = {
                    "role": "tool",
                    "content": message.content,
                    "tool_call_id": message.tool_call_id,
                }
                if message.name:
                    entry["name"] = message.name
                payload.append(entry)
                continue
            payload.append({"role": message.role, "content": message.content})
        return payload

    @staticmethod
    def _translate_tool(spec: dict[str, Any]) -> dict[str, Any]:
        """Return one tool spec in the endpoint's ``function`` wrapper.

        The registered schema dicts carry the arguments schema under
        ``input_schema`` (see ``trailguard.tools.tool_schema_dicts``); the
        OpenAI-compatible route calls the same thing ``parameters``. Both are
        accepted so a caller may hand this provider either shape, but a spec
        with neither is a configuration error and is raised loudly instead of
        silently advertising a tool with no arguments contract.
        """
        schema = spec.get("input_schema") or spec.get("parameters")
        name = spec.get("name")
        if schema is None or not name:
            raise ProviderResponseError(
                f"tool spec is missing a name or an argument schema: got keys "
                f"{sorted(spec)}; trailguard.tools.tool_schema_dicts() produces "
                "'name', 'description' and 'input_schema'"
            )
        return {
            "type": "function",
            "function": {
                "name": name,
                "description": spec.get("description", ""),
                "parameters": schema,
            },
        }

    @staticmethod
    def _translate_response(completion: Any) -> ModelResponse:
        """Return the endpoint's completion as one :class:`ModelResponse`.

        Every field is read defensively because the object is shaped by the SDK,
        not by a contract this package owns. What must not happen is a silent
        default: a completion with no choices is a malformed answer and raises,
        rather than becoming an empty final response that looks like the model
        decided to say nothing.
        """
        choices = list(getattr(completion, "choices", None) or [])
        if not choices:
            raise ProviderResponseError(
                f"stepfun returned a completion with no choices (id={getattr(completion, 'id', None)!r})"
            )

        choice = choices[0]
        message = getattr(choice, "message", None)
        if message is None:
            raise ProviderResponseError("stepfun returned a completion whose choice carries no message")

        calls: list[ToolCallRequest] = []
        seen_ids: set[str] = set()
        for index, raw_call in enumerate(getattr(message, "tool_calls", None) or [], start=1):
            function = getattr(raw_call, "function", None)
            name = getattr(function, "name", None) if function is not None else None
            raw_id = getattr(raw_call, "id", None)
            # A missing id is the endpoint being sloppy, not the model: the
            # transcript needs one to key the tool result back to the call, so a
            # positional stand-in is used and disclosed through the trace.
            call_id = str(raw_id) if raw_id else f"stepfun_call_{index}"
            if call_id in seen_ids:
                raise ProviderResponseError(
                    f"stepfun returned two tool calls with id {call_id!r}; the tool results "
                    "could not be paired back to the calls that produced them"
                )
            seen_ids.add(call_id)
            calls.append(
                ToolCallRequest(
                    id=call_id,
                    name=str(name) if name else "",
                    arguments=_decode_arguments(getattr(function, "arguments", None)),
                )
            )

        usage = getattr(completion, "usage", None)
        return ModelResponse(
            content=getattr(message, "content", None),
            tool_calls=calls,
            finish_reason=str(getattr(choice, "finish_reason", None) or "stop"),
            usage={
                "prompt_tokens": getattr(usage, "prompt_tokens", None),
                "completion_tokens": getattr(usage, "completion_tokens", None),
                "total_tokens": getattr(usage, "total_tokens", None),
            }
            if usage is not None
            else {},
        )

    # ------------------------------------------------------------------ #
    # Transport (lazy: importing this module never requires the SDK)
    # ------------------------------------------------------------------ #

    def _client(self) -> Any:
        """Return the OpenAI-compatible client, building it on first use.

        Lazy by construction: ``import openai`` happens here, inside the call
        that needs it, so ``import trailguard.providers.stepfun`` works on a
        machine without the SDK and cannot accidentally touch a credential at
        import time.

        The built client is cached in ``_client_override``, so the second turn of
        a run reuses one connection pool instead of building a second client per
        model turn.
        """
        if self._client_override is not None:
            return self._client_override

        if not self._api_key:
            # Named as an authentication failure, not a generic one: it is the
            # same class the endpoint would raise for a rejected key, so a run
            # that never had a credential and a run whose credential was revoked
            # are two instances of one failure kind instead of two.
            raise ProviderAuthenticationError(
                "STEPFUN_API_KEY is not set; export it or pass api_key=... to StepFunProvider "
                "(the harness records this as a provider failure, never as an agent answer)"
            )
        if not self.model:
            raise ProviderResponseError(
                "STEPFUN_MODEL is not set; export it or pass model=... to StepFunProvider"
            )

        try:
            import openai
        except ImportError as exc:  # pragma: no cover - depends on the install
            raise ProviderUnavailable(
                "the 'openai' package is not installed; run pip install -e '.[providers]'"
            ) from exc

        try:
            built = openai.OpenAI(
                api_key=self._api_key,
                base_url=self.base_url,
                timeout=self._timeout_s,
                max_retries=_SDK_MAX_RETRIES,
            )
        except Exception as exc:  # SDK raises its own base class for bad config
            raise ProviderUnavailable(
                f"could not build the OpenAI-compatible client for {self.base_url}"
            ) from exc

        self._client_override = built
        return built

    @staticmethod
    def _map_error(exc: Exception) -> Exception:
        """Return the :class:`ProviderError` subclass ``exc`` maps to.

        The SDK's own exception hierarchy is matched by *class*, never by
        message text: a message is an implementation detail an endpoint can
        reword without notice, whereas ``APITimeoutError`` and
        ``AuthenticationError`` are stable types. Anything unrecognised becomes
        a generic :class:`ProviderUnavailable` rather than being wrapped in an
        opaque ``RuntimeError``: a caller still learns that no answer exists.
        """
        if isinstance(exc, ProviderError):
            # Raised by this module itself -- a missing credential, a client
            # that could not be built, a malformed tool spec. Already classified.
            return exc
        try:
            import openai
        except ImportError:  # pragma: no cover - the failure came from _client()
            return ProviderUnavailable(f"stepfun transport failed: {type(exc).__name__}")

        if isinstance(exc, openai.AuthenticationError):
            return ProviderAuthenticationError(
                f"stepfun rejected the credential (HTTP {getattr(exc, 'status_code', '?')})"
            )
        if isinstance(exc, openai.PermissionDeniedError):
            return ProviderAuthenticationError(
                f"stepfun refused the request (HTTP {getattr(exc, 'status_code', '?')})"
            )
        if isinstance(exc, openai.RateLimitError):
            return ProviderRateLimited(
                f"stepfun rate-limited the caller (HTTP {getattr(exc, 'status_code', '?')})"
            )
        if isinstance(exc, openai.APITimeoutError):
            return ProviderTimeout(f"stepfun timed out: {type(exc).__name__}")
        if isinstance(exc, openai.APIConnectionError):
            return ProviderUnavailable(f"stepfun endpoint unreachable: {type(exc).__name__}")
        if isinstance(exc, openai.InternalServerError):
            return ProviderServerError(
                f"stepfun endpoint failed (HTTP {getattr(exc, 'status_code', '?')})"
            )
        if isinstance(exc, openai.APIStatusError):
            # Any remaining status error: the endpoint answered and the answer
            # was a refusal. Not retried, because a refusal is deterministic.
            return ProviderResponseError(
                f"stepfun returned HTTP {getattr(exc, 'status_code', '?')}"
            )
        return ProviderUnavailable(f"stepfun transport failed: {type(exc).__name__}")
