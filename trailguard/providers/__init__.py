"""Model provider interfaces for the TrailGuard agent harness (spec section 16).

One import surface for every layer::

    from trailguard.providers import ModelProvider, StepFunProvider, LocalModelProvider

Phase 0 exports the transport contracts and the two concrete backends of the
frozen evaluation matrix (spec 14.2): Step 5 Preview and a local 4B-8B
open-source model on DGX Spark. Both raise :class:`NotImplementedError` inside
:meth:`~trailguard.providers.base.ModelProvider.generate` -- the network client
is Phase 3 (StepFun) / Phase 4 (local). Neither provider executes tools, owns
tool schemas, or contains any safety logic.
"""

from .base import ChatMessage, ModelProvider, ModelResponse, ToolCallRequest
from .local import LocalModelProvider
from .stepfun import StepFunProvider

__all__ = [
    "ChatMessage",
    "LocalModelProvider",
    "ModelProvider",
    "ModelResponse",
    "StepFunProvider",
    "ToolCallRequest",
]
