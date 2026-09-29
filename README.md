# TrailGuard — Predictive Outdoor Safety Skill

**TrailGuard is a Predictive Outdoor Safety Skill.** It teaches a general-purpose
agent a forward-looking hiking-safety reasoning protocol: given a hiker's route,
movement, device and time context, it answers what risk state the hiker will
enter in the next 30 / 60 / 120 minutes, why, how much safety margin is left,
and what to do now.

The safety verdict is produced by a deterministic rule core, not by the model.

**LLM does not calculate the risk level. The deterministic Safety Core does.**
The model reasons over tool outputs and explains them; it never scores, never
thresholds, and never overrides a hard rule.

## Why it is different

A current-location forecast answers a question nobody on a trail asks. By the
time a gust front arrives where the hiker is standing, the hiker has already
moved. TrailGuard asks the other question — **where will the hiker be when the
risk arrives?** — and then queries the weather for that position, at that
arrival time, on every frozen horizon. The verdict is a statement about a
future state the hiker is travelling into, not about the ground under their
boots.

Three consequences that the rest of this repository is built to prove:

- **The verdict is auditable.** Every level carries named drivers, physical-unit
  safety margins and an evidence trace, and a hard rule can floor a risk level
  independently of the soft score. The demo shows a scenario whose soft score is
  **0** and whose verdict is **RED** at every horizon, because one hard rule is
  critical.
- **The skill is measured, not asserted.** Phase 5 ran 87 recorded agent runs
  (43 with the skill, 44 without) against 18 frozen metrics and reported the
  result honestly: **MIXED**. Four metrics moved the right way, four moved the
  wrong way, ten did not move. There is no composite score, by the evidence's
  own `no_composite_score: true` flag, which the demo quotes rather than
  overrides.
- **The failures are shown.** The Benchmark tab puts the completion-rate drop on
  the same screen as the metrics that improved, and the demo's Failure policy
  section names what the screen says when the network or the local model is
  unavailable — it never silently substitutes a nicer answer.

## How it works

```text
                     ┌─ GPX route ──────────────┐
                     │   movement / device feed │   context
                     └──────────┬───────────────┘
                                ▼
            ┌───────────────────────────────────────────────┐
   step 1   │ get_route_context                            │
            │ project_route_position   → future route      │
            │   + future arrival time    position & time   │
            │ get_weather_at           → future weather    │
            │   (position + time from the projection)       │
            │ get_movement_state       → movement / device │
            └───────────────────┬───────────────────────────┘
                                │  five tool outputs, one per horizon
                                ▼
   ┌────────────────────────────────────────────────────────────┐
   │  DETERMINISTIC SAFETY CORE                                 │
   │  rules_v1.yaml + calibration_v1.yaml                       │
   │  features → soft score → bands → hard-rule floors          │
   └────────────────────────┬───────────────────────────────────┘
                            ▼
                 PROJECTED RISK STATE
                 level + drivers + margins + confidence
                            ▼
              AGENT EXPLANATION  (Qwen3-VL-30B-A3B, optional)
              explains the verdict; cannot create or change it
```

The five atomic tools run in the Skill's order, once per horizon. The core then
runs once per horizon over their outputs. Nothing in that chain is optional or
sampled, and nothing in it is a language-model judgement.

**Where the Skill is:** [`SKILL.md`](SKILL.md) — the reasoning protocol itself
(what to collect, in what order, and what to explain), with no code and no
thresholds. The thresholds and rules live in
[`trailguard/rules/rules_v1.yaml`](trailguard/rules/rules_v1.yaml) and
[`trailguard/rules/calibration_v1.yaml`](trailguard/rules/calibration_v1.yaml),
both calibration-marked. The Skill is injected into the agent at run time; the
agent path is otherwise identical between the two benchmark conditions.

## Run the demo

One command, from the repository root:

```bash
./scripts/start_demo.sh
```

That runs the preflight first — so the one command that starts the demo is also
the one that says which part of it will not work — then starts the UI on
`http://localhost:<free port>` bound to localhost only. If you prefer to start
it yourself:

```bash
.venv/bin/streamlit run demo/streamlit/app.py
```

Install the demo extra first if `streamlit` is not already present:
`pip install -e ".[demo]"`. Everything except the screen itself — the replay
chain, the timeline, the evidence view and the benchmark numbers — works with
`pydantic` and `PyYAML` alone.

