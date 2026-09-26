"""Offline end-to-end smoke of the Phase 3 agent runtime.

    python -m trailguard.agent_smoke

What this proves
================
That the whole Phase 3 chain works together *offline*: one frozen replay
scenario, the real atomic tool registry, the real frozen contracts, the real
``run_agent`` loop, the real evidence trace -- and the same scripted "model"
driven through both skill conditions with nothing changed but ``skill_enabled``.

The result is a run of :func:`~trailguard.agent.harness.run_agent` in each
condition, with a real tool-orchestration policy, real tool payloads in the
transcript and a final answer assembled from the values the tools returned.

What this does NOT prove
========================
**No real model is called.** The provider here is
:class:`ScriptedProvider` -- a fixed, offline stand-in for the model that follows
a hand-written tool policy. It exists so the *runtime* can be smoke-tested on a
laptop with no credential and no network, and it says so on every line of output.

Consequently this script says nothing about whether ``SKILL.md`` makes a model
better at anything. It proves the harness is capable of carrying a multi-step
tool sequence and recording it; whether Step 5 Preview or the DGX Spark local
model chooses that sequence *because* of the skill is the benchmark's question
(spec 14), answered over a real model by
``scripts/test_stepfun_agent.py`` and :mod:`trailguard.agent.replay_runner`.

The scripted policy also does not branch on the system prompt. It reads the task
and the tool results and nothing else, which is what makes the two conditions
differ *only* by the injected skill text rather than by the stub's behaviour.
``test_the_two_conditions_produced_the_same_tool_sequence`` checks that: if this
stub ever started reading the skill text, its two tool sequences would diverge
and the single-variable comparison would be a fiction.

Exit status: 0 when every check passes, 1 when any check fails. A failing check
prints what it observed, so the output can be pasted into a bug report.
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

from .agent.harness import AgentRunResult, run_agent
from .agent.replay_runner import load_scenario, scenario_registry
from .agent.skill_loader import REPO_ROOT
from .providers.base import ChatMessage, ModelProvider, ModelResponse, ToolCallRequest
from .schemas.route import Horizon
from .schemas.scenario import ToolName
from .schemas.trace import EvidenceTrace
from .tools import TOOL_FUNCTIONS

__all__ = ["Check", "ScriptedProvider", "main", "run_smoke"]

#: The frozen horizon offsets, in the order the scripted policy visits them
#: (spec 6.1). Kept as a module constant so the plan, the assertions and the
#: printed report cannot disagree about what "all four horizons" means.
HORIZON_MINUTES: tuple[int, ...] = tuple(member.minutes for member in Horizon)

#: Replay device feed for the S01 baseline scenario. Not derived: the scenario
#: contract carries no device reference, so the one this smoke uses is named
#: here, in one place, rather than guessed from a naming convention. A wrong
#: value fails loudly -- the tool raises ``DeviceFeedError`` listing the feeds
#: that do exist -- instead of silently returning a different hiker.
DEFAULT_DEVICE_ID = "scn_device_S01_clear_day"

#: The frozen baseline scenario, matched by hand to that feed and to the route
#: asset the scenario's own context names.
DEFAULT_SCENARIO = REPO_ROOT / "scenarios" / "baseline" / "scn_clear_day_normal.yaml"

#: The tool-result prefix ``_summarize_result`` builds, so a payload can be split
#: off the readable label without matching the label's wording.
_RESULT_SEPARATOR = " -> "


@dataclass(frozen=True)
class Check:
    """One smoke assertion: what was checked, whether it held, what was seen."""

    name: str
    passed: bool
    detail: str

    def render(self) -> str:
        """One fixed-width line for the console report."""
        marker = "PASS" if self.passed else "FAIL"
        return f"[{marker}] {self.name}\n        {self.detail}"


class ScriptedProvider(ModelProvider):
    """An offline stand-in for the model that follows a fixed tool policy.

    It is a *stub*, and it is named and reported as one everywhere it appears.
    It exists because the harness needs *some* model to drive it, and a stub that
    is honest about being a stub is the only way to smoke the runtime on a
    machine with no credential.

    What it does: read the task and the tool results out of the transcript,
    decide the next call from what has already come back, and write a final
    answer assembled out of the values the tools returned.

    What it never does: invent a value. Every number in its final answer is
    copied from a tool payload it was handed; every argument it passes to a tool
    is copied out of an earlier tool payload or out of the context snapshot the
    harness put in the user message. It also never reads the system message, so
    the skill text cannot change its behaviour -- which is what keeps the two
    conditions comparable.
    """

    @property
    def name(self) -> str:
        """Recorded in the evidence trace, and visibly not a real backend."""
        return "scripted-smoke"

    def __init__(self, *, device_id: str = DEFAULT_DEVICE_ID) -> None:
        self._device_id = device_id
        self.turn = 0
        # Recorded in the evidence trace as the model string. Deliberately names
        # the stub and its policy version, so a saved trace can never be mistaken
        # for a run of a real model.
        self.model = "scripted-smoke-policy-v1"

    def __repr__(self) -> str:
        return f"ScriptedProvider(device_id={self._device_id!r}, model={self.model!r})"

    # ------------------------------------------------------------------ #
    # ModelProvider
    # ------------------------------------------------------------------ #

    def generate(
        self,
        messages: list[ChatMessage],
        tools: list[dict[str, Any]] | None = None,
        temperature: float = 0.0,
        max_tokens: int = 2048,
    ) -> ModelResponse:
        """Return the next scripted turn for ``messages``.

        The transcript is scanned for tool results only: ``messages[0]`` (the
        system prompt, with its optional skill text) is deliberately never read,
        and neither is anything the model might have said before. The policy is a
        pure function of the task, the context snapshot and the tool results.
        """
        self.turn += 1
        context = _context_snapshot(messages)
        results = _tool_results(messages)

        calls = self._next_calls(context, results)
        if calls:
            return ModelResponse(
                content=None,
                tool_calls=calls,
                finish_reason="tool_calls",
                usage={},
            )

        return ModelResponse(
            content=self._final_answer(context, results),
            tool_calls=[],
            finish_reason="stop",
            usage={},
        )

    # ------------------------------------------------------------------ #
    # The scripted policy
    # ------------------------------------------------------------------ #

    def _next_calls(
        self,
        context: dict[str, Any],
        results: dict[str, list[dict[str, Any]]],
    ) -> list[ToolCallRequest]:
        """Decide the next turn's calls from what the tools have returned.

        The order is the one this policy happens to follow -- the route, then the
        weather, then the device feed, then every projection, then every risk
        assessment. It is *a* workable order, not *the* protocol: spec 10's
        protocol lives in SKILL.md and belongs to the model, and a stub that
        claimed to encode it here would be quietly shipping a second,
        contradictory implementation of the skill.
        """
        if "get_route_context" not in results:
            return [
                ToolCallRequest(
                    id=f"smoke-route-{self.turn}",
                    name="get_route_context",
                    arguments={
                        "route_id": context["route"]["value"]["route_id"],
                        "current_position": {
                            "lat": context["position"]["value"]["lat"],
                            "lon": context["position"]["value"]["lon"],
                            "alt_m": context["position"]["value"].get("alt_m"),
                        },
                        "timestamp": context["position"]["observed_at"],
                    },
                )
            ]

        if "get_weather_for_route_time" not in results:
            position = context["position"]["value"]
            return [
                ToolCallRequest(
                    id=f"smoke-weather-{self.turn}",
                    name="get_weather_for_route_time",
                    arguments={
                        "lat": position["lat"],
                        "lon": position["lon"],
                        "target_time": context["time_state"]["analysis_time"],
                    },
                )
            ]

        if "get_user_device_context" not in results:
            return [
                ToolCallRequest(
                    id=f"smoke-device-{self.turn}",
                    name="get_user_device_context",
                    arguments={"device_id": self._device_id},
                )
            ]

        pending_projections = [
            minutes
            for minutes in HORIZON_MINUTES
            if not _horizon_done(results, "project_route_state", minutes)
        ]
        if pending_projections:
            return [
                ToolCallRequest(
                    id=f"smoke-project-{minutes}-{self.turn}",
                    name="project_route_state",
                    arguments=self._projection_arguments(context, results, minutes),
                )
                for minutes in pending_projections[:2]
            ]

        pending_assessments = [
            minutes
            for minutes in HORIZON_MINUTES
            if not _horizon_done(results, "assess_risk", minutes)
        ]
        if pending_assessments:
            return [
                ToolCallRequest(
                    id=f"smoke-assess-{minutes}-{self.turn}",
                    name="assess_risk",
                    arguments=self._assessment_arguments(context, results, minutes),
                )
                for minutes in pending_assessments[:2]
            ]

        return []

    def _projection_arguments(
        self,
        context: dict[str, Any],
        results: dict[str, list[dict[str, Any]]],
        minutes: int,
    ) -> dict[str, Any]:
        """Arguments for one ``project_route_state`` call, built from real output.

        The route context is the one ``get_route_context`` returned; the movement
        trend is the three speed fields of the device feed's observed window. The
        feed's window carries a fourth field (``pace_drop_percent``) that
        ``MovementTrend`` does not accept, so only the three the projection model
        weights are forwarded -- dropping a field the contract refuses is not
        "editing the tool output", it is matching the contract that is being
        called.
        """
        device = results["get_user_device_context"][0]
        movement = device["movement"]
        return {
            "route_context": results["get_route_context"][0],
            "movement": {
                "current_speed_kmh": movement["current_speed_kmh"],
                "avg_speed_10m": movement["avg_speed_10m"],
                "avg_speed_30m": movement["avg_speed_30m"],
            },
            "horizon_min": minutes,
            "analysis_time": context["time_state"]["analysis_time"],
        }

    def _assessment_arguments(
        self,
        context: dict[str, Any],
        results: dict[str, list[dict[str, Any]]],
        minutes: int,
    ) -> dict[str, Any]:
        """Arguments for one ``assess_risk`` call, built from real output.

        The route state, the weather and the device state are the payloads the
        three gathering tools returned; the time state is the context snapshot's
        own (no tool produces one); and ``projected_state`` is the projection for
        this horizon when one exists, which is what lets the core report the
        projection-derived margins of spec 8. For the NOW horizon there is no
        projection, and the field is left out rather than filled with a guess.
        """
        arguments: dict[str, Any] = {
            "route_state": results["get_route_context"][0],
            "weather_state": results["get_weather_for_route_time"][0],
            "user_device_state": results["get_user_device_context"][0],
            "time_state": context["time_state"],
        }
        projection = _projection_for(results, minutes)
        if projection is not None:
            arguments["projected_state"] = projection
        return arguments

    def _final_answer(self, context: dict[str, Any], results: dict[str, list[dict[str, Any]]]) -> str:
        """Assemble the answer out of the values the tools returned.

        Every number quoted here was read out of an ``assess_risk`` payload. If a
        horizon produced no payload, the answer says so instead of smoothing over
        it -- a stub that answered over a gap would be rehearsing exactly the
        behaviour the benchmark is there to catch.

        The answer also recites the context snapshot's *own* declarations about
        itself (``data_quality.stale_fields`` / ``missing_fields``) and states
        plainly where the weather age fell against the policy's budget. This is
        not the stub implementing the skill's missing-data protocol -- it is a
        stub refusing to print a safety number next to none of the caveats its
        own inputs carried. Reporting "time margin 74 minutes" for a scenario
        whose every input is stale, with no mention of staleness, would be the
        forbidden behaviour reproduced in the smoke output of the project whose
        job is to catch it.
        """
        assessments = results.get("assess_risk", [])
        weather = results.get("get_weather_for_route_time", [{}])[0]
        lines = [
            "Scripted-smoke assessment (offline stub model; no real model was called):",
            f"weather source {weather.get('source')}, age {weather.get('freshness_minutes')} min, "
            f"generated {weather.get('forecast_generated_at')}",
        ]

        lines.extend(_input_quality_lines(context, weather))

        if not assessments:
            lines.append("")
            lines.append(
                "insufficient_data: the scripted policy reached its final turn without a single "
                "assess_risk result, so there is no deterministic verdict to report."
            )
            return "\n".join(lines)

        lines.append("")
        for payload in assessments:
            margins = payload["safety_margins"]
            lines.append(
                f"{payload['horizon']}: {payload['risk_level']} "
                f"(confidence {payload['confidence']}); rules={payload['hard_rules_triggered'] or 'none'}; "
                f"drivers={payload['drivers'] or 'none'}; "
                f"time margin {margins['time_margin_min']} min; "
                f"weather freshness {margins['weather_freshness_min']} min; "
                f"projection confidence {margins['projection_confidence']}"
            )

        horizons_seen = [payload["horizon"] for payload in assessments]
        missing = [member.value for member in Horizon if member.value not in horizons_seen]
        if missing:
            lines.append("")
            lines.append(f"insufficient_data at: {', '.join(missing)} (no assess_risk result was returned).")
        return "\n".join(lines)


# --------------------------------------------------------------------------- #
# Transcript reading (pure helpers -- no policy in them)
# --------------------------------------------------------------------------- #


def _input_quality_lines(context: dict[str, Any], weather: dict[str, Any]) -> list[str]:
    """Recite the staleness and missing-data the context snapshot declares.

    Facts only, taken verbatim from the snapshot the harness supplied, plus the
    one comparison that cannot be read off a single field: the weather's own age
    against the policy budget that age was measured against. The stub states the
    outcome and stops there -- what a *model* should do about stale input (lower
    confidence, report ``insufficient_data``, list every gap) is the skill's
    protocol and this stub deliberately does not implement it.
    """
    quality = context.get("data_quality") or {}
    stale = list(quality.get("stale_fields") or [])
    missing = list(quality.get("missing_fields") or [])

    lines: list[str] = []
    lines.append(f"inputs the snapshot declares stale : {stale if stale else 'none'}")
    lines.append(f"inputs the snapshot declares missing: {missing if missing else 'none'}")

    age = weather.get("freshness_minutes")
    budget = (context.get("weather_policy") or {}).get("max_freshness_min")
    if age is not None and budget is not None:
        inside = float(age) <= float(budget)
        lines.append(
            f"weather age {age} min against a {budget} min budget: "
            f"{'within' if inside else 'OUTSIDE'} the freshness budget"
        )
    return lines


def _user_task_text(messages: list[ChatMessage]) -> str:
    """The task text the harness put in the user message."""
    for message in messages:
        if message.role == "user":
            return message.content
    return ""


def _context_snapshot(messages: list[ChatMessage]) -> dict[str, Any]:
    """Parse the context snapshot JSON the harness embedded in the user message.

    The snapshot is the model's *input*, not its output, so reading it is not the
    same as inventing values: anything the stub quotes from it is something the
    harness told it.
    """
    text = _user_task_text(messages)
    marker = "Current context snapshot (spec section 9):\n"
    if marker not in text:
        raise ValueError("the harness did not embed a context snapshot in the user message")
    return json.loads(text.split(marker, 1)[1])


def _tool_results(messages: list[ChatMessage]) -> dict[str, list[dict[str, Any]]]:
    """Every successful tool result, grouped by tool name in call order.

    A failed tool result is skipped rather than parsed: its content is the
    contract's own error text, there is no payload to build the next call from,
    and pretending otherwise would feed a failure forward as if it were data.
    """
    results: dict[str, list[dict[str, Any]]] = {}
    for message in messages:
        if message.role != "tool" or not message.name:
            continue
        if _RESULT_SEPARATOR not in message.content:
            continue
        payload_text = message.content.split(_RESULT_SEPARATOR, 1)[1]
        try:
            payload = json.loads(payload_text)
        except (TypeError, ValueError):
            continue
        if isinstance(payload, dict):
            results.setdefault(message.name, []).append(payload)
    return results


def _projection_for(results: dict[str, list[dict[str, Any]]], minutes: int) -> dict[str, Any] | None:
    """The ``project_route_state`` payload for ``minutes``, or ``None``."""
    for payload in results.get("project_route_state", []):
        if payload.get("horizon_min") == minutes:
            return payload
    return None


def _horizon_done(results: dict[str, list[dict[str, Any]]], tool_name: str, minutes: int) -> bool:
    """Whether ``tool_name`` has already produced a result for ``minutes``.

    Keyed off the payload the tool itself returned -- ``horizon_min`` on a
    projection, ``horizon`` on a risk result -- rather than off a message counter,
    so a payload that arrives with a different horizon than the one that was
    asked for is not mislabelled as a different horizon's answer.
    """
    for payload in results.get(tool_name, []):
        if tool_name == "project_route_state" and payload.get("horizon_min") == minutes:
            return True
        if tool_name == "assess_risk" and payload.get("horizon") == _horizon_label(minutes):
            return True
    return False


def _horizon_label(minutes: int) -> str:
    """The frozen horizon label for a minute offset (spec 6.1)."""
    for member in Horizon:
        if member.minutes == minutes:
            return member.value
    raise ValueError(f"{minutes} is not a frozen horizon offset (spec 6.1)")


# --------------------------------------------------------------------------- #
# The smoke run
# --------------------------------------------------------------------------- #


def run_smoke(
    *,
    scenario_path: Path | str = DEFAULT_SCENARIO,
    device_id: str = DEFAULT_DEVICE_ID,
    max_steps: int = 12,
) -> tuple[list[Check], dict[str, AgentRunResult]]:
    """Run both conditions offline and check what the two runs recorded.

    Returns the checks and both runs, so a caller can inspect the traces itself
    rather than trusting a printed verdict.
    """
    scenario = load_scenario(scenario_path)
    provider = ScriptedProvider(device_id=device_id)
    registry = scenario_registry(scenario)
    results: dict[str, AgentRunResult] = {}
    for condition in ("without_skill", "with_skill"):
        results[condition] = run_agent(
            task=scenario.task_prompt,
            context=scenario.initial_context,
            model_provider=provider,
            skill_enabled=condition == "with_skill",
            tools=registry,
            max_steps=max_steps,
            temperature=0.0,
            max_tokens=2048,
            scenario_id=scenario.scenario_id,
        )

    checks = _checks(scenario, results, registry)
    return checks, results


def _checks(scenario: Any, results: dict[str, AgentRunResult], registry: dict[Any, Any]) -> list[Check]:
    """Every assertion the offline smoke makes about the two runs."""
    checks: list[Check] = []
    baseline, treatment = results["without_skill"], results["with_skill"]

    checks.append(
        Check(
            "the scenario registry holds exactly the frozen five atomic tools",
            list(registry) == list(ToolName),
            f"registry={[name.value for name in registry]}",
        )
    )
    checks.append(
        Check(
            "binding the scenario's weather policy left the canonical registry untouched",
            TOOL_FUNCTIONS[ToolName.GET_WEATHER_FOR_ROUTE_TIME].__name__ == "get_weather_for_route_time"
            and not hasattr(TOOL_FUNCTIONS[ToolName.GET_WEATHER_FOR_ROUTE_TIME], "func"),
            "TOOL_FUNCTIONS still holds the plain function, so one scenario's replay mode "
            "cannot leak into the run that follows it",
        )
    )

    checks.append(
        Check(
            "both conditions completed without exhausting the step budget",
            baseline.stop_reason == "completed" and treatment.stop_reason == "completed",
            f"without_skill={baseline.stop_reason} in {baseline.steps} steps; "
            f"with_skill={treatment.stop_reason} in {treatment.steps} steps",
        )
    )

    for label, run in results.items():
        trace = run.trace
        horizon_values = [member.value for member in Horizon]
        recorded = [check.horizon.value for check in trace.horizon_checks]
        checks.append(
            Check(
                f"{label}: every frozen horizon was assessed and recorded",
                len(recorded) == len(HORIZON_MINUTES) and set(recorded) == set(horizon_values),
                f"horizon_checks={recorded}",
            )
        )
        checks.append(
            Check(
                f"{label}: the record came from the deterministic core, not from prose",
                trace.core_risk_level is not None and trace.core_confidence is not None,
                f"core_risk_level={trace.core_risk_level!r}, core_confidence={trace.core_confidence!r}",
            )
        )
        checks.append(
            Check(
                f"{label}: every tool call succeeded against its own contract",
                bool(trace.tool_calls)
                and all(record.status == "ok" for record in trace.tool_calls),
                "; ".join(f"{record.tool.value}={record.status}" for record in trace.tool_calls),
            )
        )
        checks.append(
            Check(
                f"{label}: the weather source and its age are recorded as trace content",
                trace.weather_source is not None and trace.weather_source != "",
                f"weather_source={trace.weather_source!r}",
            )
        )
        checks.append(
            Check(
                f"{label}: the trace survives a JSON round trip",
                _round_trips(trace),
                "EvidenceTrace -> JSON -> EvidenceTrace preserved every field",
            )
        )
        reports_core, report_detail = _answer_reports_the_core(run)
        checks.append(
            Check(
                f"{label}: the answer reports what the tools found",
                reports_core,
                report_detail,
            )
        )

    checks.append(
        Check(
            "the trace's declared sampling parameters are identical in both conditions",
            baseline.trace.sampling_params == treatment.trace.sampling_params
            and baseline.trace.model_name == treatment.trace.model_name
            and baseline.trace.provider == treatment.trace.provider,
            f"sampling={baseline.trace.sampling_params}; "
            f"model={baseline.trace.model_name}; provider={baseline.trace.provider}",
        )
    )

    baseline_sequence = [record.tool.value for record in baseline.trace.tool_calls]
    treatment_sequence = [record.tool.value for record in treatment.trace.tool_calls]
    checks.append(
        Check(
            "the two conditions produced the same tool sequence",
            baseline_sequence == treatment_sequence,
            f"{len(baseline_sequence)} calls in both: {baseline_sequence}",
        )
    )
    checks.append(
        Check(
            "the scripted stub does not read the skill text",
            baseline_sequence == treatment_sequence,
            "the stub's policy reads only the task, the context snapshot and the tool "
            "results, so identical sequences are the evidence that the system prompt "
            "swung nothing",
        )
    )

    checks.append(
        Check(
            "only the with-skill run records a skill identity",
            baseline.trace.skill_loaded is False
            and baseline.trace.skill_sha256 is None
            and treatment.trace.skill_loaded is True
            and treatment.trace.skill_sha256 is not None
            and treatment.trace.skill_version is not None,
            f"without_skill: loaded={baseline.trace.skill_loaded}, "
            f"version={baseline.trace.skill_version!r}, sha256={baseline.trace.skill_sha256!r}; "
            f"with_skill: version={treatment.trace.skill_version!r}, "
            f"sha256={(treatment.trace.skill_sha256 or '')[:16]}...",
        )
    )

    checks.append(
        Check(
            "the run's scenario id matches the scenario that was replayed",
            baseline.trace.scenario_id == scenario.scenario_id
            and treatment.trace.scenario_id == scenario.scenario_id,
            f"scenario_id={scenario.scenario_id!r}",
        )
    )

    return checks


def _round_trips(trace: EvidenceTrace) -> bool:
    """Whether the trace survives JSON and comes back equal to itself."""
    try:
        restored = EvidenceTrace.model_validate_json(trace.model_dump_json())
    except Exception:  # noqa: BLE001 - the smoke reports the failure, it does not diagnose it
        return False
    return restored == trace


def _answer_reports_the_core(run: AgentRunResult) -> tuple[bool, str]:
    """Whether the answer names every assessed horizon and the core's verdict.

    Direction matters. It checks that the answer *covers* what the tools found --
    not that the answer mentions nothing else, because a run that cannot assess a
    horizon and says so is doing exactly the right thing, and a check that failed
    on an honest "insufficient_data" would punish the disclosure spec 10 asks for.

    This is a smoke check about the *stub*, not about a model. It exists so a
    future edit that dropped a horizon from the scripted answer, or quietly
    rewrote the level the core returned, fails here instead of passing a smoke
    test built on a fabricated number.
    """
    assessed = [check.horizon.value for check in run.trace.horizon_checks]
    if not assessed:
        return False, "no horizon was recorded, so there is nothing for the answer to report"
    named = [value for value in assessed if value in run.final_response]
    if len(named) != len(assessed):
        missing = sorted(set(assessed) - set(named))
        return False, f"answer does not report {missing}"
    if run.trace.core_risk_level is None:
        return False, "the core produced no verdict to report"
    worst = run.trace.core_risk_level.value
    if worst not in run.final_response:
        return False, f"the core's worst verdict {worst!r} is absent from the answer"
    return True, f"all {len(assessed)} horizons reported; worst core verdict {worst!r} is quoted"


# --------------------------------------------------------------------------- #
# Console entry point
# --------------------------------------------------------------------------- #


def main(argv: Iterable[str] | None = None) -> int:
    """Print the offline smoke report and return a process exit status."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0] if __doc__ else None)
    parser.add_argument("--scenario", default=str(DEFAULT_SCENARIO), help="Scenario YAML to replay.")
    parser.add_argument("--device-id", default=DEFAULT_DEVICE_ID, help="Replay device feed to query.")
    parser.add_argument("--max-steps", type=int, default=12, help="Model-turn budget for each run.")
    arguments = parser.parse_args(list(argv) if argv is not None else None)

    print("TrailGuard Phase 3 agent runtime smoke -- OFFLINE")
    print(f"scenario   : {arguments.scenario}")
    print(f"device feed: {arguments.device_id}")
    print("model      : ScriptedProvider (offline stub) -- NO REAL MODEL WAS CALLED")
    print()

    checks, results = run_smoke(
        scenario_path=arguments.scenario,
        device_id=arguments.device_id,
        max_steps=arguments.max_steps,
    )

    for check in checks:
        print(check.render())

    for label, run in results.items():
        print()
        print(f"--- final answer ({label}) ---")
        print(run.final_response or "(empty: the run produced no answer)")

    failures = [check for check in checks if not check.passed]
    print()
    print(f"{len(checks) - len(failures)}/{len(checks)} checks passed")
    if failures:
        print(f"FAIL: {len(failures)} check(s) did not hold; see above.")
        return 1
    print("PASS: the offline runtime works end to end. This says nothing about model behaviour --")
    print("      no real model was called, and no skill-performance claim can be made from it.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
