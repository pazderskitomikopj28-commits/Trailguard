# TrailGuard — Final Submission Checklist

Frozen state of the repository at hand-off. Every value below was read from the
tool, not from memory: the commands that produced each row are given beside it so
a reviewer can re-run them and get the same answer.

| Field | Value |
| --- | --- |
| Final commit | HEAD. The Phase 6 freeze is `962a497` (**tag: `phase6-freeze`**); two documentation commits follow it — `1dd240d` (README to the submission standard + the three freeze-invalidated statements in the Phase 6 report corrected) and the commit adding this checklist. `git log --oneline --decorate -10` is the authority |
| Phase 6 freeze commit | `962a497` — *docs: complete TrailGuard Phase 6 competition demo* |
| Tags | `phase0-freeze`, `phase1-freeze`, `phase2-freeze`, `phase3-freeze`, `phase4-freeze`, `phase5-design-freeze`, `phase5-freeze`, `phase6-freeze` (HEAD is one commit after `phase6-freeze`) |
| Working tree | clean — `git status --short` empty, `git diff --stat` empty |
| Remote | none. Nothing was ever pushed |
| Test result | **1242 passed, 0 failed** — `pytest -q`, all offline (≈28 s) |
| Demo smoke result | **1876 checks, RESULT: PASS** — `scripts/demo_smoke.py` |
| Demo preflight result | **Verdict: RUNNABLE**, 24 ready, 0 missing, 1 degraded (`streamlit on PATH`; the operator uses `.venv/bin/streamlit` and `scripts/start_demo.sh` resolves it itself) |
| Submission archive | `/home/asus_gx10/trailguard-submission.tar.gz` |
| Archive size | 737883 bytes (720.6 KiB) |
| SHA256 | `6502527c74c138e57fd8d1eec7078e6747014e07bf451e841d7d57f758149284` |

```bash
sha256sum /home/asus_gx10/trailguard-submission.tar.gz
# 6502527c74c138e57fd8d1eec7078e6747014e07bf451e841d7d57f758149284
```

Rebuilding from HEAD instead of `1dd240d` gives a **different** digest, because
that tree contains this file: `8a9737203148e988327e94fd6b132fe7db0c02e31ce587a2f03e0a0828b68800`,
741618 bytes, 196 files. The shipped archive is the 195-file one at `1dd240d`, so
its digest can be recorded here; a tree cannot contain the archive built from
itself.

## The 19 checklist items

1. **Phase 6 acceptance recorded** — `docs/phase6_final_report.md` §20: Phase 6
   **PASS** on all 16 criteria.
2. **Phase 6 frozen, nothing tracked moved** — `git diff --stat` empty before the
   freeze commit; the freeze commit added 26 files with **8323 insertions and zero
   deletions**, so no tracked file could have been removed or rewritten. Staged
   path by path, never `git add .`.
3. **No new business code in Phase 7** — the only files modified after the freeze
   commit are `README.md` and `docs/phase6_final_report.md`, and the only file
   created is this checklist. No rule, tool, contract, model setting, scenario or
   metric was touched.
4. **README answers the seven questions** in the first screen: what TrailGuard is,
   why it is different, how it works, how to run the demo, how to run the tests,
   where the Skill is (`SKILL.md`), where the evidence is
   (`docs/phase5_evidence/` + the frozen archive).
5. **README states the positioning and the division of labour** — "TrailGuard is a
   Predictive Outdoor Safety Skill" and "LLM does not calculate the risk level. The
   deterministic Safety Core does."
6. **README shows the core chain** — future route position + future arrival time +
   future weather + movement/device state → Deterministic Safety Core → Projected
   Risk State → Agent explanation.
7. **README gives the simplest launch and labels both modes** — `./scripts/start_demo.sh`,
   with Replay Mode marked *No network required / No LLM required* and Local Agent
   Mode marked Qwen3-VL-30B-A3B at `http://127.0.0.1:8000/v1`.
8. **12-step competition walkthrough confirmed** — written out in
   `docs/phase6_final_report.md` §18 and walked end to end against the real local
   model: open → future-weather scenario → NOW safe → +120 hazard → projected
   position → future weather → deterministic level → driver → evidence trace →
   optional local agent → Benchmark tab → MIXED explained honestly with both sides.
9. **Benchmark wording is compliant** — the screen never says the skill made the
   model safer overall. It says **MIXED**, quotes `no_composite_score = True`,
   shows all 18 metrics in both conditions with each metric's own direction, and
   states 4 right way / 4 wrong way / 10 unchanged. No composite, no net, no
   "overall". `Benchmark Integrity: PASS` and `Skill Effectiveness: MIXED` are
   both the headline, in the README and in the Phase 5 report.
10. **Final regression green** — `pytest -q` → **1242 passed**; demo smoke →
    **1876 checks, PASS**; demo preflight → **RUNNABLE**. No test was added or
    edited to make this true.
11. **Streamlit actually launched and all five screens verified** — the real server
    was started on localhost, answered HTTP 200 on `/` and `/_stcore/health`, and
    the real `app.py` was driven through Streamlit's own runtime: Overview, Risk
    Timeline, Evidence, Benchmark and Architecture all rendered with no exception
    and no forbidden string (no traceback, no `File "`, no `ssh://`, no
    `STEPFUN_API_KEY`, no home path, no `0.0.0.0`). The weather radio defaults to
    index 0 = Replay, and no button is on screen before a click.
