# The Agent Runtime (Phase 3)

**Status: Phase 3 (agent skill runtime + tool orchestration).** One harness,
`run_agent()`, now drives real tool calls from a real model, in two conditions
that differ by exactly one thing. This page explains the runtime, what it
records, what it refuses to infer, and how to tell an honest comparison from a
contaminated one.

Source of truth: `trailguard/agent/harness.py`, `trailguard/agent/replay_runner.py`,
`trailguard/agent/skill_loader.py`, `trailguard/agent/trace.py`,
`trailguard/providers/`. Spec references point to
`TrailGuard_比赛版开发冻结规格_v2.1.md`.

The question Phase 3 exists to answer (spec 3.1, 15): **under the same task, the
same context, the same five atomic tools and the same model parameters, does
loading the TrailGuard Skill make the agent perform correct multi-step tool
orchestration?** This runtime is the instrument. Every design decision below is
made so that a "yes" would mean something.

---

## 1. The loop, end to end

```text
run_agent(task, context, model_provider, skill_enabled, tools, ...)
  |
  +-- system prompt = HARNESS_BASE_SYSTEM_PROMPT
  |                     + SKILL.md text            when skill_enabled=True
  |                     + (nothing)               when skill_enabled=False
  +-- user message  = task text + context.model_dump_json()
  |
  loop, at most max_steps times:
  |     response = model_provider.generate(messages, tools, temperature, max_tokens)
  |       |
  |       +-- ProviderError / any exception  -> stop "provider_error", no answer
  |       +-- response.tool_calls            -> append the assistant turn (with its
  |       |                                     calls), run each call, append each
  |       |                                     result keyed by call id, loop
  |       +-- response.content only          -> stop "completed", final answer
  |
  +-- EvidenceTrace <- TraceCollector (the only writer)
```

The model owns every decision inside the loop: which tool to call, with what
arguments, how many horizons to assess, and what to conclude. The harness owns
two things only -- the mechanics of feeding results back, and the record of what
happened. Neither of those is a decision about safety.

## 2. The single variable

`skill_enabled` is the only argument whose value differs between a baseline run
and a treatment run of the same scenario. Everything else is a plain argument of
`run_agent()` and is never derived from it:

| Identical in both conditions | Where it lives |
| --- | --- |
| task text | `scenario.task_prompt` |
| context snapshot (byte-identical JSON) | `scenario.initial_context` |
| tool registry and therefore `tool_schema_dicts()` | `scenario_registry(scenario)` |
| `temperature`, `max_tokens`, `max_steps` | fixed once per batch |
| `HARNESS_BASE_SYSTEM_PROMPT` | `harness.py` |
| `tool_failure_policy` | fixed once per batch |

`evals/compare.py` enforces the same four preconditions before it will compare
anything: same scenario ids, same model, same offered registry, same sampling
parameters. A comparison that fails a precondition is refused, not reported with
a caveat.

`HARNESS_BASE_SYSTEM_PROMPT` is runtime mechanics and carries none of the
treatment vocabulary -- no product identity, no horizon words, no freshness
budget, no uncertainty-disclosure rule, no projected-risk-state wording. Those are
the Skill's, and writing any of them into the base prompt would hand the baseline
half of the treatment and collapse the experiment. `tests/test_harness.py`
asserts the absence.

One deliberate exception, stated because a claim this central must not overstate
itself: the base prompt does carry one generic, domain-neutral habit -- "never
invent, extrapolate or fill in a value that was not returned by a tool". That is
tool etiquette any harness needs, not the TrailGuard missing-data protocol, whose
substance (list every gap, mark stale inputs, lower confidence, answer
`insufficient_data`) is entirely SKILL.md's. The sentence is shared with the
baseline on purpose: weakening the baseline to flatter the treatment is just as
much a fairness violation as contaminating it (spec 3.2).

## 3. Tool execution: the model asks, the tool answers

Each requested call goes through, in order:

1. **Name resolution.** The requested name must be a member of the frozen
   `ToolName` registry. A name that is not one cannot be represented in the
   trace at all, so it is reported back to the model as an error and is not
   counted as a tool call.
2. **Registry lookup.** If the tool was not handed to the run
   (`tools=`), the call fails as `not provided to this run` -- a registry entry
   the harness advertised but nobody supplied is a configuration error, not a
   tool failure.
3. **Contract validation.** The arguments are validated against the *same*
   `input_model` the advertised JSON schema describes. What the model was shown
   is what its arguments are checked against, so a malformed call fails with the
   contract's own message rather than reaching a tool that cannot interpret it.
