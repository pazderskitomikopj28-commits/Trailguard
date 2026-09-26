"""Replay-mode driver for agent runs: one scenario, both conditions, saved traces.

Spec 13 keeps the benchmark in *Replay Mode*: the route, the weather snapshot and
the movement/device timelines are fixed inside a ``Scenario``, so the same run can
be repeated and a no-skill / with-skill pair is a controlled comparison (spec 3.1,
17). This module is the seam that turns a scenario file into agent runs and turns
agent runs into files a judge can read.

What it does
============
* loads a scenario from YAML and validates it against the frozen contract;
* calls :func:`~trailguard.agent.harness.run_agent` -- the *same* harness, the
  *same* ``run_agent`` -- once per condition, with identical tools, sampling
  parameters, task text and context snapshot; the only thing that differs is
  ``skill_enabled``;
* writes one JSON file per run under ``runs/phase3/`` (git-ignored) holding the
  evidence trace, the stop reason and the final answer;
* computes a handful of **structural** observations about the final answer.

What it deliberately does not do
================================
It never calls a tool. Every tool call in a replayed run is the model's, executed
by the harness inside ``run_agent``: a driver that called tools itself would be
handing the baseline a plan, and the measured difference would be the driver's,
not the skill's. ``tests/test_replay_runner.py`` asserts the seam structurally.

It also never *scores* a run. ``final_answer_findings`` reports string facts -- how
long the answer is, which canonical horizon labels appear in it, whether the banned
accident-probability phrase appears in it -- because those are checkable without an
opinion about English. Deciding whether an explanation was honest, complete or
compatible with the safety core is ``evals/`` work (Phase 5) and belongs nowhere
near the thing that produced the transcript.

``python -m trailguard.agent.replay_runner`` runs a whole scenario dataset through
both conditions. It needs a real model, so without a credential it reports
``NOT RUN`` and exits non-zero rather than fabricating a run from a replay fixture
(spec 17: a frozen snapshot substituted for a live call is a fabricated answer
wearing a reproducible costume). A transport failure is reported by exception
*class name* and exits ``3`` rather than escaping as a traceback: the chained SDK
error can echo the request it refused, and the request carries the credential.
"""

from __future__ import annotations

import argparse
import functools
import json
import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Sequence

import yaml

from ..providers.base import ModelProvider, ProviderError
from ..schemas.scenario import Scenario, ToolName
from ..schemas.trace import EvidenceTrace, _redact_secret_text
from ..tools import TOOL_FUNCTIONS
from .harness import AgentRunResult, run_agent
from .skill_loader import REPO_ROOT

__all__ = [
    "DEFAULT_OUTPUT_DIR",
    "FinalAnswerFindings",
    "ReplayRunSummary",
    "final_answer_findings",
    "load_scenario",
    "replay_dataset",
    "replay_scenario",
    "scenario_registry",
]

#: The *saved* form of the model's final answer: the same redaction the evidence
#: trace applies at its own contract boundary, imported from there rather than
#: re-implemented so the two cannot drift apart. The other artifact this module
#: writes (the run file) carries the answer as free text, which no contract
#: validator sees -- so a model that echoes a credential-shaped string would
#: otherwise write it straight into a file the benchmark later reads, exactly the
#: leak :class:`~trailguard.providers.stepfun.StepFunProvider` documents as
#: covered. Over-redaction is accepted on purpose, for the same reason the trace
#: accepts it (spec 16, 18).
def _saved_answer(answer: str) -> str:
    """Return the redacted text that will be written to disk for ``answer``."""
    return _redact_secret_text(answer or "") or ""

#: Where replayed traces are written. Inside the repository but git-ignored: a run
#: is evidence, not a source file, and a benchmark directory that grows unbounded
#: has no business in version control (spec 13, 24).
DEFAULT_OUTPUT_DIR: Path = REPO_ROOT / "runs" / "phase3"

#: The two conditions of the single-variable experiment (spec 3.1).
CONDITIONS: tuple[str, ...] = ("without_skill", "with_skill")

