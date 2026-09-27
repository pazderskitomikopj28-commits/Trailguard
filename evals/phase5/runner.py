"""The Phase 5 runner: one scenario, both conditions, one repetition at a time.

This module deliberately does not reuse ``trailguard.agent.replay_runner``'s file
layout, but it reuses everything of that module that makes a run *fair* -- the
``run_agent`` harness call, ``scenario_registry``'s weather-mode binding and the
trace's own redaction policy. What it adds is what a *benchmark* needs and a
Phase 3 driver did not: a preserved record of every attempted tool call, the
provider's own error class, a repetition index, and an append-only directory.

WHY A RECORDING PROVIDER
========================
The evidence trace records every tool call that named a *registered* tool. A call
whose name is not in the registry produces no ``ToolCallRecord`` at all -- the
harness cannot name a tool the contract does not have, and says so rather than
inventing one. So ``trace.tool_calls`` under-counts exactly the calls M3 (valid
tool-call rate) is about. Counting them needs to happen at the seam where they
arrive, so the provider is wrapped rather than the harness patched: a wrapper is
identical in both conditions, adds no behaviour the model can observe, and leaves
``run_agent`` untouched.

The same wrapper is the only place the provider's error *class* survives. The
harness catches ``ProviderError`` and bare ``Exception`` identically and sets
``stop_reason='provider_error'``, which is right -- from the harness's side they
are the same event -- but it means the trace cannot tell a context-length
rejection from a timeout, and M16 needs that difference. The exception is caught,
named, and re-raised unchanged.

RUN IDENTITY
============
``run_id`` is ``<scenario_id>__<condition>__rep<repetition>``. A run id is never
overwritten: a replacement run gets its own id and records ``replaces`` and
``reason``, so a corrected run is visible as a correction rather than quietly
replacing the number a reader had already seen.
"""

from __future__ import annotations

import argparse
import functools
import json
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Sequence

from evals.phase5.manifest import (
    MAX_STEPS,
    MAX_TOKENS,
    REPETITIONS,
    REPO_ROOT,
    TEMPERATURE,
    TOOL_FAILURE_POLICY,
    RUN_ORDER,
)
from evals.phase5.preflight import preflight
from evals.phase5.references import BENCHMARK_SCENARIOS, BENCHMARK_VERSION, load_scenarios
from trailguard.agent.replay_runner import load_scenario, scenario_registry
from trailguard.providers.base import ModelProvider
from trailguard.schemas.scenario import Scenario, ToolName
from trailguard.schemas.trace import EvidenceTrace, _redact_secret_text

RAW_DIR = REPO_ROOT / "runs" / "phase5" / "raw"

CONDITIONS = ("without_skill", "with_skill")


# --------------------------------------------------------------------- wrappers


@dataclass
class ProviderTurn:
    """One model turn, reduced to what the benchmark may count.

    Only the tool-call *names* and the finish reason are kept. The content is not
    duplicated here: it is already in the trace's tool records and in the run's
    ``final_response``, and a second copy is a second thing to keep in sync.
    """

    tool_call_names: list[str] = field(default_factory=list)


@dataclass
class ProviderErrorRecord:
    """The provider's own name for what went wrong, with the message redacted."""

    class_name: str
    message: str | None

    def as_dict(self) -> dict[str, Any]:
        return {"class_name": self.class_name, "message": self.message}


