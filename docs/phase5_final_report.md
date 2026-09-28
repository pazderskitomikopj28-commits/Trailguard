# Phase 5 Final Report — Fair Skill Benchmark & Evidence Evaluation

**Question under test.** Under identical model, task, context, tool registry,
replay data, sampling parameters and agent harness, does loading `SKILL.md`
alone change the agent's *observable safety behaviour*?

**Repository.** `/home/asus_gx10/dgx-spark-hackathon`
**Result of this phase.** `16f45ec` … `c9b95d0` (HEAD). No remote; nothing pushed.
**Status.** Written for human acceptance. Per the brief, this report is **not**
committed and **not** pushed.

**Read this first.** Two verdicts are reported separately, as the protocol
requires, and they do not imply each other:

| Verdict | Value |
| --- | --- |
| **Benchmark Integrity Verdict** | **PASS** — the benchmark executed under the frozen paired protocol |
| **Skill Effectiveness Finding** | **MIXED** — measurable change in both directions, dominated by a large regression |

PASS does not mean the skill won. It means the measurement is trustworthy enough
to read. Section 19 says what the measurement says; section 18 says what it
cannot.

**Two defects in my own measurement, disclosed up front rather than buried in
section 21, because either could otherwise have changed a headline number and the
reader deserves to know they were found by me:**

1. **A positional pairing bug** in the aggregator — runs were paired by their
   position in a sorted list rather than by the `(scenario, repetition)` slot they
   occupied. Nine metrics were wrong, including the headline: M1 read −0.5625 over
   32 pairs when the truth is −0.4583 over 24. Found while building Appendix B,
   when two parts of the output disagreed about how many pairs existed (commit
   `ecd5e54`).
2. **An inverted superseding rule** — the record that corrects another was
   resolving "which record does this replace" by `run_id`, and the two records
   share that field, so the *replacement* was marked superseded and the original
   kept the pair slot. Found from `summary.csv` showing two rows with an identical
   `run_id`, while regenerating evidence for this report. It changed no number
   (commit `c9b95d0`); section 21.3 says why, and why that is a latent trap rather
   than a correction.

---

## 1. Executive Summary

Loading `SKILL.md` changed the agent's observable behaviour **decisively, and
mostly for the worse under this protocol.**

The single largest effect is not on any safety-core metric — it is on whether the
agent produced an answer at all:

* **Without the skill, 20 of 32 scored runs (62.5%) finished with a real answer.**
* **With the skill, 2 of 32 (6.3%) did.** Mean paired change **−0.4583** over the
  24 slots that have both conditions.

The mechanism is visible in the preserved traces and is not a detector artefact:
39 of the 64 scored runs ended with `stop_reason='completed'` but a final response
that is a **tool call emitted as text** (`<tool_call>\n{"name": "assess_risk", …`).
The harness only reads structured `response.tool_calls`, so such a response is
never executed and the run ends there. 12 of those 39 are baseline runs; 27 are
treatment runs. This is the Phase 4 F-1 family, seen end-on-end for the first
time.

The skill did produce genuine, measurable improvements — it is not a pure
regression:

* **M2 structured tool-call success**, +0.1958 mean paired delta. Section 13.1
  shows this advantage is almost entirely **one** pre-condition.
* **M5 horizon coverage**, Σ 2.75 → 5.50, +0.0833. The skill's four-horizon
  protocol is half as likely to be followed without it.
* **M7 hazard recall**, Σ 0.0 → 4.0, +0.2 over 20 pairs. The baseline reported
  **zero** hazards across the scenarios designed to contain them.

But the skill also made the treatment **worse where it matters most for a
safety aid**: M4 protocol completion 2 → 0, M16 context-limit failures 0 → 3.0,
and the collapse in M1 above.

**Nineteen of the eighteen-metric pair comparisons are too thin to read.** M6 has
3 pairs, M8 has 7, M14 has 1. The honest headline is not a composite score —
none was computed, by design — but a statement about four well-populated metrics
and silence about the rest.

**The measured finding is MIXED**, and specifically *not* "a wash": the largest
single effect is a large regression in answer production, and the largest
improvement (M2) reduces to one behaviour that is arguably compensation for a
documented mismatch in the frozen harness rather than general competence.

---

## 2. The Question

Verbatim, from the Phase 5 brief:

> 在相同模型、相同任务、相同 Context、相同 Tool Registry、相同 Replay Data、
> 相同 Sampling 参数和相同 Agent Harness 下，仅加载 `SKILL.md` 是否会改变
> Agent 的可观测安全行为？

> *Under the same model, the same task, the same context, the same tool registry,
> the same replay data, the same sampling parameters and the same agent harness,
> does loading `SKILL.md` alone change the agent's observable safety behaviour?*

**What would answer it.** A paired run set in which every one of those eight
things is pinned by a checkable fact (hash, field, config), the *only* difference
between the two conditions is the presence of the skill text in the system
prompt, both conditions are run under the same pre-registered order, every raw
result is preserved unedited, and every metric is computed deterministically from
the preserved trace — with no model asked to judge any run.

**What would not answer it.** Comparing against a weakened baseline; scoring an
infrastructure accident as a skill failure; repairing malformed model output so a
failed call looks like a success; aggregating after silently dropping runs;
letting an LLM judge decide whether an answer was "safe enough". Each of these
was ruled out in the frozen protocol and is re-checked by the preflight gate
(section 9).

---

## 3. What Was Held Constant

Every row was verified by `evals/phase5/preflight.py` at `fe11a68`, not asserted:

| Held constant | How it was pinned | Verified |
| --- | --- | --- |
| Model / checkpoint | `qwen3-vl-30b-a3b`, `GET /v1/models` returns exactly this id | ✅ |
| Endpoint | `http://127.0.0.1:8000/v1`, answers 200 | ✅ |
| `max_model_len` | server reports **16384**; never raised during the phase | ✅ |
| Task text | one scenario YAML per scenario, sha256 recorded in the manifest | ✅ 14/14 |
| Context snapshot | `scenario.initial_context`, byte-identical in both conditions | ✅ 14/14 |
| Tool registry | the frozen five, in order; tool schema hash `caed2924c6abd276…` | ✅ |
| Replay data | route GPX, weather snapshot, device feeds under `replay/` | ✅ |
| Sampling | `temperature` 0.0, `max_tokens` 2048 — harness defaults match the pre-registered values | ✅ |
| Agent harness | `trailguard/agent/harness.py`, unmodified for the whole phase | ✅ |
| Step budget | harness default 12, pre-registered budget 16 | ✅ |
| `tool_failure_policy` | harness default `report` | ✅ |
| `SKILL.md` | sha256 `98fd007eb23aee18f98bf67c295d7bad3c8e48f7a722022c852218fb0c649769` | ✅ |
| Rule pack | sha256 `b2630f3d32e50d65…` | ✅ |
| Calibration | sha256 `05bb20a16d7315c7…` | ✅ |
| Safety Core | Phase 4 state, untouched | ✅ |

`preflight` also asserts that **no** guided decoding, alternative parser or
modified chat template is configured, and that the provider code is unmodified —
the measured object must not be improved mid-benchmark.

**Nothing under `trailguard/` was modified during Phase 5.** Twelve protected
paths are committed and clean at `c9b95d0`.

---

## 4. The Single Variable

One variable: the presence of the `SKILL.md` text in the system prompt.

* **Without skill** — system prompt is `HARNESS_BASE_SYSTEM_PROMPT` alone.
* **With skill** — system prompt is `HARNESS_BASE_SYSTEM_PROMPT + "\n\n" + SKILL.md`.

The same byte-identical skill file was read from disk for every treatment run, and
its sha256 was re-verified after the batch. The five atomic tools — names,
descriptions, input schemas and order — are identical in both conditions, which
is what `tool_schema_dicts()` guarantees and the preflight asserts.

The baseline received **no** anti-skill instructions, no reduced tool set, no
shorter step budget and no different temperature. Weakening the baseline is
forbidden by the protocol and would have made the comparison meaningless.

---

## 5. Scenario Set

**14 scenarios**, selected by coverage rather than by convenience:

* 11 baseline scenarios (`scenarios/baseline/`) covering clear days, GPS drift,
  sub-threshold route deviation, tight sunset margins, late gust hazards, future
  gust fronts, pace drops, clearing gust bands, battery shortfall, compound soft
  pairs and multi-driver storms;
* 2 additional scenarios and 1 edge case (`S01_clear_day`,
  `S04_sunset_margin_pace_drop`, `S11_stale_gps_missing_data`) covering a clean
  control, a compound sunset+pace case, and stale/missing GPS data.

Each was chosen because it exercises a distinct branch of the Safety Core — a
scenario that fires the same rule pair as another was not added to pad the set.
The set was declared in `benchmark_manifest.json` **before** the first scored
model run, and the preflight re-verifies that the manifest's set is exactly the
set on disk (`14 scenarios in run order`).

**Not included, deliberately:** live-weather scenarios. Every scenario runs in
`replay` mode so a run cannot depend on whether open-meteo happened to be reachable.

---

## 6. Deterministic References

One reference per scenario, at `evals/phase5/references/<scenario>.json`,
generated by `evals/phase5/references.py` — **not** written by hand and not
judged.

Each reference records, per horizon: the risk level the deterministic Safety Core
produces, the hazards it fires, the drivers it names, the soft rules and the hard
rules that fire, whether a freshness check is required, and the evidence refs
proving it. The generator runs the frozen Safety Core over the frozen replay data
and serialises what came out. There is no field in which a human opinion could
hide.

The preflight re-hashes all 14 scenarios and all 14 references and reports each as
matching. A run is scored only against its own scenario's reference.

---

## 7. Metric Definitions

