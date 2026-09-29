"""The Benchmark tab: Phase 5's numbers, read-only and unimproved.

Spec 7, 25, 46. The finding Phase 5 actually reported was MIXED -- the skill moved
some metrics the right way and others the wrong way -- and that finding is the
demo's credibility. So this module deliberately does the least interesting thing
possible:

* it reads the four committed files under ``docs/phase5_evidence/`` and nothing
  else, and writes nothing at all;
* it computes **no** new statistic. Every number on screen is a value one of those
  files already contains, at the precision it already carries;
* there is no composite score, no net score and no "overall" -- the evidence's own
  ``no_composite_score: true`` is quoted, not overridden;
* every metric is shown with both conditions beside it and its own direction, so a
  delta is never read as an improvement without the reader being told which way
  "better" points for that metric.

Rounding is not applied here. The values pass through as the files wrote them, and
the tab says which file each came from so a judge can open it.
"""

from __future__ import annotations

import csv
import json
from dataclasses import dataclass, field
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
EVIDENCE_DIR = REPO_ROOT / "docs" / "phase5_evidence"

METRICS_FILE = EVIDENCE_DIR / "metrics.json"
SUMMARY_FILE = EVIDENCE_DIR / "summary.csv"
PAIRED_FILE = EVIDENCE_DIR / "paired_results.csv"
REVIEW_FILE = EVIDENCE_DIR / "manual_review.csv"

#: Shown verbatim. The frozen Phase 5 position, and the reason this tab exists.
NO_COMPOSITE_SCORE = True

DIRECTION_TEXT = {
    "higher_is_better": "higher is better",
    "lower_is_better": "lower is better",
}


@dataclass(slots=True)
class MetricRow:
    """One of the 18 frozen metrics, as the evidence recorded it."""

    metric_id: str
    numerator: str
    denominator: str
    direction: str
    direction_text: str
    mean_paired_delta: float | None
    sum_baseline: float | None
    sum_treatment: float | None
    n_non_null_baseline: int | None
    n_non_null_treatment: int | None
    n_paired_deltas: int | None
    n_runs_baseline: int | None
    n_runs_treatment: int | None
    manual_review: bool
    definition: str

    @property
    def has_delta(self) -> bool:
        return self.mean_paired_delta is not None

    @property
    def is_run_rate(self) -> bool:
        """True for a metric whose denominator is the condition's run count.

        The evidence's own wording, quoted rather than pattern-matched, because it is
        the line between a rate and a sum: M1's denominator is "runs attempted in the
        condition" and its sum counts runs, while M5's denominator is "the four frozen
        horizons the reference declares" and its sum counts horizons. Dividing the
        second by 32 would produce a number that means nothing.
        """
        return self.denominator.startswith("runs attempted in the condition")

    def _rate(self, total: float | None, runs: int | None) -> str | None:
        if total is None or not runs:
            return None
        return f"{total / runs * 100:.4g}%"

    @property
    def baseline_rate(self) -> str | None:
        """The baseline sum as a percentage of the baseline runs, or None."""
        return self._rate(self.sum_baseline, self.n_runs_baseline) if self.is_run_rate else None

    @property
    def treatment_rate(self) -> str | None:
        """The treatment sum as a percentage of the treatment runs, or None."""
        return self._rate(self.sum_treatment, self.n_runs_treatment) if self.is_run_rate else None

    @property
    def rate_text(self) -> str | None:
        """``62.5% -> 6.25%`` for a run-rate metric, and nothing for the rest.

        Brief 28 names the completion rate by name, in percentages. This is display
        arithmetic on four numbers already in this row and already on the screen
        beside it -- not a statistic this module derives -- and it cannot drift from
        the evidence because every input is the evidence's own. Phase 5's report
        writes the same figure the same way ("20 of 32 scored runs (62.5%)"), so the
        demo quotes an accepted presentation rather than inventing one.
        """
        baseline, treatment = self.baseline_rate, self.treatment_rate
        if baseline is None or treatment is None:
            return None
        return f"{baseline} -> {treatment}"



@dataclass(slots=True)
class Exclusion:
    run_id: str
    reason: str


@dataclass(slots=True)
class BenchmarkView:
    """Everything the Benchmark tab needs, copied from the evidence files."""

    generated_at: str
    no_composite_score: bool
    runs_total: int
    runs_scored: int
    runs_excluded: int
    metrics: list[MetricRow]
    exclusions: list[Exclusion]
    paired_rows: int
    manual_review_rows: int
    source_files: dict[str, str]
    trace_notes: list[str] = field(default_factory=list)

    @property
    def completion_rate(self) -> MetricRow | None:
        """The one metric brief 28 names by name, or None when it is absent."""
        for row in self.metrics:
            if row.metric_id == "M1_completion_rate":
                return row
        return None

    @property
    def improved(self) -> list[MetricRow]:
        """Metrics whose paired delta points the way the metric's own direction says.

        No arithmetic across metrics: this is a filter, and the point of showing it
        at all is that the *other* list exists on the same screen.
        """
        rows = []
        for row in self.metrics:
            if row.mean_paired_delta is None:
                continue
            if row.direction == "higher_is_better" and row.mean_paired_delta > 0:
                rows.append(row)
            elif row.direction == "lower_is_better" and row.mean_paired_delta < 0:
                rows.append(row)
        return rows

    @property
    def worsened(self) -> list[MetricRow]:
        """Metrics whose paired delta points against the metric's own direction."""
        rows = []
        for row in self.metrics:
            if row.mean_paired_delta is None:
                continue
            if row.direction == "higher_is_better" and row.mean_paired_delta < 0:
                rows.append(row)
            elif row.direction == "lower_is_better" and row.mean_paired_delta > 0:
                rows.append(row)
        return rows

    @property
    def unchanged(self) -> list[MetricRow]:
        """Metrics whose paired delta was exactly zero, or that have no pairs."""
        return [row for row in self.metrics if row.mean_paired_delta in (None, 0)]


