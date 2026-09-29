# TrailGuard Phase 6 — Development Log

Phase 6 turns the verified Phase 0–5 chain into a competition demo. This file is the
running log; `docs/phase6_final_report.md` is the acceptance artefact.

**Frozen at the start of Phase 6.** Phase 5 was committed and tagged before any demo
code was written, so the benchmark cannot be re-shaped by what the demo needs.

| Item | Value |
| --- | --- |
| Phase 5 freeze commit | `d9358e5` |
| Tag | `phase5-freeze` |
| Working tree at freeze | clean |
| Pushed | no — the repository has no remote |

## Phase 5 raw-evidence archive

Read-only archive of `runs/phase5/`, created before any demo development, so the
benchmark evidence survives a messy working tree. Not committed: `runs/` is gitignored
by the Phase 13 policy and the archive belongs with it.

| Field | Value |
| --- | --- |
| Archive path | `/home/asus_gx10/trailguard-phase5-raw-evidence.tar.gz` |
| Size | 189278 bytes |
| sha256 | `bdcb65618b49e8a28c9ed700bd29d0c49e05f0b8945d5bb6ca8b2fbd5114f57d` |
| Contents | 102 files — 87 raw run records, the four aggregates, `cause_evidence.json`, the forensic and resume notes, and the two run logs |
| Verification | `sha256sum -c /home/asus_gx10/trailguard-phase5-raw-evidence.tar.gz.sha256` |

## Phase 6 hard boundaries

Carried forward from the Phase 5 freeze and the Phase 6 brief; every one of these is
asserted by a test rather than trusted.

- `SKILL.md`, `rules_v1.yaml`, `calibration_v1.yaml`, Safety Core logic, the five tool
  contracts, the benchmark scenario set and the Phase 5 metric definitions are **not
  modified**. Nothing is tuned to make the demo look better.
- `docs/phase5_evidence/` is read-only for the demo: it is loaded, never recomputed,
  rewritten, re-normalised or re-rounded.
- No silent fallback. If the local Qwen endpoint or live weather is unavailable the UI
  says so; it never swaps in a scripted provider or a replay snapshot.
- No composite score in the UI.
- The demo reads Phase 5's **MIXED** finding and states the cost side explicitly.

## Demo architecture

Three layers, one direction of dependency, 18 Python modules and 4389 lines:

| Layer | Modules | Lines | Depends on |
|---|---|---|---|
| `demo/__init__.py` + `demo/presenters/` | 11 | 2538 | `pydantic`, `yaml`, `trailguard` — **never** `streamlit` |
| `demo/streamlit/` | 7 | 1851 | `streamlit`, `demo.presenters` |
| gate + operator tools | `scripts/demo_smoke.py`, `scripts/demo_preflight.py`, `scripts/start_demo.sh` | 933 | both |

`app.py` resolves the repository from its own location and puts it on `sys.path` before
anything else, so the demo runs from any working directory. Exactly three files import
`streamlit` and all three are inside `demo/streamlit/`: `app.py`, `components/tabs.py` and
`components/panels.py`. `charts.py`, `text.py` and the whole presenter package do not,
which is what lets the replay chain, the timeline, the evidence view and the benchmark
numbers run — and be tested — on a machine with nothing but `pydantic` and `yaml`.

The split is the brief's section 49: a presenter turns a `trailguard` object into a view
model, and the UI only renders. `run_replay` calls the five tools in the Skill's order and
copies the engine's own verdict into `HorizonRow.risk_level` — the field's comment is
"the demo never recomputes it". `worst_level` on `ReplayRun` ranks by the core's own
`RiskLevel` enum so the display order cannot drift from the core's. `charts.py` draws
offline SVG: no chart library, no tile server, no network call.

## Demo modes

Two weather modes and one agent switch, all explicit, none automatic.

