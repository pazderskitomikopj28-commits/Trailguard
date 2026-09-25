"""TrailGuard agent runtime: harness, skill loader, trace collector (spec 15).

The harness is the single runtime used by both the benchmark and the demo
(spec 15). It owns the model-turn / tool-call loop only: loading or not loading
``SKILL.md`` is its one behavioural switch (spec 3.1), every other parameter is
shared by both skill conditions.

Phase 0 scope: the loop, the interfaces and the trace plumbing are real; the
model transport (``trailguard.providers``) and the deterministic safety core
(``trailguard.risk_core``) raise ``NotImplementedError`` until their phases.
"""

from .harness import HARNESS_BASE_SYSTEM_PROMPT, AgentRunResult, run_agent
from .skill_loader import DEFAULT_SKILL_PATH, load_skill_md
from .trace import TraceCollector

__all__ = [
    "AgentRunResult",
    "DEFAULT_SKILL_PATH",
    "HARNESS_BASE_SYSTEM_PROMPT",
    "TraceCollector",
    "load_skill_md",
    "run_agent",
]
