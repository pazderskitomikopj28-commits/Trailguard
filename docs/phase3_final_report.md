# TrailGuard Phase 3 Final Report

**Phase 3 — Agent Skill Runtime & Tool Orchestration.**
Frozen spec: `TrailGuard_比赛版开发冻结规格_v2.1.md` (sections 4, 14.2, 15, 16, 18, 20,
21, 25). Previous freeze: `7e550fd` on `main`, tag `phase2-freeze`.

What Phase 3 had to deliver: one harness that lets a real model call the five frozen
atomic tools, records what it did into an evidence trace, and runs the two conditions
of the single-variable experiment — **same task, same context, same tools, same model
parameters, only `SKILL.md` differing**. What it had to refuse: giving the harness any
of the skill's expertise, because *the harness is a generic executor and the Skill is
the expert workflow*.

The verdict is at §16. Everything above it is the evidence, including the two places
where the runtime exposed a gap in an earlier phase's contract (§15).

---

## 1. Repository State

```
On branch main
HEAD: 7e550fd  feat: route-aware weather and future projection (Phase 2)
tag:  phase2-freeze (present), no phase3 tag — nothing is committed (§59)
```

Modified (15 files, +1239 / −210):

```text
.gitignore | README.md | docs/architecture.md | docs/contracts.md | tests/conftest.py
tests/test_harness.py | tests/test_providers.py | trailguard/agent/harness.py
trailguard/agent/skill_loader.py | trailguard/agent/trace.py
trailguard/providers/__init__.py | trailguard/providers/base.py
trailguard/providers/local.py | trailguard/providers/stepfun.py
trailguard/schemas/trace.py
```

New (9 files):

```text
docs/agent_runtime.md                    the Phase 3 runtime document (§53)
scripts/test_stepfun_agent.py            the live smoke (§44, §45) — never pytest-collected
tests/test_agent_runtime.py              30 tests: what a real run puts in the trace
tests/test_agent_smoke.py                13 tests: the offline smoke stays honest
tests/test_replay_runner.py              24 tests: the driver is a driver, not a tool
tests/test_stepfun_provider.py           26 tests: the StepFun transport, 0 real calls
trailguard/agent/replay_runner.py        Scenario -> run_agent() per condition -> runs/phase3/
trailguard/agent_smoke.py                python -m trailguard.agent_smoke
```

Not present, correctly: `runs/` (git-ignored, and no live run happened), `demo/`
(Phase 6), any local-model wiring (Phase 4).

Nothing is committed and nothing is pushed (§59). The working tree is intentionally
left for final acceptance.

## 2. Agent Harness

`trailguard/agent/harness.py::run_agent()` is the single entry point through which
**both** conditions run:

```python
run_agent(*, task: str, context: TrailGuardContext, model_provider: ModelProvider,
          skill_enabled: bool = True, skill_path: Path | None = None,
          tools: dict[ToolName, Callable[..., Any]] | None = None,
          max_steps: int = 12, temperature: float = 0.0, max_tokens: int = 2048,
          scenario_id: str | None = None, started_at: datetime | None = None,
          tool_failure_policy: ToolFailurePolicy = "report") -> AgentRunResult
```