4. **Execution, then recording.** The result is serialised whole when it is a
   pydantic contract (its size is bounded by its schema; truncating it would show
   the model a *cut* safety document as if it were the whole one) and capped at
   `_MAX_SUMMARY_CHARS = 600` only for an untyped payload.

The harness does not echo the model's input back as the tool's output, does not
retry a failed call, does not repair arguments, does not substitute a different
tool, and does not write the model's final answer for it. A failed call is
recorded and handed back to the model, which is free to correct itself.

### Five outcomes, five stop reasons

| Outcome | What happened | Whose failure |
| --- | --- | --- |
| `ok` | the tool ran and returned | -- |
| `invalid_tool_call` | arguments failed the tool's input contract | the model's |
| `unknown_tool` | the requested name is not in the registry | the model's |
| `tool_error` | the tool raised (bad asset, missing feed, ...), **or** the harness advertised a tool that nobody supplied (`not provided to this run`) | the tool's / the run's configuration |
| `not_implemented` | a Phase 0 stub raised `NotImplementedError` | the phase's |

The `tool_error` row carries two failures with different owners, and that is
deliberate rather than sloppy: the trace's outcome vocabulary is frozen at five
values, and the configuration failure (a tool advertised in the schema list but
absent from `tools=`) is recorded as `tool_error` because it *stops the run* the
same way a raised tool does. A reader who needs the difference reads the error
string, which names the case verbatim.

`stop_reason` distinguishes `completed`, `max_steps`, `provider_error`,
`tool_error` and `invalid_tool_call`. The last two exist so a benchmark can tell
"the agent hallucinated a tool" apart from "the weather endpoint is down"; under
the default `tool_failure_policy="report"` neither can occur, and that default is
the pre-Phase-3 behaviour, preserved so a later phase cannot silently change what
a stop reason means. `tool_failure_policy="stop"` opts into ending on the first
failure and is a harness policy -- never derived from `skill_enabled`, and
identical in both conditions.

## 4. What the trace records -- and what it refuses to infer

The typed-copy boundary is the important idea here. When a tool returns a typed
contract, the harness copies the *typed* parts of it into the evidence trace:

* a successful `assess_risk` result contributes its own `horizon`,
  `hard_rules_triggered`, `drivers`, `confidence`, `risk_level` and
  `safety_margins.weather_freshness_min`;
* a successful `get_weather_for_route_time` result contributes its `source`.

The one comparison the harness makes is the freshness verdict: the age the core
actually used, against the snapshot's own `WeatherPolicy.max_freshness_min`.
"Stale" is meaningless without the budget it was measured against, and the budget
is part of the context the tool already consumed. An age the core could not
compute leaves `freshness_ok` as `None` -- "not checkable" -- rather than being
silently treated as fresh.

Two trace fields carry the core's output separately from the agent's claim:

* `core_risk_level` / `core_confidence` -- what the tools produced;
* `final_risk_level` / `confidence` -- what the agent claimed.

The harness fills the first pair and leaves the second to the Phase 5 evaluator.
It does not read them out of a paragraph: a run whose answer happens to contain
the word "RED" must not get a RED trace entry, and a harness with an opinion about
English would make the honesty metric circular. The gap between the two fields is
where "the agent said X and the core said Y" becomes measurable.

`missing_data` is left empty on purpose. Which fields were absent *at this
horizon* is the Skill's missing-data protocol (spec 10.3); the harness has a
whole-run snapshot, and copying its `missing_fields` onto one horizon would
attribute run-level gaps to a moment that did not experience them.

## 5. Skill identity, not skill interpretation

`trailguard/agent/skill_loader.py` loads the skill text and returns it raw. It
exposes no API for extracting rules, thresholds, horizons or algorithms from it --
any such extraction would give the baseline and the treatment different effective
behaviour and break the single variable.

Two things are recorded as *provenance* (spec 18): the `version:` line of the
skill's frontmatter, and a SHA-256 of the exact bytes that were injected. The hash
is taken over the string that was loaded -- the string the harness injected -- not
over a re-read of the file, because those are the same bytes only when nothing
rewrote the file in between. `tests/test_harness.py` proves it with a variant
skill whose frontmatter version is identical and whose body is not.

That is the whole interface. The moment Python starts reading horizons,
freshness budgets or explanation blocks out of the skill, the treatment stops
being a single variable.

## 6. Providers: pure transport, with retry as data