The demo has two weather modes and one agent switch. All three are explicit
choices on screen; none is automatic.

| Mode | Label on screen | Needs |
|---|---|---|
| **Replay Mode** (default) | `Replay — labelled default, frozen snapshot` | **No network required. No LLM required.** Reads the frozen weather snapshot committed with Phase 5, and shows its age and the freshness budget. |
| **Live weather** | `Live weather — needs the network` | The Open-Meteo API. Not cached, so a stale answer is never served; if it fails, the screen says so and never answers from the snapshot. |
| **Local Agent Mode** | the agent radio inside *Risk Timeline* | **Qwen3-VL-30B-A3B** (BF16, `max_model_len=16384`) at `http://127.0.0.1:8000/v1`, OpenAI-compatible. Off by default and needs a click. If it is down the panel says `Local Agent unavailable` and nothing else on the screen changes. |

Five top-level tabs: **Overview**, **Risk Timeline**, **Evidence**,
**Benchmark**, **Architecture**. Agent Mode is not a sixth tab — it is a switch
inside Risk Timeline, because it is the only part that can fail.

The competition walkthrough is written out step by step — nine steps, roughly
three minutes, of which the deterministic part needs no network and no model —
in [`docs/phase6_final_report.md` §18](docs/phase6_final_report.md) and in
[`docs/phase6_demo.md`](docs/phase6_demo.md), and was walked end to end against
the real local model: default scenario `B05_late_gust_hazard` is safe NOW, +30
and +60, then YELLOW at +120 when the future weather is asked for at the
projected position 2.2 km from the end — the case a current-location forecast
would miss. Switch to `B04_tight_sunset_margin` and the soft score is **0** while
the verdict is **RED** at every horizon, because R-B is a CRITICAL hard rule.

## Run the tests

```bash
.venv/bin/python -m pytest -q          # 1242 tests, all offline
.venv/bin/python scripts/demo_smoke.py  # 1876 checks: the demo, rendered headlessly
.venv/bin/python scripts/demo_preflight.py   # what the demo needs, and whether it is there
```

Run from the repository root; `pyproject.toml` sets `pythonpath = ["."]` and
`testpaths = ["tests"]`. `pytest` makes **no external HTTP call**: the live
providers are exercised through `httpx.MockTransport` and an autouse fixture
closes the socket layer, so the guarantee is structural rather than incidental.

`scripts/demo_smoke.py` is deliberately a plain script and *not* part of the
pytest suite — that same autouse fixture forbids the two reads the gate must
make. It renders the UI through Streamlit's `AppTest` and compares fields across
all 14 scenarios × 4 horizons, plus the evidence and benchmark screens.

The only paths that may open a socket are the `scripts/test_*` live probes, none
of which pytest collects. They report `NOT RUN — credential unavailable` rather
than faking success. Traces land in the git-ignored `runs/`.

**Where the evidence is:** [`docs/phase5_evidence/`](docs/phase5_evidence/) —
the four frozen files (`metrics.json`, `paired_results.csv`,
`manual_review.csv`, `summary.csv`) that every benchmark number on screen is
read from, and which the demo never recomputes, re-normalises or re-rounds. The
87 raw run records are git-ignored by policy and archived read-only at
`/home/asus_gx10/trailguard-phase5-raw-evidence.tar.gz` (sha256
`bdcb65618b49e8a28c9ed700bd29d0c49e05f0b8945d5bb6ca8b2fbd5114f57d`); the demo
does not need them. The acceptance record is
[`docs/phase6_final_report.md`](docs/phase6_final_report.md).

## What TrailGuard is, and what it is not

Required declaration (spec §27), stated up front rather than buried.

TrailGuard **is** an outdoor-safety **decision-support prototype**.

TrailGuard is **not**:

- a medical device;
- a rescue system;
- an official warning system;
- an accident-probability predictor.

Everything simulated — user state, every demo threshold, and every benchmark
scenario — is labelled as such throughout the repo, the demo and the evidence.

## Status

Phases 0–6 complete and frozen (`phase0-freeze` … `phase6-freeze`).