* **System prompt:** `HARNESS_BASE_SYSTEM_PROMPT` + the `SKILL.md` text when
  `skill_enabled=True`, and nothing added when it is `False`. The base prompt is
  runtime mechanics only and deliberately carries none of the treatment vocabulary —
  no product identity, no horizon words, no freshness budget, no uncertainty rule, no
  projected-risk-state wording. One generic, domain-neutral sentence is shared with
  the baseline on purpose ("never invent, extrapolate or fill in a value that was not
  returned by a tool"): weakening the baseline to flatter the treatment is as much a
  fairness violation as contaminating it. The substance of the missing-data protocol
  — list every gap, mark stale inputs, lower confidence, answer `insufficient_data` —
  is entirely the Skill's.
* **The loop:** up to `max_steps` turns. Each provider response either carries tool
  calls (append the assistant turn *with its calls*, execute each, append each result
  keyed by call id, continue) or carries content (stop, `completed`). A provider
  exception stops with `provider_error` and no answer.
* **The model decides everything inside the loop**: which tool, with which arguments,
  how many horizons, and what to conclude. The harness owns two things only — the
  mechanics of feeding results back, and the record of what happened.
* **It does not** loop the horizons, fix a tool order, auto-fill missing calls, retry
  a failed call, repair arguments, substitute a different tool, echo the model's input
  back as a tool result, or write the final answer. `tests/test_harness.py` asserts
  each of those behaviourally, and the two that can only be fenced structurally live
  in `tests/test_agent_runtime.py`
  (`test_the_harness_does_not_iterate_the_horizons`,
  `test_the_harness_never_names_a_tool_in_a_string_literal`).

## 3. StepFunProvider

`trailguard/providers/stepfun.py` (492 lines changed; 26 tests, 0 real calls):

* OpenAI-compatible chat completions against `DEFAULT_BASE_URL = https://api.stepfun.com/v1`,
  with the model id taken from the argument or `STEPFUN_MODEL` in the environment
  (`.env.example` ships `step-5-preview`, per spec 16), and `temperature` and
  `max_tokens` applied to every request. Tool schema dicts are passed through as the
  `tools` argument, and a request with no model id is refused rather than sent with a
  default the caller never chose.
* **Failures are a typed hierarchy**, and `retryable` is data rather than behaviour:
  `ProviderTimeout` (connect/read timeout, `DEFAULT_TIMEOUT_S = 120.0`),
  `ProviderUnavailable`, `ProviderRateLimited` (429), `ProviderServerError` (5xx) are
  retryable; `ProviderAuthenticationError` (401/403, or no credential at call time) and
  `ProviderResponseError` (the endpoint answered and the answer was unusable) are not.
* **Retry budget is exactly one immediate retry** (`DEFAULT_MAX_ATTEMPTS = 2`), spent
  only on the five classes a retry can fix. The SDK's own loop is disabled
  (`max_retries=0`) so the documented budget is the whole budget; there is no backoff
  sleep, because a benchmark run must not stall on a flapping endpoint.
* `openai` is imported lazily inside `_client()`, so importing this module requires
  neither the SDK nor a credential, and no client is bound at import time.
* The credential lives in one private attribute, is masked by `__repr__`, never
  appears in `name`, and never reaches a trace, a log or an exception message — every
  failure names the *kind* of failure and the endpoint, never the key that was rejected.
* Exercised entirely over `httpx.MockTransport` with a stubbed `openai` module, so the
  suite runs offline (§11).

The two backends of the frozen `2 models x 2 conditions` matrix sit behind one
`ModelProvider.generate(messages, tools, temperature, max_tokens)` signature, so no
caller can branch on which backend is loaded — that is what keeps the matrix fair
(spec 14.2, 3.1).

## 4. Tool Calling Runtime

The lifecycle of one call, in order:

1. **Name resolution** — the requested name must be a member of the frozen `ToolName`
   registry. Anything else cannot be represented in the trace, so it is reported back
   to the model as an error and never counted as a tool call.
2. **Registry lookup** — a tool the run was not handed (`tools=`) fails as *not
   provided to this run*: a registry entry the harness advertised but nobody supplied
   is a configuration error, not a tool failure.
3. **Contract validation** — arguments are validated against the *same* `input_model`
   the advertised JSON schema describes. What the model was shown is what its
   arguments are checked against, so a malformed call fails with the contract's own
   message instead of reaching a tool that cannot interpret it.
4. **Execution and recording** — a typed result is serialised whole (its size is
   bounded by its schema; truncating it would show the model a *cut* safety document
   as if it were the whole one) and an untyped payload is capped at
   `_MAX_SUMMARY_CHARS = 600`.

Five outcomes are recorded, which is what makes a hallucinated tool call
distinguishable from a broken endpoint: `ok`, `invalid_tool_call` (the model's
arguments failed the contract), `unknown_tool` (the model named a tool that does not
exist), `tool_error` (the tool raised), `not_implemented` (a Phase 0 stub raised
`NotImplementedError`). The five `stop_reason` values — `completed`, `max_steps`,
`provider_error`, `tool_error`, `invalid_tool_call` — are asserted in
`tests/test_agent_runtime.py`, including `tool_failure_policy` never being derived from
`skill_enabled`.

`max_steps` works and is observable: a run that exhausts it stops with `max_steps` and
records how many steps it took, so a run that never finished is never mistaken for one
that finished badly.

## 5. Skill Loading

`trailguard/agent/skill_loader.py` (+85 lines) loads the text and returns it raw. It
exposes **no API for extracting** rules, thresholds, horizons or algorithms from the
skill — the moment Python starts reading horizons or freshness budgets out of the
skill, the two conditions stop differing by one variable.

What it does expose is *provenance*: the `version:` line of the skill's frontmatter and
a SHA-256 of the exact bytes that were injected. The hash is taken over the string that
was loaded — the string the harness injected — rather than over a re-read of the file,
because those are the same bytes only if nothing rewrote the file in between.
`tests/test_harness.py` proves the distinction with a variant skill whose frontmatter
version is identical and whose body is not: the recorded version is unchanged and the
hash moves.

`SKILL.md` itself is **byte-identical** to its Phase 0 version (§12, Q15).
Phase 3 findings were recorded instead of being "fixed" by editing the skill, because
the skill is the treatment and writing the treatment to accommodate the runtime would
have made the experiment measure itself.

## 6. No-Skill / With-Skill Fairness

One argument, `skill_enabled`, is the only value that differs between a baseline run
and a treatment run of the same scenario:

| Identical in both | Where |
| --- | --- |
| task text | `scenario.task_prompt` |
| context snapshot (byte-identical JSON) | `scenario.initial_context` |
| tool registry and therefore `tool_schema_dicts()` | `scenario_registry(scenario)` |
| `temperature`, `max_tokens`, `max_steps`, `tool_failure_policy` | fixed once per batch |
| `HARNESS_BASE_SYSTEM_PROMPT` | `harness.py` |
| provider, model string, transport, timeout | `ModelProvider` argument |

There is one `run_agent()` and one code path. There is no `run_baseline_agent()`, no
second harness, no second skill variant, and no hidden hint in the treatment prompt
(the treatment is the skill text verbatim, hashed for provenance).

`evals/compare.py` refuses to compare a pair that fails any of its four preconditions
— same scenario ids, same model, same offered registry, same sampling parameters — so
a comparison that is not single-variable is refused rather than reported with a
caveat.

`scenario_registry()` binds the scenario's declared weather policy **from the
scenario**, never from `skill_enabled`, so both conditions of one scenario receive
byte-identical registries; `test_both_conditions_of_a_scenario_get_the_same_registry`
compares what the bound entry *is* (`.func`, `.keywords`) rather than `==`, because
`functools.partial` falls back to identity equality and would have reported a fairness
violation that did not happen.

## 7. Evidence Trace

`TraceCollector` (`trailguard/agent/trace.py`, +68 lines) is the only writer of
`EvidenceTrace`, and the boundary it keeps is the important part of Phase 3:

* **It records what a tool returned.** A successful `assess_risk` contributes its own
  `horizon`, `hard_rules_triggered`, `drivers`, `confidence`, `risk_level` and
  `safety_margins.weather_freshness_min`; a successful `get_weather_for_route_time`
  contributes its `source`.
* **The one comparison it makes is the freshness verdict** — the age the core actually
  used against the snapshot's own `WeatherPolicy.max_freshness_min`. "Stale" is
  meaningless without the budget it was measured against. An age the core could not
  compute leaves `freshness_ok` as `None` ("not checkable") rather than silently
  passing as fresh.
* **`core_risk_level` / `core_confidence` are the tools'; `final_risk_level` /
  `confidence` are the agent's claim.** The harness fills the first pair and leaves the
  second for the Phase 5 evaluator. It does not read a verdict out of a paragraph: a
  run whose answer happens to contain "RED" must not get a RED trace entry, and a
  harness with an opinion about English would make the honesty metric circular.
  `test_the_harness_does_not_write_the_agents_claimed_verdict` pins this.
* **`missing_data` is left empty on purpose.** Which fields were absent *at this
  horizon* is the Skill's missing-data protocol; the harness has a whole-run snapshot,
  and copying its `missing_fields` onto one horizon would attribute run-level gaps to a
  moment that did not experience them.
* **Determinism for replay (spec 48):** every trace records `temperature`, `model`,
  `provider`, `max_steps`, `started_at`/`finished_at`, the scenario id, the skill
  version and the skill SHA-256 — everything a Phase 5 run needs to be repeated.
* `EvidenceTrace` round-trips through JSON with every field preserved
  (`tests/test_trace_schema.py::test_trace_serializes_to_json_and_back`), and the
  trace is what the saved run file contains.

## 8. Replay Runner

`trailguard/agent/replay_runner.py` (~470 lines, 19 tests) turns scenarios into runs:

* `load_scenario(path)` validates the YAML against the frozen `Scenario` contract, so a
  typo fails at the boundary rather than surfacing as a run against a half-parsed
  snapshot.
* `scenario_registry(scenario)` returns the canonical registry with the scenario's own
  weather policy bound in (§15, F1).
* `replay_scenario(...)` runs one scenario in one condition through the same
  `run_agent()` and writes `runs/phase3/<scenario_id>/<condition>.json`.
* `replay_dataset(...)` repeats that across the dataset and writes a batch summary
  (`summary_<UTC stamp>.json`) that is an *index* — run ids, stop reasons, structural
  findings — never a second opinion.
* `final_answer_findings(answer)` reports string facts: length, emptiness, which
  canonical horizon labels appear, whether the banned accident-probability phrase
  appears.

Two things the driver deliberately does not do, both of which would have broken the
experiment:

* **It never calls a tool.** A driver that called tools itself would be handing the
  baseline a plan, and the measured difference would be the driver's, not the skill's.
  `tests/test_replay_runner.py` asserts the seam *structurally*, by parsing the module
  with `ast` and requiring that `_execute_tool_call`, `record_core_verdict` and
  `assess_risk` never appear as executable names in it.
* **It never scores a run.** `mentions_banned_claim` is a *presence* scan and is named
  accordingly: "the accident probability is 3 %" and "there is no accident probability
  to report" both contain the banned phrase, and no substring test can tell a claim
  from a denial. An earlier revision named the field `makes_banned_claim`, which scored
  a careful denial as a protocol violation — a benchmark that punishes disclosure is
  worse than no benchmark. Both sentences set the field, and
  `test_the_banned_phrase_is_reported_as_present_even_when_it_is_denied` pins the
  limitation rather than hiding it.

Without `STEPFUN_API_KEY`, `python -m trailguard.agent.replay_runner` reports
`NOT RUN -- credential unavailable: STEPFUN_API_KEY is not set.` and exits 2, writing
no trace and fabricating none (spec 17: a frozen snapshot substituted for a live call is
a fabricated answer wearing a reproducible costume).

## 9. Offline Agent Smoke

`python -m trailguard.agent_smoke` — **executed for real, exit 0, 20/20 checks passed.**

```text
TrailGuard Phase 3 agent runtime smoke -- OFFLINE
scenario   : scenarios/baseline/scn_clear_day_normal.yaml
device feed: scn_device_S01_clear_day
model      : ScriptedProvider (offline stub) -- NO REAL MODEL WAS CALLED

[PASS] the scenario registry holds exactly the frozen five atomic tools
[PASS] binding the scenario's weather policy left the canonical registry untouched
[PASS] both conditions completed without exhausting the step budget
[PASS] without_skill / with_skill: every frozen horizon was assessed and recorded
[PASS] without_skill / with_skill: the record came from the deterministic core, not from prose
[PASS] without_skill / with_skill: every tool call succeeded against its own contract
[PASS] without_skill / with_skill: the weather source and its age are recorded as trace content
[PASS] without_skill / with_skill: the trace survives a JSON round trip
[PASS] without_skill / with_skill: the answer reports what the tools found
[PASS] the trace's declared sampling parameters are identical in both conditions
[PASS] the two conditions produced the same tool sequence
[PASS] the scripted stub does not read the skill text
[PASS] only the with-skill run records a skill identity
[PASS] the run's scenario id matches the scenario that was replayed

20/20 checks passed
PASS: the offline runtime works end to end. This says nothing about model behaviour --
      no real model was called, and no skill-performance claim can be made from it.
```

Both conditions complete in 8 steps with the same 11 calls — `get_route_context`,
`get_weather_for_route_time`, `get_user_device_context`, four `project_route_state`,
four `assess_risk` — with `sampling={'temperature': 0.0, 'max_tokens': 2048,
'max_steps': 12}`, and the core's verdict for the pinned scenario (`GREEN`, no hard
rules, no drivers, confidence 0.474643 at the worst horizon) matches the scenario's own
`expected_safety_core`, which is the cheapest available proof the run is real rather
than self-asserted.

