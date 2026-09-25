"""Scripted model-provider doubles for the Phase 0 harness tests.

Responsibility: let a test drive
:func:`trailguard.agent.harness.run_agent` without any network, any real model
and any credential, while recording exactly what the harness handed to the
provider (messages, tool schema dicts, temperature, max_tokens). Those records
are what the fairness assertions of spec 3.1 are checked against: both skill
conditions must offer the same tools and the same sampling parameters.

Explicit non-responsibility: these doubles never implement a model. They return
the scripted :class:`ModelResponse` objects a test wrote, and they never decide
anything about tools, horizons, weather or risk -- the harness and the skill own
those decisions.
"""

from __future__ import annotations

from typing import Any, Sequence

from trailguard.providers.base import ChatMessage, ModelProvider, ModelResponse

#: Answer returned when a scripted response list runs out, so a test can never
#: hang inside an unexpected extra model turn.
FALLBACK_CONTENT = "[stub] scripted responses exhausted"


class StubProvider(ModelProvider):
    """Replay a fixed list of :class:`ModelResponse` objects, recording every call.

    The provider is deliberately stateless with respect to the task: call ``n``
    receives script entry ``n`` (1-based). The recording dictionaries are plain
    data so a test can assert on them without touching provider internals.
    """

    def __init__(
        self,
        responses: Sequence[ModelResponse] | None = None,
        *,
        provider_name: str = "stub",
        model_name: str = "stub-model",
    ) -> None:
        """Store the script and start with an empty call log.

        Args:
            responses: Ordered model turns to replay. When exhausted, the
                provider answers with :data:`FALLBACK_CONTENT` instead of looping.
            provider_name: Transport identifier reported by :attr:`name`, which
                the harness records in the evidence trace's ``provider`` field.
            model_name: Concrete model string reported by ``model``, which the
                harness records in the evidence trace's ``model_name`` field.
                The two are kept separate on purpose: the benchmark's same-model
                fairness audit (spec 3.1, 14.2) compares the model string, and a
                double that conflated them would hide a model swap.
        """
        self._responses: list[ModelResponse] = list(responses or [])
        self._provider_name = provider_name
        self.model = model_name
        self.calls: list[dict[str, Any]] = []

    # ------------------------------------------------------------- provider API

    @property
    def name(self) -> str:
        """Stable provider identifier recorded in the evidence trace."""
        return self._provider_name

    def generate(
        self,
        messages: list[ChatMessage],
        tools: list[dict[str, Any]] | None = None,
        temperature: float = 0.0,
        max_tokens: int = 2048,
    ) -> ModelResponse:
        """Record the call and return the next scripted response.

        Args:
            messages: Full transcript, starting with the system prompt.
            tools: Tool schema dicts the harness advertised (or ``None``).
            temperature: Sampling temperature chosen by the harness.
            max_tokens: Output cap chosen by the harness.

        Returns:
            The scripted :class:`ModelResponse`, or a plain-text fallback once
            the script is exhausted.
        """
        self.calls.append(
            {
                "messages": list(messages),
                "tools": list(tools) if tools is not None else None,
                "temperature": temperature,
                "max_tokens": max_tokens,
            }
        )
        index = len(self.calls) - 1
        if index < len(self._responses):
            return self._responses[index]
        return ModelResponse(content=FALLBACK_CONTENT, tool_calls=[])

    # ------------------------------------------------------------- assertions

    @property
    def call_count(self) -> int:
        """How many times the harness called the provider."""
        return len(self.calls)

    def offered_tool_names(self, call_index: int = 0) -> list[str]:
        """Tool names advertised on call ``call_index`` (``[]`` when none)."""
        tools = self.calls[call_index]["tools"] or []
        return [spec.get("name", "") for spec in tools if isinstance(spec, dict)]

    def messages_of(self, call_index: int = 0) -> list[ChatMessage]:
        """Transcript handed to the provider on call ``call_index``."""
        return list(self.calls[call_index]["messages"])

    def system_prompts(self, call_index: int = 0) -> list[str]:
        """System-message contents of call ``call_index`` (at most one)."""
        return [m.content for m in self.messages_of(call_index) if m.role == "system"]

    def user_messages(self, call_index: int = 0) -> list[str]:
        """User-message contents of call ``call_index`` (at most one)."""
        return [m.content for m in self.messages_of(call_index) if m.role == "user"]

    def sampling_of(self, call_index: int = 0) -> tuple[float, int]:
        """``(temperature, max_tokens)`` the harness passed on call ``call_index``."""
        return float(self.calls[call_index]["temperature"]), int(self.calls[call_index]["max_tokens"])


class FailingProvider(ModelProvider):
    """Provider whose transport always fails.

    Used to assert that the harness turns a provider failure into
    ``stop_reason="provider_error"`` while still returning the partial evidence
    trace, instead of silently producing an answer.
    """

    def __init__(self, message: str = "stub transport failure") -> None:
        """Store the failure message raised by :meth:`generate`."""
        self.message = message
        self.model = "failing-stub-model"
        self.calls = 0

    @property
    def name(self) -> str:
        """Stable provider identifier recorded in the evidence trace."""
        return "failing-stub"

    def generate(
        self,
        messages: list[ChatMessage],
        tools: list[dict[str, Any]] | None = None,
        temperature: float = 0.0,
        max_tokens: int = 2048,
    ) -> ModelResponse:
        """Raise instead of producing a turn."""
        self.calls += 1
        raise RuntimeError(self.message)


__all__ = ["FALLBACK_CONTENT", "FailingProvider", "StubProvider"]