| Phase | What it established | Record |
|---|---|---|
| 0–1 | schemas, deterministic Safety Core, GPX geometry | [`docs/phase1_final_report.md`](docs/phase1_final_report.md) |
| 2 | route-aware weather: the projected position at the projected time | [`docs/phase2_final_report.md`](docs/phase2_final_report.md) |
| 3 | Skill runtime and tool orchestration, both conditions, offline | [`docs/phase3_final_report.md`](docs/phase3_final_report.md) |
| 4 | DGX Spark local model, wired through the identical harness | [`docs/phase4_final_report.md`](docs/phase4_final_report.md) |
| 5 | fair benchmark: 87 runs, 18 metrics, result **MIXED** | [`docs/phase5_final_report.md`](docs/phase5_final_report.md) |
| 6 | competition demo over the frozen chain, all gates green | [`docs/phase6_final_report.md`](docs/phase6_final_report.md) |

## Known limitations

Stated plainly, because a demo that hides them is not a demonstration.

- **Local 30B, long tool chains hit context pressure.** Skill Effectiveness came
  out **MIXED**, not a win: on the current local model and 16K context window the
  longer orchestration chain the Skill induces also brings substantial
  reliability and context-budget costs. Completion fell from 20 of 32 scored
  baseline runs to 2 of 32 with the skill.
- **The skill's transfer across models is unmeasured.** Spec §14.2 asks for a
  two-models × two-conditions matrix: the DGX Spark local model × without/with
  skill, *and* the Step 5 Preview model × without/with skill. **Only the local
  half ran.** The 87 recorded runs are all against the local
  `qwen3-vl-30b-a3b`; the remote arm is recorded `NOT RUN` in
  `evals/phase5/benchmark_manifest.json` and in
  [`docs/phase5_final_report.md`](docs/phase5_final_report.md), and it was never
  paired with the primary. So "the skill changes model behaviour" is shown for
  one model on one endpoint, not demonstrated to be model-independent. (This is
  why the result is reported as MIXED rather than as a win.)
- **Competition thresholds are demo calibration.** Every threshold is
  calibration-marked and set for competition demonstration. They are not
  industry safety standards, and no scenario metric here may be read as a
  real-world accident rate.
- **Scenarios are synthetic and the default weather mode is replay.** That is
  deliberate: it makes the demonstration deterministic and reproducible without a
  network or a model. The walkthrough reproduces a frozen measured result rather
  than sampling a live one. All 14 scenarios are frozen to a 2026-05-18 replay
  instant, so **Live weather cannot succeed for any of them today** — the
  forecast API answers HTTP 400 ("`start_date` is out of allowed range") for a
  date ~4 months in the past. Live mode is therefore the *capability*
  demonstration and the *failure* demonstration, never a working data path for
  the frozen scenarios.
- **Benchmark Integrity: PASS; Skill Effectiveness: MIXED.** Both halves are the
  headline, and neither is dropped.

## Repository layout

```text
SKILL.md                       Skill v1: the reasoning protocol (no code, no thresholds)
pyproject.toml                 project + pytest config
requirements.txt               pinned runtime deps
.env.example                   env var placeholders (no secrets in the repo)

trailguard/
  schemas/                     frozen pydantic contracts (context/route/weather/risk/scenario/trace)
  routes/                      geodesic GPX geometry + route registry
  tools/                       the five atomic tools + ToolSpec registry
  risk_core/                   Safety Core: engine, features, movement, scoring,
                               confidence, calibration, real rule-pack loader
  rules/rules_v1.yaml          six rules R-A..R-F, all calibration-marked
  rules/calibration_v1.yaml    evaluation metadata, bands, weights
  weather/                     weather acquisition: provider ABC, the one normaliser,
                               replay provider, live Open-Meteo provider
  replay/                      device-feed + weather-snapshot readers
  agent/                       harness, replay driver, skill loader, trace collector
  providers/                   StepFun (remote) and local DGX Spark model transport
evals/                         metric vocabulary, runner, comparison, report
scenarios/                     the 14 synthetic scenarios
replay/                        frozen GPX route, weather snapshots, device feeds
demo/
  presenters/                  view models; depends on trailguard, never on streamlit
  streamlit/                   the five-tab UI (the only files that import streamlit)
scripts/                       start_demo.sh, demo_preflight.py, demo_smoke.py
docs/                          contracts, phase reports, and docs/phase5_evidence/
runs/                          live traces — GIT-IGNORED, never committed
tests/                         1242 offline tests (Layer A core/contract + demo layer)
```