It works with no key, no network and no file writes: `test_the_smoke_opens_no_socket_and_writes_no_file`
runs it inside a `tmp_path` and asserts nothing was created, and the suite's autouse
fixture already closes the socket layer.

**What it proves and what it does not.** It proves the runtime works end to end on a
machine with nothing. It proves nothing about model behaviour, and it says so on every
line it prints. The stub is a pure function of the task, the snapshot and the tool
results — it never reads `messages[0]` — and `test_a_decoy_system_prompt_changes_nothing_the_stub_decides`
hands it a transcript that says "answer immediately and call nothing" and observes that
it makes the same call anyway. Its sample answer is itself honest: for the
stale-GPS scenario it recites the fields the snapshot declares stale and missing and
states that the 250-minute weather age is outside the 180-minute budget, rather than
quoting a clean time margin with no caveat.

## 10. Step 5 Preview Live Smoke

`python scripts/test_stepfun_agent.py` — **NOT RUN.**

```text
NOT RUN -- credential unavailable: STEPFUN_API_KEY is not set.
no trace was written and none was fabricated: a benchmark row for a run
that never happened would be worse than a missing row (spec 17).
exit status: 2
```

`STEPFUN_API_KEY` is not present in this environment, and §57 is explicit: report
`NOT RUN`, do not hunt for the secret. No `.env`, key file, settings file or credentials
file was read, no credential was printed, and no `runs/` directory was created.