`ModelProvider` (`trailguard/providers/base.py`) turns one list of `ChatMessage`
into one `ModelResponse` for one model. It executes nothing. The two backends of
the frozen `2 models x 2 conditions` matrix (spec 14.2) implement it
interchangeably behind an identical `generate(messages, tools, temperature,
max_tokens)` signature, so no caller can branch on which backend is loaded.

Failures are a hierarchy, not a string. `retryable` is the one bit of policy on it
and it is data rather than behaviour:

| Failure | `retryable` | Raised when |
| --- | --- | --- |
| `ProviderTimeout` | yes | connect/read timeout expired |
| `ProviderUnavailable` | yes | endpoint unreachable, SDK missing, client unbuildable |
| `ProviderRateLimited` | yes | HTTP 429 |
| `ProviderServerError` | yes | HTTP 5xx |
| `ProviderAuthenticationError` | **no** | HTTP 401/403, or no credential at call time |
| `ProviderResponseError` | **no** | the endpoint answered and the answer was unusable |

`StepFunProvider` spends at most **one** immediate retry, and only for the
failures a retry can fix. The SDK's own retry loop is disabled (`max_retries=0`)
so the budget is exactly what the module documents; there is no backoff sleep,
because a benchmark run must not stall on a flapping endpoint.

Secret handling: the credential lives in one private attribute, is masked by
`__repr__`, is never returned by `name`, and never appears in a trace, a log or an
exception message. Every failure this provider raises names the *kind* of failure
and the endpoint, never the key that was rejected. `openai` is imported lazily
inside `_client()`, so importing the provider requires neither the SDK nor a
credential, and no client is bound at import time.

## 7. Replay Mode

`trailguard/agent/replay_runner.py` turns a scenario file into runs and runs into
files a judge can read:

* `load_scenario(path)` reads and validates one scenario against the frozen
  contract, so a typo fails at the boundary instead of surfacing as a run against
  a half-parsed snapshot;
* `scenario_registry(scenario)` returns the canonical registry with the
  scenario's own weather policy bound in -- see §9;
* `replay_scenario(...)` runs one scenario in one condition through the *same*
  `run_agent` and writes `runs/phase3/<scenario_id>/<condition>.json`;
* `replay_dataset(...)` repeats that across the dataset and writes a batch summary
  (`runs/phase3/summary_<utc>.json`) that is an index -- run ids, stop reasons and
  structural findings -- never a second opinion;
* `final_answer_findings(answer)` reports string facts: length, emptiness, which
  canonical horizon labels appear, and whether the banned accident-probability
  phrase appears.

The driver never calls a tool and never scores a run. A driver that called tools
itself would hand the baseline a plan, and the measured difference would then be
the driver's, not the Skill's; `tests/test_replay_runner.py` asserts the seam
structurally. A driver that graded its own results would be the evaluator living
inside the thing being evaluated, which is why `mentions_banned_claim` is a
*presence* scan: "the accident probability is 3 %" and "there is no accident
probability to report" both contain the phrase, and no substring test can tell a
claim from a denial, so the field says what it saw and leaves the judgement to
Phase 5.

`python -m trailguard.agent.replay_runner` runs the dataset over the configured
model. Without `STEPFUN_API_KEY` there is no model to run and the honest answer is
to say `NOT RUN` and exit non-zero: substituting a stub, a cached trace or a
replay fixture would produce a benchmark row for a run that never happened
(spec 17).

## 8. The offline smoke

```bash
python -m trailguard.agent_smoke                      # 20 checks, exit 0
python -m trailguard.agent_smoke --scenario scenarios/edge_cases/scn_stale_gps_missing_fix.yaml \
                                --device-id scn_device_S11_stale_gps_missing_data
```

This is the one command that proves the whole runtime works together on a machine
with no credential and no network: one frozen scenario, the real registry, the
real frozen contracts, the real `run_agent`, the real evidence trace -- driven
through both conditions by `ScriptedProvider`, a scripted offline stand-in for the
model.

**It proves the runtime works. It proves nothing about model behaviour**, and it
says so on every line of its output. No real model is called; the skill's effect
is the benchmark's question (spec 14). The stub does not read the system prompt,
so the two conditions differ *only* by the injected text -- `test_a_decoy_system_prompt_changes_nothing_the_stub_decides`
hands it a transcript that says "call nothing" and observes that it calls the same
tool anyway.

The stub's sample output is itself honest: for the stale-GPS scenario its answer
recites the inputs the snapshot declares stale and missing and states that the
weather age is outside the budget, rather than quoting a clean time margin next to
no caveat. A smoke script that reproduced the forbidden disclosure pattern would
be rehearsing the failure mode inside the project built to catch it.

## 9. Two Phase 3 findings (recorded, not fixed in-phase)