#: ``(canonical horizon label, spellings a plain answer might use)`` pairs, in the
#: frozen order. The canonical label comes from the contract; the rest are the
#: forms a model actually writes. Only used for a *presence* check -- never to
#: infer that a horizon was analysed.
#:
#: Deliberately a tuple of pairs rather than a mapping keyed by horizon: a dict
#: literal keyed by ``"+30min"`` is exactly the shape ``test_tool_contracts.py``
#: forbids in package code (spec 4.2 bans a single call that answers the whole
#: horizon set), and a spelling table has no business looking like the result
#: payload of a banned one-shot tool.
_HORIZON_SPELLINGS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("NOW", ("now", "current", "present", "immediate")),
    ("+30min", ("+30", "30 min", "30min", "30 minutes", "next 30")),
    ("+60min", ("+60", "60 min", "60min", "60 minutes", "next 60", "1 hour")),
    ("+120min", ("+120", "120 min", "120min", "120 minutes", "2 hours", "two hours")),
)

#: The banned identifier (spec 6.2, 12): no metric may be reported as a real-world
#: accident rate, and no code, field or answer may claim one. Checked as a
#: *presence* scan, which is all a string can honestly do -- see the module note on
#: why this is not a verdict.
BANNED_CLAIM_MARKERS: tuple[str, ...] = (
    "accident probability",
    "accident_probability",
    "probability of accident",
)


@dataclass(frozen=True)
class FinalAnswerFindings:
    """String facts about one final answer -- not a judgement about it.

    Every field is a mechanical observation. ``horizons_mentioned`` answers "which
    of the four canonical horizon labels appear in this text", which is a coverage
    fact a benchmark needs; it does not answer "did the agent analyse that horizon"
    -- that is what the evidence trace records, from tool calls.

    ``mentions_banned_claim`` is a *presence* scan and is named accordingly, which
    is a deliberate weakness rather than an oversight. "The accident probability of
    this trail is 3%" and "there is no accident probability to report" both contain
    the banned phrase, and no substring test can tell the claim from the denial --
    so the finding reports that the phrase appears and lets the evaluator decide
    what the appearance means. Naming it ``makes_banned_claim`` would have made a
    denial look like a violation, and a benchmark that scores a careful answer as
    a protocol failure is worse than no benchmark at all.
    """

    length_chars: int = 0
    is_empty: bool = True
    horizons_mentioned: tuple[str, ...] = ()
    mentions_banned_claim: bool = False

    def as_dict(self) -> dict[str, Any]:
        """JSON-ready form for the saved run file."""
        return {
            "length_chars": self.length_chars,
            "is_empty": self.is_empty,
            "horizons_mentioned": list(self.horizons_mentioned),
            "mentions_banned_claim": self.mentions_banned_claim,
        }


@dataclass(frozen=True)
class ReplayRunSummary:
    """One replayed run: its identity, its outcome, and where it was written."""

    scenario_id: str
    condition: str
    run_id: str
    stop_reason: str
    steps: int
    skill_loaded: bool
    model_name: str
    provider: str
    final_response: str
    findings: FinalAnswerFindings
    trace: EvidenceTrace
    output_path: Path
    recorded_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    def as_dict(self) -> dict[str, Any]:
        """The full run file: outcome, structural findings and the evidence trace."""
        return {
            "scenario_id": self.scenario_id,
            "condition": self.condition,
            "run_id": self.run_id,
            "stop_reason": self.stop_reason,
            "steps": self.steps,
            "skill_loaded": self.skill_loaded,
            "model_name": self.model_name,
            "provider": self.provider,
            "final_response": self.final_response,
            "findings": self.findings.as_dict(),
            "trace": json.loads(self.trace.model_dump_json()),
        }


