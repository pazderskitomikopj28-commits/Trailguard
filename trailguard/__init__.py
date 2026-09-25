"""TrailGuard contract and data layer.

This package holds the frozen Phase 0 contracts shared by every other layer of
the project: schemas, the rule pack data file and (written by later phases)
tools, risk core, agent harness, providers and evals.

Phase 0 scope is deliberately limited to contracts, interfaces, schemas, YAML
rule data, tests and docs. No business logic lives here: no GPX math, no
ETA/sunset math, no weather HTTP calls, no risk scoring, no model calls.

Phase 0: specification and project foundation.
"""

__version__ = "0.1.0"