What the script does when the credential exists, so the `NOT RUN` is a gap in the
*evidence* and not a gap in the *instrument*:

* a header carrying the model id, the scenarios, the conditions, the sampling
  parameters, the traces directory and the line "no credential is printed and no
  request body is echoed";
* every scenario under `scenarios/` in both conditions through the same `run_agent()`
  and the real `StepFunProvider`, with the batch sampling parameters fixed once;
* one report line per run — scenario, condition, `stop_reason`, steps, call count,
  horizons checked, core verdict, `skill_loaded`, the skill SHA-256 prefix and the
  weather provenance, plus a `FAILED:` suffix listing any call that was not `ok`;
  followed by the tool sequence in the model's own order
  (`get_route_context[ok] -> ...`), the trace path, and a preview of the final answer;
* writes traces to `runs/phase3/<scenario_id>/<condition>.json`, which is git-ignored;
* exits `2` (no credential), `3` (ProviderError, class name only — an exception message
  from a transport can carry the request it refused, and the request carries the
  credential), `4` (a run did not complete) or `0` (both conditions completed);
* **does not score the comparison** — no statistical significance, no superiority
  claim. That is Phase 5's job, and the footer says so.

The report line and the call-sequence line are exercised offline, before a live run can
rely on them: `tests/test_replay_runner.py` imports the script by path (pytest still
does not *collect* it), builds a real `ReplayRunSummary` with the scripted provider and
asserts the line's contents — including that a decoy `STEPFUN_API_KEY` in the environment
appears neither in the console line nor in the saved trace.

