"""StepFun Step 5 Preview provider -- Phase 0 interface only.

Step 5 Preview is the remote model of the frozen evaluation matrix (spec 14.2):
it gives TrailGuard a strong, tool-capable reference model to compare the local
DGX Spark model against, under identical skill conditions.

Configuration is read **once, at construction time, from ``os.environ``**
(defaults listed below), never from a tracked file and never hardcoded:

* ``STEPFUN_MODEL`` -- model id (``.env.example`` ships ``step-5-preview``);
* ``STEPFUN_BASE_URL`` -- OpenAI-compatible base URL;
* ``STEPFUN_API_KEY`` -- credential.

Secret handling: the API key is kept in a private attribute, is never returned
by :attr:`name`, never written into a trace, and :meth:`__repr__` masks it. The
provider has no key of its own beyond the process environment, so a leaked
traceback cannot leak a credential. It exposes no credential *probe* (no
``has_api_key``-style member): the transport surface stays identical to
:class:`~trailguard.providers.local.LocalModelProvider`, so nothing can branch on
which backend is loaded.

Phase 0 does not import ``openai``: no HTTP client is created here, so importing
this module can never fail on a missing SDK and no credential can be used
accidentally. Phase 3 constructs the client inside :meth:`generate` (or a lazy
``_client()`` helper) on top of the OpenAI-compatible chat-completions API.
"""

from __future__ import annotations

import os
from typing import Any

from .base import ChatMessage, ModelProvider, ModelResponse

__all__ = ["StepFunProvider"]


class StepFunProvider(ModelProvider):
    """OpenAI-compatible chat-completions transport for Step 5 Preview.

    Responsibilities:

    * hold the model / base-URL / credential configuration for Step 5 Preview
      in exactly one place (spec 16: model config must not spread into business
      code);
    * translate the harness's :class:`~trailguard.providers.base.ChatMessage`
      transcript and tool schema dicts into one provider request and back into a
      :class:`~trailguard.providers.base.ModelResponse`;
    * honour the harness's ``temperature`` / ``max_tokens`` so sampling is
      identical in both skill conditions (spec 3.1).

    NOT responsible for:

    * executing the tool calls it reports (the harness owns the registry, spec 15);
    * deciding which horizon, weather snapshot or rule pack to use (SKILL.md and
      ``trailguard.risk_core``);
    * any safety logic, freshness policy or risk scoring;
    * network retries / rate limiting (a Phase 3 transport concern);
    * reporting whether a credential is present. The base interface
      :class:`~trailguard.providers.base.ModelProvider` has no such member -- and
      neither does :class:`~trailguard.providers.local.LocalModelProvider`, which
      needs no credential at all -- so this class deliberately exposes none
      either: a credential-probing method would make the two providers
      non-interchangeable and let a caller branch on the backend, which spec 16
      (and the frozen 2-models x 2-conditions matrix, spec 14.3/3.1) forbids. A
      missing credential surfaces as a Phase 3 transport error from the client
      construction, not as a public probe.

    Raises:
        NotImplementedError: Always, in Phase 0.
    """

    def __init__(
        self,
        model: str | None = None,
        base_url: str | None = None,
        api_key: str | None = None,
    ) -> None:
        """Capture configuration from arguments, falling back to the environment.

        Args:
            model: Model id; defaults to ``STEPFUN_MODEL``.
            base_url: OpenAI-compatible base URL; defaults to ``STEPFUN_BASE_URL``.
            api_key: Credential; defaults to ``STEPFUN_API_KEY``. Stored privately
                and never logged.
        """
        self.model: str | None = model or os.environ.get("STEPFUN_MODEL")
        self.base_url: str | None = base_url or os.environ.get("STEPFUN_BASE_URL")
        self._api_key: str | None = api_key or os.environ.get("STEPFUN_API_KEY")

    @property
    def name(self) -> str:
        """Stable provider id ``"stepfun"`` (recorded in the evidence trace)."""
        return "stepfun"

    def __repr__(self) -> str:
        """Mask the credential so no accidental log or traceback leaks it."""
        key_state = "<set>" if self._api_key else None
        return f"StepFunProvider(model={self.model!r}, base_url={self.base_url!r}, api_key={key_state})"

    def generate(
        self,
        messages: list[ChatMessage],
        tools: list[dict[str, Any]] | None = None,
        temperature: float = 0.0,
        max_tokens: int = 2048,
    ) -> ModelResponse:
        """Not implemented in Phase 0.

        Args:
            messages: Transcript to send, starting with the harness system prompt.
            tools: Tool schema dicts from ``trailguard.tools.tool_schema_dicts``.
            temperature: Sampling temperature fixed by the harness (spec 3.1).
            max_tokens: Output cap fixed by the harness (spec 3.1).

        Raises:
            NotImplementedError: Always -- Phase 3 wires the OpenAI-compatible client.
        """
        raise NotImplementedError(
            "Phase 3: StepFun Step 5 Preview via OpenAI-compatible chat completions"
        )
