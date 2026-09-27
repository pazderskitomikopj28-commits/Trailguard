"""Model provider interfaces for the TrailGuard agent harness (spec section 16).

One import surface for every layer::

    from trailguard.providers import ModelProvider, StepFunProvider, LocalModelProvider

Phase 0 exports the transport contracts and the two concrete backends of the
frozen evaluation matrix (spec 14.2): Step 5 Preview and a local
`Qwen/Qwen3-VL-30B-A3B-Instruct` checkpoint served by vLLM on DGX Spark. Phase 3
wires the StepFun transport on top of the OpenAI-compatible chat-completions API
and Phase 4 wires the local one the same way (the SDK is imported lazily, so
importing this package still needs nothing beyond the base install). Both
backends were deployed and exercised against the frozen harness in Phase 4; the
local endpoint reports `max_model_len=16384`, which is what the runs stayed at.
Neither provider executes tools, owns tool schemas, or contains any safety logic.

The failure vocabulary is exported alongside the contracts, so a caller that
catches a transport failure branches on one hierarchy instead of on two::

    from trailguard.providers import ProviderError, ProviderTimeout
"""

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
from .local import LocalModelProvider
from .stepfun import StepFunProvider

__all__ = [
    "ChatMessage",
    "LocalModelProvider",
    "ModelProvider",
    "ModelResponse",
    "ProviderAuthenticationError",
    "ProviderError",
    "ProviderRateLimited",
    "ProviderResponseError",
    "ProviderServerError",
    "ProviderTimeout",
    "ProviderUnavailable",
    "StepFunProvider",
    "ToolCallRequest",
]