def final_answer_findings(answer: str) -> FinalAnswerFindings:
    """Return the structural observations of one final answer.

    Responsibility: mechanical facts only -- length, emptiness, which canonical
    horizon labels appear, and whether the banned phrase appears somewhere in the
    text. Not responsible for: deciding whether the answer was good, honest,
    complete or consistent with the deterministic core. Those are Phase 5 metrics,
    and a driver that pre-judged them would put the evaluator inside the thing
    being evaluated.
    """
    text = answer or ""
    lowered = text.lower()
    mentioned = tuple(
        horizon
        for horizon, spellings in _HORIZON_SPELLINGS
        if any(spelling in lowered for spelling in spellings)
    )
    return FinalAnswerFindings(
        length_chars=len(text),
        is_empty=not text.strip(),
        horizons_mentioned=mentioned,
        mentions_banned_claim=any(marker in lowered for marker in BANNED_CLAIM_MARKERS),
    )


def load_scenario(path: Path | str) -> Scenario:
    """Load and validate one scenario YAML file.

    Responsibility: read the file the caller named and hand back the frozen
    contract, so a typo in a scenario fails here instead of surfacing as an agent
    run against a half-parsed snapshot. Not responsible for: choosing which
    scenarios to run, or filling in defaults the file does not carry -- a scenario
    with no weather snapshot must stay one with no weather snapshot.
    """
    target = Path(path)
    payload = yaml.safe_load(target.read_text(encoding="utf-8"))
    return Scenario.model_validate(payload)


def scenario_registry(
    scenario: Scenario,
    *,
    tools: dict[ToolName, Callable[..., Any]] | None = None,
) -> dict[ToolName, Callable[..., Any]]:
    """Return the registry a scenario must be run with: the canonical tools with
    the scenario's own weather policy bound in.

    Why this exists (Phase 3 finding, recorded rather than worked around)
    ====================================================================
    ``GetWeatherForRouteTimeInput`` -- the frozen contract the harness validates
    the model's arguments against -- carries exactly ``lat``, ``lon`` and
    ``target_time``. A scenario, meanwhile, declares how weather may be obtained
    (``initial_context.weather_policy``: ``mode: replay`` plus a
    ``snapshot_ref``), and the tool's *Python* signature has the keyword-only
    ``policy`` / ``provider`` arguments to reach that mode. Those two facts do not
    meet: a replay scenario replayed through the bare
    ``trailguard.tools.TOOL_FUNCTIONS`` registry cannot express "use your own
    frozen snapshot", so the weather tool falls back to its documented live
    default and the run queries live weather instead of the fixture the scenario
    names -- which makes the run non-reproducible, and, with the frozen scenario
    dates now outside the live provider's range, an outright failure.

    That is a gap in the frozen tool contract, not something this function should
    fix: widening ``GetWeatherForRouteTimeInput`` to carry a policy would change
    the Phase 1 tool surface the benchmark is frozen against, and it is recorded
    as a finding for the phase that owns it. What this function does instead is
    use the documented dependency-injection seam -- ``policy=``, which the tool
    exposes precisely so "a test or a demo pins a mode without touching the policy
    schema" -- to bind the scenario's declared mode in at the registry level,
    where no contract changes and nothing is hidden from the model: the tool's
    advertised schema is still ``lat``/``lon``/``target_time``, and the mode is the
    one the scenario already declared.

    The registry returned is a fresh dict on every call. ``TOOL_FUNCTIONS`` is
    module-level and shared, so binding a policy into it in place would leak one
    scenario's weather mode into every run that followed -- including the other
    condition of the same experiment.

    Fairness (spec 3.1): the registry is derived from the scenario, so both the
    baseline and the treatment run of that scenario receive byte-identical
    registries. Nothing here reads ``skill_enabled``.
    """
    base = dict(tools) if tools is not None else dict(TOOL_FUNCTIONS)
    weather = base.get(ToolName.GET_WEATHER_FOR_ROUTE_TIME)
    if weather is None:
        return base
    base[ToolName.GET_WEATHER_FOR_ROUTE_TIME] = functools.partial(
        weather, policy=scenario.initial_context.weather_policy
    )
    return base