| Control | Label on screen | Default | Behaviour |
|---|---|---|---|
| weather mode | `Replay — labelled default, frozen snapshot` | yes | reads the frozen snapshot committed with Phase 5; its age and the freshness budget are both shown |
| weather mode | `Live weather — needs the network` | no | asks a real forecast service; **not cached**, so a stale answer is never served |
| who reads the five tools (inside Risk Timeline) | deterministic core, or the local Qwen agent | deterministic | the agent is never run on page load; it needs a click |

Replay is the explicit, labelled default, and the scenario banner under the title states
which mode is in force and what it means ("replayed from the frozen snapshot, and labelled
as replay" / "fetched live, and labelled as live"). Replay needs no network and no model,
which is what makes it the competition-safe base.

## Scenario selection

14 scenarios are committed with frozen Phase 5 references. The picker offers three, plus
the control, plus a collapsible full list — not 14 radio buttons.

| Group | Scenarios | What each isolates |
|---|---|---|
| Featured | `B05_late_gust_hazard` (G/G/G/Y) | the level from the soft-score band alone: R-C fires only at +120 |
| Featured | `B04_tight_sunset_margin` (R/R/R/R) | the level from a hard-rule floor alone: soft score **0**, RED, because R-B is CRITICAL |
| Featured | `B10_compound_soft_pair` (R/R/R/R) | the level from compound escalation alone: R-D + R-E → R-F |
| Control | `S01_clear_day` (G/G/G/G) | nothing fires; the demo does not cry wolf |
| Advanced (10) | incl. `S04_sunset_margin_pace_drop` | one case per rule family and per edge |

`S04` is the stress case Phase 4 and 5 used and is deliberately **not** on the live path;
it is reachable under "show all 14 scenarios" and its costs are discussed on the Benchmark
tab. Every label, level, rule id and "clears" flag is read from the frozen reference at
load time, so a catalogue label cannot drift from what the benchmark measured. The only
hand-written column is the editorial one — what the case proves.

## UI structure

Five top-level tabs and a sidebar. Five is the ceiling; the sidebar holds the two choices a
judge should be able to challenge.

| Tab | The question it answers |
|---|---|
| **Overview** | What is this, and what is the answer? |
| **Risk Timeline** | Where does that answer come from, and what happens next? |
| **Evidence** | Why should I believe it? |
| **Benchmark** | What was measured, including what went the wrong way? |
| **Architecture** | How is it built, and what was frozen to build it? |

Agent Mode is not a sixth tab. It is a radio inside Risk Timeline — *who reads the five
tools* — because that contrast is the answer to that tab's question, and because it is the
only part of the demo that can fail and therefore sits one click below the core value.

Overview carries the one-sentence positioning ("it does not report the weather outside, it
reports where the hiker will be when the risk arrives"), the verdict badge, the four-horizon
strip, and the five-step journey through the tools. Risk Timeline carries the horizon rows,
the driver table, the margins and the agent switch. Evidence carries the six artifact hashes -- the two Safety Core files and the four
inputs -- and the provenance line and the per-horizon tool chain — one row per horizon, each a value a
tool returned — with the frozen Phase 5 reference for the scenario behind a "View raw JSON"
disclosure rather than dumped on the screen. Benchmark carries the frozen Phase 5
numbers. Architecture carries the frozen-asset table and the DGX Spark status.

## Failure policy

There is no bare `except` in `app.py`. Every handler names its own reason, renders a box
with a title, a body, a "what this means" line and the one command that fixes it, and
anything unexpected propagates to the Streamlit console — which the operator sees and a
judge never does. `st.exception` is never called.

| Failure | What the screen says | What it never does |
|---|---|---|
| live weather refused | `Live weather unavailable` + the provider's own reason, and an offer to switch back | answer the request from the replay snapshot |
| local model down | `Local Agent unavailable` + the reason, and the fix line | substitute a scripted provider or a canned answer |
| model run failed | `The agent run failed` + `ExceptionType: message` | edit, truncate or rewrite the model's answer |
| a tab raised | `The <tab> tab could not be drawn` | take the other tabs or the Overview verdict with it |
| the catalogue is unreadable | `The scenario catalogue could not be loaded` | show a half-empty page as if it were the demo |

The screen never shows an exception object, a host address, an absolute path or a
credential. The agent's own answer is the only untrusted string on screen, and it is drawn
inside a code fence it cannot close.

## Benchmark presentation policy

- The Benchmark tab reads only the four committed files under `docs/phase5_evidence/`, and
  writes nothing. It computes no new statistic: every number on screen is a value one of
  those files already contains, at the precision it already carries. Rounding is not
  applied here.
- Both conditions are shown beside every metric, with that metric's own direction
  ("higher is better" / "lower is better"), so a delta is never read as an improvement
  without the reader being told which way better points.
- The finding is stated as **MIXED** with the counts on screen: 4 metrics moved the right
  way, 4 moved the wrong way, 10 did not move. The chart draws the mean paired delta per
  metric, in the direction each metric calls better, with the four regressions at full
  length. Both sides are on the same screen on purpose.
- No composite score, no net score, no "overall". The evidence's own
  `no_composite_score: true` is quoted, not overridden, and the tab says the 18 metrics are
  never summed, averaged or ranked against each other.
- There is no "run the benchmark again" button. The headline is written as the honest
  sentence, not as a win: TrailGuard changes the agent's orchestration behaviour and
  increases proactive future-risk investigation in several dimensions, and on the current
  local 30B model and 16K context window the longer orchestration chain also introduces
  substantial reliability and context-budget costs.

## Live demo sequence

The path that was walked end to end against the real local model, in order. Roughly three
minutes; the deterministic part needs no network and no model.

1. **Preflight.** `.venv/bin/python scripts/demo_preflight.py` → `Verdict: RUNNABLE`,
   0 missing, 1 degraded (`streamlit on PATH` — the operator uses `.venv/bin/streamlit`),
   24 ready, including `qwen3-vl-30b-a3b` at the local endpoint with `max_model_len=16384`.
2. **Overview.** The one-sentence positioning, the verdict badge, the four-horizon strip.
   Default scenario `B05_late_gust_hazard`: `Safe now, YELLOW by +120min. This is the case
   a current-location forecast would miss.`
3. **The future-weather story.** NOW GREEN / +30 GREEN / +60 GREEN, then +120 YELLOW with
   score 40 and R-C firing on a 50 km/h gust at the projected position (46.12142,
   7.79720), 2.236 km from the end. The weather was asked for at the future position, not
   here.
4. **Route projection.** The SVG shows the current point, the three projected points and
   the destination, so the judge can see the weather query moving along the route.
5. **Evidence.** The six artifact hashes -- the two Safety Core files and the four inputs -- the provenance line, and the per-horizon tool
   chain — one row per horizon, each a value a tool returned.
6. **Run the local agent** (Risk Timeline → the agent radio → the button). Real vLLM,
   frozen sampling (temperature 0.0, max_tokens 2048, max_steps 16), the same
   `scenario_registry` and the same `run_agent` call Phase 5 used. The panel shows the tool
   calls it actually made, the wall clock, and the answer verbatim.
7. **Switch to `B04_tight_sunset_margin`.** Soft score 0, verdict RED at every horizon,
   R-B CRITICAL — `Sunset Margin: time_margin_min=14.1121 lt 30 (time_margin_min_warn, band
   warn)` — the core's own driver sentence. The score is not the verdict.
8. **Benchmark.** MIXED, with the completion-rate drop (20 of 32 baseline runs completed,
   2 of 32 with the skill) and the context-limit regression on the same screen as the four
   metrics that improved.
9. **Failure handling.** Switch the weather mode to Live and let it fail: the box names
   weather and says it will not silently substitute the snapshot. Then point the demo at a
   dead endpoint: the agent panel says `Local Agent unavailable` and nothing else on the
   screen changes.
