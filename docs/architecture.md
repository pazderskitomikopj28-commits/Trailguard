# TrailGuard Architecture

**Status: Phase 0 (specification and foundation).** Every module described here is a
contract, a stub or a loader; no route math, weather call or risk scoring is executed
yet. This document explains how the layers fit together, which file owns what, and
what is deliberately left to later phases.

Spec references below point to the single source of truth:
`TrailGuard_比赛版开发冻结规格_v2.1.md`.

---

## 1. What TrailGuard is

An outdoor agent usually answers "is it safe right now?". TrailGuard answers the
forward-looking question instead:

> If the hiker keeps moving at the currently observable movement trend along the
> current route, what risk state will they enter in the next 30 / 60 / 120 minutes,
> why, how much safety margin is left, and what should they do now?

The answer is never a single number. It is a level, a timeline, named drivers,
physical-unit safety margins, a confidence figure and a recommended action -- all of
them traceable to tool outputs and to a deterministic safety core.

## 2. The three pillars (spec 1)

| Pillar | Carried by | One-line role |
| --- | --- | --- |
| **1. TrailGuard Skill** | `SKILL.md` (loaded by `trailguard/agent/skill_loader.py`) | Hands a general-purpose agent the professional reasoning protocol: when to trigger, what context to collect, how many horizons to analyse, how to handle missing or stale data, how to explain. |
| **2. Deterministic Safety Core** | `trailguard/risk_core/` + `trailguard/rules/rules_v1.yaml` (invoked through `trailguard/tools/risk_assessment.py`) | Produces the safety verdict: hard rules, soft score, level, drivers, margins, confidence. No language model involved. |
| **3. DGX Spark Safety Benchmark** | `evals/` + `scenarios/` + `replay/` + `demo/` (Phase 5/6) | Measures whether the skill actually improves tool use, future-risk reasoning, safety-constraint compliance and explanation reliability, and records the run on real local hardware. |

The claim the project must prove: **under the same model, the same tools and the same
input, loading the TrailGuard Skill produces a measurable improvement** -- and the
benchmark is designed so that "no improvement" is also an acceptable, recordable
outcome (spec 3.2).

## 3. The core chain (spec 2)

```text
Current State            context snapshot (spec 9)
  + Future Route Position  project_route_state
  + ETA                     derived inside the safety core
  + Future Weather          get_weather_for_route_time @ projected position/time
  + User Trend              movement windows from the device feed
  + Device / Comms State    battery, network, last contact
        |
        v
Projected Safety State      (a state, never an accident probability)
        |
        v
NOW / +30min / +60min / +120min risk      one assess_risk per horizon
        |
        v
Safety Margins              minutes, percent battery, meters, freshness, confidence
        |
        v
Recommended Action          plain-language action + uncertainty
```

Route-aware weather is the load-bearing move: hazard features are read at the
*projected* position and *projected* arrival time (rule R-C), not at the current
location.

## 4. Component map