It is never collected by pytest: `tests/conftest.py::collect_ignore` lists both
`../scripts/test_live_weather.py` and `../scripts/test_stepfun_agent.py`, and
`pytest scripts/ --collect-only` reports *no tests collected in 0.06s*.

## 11. Security & Credential Handling

* **Reads:** nothing outside the repository. No `.env`, no API key file, no token, no
  `~/.claude/settings.json`, no SSH key, no credentials file — in any script, in any
  test, and in this report.
* **Writes:** no secret appears in code, in a trace, in a log, in a commit or in this
  document. `StepFunProvider` never puts the key in `name` or `__repr__`, and
  `trailguard.schemas.trace` carries `_redact_secret_text`, which the live smoke uses
  for console output so the console is held to the same standard as the trace.
* **Runtime:** the key is read from the environment at the one place a provider is
  built (`_provider_from_environment()`), outside pytest entirely.
* **Network:** `python -m pytest` makes zero external calls. `tests/conftest.py` closes
  `socket.connect`/`connect_ex`/`create_connection`/`getaddrinfo` autouse, so the
  guarantee is structural rather than promised; the StepFun transport is exercised over
  `httpx.MockTransport` with a stubbed `openai` module. The only paths that may open a
  socket are `scripts/test_live_weather.py` and `scripts/test_stepfun_agent.py`, neither
  of which pytest collects.
* **Artefacts:** live traces go to `runs/phase3/`, ignored by `.gitignore` — a trace can
  quote a model answer, and a directory that grows with every live run has no business
  in version control.
* **Git:** no commit, no push (§59). Only the working tree was left for acceptance.

## 12. Frozen Asset Verification

Verified with `git diff --name-only HEAD -- <paths>` (empty output) and byte-level
SHA-256:

| Asset | Status | SHA-256 |
| --- | --- | --- |
| `SKILL.md` | byte-identical to Phase 0 | `98fd007eb23aee18f98bf67c295d7bad3c8e48f7a722022c852218fb0c649769` |
| `trailguard/rules/rules_v1.yaml` | byte-identical | `b2630f3d32e50d6533a01172fbdd4f5db670fcdc474a2c8ebbf1bcebda71a5d4` |
| `trailguard/rules/calibration_v1.yaml` | byte-identical | `05bb20a16d7315c7693b0eb8b642ac3fa810555c259add43833c90d77c553ec5` |
| `replay/weather/scn_weather_snapshot.json` | byte-identical (Phase 0 fixture, never rewritten) | `817543047a1b002d8fece6eaa9ca0cffe76500a5d964f46deb29fa5e97ee23bd` |

Also unchanged: `scenarios/`, `replay/routes/`, `replay/devices/`, every tool body and
every tool *contract*, the safety core, the route geometry and the weather providers.
The one intentional, additive change anywhere near a frozen surface is on the trace
schema itself, which Phase 3 owns: `EvidenceTrace` gained three optional fields with
defaults (`core_risk_level`, `core_confidence`, `skill_sha256`) so every existing trace
still validates — the same additive-only rule Phase 2 followed, and the reason a Phase 5
run can tell "what the tools produced" from "what the agent claimed" at all. Nothing was
removed, renamed, retyped or re-defaulted.

**No Qwen integration.** `grep -rni "qwen|127.0.0.1:8000"` finds only prose in docs
stating that the local model stays isolated, and nothing at all in `trailguard/` or
`scripts/`. `LocalModelProvider` remains a Phase 4 stub: its `generate()` raises
`NotImplementedError("Phase 4: local 4B-8B open-source model served on DGX Spark")` and no
product code path constructs it — the replay driver's `_provider_from_environment()`
builds `StepFunProvider` and nothing else.

The frozen spec itself (`TrailGuard_比赛版开发冻结规格_v2.1.md`, outside the repository) was
not modified; its hash at writing time is
`09a453ad0eda4882129ba510abb0c1933dd07d7038824343e9810d19d6603d07`.

## 13. Test Results

```text
$ python -m pytest -v
collected 642 items
642 passed
0 failed, 0 skipped, 0 xfailed, 0 warnings
time: 8.70 s
```

Baseline was 544 → **642 tests, +98 new**, with zero unexplained regressions: nothing
was deleted, weakened or skipped to get to green. New test modules:

