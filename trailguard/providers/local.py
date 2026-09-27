"""Local model provider for the DGX Spark box: the second model of the matrix.

Spec section 14.1 requires at least one local open-source model on the DGX Spark
hardware, used for local agent inference, the no-skill / with-skill comparison,
the contrast against Step 5 Preview, and the check that the TrailGuard Skill
transfers across models. Spec section 14.3 additionally requires real runtime
evidence to be recorded for the local run:

* GPU model (GB10);
* GPU utilisation;
* local inference latency;
* tokens/s (when available);
* benchmark runtime;
* scenario count.

That evidence is *measured*, not claimed: the harness records sampling and run
metadata in the evidence trace (spec 18) and Phase 5 aggregates it into
``DgxSparkEvidence`` / ``BenchmarkReport`` in ``evals.report``. This provider is
therefore a thin transport and invents no numbers.

Configuration is read **once, at construction time** (spec 16), from the
arguments and then from the environment:

* ``TRAILGUARD_LOCAL_MODEL`` -- served model id;
* ``TRAILGUARD_LOCAL_BASE_URL`` -- OpenAI-compatible base URL of the local server;
* ``TRAILGUARD_LOCAL_API_KEY`` -- a placeholder credential, for a server that
  insists on one.

``TG_LOCAL_MODEL`` and ``TG_LOCAL_BASE_URL`` are still honoured as fallbacks:
they are the names Phase 0 shipped in ``.env.example``, and silently dropping a
name an operator may already have exported would fail a working deployment for
no reason the operator can see. The new names win when both are set.

No real secret is involved: a local vLLM deployment authenticates nothing, so the
"credential" is a non-secret placeholder the client requires structurally. It is
handled exactly like a credential anyway -- one private attribute, masked by
:meth:`__repr__`, never in a trace, a log or an exception message -- because a
rule that holds only for values that look secret is not a rule.

Tool calling is the whole point of this provider in Phase 4. The server is a
local vLLM started with ``--enable-auto-tool-choice --tool-call-parser hermes``,
and the model's own chat template emits the Hermes envelope, so tool calls arrive
as native ``message.tool_calls``. This module contains **no** tool-call parsing:
not a regex, not an XML reader, not a split on the envelope. If the endpoint ever
stops returning structured calls, this provider will faithfully report an empty
``tool_calls`` list and the harness will record whatever the model actually did --
which is the honest outcome, because a provider that rescued tool calls out of
prose would be the component under test manufacturing its own evidence.

``skip_special_tokens`` is likewise not touched from here. The ``hermes`` parser
forces it off server-side when tools are advertised; setting it from the client
would be this module compensating for the server's configuration.
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

__all__ = ["DEFAULT_BASE_URL", "DEFAULT_MAX_ATTEMPTS", "DEFAULT_MODEL",
           "DEFAULT_TIMEOUT_S", "PLACEHOLDER_API_KEY", "LocalModelProvider"]

#: The local vLLM endpoint on the Spark box itself. Loopback by default and
#: never a remote address: an operator can point it at an SSH tunnel through the
#: constructor argument or the environment, and no host, IP or port is baked in.
DEFAULT_BASE_URL: str = "http://127.0.0.1:8000/v1"

#: The model id this deployment serves. Overridable so the same provider code
#: drives a differently-named local checkpoint without a branch anywhere.
DEFAULT_MODEL: str = "qwen3-vl-30b-a3b"

#: Read timeout in seconds, finite by construction (spec 12). Generous, because a
#: tool-calling turn carries the whole route/weather/device transcript and runs on
#: a shared GPU with ``--max-num-seqs 1``: a tight timeout would fail a run that
#: was merely thinking, and a failed run is recorded as ``provider_error``.
DEFAULT_TIMEOUT_S: float = 300.0

#: Total attempts including the first. Spec 12 allows at most one retry, so the
#: default is 2 and no caller can configure more -- an unbounded retry loop
#: inside a benchmark run is indistinguishable from a hang.
DEFAULT_MAX_ATTEMPTS: int = 2

#: The SDK's own retry loop, disabled, exactly as in the StepFun transport: one
#: readable retry budget instead of two overlapping ones.
_SDK_MAX_RETRIES: int = 0

#: Structural placeholder for a local server that requires the header. Not a
#: secret, and never printed.
PLACEHOLDER_API_KEY: str = "EMPTY"

#: Environment variable names, in precedence order. The Phase 4 names first,
#: then the Phase 0 names kept as fallbacks.
_MODEL_ENV = ("TRAILGUARD_LOCAL_MODEL", "TG_LOCAL_MODEL")
_BASE_URL_ENV = ("TRAILGUARD_LOCAL_BASE_URL", "TG_LOCAL_BASE_URL")
_API_KEY_ENV = ("TRAILGUARD_LOCAL_API_KEY",)


def _from_environment(names: tuple[str, ...]) -> str | None:
    """Return the first of ``names`` that is set to a non-empty value."""
    for name in names:
        value = os.environ.get(name)
        if value:
            return value
    return None


def _decode_arguments(payload: Any) -> dict[str, Any]:
    """Return the tool-call arguments as a dict, without ever inventing content.

    OpenAI-compatible endpoints send ``function.arguments`` as a JSON *string*.
    A malformed string is preserved under ``_raw`` rather than replaced by an
    empty dict: an empty dict would look like "the model called the tool with no
    arguments" and would sail through contract validation, whereas ``_raw`` makes
    the call fail validation, which the harness records as an error and hands back
    to the model so it can correct itself.

    Deliberately ``json.loads`` and nothing else: ``eval`` and
    ``ast.literal_eval`` would execute or trust model-supplied text, and a model
    turn is exactly the input that must never reach an interpreter.
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


