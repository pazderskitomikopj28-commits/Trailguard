"""Deterministic Safety Core boundary (spec Pillar 2, sections 7 and 8).

This package holds the *contracts* of the safety core plus the one piece of
real Phase 0 code, the YAML rule-pack loader. Safety-relevant levels must not
come from a language model's intuition, so the boundary between "rules and
arithmetic" and "explanation" is drawn here, explicitly.

Phase 0 exports:

* :class:`RuleDefinition` / :class:`RulePack` / :func:`load_rule_pack` --
  the frozen six-rule pack (R-A .. R-F) as validated data.
* :class:`RiskEngine` -- the abstract engine boundary; the pipeline order
  (feature extraction -> hard rules set severity floor -> soft score -> level
  mapping -> R-F compound escalation -> confidence) is documented here and in
  ``risk_core/engine.py``, but only steps 1-5 are Phase 1 work -- the confidence
  step belongs to Phase 2, which is why the Phase 1 stub message stops before it.
* :func:`aggregate_soft_score` -- soft-score aggregation, Phase 1.
* :func:`compute_confidence` -- confidence model, Phase 2.

Nothing in this package evaluates a rule, computes a projection, fetches
weather or calls a model; every such entry point raises ``NotImplementedError``
naming the phase that owes it.

>>> from trailguard.risk_core import load_rule_pack
>>> pack = load_rule_pack("trailguard/rules/rules_v1.yaml")
>>> [rule.id for rule in pack.rules]
['R-A', 'R-B', 'R-C', 'R-D', 'R-E', 'R-F']
"""

from .confidence import compute_confidence
from .engine import RiskEngine
from .hard_rules import EXPECTED_RULE_IDS, RuleDefinition, RulePack, load_rule_pack
from .scoring import aggregate_soft_score

__all__ = [
    "EXPECTED_RULE_IDS",
    "RiskEngine",
    "RuleDefinition",
    "RulePack",
    "aggregate_soft_score",
    "compute_confidence",
    "load_rule_pack",
]