def replay_scenario(
    *,
    scenario: Scenario,
    model_provider: ModelProvider,
    skill_enabled: bool,
    output_dir: Path | str = DEFAULT_OUTPUT_DIR,
    tools: dict[ToolName, Callable[..., Any]] | None = None,
    temperature: float = 0.0,
    max_tokens: int = 2048,
    max_steps: int = 12,
    tool_failure_policy: str = "report",
) -> ReplayRunSummary:
    """Replay one scenario in one condition and write its evidence trace.

    Fairness contract (spec 3.1): ``task`` is ``scenario.task_prompt``, ``context``
    is ``scenario.initial_context``, and every other argument is one the caller
    fixed once for the whole batch. ``skill_enabled`` is the only value that moves
    between a baseline run and a treatment run of the same scenario -- which is
    what makes the pair a single-variable experiment rather than two runs.

    The registry handed to the harness is :func:`scenario_registry`, so the
    scenario's declared weather mode is honoured and both conditions of the same
    scenario receive byte-identical registries.
    """
    condition = "with_skill" if skill_enabled else "without_skill"
    result: AgentRunResult = run_agent(
        task=scenario.task_prompt,
        context=scenario.initial_context,
        model_provider=model_provider,
        skill_enabled=skill_enabled,
        tools=scenario_registry(scenario, tools=tools),
        max_steps=max_steps,
        temperature=temperature,
        max_tokens=max_tokens,
        scenario_id=scenario.scenario_id,
        tool_failure_policy=tool_failure_policy,  # type: ignore[arg-type]
    )

    # Measured on the text that will actually be saved, so the run file is
    # internally consistent: `length_chars` describes the answer on disk.
    answer = _saved_answer(result.final_response)
    findings = final_answer_findings(answer)
    target_dir = Path(output_dir) / scenario.scenario_id
    target_dir.mkdir(parents=True, exist_ok=True)
    output_path = target_dir / f"{condition}.json"
    summary = ReplayRunSummary(
        scenario_id=scenario.scenario_id,
        condition=condition,
        run_id=result.run_id,
        stop_reason=result.stop_reason,
        steps=result.steps,
        skill_loaded=result.trace.skill_loaded,
        model_name=result.trace.model_name,
        provider=result.trace.provider,
        final_response=answer,
        findings=findings,
        trace=result.trace,
        output_path=output_path,
    )
    output_path.write_text(
        json.dumps(summary.as_dict(), indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    return summary


def replay_dataset(
    *,
    scenario_paths: Sequence[Path | str],
    model_provider: ModelProvider,
    output_dir: Path | str = DEFAULT_OUTPUT_DIR,
    conditions: Iterable[str] = CONDITIONS,
    tools: dict[ToolName, Callable[..., Any]] | None = None,
    temperature: float = 0.0,
    max_tokens: int = 2048,
    max_steps: int = 12,
    tool_failure_policy: str = "report",
) -> list[ReplayRunSummary]:
    """Replay every scenario in every condition, then write a batch summary.

    The summary file is deliberately terse: run ids, stop reasons and the
    structural findings, so a judge can see the shape of a batch without opening
    every trace. The traces themselves are the evidence; the summary is an index.
    """
    summaries: list[ReplayRunSummary] = []
    for path in scenario_paths:
        scenario = load_scenario(path)
        for condition in conditions:
            summaries.append(
                replay_scenario(
                    scenario=scenario,
                    model_provider=model_provider,
                    skill_enabled=condition == "with_skill",
                    output_dir=output_dir,
                    tools=tools,
                    temperature=temperature,
                    max_tokens=max_tokens,
                    max_steps=max_steps,
                    tool_failure_policy=tool_failure_policy,
                )
            )

    batch_dir = Path(output_dir)
    batch_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    (batch_dir / f"summary_{stamp}.json").write_text(
        json.dumps(
            {
                "recorded_at": datetime.now(timezone.utc).isoformat(),
                "model_name": summaries[0].model_name if summaries else None,
                "provider": summaries[0].provider if summaries else None,
                "sampling": {"temperature": temperature, "max_tokens": max_tokens, "max_steps": max_steps},
                "runs": [
                    {
                        "scenario_id": summary.scenario_id,
                        "condition": summary.condition,
                        "run_id": summary.run_id,
                        "stop_reason": summary.stop_reason,
                        "steps": summary.steps,
                        "findings": summary.findings.as_dict(),
                        "trace": str(summary.output_path),
                    }
                    for summary in summaries
                ],
            },
            indent=2,
            ensure_ascii=False,
        )
        + "\n",
        encoding="utf-8",
    )
    return summaries


# --------------------------------------------------------------------------- #
# Command line: ``python -m trailguard.agent.replay_runner``
# --------------------------------------------------------------------------- #


def _provider_from_environment() -> ModelProvider:
    """Build the Step 5 Preview provider from the environment, or fail loudly."""
    from ..providers.stepfun import StepFunProvider

    return StepFunProvider()


def main(argv: Sequence[str] | None = None) -> int:
    """Replay the dataset through both conditions over the configured model.

    Without ``STEPFUN_API_KEY`` there is no model to run, and the honest answer is
    to say so and exit non-zero: a driver that substituted a stub, a cached trace
    or a replay fixture would produce a benchmark row for a run that never
    happened (spec 17).
    """
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0] if __doc__ else None)
    parser.add_argument(
        "--scenarios",
        nargs="*",
        default=None,
        help="Scenario YAML files. Default: every file under <repo>/scenarios/.",
    )
    parser.add_argument("--out", default=str(DEFAULT_OUTPUT_DIR), help="Directory for saved traces.")
    parser.add_argument("--max-steps", type=int, default=12)
    parser.add_argument("--temperature", type=float, default=0.0)
    parser.add_argument("--max-tokens", type=int, default=2048)
    parser.add_argument(
        "--conditions",
        nargs="*",
        default=list(CONDITIONS),
        choices=list(CONDITIONS),
        help="Which conditions to run. Both, by default: a one-condition batch is not a comparison.",
    )
    arguments = parser.parse_args(argv)

    if not os.environ.get("STEPFUN_API_KEY"):
        print("NOT RUN -- credential unavailable: STEPFUN_API_KEY is not set.")
        print("This driver needs a live model; no trace was written and none was fabricated.")
        return 2

    paths = (
        [Path(path) for path in arguments.scenarios]
        if arguments.scenarios
        else sorted((REPO_ROOT / "scenarios").rglob("*.yaml"))
    )
    if not paths:
        print(f"NOT RUN -- no scenario files found under {REPO_ROOT / 'scenarios'}")
        return 2

    try:
        summaries = replay_dataset(
            scenario_paths=paths,
            model_provider=_provider_from_environment(),
            output_dir=arguments.out,
            conditions=arguments.conditions,
            temperature=arguments.temperature,
            max_tokens=arguments.max_tokens,
            max_steps=arguments.max_steps,
        )
    except ProviderError as exc:
        # Class name only, for the same reason the live script prints only the
        # class name: a transport exception chains the SDK error as ``__cause__``,
        # and an endpoint that echoes the request it refused (a gateway, a proxy)
        # would carry the request body and the Authorization header into a printed
        # traceback -- into a terminal, a CI log or a bug report. The class name
        # says "no answer exists"; the detail would say what the key was.
        print(f"PROVIDER FAILURE: {type(exc).__name__}")
        print("the run stopped before producing an answer; no trace was written and nothing was scored.")
        return 3
    for summary in summaries:
        print(
            f"{summary.scenario_id:32s} {summary.condition:14s} {summary.stop_reason:16s} "
            f"steps={summary.steps:2d} -> {summary.output_path}"
        )
    return 0


if __name__ == "__main__":  # pragma: no cover - exercised by the live smoke script
    raise SystemExit(main())