| Module | Tests | What it pins |
| --- | --- | --- |
| `tests/test_agent_runtime.py` | 30 | the trace comes from real execution; freshness judged against the snapshot's own budget; the core's verdict, not prose; the model cannot rewrite the trace after the fact; both failure policies; provider failure = `provider_error`; call-id pairing and provider order; no horizon loop, no tool name in a string literal, the skill text verbatim or nothing |
| `tests/test_stepfun_provider.py` | 26 | the whole transport over `httpx.MockTransport`: retryable vs non-retryable taxonomy, one-retry budget, secret masking, `name`, error mapping, tool-call and content parsing |
| `tests/test_replay_runner.py` | 24 | findings are facts; the registry holds exactly five tools and never mutates; the frozen weather contract has no field for the scenario's mode; the driver never executes a tool or reads the skill; traces land in the git-ignored tree; `NOT RUN` without a credential; **the live smoke's report line works before a live run and prints no credential** |
| `tests/test_agent_smoke.py` | 13 | every smoke check passes for two shipped scenarios; the core agrees with the pinned expectation; the two conditions differ only by the skill; no socket, no files; the answer discloses its staleness and not staleness it does not have; the command line labels itself offline |

The StepFun tests make **0 real StepFun calls**: every one goes through a fake transport
or a stubbed SDK module, so the suite is unaffected by endpoint availability.

## 14. Adversarial Self-Audit (Q1–Q17)

**Q1 — Does the harness have any horizon loop?**
No. There is no `for` over `Horizon` in `harness.py`; the loop is over *provider
responses*, and the number of iterations is the model's choice. Two tests assert it
directly: `test_the_harness_does_not_iterate_the_horizons` and
`test_the_harness_never_names_a_tool_in_a_string_literal` (which forbids even the
*spelling* of a horizon or tool name in a harness literal). The four horizons appear in
the smoke's trace because the stub chose to assess four, not because the harness did.

**Q2 — Does the harness fix a tool-call order?**
No. The order is whatever the model emitted, verified two ways: the driver test proves
the driver never calls a tool, and the smoke's tool sequence is a property of the
stub's policy rather than of the harness (a decoy system prompt changes nothing).
Nothing in the harness mentions `get_route_context` before `assess_risk`.

**Q3 — Are No-Skill and With-Skill really the same `run_agent()`?**
Yes — one function, one code path, one file. `replay_scenario()` and the offline smoke
both call `run_agent()` and vary only `skill_enabled`; the smoke's
`test_the_two_smoke_conditions_differ_only_by_the_injected_skill` asserts identical
sampling parameters and identical tool sequences across the two conditions.

**Q4 — Is `SKILL.md` the only conditional difference?**
Yes. `skill_enabled=True` appends the loaded skill text to `HARNESS_BASE_SYSTEM_PROMPT`;
`skill_enabled=False` appends nothing. The base prompt is byte-identical in both, and
`tests/test_harness.py::TREATMENT_VOCABULARY` asserts the absence of the treatment words
from it. The registry, task, context, sampling parameters, provider and failure policy
are all arguments fixed outside the condition loop.

**Q5 — Are the tool schemas identical?**
Yes, byte for byte, because both conditions receive the same dict — the one
`scenario_registry(scenario)` returns, which is derived from the scenario and never from
`skill_enabled`. `test_both_conditions_of_a_scenario_get_the_same_registry` compares the
bound entry's `.func` and `.keywords`.

**Q6 — Are model / temperature / max_tokens / max_steps identical?**
Yes, and identical *sampling parameters are recorded in the trace*, so this is auditable
rather than asserted: the smoke prints
`sampling={'temperature': 0.0, 'max_tokens': 2048, 'max_steps': 12}` for both
conditions, and `test_the_two_smoke_conditions_differ_only_by_the_injected_skill` compares
them.

**Q7 — Was the baseline weakened?**
No. The baseline gets the same prompt mechanics, the same five tools, the same schemas,
the same step budget and the same failure policy. The one sentence it shares with the
runtime is domain-neutral tool etiquette. It is a normal general-purpose agent facing
the same task.

**Q8 — Does the treatment have a hidden hint?**
No. The treatment prompt is `HARNESS_BASE_SYSTEM_PROMPT` + the skill text verbatim, and
the skill's SHA-256 is recorded so any reader can verify the exact bytes injected. There
is no appended nudge, no tool-name hint, no ordering hint. (The skill itself is *meant*
to teach the protocol — that is the treatment, and it is the claimed source of any
improvement.)

**Q9 — Do all tool calls really come from the model's structured output?**
Yes. Every call is parsed from the provider's `tool_calls`; the harness invents none. The
runtime tests drive `run_agent()` with hand-built `ModelResponse` objects and check that
the trace records exactly the calls the model made, in the model's order, paired by call
id. The AST guard proves the driver never executes a tool itself.

**Q10 — Is the evidence trace from real execution, not the model's self-report?**
Yes. Fields are typed copies of tool results, and the harness never reads a verdict out
of prose: `test_the_trace_records_tool_output_not_the_models_description_of_it`,
`test_the_model_cannot_rewrite_the_trace_after_the_fact` (a model reply claiming a
different verdict changes nothing in the trace), and
`test_a_completed_run_leaves_the_claimed_verdict_to_the_evaluator`.