Eighteen metrics, frozen in `evals/phase5/metrics.py`. Every metric carries a
numerator, a denominator, a direction, one of three permitted sources
(trace / answer / raw record) and whether it requires manual review. Full frozen
definitions are in Appendix C. In brief:

| # | Metric | Reads from | Direction |
| --- | --- | --- | --- |
| M1 | completion rate | raw record + answer | higher |
| M2 | structured tool-call success | trace | higher |
| M3 | valid tool-call rate | trace | higher |
| M4 | protocol completion | trace | higher |
| M5 | horizon coverage | trace | higher |
| M6 | risk level exact match | answer + reference | higher |
| M7 | hazard recall | answer + reference | higher |
| M8 | hard rule recall | answer + reference | higher |
| M9 | risk driver recall | answer + reference | higher |
| M10 | missed hazard rate | answer + reference | lower |
| M11 | false alarm rate | answer + reference | lower |
| M12 | core contradiction rate | answer + core verdict | lower |
| M13 | hard rule softening | answer + reference | lower |
| M14 | unsupported probability claim | answer | lower |
| M15 | malformed tool-call rate | trace | lower |
| M16 | context-limit failure rate | raw record + forensic evidence | lower |
| M17 | max-step failure rate | raw record | lower |
| M18 | provider failure rate | raw record | lower |

Three constraints on the metrics themselves:

1. **No composite score.** `metrics.json` carries `"no_composite_score": true`.
   No weighted average, no "overall skill score", no index. Averaging a
   regression in answer production against an improvement in horizon coverage
   would hide whichever one the reader cared about.
2. **Every denominator travels with its numerator.** Any metric whose denominator
   is zero reports **`null`**, never `0.0` and never `1.0`.
3. **Answer-text metrics are declared lower bounds.** A regex can find `R-B` and
   cannot see a rule described in words, so M8/M9/M13 under-report. The report
   says so wherever they are quoted.

---

## 8. Run Order and Repetitions

* **Repetitions: 3**, pre-registered in the manifest before any scored run.
* **Run order: pre-registered balanced alternation**, not randomised — so a
  warm endpoint or a degrading server cannot preferentially benefit one
  condition.
* **Cell:** 14 scenarios × 3 repetitions × 2 conditions = **84 planned runs**.

Observed on disk: **87 records** — the 84 planned, plus 2 non-scored warm-up runs
(one per condition) and 1 replacement record (section 17). Every record is
preserved at `runs/phase5/raw/<run_id>.json`.

---

## 9. Execution Record

| Item | Value |
| --- | --- |
| Phase 4 baseline | `e3ca520fb5fcb6d94e65f23f9f564202f60e6116`, tag `phase4-freeze` |
| Benchmark protocol doc | `bdb6c87` |
| Design freeze | `e0fc196`, tag `phase5-design-freeze` |
| Batch execution | 84/84 runs completed and preserved |
| Preflight verdict | **PREFLIGHT PASS** — 37 checks, re-run at `fe11a68`, `ecd5e54` and `c9b95d0` |
| Aggregate commits | `16f45ec`, `f46cc1c`, `3c9142d`, `fb11041`, `fe11a68`, `ecd5e54`, `c9b95d0` |
| Final HEAD | `c9b95d0` |
| `git status` at report time | clean |
| Remote | none; nothing pushed |

**Thirteen infrastructure pauses** occurred during the batch, each an HTTP 400
from the local vLLM endpoint. Each is logged in `runs/phase5/pause_log.csv` with
its run id, step count, tool-call count and timestamp; the endpoint answered 200 on
health check after every one. Each pause was resumed, so no planned run was
abandoned and no run_id was ever overwritten to work around one. The cause is
investigated in section 18.1.

**Regression suite:** 1145 tests, all passing, at `c9b95d0`.

---

## 10. Warm-up

Four warm-up runs (two per condition, recorded `scored: false`) were executed
before the scored batch and are preserved. Their purpose was to find the
failures the design had not anticipated, and they did — which changed what two
metrics mean:

1. A run can end `stop_reason='completed'` with a final answer that is a **tool
   call emitted as text**. M1 was therefore defined to require that the answer be
   an answer.
2. Rule and driver ids are found by regex, so an answer that says "future weather
   hazard" in words scores as a miss. M8 and M9 are therefore **lower bounds**.

**The warm-up is not a result.** It is excluded from every aggregate; the
exclusion reason is stated in `metrics.json` for each warm-up record
(`recorded scored:false (the pre-batch warm-up); measured for information only,
and excluded from every aggregate`).

---

## 11. Benchmark Integrity Verdict

# **PASS — Benchmark executed under the frozen paired protocol**

The reasoning, and what each step rules out:

1. **The scenario set was frozen before the first scored model run.** The design
   freeze `e0fc196` predates the batch; all 87 records are on or after the
   manifest's own freeze timestamp, which the preflight checks record by record.
2. **Only the skill varied.** Section 3 is a list of verified pins, not claims.
3. **Every run is preserved unedited.** 87 records, append-only. No raw record
   was modified at any point in this phase, including during manual review.
4. **Malformed output was never repaired.** No client-side JSON repair, no regex
   extraction, no manual argument reconstruction exists anywhere in the
   evaluation path. A malformed batch is recorded as a structured tool-call
   failure and counted as one.
5. **Infrastructure accidents were excluded and never scored as skill
   failures.** 20 of them, 10 per condition (section 17).
6. **Repetitions and order were pre-registered**, not chosen after the fact.
7. **No model judged any run.** Manual review read the preserved records and
   traced numbers back to tool payloads.
8. **The aggregates re-run from the evidence without disturbing them.** Re-running
   `evaluate` after the superseding fix (section 21.3, item 13) left
   `paired_results.csv` and `manual_review.csv` byte-identical and every metric
   value unchanged — the premise behind "recomputable from the evidence" is not
   asserted here, it was exercised.

**This verdict is about the measurement, not about the skill.** It says the
numbers in sections 12–15 are what the protocol produced. It does not say the
skill works.

---

## 12. Results: Completion and Protocol

### M1 — completion rate — **the headline**

| | Baseline | Treatment |
| --- | --- | --- |
| Completed with a real answer | **20 / 32 = 0.625** | **2 / 32 = 0.0625** |
| `completed` but the answer is a tool call | 12 | 27 |
| `provider_error`, cause proven context-limit | 0 | 3 |
| Mean paired delta | **−0.4583** over 24 slots | |

**The mechanism, read from the records.** 39 of the 64 scored runs ended
`stop_reason='completed'` and a final response that begins:

```text
Now I'll run the risk assessment for the +30min horizon:

<tool_call>
{"name": "assess_risk", "arguments": {"route_state": …
```

The harness reads only structured `response.tool_calls`. A tool call written into
the content channel gets no execution, and the run ends. **I read these responses
directly rather than trusting the detector** — they genuinely end mid-invocation
with literal `<tool_call>` text and an unterminated JSON argument list. The
detector is symmetric: it also flags 12 of the baseline's 20 completed runs.

**What this is.** The Phase 4 F-1 family, observed end-on-end from the agent side
for the first time. Loading the skill makes this specific formatting failure
**more than twice as likely** (27/32 vs 12/32 among completed runs; 84% vs 38% of
all scored runs).

**What it is not.** It is not the baseline being better at safety. It is a
difference in whether the model produces a parsable answer. For a safety aid,
that is the binding constraint — an agent that emits a tool call instead of an
answer gives the hiker nothing. Section 19 returns to this.

### M2 — structured tool-call success

Σ **22.4365** (baseline) vs **28.2497** (treatment) across 32 scored runs each;
mean paired delta **+0.1958**. Section 12.1 decomposes it, because the
decomposition changes what it means.

#### 12.1 M2 decomposed — the advantage is one pre-condition

M2's treatment advantage is real but is **almost entirely one behaviour.** Computed
over exactly the 64 scored runs that M2's own denominators sum over:

| | scored runs | calls | ok | rate | failed for `analysis_time` | failed for `device_id` | rate excluding `analysis_time` calls |
| --- | --- | --- | --- | --- | --- | --- | --- |
| Baseline | 32 | 404 | 282 | 0.6980 | **75** | 47 | **0.8571** |
| Treatment | 32 | 334 | 296 | 0.8862 | **0** | 38 | **0.8862** |

Set the `analysis_time` calls aside and the gap falls from **+0.1882 to
+0.0291**. The skill's entire structured-tool-call advantage is that it teaches
the agent to pass a clock anchor the tool requires.

**Three readings, each of which must travel with any claim built on M2:**

* **It is a real skill effect.** `project_route_state`'s docstring says the anchor
  is "supplied by the caller that owns a clock", and `SKILL.md`'s Context Protocol
  step 3 tells the agent to check that the time state — *"analysis time and sunset
  time"* — exists and is real. Skill text never says "pass `analysis_time`", but it
  elevates it from an unnamed clock to a value the agent has verified exists. **The
  treatment supplied it on 98 of 98 `project_route_state` calls; the baseline on 96
  of 171.**