class RecordingProvider:
    """A pass-through :class:`ModelProvider` that remembers each turn.

    Delegates every argument untouched and returns the response untouched; the
    wrapped provider is the only thing that talks to the model. It records the
    tool-call names of every successful turn and the class of the first failure,
    and re-raises that failure so ``run_agent`` records the stop reason exactly as
    it would have without the wrapper.
    """

    def __init__(self, inner: ModelProvider) -> None:
        self.inner = inner
        self.turns: list[ProviderTurn] = []
        self.provider_error: ProviderErrorRecord | None = None

    # -- identity, forwarded ---------------------------------------------------
    #: The harness reads ``provider.name`` and ``provider.model`` to label the
    #: trace, and a wrapper that answered for them would stamp every run in the
    #: benchmark with its own class name -- "RecordingProvider:model-unset" --
    #: which is exactly the identity a reader must be able to trust. Both are
    #: forwarded. When the wrapped provider reports no model string the gap stays
    #: a gap, because the harness records it as one.

    @property
    def name(self) -> str:
        return str(self.inner.name)

    @property
    def model(self) -> str | None:
        return getattr(self.inner, "model", None)

    # -- the ModelProvider surface, unchanged in behaviour ---------------------
    def generate(
        self,
        messages: Sequence[Any],
        *,
        tools: Sequence[dict[str, Any]] | None = None,
        temperature: float = 0.0,
        max_tokens: int = 2048,
    ) -> Any:
        try:
            response = self.inner.generate(
                messages,
                tools=tools,
                temperature=temperature,
                max_tokens=max_tokens,
            )
        except BaseException as exc:  # noqa: BLE001 - recorded, then re-raised verbatim
            if self.provider_error is None:
                self.provider_error = ProviderErrorRecord(
                    class_name=type(exc).__name__,
                    # The message is the one field that could carry a rejected
                    # credential; the trace's own text policy is applied to it
                    # rather than a private approximation of it.
                    message=_redact_secret_text(str(exc)),
                )
            raise
        names = [str(getattr(call, "name", "")) for call in (response.tool_calls or [])]
        self.turns.append(ProviderTurn(tool_call_names=names))
        return response

    # -- what the benchmark counts ------------------------------------------- #
    @property
    def attempted_tool_calls(self) -> int:
        """Every call the provider returned, including ones with no registry entry."""
        return sum(len(turn.tool_call_names) for turn in self.turns)

    @property
    def attempted_names(self) -> list[str]:
        return [name for turn in self.turns for name in turn.tool_call_names]

    @property
    def unknown_tool_calls(self) -> int:
        registered = {tool.value for tool in ToolName}
        return sum(1 for name in self.attempted_names if name not in registered)


# ------------------------------------------------------------------ run records


