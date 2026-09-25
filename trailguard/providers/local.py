"""Local 4B-8B open-source model provider for the DGX Spark box.

Spec section 14.1 requires at least one local 4B-8B open-source model, used for
local agent inference, the no-skill / with-skill comparison, the contrast
against Step 5 Preview, and the check that the TrailGuard Skill transfers across
models. Spec section 14.3 additionally requires real runtime evidence to be
recorded for the local run:

* GPU model (GB10);
* GPU utilisation;
* local inference latency;
* tokens/s (when available);
* benchmark runtime;
* scenario count.

That evidence is *measured*, not claimed: the harness records sampling and run
metadata in the evidence trace (spec 18) and Phase 5 aggregates it into
``DgxSparkEvidence`` / ``BenchmarkReport`` in ``evals.report``. This provider
therefore stays a thin transport and does not invent numbers.

Configuration is read once, at construction time, from ``os.environ``:

* ``TG_LOCAL_MODEL`` -- local model id / repo name;
* ``TG_LOCAL_BASE_URL`` -- OpenAI-compatible base URL of the local server
  (vLLM / SGLang / llama.cpp server style).

No secret is needed locally, so no credential is stored here. Phase 4 may still
send a placeholder API key if the chosen local server insists on one; that is a
transport detail of the client construction, not a stored secret.

Phase 0 does not import any inference client, so importing this module cannot
fail on a missing local runtime.
"""

from __future__ import annotations

import os
from typing import Any

from .base import ChatMessage, ModelProvider, ModelResponse

__all__ = ["LocalModelProvider"]


class LocalModelProvider(ModelProvider):
    """OpenAI-compatible transport for a local 4B-8B model on DGX Spark.

    Responsibilities:

    * hold the local model id and base URL in exactly one place (spec 16);
    * present the same :class:`~trailguard.providers.base.ModelProvider`
      surface as :class:`~trailguard.providers.stepfun.StepFunProvider`, so the
      harness and the benchmark cannot tell which backend is loaded and the
      ``2 models x 2 skill conditions`` matrix stays fair (spec 14.2, 3.1);
    * let the harness record real GB10 runtime evidence around each call
      (spec 14.3).

    NOT responsible for:

    * loading, quantising or serving the model (a deployment concern);
    * executing tool calls or owning the tool registry (the harness, spec 15);
    * any safety logic, freshness policy or risk scoring;
    * asserting that the local model is better or worse than Step 5 Preview --
      results are recorded, never assumed (spec 3.2).

    Raises:
        NotImplementedError: Always, in Phase 0.
    """

    def __init__(self, model: str | None = None, base_url: str | None = None) -> None:
        """Capture configuration from arguments, falling back to the environment.

        Args:
            model: Local model id; defaults to ``TG_LOCAL_MODEL``.
            base_url: OpenAI-compatible base URL of the local server; defaults
                to ``TG_LOCAL_BASE_URL``.
        """
        self.model: str | None = model or os.environ.get("TG_LOCAL_MODEL")
        self.base_url: str | None = base_url or os.environ.get("TG_LOCAL_BASE_URL")

    @property
    def name(self) -> str:
        """Stable provider id ``"local"`` (recorded in the evidence trace)."""
        return "local"

    def __repr__(self) -> str:
        return f"LocalModelProvider(model={self.model!r}, base_url={self.base_url!r})"

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
            NotImplementedError: Always -- Phase 4 serves the local 4B-8B model.
        """
        raise NotImplementedError(
            "Phase 4: local 4B-8B open-source model served on DGX Spark"
        )
