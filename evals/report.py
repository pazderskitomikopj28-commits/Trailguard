"""Benchmark report contracts, including the DGX Spark runtime evidence (spec sections 14.3, 11, 12).

Two containers, no behaviour:

* :class:`DgxSparkEvidence` -- the platform evidence the competition requires to
  be recorded for every local-inference benchmark: GPU model, GPU utilisation,
  local inference latency, tokens/s when the runtime reports it, benchmark
  runtime and the number of scenarios (spec 14.3). At least one such real local
  inference record must be shown in the README and the demo video; that is why
  it is a first-class contract and not a log line.
* :class:`BenchmarkReport` -- the aggregate of one benchmark execution: the runs
  it contains, a per-metric summary, the regressions and the failure cases, and
  the platform evidence for the machine they were produced on.

This module is interface-only in Phase 0. Nothing here computes a summary,
classifies a regression or measures the GPU; Phase 5 owns that (spec 25), and
``None`` platform fields mean "not measured", never "zero".
"""

from pydantic import BaseModel, ConfigDict, Field

from .runner import EvalRunResult

__all__ = ["BenchmarkReport", "DgxSparkEvidence"]


class DgxSparkEvidence(BaseModel):
    """Real local-inference evidence for one benchmark execution (spec 14.3).

    Responsibility: record what actually happened on the DGX Spark box, so the
    "runs on a local GB10" claim is backed by numbers a reviewer can re-check.
    Not responsible for: measuring anything -- the benchmark runner collects
    these values, and a ``None`` field means the measurement was not taken (it
    must be reported as missing, never defaulted to a flattering value).
    """

    model_config = ConfigDict(extra="forbid")

    gpu_model: str = Field(..., description="GPU identifier reported by the runtime, e.g. 'NVIDIA GB10'.")
    gpu_utilization_avg: float | None = Field(
        default=None, description="Average GPU utilisation over the benchmark, in percent, when sampled."
    )
    tokens_per_s: float | None = Field(
        default=None, description="Decode throughput in tokens per second, when the runtime reports it."
    )
    local_inference_latency_s: float | None = Field(
        default=None, description="Mean local end-to-end inference latency in seconds, when measured."
    )
    benchmark_runtime_s: float | None = Field(
        default=None, description="Wall-clock duration of the whole benchmark in seconds, when measured."
    )
    scenario_count: int = Field(..., description="Number of scenarios executed in the measured run.")


class BenchmarkReport(BaseModel):
    """Aggregate outcome of one benchmark execution over the scenario dataset.

    Responsibility: collect the individual runs, the per-metric summary, the
    regressions (where the treatment lost to the baseline, spec 3.2 requires
    these to be reported, not hidden) and the failure cases (runs that errored,
    hit the step cap or lost their provider), plus the platform evidence.
    Not responsible for: producing any of those values (Phase 5), or for
    claiming that the skill is better -- the summary is data, the verdict is the
    reviewer's.
    """

    model_config = ConfigDict(extra="forbid")

    runs: list[EvalRunResult] = Field(..., description="Every run included in this benchmark.")
    metric_summary: dict = Field(
        ..., description="Metric name -> aggregated value over the runs, with per-condition breakdown."
    )
    regressions: list[str] = Field(
        ...,
        description="Cases where the with-skill condition did worse than the baseline (spec 3.2).",
    )
    failure_cases: list[str] = Field(
        ..., description="Runs that failed, hit the max-step budget or lost their provider."
    )
    dgx_spark_evidence: DgxSparkEvidence | None = Field(
        ..., description="Platform evidence for this benchmark, or None when not measured (spec 14.3)."
    )