class LocalModelProvider(ModelProvider):
    """OpenAI-compatible chat-completions transport for the local DGX Spark model.

    Args:
        model: Served model id; falls back to ``TRAILGUARD_LOCAL_MODEL`` /
            ``TG_LOCAL_MODEL`` / :data:`DEFAULT_MODEL`.
        base_url: OpenAI-compatible base URL; falls back to
            ``TRAILGUARD_LOCAL_BASE_URL`` / ``TG_LOCAL_BASE_URL`` /
            :data:`DEFAULT_BASE_URL`.
        api_key: Placeholder credential for a server that insists on one; falls
            back to ``TRAILGUARD_LOCAL_API_KEY`` / :data:`PLACEHOLDER_API_KEY`.
            Stored privately, masked by ``__repr__``, never logged.
        timeout_s: Read timeout in seconds. Finite and positive, or the
            construction fails (spec 12).
        max_attempts: Total attempts including the first; 1 or 2 only.
        client: An already-built OpenAI-compatible client. Takes precedence over
            everything else, which is what lets the offline tests inject a client
            over ``httpx.MockTransport`` (spec 13) -- and what lets an operator
            supply a shared, pre-configured client.

    Raises:
        ProviderUnavailable: A non-positive timeout, or a ``max_attempts`` outside
            1..2 -- both are configuration errors raised at construction, so a
            misconfigured provider fails before it can produce a run at all.
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

        self.model: str = (
            model or _from_environment(_MODEL_ENV) or DEFAULT_MODEL
        )
        self.base_url: str = (
            base_url or _from_environment(_BASE_URL_ENV) or DEFAULT_BASE_URL
        )
        self._api_key: str = api_key or _from_environment(_API_KEY_ENV) or PLACEHOLDER_API_KEY
        self._timeout_s = float(timeout_s)
        self._max_attempts = int(max_attempts)
        # Deliberately NOT named ``_client``: an instance attribute of that name
        # would shadow the ``_client()`` method and make every call fail with a
        # ``TypeError`` that looks like a transport failure.
        self._client_override: Any | None = client

    @property
    def name(self) -> str:
        """Stable provider id ``"local"`` (recorded in the evidence trace)."""
        return "local"

    def __repr__(self) -> str:
        """Report the configuration, and claim no credential.

        The local endpoint authenticates nobody, so there is no secret to mask and
        no secret to pretend exists: printing ``api_key=<set>`` would signal secret
        state the transport does not carry, and a reader auditing a trace or a log
        should not be told to look for something that was never there. The private
        placeholder attribute still holds whatever an operator exported, and still
        never reaches this string.

        The remote provider masks a real credential in its ``__repr__``; that rule
        is unchanged. This is not an exception to it -- it is the same rule applied
        to a transport that has nothing to hide.
        """
        return f"LocalModelProvider(model={self.model!r}, base_url={self.base_url!r})"

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

        Responsibility: one request, one response, with the sampling parameters the
        harness fixed (identical in both skill conditions, spec 3.1). Not
        responsible for: retrying more than the documented budget, executing the
        tool calls it reports, or deciding what the model should have asked for.

        The request carries ``tool_choice="auto"`` whenever tools are advertised.
        That is the endpoint's documented behaviour for an auto-tool-choice
        deployment, stated explicitly so the request on the wire is auditable
        rather than an implicit default two layers down. It is a property of the
        *endpoint*, not of the model and not of the skill condition: the same call
        shape reaches both conditions, and the harness is never consulted.

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
            ProviderError: Any transport failure, with a subclass naming the kind.
                A failure is never answered with a synthesised turn: if the local
                server is down, the run stops with ``provider_error`` and the trace
                says so. There is no fallback to another provider and no cached
                answer -- a rerouted run would be a benchmark row for a run that
                did not happen (spec 17).
        """
        request: dict[str, Any] = {
            "model": self.model,
            "messages": self._translate_messages(messages),
        }
        if tools:
            request["tools"] = [self._translate_tool(spec) for spec in tools]
            request["tool_choice"] = "auto"
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
            f"local: no request was made ({last_error!r}); the retry budget is misconfigured"
        )

    # ------------------------------------------------------------------ #
    # Wire translation (pure, no I/O -- unit-tested without a client)
    # ------------------------------------------------------------------ #

    @staticmethod
    def _translate_messages(messages: list[ChatMessage]) -> list[dict[str, Any]]:
        """Return the transcript in the endpoint's chat-completions shape.

        The assistant and ``tool`` roles are the interesting ones: an assistant turn
        must carry the calls it made (otherwise the tool results that follow are
        orphans and the endpoint rejects the request), and a ``tool`` result must
        carry the ``tool_call_id`` of the call it answers so the endpoint can pair
        them. Provider order is preserved exactly -- the pairing is positional, so
        reordering would answer a different call.

        The ids are forwarded verbatim, end to end: the value the server assigned to
        a call is the value handed back in the ``tool`` role, so the pairing the
        endpoint established is the pairing this provider asserts.
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
        accepted so a caller may hand this provider either shape, but a spec with
        neither is a configuration error and is raised loudly instead of silently
        advertising a tool with no arguments contract.

        This mirrors the remote provider's translator because both face the same
        two shapes, and the two transports stay independently readable rather than
        sharing a module neither owns.
        """
        schema = spec.get("input_schema") or spec.get("parameters")
        name = spec.get("name")
        if schema is None or not name:
            raise ProviderResponseError(
                f"tool spec is missing a name or an argument schema: got keys "
                f"{sorted(spec)}; trailguard.tools.tool_schema_dicts() produces "
                f"'name', 'description' and 'input_schema'"
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

        Read defensively, because the object is shaped by the SDK rather than by a
        contract this package owns. What must not happen is a silent default: a
        completion with no choices is a malformed answer and raises, rather than
        becoming an empty final response that looks like the model decided to say
        nothing.

        ``message.tool_calls`` is copied through as the endpoint sent it. The
        provider neither embellishes it (no synthetic ids, no inferred names) nor
        parses it out of ``content`` -- a structured call the endpoint did not
        send is not one this module will invent.
        """
        choices = list(getattr(completion, "choices", None) or [])
        if not choices:
            raise ProviderResponseError(
                f"local server returned a completion with no choices "
                f"(id={getattr(completion, 'id', None)!r})"
            )

        choice = choices[0]
        message = getattr(choice, "message", None)
        if message is None:
            raise ProviderResponseError(
                "local server returned a completion whose choice carries no message"
            )

        calls: list[ToolCallRequest] = []
        seen_ids: set[str] = set()
        for index, raw_call in enumerate(getattr(message, "tool_calls", None) or [], start=1):
            function = getattr(raw_call, "function", None)
            name = getattr(function, "name", None) if function is not None else None
            raw_id = getattr(raw_call, "id", None)
            # A missing id is the endpoint being sloppy, not the model: the
            # transcript needs one to key the tool result back to the call, so a
            # positional stand-in is used and disclosed through the trace.
            call_id = str(raw_id) if raw_id else f"local_call_{index}"
            if call_id in seen_ids:
                raise ProviderResponseError(
                    f"local server returned two tool calls with id {call_id!r}; the tool "
                    "results could not be paired back to the calls that produced them"
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

        Lazy by construction: ``import openai`` happens here, inside the call that
        needs it, so ``import trailguard.providers.local`` works on a machine
        without the SDK and cannot touch the environment at import time.

        The built client is cached in ``_client_override``, so the second turn of a
        run reuses one connection pool instead of building a second client per
        model turn.
        """
        if self._client_override is not None:
            return self._client_override

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
            # The exception is chained, not summarised: an SDK construction failure
            # carries the configuration it rejected, and echoing that back could
            # surface a real credential if an operator pointed this at a remote
            # endpoint. The class name is what a caller can act on.
            raise ProviderUnavailable(
                f"could not build the OpenAI-compatible client for {self.base_url}: "
                f"{type(exc).__name__}"
            ) from exc

        self._client_override = built
        return built

    @staticmethod
    def _map_error(exc: Exception) -> Exception:
        """Return the :class:`ProviderError` subclass ``exc`` maps to.

        The SDK's own exception hierarchy is matched by *class*, never by message
        text: a message is an implementation detail an endpoint can reword without
        notice, whereas ``APITimeoutError`` and ``AuthenticationError`` are stable
        types. Anything unrecognised becomes a generic
        :class:`ProviderUnavailable` rather than being wrapped in an opaque
        ``RuntimeError``: a caller still learns that no answer exists.

        An unreachable local endpoint is the failure worth naming best here. It is
        reported as :class:`ProviderUnavailable` -- there was no answer -- and the
        run stops with ``provider_error``. Nothing falls back, so a Spark box with
        its server stopped produces an honest record of a stopped agent rather
        than a plausible-looking answer from somewhere else.
        """
        if isinstance(exc, ProviderError):
            # Raised by this module itself -- a malformed tool spec, a client that
            # could not be built. Already classified.
            return exc
        try:
            import openai
        except ImportError:  # pragma: no cover - the failure came from _client()
            return ProviderUnavailable(f"local transport failed: {type(exc).__name__}")

        if isinstance(exc, openai.AuthenticationError):
            return ProviderAuthenticationError(
                f"local server rejected the credential (HTTP "
                f"{getattr(exc, 'status_code', '?')}); a local vLLM deployment should "
                "not be authenticating anyone -- check the endpoint this is pointed at"
            )
        if isinstance(exc, openai.PermissionDeniedError):
            return ProviderAuthenticationError(
                f"local server refused the request (HTTP {getattr(exc, 'status_code', '?')})"
            )
        if isinstance(exc, openai.RateLimitError):
            return ProviderRateLimited(
                f"local server rate-limited the caller (HTTP "
                f"{getattr(exc, 'status_code', '?')}); with --max-num-seqs 1 a burst is "
                "expected, and the run stops rather than queueing silently"
            )
        if isinstance(exc, openai.APITimeoutError):
            return ProviderTimeout(f"local server timed out: {type(exc).__name__}")
        if isinstance(exc, openai.APIConnectionError):
            return ProviderUnavailable(
                f"local server unreachable at the configured endpoint: {type(exc).__name__}; "
                "no fallback was attempted -- start the server or point "
                "TRAILGUARD_LOCAL_BASE_URL at it"
            )
        if isinstance(exc, openai.InternalServerError):
            return ProviderServerError(
                f"local server failed (HTTP {getattr(exc, 'status_code', '?')})"
            )
        if isinstance(exc, openai.APIStatusError):
            # Any remaining status error: the endpoint answered and the answer was
            # a refusal. Not retried, because a refusal is deterministic. This is
            # where a misconfigured --tool-call-parser surfaces (vLLM answers 400
            # for a tool_choice it cannot serve), which is exactly the failure the
            # Phase 4 preflight exists to catch before any TrailGuard code runs.
            return ProviderResponseError(
                f"local server returned HTTP {getattr(exc, 'status_code', '?')}: "
                f"{type(exc).__name__}"
            )
        return ProviderUnavailable(f"local transport failed: {type(exc).__name__}")