**Q11 — Does the safety core decide the risk result?**
Yes. `assess_risk` delegates to `DeterministicRiskEngine` with thresholds from
`rules/rules_v1.yaml` and evaluation metadata from `calibration_v1.yaml`; the harness
copies the result and the core's confidence verbatim. For the pinned clear-day scenario
the core's verdict (`GREEN`, confidence 0.474643, no hard rules) is what the trace
records, and it agrees with the scenario's own pinned expectation.

**Q12 — Can the model modify a hard rule?**
No. Hard-rule severity floors are set inside the core; the model never touches the rule
pack, cannot see it, and has no tool that changes it. The only path a "hard rule not
softened" check exists is one that observes the core's output — it does not rewrite
anything (spec 46: the harness must not rewrite an answer to pass a check). The core
itself was not modified in Phase 3 (§51).

**Q13 — Are provider error / tool error / max_steps observable?**
Yes, as five distinct `stop_reason` values plus five tool outcomes, each with its own
test: `test_a_provider_failure_still_means_provider_error`,
`test_stop_on_failure_ends_the_run_with_tool_error`,
`test_stop_on_an_unknown_tool_ends_the_run_with_invalid_tool_call`,
`test_stop_on_arguments_the_contract_rejects_ends_the_run_with_invalid_tool_call`,
`test_max_steps_is_enforced` (in `tests/test_harness.py`) and
`test_the_failure_policy_is_the_same_in_both_skill_conditions`. The live smoke prints the
stop reason for every run and exits non-zero if a run did not complete.

**Q14 — Can an API key reach the trace or the log?**
No. The key lives in one private provider attribute, is masked by `__repr__`, is never
returned by `name`, and no trace, log or exception message contains it; the live smoke's
console output passes through `_redact_secret_text`. The credential-shaped path is
excluded from pytest collection, and no test reads it.

**Q15 — Is `SKILL.md` byte-unchanged?**
Yes — SHA-256 `98fd007eb23aee18f98bf67c295d7bad3c8e48f7a722022c852218fb0c649769`, and
`git diff --name-only HEAD -- SKILL.md` is empty (§12).

**Q16 — Was Qwen integrated early?**
No. `LocalModelProvider.generate()` raises `NotImplementedError` naming Phase 4,
nothing outside tests constructs it, and `grep -rni "qwen"` over the working tree
returns only prose. (An earlier revision of this answer said `127.0.0.1:8000`
"appears only in test fixtures and in prose"; no test fixture contains that string
— the only occurrences are `docs/`, so the claim was cut back to what is true.)

**Q17 — Was benchmark aggregation implemented early?**
No. There is no aggregation, no scoring, no metric computation and no statistical test
anywhere in Phase 3. `evals/` was not touched. The batch summary is an index of run ids
and stop reasons, and the live smoke explicitly declines to compare the conditions.
`final_answer_findings` reports string facts only and names its own limitation (§8).

### The audit these answers were checked against

This phase was audited by reviewers with no stake in it: five dimensions (happy path,
fairness, boundary conditions, error/failure, requirement coverage), nineteen agents,
twenty-five raw findings. **Seven survived independent verification and were fixed; one
claimed finding was refuted by re-running it.** Every fix was then *mutation-tested* —
the code was deliberately broken to confirm the test that is supposed to catch it
actually fails — because a test added in the same commit as a bug can be written to
agree with the bug. All fences hold.

| Finding | What was wrong | Fix |
| --- | --- | --- |
| tie-break in `worst_core_verdict()` | docstring promised "ties keep the first-seen order"; `sorted(...)[-1]` returned the **last** of a tied group, so the reported confidence could belong to a different horizon than the reported level | rewritten as `max(...)`, which returns the first maximal element, plus three tests (different levels, order-independence, and an exact-rank tie) |
| `tool_failure_policy="stop"` had zero coverage | mutating `if tool_failure_policy == "stop"` to `if False` left the whole suite green | three tests, one per stop reason it produces |
| a test that asserted nothing | the empty-dataset test never reached the branch it named | rewritten to reach the real branch via a temp `REPO_ROOT` |
| `length_chars` measured a different string than was saved | the answer was measured before redaction but written after, so a run file could disagree with itself | measured on the redacted text; a new test carries a decoy secret through the redactor |
| `tool_error` conflated two owners | "the tool raised" and "the harness advertised a tool nobody supplied" shared one row with no way to tell them apart | recorded honestly in the doc, since the trace's five-outcome vocabulary is frozen; the error string names the case |
| the horizon-loop guard was blind | it inspected `ast.comprehension.iter` only, so `[c.horizon for c in x]` passed | it now dumps the enclosing comprehension node, and the mutation was re-run to confirm it fails |
| five documentation defects | stale test counts, a device-feed filename, a non-existent test name, an over-claimed `127.0.0.1:8000` assertion, and a `runs/phase3/` description implying runs exist | corrected, each with the correction recorded in place rather than quietly replaced |

