"""Deterministic Safety Core boundary and implementation (spec Pillar 2, sections 7 and 8).

This package is where risk levels come from rules and arithmetic, never from a
language model's intuition: the boundary between "rules and arithmetic" and
"explanation" is drawn here, explicitly. It holds the frozen rule-pack loader, the
demo-calibration file the engine reads its parameters from, and the Phase 1 engine
that evaluates the pack for one horizon.

Exports:

* :class:`RuleDefinition` / :class:`RulePack` / :func:`load_rule_pack` --
  the frozen six-rule pack (R-A .. R-F) as validated data.
* :class:`Calibration` / :func:`load_calibration` -- the demo-calibration
  parameters (clause table, score bands, severity weights, movement weights,
  battery drain, projection reliability, confidence weights) as validated data.
* :class:`RiskEngine` -- the abstract boundary. Its docstring fixes the pipeline
  order (feature extraction -> hard rules set severity floor -> soft score ->
  level mapping -> R-F compound escalation -> confidence); called on the base it
  still returns the Phase 0 placeholder, which raises ``NotImplementedError``.
* :class:`DeterministicRiskEngine` -- the Phase 1 engine: it evaluates the loaded
  pack for one horizon and returns a fully explainable :class:`RiskResult`.
* :func:`aggregate_soft_score` -- soft-score aggregation.
* :func:`compute_confidence` -- the confidence model, an engineering statement
  about the inputs.

Nothing in this package fetches an input, projects a future position, fetches
weather, reads a clock or calls a model: ``assess_risk`` runs with
``STEPFUN_API_KEY`` unset and with no network (spec 25), and every instant it
stamps a verdict with is the caller's ``analysis_time`` (spec 19).

>>> from trailguard.risk_core import load_rule_pack
>>> pack = load_rule_pack("trailguard/rules/rules_v1.yaml")
>>> [rule.id for rule in pack.rules]
['R-A', 'R-B', 'R-C', 'R-D', 'R-E', 'R-F']
>>> from trailguard.risk_core import DeterministicRiskEngine
>>> engine = DeterministicRiskEngine(pack)
>>> engine.pack is pack
True
"""

from .calibration import CALIBRATION_PATH, Calibration, load_calibration
from .confidence import compute_confidence
from .engine import DeterministicRiskEngine, RiskEngine
from .hard_rules import EXPECTED_RULE_IDS, RuleDefinition, RulePack, load_rule_pack
from .scoring import SOFT_SCORE_BANDS, aggregate_soft_score

__all__ = [
    "CALIBRATION_PATH",
    "EXPECTED_RULE_IDS",
    "SOFT_SCORE_BANDS",
    "Calibration",
    "DeterministicRiskEngine",
    "RiskEngine",
    "RuleDefinition",
    "RulePack",
    "aggregate_soft_score",
    "compute_confidence",
    "load_calibration",
    "load_rule_pack",
]
