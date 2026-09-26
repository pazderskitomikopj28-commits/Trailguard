#!/usr/bin/env python
"""``python scripts/test_stepfun_agent.py`` -- the live agent runtime, over the wire.

This is the one script in the repository that puts a real model in the TrailGuard
agent loop, and it is the Phase 3 counterpart of ``scripts/test_live_weather.py``:
pytest never runs it, and it is listed in ``tests/conftest.py::collect_ignore`` so
no run of the suite can reach it -- by accident, by ``pytest scripts/``, or by
anything else. The suite proves the runtime offline (``python -m
trailguard.agent_smoke``); this script proves the runtime against the model it
will actually be driven by (spec 14).

What it runs
============
Every scenario under ``scenarios/``, in *both* skill conditions, through the same
:func:`trailguard.agent.harness.run_agent`, with the same tool registry, the same
``temperature`` / ``max_tokens`` / ``max_steps`` and the same ``task`` and
``context``. ``skill_enabled`` is the only argument that differs between the two
runs of a scenario (spec 3.1), which is what makes the pair a single-variable
experiment rather than two separate runs that happen to be printed side by side.
Each run's evidence trace is written to ``runs/phase3/`` (git-ignored), and a batch
summary beside them.

What it does not do
===================
* It does not score the runs. Comparing no-skill against with-skill -- tool-call
  correctness, horizon coverage, freshness handling, honesty -- is the Phase 5
  benchmark's job, and a live driver that pre-judged its own results would be
  grading its own homework.
* It does not look for a credential. ``STEPFUN_API_KEY`` is either in the
  environment or it is not; when it is not, the script prints ``NOT RUN`` and
  exits non-zero. It reads no ``.env``, no key file and no settings file, and it
  never prints the key, the base URL with a token in it, or any part of a request
  (spec 16).
* It does not fake a run. A scenario whose tools fail produces a run that records
  the failures and is reported as failing; there is no substitution of a replay
  fixture for a live model turn, because a benchmark row for a run that never
  happened is worse than a missing row (spec 17).

Usage::

    python scripts/test_stepfun_agent.py                       # every scenario, both conditions
    python scripts/test_stepfun_agent.py --scenario scenarios/baseline/scn_clear_day_normal.yaml
    python scripts/test_stepfun_agent.py --conditions with_skill
    python scripts/test_stepfun_agent.py --max-steps 16

Exit codes: ``0`` every run completed, ``2`` credential unavailable (NOT RUN),
``3`` a provider/transport failure, ``4`` at least one run did not complete.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:  # allow running from any working directory
    sys.path.insert(0, str(REPO_ROOT))

from trailguard.agent.replay_runner import DEFAULT_OUTPUT_DIR, replay_dataset  # noqa: E402
from trailguard.providers.base import ProviderError  # noqa: E402
from trailguard.providers.stepfun import StepFunProvider  # noqa: E402
from trailguard.schemas.trace import _redact_secret_text  # noqa: E402

#: Printed verbatim when no credential is present (spec 16).
NOT_RUN = "NOT RUN -- credential unavailable: STEPFUN_API_KEY is not set."

#: How much of a model's final answer to echo. A live answer is the one part of a
#: run this console shows that came from the model rather than from a tool, so it
#: is capped, and the cap is not a place to paste a paragraph into a bug report.
ANSWER_PREVIEW_CHARS = 240


def _scenario_paths(arguments: argparse.Namespace) -> list[Path]:
    """The scenario files to replay: the ones named, or the whole dataset."""
    if arguments.scenario:
        return [Path(path) for path in arguments.scenario]
    found = sorted((REPO_ROOT / "scenarios").rglob("*.yaml"))
    return found


def _preview(text: str) -> str:
    """A short, redacted echo of one model answer.

    Passed through the same redaction the evidence trace applies, so the console
    can never show a string the saved trace would have hidden (spec 18). The
    private helper is imported deliberately for that reason: duplicating the
    pattern list here would let the two drift apart, and the whole point is that
    they cannot.
    """
    redacted = _redact_secret_text(text or "") or ""
    if len(redacted) <= ANSWER_PREVIEW_CHARS:
        return redacted.replace("\n", " ")
    return redacted[:ANSWER_PREVIEW_CHARS].replace("\n", " ") + " ..."


def _report(summary: object) -> str:
    """One line describing what a run actually did, with no verdict attached."""
    trace = summary.trace  # type: ignore[attr-defined]
    calls = [(record.tool.value, record.status) for record in trace.tool_calls]
    failed = [f"{name}={status}" for name, status in calls if status != "ok"]
    core = trace.core_risk_level.value if trace.core_risk_level else "-"
    weather = _redact_secret_text(trace.weather_source) or "-"
    line = (
        f"{summary.scenario_id:26s} {summary.condition:14s} {summary.stop_reason:16s} "
        f"steps={summary.steps:2d} calls={len(calls):2d} "
        f"horizons={len(trace.horizon_checks)} "
        f"core={core} skill={summary.skill_loaded} "
        f"skill_sha={trace.skill_sha256[:12] if trace.skill_sha256 else '-'} "
        f"weather={weather!r}"
    )
    if failed:
        line += f"  FAILED: {'; '.join(failed)}"
    return line


def _call_sequence(summary: object) -> str:
    """The tool calls in the order the model made them, with their outcomes.

    Printed because "calls=11" is a count and not a record: whether the horizons
    were each assessed, and whether a call failed, is what the sequence shows.
    """
    records = summary.trace.tool_calls  # type: ignore[attr-defined]
    if not records:
        return "(no tool calls: the model answered without calling one)"
    return " -> ".join(f"{record.tool.value}[{record.status}]" for record in records)


def main(argv: list[str] | None = None) -> int:
    """Replay the dataset over the live model, in both skill conditions."""
    parser = argparse.ArgumentParser(
        description="Run the TrailGuard agent against the live Step 5 Preview endpoint (never run by pytest)."
    )
    parser.add_argument(
        "--scenario",
        action="append",
        default=None,
        help="Scenario YAML to replay. Repeatable. Default: every file under <repo>/scenarios/.",
    )
    parser.add_argument(
        "--conditions",
        nargs="*",
        default=["without_skill", "with_skill"],
        choices=["without_skill", "with_skill"],
        help="Which conditions to run. Both, by default: one condition is not a comparison.",
    )
    parser.add_argument("--out", default=str(DEFAULT_OUTPUT_DIR), help="Directory for saved traces.")
    parser.add_argument("--max-steps", type=int, default=12, help="Model-turn budget for each run.")
    parser.add_argument("--temperature", type=float, default=0.0)
    parser.add_argument("--max-tokens", type=int, default=2048)
    arguments = parser.parse_args(argv)

    if not os.environ.get("STEPFUN_API_KEY"):
        # The honest answer to "no credential" is to say so and stop. This script
        # reads no key file, no .env and no settings file to find one, and it
        # never prints the name of a variable it did not find (spec 16).
        print(NOT_RUN)
        print("no trace was written and none was fabricated: a benchmark row for a run")
        print("that never happened would be worse than a missing row (spec 17).")
        return 2

    paths = _scenario_paths(arguments)
    if not paths:
        print(f"NOT RUN -- no scenario files found under {REPO_ROOT / 'scenarios'}")
        return 2

    print("TrailGuard Phase 3 live agent smoke (real model, real tools, real safety core)")
    print(f"model        : {StepFunProvider().model or '(STEPFUN_MODEL unset)'}")
    print(f"scenarios    : {', '.join(path.stem for path in paths)}")
    print(f"conditions   : {', '.join(arguments.conditions)}")
    print(f"sampling     : temperature={arguments.temperature} max_tokens={arguments.max_tokens} "
          f"max_steps={arguments.max_steps}")
    print(f"traces       : {arguments.out}/")
    print("no credential is printed and no request body is echoed (spec 16).")
    print()

    try:
        summaries = replay_dataset(
            scenario_paths=paths,
            model_provider=StepFunProvider(),
            output_dir=arguments.out,
            conditions=arguments.conditions,
            temperature=arguments.temperature,
            max_tokens=arguments.max_tokens,
            max_steps=arguments.max_steps,
        )
    except ProviderError as exc:
        # Class name only: an exception message from a transport can carry the
        # request it refused, and the request carries the credential.
        print(f"PROVIDER FAILURE: {type(exc).__name__}")
        print("the run stopped before producing an answer; nothing was scored.")
        return 3

    incomplete = 0
    for summary in summaries:
        print(_report(summary))
        print(f"             calls : {_call_sequence(summary)}")
        print(f"             trace : {summary.output_path}")
        print(f"             answer: {_preview(summary.final_response) or '(none: the run produced no answer)'}")
        if summary.stop_reason != "completed":
            incomplete += 1

    print()
    print(f"{len(summaries)} runs recorded under {arguments.out} (git-ignored).")
    print("this script reports what happened; it does not score it -- the no-skill /")
    print("with-skill comparison is the Phase 5 benchmark's job (spec 14).")
    if incomplete:
        print(f"{incomplete} run(s) did not complete: see the stop reasons above.")
        return 4
    return 0


if __name__ == "__main__":  # pragma: no cover - exercised by the command, not by pytest
    raise SystemExit(main())
