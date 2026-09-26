"""Model provider interfaces for the TrailGuard agent harness (spec section 16).

One import surface for every layer::

    from trailguard.providers import ModelProvider, StepFunProvider, LocalModelProvider

Phase 0 exports the transport contracts and the two concrete backends of the
frozen evaluation matrix (spec 14.2): Step 5 Preview and a local 4B-8B
open-source model on DGX Spark. Phase 3 wires the StepFun transport on top of
the OpenAI-compatible chat-completions API (the SDK is imported lazily, so
importing this package still needs nothing beyond the base install); the local
backend still raises :class:`NotImplementedError` until Phase 4. Neither
provider executes tools, owns tool schemas, or contains any safety logic.

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