```text
dgx-spark-hackathon/
├── SKILL.md                          Skill v1: reasoning protocol, no code (spec 10)
├── pyproject.toml / requirements.txt / .env.example
├── docs/                             architecture, contracts, evaluation protocol
│
├── trailguard/
│   ├── schemas/                      frozen contracts; the ONE shared context
│   │   ├── context.py                TrailGuardContext, Observed, GeoPoint, DeviceState,
│   │   │                              RouteContext, TimeState, WeatherPolicy, DataQualityReport
│   │   ├── route.py                  Horizon, MovementTrend, RouteProjection
│   │   ├── weather.py                WeatherForRouteTime + freshness_status()
│   │   ├── risk.py                   RiskLevel, Severity, RuleDriver, SafetyMargins,
│   │   │                              RiskResult, HorizonRiskAssessment
│   │   ├── scenario.py               ToolName, ForbiddenBehavior, Scenario, TimelinePoint
│   │   └── trace.py                  ToolCallRecord, HorizonCheckRecord, EvidenceTrace
│   │                                  (+ secret redaction)
│   ├── tools/                        the five atomic tools (spec 4, 5)
│   │   ├── __init__.py               TOOL_SPECS / TOOL_FUNCTIONS / tool_schema_dicts()
│   │   ├── route_context.py          get_route_context          (Phase 1)
│   │   ├── route_projection.py       project_route_state         (Phase 1)
│   │   ├── weather_route_time.py     get_weather_for_route_time (Phase 2)
│   │   ├── user_device.py            get_user_device_context     (Phase 1)
│   │   └── risk_assessment.py        assess_risk                 (Phase 1)
│   ├── risk_core/                    deterministic safety core (spec 7, 8)
│   │   ├── hard_rules.py             RuleDefinition, RulePack, load_rule_pack() -- REAL loader
│   │   ├── engine.py                 RiskEngine interface + fixed pipeline order
│   │   ├── scoring.py                aggregate_soft_score() + demo-calibration bands
│   │   └── confidence.py             compute_confidence()
│   ├── rules/
│   │   └── rules_v1.yaml             six rules R-A..R-F, all calibration-marked
│   ├── agent/                        TrailGuard's own runtime (spec 15)
│   │   ├── harness.py                run_agent(): system prompt -> tool loop -> trace
│   │   ├── skill_loader.py           load_skill_md(): raw text, never parsed
│   │   └── trace.py                  TraceCollector -> EvidenceTrace
│   └── providers/                    model transport (spec 16)
│       ├── base.py                   ChatMessage, ToolCallRequest, ModelResponse, ModelProvider
│       ├── stepfun.py                StepFunProvider (Step 5 Preview)   (Phase 3)
│       └── local.py                  LocalModelProvider (DGX Spark)     (Phase 4)
│
├── evals/                            benchmark layer (schema only in Phase 0)
│   ├── metrics.py                    frozen metric names + MetricResult
│   ├── runner.py                     run_scenario() -> EvalRunResult     (Phase 5)
│   ├── compare.py                    compare_runs() + fairness preconditions
│   └── report.py                     BenchmarkReport, DgxSparkEvidence
│
├── scenarios/                        synthetic scenario dataset (spec 13)
│   ├── baseline/ edge_cases/ compound/
├── replay/                           frozen GPX + weather snapshot (spec 17, 24)
│   ├── routes/                       scn_route_sample.gpx
│   └── weather/                      scn_weather_snapshot.json
│       (spec 24 also reserves replay/timelines/; it is NOT created, because the v1
│        movement/device timelines live inline in each scenario YAML -- see
│        replay/README.md section 1)
├── demo/                             Streamlit demo -- NOT created yet (Phase 6)
├── tests/                            Layer A safety-core + contract / fairness / packaging tests
└── app/                              legacy empty scaffolding -- untouched, not imported
```

## 5. How the layers relate

**Skill <-> Harness.** `SKILL.md` is prose for the model. The harness
(`trailguard/agent/harness.py`) never reads it semantically: `load_skill_md()` returns
raw text which is appended to the system prompt when `skill_enabled=True`, and omitted
entirely when `skill_enabled=False`. That single difference is the whole experiment
(spec 3.1) -- the harness cannot interpret the skill, so it cannot accidentally give
the baseline a bit of the treatment.

**Everything else is frozen, not just "similar".** A baseline run and a treatment run
receive: the same `tools` registry and therefore the same `tool_schema_dicts()`, the
same `temperature` / `max_tokens` / `max_steps`, the same `task` text, the same
`context` snapshot (byte-identical JSON) and the same
`HARNESS_BASE_SYSTEM_PROMPT`. That base prompt is *deliberately neutral* runtime
mechanics -- how the loop feeds results back, that a value no tool returned must not be
invented -- and carries none of the treatment vocabulary (no horizon words, no freshness
budget, no missing-data rule, no projected-risk-state wording). Writing any of those
here would hand the baseline half of the treatment, so
`tests/test_harness.py::TREATMENT_VOCABULARY` asserts their absence and
`test_the_baseline_prompt_is_exactly_the_shared_runtime_instructions` asserts the
baseline prompt is the base text with nothing else added.

**Harness <-> Tools.** The harness advertises `tool_schema_dicts()` (five entries, in
registry order), executes whatever the model asks for inside `try/except`, and records
the outcome (`ok` / `error` / `not_implemented`) in the evidence trace. It does not
retry, repair or substitute calls, and it does not decide which tools a task required.

