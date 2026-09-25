"""TrailGuard benchmark layer (spec sections 11, 12, 13, 3.1).

Public surface for the eval layer::

    from evals import (
        AgentMetricName, ExplanationMetricName, ScenarioMetricName,
        MetricResult, EvalRunResult, ComparisonReport, BenchmarkReport,
        DgxSparkEvidence,
    )

Phase 0 scope: the metric vocabulary, the run/result containers and the
comparison contract. Nothing in this package computes a metric, runs a scenario
or aggregates a delta yet -- those bodies raise ``NotImplementedError`` and are
Phase 5 work (spec 25).

The naming prohibition of ``evals/metrics.py`` applies to everything re-exported
here: scenario-level metrics describe the synthetic scenario dataset (spec 13)
and must never be reported as real-world accident, miss or false-alarm rates
(spec 12, 22, 27).
"""

from .compare import ComparisonReport
from .metrics import AgentMetricName, ExplanationMetricName, MetricResult, ScenarioMetricName
from .report import BenchmarkReport, DgxSparkEvidence
from .runner import EvalCondition, EvalRunResult

__all__ = [
    "AgentMetricName",
    "ExplanationMetricName",
    "ScenarioMetricName",
    "MetricResult",
    "EvalCondition",
    "EvalRunResult",
    "ComparisonReport",
    "BenchmarkReport",
    "DgxSparkEvidence",
]
