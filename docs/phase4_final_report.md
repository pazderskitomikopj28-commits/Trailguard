# TrailGuard Phase 4 Final Report

**Phase 4 — Local Model Integration on DGX Spark.**
Frozen spec: `TrailGuard_比赛版开发冻结规格_v2.1.md` (sections 4, 9, 12, 13, 14, 16, 17,
20, 25). Previous freeze: `64800f4` on `main`
(`feat: agent skill runtime and tool orchestration (Phase 3)`); no Phase 4 commit —
**nothing in Phase 4 is committed** (§77).

What this phase had to prove: that a locally served Qwen3-VL-30B-A3B on this DGX Spark
emits **native structured** `message.tool_calls`, and that TrailGuard's generic harness
can drive that model through the five frozen tools, the deterministic safety core and an
evidence trace without a single provider-specific branch. What it had to refuse: any
client-side regex that reads a tool call out of `content`, any harness special case for
the local model, any edit to `SKILL.md`, and any silent fallback to another transport
when the local endpoint is down.

The verdict is at §20. Everything above it is the evidence, including the one run that
failed and its exact cause (§13, §14).

---

## 1. Procedure Actually Followed

Executed in the order the acceptance brief fixed. The record below is what each step
produced.

| # | Step | Outcome |
|---|------|---------|
| 1 | Compose backup, hash recorded | `docker-compose.yml.phase4-pre-toolcall.bak`, sha256 `66f1bedd4fefb25635d890f231b708f93ead7c626ffe1506bf5c6650e28d9aab` |
| 2 | Minimal compose edit | 3 lines added, nothing else (§4) |
| 3 | `docker compose config` + `diff -u` | Diff shows only the 3 tool-calling lines (§4) |
| 4 | `up -d`, read the logs | `enable_auto_tool_choice=True`, `tool_call_parser='hermes'`, `Application startup complete` (§4) |
| 5 | `/health` and `/v1/models` | 200 with an empty body; one model `qwen3-vl-30b-a3b`, `max_model_len=16384` (§2) |
| 6 | Independent probe at `scripts/test_tool_calling.py` | Built; raw `urllib`, no SDK, no TrailGuard provider |
| 7–10 | Gates A (`required`), B (`auto`), C (continuation) | All PASS (§5) |
| 8 | Content-as-tool-call is a FAIL | Enforced structurally: every assertion in that check is negative |
| 11 | Machine-checkable per-gate PASS/FAIL | Each gate emits booleans and a JSON report to `runs/phase4/tool_calling_probe.json` |
| 12 | One variable at a time, record all attempts | Three attempts recorded on the failing run (§13) |
| 13 | Only then implement `LocalModelProvider` | Done after the gates passed |
| 14–15 | Defaults and provider responsibilities | §7 |
| 16 | No provider/model branch in the harness | Enforced by test (§8, §19) |
| 17 | `SKILL.md` byte-unchanged | §9 |
| 18 | pytest needs no GPU/Docker/localhost | §19 |
| 19 | Real local agent smoke at `scripts/test_local_agent.py` | §10 |
| 20–22 | The three real runs | §11, §12, §13 |
| 24 | `pytest -v` and `python -m trailguard.agent_smoke` | §19 |
| 25 | This report | — |
| 26–27 | Verdict; `git status` / `git diff`; no commit, no push | §20, Appendix B |

---

## 2. What the Deployment Is

Measured on 2026-09-27, not assumed:

```
container : qwen3-vl-30b-a3b  (up)
GET  /health      -> HTTP 200, empty body
GET  /v1/models   -> 200, one model "qwen3-vl-30b-a3b", max_model_len 16384
```

`/health` sits at the **deployment root**, not under `/v1`, and it answers 200 with an
empty body. That is a health check behaving correctly: it asserts one bit and refuses to
make the caller parse a payload for it. Both facts are load-bearing later (§13, §18).

The compose file freezes `dtype: bfloat16`, `--max-model-len 16384`, `--max-num-seqs 1`,
`--gpu-memory-utilization 0.70`, and `127.0.0.1:8000:8000` with
`restart: unless-stopped`. All preserved. The container-internal `--host 0.0.0.0` is
normal for a container and was **not** misread as host exposure; the published port is
loopback-only, and no port, firewall or SSH setting was changed.

---

## 3. Before the Change: the Capability Was Absent, and the Server Said So

`runs/phase4/preflight/before_enable.json` is the pre-edit probe. The server did not
merely fail to return structured calls — it named the flags that were missing:

```json
{"required": {"ok": false, "error_type": "BadRequestError",
  "error": "400 - tool_choice=\"required\" requires --tool-call-parser to be set"},
 "auto": {"ok": false, "error_type": "BadRequestError",
  "error": "400 - \"auto\" tool choice requires --enable-auto-tool-choice and "
           "--tool-call-parser to be set"}}
```

This is what makes the compose edit necessary rather than convenient. It is also why the
parser was not chosen at random: the server stated exactly which capability was missing,
and `hermes` is the parser that reads this checkpoint's native envelopes.

---

## 4. The Compose Edit

Backup first, then the smallest possible change. `diff -u` against the backup:

```diff
--- docker-compose.yml.phase4-pre-toolcall.bak
+++ docker-compose.yml
@@ -42,3 +42,6 @@
       - 0.0.0.0
       - --port
       - "8000"
+      - --enable-auto-tool-choice
+      - --tool-call-parser
+      - hermes
```

Nothing else moved: not the weights, not the dtype, not the model path, not
`--max-model-len`, not `--max-num-seqs`, not the port binding, not the restart policy. No
`docker system prune`, no weight re-pull, no CUDA/driver/kernel/firewall/SSH change of
any kind.

Post-restart, the deployment reports the flags it actually started with
(`logs/phase4-restart-hermes.log`):

```
non-default args: {'model_tag': '/models/qwen3-vl', 'enable_auto_tool_choice': True,
 'tool_call_parser': 'hermes', 'host': '0.0.0.0', 'model': '/models/qwen3-vl',
 'dtype': 'bfloat16', 'max_model_len': 16384,
 'served_model_name': ['qwen3-vl-30b-a3b'], 'gpu_memory_utilization': 0.7,
 'max_num_seqs': 1}
Starting vLLM server on http://0.0.0.0:8000
Application startup complete.
```

---

## 5. The Capability Probe (Steps 6–11)

`scripts/test_tool_calling.py` talks to the HTTP wire protocol with raw `urllib` and
**no SDK**, deliberately: an SDK can coerce a null into an empty list, or drop a field it
has no model for, and either would let the probe report a capability the server does not
have. It parses no tool call out of text. Where it looks at `content` at all, it is to
detect the failure — a body that spells the envelope while `tool_calls` is null — and
that detection is reported, never rescued.

Gates, in order, all on the live endpoint:

| Gate | Requirement | Result |
|------|-------------|--------|
| A `tool_choice="required"` | `message.tool_calls` is a non-empty list; each call has a non-empty `id`, a `function.name`, and arguments that parse as a JSON **object** carrying the schema's declared required parameter | **PASS** — 1 call, `arguments='{"city": "Reykjavik"}'`, `finish_reason=tool_calls` |
| B `tool_choice="auto"` | Same structure, chosen freely | **PASS** — 1 call, `finish_reason=tool_calls` |
| C tool-result continuation | Replay B's assistant turn verbatim with its `tool_calls`, append a `tool`-role message keyed by that `tool_call_id`, require a final answer that uses the tool's value | **PASS** — `finish_reason=stop`, the answer quotes the value, no envelope in text |

Every gate prints the raw wire fields and writes `runs/phase4/tool_calling_probe.json`,
which is read back and verified after writing. `message.tool_calls` came back as a JSON
**list** with real string ids and string `function.arguments` on every gate.
`reasoning_content` was never read or printed.

---

## 6. Gate D — Added, Because an Agent Run Asked the Question

Gate D was not among the original three. It exists because the S04 compound run (§13)
emitted *batches* of tool calls — five in one turn, four in the next — and the harness
executed all of them from `message.tool_calls`. Nothing in gates A–C distinguishes "one
call arrives structured" from "a batch does", and that distinction decided a run.

**Gate D:** ask for two calls in one response; require that *both* return as structured
calls with distinct arguments.

```
message.tool_calls JSON type : list
message.tool_calls count     : 2
finish_reason                : tool_calls
tool_calls[0].function.arguments : {"city": "Reykjavik"}
tool_calls[1].function.arguments : {"city": "Nairobi"}
VERDICT: PASS
```

It passes at the default batch budget (2048) **and** at 512, which matters: the toy
arguments are small, so the batch fits either way. That is what later isolates the real
cause of the S04 failure — that failure was never about parallel calls as such.

Note also the budget handling: judging a two-envelope batch on a single-call token budget
would have measured the budget, not the endpoint. Hence a dedicated
`--parallel-max-tokens 2048`.

---

## 7. The LocalModelProvider (Steps 13–15)

`trailguard/providers/local.py`, built only after the gates passed.

Responsibilities, and nothing beyond them: build the request from `messages`, `tools` and
the sampling parameters; read back `content`, `tool_calls`, `finish_reason`, `usage`;
classify transport errors into `ProviderError` subclasses. It does not execute tools,
drive the horizon loop, order tool calls, compute risk or know anything about skills.

- Defaults `TRAILGUARD_LOCAL_BASE_URL=http://127.0.0.1:8000/v1` and
  `TRAILGUARD_LOCAL_MODEL=qwen3-vl-30b-a3b`; the legacy `TG_LOCAL_*` names are honoured
  but overridden by the new ones. **No host or IP is hardcoded in code.**
- `tool_call.id` is preserved verbatim from the wire, through the provider and the
  harness, and back into the `tool`-role message keyed by `tool_call_id` (gate C proves
  the server needs that key; the two S01 runs of §11 and §12 together show it working
  across 28 structured tool calls).
- `function.arguments` is parsed with `json.loads` only. No `eval`, no
  `ast.literal_eval`. Illegal JSON surfaces as `invalid_tool_call`, not as a repair.
- `import openai` happens inside `_client()`, so the module imports on a machine without
  the SDK and touches no environment at import time.
- An unreachable endpoint raises `ProviderUnavailable` and the run stops with
  `provider_error`. **There is no fallback** — not to StepFun, not to a scripted
  provider. A stopped server yields an honest record of a stopped agent.
- SDK error classes are matched by **class**, never by message text, and client
  construction failures are reported as class names so no echoed configuration can carry
  a credential.

