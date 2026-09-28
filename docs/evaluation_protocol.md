# Evaluation Protocol

Source of truth: spec sections 3, 11, 12, 13, 14. Metric vocabulary is frozen in
`evals/metrics.py`; the runner interface is `evals/runner.py`; the fairness
preconditions live in `evals/compare.py`.

Phase 0 defines the protocol and the metric names. Running the benchmark is Phase 5.

---

## 1. Two layers, and no circular proof (spec 11.1)

**Layer A -- Safety Core tests** (`tests/`, deterministic pytest)

Tests the *implementation*: rule engine, route math, route projection, ETA, time
margin, risk aggregation, confidence behaviour. These are ordinary unit and
integration tests with fixed inputs and fixed expected outputs -- no language model is
involved. Expected outcomes are pinned in the scenario dataset
(`Scenario.expected_safety_core`), so a rule cannot be "correct" merely because it
agrees with itself.

**Layer B -- Agent Skill Evaluation** (`evals/runner.py`, model-driven)

Tests the *skill*, i.e. whether a language model, when taught the TrailGuard protocol,
actually behaves differently:

* did it call the right tools?
* did it gather the full context?
* did it check freshness?
* did it analyse all required future horizons?
* did it invent (hallucinate) data?
* did it override the safety core?
* did it explain the evidence faithfully?
* did it disclose uncertainty?

Layer B never re-tests Layer A, and Layer A's passing never substitutes for a Layer B
result. That separation is what keeps the benchmark from being a circular argument.

## 2. Frozen metric names (spec 12)

### Agent behaviour (`AgentMetricName`)

