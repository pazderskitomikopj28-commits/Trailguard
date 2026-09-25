"""Model provider contracts for the TrailGuard agent harness (spec section 16).

Spec section 16 freezes one provider interface so that a *single* harness
(``trailguard.agent.harness``) can drive both models of the frozen
``2 models x 2 skill conditions`` matrix (spec 14.2):

* :class:`~trailguard.providers.stepfun.StepFunProvider` -- Step 5 Preview
  through an OpenAI-compatible chat-completions endpoint;
* :class:`~trailguard.providers.local.LocalModelProvider` -- a local 4B-8B
  open-source model served on the DGX Spark box (spec 14.1).

Responsibility of a provider: pure transport. It turns one list of
:class:`ChatMessage` into one :class:`ModelResponse` for one model, honouring
the sampling parameters the harness hands in, and reports tool calls the model
asked for via :attr:`ModelResponse.tool_calls`.

NOT responsible for (Phase 0 boundary -- later phases own these):

* executing tool calls or owning the tool registry (the harness does, spec 15);
* deciding which horizons / weather / rule data to fetch (SKILL.md + tools);
* freshness policy, confidence, risk scoring or any safety logic
  (``trailguard.risk_core``, spec 8);
* scattering model configuration through business code (spec 16) -- every
  provider reads its own configuration once, at construction time.

Note on tool calling: spec section 16 sketches a ``tool_call`` method alongside
``generate``. The frozen Phase 0 surface expresses tool calling *inside*
:meth:`ModelProvider.generate` through :attr:`ModelResponse.tool_calls`, so the
harness stays provider-agnostic and a replay provider can be written without
any HTTP client. The harness owns tool execution either way.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

__all__ = ["ChatMessage", "ModelProvider", "ModelResponse", "ToolCallRequest"]


class ToolCallRequest(BaseModel):
    """A tool invocation requested by the model.

    The harness -- not the provider -- decides whether the requested tool
    exists, is implemented in the current phase, and is allowed to run.
    """

    model_config = ConfigDict(extra="forbid")

    id: str = Field(description="Provider-assigned call id; echoed back in the tool result.")
    name: str = Field(description="Requested tool name, e.g. 'project_route_state'.")
    arguments: dict[str, Any] = Field(
        default_factory=dict, description="Raw model-supplied tool arguments (unvalidated here)."
    )


class ChatMessage(BaseModel):
    """One message in a provider-agnostic conversation.

    Roles follow the OpenAI-compatible chat shape so both the remote
    (Step 5 Preview) and the local model see identical transcripts.
    ``tool_call_id`` links a ``tool`` message back to the
    :class:`ToolCallRequest` it answers; ``name`` carries the tool name.

    ``tool_calls`` is the assistant side of the same exchange: an assistant turn
    that requested tools must record which calls it made, otherwise the
    ``tool`` messages that follow are orphans and neither provider can rebuild
    the transcript OpenAI-compatibly (both the remote chat-completions endpoint
    and the local prompt template key tool results by call id). Defined after
    :class:`ToolCallRequest` because pydantic resolves annotations eagerly.
    """

    model_config = ConfigDict(extra="forbid")

    role: Literal["system", "user", "assistant", "tool"] = Field(
        description="Conversation role: system (harness + optional SKILL.md), "
        "user (task), assistant (model output), tool (tool result)."
    )
    content: str = Field(description="Message text.")
    tool_call_id: str | None = Field(
        default=None, description="Id of the ToolCallRequest this tool result answers."
    )
    name: str | None = Field(default=None, description="Tool name for a tool result message.")
    tool_calls: list[ToolCallRequest] = Field(
        default_factory=list,
        description="Tool calls this assistant turn requested, in emission order (empty for other roles).",
    )


class ModelResponse(BaseModel):
    """One model turn: either final text content, or tool calls to execute.

    ``content`` and ``tool_calls`` are both optional so a provider can return a
    pure text turn, a pure tool-calling turn, or a mixed turn; the harness
    checks ``tool_calls`` first, then falls back to ``content``.
    """

    model_config = ConfigDict(extra="forbid")

    content: str | None = Field(default=None, description="Assistant text, if any.")
    tool_calls: list[ToolCallRequest] = Field(
        default_factory=list, description="Tool calls the model asked for, in emission order."
    )
    finish_reason: str = Field(default="stop", description="Raw provider finish reason.")
    usage: dict[str, Any] = Field(
        default_factory=dict,
        description="Provider usage counters (prompt/completion tokens when reported).",
    )


class ModelProvider(ABC):
    """Transport interface every TrailGuard model backend implements.

    Implementations must be interchangeable inside the harness: the harness
    passes the same messages, tool schema dicts, temperature, max_tokens and
    max_steps regardless of which provider is loaded, which is what makes the
    no-skill / with-skill comparison fair (spec 3.1).

    Implementations are NOT allowed to:

    * invent weather, route or risk facts (the deterministic core and the tools
      own data; the model only reasons over what it is given, spec 4.2);
    * execute tools, run the horizon loop, or apply safety overrides;
    * hardcode a credential or a model choice in business code (spec 16).
    """

    @property
    @abstractmethod
    def name(self) -> str:
        """Stable provider identifier, e.g. ``"stepfun"`` or ``"local"``.

        Recorded verbatim in the evidence trace's ``provider`` field (spec 18);
        the concrete model string is exposed separately as ``model`` so the
        trace can distinguish provider from model.
        """
        raise NotImplementedError

    @abstractmethod
    def generate(
        self,
        messages: list[ChatMessage],
        tools: list[dict[str, Any]] | None = None,
        temperature: float = 0.0,
        max_tokens: int = 2048,
    ) -> ModelResponse:
        """Produce exactly one model turn for ``messages``.

        Args:
            messages: Full transcript in OpenAI-compatible shape.
            tools: JSON-schema-shaped tool specs (see
                ``trailguard.tools.tool_schema_dicts``) or ``None`` when the
                model must answer without tools.
            temperature: Sampling temperature chosen by the harness; identical
                across skill conditions (spec 3.1).
            max_tokens: Output cap chosen by the harness; identical across
                skill conditions.

        Returns:
            A :class:`ModelResponse` carrying either text content or tool calls.

        Raises:
            NotImplementedError: In Phase 0, for every concrete provider.
        """
        raise NotImplementedError