The local server authenticates nobody, so no secret exists for it and none was read.
`TRAILGUARD_LOCAL_API_KEY` exists only as a structural placeholder for a client that
insists on sending the header.

---

## 8. Harness Compatibility (Step 16)

The local model runs the **same** `run_agent`, the same `replay_scenario`, the same five
frozen tools and the same deterministic safety core as Step 5 Preview. The provider is
injected, as it always was:

```python
# trailguard/agent/replay_runner.py -- the ONLY change to this file
def _provider_from_environment(provider_name: str = "stepfun") -> ModelProvider:
    if provider_name == "local":
        from ..providers.local import LocalModelProvider
        return LocalModelProvider()
    if provider_name == "stepfun":
        from ..providers.stepfun import StepFunProvider
        return StepFunProvider()
    raise ValueError(
        f"unknown provider {provider_name!r}; the replay driver serves 'stepfun' and 'local'. "
        "A name it does not know would otherwise build an endpoint nobody chose."
    )
```

That is a driver-level knob, not a branch in the harness: `run_agent` is untouched, and
whatever it receives is handed to the identical loop. There is no `run_qwen_agent`, no
`if provider == "local"`, and no `"qwen" in model` anywhere in the agent path — enforced
by an AST-level test rather than by review (§19).

The knob is a CLI argument (`--provider {stepfun,local}`, default `stepfun`) that
`main()` passes down; inside `replay_scenario` the parameter is threaded through
unchanged. The credential gate applies to `stepfun` only: `local` runs against a
server that authenticates nobody, so demanding `STEPFUN_API_KEY` for it would be a lie
about why nothing happened.

---

## 9. The Skill Is Untouched (Step 17)

`SKILL.md` sha256 `98fd007eb23a…`, byte-identical to the Phase 3 freeze, verified at the
start of every smoke run and printed in its header. No wording was adjusted to coax the
local model into behaving. Where the local model misbehaved, the behaviour was recorded
(§13) — the skill was not tuned for it, and neither were the five tool contracts or the
safety core. `assess_risk` still requires the full nested state; that the model then
miscounts braces while serialising it is a model finding, not a contract defect.

---

## 10. The Local Agent Smoke (Steps 18–19)

`scripts/test_local_agent.py` — a real driver, not a pytest module, because it needs a
30B checkpoint behind an HTTP endpoint and the suite must make no HTTP call at all.

Three runs, in the frozen order, with `skill_enabled` the only difference inside a pair:

```python
RUN_PLAN = (("S01", True), ("S01", False), ("S04", True))
```

It records, per run: steps, stop reason, the ordered tool sequence with per-call status,
the horizons actually checked, the core's verdict, the model's answer verbatim, token
usage and wall time (collected in a driver-level wrapper, because `EvidenceTrace` has no
usage field — §18), and the trace path. Exit codes: `0` all runs completed and no check
failed; `2` the endpoint was not usable and **no trace was written**; `3` the transport
failed; `4` a run did not complete or a semantic-consistency check failed.

It also carries four driver-level semantic checks. None repairs a run; two fail one:

| check | blocking? | condition |
|---|---|---|
| `check_hard_rule_softened` | yes, for two cases | core RED + a de-escalation phrase + no RED marker anywhere; or core RED + the answer claiming GREEN/YELLOW and never naming RED |
| `check_answer_is_not_a_raw_tool_call` | yes | the answer *is* an unparsed tool-call envelope while the harness saw no tool call — for **every** scenario, not only S04 |
| `check_hard_rule_reported` | no — recorded | hard rule R-B absent from the answer (matched as a whole token, not a substring: `"b" in "manageable"` must not count as reporting R-B) |
| `check_answer_commands_a_stop` | no — recorded | core RED paired with no stop advice at all |

The line between them is the one the brief draws: whether the endpoint produced
structured tool calls, and whether the answer contradicts a RED verdict, are properties
of the *protocol* and fail the run. Whether the prose happens to name a rule or contains
a stop phrase is English, and wasting a run over it would be this driver grading the
model's wording. The recorded findings are printed, kept beside the trace and counted —
they are not upgraded into failures to make the smoke more impressive, nor downgraded
into silence. Severity travels with each finding as a typed value, so the dispatch
cannot recover it from the message's first words (see F-4).

The third check was added during this phase because of what §13 found. Its initial
absence is recorded as a defect in this phase's own tooling (§18, F-4).

---

## 11. Run 1 — S01, `skill_enabled=True` (Step 20)

Scenario `S01_clear_day` (`scenarios/baseline/scn_clear_day_normal.yaml`),
`temperature=0.0`, `max_tokens=1024`, `max_steps=16`.

```
stop_reason  : completed          steps: 15
tool sequence: get_route_context[ok]
             -> get_user_device_context[error]
             -> get_user_device_context[ok]
             -> project_route_state[ok] x3
             -> get_weather_for_route_time[ok] x4
             -> assess_risk[ok] x4
horizons     : NOW, +30min, +60min, +120min   (all four assessed)
core verdict : GREEN (confidence 0.557143); triggered_rules ()
model turns  : 15   wall: 197.0s
tokens       : 169352 total (prompt 164342 / completion 5010)
throughput   : 25.47 completion tokens/s (measured)
trace        : runs/phase4/S01_clear_day/with_skill.json
```