**Tools <-> Safety Core.** Four tools deliver facts (`route context`, `projection`,
`weather`, `device state`); the fifth, `assess_risk`, delegates to the safety core in
`trailguard/risk_core/`, whose rule pack is loaded from `rules/rules_v1.yaml` by
`load_rule_pack()`. The engine pipeline order is fixed: feature extraction -> hard
rules set a severity floor -> soft score -> level mapping -> R-F compound escalation ->
confidence.

**Core <-> Schemas.** Everything crossing a boundary is a pydantic v2 contract with
`extra="forbid"`, so a malformed payload fails at the boundary instead of leaking into
a verdict. `TrailGuardContext` is the ONE snapshot shared by agent, tools and
benchmark (spec 9), and every observation is wrapped in `Observed[...]` so freshness,
provenance and confidence are always available.

**Harness <-> Providers.** `ModelProvider` is pure transport (Step 5 Preview in
Phase 3, a local 4B-8B model on DGX Spark in Phase 4). One harness drives both, which
is what keeps the `2 models x 2 skill conditions` matrix fair (spec 14.2).

**Everything <-> Trace.** `TraceCollector` is the only writer of `EvidenceTrace`
(spec 18): tool calls with redacted arguments, horizons checked, freshness checks,
triggered rules, drivers, final level, confidence, missing data, uncertainty flag,
sampling params. The benchmark computes numbers from the trace; the demo displays it.

**Replay <-> Benchmark <-> Demo.** Replay Mode (spec 17) fixes the GPX route, the
weather snapshot and the movement/device timelines inside a `Scenario`, so a run is
reproducible and a no-skill / with-skill pair is a controlled comparison. The demo
(Phase 6) replays the same scenarios and shows the same trace the benchmark scored.

## 6. Phase 0 vs later phases

Phase 0 ships contracts, the YAML rule pack (with its real loader), the harness loop,
the skill text, the metric vocabulary and these docs. Nothing else.

| Area | Phase 0 (done) | Later |
| --- | --- | --- |
| Schemas | all contract modules, frozen field sets | additive changes only, spec amendment first |
| Tools | signatures, input/output models, `ToolSpec` boundaries, stubs raising `NotImplementedError` | Phase 1 bodies for route/projection/device/assess; Phase 2 for weather |
| Safety core | `RuleDefinition` / `RulePack` validation, real YAML loader, engine interface, band vocabulary | Phase 1 rule evaluation, projection, ETA, sunset margin; Phase 2 confidence |
| Rule pack | `rules_v1.yaml` authored, six rules, every threshold calibration-marked | re-tuning is data-only; no new rules in the MVP |
| Agent harness | `run_agent()` loop, fairness contract, trace collection | Phase 3 providers wired; Phase 5 semantic trace fields |
| Skill | `SKILL.md` v1 protocol text | revisions stay protocol-level, never implementation-level |
| Evals | metric names, `MetricResult`, `EvalRunResult`, `ComparisonReport`, fairness preconditions | Phase 5 aggregation, DGX Spark evidence |
| Replay / scenarios | `Scenario` schema | scenario dataset (13), replay assets (17) |
| Demo | -- | Phase 6 Streamlit |

Forbidden in Phase 0 by spec 26: UI polish, wearables, mobile, databases, dozens of
rules, trained models, multi-agent designs. Phase 0 and Phase 1 must be stable first.

## 7. Boundaries worth restating

* The Skill is not responsible for GPX math, the weather API, risk formulas or
  threshold execution (spec 1, pillar 1).
* No tool answers the whole 30/60/120 question in one call; `forecast_risk` does not
  exist and must not be added (spec 4.2).
* The domain concept is a **Projected Risk State**; the banned accident-probability
  identifier is never used as a code identifier, field, attribute or function name
  in the safety core or the skill (it survives only as the test guard
  ``BANNED_IDENTIFIER`` and the ``CLAIM_ACCIDENT_PROBABILITY`` forbidden-behavior
  enum member), and no metric may be reported as a real-world accident rate
  (spec 6.2, 12).
* Every rule threshold is `competition_demo_calibration` with `calibration: true` and
  `review_status: pending` -- never an industry safety standard (spec 7).
* Nothing in this repository executes business logic during Phase 0, and the legacy
  `app/` scaffolding is neither imported nor modified.
