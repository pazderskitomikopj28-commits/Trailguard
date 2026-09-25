# TrailGuard -- Predictive Outdoor Safety Skill

TrailGuard teaches a general-purpose agent a professional hiking-safety reasoning
protocol: given a hiker's current route, movement, device and time context, it answers
what risk state the hiker will enter in the next 30 / 60 / 120 minutes, why, how much
safety margin is left, and what to do now.
The safety verdict is produced by a deterministic rule core, not by the model; the model
only reasons over tool outputs and explains them.

**Status: Phase 0 -- specification and foundation.** Contracts, schemas, the frozen
YAML rule pack (plus its loader), the agent harness loop, `SKILL.md` v1 and the
evaluation protocol exist. No business logic runs yet: every tool body and the risk
engine raise `NotImplementedError` naming the phase that owns them. See
[`docs/architecture.md`](docs/architecture.md).

## Layout

```text
SKILL.md                       Skill v1: reasoning protocol (no code, no thresholds)
pyproject.toml                 project + pytest config
requirements.txt               pinned runtime deps
.env.example                   env var placeholders (no secrets in the repo)
docs/                          architecture.md, contracts.md, evaluation_protocol.md

trailguard/
  schemas/                     frozen pydantic contracts (context/route/weather/risk/scenario/trace)
  tools/                       the five atomic tools + ToolSpec registry
  risk_core/                   safety core interfaces + real rule-pack loader
  rules/rules_v1.yaml          six rules R-A..R-F, all calibration-marked
  agent/                       harness, skill loader, trace collector
  providers/                   StepFun (remote) and local DGX Spark model transports
evals/                         metric vocabulary, runner, comparison, report
scenarios/                     synthetic scenario dataset (3 fixtures in Phase 0; 30-50 in Phase 3+)
replay/                        frozen routes / weather snapshots (replay/timelines/ reserved, not created)
tests/                         Layer A safety-core tests
                                (demo/ Streamlit UI is Phase 6 -- the directory does not exist yet)
```

## Tests

```bash
.venv/bin/python -m pytest -q
```

Run from the repository root. `pyproject.toml` sets `pythonpath = ["."]` and
`testpaths = ["tests"]`.

## Roadmap

Phase 0 specification (this state) -> Phase 1 deterministic core (route context,
projection, ETA, sunset margin, rules) -> Phase 2 weather + horizon loop + confidence
-> Phase 3 skill loading + providers -> Phase 4 DGX Spark local model -> Phase 5
benchmark -> Phase 6 demo -> Phase 7 polish. Phase definitions live in the frozen spec
`TrailGuard_比赛版开发冻结规格_v2.1.md` (section 25).

All thresholds are competition demo-marked, not industry safety standards. Scenario
metrics describe behaviour on synthetic scenarios and must never be reported as
real-world accident rates.