12. **Agent Mode verified live, not assumed** — Qwen was online, so the optional
    step was run through the UI for real: 11 tool calls, stop reason *finished with
    an answer*, 105.4 s wall clock, core verdict GREEN, agent claim unreadable so
    the panel reported that no comparison was possible rather than asserting one,
    and the answer shown verbatim. Submission was not blocked on this step; if the
    endpoint had been down the panel would have said `Local Agent unavailable`.
13. **Artifact hashes verified on screen** — SKILL.md
    `98fd007e…`, `rules_v1.yaml` `b2630f3d…`, `calibration_v1.yaml` `05bb20a1…`,
    and the four evidence files match the Phase 5 freeze; the Architecture tab
    marks every one "matches the evaluated hash".
14. **Secret sweep clean** — 195 tracked files scanned for high-confidence secret
    patterns and for assigned credential literals: none. No `.env` exists anywhere
    in the tree and none is tracked. `.env.example` is tracked and gives a
    non-empty value only to non-secret settings (`STEPFUN_BASE_URL`,
    `STEPFUN_MODEL`, `TRAILGUARD_LOCAL_BASE_URL`, `TRAILGUARD_LOCAL_MODEL`); every
    key field is empty.
15. **Submission archive clean** — 195 files. Excluded and verified absent:
    `.venv/`, `__pycache__/`, `.pytest_cache/`, `runs/phase5/raw/`, `runs/demo/`,
    `logs/`, `*.log`, `*.tmp`, `.env`, `*.so`, `*.pth`, `*.safetensors`, model
    weights, HF cache, the Qwen deployment directory. Included and verified
    present: `README.md`, `SKILL.md`, `trailguard/`, `demo/`, `scripts/`,
    `tests/`, `scenarios/`, `replay/`, `evals/`, `docs/` (including
    `docs/phase5_evidence/`), `pyproject.toml`, `requirements.txt`, `.env.example`.
16. **Archive re-verified by extraction** — extracted to a bare directory and run
    there: demo smoke **1876 checks PASS**, preflight **RUNNABLE** (23 ready, 0
    missing, 2 degraded — `runs/` is gitignored so absent by design). Tests:
    **1240 passed, 2 failed**. The two failures are `tests/test_secrets_git.py`'s
    `test_git_confirms_dot_env_is_excluded` and `test_scanned_set_is_not_empty`,
    which shell out to `git ls-files` / `git check-ignore` and therefore only apply
    to a git working tree. They are not regression: the same command inside the
    repository is **1242 passed, 0 failed**.
17. **No composite score, no rounding of the evidence** — the Benchmark tab reads
    the four committed files and writes nothing; the only derived number on that
    screen is the completion rate's percentage, suppressed for any metric whose
    denominator is not the run count. `docs/phase5_evidence/` was never recomputed,
    re-normalised or re-rounded.
18. **Known limitations disclosed, not hidden** — see below; they are also stated
    in the README and in `docs/phase6_final_report.md` §19.
19. **Nothing pushed, no external action taken** — the repository has no remote.
    Phase 7 ends here, waiting for human submission.

## Known limitations

- **Local 30B, long tool chains hit context pressure.** The skill's result is
  **MIXED**, not a win: on the current local model and 16K context window the
  longer orchestration chain the Skill induces also brings substantial reliability
  and context-budget costs. Completion fell from 20 of 32 scored baseline runs
  (62.5%) to 2 of 32 with the skill (6.25%).
- **Competition thresholds are demo calibration.** Every threshold is
  calibration-marked and set for competition demonstration. They are not industry
  safety standards, and no scenario metric may be reported as a real-world
  accident rate.
- **Synthetic and replay scenarios are used for the deterministic demonstration.**
  That is deliberate — it makes the presentation reproducible with no network and
  no model — but it means the walkthrough reproduces a frozen measured result
  rather than sampling a live one. TrailGuard is an outdoor-safety decision-support
  prototype, not a medical device, rescue system, official warning system or
  accident-probability predictor.
- **The submission archive is a source archive, not a git checkout.** Two
  secret-hygiene tests require a git working tree and are expected to fail
  outside one (item 16). Inside the repository all 1242 tests pass.

## Scope of what Phase 7 changed

| Path | Change |
| --- | --- |
| `README.md` | rewritten to the submission standard; every number re-read from `docs/phase5_evidence/` and the demo's own presenter |
| `docs/phase6_final_report.md` | three statements made false by the freeze commit rewritten in the past tense with the freeze recorded; item 21 added disclosing that this happened |
| `docs/phase6_demo.md` | (committed with the Phase 6 freeze) pre-recount line totals corrected to the measured 4389 / 2538 / 1851 / 933 |
| `docs/submission_checklist.md` | this file |

Nothing else. No rule, tool, contract, scenario, metric, model setting or
benchmark artefact was modified.

---

*Built from the tree at commit `1dd240d`. The archive therefore does not contain
this file, because an archive cannot contain the digest of itself — the digest
above is of the archive built from that commit, before this checklist existed.
`sha256sum` it to confirm.*