* **It is also compensation for a harness defect.** The tool's *schema* declares
  `analysis_time` **optional** ("Optional in the contract because spec 5.2 freezes
  exactly three fields") while the *implementation* requires it and raises
  otherwise. A model reading only the schema may legitimately omit it. Neither
  condition is wrong; the contract is internally inconsistent. This is frozen
  Phase 4 behaviour and was deliberately not fixed during Phase 5.
* **The `device_id` failures are a third thing, and neither condition was ever
  going to pass them.** The 85 failures across both conditions are the agents
  guessing an identifier that appears nowhere in the prompt: the baseline invented
  `device_123` (41 calls), `hiker_device_01` (3) and `device_001` (3); the treatment
  tried `device_123` (23), `scn_device_sample` (9), `B09_battery_shortfall` (4),
  `device_01` (1) and `device` (1). The correct feed is named
  `scn_device_<scenario_id>` — a convention stated in no tool description, no
  schema and no line of `SKILL.md`. The treatment guesses *better* (it reaches for
  prefix-shaped and scenario-shaped names) but still never finds it. So 47 of the
  baseline's failures and 38 of the treatment's were unwinnable before the first
  token was generated.

One metric, three different stories inside it. That is why section 19 says MIXED
rather than leaning on M2's headline.

### M3 — valid tool-call rate

**1.0 in both conditions, 32/32.** No run in either condition called a tool
outside the frozen registry. Both conditions stay inside the tool boundary.

### M4 — protocol completion

Baseline **2**, treatment **0**; mean paired delta **−0.0833**. The definition is
demanding — all five tools called at least once, all four horizons assessed, and a
freshness check on every horizon the reference marks as needing one. Both
conditions score near zero, so this metric separates the two conditions only
slightly and mostly by the treatment failing outright.

### M17 — max-step failures: **0 in both conditions.**
### M18 — provider failures: **0 in both conditions** (the 20 provider errors are
excluded, section 17).

---

## 13. Results: Safety Core Fidelity

These six metrics read the **answer text** against the scenario's Deterministic
Reference, and are therefore lower bounds (section 18.4): a run that describes a
rule in words rather than by its id is scored as a miss.

### M6 — risk level exact match

Σ **1.5** vs **0.0**; mean paired delta **−0.5** over only **3** pairs
(`n_non_null` 5 vs 16). **Three pairs is not a result.** It is reported because
suppressing it would be a choice, and it is the only metric where the baseline has
a numerator and the treatment has none.

### M7 — hazard recall

Σ **0.0** (baseline) vs **4.0** (treatment); mean paired delta **+0.2** over 20
pairs. **The baseline reported zero hazards across scenarios built to contain
them.** Whatever else the skill does, it surfaces hazards the base model does not.

### M8 — hard rule recall

**0.0 vs 0.0, over 7 pairs.** Sixteen flagged rows exist; manual review found all
sixteen sit on runs whose entire response is a tool-call invocation, so there is no
answer text in which a rule could be named — the same event as M1, not a second
finding (section 16). With a lower-bound regex and 7 pairs, this metric carries
essentially no information.

### M9 — risk driver recall

**0.0 vs 0.0**, over 20 pairs. Lower bound (regex).

### M12 — core contradiction rate

**0.0 vs 0.0**, 24 pairs. Neither condition published a level the deterministic
core contradicts. This is the metric that would matter most if it were non-zero —
an agent that reported GREEN over a RED verdict is the failure the Safety Core
exists to prevent — and it is zero in both conditions, on all 24 slots.

### M13 — hard rule softening

**0.0 vs 0.0**, 7 pairs. Neither condition softened a hard rule into a softer one.
Lower bound (regex), and only 7 pairs.

---

## 14. Results: Failure Trade-offs

### M10 — missed hazard rate

Σ **23.0 vs 23.0**; mean paired delta **−0.2** over 20 pairs. Identical totals,
different pair structure — the treatment's misses concentrate where it also failed
to answer.

### M11 — false alarm rate

**0.0 vs 0.0**, 24 pairs. Neither condition invented a hazard on a scenario that
fires nothing.

### M14 — unsupported probability claim rate

**0.0 vs 0.0**, after manual review overturned all **6** automatic baseline flags.
`metrics.json` reports both figures: `sum_baseline` 0.0 and
`automatic_sum_baseline` 6.0, with the six run ids listed. Every one of the six
turned out to be the weather tool's own `precipitation_probability` quoted back as
a percentage (payload `0.18` rendered as `18% chance`; `0.08` as `8%`) — a
probability the deterministic core *did* produce, which the frozen definition
explicitly excludes. See section 16.

### M15 — malformed tool-call rate

**0.0 vs 0.0**, 32/32 in both conditions. No run in either condition emitted a
call that failed its tool's input contract. This is the metric most at risk of
being quietly massaged, so the rule is stated plainly: **no client-side JSON
repair exists anywhere in this evaluation path.** A malformed batch is preserved
under `_raw` by the transport and counted as a failure.

---

## 15. Results: Coverage and Context

### M5 — horizon coverage

Σ **2.75** (baseline) vs **5.50** (treatment); mean paired delta **+0.0833**;
distributions baseline `{0:27, 1:3, 3:3, 4:10}` vs treatment `{0:16, 1:13, 2:4,
3:9}`. The skill's four-horizon protocol is followed about twice as often — the
clearest skill-attributable improvement in the whole set. This is trace-derived,
not answer-derived: `horizons_checked` is populated once per `assess_risk` call, so
it survives the M1 collapse unscathed.

### M16 — context-limit failure rate

**0.0 vs 3.0**; mean paired delta **+0.125** over 24 pairs. Three treatment runs
(`B08_gust_band_clearing__with_skill__rep2`, `B10_compound_soft_pair__with_skill__rep1`,
`B10_compound_soft_pair__with_skill__rep3`) exhausted the 16384-token window.
Zero baseline runs did. **Read section 18.1 before quoting this number** — the
magnitude is uncertain even though the sign is not.

---

## 16. Manual Review

**152 automatic flags were raised, collapsing onto 151 distinct
`(run_id, metric)` keys. All 151 were adjudicated. No original trace was edited.
No automatic flag was deleted.** The queue is `runs/phase5/manual_review.csv`;
`automatic_flag` and `short_reason` are byte-identical to what the automatic
writer produced (verified by diff against a pre-decision backup), and each
decision sits in `manual_decision` beside them, as the manifest's rule requires.

The 152-versus-151 gap is not a rounding convenience: the superseded pair at
`S01_clear_day__without_skill__rep1` share that `run_id`, so the original and its
replacement each raise an M2 flag against the same key. One decision covers both,
and it is accurate for both — each record fails exactly one device-feed call and
one `analysis_time` call.

| Metric | Flags | Distinct keys | Decision | Effect on the numbers |
| --- | --- | --- | --- | --- |
| M2 | 87 | 86 | `confirmed_with_correction` | none |
| M1 | 40 | 40 | `confirmed` | none |
| M8 | 16 | 16 | `confirmed_redundant` | none |
| M14 | 6 | 6 | `overturn` | M14 baseline numerator 6 → 0 |
| M16 | 3 | 3 | `confirmed` | none |

### M2 — conclusion confirmed, stated cause corrected (86 rows)

The flag's *conclusion* is right: all **195** failed calls across the 86 flagged
records — scored or excluded — passed their tool's JSON schema, so none is a
malformed argument and M15 rightly excludes them. The flag's *cause* — "failed for
environment reasons" — is wrong, and the two causes are not equivalent:

* **109 calls** passed a `device_id` that exists nowhere: not in the context
  snapshot (the scenario's `device` block carries `battery_percent`, movement and
  fatigue but no identifier), not enumerated by the schema (`device_id` is a free
  string), and not in `SKILL.md`. The correct feed name is
  `scn_device_<scenario_id>`, a convention nothing in the prompt states. Models
  inferred `device_123`, `scn_device_sample` and other plausible-looking names and
  failed. **Neither condition could have succeeded.** (Restricted to the scored
  runs this is 47 baseline and 38 treatment — section 12.1.)
* **86 calls** omitted `analysis_time` — the analysis_time case already discussed
  in section 12.1. **All baseline; all treatment `project_route_state` calls
  carried it.** (75 of these fall on scored runs.)

**The two figures above cover every flagged record; section 12.1's cover only the
scored ones.** The difference is the infra-excluded and warm-up records, which are
flagged but contribute to no aggregate.

### M1 — confirmed (40 rows)

Every one of the 40 responses was read. Each opens with a tool-call invocation and
carries no answer text. Nothing was completed.

### M8 — confirmed_redundant (16 rows)

Every one of the 16 responses is a tool-call invocation, so there is no answer
text in which a rule could be named. That is M1's finding restated, not a second
one, and M8 already scores these runs 0. **No number changes.**

### M14 — overturned (6 rows)

Each flagged number was traced back to the tool payloads in that run before the
decision was written:

| run | flagged phrase | `precipitation_probability` in that run's payloads |
| --- | --- | --- |
| `B08…without_skill__rep1`, `…rep2` | `18% chance` | `0.09`, `0.12`, `0.18` |
| `B09…without_skill__rep1`, `…rep2` | `Probability: 8%` | `0.08`, `0.1`, `0.12` |
| `B09…without_skill__rep3` | `probability of 8%` | `0.08`, `0.1`, `0.12` |
| `B10…without_skill__rep3` | `probability: 8%` | `0.08`, `0.1`, `0.12` |

Two independent grounds: the number **is** produced by the deterministic core, and
a precipitation probability is **not** a probability of harm.

### M16 — confirmed (3 rows)