Fourteen structured tool calls, thirteen of them `ok`, in fifteen model turns. The chain
the verdict criterion asks for ran end to end: structured `tool_calls` → the harness →
real TrailGuard tools against real replay data → tool results → continuation → a final
answer covering all four horizons, quoting the core's per-horizon verdicts, and
disclosing that the weather is a synthetic fixture at 30 minutes old and that projection
confidence decays from 0.485 to 0.45.

`project_route_state` appears three times, not four: the `NOW` horizon needs no
projection, so the model asked for `+30min`, `+60min` and `+120min` only. That the count
matches the horizons that *need* projecting is itself part of the evidence.

Two things worth recording about this run:

- The model first called `get_user_device_context` with `device_id: "device_123"` — an id
  that does not exist. The tool raised `DeviceFeedError`; that error came back as the
  tool result; and the model recovered by calling it with the real feed name
  `scn_device_S01_clear_day`. The error path works, and so does recovery.
- The per-turn prompt column climbs from 8453 to 15116. The window is 16384.

---

## 12. Run 2 — S01, `skill_enabled=False` (Step 21)

Identical in every respect but `skill_enabled`.

```
stop_reason  : completed          steps: 15
tool sequence: get_route_context[ok]
             -> get_user_device_context[error]
             -> project_route_state[error]
             -> project_route_state[ok] x3
             -> get_weather_for_route_time[ok] x4
             -> assess_risk[ok] x4
horizons     : NOW, +30min, +60min, +120min
core verdict : GREEN (confidence 0.557143); triggered_rules ()
model turns  : 15   wall: 211.9s
tokens       : 144809 total (prompt 139346 / completion 5463)
throughput   : 25.81 completion tokens/s (measured)
```