Both are gaps between a frozen contract and what a replayed run needs. Neither is
fixed here, because fixing either changes a Phase 1/Phase 2 frozen surface inside
a phase that does not own it.

**F1 -- `GetWeatherForRouteTimeInput` cannot express the scenario's weather mode.**
The contract carries exactly `lat`, `lon`, `target_time`; `WeatherPolicy` (mode +
`snapshot_ref`) is how a scenario says where weather may come from, and the tool's
*Python* signature accepts it as a keyword-only `policy=` -- so a model can never
ask for replay mode. A replay scenario replayed through the bare
`TOOL_FUNCTIONS` registry therefore falls back to the tool's documented live
default: it queries live weather instead of its own frozen snapshot, which makes
the run non-reproducible and, with the frozen scenario dates now outside the live
provider's range, an outright failure. `scenario_registry()` closes the gap at the
registry level using the tool's own documented dependency-injection seam, without
touching the contract or hiding anything from the model (the advertised schema is
still `lat`/`lon`/`target_time`; the mode is the one the scenario already
declared). `test_the_frozen_weather_contract_has_no_field_for_the_scenarios_mode`
records the gap and is the test to delete when a later phase adds the field.

**F2 -- `get_user_device_context` requires an id the agent is given nowhere.**
The scenario contract declares `route_ref` and `weather_snapshot_ref` but no
`device_feed_ref`, and the context snapshot carries no device id, while the tool
resolves a feed *only* by file stem under `replay/devices/`. A faithful agent run
therefore cannot know which feed to ask for and records a failed tool call. Both
conditions are affected identically, so the comparison stays fair -- but a live
run will show the failure, and it must not be mistaken for a model mistake. The
offline smoke passes the feed id explicitly (`--device-id`) and says which one it
used in its output, for exactly that reason.

## 10. The live smoke

```bash
python scripts/test_stepfun_agent.py                          # every scenario, both conditions
python scripts/test_stepfun_agent.py --scenario scenarios/baseline/scn_clear_day_normal.yaml
```

The one script that puts a real model in the loop. It is never collected by pytest
(`tests/conftest.py::collect_ignore`), so no run of the suite can reach it -- by
accident, by `pytest scripts/`, or otherwise.

It reports what happened and stops there: it does not score the comparison, that
is the Phase 5 benchmark's job. Without `STEPFUN_API_KEY` it prints
`NOT RUN -- credential unavailable: STEPFUN_API_KEY is not set.` and exits 2,
writing no trace and fabricating none. It reads no `.env`, no key file and no
settings file, and it never prints the credential or a request body. Traces are
written to `runs/phase3/`, which is git-ignored: a run is evidence, not a source
file, and a directory that grows with every live run has no business in version
control.

## 11. Boundaries: what Phase 3 does not include

* `LocalModelProvider` is still a Phase 4 stub that raises `NotImplementedError`
  naming its phase. No Qwen model is wired into the product runtime anywhere.
* No benchmark aggregation, no superiority claim. Nothing here says the Skill
  helps; it says the instrument for finding out is in place and is honest about
  what it measures.
* No Streamlit, no map UI, no `demo/` directory (Phase 6).
* No training, no fine-tuning, no agent swarm, no memory, no database.
* No language model participates in risk computation. The chain from GPX to
  verdict runs with `STEPFUN_API_KEY` unset.
* `python -m pytest` makes **zero** external HTTP calls: the autouse fixture in
  `tests/conftest.py` closes the socket layer, and the StepFun transport is
  exercised over `httpx.MockTransport`. The only paths that may open a socket are
  `scripts/test_live_weather.py` and `scripts/test_stepfun_agent.py`.

## 12. Before believing any comparison

1. Same scenario ids, same model string, same offered registry, same sampling
   parameters -- `evals/compare.py` refuses otherwise.
2. `skill_loaded` is `True` in exactly the treatment traces, and the recorded
   `skill_sha256` is the hash of the text actually injected.
3. The tool sequences came from the model. Check the harness did not make them:
   no horizon iteration, no fixed tool order, no auto-fill of missing calls.
4. `core_risk_level` disagrees with `final_risk_level` where the agent edited a
   verdict -- that disagreement is a finding, not a rounding difference.
5. The run that failed reported that it failed. A green comparison with no failed
   runs in it has not been run against anything hard yet.

---

See also [`architecture.md`](architecture.md) for the layer map,
[`contracts.md`](contracts.md) for the five tool contracts, and
[`evaluation_protocol.md`](evaluation_protocol.md) for what the benchmark will
measure.
