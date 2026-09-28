# TrailGuard -- Predictive Outdoor Safety Skill

TrailGuard teaches a general-purpose agent a professional hiking-safety reasoning
protocol: given a hiker's current route, movement, device and time context, it answers
what risk state the hiker will enter in the next 30 / 60 / 120 minutes, why, how much
safety margin is left, and what to do now.
The safety verdict is produced by a deterministic rule core, not by the model; the model
only reasons over tool outputs and explains them.

**Status: Phase 4 -- DGX Spark local model integration** (complete, pending acceptance).
The full factual chain runs end to end with no model, no key and no GPU: a real GPX route
becomes a route context, a route projection, a weather reading *for the projected position
at the projected arrival time* -- from a frozen snapshot or from the live Open-Meteo API
-- and a rule-driven risk verdict with named drivers, physical-unit safety margins and an
evidence trace. On top of that, one harness lets a real model call the five atomic tools and
records what it did; the baseline and the treatment run through that same `run_agent()`,
differing only by whether `SKILL.md` is injected, so the question "does the skill change
tool orchestration?" has an instrument that can answer it. In Phase 4 that instrument was
pointed at the second model of the frozen matrix: a local
`Qwen/Qwen3-VL-30B-A3B-Instruct` served by vLLM on DGX Spark (`max_model_len=16384`,
`--tool-call-parser hermes`), reached through the identical harness with a driver-level
`--provider local` knob and no branch in the agent path. Three real runs were recorded
(see [`docs/phase4_final_report.md`](docs/phase4_final_report.md)); the local model
produced structured tool calls and completed an S01 pair end to end, and it failed in three
specific, unresolved ways on the compound scenario (F-1, F-2, F-3) that Phase 5 must design
around rather than hide.
See [`docs/architecture.md`](docs/architecture.md),
[`docs/deterministic_core.md`](docs/deterministic_core.md),
[`docs/route_aware_weather.md`](docs/route_aware_weather.md) and
[`docs/agent_runtime.md`](docs/agent_runtime.md).
Phase 5 (fair benchmark) and Phase 6 (demo) are not started.

The safety verdict is still never computed by a language model, and the chain from GPX
to verdict still runs with `STEPFUN_API_KEY` unset.

## Layout

```text
SKILL.md                       Skill v1: reasoning protocol (no code, no thresholds)
pyproject.toml                 project + pytest config
requirements.txt               pinned runtime deps
.env.example                   env var placeholders (no secrets in the repo)
docs/                          architecture, contracts, deterministic_core,
                               route_aware_weather, evaluation_protocol, agent_runtime,
                               phase0/phase1/phase2/phase3/phase4 final reports

trailguard/
  schemas/                     frozen pydantic contracts (context/route/weather/risk/scenario/trace)
  routes/                      geodesic GPX geometry + route registry
  tools/                       the five atomic tools + ToolSpec registry
  risk_core/                    safety core: engine, features, movement, scoring,
                                confidence, calibration, real rule-pack loader
  rules/rules_v1.yaml          six rules R-A..R-F, all calibration-marked
  rules/calibration_v1.yaml    evaluation metadata, bands, weights (all calibration-marked)
  weather/                      weather acquisition (spec 17): provider ABC, THE one
                                normaliser, replay provider, live Open-Meteo provider
  replay/                       device-feed + weather-snapshot readers
  smoke.py                      python -m trailguard.smoke: the chain, run and printed
  weather_smoke.py              python -m trailguard.weather_smoke: route-aware weather
                                over four horizons, printed (Phase 2 demo harness)
  agent/                       harness, replay driver, skill loader, trace collector
  agent_smoke.py                python -m trailguard.agent_smoke: the runtime, both
                                conditions, no key and no network (Phase 3)
  providers/                   StepFun (remote, real) and local DGX Spark model transport
                                (both wired: local is Qwen3-VL-30B-A3B over vLLM, OpenAI-
                                compatible, tool-call-parser hermes)
evals/                         metric vocabulary, runner, comparison, report
scenarios/                     synthetic scenario dataset
replay/                        frozen GPX route, weather snapshots, device feeds
runs/phase3/                   live evidence traces land here -- GIT-IGNORED, never
                               committed; the directory appears with the first live run
runs/phase4/                   Phase 4 local-model smoke traces and the capability probe
                               report -- GIT-IGNORED for the same reason
scripts/                       test_live_weather.py, test_stepfun_agent.py,
                               test_local_agent.py, test_tool_calling.py -- the only paths
                               that may open a socket; never collected by pytest (spec 13, 14)
tests/                         Layer A safety-core + contract / fairness / packaging /
                                weather-provider / agent-runtime / local-provider /
                                local-agent-driver tests
                                (demo/ Streamlit UI is Phase 6 -- the directory does not exist yet)
```

