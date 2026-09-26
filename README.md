# TrailGuard -- Predictive Outdoor Safety Skill

TrailGuard teaches a general-purpose agent a professional hiking-safety reasoning
protocol: given a hiker's current route, movement, device and time context, it answers
what risk state the hiker will enter in the next 30 / 60 / 120 minutes, why, how much
safety margin is left, and what to do now.
The safety verdict is produced by a deterministic rule core, not by the model; the model
only reasons over tool outputs and explains them.

**Status: Phase 1 -- deterministic core.** The full factual chain runs end to end with
no model, no key, no network and no GPU: a real GPX route becomes a route context, a
route projection, a replay weather reading and a rule-driven risk verdict with named
drivers, physical-unit safety margins and an evidence trace. See
[`docs/architecture.md`](docs/architecture.md) and
[`docs/deterministic_core.md`](docs/deterministic_core.md).
Phase 2 (live weather + the horizon loop in the Skill), Phase 3+ (skill loading,
providers, DGX Spark benchmark, demo) are not started.

## Layout

```text
SKILL.md                       Skill v1: reasoning protocol (no code, no thresholds)
pyproject.toml                 project + pytest config
requirements.txt               pinned runtime deps
.env.example                   env var placeholders (no secrets in the repo)
docs/                          architecture.md, contracts.md, deterministic_core.md,
                               evaluation_protocol.md, phase0_final_acceptance.md

trailguard/
  schemas/                     frozen pydantic contracts (context/route/weather/risk/scenario/trace)
  routes/                      geodesic GPX geometry + route registry
  tools/                       the five atomic tools + ToolSpec registry
  risk_core/                    safety core: engine, features, movement, scoring,
                                confidence, calibration, real rule-pack loader
  rules/rules_v1.yaml          six rules R-A..R-F, all calibration-marked
  rules/calibration_v1.yaml    evaluation metadata, bands, weights (all calibration-marked)
  replay/                       device-feed + weather-snapshot readers
  smoke.py                      python -m trailguard.smoke: the chain, run and printed
  agent/                       harness, skill loader, trace collector
  providers/                   StepFun (remote) and local DGX Spark model transports
evals/                         metric vocabulary, runner, comparison, report
scenarios/                     synthetic scenario dataset (3 fixtures; 30-50 in Phase 3+)
replay/                        frozen GPX route, weather snapshot, device feeds
tests/                         Layer A safety-core + contract / fairness / packaging tests
                                (demo/ Streamlit UI is Phase 6 -- the directory does not exist yet)
```

## Tests

```bash
.venv/bin/python -m pytest -q          # 487 tests
.venv/bin/python -m trailguard.smoke   # the same chain, run once and printed
```

Run from the repository root. `pyproject.toml` sets `pythonpath = ["."]` and
`testpaths = ["tests"]`. The smoke command drives the chain for one scenario across
all four horizons and prints every stage; it asserts nothing, so read its output
rather than its exit code.

## Roadmap

Phase 0 specification -> **Phase 1 deterministic core (this state)** -> Phase 2 weather
+ horizon loop + confidence -> Phase 3 skill loading + providers -> Phase 4 DGX Spark
local model -> Phase 5 benchmark -> Phase 6 demo -> Phase 7 polish. Phase definitions
live in the frozen spec `TrailGuard_比赛版开发冻结规格_v2.1.md` (section 25).

All thresholds are competition demo-marked, not industry safety standards. Scenario
metrics describe behaviour on synthetic scenarios and must never be reported as
real-world accident rates.
