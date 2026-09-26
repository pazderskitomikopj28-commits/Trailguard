# TrailGuard -- Predictive Outdoor Safety Skill

TrailGuard teaches a general-purpose agent a professional hiking-safety reasoning
protocol: given a hiker's current route, movement, device and time context, it answers
what risk state the hiker will enter in the next 30 / 60 / 120 minutes, why, how much
safety margin is left, and what to do now.
The safety verdict is produced by a deterministic rule core, not by the model; the model
only reasons over tool outputs and explains them.

**Status: Phase 2 -- route-aware weather.** The full factual chain runs end to end with
no model, no key and no GPU: a real GPX route becomes a route context, a route
projection, a weather reading *for the projected position at the projected arrival
time* -- from a frozen snapshot or from the live Open-Meteo API -- and a rule-driven
risk verdict with named drivers, physical-unit safety margins and an evidence trace.
See [`docs/architecture.md`](docs/architecture.md),
[`docs/deterministic_core.md`](docs/deterministic_core.md) and
[`docs/route_aware_weather.md`](docs/route_aware_weather.md).
Phase 3 (skill loading + providers), Phase 4 (DGX Spark local model), Phase 5
(benchmark) and Phase 6 (demo) are not started.

## Layout

```text
SKILL.md                       Skill v1: reasoning protocol (no code, no thresholds)
pyproject.toml                 project + pytest config
requirements.txt               pinned runtime deps
.env.example                   env var placeholders (no secrets in the repo)
docs/                          architecture, contracts, deterministic_core,
                               route_aware_weather, evaluation_protocol,
                               phase0/phase1/phase2 final reports

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
  agent/                       harness, skill loader, trace collector
  providers/                   StepFun (remote) and local DGX Spark model transports
evals/                         metric vocabulary, runner, comparison, report
scenarios/                     synthetic scenario dataset (3 fixtures; 30-50 in Phase 3+)
replay/                        frozen GPX route, weather snapshots, device feeds
scripts/                       test_live_weather.py -- the only path that may open a
                                socket; never collected by pytest (spec 13, 14)
tests/                         Layer A safety-core + contract / fairness / packaging /
                                weather-provider tests
                                (demo/ Streamlit UI is Phase 6 -- the directory does not exist yet)
```

## Tests

```bash
.venv/bin/python -m pytest -q              # 544 tests, all offline
.venv/bin/python -m trailguard.smoke       # the same chain, run once and printed
.venv/bin/python -m trailguard.weather_smoke   # route-aware weather over NOW/+30/+60/+120
```

Run from the repository root. `pyproject.toml` sets `pythonpath = ["."]` and
`testpaths = ["tests"]`. The smoke commands drive the chain for one scenario across
all four horizons and print every stage; they assert nothing, so read their output
rather than their exit code.

`python -m pytest` makes **no external HTTP call**: the live provider is exercised
through `httpx.MockTransport` and an autouse fixture closes the socket layer, so the
guarantee is structural. The only path that may open a socket is
`scripts/test_live_weather.py`, which pytest never collects and which reports
`LIVE WEATHER NETWORK UNAVAILABLE` rather than faking success. Live weather is
optional: it needs `pip install -e ".[weather]"` (`httpx`), and replay Mode needs
nothing beyond the base install.

## Roadmap

Phase 0 specification -> Phase 1 deterministic core -> **Phase 2 route-aware weather
(this state)** -> Phase 3 skill loading + providers -> Phase 4 DGX Spark local model ->
Phase 5 benchmark -> Phase 6 demo -> Phase 7 polish. Phase definitions
live in the frozen spec `TrailGuard_比赛版开发冻结规格_v2.1.md` (section 25).

All thresholds are competition demo-marked, not industry safety standards. Scenario
metrics describe behaviour on synthetic scenarios and must never be reported as
real-world accident rates.