| Metric | Definition |
| --- | --- |
| `TOOL_CALL_COMPLETENESS` | Fraction of tools the scenario requires that the agent actually called. |
| `REQUIRED_HORIZON_COVERAGE` | Fraction of required horizons the agent actually analysed (recorded in the trace's horizon checks). |
| `WRONG_TOOL_RATE` | Fraction of tool calls that are outside the frozen five-tool registry, or that misuse a registered tool. |
| `MISSING_DATA_COMPLIANCE` | Fraction of runs that, facing missing key inputs, listed them, lowered confidence or returned `insufficient_data` instead of guessing. |
| `FRESHNESS_COMPLIANCE` | Fraction of runs that marked freshness, lowered confidence accordingly and disclosed it. |
| `SAFETY_CORE_OVERRIDE_RATE` | Fraction of runs whose claimed risk level differs from the deterministic core's level without an explicit, disclosed justification. |
| `HALLUCINATED_FACT_RATE` | Fraction of runs containing a fact (weather value, route fact, device state, number) absent from tool outputs and the context snapshot. |

### Explanation (`ExplanationMetricName`)

| Metric | Definition |
| --- | --- |
| `EXPLANATION_GROUNDING` | Fraction of explanation claims traceable to an actual tool output or context field. |
| `DRIVER_CONSISTENCY` | Fraction of reported drivers that match the drivers the deterministic core reported. |
| `UNCERTAINTY_DISCLOSURE` | Fraction of runs that explicitly disclosed uncertainty, missing data or staleness. |

### Scenario-level safety behaviour (`ScenarioMetricName`)

| Metric | Definition |
| --- | --- |
| `SCENARIO_HAZARD_MISS_RATE` | Fraction of scenarios whose designed hazard was never surfaced in the agent's answer. |
| `SCENARIO_FALSE_ALARM_RATE` | Fraction of scenarios in which the agent reported a hazard the scenario does not contain. |
| `PROJECTED_RISK_DETECTION_RATE` | Fraction of scenarios where the hazard the scenario places at a *future* horizon was detected at that horizon. |
| `LEAD_TIME` | Minutes by which the agent flagged the projected risk before the hazard onset, when the scenario defines an onset. |

Every metric is reported as a `MetricResult(name, value, unit, n, details)` -- a value
plus its unit plus a sample size -- so no aggregate can travel without its denominator.

## 3. Fairness constraints (spec 3.1, 3.2)

The comparison is `models x {without skill, with skill}` (spec 14.2). Between the two
skill conditions the following are identical:

* the same model (and the same provider configuration);
* the same user task text;
* the same context snapshot (spec 9);
* the same tool set and the same tool schemas (`tool_schema_dicts()`);
* the same sampling parameters (`temperature`, `max_tokens`);
* the same maximum number of steps (`max_steps`);
* the same external data snapshot (frozen GPX and weather snapshot in replay mode).

The **only** difference is whether the `SKILL.md` text is present in the system prompt.
`evals/compare.py` checks these preconditions (same scenario-id set, same model, same
tool set, same sampling params) and raises `ValueError` naming the violation before it
aggregates anything, so a broken comparison cannot silently produce numbers.

**Weakening the baseline is forbidden.** The baseline is a normal general-purpose agent:
no anti-skill instructions, no "only look at current weather" hint, no reduced tool set,
no shorter step budget, no different temperature. A baseline written to lose is not a
baseline.

**Results are recorded either way.** If the skill helps, that is recorded. If some
scenarios show no change, that is recorded. If some scenarios regress, that is
recorded. Removing failed treatment samples, re-running until the numbers look better,
or hand-editing benchmark figures is forbidden (spec 3.2).

## 4. Naming prohibition

Scenario-level results describe behaviour on **synthetic** scenarios. In particular:

* **Scenario Hazard Miss Rate must never be presented as a real-world accident miss
  rate, and no other metric may be presented as a real-world accident, injury or
  fatality rate.** The wording "accident rate", "injury rate" or "missed accidents" is
  prohibited in reports, README, demo and slides.
* The permitted wording is the frozen name itself: *Scenario Hazard Miss Rate*, always
  labelled as measured on the synthetic scenario dataset.

This is a direct consequence of the risk disclaimer (spec 27): TrailGuard is a
prototype safety *aid*, not a medical device, rescue system, official warning system or
accident-probability predictor, and all thresholds in the rule pack are
`competition_demo_calibration` values pending review -- never industry safety
standards.

## 5. Reproducibility

* **Replay Mode** (spec 17) fixes the GPX route, the weather snapshot and the
  movement/device timelines per scenario, so runs do not depend on live weather or API
  jitter. `WeatherPolicy.mode` records `replay` vs `live`; `live` is an add-on, never
  the default for the benchmark.
* Every run produces an `EvidenceTrace` (spec 18): tool calls with redacted arguments,
  horizons checked, freshness checks, weather source and freshness, triggered rules,
  drivers, final level, confidence, missing data, uncertainty disclosure and sampling
  parameters. Metric results must be computable from the trace plus the scenario
  expectations -- not from a reviewer's memory of the run.
* DGX Spark runtime evidence (`DgxSparkEvidence` in `evals/report.py`: GPU model,
  average GPU utilisation, tokens/s, local inference latency, benchmark runtime,
  scenario count) is recorded for local runs and must be measured, not claimed
  (spec 14.3).

## 5.1 Where the Phase 5 paired benchmark actually lives

This document is the protocol for the *protocol*. The paired skill benchmark that
implements it -- the paired run set, the 14 scenarios, the 18 metrics, the
preflight gate and the final verdict -- is a separate, self-contained Phase 5
artefact:

* **`docs/phase5_benchmark_protocol.md`** -- the frozen benchmark protocol: the
  research question, what is held constant, the single variable, the metric
  definitions with numerators and denominators, the pre-registered run order and
  repetitions, and the rule that no composite score may be computed.
* **`evals/phase5/`** -- the implementation. `manifest.py` *builds*
  `benchmark_manifest.json` from the assets on disk rather than trusting a written
  claim; `references.py` generates a Deterministic Reference per scenario by running
  the frozen Safety Core; `evaluate.py` scores every preserved record
  deterministically; `preflight.py` is the gate that must return
  `PREFLIGHT PASS` before a scored run.
* **`docs/phase5_final_report.md`** -- the result, with the Benchmark Integrity
  Verdict and the Skill Effectiveness Finding reported separately.
* **`runs/phase5/`** -- the raw evidence. Note that `runs/` is gitignored by a
  Phase 13 policy, so the preserved run records live on disk only and are **not**
  in git; human-readable copies of the four aggregates are in
  `docs/phase5_evidence/`.

The manual-review rule, restated because it is the one most easily got wrong:
manual review resolves ambiguity only. **The original trace is never edited**, and a
reviewer's decision is recorded *beside* the automatic flag, never over it.

## 6. Scenario dataset expectations (spec 13)

Each scenario pins its `expected_required_tools`, `expected_required_horizons`,
`forbidden_behaviors` and (where applicable) `expected_safety_core`, and carries a
`simulation_note` plus `is_synthetic=True` so no synthetic input can be mistaken for a
real incident record. The v1 dataset targets 30-50 high-quality scenarios (baseline,
edge cases, compound) before any expansion -- that is the Phase 3/5 authoring target,
not the current state: ``scenarios/`` ships exactly three fixtures in Phase 0, so
every dataset-level number in Phase 5 must be quoted with its N.