## Tests

```bash
.venv/bin/python -m pytest -q                       # 1145 tests, all offline
.venv/bin/python -m pytest -v                       # the same, one line per test
.venv/bin/python -m trailguard.smoke                # the chain, run once and printed
.venv/bin/python -m trailguard.weather_smoke         # route-aware weather over NOW/+30/+60/+120
.venv/bin/python -m trailguard.agent_smoke           # the agent runtime, both conditions, offline
```

Run from the repository root. `pyproject.toml` sets `pythonpath = ["."]` and
`testpaths = ["tests"]`. The smoke commands drive the chain for one scenario across
all four horizons and print every stage. `weather_smoke` asserts nothing, so read
its output rather than its exit code; `agent_smoke` runs 20 named checks and
returns a non-zero exit when one of them fails, so its exit code is meaningful —
but its output is what tells you *which* check failed.

`agent_smoke` is the one command that exercises the whole runtime -- the real registry,
the real frozen contracts, the real `run_agent()`, the real evidence trace -- with no
credential and no network. It drives both conditions with a scripted stub, so it proves
the runtime works and proves nothing about whether the skill helps; it says so on every
line it prints.

`python -m pytest` makes **no external HTTP call**: the live providers are exercised
through `httpx.MockTransport` and an autouse fixture closes the socket layer, so the
guarantee is structural. The only paths that may open a socket are
`scripts/test_live_weather.py`, `scripts/test_stepfun_agent.py`,
`scripts/test_local_agent.py` and `scripts/test_tool_calling.py`, none of which pytest
collects and which report `LIVE WEATHER NETWORK UNAVAILABLE` / `NOT RUN -- credential
unavailable` / `NOT RUN -- local endpoint unavailable` rather than faking success.
Live weather is optional: it needs `pip install -e ".[weather]"` (`httpx`), and replay
Mode needs nothing beyond the base install. The local-model smoke reaches
`http://127.0.0.1:8000` and needs the DGX Spark server running; the live agent smoke
additionally needs `STEPFUN_API_KEY` in the environment (never in a file the command
reads) and writes its traces to the git-ignored `runs/phase3/` / `runs/phase4/`.

## Roadmap

Phase 0 specification -> Phase 1 deterministic core -> Phase 2 route-aware weather ->
Phase 3 skill runtime & tool orchestration -> **Phase 4 DGX Spark local model (this state,
pending acceptance)** -> Phase 5 benchmark -> Phase 6 demo -> Phase 7 polish. Phase
definitions live in the frozen spec `TrailGuard_比赛版开发冻结规格_v2.1.md` (section 25).

Phase 3 is complete as an *instrument*, not as a result: it makes both conditions run
and records what happened, but it does not aggregate, score or claim that the skill
wins -- that is Phase 5's job, and "no improvement" remains an acceptable, recordable
outcome (spec 3.2).

Phase 4 wired the second model of the frozen matrix and ran it three times. It is complete
as a *measurement*, not as a result either: the local model emitted real structured tool
calls and finished an S01 pair end to end, and it failed on the compound scenario in three
ways that are recorded unresolved (F-1 malformed JSON when batching `assess_risk`, F-2 the
frozen 16384-token window, F-3 therefore no local evidence on hard-rule respect). None of
the three was repaired, worked around or tuned away. `docs/phase4_final_report.md` is the
record; `runs/phase4/` holds the raw traces it cites, git-ignored, never committed.

All thresholds are competition demo-marked, not industry safety standards. Scenario
metrics describe behaviour on synthetic scenarios and must never be reported as
real-world accident rates.