Two observations, recorded without comparison — **this phase makes no claim about which
condition is better** (that is Phase 5's benchmark and its statistics):

- This run invented a second plausible device id (`hiker_device_01`), got the same
  `DeviceFeedError`, and did **not** recover: it never obtained device context at all.
- Without the skill's time state, `project_route_state` was first called without the
  `analysis_time` it requires and failed with a `ValueError` naming the caller that owns
  the clock. The model then supplied it. Both are correct behaviour of the frozen tool:
  the contract demands the anchor and did not silently default it.

---

## 13. Run 3 — S04, `skill_enabled=True` (Step 22): The Failure

Scenario `S04_sunset_margin_pace_drop` (`scenarios/compound/`), same skill, same tools,
same sampling as run 1.

```
stop_reason  : completed          steps: 3
tool sequence: get_route_context[ok]
             -> get_user_device_context[error]
             -> project_route_state[ok] x3
             -> get_weather_for_route_time[ok] x4
horizons     : (none)
core verdict : (none) -- assess_risk never ran, so the deterministic core never returned
```

Turns 1 and 2 behaved: the model emitted **five, then four** tool calls in single
messages, and the harness executed all nine from `message.tool_calls`. Turn 3 is where it
broke, and it broke differently at each budget tried — recorded here as step 12's three
attempts, one variable at a time:

| Attempt | `max_tokens` | Turn-3 `finish_reason` | Completion | Envelopes in `content` | Parser error in the vLLM log |
|---------|--------------|------------------------|------------|------------------------|------------------------------|
| 1 | 1024 | `length` | 1024 | 2 open / 1 close, the last cut mid-string | `Unterminated string starting at: line 2 column 1064` |
| 2 | 2048 | `length` | 2048 | 4 open / 3 close | `Extra data: line 2 column 1455` |
| 3 | 3072 | `stop` | 2604 | **4 open / 4 close — all four complete** | `Extra data: line 2 column 1455` |

At 3072 the model stopped on its own, having written four *complete* envelopes, and the
endpoint still returned `tool_calls` empty with all of that text in `content`. The smoke
failed this run (exit 4) on `check_answer_is_not_a_raw_tool_call`:

```
CHECK FAILED: the run's answer is a tool call the endpoint left in content text
(complete but unparsed): the model emitted the opening envelope tag and
message.tool_calls was empty, so no real TrailGuard tool ran and no risk verdict was
computed. This is spec 9's failure mode... The answer is kept verbatim in the trace;
nothing was rewritten.
```

Because `assess_risk` never ran for S04, **the RED verdict and hard rule R-B were never
reached, and the R-B semantic checks were therefore not exercised in Phase 4.** That is a
coverage gap, stated as such rather than left implied (§18, F-3).

---

## 14. The S04 Root Cause

Three independent lines of evidence, all pointing at the same place.

**(a) The parser's own rule, applied to the bytes the client received.**
vLLM's Hermes parser compiles a regex over the envelope tags and then, for every match in
the message, calls `json.loads` inside a single list comprehension wrapped in one `try`.
Applying that exact pattern and that exact call to the stored answer:

```
envelope 0: 1183 chars -> parses -> dict keys=['arguments', 'name']
envelope 1: 1457 chars -> JSONDecodeError: Extra data: line 2 column 1455
envelope 2: 1457 chars -> JSONDecodeError: Extra data: line 2 column 1455
envelope 3: 1456 chars -> JSONDecodeError: Extra data: line 2 column 1455
```

**(b) What the extra data is.** `JSONDecodeError("Extra data")` means a complete JSON
object parsed and then more bytes followed. Decoding envelope 1 with `raw_decode` shows
the object ending at char 1454 of 1457 — leaving `}\n`:

```json
"projection_confidence": 0.4268}}}}
```

That closing brace is one too many. Envelopes 1, 2 and 3 each carry exactly one
superfluous `}`; envelope 0 (1183 chars) is well-formed. The longer the nested object,
the more likely the miscount.

**(c) The server agrees.** `logs/phase4-restart-hermes.log` at 09:09:00 and again at
09:26:19:

```
ERROR [hermes_tool_parser.py:139] Error in extracting tool call from response.
  File ".../vllm/tool_parsers/hermes_tool_parser.py", line 114, in extract_tool_calls
    json.loads(match[0] if match[0] else match[1])
json.decoder.JSONDecodeError: Extra data: line 2 column 1455 (char 1455)
```

**So the cause is neither truncation nor a parser that cannot handle batches.** The local
model emitted **malformed JSON** — one extra closing brace in 3 of the 4 `assess_risk`
envelopes — and vLLM's Hermes parser is **all-or-nothing across a message**: one
envelope that fails to parse discards the whole batch, including the well-formed first
call, and degrades the entire message to `content`.

Two facts bound the finding honestly:

- The **same** argument shape parses cleanly when the model emits it one call at a time:
  S01's four `assess_risk` calls, each a deeply nested state object, all parsed and all
  executed, in both runs. The defect appears when the model batches them.
- Raising the output budget does not fix it (attempt 3: 2604 tokens, four complete
  envelopes, still malformed). It is not a budget problem.

Nothing was done about it. The parser was not switched — there is no evidence for
`qwen3_xml` here, and the evidence says the parser's rule is fine and its *input* was
malformed, so switching would have concealed a model defect. No harness code repairs it.
No skill wording was adjusted. No tool order was overridden.

---

## 15. Why S04 Could Not Simply Be Given More Room

Recorded because it forecloses the obvious next move. At `max_tokens=3072` the batch
completes (2604 tokens), and the transcript then stands at roughly:

```
turn 3 prompt 10893 + assistant batch 2604 + four assess_risk results ≈ 15589 tokens
against a 16384 window
```

A final answer needs headroom the window does not contain. `--max-model-len 16384` and
`--max-num-seqs 1` are frozen and neither may be changed for this phase. So the compound
scenario cannot be completed by the local deployment under the frozen configuration with
a batching model — a Phase 5 risk to inherit, recorded rather than solved.

---

## 16. Semantic Consistency, and Why the Spec-9 Detector Is Structural

The harness has no repair path. Where a model's answer diverges from the core's verdict,
the divergence is printed and the answer is kept verbatim in the trace.

The detector added this phase (`check_answer_is_not_a_raw_tool_call`) fires for **every**
scenario, is blocking, and is assembled so the literal envelope tag never appears as a
string in the module — the tag is built from fragments, and tests assert that the module
contains no `json.loads`, `re.search` or `re.findall` over a tool-call body. If that
module ever grows a client-side parser, the suite fails.

Its necessity is the point of §18: without it, the first S04 run exited **0** with an
answer that was raw tool-call text, because every existing check keyed on a core verdict
that never existed. The run that fails loudly is the run whose numbers a reader re-checks.

---

## 17. Parameters Varied, and Why

| Parameter | Values | Reason |
|-----------|--------|--------|
| `skill_enabled` | True, False | The single variable of the frozen experiment (spec 3.1) |
| `max_tokens` (S01 pair) | 2048 → 1024 | At 2048 the S01 chain reached a 14337-token prompt and the server refused the request: 14337 + 2048 = 16385 > 16384. Reduced **once, for both conditions of the pair identically**, so the pair stays single-variable |
| `max_tokens` (S04) | 1024, 2048, 3072 | Step 12's one-variable-at-a-time investigation of the turn-3 failure. All three recorded; none concealed |
| parser | never changed | `hermes` retained. §14 shows the parser's rule is correct and its input was malformed |
| `max_steps` | 12 (frozen) → 16 (smoke) | See below — the one deviation that is **load-bearing**, and it was left out of the first draft of this table |

On `max_steps`. The frozen default is **12**: `replay_runner.main` (`--max-steps` default
12), the `replay_scenario` and `run_agent` library defaults, and the Phase 3 StepFun
smoke all use 12, and `docs/evaluation_protocol.md` §3 lists "the same maximum number of
steps" among the frozen fairness constraints. The Phase 4 smoke driver defaults to **16**
(`--max-steps`, [scripts/test_local_agent.py](../scripts/test_local_agent.py)). Both S01
runs record `"max_steps": 16` in `sampling_params`, and both used 15 model turns.

The consequence is not cosmetic. `run_agent` loops `for step in range(1, max_steps + 1)`
and initialises `stop_reason` to `max_steps` with an empty `final_response`. Under the
frozen 12-step budget both S01 runs would have stopped at step 12 with
`stop_reason="max_steps"` and **no answer** — §11 and §12 are therefore not reproducible
under the frozen protocol as recorded, and the `evals/compare.py` fairness check does not
catch it because it compares within a pair, where both sides carried the same 16.

What is and is not affected: the S01 pair remains a valid single-variable comparison (the
deviation is uniform across both conditions, so no number inside the pair is wrong); what
is compromised is the *freeze's auditability*, because a parameter that decided whether
the runs could complete at all appears in every recorded trace with no justification in
the section whose job is to justify it. No test pinned the smoke's default to 12 — the
Phase 4 test suite pins 12 as the harness default while the driver that called it used 16.

Two consequences, recorded rather than smoothed over:

1. **Phase 5 must use one `max_steps` for every scored run, in both conditions**, and
   record its value in the manifest and in the traces. The 16 above is a Phase 4 driver
   default, not a renegotiated freeze; whether 12 or 16 is the right value is a decision
   to make *before* the first scored run and write down, not to discover afterwards.
2. This section is where a future reader looks for what moved. A parameter that moved and
   was not listed here is the exact failure this report exists to prevent, so it is listed
   now, with its consequence, instead of being quietly left at the value that happened to
   be typed.

---

## 18. Findings Register

**F-1 (high, unresolved) — the local model emits malformed JSON when it batches
`assess_risk` calls.** Three of four envelopes in S04 turn 3 carried one superfluous
closing brace. vLLM's Hermes parser discards the whole batch, so `assess_risk` never ran,
the deterministic core never returned a verdict, and the compound scenario produced no
answer. Repairing this is out of scope for Phase 4 and was not attempted.

**F-2 (high, unresolved) — the frozen 16384-token window cannot finish S04 with a
batching model.** Measured: 10893 + 2604 + 4 results ≈ 15589, leaving no headroom for a
final answer. `--max-model-len` is frozen and was not touched.

**F-3 (medium, unresolved) — the RED / R-B semantic checks were never exercised
locally.** Because F-1 prevented S04's core verdict from being computed,
`check_hard_rule_reported` and `check_answer_commands_a_stop` had nothing to check. This
phase therefore provides **no local evidence** about whether the model respects a hard
RED rule. It is a coverage gap, not a pass.

**F-4 (medium, fixed) — this phase's own smoke missed the §9 failure mode.** The first
S04 run exited 0. The answer was raw envelope text, but every existing check keyed on a
core verdict that did not exist. `check_answer_is_not_a_raw_tool_call` was added, with
tests covering both shapes (`complete but unparsed`, `truncated mid-envelope`) and a
structural guarantee that the module never parses what it rejects.

**F-5 (low, fixed) — three URL bugs in one preflight, any of which would have reported a
healthy server as down.** (i) `{base_url}/health` asked for `/v1/health` → 404; (ii)
`/health` has an empty body, so `json.loads("")` crashed before the script printed a
word; (iii) the fix introduced a second, inconsistent URL-building convention and produced
`/v1/v1/models` → 404. `_root`, `_status_of` and `MODELS_PATH` now build every URL the
same way, and `tests/test_local_agent_driver.py` pins the exact URLs probed. These are
recorded because a preflight that lies about a healthy deployment sends someone to
restart a service that was never broken, and a preflight that crashes before printing
looks like a broken endpoint.

**F-6 (informational, by design) — `EvidenceTrace` carries no token-usage field.** The
contract is `extra="forbid"` and frozen from Phase 0, so per-run token and latency numbers
are collected by a driver-level measurement wrapper and printed beside the run instead of
being added to the trace. Adding the field would mean editing a frozen Phase-0 schema,
which this phase does not do.

**F-7 (informational) — the model invents device ids.** `device_123`, `device_01` and
`hiker_device_01`, none of which exists. In run 1 it recovered from the tool error; in
run 2 it invented an id and never recovered. The device feed is never handed to the
agent, so the model has to guess, and a wrong guess is answered by a tool error rather
than by a silent fabrication — which is the correct behaviour of the frozen tool and is
recorded here as a model behaviour to watch, not as a harness defect.

**F-8 (medium, fixed) — the smoke's own severity test compared message text, and its
dispatch demoted any finding it did not recognise.** `main` decided blocking versus
recorded by checking whether a finding's *text* started with one of three hand-picked
prefixes. A check added later, or an existing message reworded, fell into the `else`
branch: the run printed `FINDING:`, left the trace, and exited **0** — a failed safety
check reported as a pass. That is the same class as F-4, one layer further in: F-4 was a
missing check, F-8 was a dispatch that could not fail loudly enough to be trusted with
the checks it already had. `trailguard/providers/local.py` states the rule for exceptions
("matched by *class*, never by message text"); the driver's one safety-critical decision
was made by exactly the mechanism that file names as wrong. Fixed by a typed `Finding`
carrying its own severity, and pinned by tests that read the dispatch with `ast` and
assert no string operation touches the finding at all.

---

## 19. Test Evidence (Step 24)

The full suite runs with no GPU, no container and no network: the autouse
`no_external_network` fixture closes `socket.connect`, `connect_ex`, `create_connection`
and `getaddrinfo`. The local-model drivers are excluded via `collect_ignore`, and the
exclusion is itself tested.

```
$ python -m pytest -v
766 passed in 9.79s

$ python -m trailguard.agent_smoke
20/20 checks passed
PASS: the offline runtime works end to end. This says nothing about model behaviour --
      no real model was called, and no skill-performance claim can be made from it.
```

New this phase:

| Module | Tests | What it pins |
|--------|-------|--------------|
| `tests/test_local_provider.py` | 66 | `LocalModelProvider` against a fake transport: defaults, env precedence, verbatim `tool_call.id`, JSON-only argument parsing with `invalid_tool_call` on garbage, no `eval`/`ast`, error classification by class not message, no fallback, no credential in any message |
| `tests/test_local_agent_driver.py` | 53 | The smoke's pure decisions: `_root`, the exact preflight URLs, the context limit read from the server, every semantic check, `RUN_PLAN`, exit-code documentation, the §9 detector's structural guarantee, and AST-level proof that no provider/model branch exists in the harness |

Mutation testing is the acceptance standard here: the earlier rounds mutated the provider
and the driver 19 + 6 ways (dropping `tool_call.id`, coercing arguments, swallowing a
transport error, softening a RED check, substring-matching a rule id, and a
`"Skill (tuned for the local model)"` mutation of the frozen asset) and every mutation was
caught by a failing test before it could pass.

---

## 20. Verdict

**PASS — Ready for Phase 5.**

The criterion is met. On both S01 conditions the full chain ran: the endpoint returned
real structured `tool_calls` → the harness executed the real TrailGuard tools against real
replay data → tool results came back → the model continued across fifteen turns → a final
response was produced. Twenty-eight structured tool calls round-tripped across that pair
(14 + 14), including batches of five and four in single messages. All four capability
gates pass, including the parallel-batch gate. `SKILL.md` is byte-unchanged, the harness has
no provider branch, and nothing fell back when the endpoint was unhappy.

Phase 5 inherits, explicitly:

1. **F-1**: the local model emits malformed JSON when it batches `assess_risk`. Any
   scenario whose model calls it in a batch will lose the whole batch. Not fixed, not
   worked around, not tuned away.
2. **F-2**: the frozen 16384 window cannot finish a compound scenario with a batching
   model. Compound scenarios are at risk locally regardless of F-1.
3. **F-3**: there is **no local evidence** on RED / hard-rule R-B respect. Do not assume
   the local model handles a RED verdict correctly because Step 5 Preview's behaviour was
   recorded in Phase 3.
4. **F-8's fix**: the smoke's severity is now a typed value, and Phase 5 can count
   blocking and recorded findings without re-deriving them from message text.
5. **§17's `max_steps` deviation**: the smoke ran at 16, not the frozen 12, and under 12
   the S01 runs would have produced no final answer. Phase 5 must fix one `max_steps`
   before its first scored run and record it in the manifest.

Nothing in Phase 4 is committed and nothing is pushed (Appendix B).

---

## Appendix B — Repository State

```
$ git status --short
 M .env.example
 M tests/conftest.py
 M tests/test_providers.py
 M tests/test_replay_runner.py
 M trailguard/agent/replay_runner.py
 M trailguard/providers/local.py
?? docs/phase4_final_report.md
?? scripts/test_local_agent.py
?? scripts/test_tool_calling.py
?? tests/test_local_agent_driver.py
?? tests/test_local_provider.py

$ git diff --stat
 .env.example                      |  20 +-
 tests/conftest.py                 |  11 +
 tests/test_providers.py           | 152 +++++++-
 tests/test_replay_runner.py       | 105 ++++++-
 trailguard/agent/replay_runner.py |  43 +++-
 trailguard/providers/local.py     | 560 +++++++++++++------
 6 files changed, 821 insertions(+), 70 deletions(-)

$ git log --oneline -1
64800f4 feat: agent skill runtime and tool orchestration (Phase 3)
```

Not committed, as the phase's acceptance rule requires: Phase 4 stays pending acceptance.
Not pushed. `runs/` is git-ignored, so the traces and the probe report live on disk and are
cited by path in this report rather than by commit. No `.env`, token, API key,
`~/.claude/settings.json` or SSH material was read, printed, committed or written into any
file, and no system or SSH configuration was changed. `scripts/test_tool_calling.py` is
additionally excluded from pytest so that the capability probe stays exactly as capable of
failing as the day it was run.

---

## Appendix A — Q&A

**Q1. Does the local server return native structured `tool_calls`?**
Yes. Gates A, B, C and D all return `message.tool_calls` as a JSON list of objects with
real ids and string `function.arguments`. Verified against the raw wire body, not through
an SDK.

**Q2. Was any tool call ever parsed out of `content` by this project?**
No. The probe and the smoke both *detect* the envelope-in-text failure and neither reads
it. Tests assert the modules contain no `json.loads`, `re.search` or `re.findall` over a
tool-call body, and the literal tag is assembled from fragments so it appears in no source
file.

**Q3. What parser is in use, and was it switched?**
`hermes`, server-side. Never switched. §14 shows the parser's rule is correct and its
input was malformed, so a switch would have concealed a model defect.

**Q4. What exactly broke in S04?**
The model emitted three of four `assess_risk` tool-call envelopes with one superfluous
closing brace. vLLM's Hermes parser is all-or-nothing per message, so those three
discarded the fourth (well-formed) call too, and the whole batch was degraded to
`content`. No `assess_risk` ran, so no verdict was computed.

**Q5. Was it a truncation problem?**
At `max_tokens=1024`, yes — that attempt shows `Unterminated string`. At 2048 and 3072,
no: the 3072 attempt stopped on its own (`finish_reason=stop`) with four complete
envelopes, and still failed on malformed JSON. Raising the budget made the cause
*visible*, not gone.

**Q6. Could S04 be fixed by a bigger context window?**
The window is frozen at 16384 and may not be changed. Measured need is ≈15589 tokens
before the final answer, so headroom does not exist. Even an unfrozen window would have
left F-1 in place.

**Q7. Does the harness contain any provider- or model-specific branch?**
No. The provider is injected. An AST-level test asserts no `if provider ==` and no
`"qwen" in model` in the agent path, and the only driver change is a CLI `--provider`
knob on `replay_runner.main()`.

**Q8. Was `SKILL.md` changed for the local model?**
No. sha256 `98fd007eb23a…`, unchanged since Phase 3, printed at the head of every smoke
run.

**Q9. Did the model respect the deterministic core's verdicts?**
On S01, yes — both conditions reported GREEN across all four horizons, matching the core.
On S04 this is **unknown**, because the core never ran (F-3). No local claim is made about
RED handling.

**Q10. What happens if the local endpoint is down?**
`LocalModelProvider` raises `ProviderUnavailable`, the run stops with `provider_error`,
and the smoke exits 3 (or 2 at preflight, writing no trace). There is no fallback to
StepFun or to any scripted provider.

**Q11. Does pytest need the GPU, Docker or localhost:8000?**
No. 766 tests in 9.79s with the network closed and both drivers in `collect_ignore`.

The SDK is a separate matter, and the first draft of this answer overstated it. `import
openai` is deferred to first use *inside* `LocalModelProvider._client()`, which is why the
**library module** imports on a machine without the SDK — but the suite is not the
library. `tests/test_local_provider.py` and `tests/test_stepfun_provider.py` import
`openai` and `httpx` at module scope, and both are declared as extras (`[providers]` and
`[weather]`) rather than base dependencies. A judge reproducing on `pip install -e .[dev]`
therefore gets a **collection ImportError**, not a pass. The honest reproduction command
is `pip install -e ".[dev,providers,weather]"`. The no-GPU / no-Docker / no-localhost rule
itself holds, and the autouse socket fixture plus `collect_ignore` are what enforce it.

So: 766 tests pass with the network closed, on a box with no GPU, no container and nothing
on port 8000 — and with `openai` and `httpx` installed.

**Q12. How were token usage and latency recorded, given `EvidenceTrace` has no such
field?**
A driver-level `_RecordingLocalProvider` wraps the real provider, delegates `generate`
untouched, and records per-turn prompt/completion tokens, wall time and finish reason. The
trace schema is `extra="forbid"` and frozen; adding a field would have meant editing a
Phase-0 contract.

**Q13. What were the measured costs?**
S01 with skill: 169,352 tokens (164,342 prompt / 5,010 completion), 197.0s, 25.47
completion tokens/s. S01 without skill: 144,809 (139,346 / 5,463), 211.9s, 25.81 tok/s.
S04: 32,893 tokens, 279.1s. Measurement only — no optimisation was attempted or claimed.

**Q14. Why does the prompt grow so fast?**
The OpenAI-compatible API is stateless: every turn re-sends the entire transcript,
including every tool result so far. S01's per-turn prompt climbed 8453 → 15116 across
fifteen turns. Tool results dominate, and `assess_risk` arguments re-send the whole nested
state.

**Q15. Is the container exposed publicly?**
No. The published port is `127.0.0.1:8000:8000`. The container-internal `--host 0.0.0.0`
is normal and was not misread as host exposure; no port, firewall or SSH setting was
changed.

**Q16. What was the only infrastructure change?**
Three compose lines: `--enable-auto-tool-choice`, `--tool-call-parser`, `hermes`. Backed up
first, diffed after, and the post-restart server log confirms exactly those settings.
`--max-model-len 16384`, `--max-num-seqs 1`, `--gpu-memory-utilization 0.70`, BF16, the
weights, the model path and the port binding are untouched.

**Q17. Did any secret get read, printed or committed?**
No. The local server authenticates nobody, so no credential exists for it.
`TRAILGUARD_LOCAL_API_KEY` is an empty structural placeholder. SDK construction errors are
reported as class names precisely so an echoed configuration cannot leak a credential. No
`.env`, token, `~/.claude/settings.json` or SSH material was read.

**Q18. Did Phase 4 make any benchmark or superiority claim?**
No. No StepFun-versus-local comparison, no statistics, no scenario expansion. The
`without_skill` run exists so Phase 5 has both conditions on record, and its two
divergences are described as observations, not scores.

**Q19. What went wrong in this phase's own tooling?**
Three preflight URL bugs, any of which would have reported a healthy deployment as down
(F-5); and a smoke script that exited 0 on a run whose answer was raw tool-call text
(F-4). Both are fixed and both are now covered by tests that failed before the fixes.

**Q20. What should Phase 5 do first?**
Treat F-1 as a known local-model failure mode and measure it before trusting any local
compound-scenario number: log, per run, how many tool-call envelopes the model emitted and
how many parsed. Do not repair it in the harness and do not reword the skill for it. If
compound scenarios are needed, decide explicitly how a lost batch is to be scored — that
is a benchmark-design decision, not a Phase 4 fix.