@dataclass
class RunRecord:
    """One preserved run, written verbatim under ``runs/phase5/raw/``."""

    payload: dict[str, Any]
    path: Path

    def save(self) -> Path:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(
            json.dumps(self.payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
        )
        return self.path


def run_id_for(scenario_id: str, condition: str, repetition: int) -> str:
    return f"{scenario_id}__{condition}__rep{repetition}"


def run_one(
    *,
    scenario: Scenario,
    model_provider: ModelProvider,
    skill_enabled: bool,
    repetition: int,
    output_dir: Path | None = None,
    replaces: str | None = None,
    reason: str | None = None,
) -> RunRecord:
    """Run one scenario in one condition and preserve the record.

    Everything the model was shown is replayed from the scenario; nothing here
    calls a tool. ``skill_enabled`` is the only argument that varies between the
    two records of a pair.

    ``output_dir`` is resolved at call time rather than bound as a default, so a
    test can redirect it. A bound default silently kept writing to the real
    ``runs/phase5/raw`` while the test believed it was writing to a scratch
    directory -- which is exactly the direction of bug a benchmark must not have.
    """
    from trailguard.agent.harness import run_agent

    directory = Path(output_dir) if output_dir is not None else RAW_DIR
    condition = "with_skill" if skill_enabled else "without_skill"
    recording = RecordingProvider(model_provider)
    registry = scenario_registry(scenario)

    run_id = run_id_for(scenario.scenario_id, condition, repetition)
    if replaces is None:
        target = directory / f"{run_id}.json"
        if target.exists():
            raise FileExistsError(
                f"{target} already exists. A run id is never overwritten. To correct it, "
                "call run_one(replaces=<the run id it replaces>, reason='why') and a new "
                "record with its own id is written beside it."
            )
    else:
        # A replacement is a NEW record with its OWN id -- <run_id>__r2, __r3 --
        # naming the record it supersedes. The superseded record is left exactly
        # as it was written, so a reader who already saw the old number can see
        # what changed and why, and the evaluator excludes only the record that
        # was named as superseded. Overwriting in place would make a correction
        # invisible, which is what this is designed to prevent.
        target = _next_replacement_path(directory, run_id, replaces)

    started = datetime.now(timezone.utc)
    result = run_agent(
        task=scenario.task_prompt,
        context=scenario.initial_context,
        model_provider=recording,
        skill_enabled=skill_enabled,
        tools=registry,
        max_steps=MAX_STEPS,
        temperature=TEMPERATURE,
        max_tokens=MAX_TOKENS,
        scenario_id=scenario.scenario_id,
        tool_failure_policy=TOOL_FAILURE_POLICY,  # type: ignore[arg-type]
    )
    finished = datetime.now(timezone.utc)
    trace: EvidenceTrace = result.trace

    payload: dict[str, Any] = {
        "run_id": run_id,
        "scenario_id": scenario.scenario_id,
        "condition": condition,
        "repetition": repetition,
        "benchmark_version": BENCHMARK_VERSION,
        "model_name": trace.model_name,
        "provider": trace.provider,
        "skill_loaded": trace.skill_loaded,
        "skill_version": trace.skill_version,
        "skill_sha256": trace.skill_sha256,
        "started_at": started.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "finished_at": finished.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "wall_clock_seconds": round((finished - started).total_seconds(), 3),
        # Frozen sampling, recorded per run so a drift is visible in the record.
        "temperature": TEMPERATURE,
        "max_tokens": MAX_TOKENS,
        "max_steps": MAX_STEPS,
        "tool_failure_policy": TOOL_FAILURE_POLICY,
        # What the trace cannot see.
        "attempted_tool_calls": recording.attempted_tool_calls,
        "attempted_tool_names": recording.attempted_names,
        "unknown_tool_calls": recording.unknown_tool_calls,
        "provider_error": recording.provider_error.as_dict()
        if recording.provider_error is not None
        else None,
        "provider_turns": len(recording.turns),
        # The trace is the evidence; the answer is the claim.
        "stop_reason": result.stop_reason,
        "steps": result.steps,
        "final_response": _redact_secret_text(result.final_response) or "",
        "trace": json.loads(trace.model_dump_json()),
    }
    if replaces is not None:
        if not reason:
            raise ValueError(
                f"a replacement for {replaces!r} must record why it replaced it; a silent "
                "correction is indistinguishable from a rewrote result"
            )
        payload["replaces"] = replaces
        payload["reason"] = reason

    return RunRecord(payload=payload, path=target)


def _next_replacement_path(directory: Path, run_id: str, replaces: str) -> Path:
    """The next free ``<run_id>__rN.json`` beside the record being superseded.

    ``replaces`` must name a record that exists: replacing something that was
    never written is a caller error, not a new fact.
    """
    if not (directory / f"{replaces}.json").is_file():
        raise FileNotFoundError(
            f"{replaces!r} is not a record in {directory}; a replacement must name one"
        )
    index = 2
    while (directory / f"{run_id}__r{index}.json").is_file():
        index += 1
    return directory / f"{run_id}__r{index}.json"


# --------------------------------------------------------------------- the batch


def load_scenario_for_benchmark(scenario_id: str) -> tuple[Path, Scenario]:
    for scenario_path, scenario in load_scenarios():
        if scenario.scenario_id == scenario_id:
            return scenario_path, scenario
    raise KeyError(f"{scenario_id!r} is not one of the {len(BENCHMARK_SCENARIOS)} benchmark scenarios")


def planned_runs() -> list[tuple[str, str, int]]:
    """The exact batch, in the pre-registered order."""
    plan: list[tuple[str, str, int]] = []
    for repetition, condition in RUN_ORDER:
        for scenario_id in BENCHMARK_SCENARIOS:
            plan.append((scenario_id, condition, repetition))
    return plan


def run_benchmark(
    model_provider: ModelProvider | None = None,
    *,
    base_url: str | None = None,
    confirm: bool = False,
    on_record=None,
    plan: Sequence[tuple[str, str, int]] | None = None,
    log=None,
) -> int:
    """The scored batch: 84 runs in the pre-registered order.

    Three rules govern this function and they are the reason it is not a loop in a
    shell script.

    1. IT GATES ITSELF. The preflight runs first and any fatal failure aborts
       before a single model call. A benchmark that starts and then discovers the
       scenario set moved has already spent the results it was about to report.

    2. IT APPENDS. A run id that already exists on disk is skipped, never
       re-run and never overwritten. That makes the batch resumable -- a machine
       that died at run 40 continues at 41 -- while keeping every number a
       reader can see the number that was actually saved. A run the operator
       wants replaced is replaced through ``run_one(replaces=..., reason=...)``,
       which writes a new id.

    3. IT PAUSES ON AN INFRA ACCIDENT AND ONLY ON AN INFRA ACCIDENT. A transport
       failure -- timeout, connection refused, auth, rate limit, a 5xx -- means
       the evidence for that run is missing rather than negative, so continuing
       would record a *skill* failure that is really a *machine* failure. It stops,
       says which run it stopped on, and leaves the record on disk. A context-length
       rejection is not an infra accident: the request was made and answered and
       M16 counts it as a result.

    Returns 0 when the batch finished or resumed clean, 1 when it aborted.
    """
    out = log or (lambda message: print(message))
    if not confirm:
        out(
            f"Refusing to run {len(planned_runs())} scored model calls without confirm=True. "
            "Run the preflight first, then call run_benchmark(confirm=True)."
        )
        return 1

    report = preflight(base_url=base_url)
    out("Phase 5 benchmark preflight")
    out(report.render())
    if report.failed:
        out(f"VERDICT: {report.verdict} -- the scored batch will not start.")
        for check in report.failed:
            out(f"  - {check.name}: {check.detail}")
        return 1
    out(f"VERDICT: {report.verdict}")

    provider = model_provider or make_provider()
    queue = list(plan) if plan is not None else planned_runs()
    out(f"{len(queue)} scored run(s) queued in the pre-registered order.")

    done = 0
    skipped = 0
    for index, (scenario_id, condition, repetition) in enumerate(queue, start=1):
        run_id = run_id_for(scenario_id, condition, repetition)
        preserved = existing_records(run_id)
        if preserved:
            skipped += 1
            newest = preserved[-1].name
            note = "" if newest == f"{run_id}.json" else f" (superseded by {newest})"
            out(f"[{index}/{len(queue)}] {run_id}: already preserved, skipping{note}")
            continue

        _, scenario = load_scenario_for_benchmark(scenario_id)
        record = run_one(
            scenario=scenario,
            model_provider=provider,
            skill_enabled=condition == "with_skill",
            repetition=repetition,
        )
        record.payload["scored"] = True
        record.save()
        done += 1
        payload = record.payload
        out(
            f"[{index}/{len(queue)}] {payload['run_id']}: "
            f"model={payload['model_name']} stop={payload['stop_reason']} "
            f"steps={payload['steps']} answer={len(payload['final_response'])} chars "
            f"tools={payload['attempted_tool_calls']}/{len(payload['trace']['tool_calls'])} ok "
            f"wall={payload['wall_clock_seconds']}s"
        )
        if on_record is not None:
            on_record(record)

        error = payload.get("provider_error")
        if error and not is_context_limit_error(error.get("message") or ""):
            out("")
            out(
                f"PAUSED after {run_id}: {error.get('class_name')} -- "
                f"{_redact_secret_text(str(error.get('message')))[:200]}"
            )
            out(
                "An infra accident is not a skill failure. The record is preserved, the run is "
                "excluded from the skill metrics, and the batch stops here. Fix the endpoint, "
                "then call run_benchmark(confirm=True) again: the finished runs are skipped."
            )
            return 1

    out("")
    out(f"Batch finished: {done} run(s) newly scored, {skipped} already preserved, "
        f"{len(list(RAW_DIR.glob('*.json')))} record(s) on disk.")
    return 0


def existing_records(run_id: str, directory: Path | None = None) -> list[Path]:
    """Every generation of one run id, oldest first.

    ``<run_id>.json`` plus any ``<run_id>__rN.json`` replacements. A batch that
    sees any of them treats the pair slot as filled, because the newest record is
    the current one -- but it does not delete the others.
    """
    store = Path(directory) if directory is not None else RAW_DIR
    return sorted(store.glob(f"{run_id}*.json"))


def is_context_limit_error(message: str) -> bool:
    """True when a provider error is a context-length rejection, not an outage.

    The same vocabulary ``evals.phase5.metrics`` freezes, imported rather than
    copied, so the runner's pause decision and the evaluator's M16 cannot drift
    apart: a message one calls a result and the other calls an outage would both
    pause the batch and count it as nothing.
    """
    from evals.phase5.metrics import CONTEXT_LIMIT_SUBSTRINGS

    lowered = (message or "").lower()
    return any(sub in lowered for sub in CONTEXT_LIMIT_SUBSTRINGS)


def run_warmup(model_provider: ModelProvider, *, scenario_id: str = "S01_clear_day") -> list[RunRecord]:
    """One unscored run per condition on the warm-up scenario.

    Traced, preserved and analysed -- and excluded from every metric. It exists to
    prove the endpoint answers before a batch of 84 runs is committed to, not to
    produce a preview of the result.
    """
    records = []
    for skill_enabled in (False, True):
        scenario_path, scenario = load_scenario_for_benchmark(scenario_id)
        record = run_one(
            scenario=scenario,
            model_provider=model_provider,
            skill_enabled=skill_enabled,
            repetition=0,
        )
        record.payload["scored"] = False
        record.payload["warmup"] = True
        record.save()
        records.append(record)
    return records


def make_provider() -> ModelProvider:
    """The local endpoint. No credential is read or required."""
    from trailguard.providers.local import LocalModelProvider

    return LocalModelProvider()


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Phase 5 benchmark runner.")
    parser.add_argument("--warmup", action="store_true", help="run the unscored warm-up only")
    parser.add_argument("--benchmark", action="store_true",
                        help="run the scored batch (requires --confirm)")
    parser.add_argument("--confirm", action="store_true",
                        help="acknowledge the scored batch makes real model calls")
    parser.add_argument("--scenario", default=None, help="restrict to one scenario id")
    parser.add_argument("--condition", choices=CONDITIONS, default=None)
    parser.add_argument("--repetition", type=int, default=None)
    parser.add_argument("--dry-run", action="store_true", help="print the plan and exit")
    args = parser.parse_args(list(argv) if argv is not None else None)

    if args.dry_run:
        plan = planned_runs()
        if args.scenario:
            plan = [row for row in plan if row[0] == args.scenario]
        if args.condition:
            plan = [row for row in plan if row[1] == args.condition]
        if args.repetition is not None:
            plan = [row for row in plan if row[2] == args.repetition]
        print(f"{len(plan)} run(s) planned:")
        for scenario_id, condition, repetition in plan:
            print(f"  {run_id_for(scenario_id, condition, repetition)}")
        return 0

    provider = make_provider()
    if args.warmup:
        records = run_warmup(provider, scenario_id=args.scenario or "S01_clear_day")
        for record in records:
            print(
                f"  warm-up {record.payload['run_id']}: stop={record.payload['stop_reason']} "
                f"steps={record.payload['steps']} answer={len(record.payload['final_response'])} chars"
            )
        print(f"{len(records)} unscored warm-up run(s) written to {RAW_DIR}")
        return 0

    if args.benchmark:
        plan = planned_runs()
        if args.scenario:
            plan = [row for row in plan if row[0] == args.scenario]
        if args.condition:
            plan = [row for row in plan if row[1] == args.condition]
        if args.repetition is not None:
            plan = [row for row in plan if row[2] == args.repetition]
        return run_benchmark(provider, confirm=args.confirm, plan=plan)

    print(
        "The scored batch is run by run_benchmark() (or --benchmark --confirm), which checks "
        f"the preflight first. Refusing to start {len(planned_runs())} scored runs from a bare "
        "command line.",
        file=sys.stderr,
    )
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