Each is backed by `runs/phase5/cause_evidence.json`, not by an inference from the
record: the probe rebuilt a **lower bound** of the prompt from the preserved trace
(exact except that the model's own assistant text is minimised) and submitted it
with the real `max_tokens=2048`. A refusal proves the real prompt was over the
window. The endpoint's own refusal message is the evidence.

---

## 17. Excluded Runs

**23 of 87 records are excluded from every aggregate.** Exclusions are
**symmetric: 10 infra accidents in each condition.**

| Condition | Infra accident | Warm-up | Superseded | Total |
| --- | --- | --- | --- | --- |
| Baseline (`without_skill`) | 10 | 1 | 1 | 12 |
| Treatment (`with_skill`) | 10 | 1 | 0 | 11 |

Every exclusion carries its reason in `metrics.json`, and the full run-by-run list
is in Appendix A. Named individually:

* **20 infra accidents** — HTTP 400 from the local endpoint. Recorded, excluded,
  and **never scored as skill failures**. The cause is investigated in section
  18.1; 3 of the original 23 were reclassified as proven context-limit failures
  (and are therefore *scored*, section 15), leaving 20 unclassified.
* **2 warm-up runs** — `S01_clear_day__{condition}__rep0`, recorded `scored:
  false`, measured for information only.
* **1 superseded record** — `S01_clear_day__without_skill__rep1` (the
  `RecordingProvider` identity defect, section 21.3). The original is preserved;
  the replacement is `S01_clear_day__without_skill__rep1__r2` and records
  `replaces` and its reason. The original keeps the pair slot's transparency row
  but is never the record a comparison reads.

**The asymmetry that matters is the opposite of what the raw counts suggest.** The
three *scored* context-limit failures are all treatment runs. An excluded run
contributes nothing to any metric; a scored context-limit failure contributes
`M16 = 1.0`. The treatment is therefore penalised *more*, not less, by the
reclassification.

---

## 18. What the Numbers Cannot Say

### 18.1 The HTTP 400 ambiguity — and how far it can be resolved

23 of 84 runs stopped on HTTP 400. `trailguard/providers/local.py:556-565`
preserves only `type(exc).__name__` on `openai.APIStatusError` and **discards
`exc.message` and `exc.body`**, both of which carry the context-length sentence.
From the record, a context-length refusal and a transport failure are
indistinguishable.

`trailguard/` is a protected path and the harness is the measured object, so it
was **not** changed. Instead a forensic probe
(`runs/phase5/forensic_check.py`) rebuilt a **lower bound** of each failing prompt
from the preserved trace and submitted it with the real `max_tokens`.

**What is established:**
* the endpoint was healthy after every one of the 13 pauses (200 on `/v1/models`);
* all 23 failures occurred at 14–16 steps;
* the server's refusal text is *"This model's maximum context length is 16384
  tokens. However, you requested 2048 output tokens and your prompt contains at
  least 16369 input tokens"*;
* vLLM's check is `prompt + max_tokens ≤ 16384`, so with `max_tokens=2048` the
  prompt must stay under 14336.

**What is proven:** 3 runs (`cause: context_limit`). A refusal of a *lower bound*
proves the real prompt was longer still.

**What is not proven:** the other **20**, which stay `unclassified`. An accepted
lower bound proves nothing — the missing assistant text could still have pushed
the real prompt over. Those 20 are excluded as infra accidents and read **0** for
M16.

**Consequence, stated as a bound.** If all 20 unclassified runs were themselves
context failures:

| | as run | if all 20 were context failures |
| --- | --- | --- |
| M1 baseline | 20/32 = 0.625 | 20/42 = 0.476 |
| M1 treatment | 2/32 = 0.0625 | 2/42 = 0.048 |
| M16 baseline | 0/32 | 10/42 = 0.238 |
| M16 treatment | 3/32 = 0.094 | 13/42 = 0.310 |

M1's *sign* is robust in both readings; its magnitude is not. M16's magnitude is
uncertain but its sign is not.

### 18.2 Nineteenth-of-eighteen-metric silence

M6 has 3 pairs, M8 has 7, M14 has 1. These are reported as numbers because
suppressing them would be a choice, but **they support no claim in either
direction.** Appendix B shows the pair count beside every delta for exactly this
reason.

### 18.3 Eight well-populated metrics, and no composite

M1, M2, M3, M5, M7, M10, M11–M12 and M16 have 20–32 pairs. That is the whole
evidentiary base. No composite score was computed, because averaging a regression
in answer production against an improvement in horizon coverage would report
whichever the reader cared about least.

### 18.4 Answer-text metrics are lower bounds

M6, M7, M8, M9, M10, M11, M12, M13 and M14 read the answer with a regex. A run
that says "future weather hazard" in words rather than "R-C" scores as a miss.
The treatment's M1 collapse makes this *worse*, not better: 27 treatment runs have
no answer text to scan at all, so the treatment's answer-text metrics are computed
over far fewer runs (M7: 27 vs 23 non-null; M8: 9 vs 7).

### 18.5 The benchmark cannot separate "the skill" from "a longer system prompt"

The treatment's system prompt is longer and more prescriptive. M1's regression
could be caused by the skill's content, by its length, or by the interaction of
either with this model's tool-formatting. The protocol varied only one thing, and
that is what it measures — but attributing the *direction* to the skill's wording
rather than to its presence is a Phase 6 question this design cannot answer.

---

## 19. Skill Effectiveness Finding

# **MIXED**

The skill **did** change observable safety behaviour — measurably, in both
directions, under a protocol in which nothing else varied. That rules out NEUTRAL.
It is not POSITIVE, and it is not conclusively NEGATIVE either.

**What improved, and is trustworthy:**

* M7 hazard recall, **Σ 0.0 → 4.0** over 20 pairs. The base model reported no
  hazards on scenarios built to contain them; the skilled agent did. For a safety
  aid, this is the clearest argument that the skill is worth having.
* M5 horizon coverage, **Σ 2.75 → 5.50**. The four-horizon protocol is followed
  about twice as often.
* M2 structured tool-call success, **+0.1958** — but see section 12.1: set aside
  the `analysis_time` calls and the gap falls to **+0.0291** over the scored
  population, and what remains is arguably compensation for a schema/implementation
  mismatch rather than competence.

**What regressed:**

* **M1 completion, 62.5% → 6.3%, mean Δ −0.4583.** This is the dominant effect
  and it is not a rounding artefact — 27 of 32 treatment runs ended in a tool call
  emitted as text.
* M4 protocol completion, 2 → 0.
* M16 context-limit failures, 0 → 3.0.

**The finding in one sentence.** `SKILL.md` makes this model follow the TrailGuard
protocol more often and surface hazards it otherwise misses, while more than
doubling the rate at which it fails to produce a usable answer at all — and on the
evidence available, the second effect is larger than the first.

**Why MIXED rather than NEGATIVE.** The hazard-recall improvement (M7) and the
horizon-coverage improvement (M5) are both well-populated and both point the same
way, and both are the metrics that most directly measure the skill's purpose.
Declaring the skill a net negative on the strength of M1 alone would discount
them.

**Why MIXED rather than POSITIVE.** An agent that emits a tool call instead of an
answer in 84% of runs is not a usable safety aid, whatever it does in the 16% where
it answers.

**Confidence.** Moderate for the four well-populated metrics. Low for everything
else. The M1 direction is robust to the 400 ambiguity (section 18.1); its
magnitude is not.

---

## 20. Comparison Against Phase 4

**Phase 4 established** that the agent runtime, the five atomic tools, the Safety
Core and the tool contracts behave as specified, under test, deterministically and
offline — **766 tests** at the Phase 4 freeze (verified by extracting `e3ca520f` into
a clean tree and running the suite: 764 pass, and the two that cannot run outside a
git checkout are `tests/test_secrets_git.py`'s `.gitignore` structure checks).
It identified defects F-1, F-2 and F-3 *inside the harness* and left them, by
instruction, unfixed.

Phase 5 takes the suite from 766 to **1145**.

**Phase 5 adds** that, run end-to-end through the real harness with the real
model, the **F-1 failure mode appears in the wild and is the dominant effect of
loading the skill.** Phase 4 saw F-1 in unit tests; Phase 5 measured how often it
occurs and that the skill makes it more frequent. That is a genuinely new fact and
it is the most valuable single output of this phase.

Phase 5 also **confirms the harness's own invariants hold under load** — M3 1.0
and M15 0.0 in both conditions: the frozen tool boundary is never crossed and no
call ever fails its contract, in 84 runs.

Phase 5 does **not** re-test the Safety Core, and Layer A's passing never
substitutes for a Layer B result.

---

## 21. Threats to Validity

### 21.1 Designed out

* **Baseline weakening.** The baseline is a normal general-purpose agent.
* **Cherry-picked run order.** Balanced alternation, pre-registered.
* **Silent run dropping.** Append-only raw records; a pause is logged and resumed,
  never a reason to abandon a slot.
* **Client-side rescue of malformed output.** None exists in the evaluation path.
* **LLM-as-judge.** None. Manual review read records and traced numbers to payloads.
* **Composite score.** None computed.

### 21.2 Not designed out, and disclosed

* **The 400 ambiguity** (section 18.1): M16's magnitude and M1's magnitude are both
  bounded but not pinned.
* **Answer-text metrics are lower bounds**, and the treatment's M1 collapse shrinks
  their denominators further.
* **"The skill" is indistinguishable from "a longer system prompt"** (section 18.5).
* **Three metrics have 7 or fewer pairs.**

### 21.3 Defects found in this phase's own tooling — all disclosed

Each was found by running something, not by reasoning about it, and each was fixed
in `evals/phase5/` with a test that reads its output back from a scratch directory.

1. **Bound-default output paths** (`16f45ec`). `path: Path = SUMMARY_PATH` binds the
   constant at function-creation time, so reassigning the module attribute changed
   nothing and every caller wrote to the original location. Fixed with an `_out()`
   resolver called at invocation.
2. **`write_manual_review` appending duplicates** — the cause of the fabricated
   aggregate files below.
3. **Four scanner defects** found by test failures: rule-id case sensitivity,
   `<tool_use>` tags not recognised as tool calls, a dead probability regex, and
   mid-sentence horizon aliases mis-attributing a level to the wrong horizon.
4. **M12's numerator rewritten** to the frozen "disagrees" wording.
5. **`run_one(replaces=…)` overwrote the same path** — replaced by a generation
   suffix plus a recorded reason.
6. **`RecordingProvider` identity defect** — a replacement run recorded a different
   provider than the one that produced the trace. The original
   `S01_clear_day__without_skill__rep1` is preserved; its replacement records
   `replaces` and the reason.
7. **`phase5-design-freeze` tag moved** `5dd3c12` → `e0fc196`, because the first
   commit did not yet contain the frozen manifest.
8. **Two frozen tests edited in a prior session** —
   `tests/test_deterministic_core.py` (a snapshot line) and
   `tests/test_weather_providers.py` (a `snapshot_ref` assertion). Both edits were
   to snapshots of *values the frozen code changed deliberately*, not to weaken an
   assertion about it.
9. **`max_steps` pre-registration corrected 12 → 16**, with the derivation recorded
   in the manifest: the protocol needs 13 tool calls (1 route + 1 device + 4
   horizons × 3 calls), and a 12-step budget would have truncated runs for a reason
   that is not the skill's behaviour.
10. **The fabricated-aggregate pollution incident** (`3c9142d`). A smoke test of
    mine wrote four fabricated aggregate files (`summary.csv`,
    `paired_results.csv`, `manual_review.csv`, `metrics.json`) into the real
    `runs/phase5/`, describing one synthetic run. I verified their contents,
    verified recurrence was prevented, verified the whole suite was hermetic
    against the result store, and then removed exactly those four files. **No raw
    evidence under `raw/` was touched.**
11. **The positional-zip pairing defect** (`ecd5e54`, section 2 of the appendix
    below) — the most serious of these, because it produced a plausible wrong
    headline number rather than an error.
12. **Two methodological errors of my own** (section 22.2).
13. **The inverted superseding rule** (`c9b95d0`) — the subject of section 22.3.
    Found while regenerating the evidence for this report, from `summary.csv`
    showing two rows with an identical `run_id`.

---

## 22. Reproduction

### 22.1 Commands

```bash
# 1. Verify the freeze and the single variable (base_url only for the health check)
.venv/bin/python -m evals.phase5.preflight
#    -> VERDICT: PREFLIGHT PASS   (37 checks)

# 2. Score the preserved runs and rewrite the aggregates
.venv/bin/python -m evals.phase5.evaluate
#    -> runs/phase5/{summary,paired_results,manual_review}.csv, metrics.json

# 3. Re-run the whole regression suite
.venv/bin/python -m pytest -q
#    -> 1145 passed
```

**Endpoint configuration.** vLLM serving `qwen3-vl-30b-a3b` at
`http://127.0.0.1:8000/v1`, `max_model_len=16386`→**16384** (the frozen value; no
attempt was made to raise it), `quantization` untouched, default parser and chat
template, guided decoding disabled. Verify with:

```bash
curl -s http://127.0.0.1:8000/v1/models
```

**Commits and tags.** `phase4-freeze` → `e3ca520fb5fc`; `phase5-design-freeze` →
`e0fc196`; aggregate path `16f45ec` → `c9b95d0`.

**Where the evidence lives.** `runs/` is gitignored by a Phase 13 policy, so the
raw evidence is **on disk only, not in git**:
`runs/phase5/raw/*.json` (87 records), `metrics.json`, `summary.csv`,
`paired_results.csv`, `manual_review.csv`, `cause_evidence.json`,
`pause_log.csv`, `benchmark_run.log`. Human-readable copies of the four
aggregates are in `docs/phase5_evidence/`. The two Phase 5 forensic scripts
(`forensic_check.py`, `resolve_manual_review.py`) also live under `runs/phase5/`
and are likewise not in git; what each did is described in sections 16 and 18.1.

### 22.2 Two methodological errors of my own, disclosed because they affected numbers

1. **The first forensic probe used `max_tokens=16`** and reported all 23 runs
   accepted. That was wrong: vLLM's window check is `prompt + max_tokens ≤ 16384`,
   so a small `max_tokens` accepts prompts the real request would have refused. I
   caught it by re-deriving the arithmetic rather than by the endpoint telling me,
   and re-ran with the real `max_tokens=2048` — which produced the 3 proven
   refusals. Had I not caught it, M16 would have read 0 for all three runs and 23
   runs would have stayed unclassified.
2. **I over-stated the reproducibility of the failures.** I wrote that the
   failures recur per (scenario, condition). `B08_gust_band_clearing__with_skill`
   completed rep1 at 12 steps and failed rep2 at 14, which refutes
   bit-reproducibility. I corrected the note in place with an explicit
   "correction to the note above" rather than leaving the original standing.

### 22.3 The superseding defect, and the false claim inside the evidence

Found during report preparation, not by a test, which is the part worth
emphasising: **a defect in my own aggregation could hide from a 1145-test suite.**
The suite passed the entire time.

`load_runs` keyed its lookup on `record["run_id"]`. The replacement record for
`S01_clear_day__without_skill__rep1` **carries the same `run_id` string as the record
it corrects** — the two differ only by filename, by the `__r2` suffix. So
`records[replaces]` resolved to whichever copy was loaded last, and the marking
loop flagged the *replacement* as superseded while the original kept the pair slot.
Backwards, and silent: nothing raised, both records scored, every comparison read
the wrong one.

**What it changed: no number.** I scored both records against the reference and all
18 metrics agree — M1 1.0, M2 0.875, M5 1.0, M6 0.75 on each. Re-aggregating after
the fix left `paired_results.csv` and `manual_review.csv` byte-identical. So this is
a latent-trap fix, not a correction; the trap would have bitten on any future
re-aggregation in which the two did differ.

**Why the trap existed at all.** The replacement was created to correct the
`RecordingProvider` identity defect: the original record claims
`provider: RecordingProvider`, `model_name: RecordingProvider:model-unset`, which
describes the recording layer rather than the model that produced the trace. The
replacement carries `provider: local`, `model_name: qwen3-vl-30b-a3b`.

**A false claim inside the preserved evidence.** The replacement's own `reason`
field reads *"the first record's model_name/provider were mislabelled by the
recording provider … the run itself is preserved and unchanged, this record carries
the corrected identity."* **That is false.** The replacement is a **re-run**, not a
re-label: its `trace.run_id` UUID is different (`84be9701…` vs `fa326e2f…`), its
start and finish are 13 minutes later, several of its tool-call arguments differ,
and its `final_response` is a differently-worded answer. Nothing about its
"unchanged" claim survives inspection.

The evidence is append-only by policy and no raw record may be edited during Phase
5, so **that inaccurate sentence stays on disk, in the preserved evidence, and is
disclosed here instead of corrected.** A Phase 6 reader who trusts the `reason`
field will believe a run was re-labelled when it was re-executed. The M2 decision
already in the queue happens to remain accurate for both records — each fails
exactly one device-feed call and one `analysis_time` call — so no manual decision
is invalidated, but that is luck rather than checking.

**The test could not have caught it either.** `_write_raw` derives a record's
filename from its `run_id`, so the existing superseding test could only express
shapes in which the two files are distinguishable by name *and* by run_id. The new
test writes the two files explicitly, and I verified it fails against the reverted
code (`KeyError: 'superseded_by'`).

---

## 23. Skill Effectiveness Finding

# **MIXED**

Restated here because the Phase 5 brief names this slot. See section 19 for the
reasoning and section 18 for what the finding cannot support.

* Benchmark Integrity: **PASS**.
* Skill Effectiveness: **MIXED** — measurable improvement in hazard surfacing
  (M7) and horizon coverage (M5); measurable regression in answer production (M1,
  62.5% → 6.3%) and in context-window failures (M16, 0 → 3.0).
* The largest single effect is a **regression**.
* M2's advantage reduces to one pre-condition once the `analysis_time` calls are
  set aside (+0.1958 → +0.0291 over the scored population).
* No composite score was computed and none should be inferred.

---

## 24. Phase 6 Handoff

**What Phase 6 inherits:**

* 87 preserved raw records and a verified PASS preflight, so any future claim can
  be recomputed from the evidence rather than from a summary.
* A corrected, slot-keyed pairing with a test that prevents `metrics.json` and
  `paired_results.csv` from drifting apart again.
* The forensic method for distinguishing a context-length refusal from a transport
  failure without modifying the provider.

**What Phase 6 must not re-litigate:**

* Whether `SKILL.md` was byte-identical across the batch — it was, and the
  preflight re-proves it every run.
* Whether the baseline was weakened — it was not.
* Whether the M1 responses really contain tool calls — they were read directly.
* Whether the M14 flags were real claims — they were traced to tool payloads and
  overturned with both figures retained.

**What Phase 6 should fix, in this order:**

1. **The tool-call-as-text failure (M1).** This is the binding constraint. A model
   that writes `<tool_call>` into the content channel gets nothing executed. This is
   the highest-value target in the project.
2. **The `project_route_state` schema/implementation contradiction.** `analysis_time`
   is optional in the frozen contract and required by the implementation. Either
   the schema should declare it required, or the harness should pass it. Until then
   M2 measures skill-versus-baseline compliance with an undocumented pre-condition.
3. **The undiscoverable `device_id`.** 109 calls failed because the identifier
   appears nowhere the model could read it. Add it to the context snapshot or
   enumerate it in the schema.
4. **The provider's discarded error body.** Two lines in
   `trailguard/providers/local.py` would end the 400 ambiguity permanently.
5. **The `max_model_len` headroom.** Three treatment runs hit the window at 14–16
   steps. The frozen value stays frozen for the benchmark, but the demo needs a
   path through it.

**What Phase 6 must not do:** fix Phase 4's F-1/F-2/F-3 and then re-run Phase 5
against the fixed harness without saying so. The benchmark's value is that it
measured *this* harness. A fixed harness is a different measurement.

---

## Self-audit (Q1–Q22)

Each question from the Phase 5 brief, answered with the section of this report that
carries the evidence rather than with a yes. Where the answer is a *no*, or a
*not exactly*, that is what is written.

| # | Question | Answer | Where it is shown |
| --- | --- | --- | --- |
| Q1 | Were the scenarios frozen before the first scored model run? | **Yes.** The design freeze `e0fc196` (tag `phase5-design-freeze`) predates every raw record, and the manifest's own freeze timestamp is checked record by record. | §5, §9, §11.1 |
| Q2 | Does every scenario have a Deterministic Reference, generated not judged? | **Yes**, 14 for 14, produced by `references.py` running the frozen Safety Core over the frozen replay data. No field carries a human judgement. | §6 |
| Q3 | Was the run order fixed in advance and balanced, not randomised? | **Yes** — pre-registered balanced alternation, written into the manifest before the batch, precisely so a warming endpoint cannot favour one condition. | §8 |
| Q4 | Were repetitions pre-registered? | **Yes**, 3, in the manifest, before any scored run. 14 × 3 × 2 = 84. | §8 |
| Q5 | Were both conditions run with identical sampling, context, tools, harness? | **Yes**, and each pin was verified by `preflight` rather than asserted: model id, `temperature` 0.0, `max_tokens` 2048, `max_steps` 16, `max_model_len` 16384, `tool_failure_policy` `report`, harness unmodified, byte-identical context snapshots and replay fixtures. | §3, §4 |
| Q6 | Is `SKILL.md` byte-identical to the Phase 4 freeze? | **Yes.** sha256 `98fd007eb23aee18f98bf67c295d7bad3c8e48f7a722022c852218fb0c649769`, re-verified after the batch. | §3, §4 |
| Q7 | Are the five atomic tools identical in name/description/schema/order? | **Yes.** `get_route_context`, `project_route_state`, `get_weather_for_route_time`, `get_user_device_context`, `assess_risk`, in that order; tool schema sha256 `caed2924c6abd276…`; identical in both conditions. | §3 |
| Q8 | Was any forbidden tool offered? | **No.** `run_all_horizons` and `forecast_risk` are in the manifest's `excluded_names`, and the preflight asserts they are absent. M3 is 1.0/1.0 — no run named a tool outside the registry. | §3, §12 |
| Q9 | Any client-side JSON repair, regex extraction or manual XML parsing of model output? | **No.** None exists anywhere in the evaluation path. The one regex in this report that touches model *output* is the answer-text scanner, which reads a finished answer and never repairs a call in flight. | §12 (M15), §14 (M15) |
| Q10 | Were malformed tool calls counted as structured tool-call failures? | **Yes.** M15 counts them and nothing repairs them; the transport preserves a malformed batch under `_raw` so the count can be re-derived. M15 came out 0.0/0.0 — no run produced one. | §14 |
| Q11 | Was `max_model_len` left at 16384? | **Yes**, never raised. The server reports 16384 on every preflight. | §3, §9 |
| Q12 | Was quantization / parser / chat-template / guided-decoding touched? | **No.** The preflight asserts the provider code is unmodified and JSON mode disabled in the transport. | §3, §9 |
| Q13 | Was any Phase 4 F-1/F-2/F-3 defect fixed before or during Phase 5? | **No.** Nothing under `trailguard/` was modified. The F-1 family is *observed* here (M1) rather than repaired — observing it is the point. | §3, §12, §20 |
| Q14 | Was every raw result preserved under `runs/phase5/raw/<run_id>.json`? | **Yes**, 87 records, append-only. | §8, §9 |
| Q15 | Was any run_id ever overwritten? | **No.** A pause resumed the same run_id; the one correction created a new file with a `__r2` suffix and a recorded `replaces`/`reason`. | §17, §22.3 |
| Q16 | Were corrections made through `replaces` with a stated reason, not edits? | **Yes** — with the disclosure that the stated reason is inaccurate on one point: the replacement is a re-run, not a re-label, and says so wrongly. §22.3. | §17, §22.3 |
| Q17 | Were infra accidents excluded and never scored as skill failures? | **Yes**, 20 of them, **10 in each condition**. Every exclusion carries its reason in `metrics.json`. | §11.5, §17 |
| Q18 | Was context overflow recorded as an experimental result? | **Yes.** Three runs with a proven context-limit cause are *scored* (M16), not excluded. 20 others remain unclassified and read 0 — §18.1 bounds how wrong that could be. | §15, §17, §18.1 |
| Q19 | Was any LLM-as-judge used as a primary measure? | **No.** Every metric is deterministic from the trace, the answer text against a generated reference, or the raw record. The manual review in §16 read records and traced numbers back to tool payloads. | §7, §16 |
| Q20 | Was any composite score invented? | **No.** `metrics.json` carries `no_composite_score: true`, and no weighted average or index was computed anywhere. | §7.3, §19 |
| Q21 | Does the verdict separate benchmark integrity from skill effect? | **Yes.** §11 = **PASS** (the measurement is trustworthy); §19/§23 = **MIXED** (the skill's effect is bidirectional). Neither implies the other. | §11, §19, §23 |
| Q22 | Were any secrets read, printed, or committed? | **No.** No `.env`, API key, token, SSH key, `~/.claude/settings.json` or any credential file was read, printed or committed at any point. The `stepfun` secondary provider was not used and its credential was not sought; the benchmark ran entirely against the local primary. | — |

---

# Appendices

Appendix A is every run with the five metrics the argument turns on. Appendix B is
every metric's paired delta with its pair count beside it, so no delta can be read
without its denominator. Appendix C is all eighteen metrics from `metrics.json`.
Appendix D is the frozen protocol and manifest that sections 3–8 refer back to.

A, B and C are generated by `runs/phase5/build_appendices.py` from
`docs/phase5_evidence/`, and that script reads each file's own size and sha256 at
build time. The first version of it hardcoded those hashes, which is how this report
came within one draft of quoting a hash that no longer matched the evidence beside
it, after `metrics.json` was corrected.

### Appendix A — every run, with the five metrics that mattered most

| run_id | condition | rep | scored | exclusion_reason | M1 | M4 | M5 | M7 | M16 |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| `B01_mid_route_all_clear__with_skill__rep1` | with | 1 | False | infra accident (ProviderResponseError) | 0 | 0 | 0.75 | — | 0 |
| `B01_mid_route_all_clear__with_skill__rep2` | with | 2 | True |  | 0 | 0 | 0 | — | 0 |
| `B01_mid_route_all_clear__with_skill__rep3` | with | 3 | False | infra accident (ProviderResponseError) | 0 | 0 | 0.75 | — | 0 |
| `B01_mid_route_all_clear__without_skill__rep1` | without | 1 | True |  | 1 | 0 | 0 | — | 0 |
| `B01_mid_route_all_clear__without_skill__rep2` | without | 2 | True |  | 0 | 0 | 0.25 | — | 0 |
| `B01_mid_route_all_clear__without_skill__rep3` | without | 3 | True |  | 1 | 0 | 0 | — | 0 |
| `B02_gps_drift_warn__with_skill__rep1` | with | 1 | True |  | 0 | 0 | 0.25 | 0.25 | 0 |
| `B02_gps_drift_warn__with_skill__rep2` | with | 2 | True |  | 0 | 0 | 0.25 | 0.25 | 0 |
| `B02_gps_drift_warn__with_skill__rep3` | with | 3 | True |  | 0 | 0 | 0.25 | 0.25 | 0 |
| `B02_gps_drift_warn__without_skill__rep1` | without | 1 | True |  | 0 | 0 | 0 | 0 | 0 |
| `B02_gps_drift_warn__without_skill__rep2` | without | 2 | True |  | 0 | 0 | 0 | 0 | 0 |
| `B02_gps_drift_warn__without_skill__rep3` | without | 3 | True |  | 0 | 0 | 0 | 0 | 0 |
| `B03_route_deviation_under_threshold__with_skill__rep1` | with | 1 | False | infra accident (ProviderResponseError) | 0 | 0 | 0.5 | — | 0 |
| `B03_route_deviation_under_threshold__with_skill__rep2` | with | 2 | False | infra accident (ProviderResponseError) | 0 | 0 | 0.75 | — | 0 |
| `B03_route_deviation_under_threshold__with_skill__rep3` | with | 3 | False | infra accident (ProviderResponseError) | 0 | 0 | 0.5 | — | 0 |
| `B03_route_deviation_under_threshold__without_skill__rep1` | without | 1 | True |  | 1 | 0 | 0 | — | 0 |
| `B03_route_deviation_under_threshold__without_skill__rep2` | without | 2 | True |  | 1 | 0 | 0 | — | 0 |
| `B03_route_deviation_under_threshold__without_skill__rep3` | without | 3 | False | infra accident (ProviderResponseError) | 0 | 0 | 0.75 | — | 0 |
| `B04_tight_sunset_margin__with_skill__rep1` | with | 1 | True |  | 0 | 0 | 0 | 0 | 0 |
| `B04_tight_sunset_margin__with_skill__rep2` | with | 2 | True |  | 0 | 0 | 0 | 0 | 0 |
| `B04_tight_sunset_margin__with_skill__rep3` | with | 3 | True |  | 0 | 0 | 0 | 0 | 0 |
| `B04_tight_sunset_margin__without_skill__rep1` | without | 1 | True |  | 0 | 0 | 0 | 0 | 0 |
| `B04_tight_sunset_margin__without_skill__rep2` | without | 2 | True |  | 0 | 0 | 0 | 0 | 0 |
| `B04_tight_sunset_margin__without_skill__rep3` | without | 3 | True |  | 0 | 0 | 0 | 0 | 0 |
| `B05_late_gust_hazard__with_skill__rep1` | with | 1 | True |  | 0 | 0 | 0.25 | 0 | 0 |
| `B05_late_gust_hazard__with_skill__rep2` | with | 2 | True |  | 0 | 0 | 0.25 | 0 | 0 |
| `B05_late_gust_hazard__with_skill__rep3` | with | 3 | True |  | 0 | 0 | 0.25 | 0 | 0 |
| `B05_late_gust_hazard__without_skill__rep1` | without | 1 | False | infra accident (ProviderResponseError) | 0 | 1 | 1 | 1 | 0 |
| `B05_late_gust_hazard__without_skill__rep2` | without | 2 | False | infra accident (ProviderResponseError) | 0 | 1 | 1 | 1 | 0 |
| `B05_late_gust_hazard__without_skill__rep3` | without | 3 | True |  | 1 | 0 | 0.25 | 0 | 0 |
| `B06_future_gust_front__with_skill__rep1` | with | 1 | True |  | 0 | 0 | 0.25 | 0 | 0 |
| `B06_future_gust_front__with_skill__rep2` | with | 2 | True |  | 0 | 0 | 0 | 0 | 0 |
| `B06_future_gust_front__with_skill__rep3` | with | 3 | True |  | 0 | 0 | 0.25 | 0 | 0 |
| `B06_future_gust_front__without_skill__rep1` | without | 1 | False | infra accident (ProviderResponseError) | 0 | 1 | 1 | 1 | 0 |
| `B06_future_gust_front__without_skill__rep2` | without | 2 | False | infra accident (ProviderResponseError) | 0 | 1 | 1 | 1 | 0 |
| `B06_future_gust_front__without_skill__rep3` | without | 3 | False | infra accident (ProviderResponseError) | 0 | 1 | 1 | 1 | 0 |
| `B07_pace_drop_warn__with_skill__rep1` | with | 1 | False | infra accident (ProviderResponseError) | 0 | 0 | 0.75 | 0.75 | 0 |
| `B07_pace_drop_warn__with_skill__rep2` | with | 2 | True |  | 1 | 0 | 0.25 | 0.25 | 0 |
| `B07_pace_drop_warn__with_skill__rep3` | with | 3 | False | infra accident (ProviderResponseError) | 0 | 0 | 0.75 | 0.75 | 0 |
| `B07_pace_drop_warn__without_skill__rep1` | without | 1 | True |  | 1 | 0 | 0 | 0 | 0 |
| `B07_pace_drop_warn__without_skill__rep2` | without | 2 | True |  | 1 | 0 | 0 | 0 | 0 |
| `B07_pace_drop_warn__without_skill__rep3` | without | 3 | True |  | 1 | 0 | 0 | 0 | 0 |
| `B08_gust_band_clearing__with_skill__rep1` | with | 1 | True |  | 0 | 0 | 0 | 0 | 0 |
| `B08_gust_band_clearing__with_skill__rep2` | with | 2 | True |  | 0 | 0 | 0.75 | 1 | 1 |
| `B08_gust_band_clearing__with_skill__rep3` | with | 3 | True |  | 0 | 0 | 0 | 0 | 0 |
| `B08_gust_band_clearing__without_skill__rep1` | without | 1 | True |  | 1 | 0 | 0 | 0 | 0 |
| `B08_gust_band_clearing__without_skill__rep2` | without | 2 | True |  | 1 | 0 | 0 | 0 | 0 |
| `B08_gust_band_clearing__without_skill__rep3` | without | 3 | True |  | 1 | 0 | 0 | 0 | 0 |
| `B09_battery_shortfall__with_skill__rep1` | with | 1 | True |  | 0 | 0 | 0.25 | 0.25 | 0 |
| `B09_battery_shortfall__with_skill__rep2` | with | 2 | False | infra accident (ProviderResponseError) | 0 | 0 | 0.75 | 0.75 | 0 |
| `B09_battery_shortfall__with_skill__rep3` | with | 3 | True |  | 0 | 0 | 0.25 | 0.25 | 0 |
| `B09_battery_shortfall__without_skill__rep1` | without | 1 | True |  | 1 | 0 | 0 | 0 | 0 |
| `B09_battery_shortfall__without_skill__rep2` | without | 2 | True |  | 1 | 0 | 0 | 0 | 0 |
| `B09_battery_shortfall__without_skill__rep3` | without | 3 | True |  | 1 | 0 | 0 | 0 | 0 |
| `B10_compound_soft_pair__with_skill__rep1` | with | 1 | True |  | 0 | 0 | 0.75 | 0.75 | 1 |
| `B10_compound_soft_pair__with_skill__rep2` | with | 2 | True |  | 0 | 0 | 0 | 0 | 0 |
| `B10_compound_soft_pair__with_skill__rep3` | with | 3 | True |  | 0 | 0 | 0.75 | 0.75 | 1 |
| `B10_compound_soft_pair__without_skill__rep1` | without | 1 | True |  | 1 | 0 | 0 | 0 | 0 |
| `B10_compound_soft_pair__without_skill__rep2` | without | 2 | True |  | 1 | 0 | 0 | 0 | 0 |
| `B10_compound_soft_pair__without_skill__rep3` | without | 3 | True |  | 1 | 0 | 0 | 0 | 0 |
| `B11_multi_driver_storm__with_skill__rep1` | with | 1 | True |  | 0 | 0 | 0 | 0 | 0 |
| `B11_multi_driver_storm__with_skill__rep2` | with | 2 | True |  | 0 | 0 | 0 | 0 | 0 |
| `B11_multi_driver_storm__with_skill__rep3` | with | 3 | True |  | 0 | 0 | 0 | 0 | 0 |
| `B11_multi_driver_storm__without_skill__rep1` | without | 1 | True |  | 0 | 0 | 0 | 0 | 0 |
| `B11_multi_driver_storm__without_skill__rep2` | without | 2 | True |  | 0 | 0 | 0 | 0 | 0 |
| `B11_multi_driver_storm__without_skill__rep3` | without | 3 | True |  | 0 | 0 | 0 | 0 | 0 |
| `S01_clear_day__with_skill__rep0` | with | 0 | False | recorded scored:false (the pre-batch w | 0 | 0 | 0.25 | — | 0 |
| `S01_clear_day__with_skill__rep1` | with | 1 | True |  | 0 | 0 | 0.25 | — | 0 |
| `S01_clear_day__with_skill__rep2` | with | 2 | True |  | 0 | 0 | 0.25 | — | 0 |
| `S01_clear_day__with_skill__rep3` | with | 3 | True |  | 0 | 0 | 0 | — | 0 |
| `S01_clear_day__without_skill__rep0` | without | 0 | False | recorded scored:false (the pre-batch w | 1 | 1 | 1 | — | 0 |
| `S01_clear_day__without_skill__rep1` | without | 1 | False | superseded by S01_clear_day__without_s | 1 | 1 | 1 | — | 0 |
| `S01_clear_day__without_skill__rep1` | without | 1 | True |  | 1 | 1 | 1 | — | 0 |
| `S01_clear_day__without_skill__rep2` | without | 2 | True |  | 1 | 1 | 1 | — | 0 |
| `S01_clear_day__without_skill__rep3` | without | 3 | True |  | 0 | 0 | 0 | — | 0 |
| `S04_sunset_margin_pace_drop__with_skill__rep1` | with | 1 | True |  | 0 | 0 | 0 | 0 | 0 |
| `S04_sunset_margin_pace_drop__with_skill__rep2` | with | 2 | True |  | 0 | 0 | 0 | 0 | 0 |
| `S04_sunset_margin_pace_drop__with_skill__rep3` | with | 3 | True |  | 0 | 0 | 0 | 0 | 0 |
| `S04_sunset_margin_pace_drop__without_skill__rep1` | without | 1 | False | infra accident (ProviderResponseError) | 0 | 0 | 0.75 | 0.75 | 0 |
| `S04_sunset_margin_pace_drop__without_skill__rep2` | without | 2 | False | infra accident (ProviderResponseError) | 0 | 0 | 0.75 | 0.75 | 0 |
| `S04_sunset_margin_pace_drop__without_skill__rep3` | without | 3 | True |  | 0 | 0 | 0 | 0 | 0 |
| `S11_stale_gps_missing_data__with_skill__rep1` | with | 1 | False | infra accident (ProviderResponseError) | 0 | 0 | 0.5 | — | 0 |
| `S11_stale_gps_missing_data__with_skill__rep2` | with | 2 | True |  | 1 | 0 | 0 | — | 0 |
| `S11_stale_gps_missing_data__with_skill__rep3` | with | 3 | False | infra accident (ProviderResponseError) | 0 | 0 | 0.5 | — | 0 |
| `S11_stale_gps_missing_data__without_skill__rep1` | without | 1 | False | infra accident (ProviderResponseError) | 0 | 1 | 1 | — | 0 |
| `S11_stale_gps_missing_data__without_skill__rep2` | without | 2 | False | infra accident (ProviderResponseError) | 0 | 1 | 1 | — | 0 |
| `S11_stale_gps_missing_data__without_skill__rep3` | without | 3 | True |  | 1 | 0 | 0.25 | — | 0 |

The complete row set — all 18 metrics for all 87 records, verbatim — is `docs/phase5_evidence/summary.csv` (22221 bytes, `d05eddfbae009520`). This table shows the six metrics whose values the argument in sections 12–15 turns on; the other twelve are in the file and rolled up in Appendix C.

The manual-review queue is `docs/phase5_evidence/manual_review.csv` (102538 bytes, `77127a5eb0f3e512`) — 152 lines, one header plus 151 decided rows. Section 16 explains why 152 automatic flags land on 151 rows.

### Appendix B — paired deltas, per metric

| metric | complete pairs | incomplete pairs | mean Δ (treat − base) | range of Δ |
| --- | --- | --- | --- | --- |
| `M10_missed_hazard_rate` | 20 | 43 | -0.2000 | -1 … +0 |
| `M11_false_alarm_rate` | 24 | 43 | +0.0000 | +0 … +0 |
| `M12_core_contradiction_rate` | 24 | 43 | +0.0000 | +0 … +0 |
| `M13_hard_rule_softening_rate` | 7 | 43 | +0.0000 | +0 … +0 |
| `M14_unsupported_probability_claim_rate` | 1 | 43 | +0.0000 | +0 … +0 |
| `M15_malformed_tool_call_rate` | 24 | 43 | +0.0000 | +0 … +0 |
| `M16_context_limit_failure_rate` | 24 | 43 | +0.1250 | +0 … +1 |
| `M17_max_step_failure_rate` | 24 | 43 | +0.0000 | +0 … +0 |
| `M18_provider_failure_rate` | 24 | 43 | +0.0000 | +0 … +0 |
| `M1_completion_rate` | 24 | 43 | -0.4583 | -1 … +0 |
| `M2_structured_tool_call_success_rate` | 24 | 43 | +0.1958 | +0.020202 … +0.313187 |
| `M3_valid_tool_call_rate` | 24 | 43 | +0.0000 | +0 … +0 |
| `M4_protocol_completion_rate` | 24 | 43 | -0.0833 | -1 … +0 |
| `M5_horizon_coverage` | 24 | 43 | +0.0833 | -0.75 … +0.75 |
| `M6_risk_level_exact_match` | 3 | 43 | -0.5000 | -0.75 … +0 |
| `M7_hazard_recall` | 20 | 43 | +0.2000 | +0 … +1 |
| `M8_hard_rule_recall` | 7 | 43 | +0.0000 | +0 … +0 |
| `M9_risk_driver_recall` | 20 | 43 | +0.0000 | +0 … +0 |

A pair is *complete* only when both conditions have a value for that metric on that (scenario, repetition). An incomplete pair is reported as incomplete, never as a zero. The full row set with every individual pair is `docs/phase5_evidence/paired_results.csv` (63039 bytes, `13c0a62c5e2aaa0c`).

### Appendix C — metric-by-metric counts, from `metrics.json`

| metric | direction | N base | N treat | Σ base | Σ treat | mean Δ | pairs | note |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| `M10_missed_hazard_rate` | lower is better | 23 | 27 | 23 | 23 | -0.2000 | 20 |  |
| `M11_false_alarm_rate` | lower is better | 32 | 32 | 0 | 0 | +0.0000 | 24 |  |
| `M12_core_contradiction_rate` | lower is better | 32 | 32 | 0 | 0 | +0.0000 | 24 |  |
| `M13_hard_rule_softening_rate` | lower is better | 7 | 9 | 0 | 0 | +0.0000 | 7 |  |
| `M14_unsupported_probability_claim_rate` | lower is better | 20 | 2 | 0 | 0 | +0.0000 | 1 | automatic baseline 6, 6 overturns → resolved |
| `M15_malformed_tool_call_rate` | lower is better | 32 | 32 | 0 | 0 | +0.0000 | 24 |  |
| `M16_context_limit_failure_rate` | lower is better | 32 | 32 | 0 | 3 | +0.1250 | 24 |  |
| `M17_max_step_failure_rate` | lower is better | 32 | 32 | 0 | 0 | +0.0000 | 24 |  |
| `M18_provider_failure_rate` | lower is better | 32 | 32 | 0 | 0 | +0.0000 | 24 |  |
| `M1_completion_rate` | higher is better | 32 | 32 | 20 | 2 | -0.4583 | 24 |  |
| `M2_structured_tool_call_success_rate` | higher is better | 32 | 32 | 22.4365 | 28.2497 | +0.1958 | 24 |  |
| `M3_valid_tool_call_rate` | higher is better | 32 | 32 | 32 | 32 | +0.0000 | 24 |  |
| `M4_protocol_completion_rate` | higher is better | 32 | 32 | 2 | 0 | -0.0833 | 24 |  |
| `M5_horizon_coverage` | higher is better | 32 | 32 | 2.75 | 5.5 | +0.0833 | 24 |  |
| `M6_risk_level_exact_match` | higher is better | 5 | 16 | 1.5 | 0 | -0.5000 | 3 |  |
| `M7_hazard_recall` | higher is better | 23 | 27 | 0 | 4 | +0.2000 | 20 |  |
| `M8_hard_rule_recall` | higher is better | 7 | 9 | 0 | 0 | +0.0000 | 7 |  |
| `M9_risk_driver_recall` | higher is better | 23 | 27 | 0 | 0 | +0.0000 | 20 |  |

`N base` / `N treat` count the runs in that condition with a non-null value for the metric — the denominator that travelled with the numerator. `metrics.json` is (85378 bytes, `282ad6bad170776e`).

One rounding note, so a reader who re-adds the column is not puzzled: the `Σ` figures here come from `metrics.json`, which sums the metric values at full floating-point precision, while `summary.csv` and `paired_results.csv` render each per-run value at six decimal places. Re-adding the rounded column reproduces the totals to five places and can differ in the sixth — `M2`, for instance, re-adds to 22.436543 / 28.249748 against the 22.436536 / 28.249745 quoted above, a 7e-6 gap that is the rendering, not a different measurement. Every paired delta in this table is unaffected, because it is computed before any rounding.
### Appendix D — the frozen protocol document and the manifest

The protocol is `evals/phase5/benchmark_manifest.json` — 35738 bytes, sha256
`5536b1d7bf7fd4f6c6a7b22ba196e50b3fcfc8dc5c360a114d81676550628ec1`, computed
from the file on disk and not from this sentence. The points sections 3–8 lean on:

| Contract term | Frozen value |
| --- | --- |
| Question | *"Under an identical model, task, context, tool registry, replay data, sampling parameters and agent harness, does loading `SKILL.md` change the agent's observable safety behaviour?"* |
| `single_variable.varies` | `skill_loaded` — the `SKILL.md` text appended to the system prompt. Nothing else. |
| `single_variable.frozen` | 14 fields: model and served checkpoint; provider and `base_url`; task text; context snapshot; tool registry (names, descriptions, JSON schemas, order); replay weather/route/device fixtures; `analysis_time`; `temperature`; `max_tokens`; `max_steps`; `max_model_len`; `tool_failure_policy`; agent harness; external state (no network, no live weather). |
| `single_variable.invalid_if` | any frozen field differs within a pair — then the pair is `PAIR INVALID`, reported, and **excluded from the paired deltas rather than repaired**. |
| `sampling` | `temperature` 0.0, `max_tokens` 2048, `max_steps` **16**, `max_model_len` 16384, `seed` null, `top_p` null, presence/frequency penalty null, `tool_failure_policy` `report`. |
| `max_steps` derivation | 13 required tool calls (route 1 + device 1 + projection 3 + weather 4 + assess 4), plus the documented `DeviceFeedError` recovery turn, plus the answer turn. First pre-registered at 12; the warm-up showed 12 cannot fit the protocol, and it was corrected **before any scored run**. |
| Pairing | A pair is `(scenario, without_skill)` vs `(scenario, with_skill)` from the same repetition. 42 planned pairs (14 per repetition × 3). |
| Raw results | `runs/phase5/raw/<run_id>.json`, `run_id = <scenario_id>__<condition>__rep<repetition>`. A run_id is **never** overwritten; a replacement gets a new run_id and records `replaces` and `reason`. |
| `skill_loaded` | `SKILL.md`, `byte_unchanged: true`, sha256 `98fd007eb23aee18f98bf67c295d7bad3c8e48f7a722022c852218fb0c649769`. |
| Tool registry | the frozen five, `order_frozen: true`, schema sha256 `caed2924c6abd276…`, `excluded_names: [run_all_horizons, forecast_risk]`. |
| Safety Core | `trailguard/rules/rules_v1.yaml` sha256 `b2630f3d32e50d6…`, `trailguard/rules/calibration_v1.yaml` sha256 `05bb20a16d7315c7…`. |
| Warm-up | one unscored run per condition on `S01_clear_day`, before the first scored run; traced, excluded from every metric. |
| Manual review | `runs/phase5/manual_review.csv`, columns `run_id, metric, automatic_flag, manual_decision, short_reason`. Rule: resolves ambiguity only; **the original trace is never edited**; a decision is recorded *beside* the automatic flag, not over it. |
| Forbidden during Phase 5 | client-side JSON repair / regex extraction / manual XML parsing of model output; any `max_model_len` increase past 16384; any change of quantization, parser, chat template or guided decoding; any edit to `SKILL.md`, the Safety Core, the five tool contracts or the scenario set; any new tool; any LLM-as-judge as a primary evaluation; `git push` or any change to SSH or system configuration. |
| Failure vocabulary | `context_limit_failure` (heuristic string match, raw string preserved); `infra_accident` (`provider_error` — pauses, recorded, excluded, never deleted); `max_step_failure` (a real result, not an accident); `not_implemented` (a Phase 0 stub — must not occur); `structured_tool_call_failure` (error prefix `invalid arguments for `, Phase 4's F-1 shape, counted as M15 and never repaired client-side); `unknown_tool`. |

**On the six provider-failure classes.** They are recorded as *classes* in the
manifest: `ProviderUnavailable`, `ProviderTimeout`, `ProviderAuthenticationError`,
`ProviderRateLimited`, `ProviderResponseError`, `ProviderServerError`. Section 18.1
is about the fact that the local provider records only the exception's *type name*
and discards the HTTP body that would distinguish them, so `ProviderResponseError`
genuinely carries two very different causes. The vocabulary is not the problem; the
discarded message is.

**On the secondary identity.** The manifest names a secondary provider,
`stepfun`, with status *"NOT RUN unless STEPFUN_API_KEY is present; never paired with
the primary."* It was not run. The credential was not sought, not read and not
tested for, and every number in this report comes from the local primary. Under the
Phase 5 security rule, a missing credential is reported as `NOT RUN — credential
unavailable` rather than hunted for.