def _sha256(path: Path) -> str | None:
    import hashlib

    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        return None


def available() -> bool:
    """True when the frozen evidence is present, so the tab can say why not."""
    return METRICS_FILE.is_file() and SUMMARY_FILE.is_file()


def _count_rows(path: Path) -> int:
    try:
        with path.open(newline="", encoding="utf-8") as handle:
            return sum(1 for _ in csv.DictReader(handle))
    except OSError:
        return 0


def load_benchmark(evidence_dir: Path | None = None) -> BenchmarkView:
    """Read the frozen Phase 5 evidence. Raises ``FileNotFoundError`` when absent.

    ``evidence_dir`` is a parameter so a test can point at a copy; the default is
    the committed directory, which this function only ever opens for reading.
    """
    directory = evidence_dir if evidence_dir is not None else EVIDENCE_DIR
    metrics_path = directory / METRICS_FILE.name
    if not metrics_path.is_file():
        raise FileNotFoundError(
            f"Phase 5 evidence is not present at {metrics_path}; the Benchmark tab "
            "cannot show numbers that were never committed."
        )
    payload = json.loads(metrics_path.read_text(encoding="utf-8"))

    rows: list[MetricRow] = []
    for metric_id in sorted(payload.get("metrics", {})):
        entry = payload["metrics"][metric_id]
        direction = entry.get("direction", "")
        rows.append(
            MetricRow(
                metric_id=metric_id,
                numerator=entry.get("numerator", ""),
                denominator=entry.get("denominator", ""),
                direction=direction,
                direction_text=DIRECTION_TEXT.get(direction, direction),
                mean_paired_delta=entry.get("mean_paired_delta"),
                sum_baseline=entry.get("sum_baseline"),
                sum_treatment=entry.get("sum_treatment"),
                n_non_null_baseline=entry.get("n_non_null_baseline"),
                n_non_null_treatment=entry.get("n_non_null_treatment"),
                n_paired_deltas=entry.get("n_paired_deltas"),
                n_runs_baseline=entry.get("n_runs_baseline"),
                n_runs_treatment=entry.get("n_runs_treatment"),
                manual_review=bool(entry.get("manual_review")),
                definition=entry.get("definition", ""),
            )
        )

    exclusions = [
        Exclusion(run_id=entry.get("run_id", ""), reason=entry.get("reason", ""))
        for entry in payload.get("exclusions", [])
    ]

    return BenchmarkView(
        generated_at=payload.get("generated_at", ""),
        no_composite_score=bool(payload.get("no_composite_score", NO_COMPOSITE_SCORE)),
        runs_total=int(payload.get("runs_total", 0)),
        runs_scored=int(payload.get("runs_scored", 0)),
        runs_excluded=int(payload.get("runs_excluded", 0)),
        metrics=rows,
        exclusions=exclusions,
        paired_rows=_count_rows(directory / PAIRED_FILE.name),
        manual_review_rows=_count_rows(directory / REVIEW_FILE.name),
        source_files={
            name: (_sha256(directory / name) or "absent")
            for name in (SUMMARY_FILE.name, PAIRED_FILE.name, REVIEW_FILE.name, METRICS_FILE.name)
        },
        trace_notes=[
            "Read from docs/phase5_evidence/ as committed. Nothing recomputed, "
            "re-normalised, re-rounded or re-derived.",
            "Deltas are treatment minus baseline per pair, exactly as the evidence "
            "wrote them; the metric's own direction decides whether that is good.",
            f"no_composite_score = {payload.get('no_composite_score', NO_COMPOSITE_SCORE)}: "
            "the 18 metrics are never summed, averaged or ranked against each other.",
        ],
    )


def per_scenario_table(evidence_dir: Path | None = None) -> list[dict[str, str]]:
    """One row per preserved run, straight out of ``summary.csv``.

    Included so a judge who asks "which run was that?" gets an answer from the
    record rather than from memory.
    """
    directory = evidence_dir if evidence_dir is not None else EVIDENCE_DIR
    path = directory / SUMMARY_FILE.name
    if not path.is_file():
        return []
    with path.open(newline="", encoding="utf-8") as handle:
        return [dict(row) for row in csv.DictReader(handle)]