Two things this audit does *not* establish, said plainly: the audit was of Phase 3's own
code against Phase 3's own spec, and it says nothing about whether the Skill works — that
remains Phase 5's question (F4, F5 below). It also did not touch F1 or F2, which are
frozen-contract gaps belonging to the phases that own those contracts.

## 15. Known Limitations

**F1 — `GetWeatherForRouteTimeInput` cannot express the scenario's weather mode.**
The contract carries exactly `lat`, `lon`, `target_time`. `WeatherPolicy` (mode +
`snapshot_ref`) is how a scenario says where weather may come from, and the tool's
*Python* signature accepts it as a keyword-only `policy=` — but the contract the harness
validates the model's arguments against does not. A replay scenario replayed through
the bare `TOOL_FUNCTIONS` registry therefore falls back to the documented live default:
it queries live weather instead of its own frozen snapshot (which made the smoke fail
with a live date-range error during development, and which would make any benchmark run
non-reproducible). `scenario_registry()` closes the gap at the *registry* level using
the tool's own DI seam — no contract change, nothing hidden from the model: the
advertised schema is still `lat`/`lon`/`target_time` and the mode is the one the
scenario already declared. This is a Phase 1/2 contract gap recorded for the phase that
owns it; `test_the_frozen_weather_contract_has_no_field_for_the_scenarios_mode` is the
assertion to delete when the field is added.

**F2 — `get_user_device_context` requires an id the agent is given nowhere.** The id is
a file stem under `replay/devices/`. `Scenario` declares `route_ref` and
`weather_snapshot_ref` but no `device_feed_ref`, and the context snapshot carries no
device id — so a faithful agent cannot know which feed to ask for and will record a
failed tool call. Both conditions are affected identically, so the comparison stays
fair, but a live run's device-call failure is a *contract gap*, not a model mistake. The
offline smoke passes the feed id explicitly (`--device-id`) and names the one it used.

**F3 — `mentions_banned_claim` is a presence scan.** It cannot tell a claim from a
denial; both set it. Deliberately documented on the type and pinned by a test rather
than fixed by pretending a substring test can resolve negation. Real judgement of the
disclosure protocol is Phase 5's.

**F4 — the offline smoke is not evidence about the skill.** It proves the runtime and
nothing about model behaviour. No skill-performance claim is available from this phase.

**F5 — no live run happened.** §10 is `NOT RUN`; the live path is implemented and
tested offline but has not been executed against Step 5 Preview, so nothing here
reports what a real model did with the tools.

**F6 — `docs/contracts.md` was stale about `assess_risk`.** It listed `horizon_min`,
`weather_freshness_min`, `data_quality` and `engine` as if they were contract fields;
they are keyword-only *Python function* parameters and are unreachable through the
harness's contract validation. Corrected, with the correction recorded in the document
rather than papered over, because a reader could otherwise believe a model can select a
horizon by argument. (Which horizon is assessed is a property of the data: the
`projected_state` horizon, or `NOW` when none was passed.)

**F7 — dataset size.** Three scenarios ship. A statistically meaningful no-skill vs
with-skill comparison needs the 30–50 scenarios of spec 13; that is Phase 3+ dataset
growth and Phase 5's work, not this phase's claim.

## 16. Final Verdict

```text
PASS — Ready for Phase 4
```

Against the §61 checklist:

```text
[x] same run_agent handles both conditions
[x] StepFunProvider implemented
[x] structured tool calls supported
[x] tool results returned to the model
[x] multi-step loop works
[x] max_steps works and is observable
[x] unknown / invalid tools handled, and distinguishable from a broken tool
[x] five existing tools unchanged in responsibility
[x] no horizon loop in the harness
[x] no hardcoded tool order
[x] SKILL loaded only when skill_enabled
[x] No-Skill baseline not weakened
[x] tools identical in both conditions
[x] model parameters identical in both conditions
[x] EvidenceTrace populated from real execution
[x] trace sanitised (no key, no secret)
[x] replay runner works
[x] offline agent smoke exit 0
[x] full pytest green (642 passed, 0 failed, 0 warnings)
[x] frozen assets byte-identical
[x] LocalModelProvider still deferred
[x] no Qwen integration
[x] no benchmark aggregation
[ ] live StepFun smoke  — NOT RUN, credential unavailable (§10); recorded, not simulated
```

The single unchecked box is the live smoke, and it is unchecked for an environmental
reason — `STEPFUN_API_KEY` is not present — rather than an implementation one. Per §57
it is reported as `NOT RUN` with no secret hunting, no fabricated trace and no claim
about what a real model did.

Per §62: Phase 3 stops here. No Qwen, no bulk benchmark, no Streamlit, no skill
modification, no Phase 4 work, no commit and no push.
