"""TrailGuard's own lightweight agent harness (spec sections 15, 3.1, 4.2).

Claude Code + Step 5 Preview are the *development* agents, not the TrailGuard
runtime (spec 15). This module is the runtime: it loads (or does not load)
``SKILL.md``, registers the atomic tools, executes the model's tool calls,
feeds the results back, produces the final answer and records a structured
:class:`~trailguard.schemas.trace.EvidenceTrace`. The benchmark and the demo use
this one harness.

FAIRNESS (spec 3.1) -- this is the contract the whole evaluation rests on
========================================================================
The tools dict, the tool schemas, ``temperature``, ``max_tokens``, ``max_steps``,
the ``context`` snapshot, the ``task`` text AND :data:`HARNESS_BASE_SYSTEM_PROMPT`
are IDENTICAL in both skill conditions; they are plain arguments of
:func:`run_agent` and are never derived from ``skill_enabled``. The ONLY
difference between the baseline and the treatment condition is whether the
``SKILL.md`` text is appended to the system prompt.

That base prompt is therefore neutral runtime mechanics -- how the loop feeds
tool results back, that a tool's output is the only source of values, that an
unimplemented tool is reported as such -- and carries nothing of the TrailGuard
*safety protocol*: no product identity, no horizon vocabulary, no freshness
budget, no uncertainty-disclosure rule and no projected-risk-state wording.
Those are the treatment, they live in SKILL.md, and writing any of them
here would hand the baseline half the treatment and collapse the single-variable
comparison the benchmark exists to make. ``tests/test_harness.py`` asserts the
base prompt stays free of that vocabulary.

One deliberate exception, stated plainly because a fairness claim this central
must not overstate: the base prompt DOES carry one generic, domain-neutral tool
etiquette -- "never invent, extrapolate or 'fill in' a value that was not
returned by a tool". That is runtime mechanics any tool-using harness needs (it
is what makes a tool result worth feeding back), NOT the TrailGuard missing-data
*protocol*: the protocol's substance -- list every missing field, mark stale
inputs, lower confidence, answer ``insufficient_data`` where a horizon cannot be
assessed -- is entirely SKILL.md's and appears nowhere here. The sentence is
shared with the baseline on purpose: removing it would weaken the baseline to
flatter the treatment, which spec 3.2 forbids just as firmly as contamination.

What the harness records (Phase 3)
==================================
When a tool returns a typed contract, the harness copies the *typed* parts of
it into the evidence trace -- and only the typed parts. A successful
``assess_risk`` result carries its own ``horizon``, ``hard_rules_triggered``,
``drivers``, ``confidence``, ``safety_margins.weather_freshness_min`` and
``risk_level``; those are copied into ``horizon_checks``, ``triggered_rules``,
``risk_drivers``, ``core_risk_level`` and ``core_confidence``. The freshness
verdict for a horizon is the one comparison the harness does make -- the age the
core actually used against the snapshot's own ``WeatherPolicy`` -- because
"stale" is meaningless without the budget it was measured against, and the
budget is part of the context the tool already consumed.

What the harness still refuses to do is infer trace content from the model's
*prose*. ``final_risk_level``, ``confidence`` and ``uncertainty_disclosed``
describe what the agent *claimed*, and reading them out of a paragraph is a
semantic judgement that belongs to the eval layer (Phase 5): the harness has no
opinion about English, and a run whose answer happens to contain the word
"RED" must not get a RED trace entry. The two halves stay separate on purpose,
so a benchmark can measure the gap between what the tools produced
(``core_risk_level``) and what the agent said (``final_risk_level``).

Boundaries
==========
Not responsible for (and deliberately NOT implemented here):
* the 30/60/120 horizon loop, missing-data protocol or safety overrides --
  those are the SKILL's job (spec 10); this harness only supplies the loop of
  "model turn -> tool results -> next model turn", and it records the horizons
  the *model* chose to assess rather than iterating them itself;
* repairing a run: it never re-orders tool calls, retries a failed call, calls
  a tool on the model's behalf, or writes the model's final answer for it;
* any safety logic, weather fetching or scoring (``trailguard.risk_core``,
  ``trailguard.tools``).
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Literal

from pydantic import BaseModel, ValidationError

from ..providers.base import ChatMessage, ModelProvider, ModelResponse, ProviderError
from ..schemas.context import TrailGuardContext, WeatherPolicy
from ..schemas.risk import RiskResult
from ..schemas.scenario import ToolName
from ..schemas.trace import EvidenceTrace, HorizonCheckRecord
from .skill_loader import load_skill_md, skill_identity
from .trace import TraceCollector

__all__ = ["AgentRunResult", "HARNESS_BASE_SYSTEM_PROMPT", "StopReason", "ToolOutcome", "run_agent"]

#: Instruction block that is byte-identical in both skill conditions. It is
#: deliberately *neutral*: it describes the runtime mechanics of the harness and
#: nothing else. Any safety-domain instruction that appeared here (which data
#: counts as stale, what must be disclosed, how a future state is expressed)
#: would be part of the treatment, and the treatment lives only in SKILL.md.
HARNESS_BASE_SYSTEM_PROMPT: str = """You are a general-purpose assistant running \
inside an agent runtime that hands you one structured context snapshot and a set \
of atomic tools you may call.

How the runtime works:
1. Each tool call you request is executed outside the model; its result is \
appended to the conversation as a tool message keyed by call id, and your next \
turn can read it.
2. Use the tools to obtain the information you need. Never invent, extrapolate or \
"fill in" a value that was not returned by a tool.
3. When a tool reports that it is not implemented in the current phase, report \
that plainly; do not fabricate its result.
4. If a tool you want was not offered to you, do not assume it exists; answer \
from the tools you do have.
5. Keep any credential-shaped value out of your visible output.

Answer the user's question using the evidence you actually gathered, and cite \
the tool outputs you relied on."""

#: Stop reasons of :func:`run_agent` (frozen vocabulary for the eval layer).
#:
#: Five values, because they answer five different questions, and collapsing any
#: two of them would hide a real failure mode behind a different one:
#:
#: ``completed``
#:     The model answered without requesting further tool calls.
#: ``max_steps``
#:     The turn budget ran out while the model was still calling tools.
#: ``provider_error``
#:     The model never answered: a transport failure (see
#:     :class:`~trailguard.providers.base.ProviderError`) or any other
#:     exception raised inside ``model_provider.generate``. ``final_response``
#:     is empty, because no answer exists.
#: ``tool_error``
#:     A tool the model called failed, and the run was configured to stop on
#:     the first failure (``tool_failure_policy="stop"``). The model asked for
#:     something reasonable; the tool could not deliver it.
#: ``invalid_tool_call``
#:     The model asked for a tool that was not offered, or supplied arguments
#:     that failed the tool's own input contract, and the run was configured to
#:     stop on the first such call. This is the model's failure, not the
#:     tool's, and a benchmark must be able to tell the two apart: "the agent
#:     hallucinated a tool" and "the weather endpoint is down" are not the same
#:     finding.
#:
#: The default ``tool_failure_policy="report"`` never produces the last two: a
#: failed call is recorded and fed back to the model, which is free to correct
#: itself, and only a completed run, an exhausted budget or a provider failure
#: can end a loop. That default is the pre-Phase-3 behaviour, preserved so a
#: later phase cannot silently change what a stop reason means.
StopReason = Literal["completed", "max_steps", "provider_error", "tool_error", "invalid_tool_call"]

#: How a failed tool call is handled: ``"report"`` (record it, hand the failure
#: back to the model, keep looping) or ``"stop"`` (end the run immediately with
#: ``tool_error`` / ``invalid_tool_call``). Identical in both skill conditions,
#: and identical for every run of a given experiment: it is a harness policy,
#: never something derived from ``skill_enabled``.
ToolFailurePolicy = Literal["report", "stop"]

#: What actually happened to one tool call. ``ok`` means the tool ran and
#: returned; the other four are the model's fault (``invalid_tool_call``,
#: ``unknown_tool``) or the tool's (``tool_error``, ``not_implemented``).
ToolOutcome = Literal["ok", "invalid_tool_call", "unknown_tool", "tool_error", "not_implemented"]

#: Size cap for *unstructured* tool results only (a raw string or a mapping the
#: caller handed back). A pydantic contract payload is never truncated: it is a
#: bounded, typed document, and silently cutting it would show the model a
#: truncated route context or weather snapshot as if it were the whole result.
_MAX_SUMMARY_CHARS = 600


def _now() -> datetime:
    """Current UTC instant."""
    return datetime.now(timezone.utc)


def _provider_label(model_provider: ModelProvider) -> tuple[str, str]:
    """Return ``(provider, model_name)`` labels for the evidence trace.

    The two labels answer two different questions and must not be conflated:
    ``provider`` is the *transport* (``ModelProvider.name``: ``"stepfun"``,
    ``"local"``) and ``model_name`` is the concrete model string the provider
    was constructed with (``ModelProvider.model``). Recording the provider id as
    the model name -- or the Python class name as the provider -- would make
    ``evals.compare._check_same_model`` compare transport ids as if they were
    model ids, so the frozen ``2 models x 2 conditions`` matrix (spec 14.2)
    could swap one StepFun model for another and still pass its own fairness
    audit. When a provider reports no model string the gap is recorded as a gap
    rather than papered over with the provider id.
    """
    try:
        provider = str(model_provider.name)
    except Exception:  # pragma: no cover - defensive: providers always expose name
        provider = type(model_provider).__name__
    model = getattr(model_provider, "model", None)
    model_name = str(model) if model else f"{provider}:model-unset"
    return provider, model_name


def _resolve_registry(
    tools: dict[ToolName, Callable[..., Any]] | None,
) -> tuple[dict[ToolName, Callable[..., Any]], list[dict[str, Any]]]:
    """Return the executable tools and the tool schema dicts to advertise.

    Responsibility: turn the caller's ``tools`` argument into an executable
    registry and the matching JSON-schema-shaped specs. Not responsible for:
    deciding which tools are *required* for a task (that is the skill protocol,
    spec 10.1) -- every registered tool is always advertised, in both skill
    conditions, exactly as spec 3.1 requires.

    When ``tools`` is ``None`` the canonical registry
    (``trailguard.tools.TOOL_FUNCTIONS``) is used, so the harness matches the
    demo. If that registry cannot be imported the harness still runs with an
    empty registry and advertises no tools at all: the model then fails loudly
    instead of silently succeeding against a phantom tool.

    Keys that are not part of the Phase 0 ``ToolName`` registry are rejected
    loudly rather than dropped: a silently discarded key (a misspelled
    ``"get_weather"``, a planned ``"find_safe_points"``) would produce a run that
    quietly never offers that tool, and the two skill conditions could then end
    up with *different* tool sets -- which surfaces later as a confusing
    ``evals.compare`` fairness failure instead of the real configuration error.
    An evidence trace can only record registered tools, and advertising an
    unregistered one would break the atomic-tool contract (spec 4.1).
    """
    registry: dict[ToolName, Callable[..., Any]] = {}
    if tools is None:
        try:
            from ..tools import TOOL_FUNCTIONS
        except ImportError:  # pragma: no cover - registry written by another layer
            registry = {}
        else:
            registry = dict(TOOL_FUNCTIONS)
    else:
        for key, func in tools.items():
            try:
                tool = key if isinstance(key, ToolName) else ToolName(str(key))
            except ValueError:
                raise ValueError(
                    f"unknown tool {key!r} passed to run_agent(tools=...): the Phase 0 registry "
                    f"holds exactly {[t.value for t in ToolName]}. A misspelled or unregistered "
                    "tool is a configuration error; it is never silently dropped, because a "
                    "condition that silently lacks a tool cannot be compared fairly (spec 3.1)."
                ) from None
            registry[tool] = func

    specs: list[dict[str, Any]] = []
    try:
        from ..tools import tool_schema_dicts
    except ImportError:  # pragma: no cover - registry written by another layer
        return registry, specs

    available = {tool.value for tool in registry}
    specs = [spec for spec in tool_schema_dicts() if spec.get("name") in available]
    return registry, specs


def _coerce_arguments(arguments: Any) -> dict[str, Any]:
    """Normalise model-supplied tool arguments into a dict.

    Providers hand the harness whatever the model emitted: a dict, a JSON
    string, or ``None``. Secret-shaped keys are NOT touched here -- redaction
    happens in :class:`~trailguard.schemas.trace.ToolCallRecord`, at the
    contract boundary, so no code path can store a credential unredacted.
    """
    if arguments is None:
        return {}
    if isinstance(arguments, dict):
        return dict(arguments)
    if isinstance(arguments, str):
        try:
            decoded = json.loads(arguments)
        except (TypeError, ValueError):
            return {"_raw": arguments}
        return _coerce_arguments(decoded)
    return {"_raw": repr(arguments)}


def _summarize_result(tool: ToolName, result: Any) -> str:
    """Build the readable, bounded summary appended as the tool result message.

    Responsibility: give the model the substance of the result (not just "ok").
    Not responsible for: judging, filtering or re-interpreting the result -- no
    safety reasoning happens here, and nothing may be dropped from it either.

    Trimming policy: a pydantic contract payload
    (:class:`~trailguard.schemas.context.RouteContext`,
    :class:`~trailguard.schemas.weather.WeatherForRouteTime`,
    :class:`~trailguard.schemas.route.RouteProjection`,
    :class:`~trailguard.schemas.risk.RiskResult`, ...) is serialized whole. Its
    size is bounded by its own schema, and cutting it at an arbitrary character
    count would hand the model a *truncated safety document* -- a route context
    whose remaining fields are simply gone, with nothing in the text saying so.
    The cap applies to untyped payloads only (a raw string, an arbitrary mapping),
    where an unbounded blob really can blow up the next request.
    """
    if result is None:
        return f"{tool.value}: no value returned"
    if isinstance(result, BaseModel):
        # A contract payload: typed, bounded, and safe to hand over in full.
        return f"{tool.value} -> {result.model_dump_json()}"
    if isinstance(result, str):
        payload = result
    else:
        payload = json.dumps(result, default=str)
    summary = f"{tool.value} -> {payload}"
    if len(summary) > _MAX_SUMMARY_CHARS:
        summary = summary[:_MAX_SUMMARY_CHARS] + " ...[truncated]"
    return summary


@dataclass
class AgentRunResult:
    """Outcome of one :func:`run_agent` call.

    Responsibility: return the final answer together with the evidence trace and
    why the loop stopped, so the eval layer never has to re-read a log.
    Not responsible for: scoring the run (the repo-root ``evals`` package, Phase 5).

    ``final_response`` is the empty string exactly when the run did not produce
    one -- ``max_steps``, ``provider_error``, or a policy-driven early stop. The
    harness never writes an answer on the model's behalf, so an empty field means
    "there is no answer", not "the answer was empty" (spec 3.1, 10.5).
    """

    run_id: str
    final_response: str
    trace: EvidenceTrace
    stop_reason: StopReason
    steps: int = 0


def run_agent(
    *,
    task: str,
    context: TrailGuardContext,
    model_provider: ModelProvider,
    skill_enabled: bool = True,
    skill_path: Path | None = None,
    tools: dict[ToolName, Callable[..., Any]] | None = None,
    max_steps: int = 12,
    temperature: float = 0.0,
    max_tokens: int = 2048,
    scenario_id: str | None = None,
    started_at: datetime | None = None,
    tool_failure_policy: ToolFailurePolicy = "report",
) -> AgentRunResult:
    """Run one agent episode and return its answer plus its evidence trace.

    The loop is: build the system prompt (harness base instructions, plus the
    ``SKILL.md`` text when ``skill_enabled``), build the user message (task text
    plus the JSON of the context snapshot), then alternate
    ``model_provider.generate`` -> execute the requested tool calls -> append the
    tool results -> repeat, until the model answers without tool calls or
    ``max_steps`` is reached.

    Args:
        task: Task text. Identical in both skill conditions.
        context: Shared context snapshot (spec 9). Identical in both conditions.
        model_provider: Transport backend (spec 16); identical in both conditions.
        skill_enabled: When True the SKILL.md text is appended to the system
            prompt. This is the ONLY knob that differs between the baseline and
            the treatment condition (spec 3.1).
        skill_path: Explicit skill file; ``None`` means ``<repo root>/SKILL.md``.
            Loaded lazily, at run time, never at import time.
        tools: Executable tool registry. ``None`` uses the canonical
            ``trailguard.tools.TOOL_FUNCTIONS`` registry.
        max_steps: Hard cap on model turns (identical in both conditions).
        temperature: Sampling temperature (identical in both conditions).
        max_tokens: Output cap (identical in both conditions).
        scenario_id: Scenario id written into the evidence trace.
        started_at: Run start instant; defaults to the current UTC instant.
        tool_failure_policy: ``"report"`` (default) feeds a failed tool call back
            to the model and keeps looping; ``"stop"`` ends the run with
            ``tool_error`` or ``invalid_tool_call``. A harness policy, so it must
            be the same for every run of an experiment -- never derived from
            ``skill_enabled``.

    Returns:
        An :class:`AgentRunResult` with the final answer, the frozen evidence
        trace and the stop reason.

    Raises:
        FileNotFoundError: ``skill_enabled`` is True but the skill file does not
            exist. A with-skill run must never silently degrade into a baseline
            run, because that would quietly destroy the single-variable
            comparison of spec 3.1.
        ValueError: ``skill_enabled`` is True and the skill file is empty.
    """
    run_id = uuid.uuid4().hex
    started = started_at or _now()
    registry, tool_specs = _resolve_registry(tools)

    if skill_enabled:
        skill_text = load_skill_md(skill_path)
        system_prompt = f"{HARNESS_BASE_SYSTEM_PROMPT}\n\n{skill_text}"
        skill_loaded = True
        # Identity of the text that was actually injected: the version its own
        # frontmatter declares and a hash of the exact bytes. Recorded as
        # provenance, never read as protocol content (spec 3.1, 18).
        identity = skill_identity(skill_text)
    else:
        # Nothing skill-related in the baseline system prompt: no mention of the
        # skill, no weakened instructions, no different tools (spec 3.1).
        system_prompt = HARNESS_BASE_SYSTEM_PROMPT
        skill_loaded = False
        identity = None

    provider_label, model_label = _provider_label(model_provider)
    collector = TraceCollector.new(
        run_id=run_id,
        scenario_id=scenario_id,
        model_name=model_label,
        provider=provider_label,
        skill_loaded=skill_loaded,
        # Decoding / loop parameters only. The skill condition is deliberately
        # NOT recorded here: it lives in `skill_loaded`, and listing it as a
        # "parameter" would make the two conditions look like they were sampled
        # differently, which spec 3.1 forbids.
        sampling_params={
            "temperature": temperature,
            "max_tokens": max_tokens,
            "max_steps": max_steps,
        },
        started_at=started,
    )
    if identity is not None:
        collector.skill_version = identity.version
        collector.skill_sha256 = identity.sha256

    messages: list[ChatMessage] = [
        ChatMessage(
            role="system",
            content=system_prompt,
        ),
        ChatMessage(
            role="user",
            content=(
                f"{task}\n\n"
                "Current context snapshot (spec section 9):\n"
                f"{context.model_dump_json()}"
            ),
        ),
    ]

    steps = 0
    stop_reason: StopReason = "max_steps"
    final_response = ""

    for step in range(1, max_steps + 1):
        steps = step
        try:
            response: ModelResponse = model_provider.generate(
                messages,
                tools=tool_specs or None,
                temperature=temperature,
                max_tokens=max_tokens,
            )
        except ProviderError:
            # A transport failure is not an agent answer. The run stops with
            # stop_reason 'provider_error' and the trace still records every
            # tool call that happened before it. The exception itself is not
            # re-raised and not logged here: its class names the failure kind,
            # and a message could carry a rejected credential.
            stop_reason = "provider_error"
            break
        except Exception:
            # Anything else a provider raises is treated the same way. Widening
            # this to ``except Exception`` is deliberate -- an unexpected
            # RuntimeError inside a transport must still stop the run honestly
            # rather than escape into whatever is orchestrating the benchmark --
            # but the two clauses stay separate so the intended path is legible.
            stop_reason = "provider_error"
            break

        if response.tool_calls:
            # The assistant turn carries the calls it made, so the ``tool``
            # messages that follow are keyed to a live transcript rather than
            # being orphans: a provider rebuilding an OpenAI-compatible request
            # needs both halves (spec 16).
            messages.append(
                ChatMessage(
                    role="assistant",
                    content=response.content or "",
                    tool_calls=list(response.tool_calls),
                )
            )
            stop_on_failure = False
            for call in response.tool_calls:
                summary, outcome = _execute_tool_call(
                    call,
                    registry,
                    collector,
                    weather_policy=context.weather_policy,
                )
                messages.append(
                    ChatMessage(
                        role="tool",
                        content=summary,
                        tool_call_id=call.id,
                        name=call.name,
                    )
                )
                if tool_failure_policy == "stop" and outcome not in ("ok",):
                    stop_on_failure = True
                    stop_reason = (
                        "invalid_tool_call"
                        if outcome in ("invalid_tool_call", "unknown_tool")
                        else "tool_error"
                    )
                    break
            if stop_on_failure:
                break
            continue

        final_response = response.content or ""
        stop_reason = "completed"
        break

    trace = collector.finish(finished_at=_now())
    return AgentRunResult(
        run_id=run_id,
        final_response=final_response,
        trace=trace,
        stop_reason=stop_reason,
        steps=steps,
    )


def _record_risk_evidence(
    result: Any,
    collector: TraceCollector,
    weather_policy: WeatherPolicy | None,
) -> None:
    """Copy the bonded parts of a real ``assess_risk`` result into the trace.

    Responsibility: fill ``horizon_checks``, ``triggered_rules``, ``risk_drivers``
    and the core's own verdict from a contract the tool actually returned. Not
    responsible for: deciding which horizons *should* have been assessed (that is
    the skill protocol and the Phase 5 metrics), inferring anything from the
    model's answer, or applying any safety logic.

    Every value is copied verbatim. The only comparison made here is the
    freshness one, and it is a comparison the trace cannot be read without:
    ``freshness_ok`` is the age the core reported, measured against the
    snapshot's own ``max_freshness_min`` budget. An age the core could not
    compute leaves ``freshness_ok`` as ``None`` -- "not checkable" -- rather than
    being silently treated as fresh.

    ``missing_data`` stays empty on purpose. Which fields were absent *at this
    horizon* is the skill's missing-data protocol (spec 10.3); the harness has a
    whole-run snapshot and copying its ``missing_fields`` onto one horizon would
    attribute run-level gaps to a moment that did not experience them.
    """
    if not isinstance(result, RiskResult):
        # The registry is the contract boundary; a caller who registered a
        # different return type gets no fabricated risk evidence.
        return

    age = result.safety_margins.weather_freshness_min
    budget = weather_policy.max_freshness_min if weather_policy is not None else None
    freshness_ok: bool | None = None
    if age is not None and budget is not None:
        freshness_ok = age <= budget

    collector.record_horizon(
        HorizonCheckRecord(
            horizon=result.horizon,
            weather_freshness_min=age,
            freshness_ok=freshness_ok,
            missing_data=[],
        )
    )
    collector.record_triggered_rules(list(result.hard_rules_triggered))
    collector.record_risk_drivers(list(result.drivers))
    collector.record_core_verdict(result.horizon, result.risk_level, result.confidence)


def _execute_tool_call(
    call: Any,
    registry: dict[ToolName, Callable[..., Any]],
    collector: TraceCollector,
    *,
    weather_policy: WeatherPolicy | None = None,
) -> tuple[str, ToolOutcome]:
    """Execute one requested tool call; return ``(tool message text, outcome)``.

    Responsibility: run the requested tool with the model's arguments, and record
    exactly what happened -- success, ``NotImplementedError`` (a Phase 0 stub), a
    contract violation, or any other failure -- in the evidence trace. Not
    responsible for: retrying, repairing arguments or substituting another tool; a
    failed call is reported to the model as a failure and the model decides what
    to do next.

    A requested name that is not a registered ``ToolName`` cannot be represented
    in :class:`~trailguard.schemas.trace.ToolCallRecord`; it is reported back to
    the model as an error message and is not counted as a tool call.

    Provenance the harness *can* fill honestly: the ``source`` of a successful
    ``get_weather_for_route_time`` result, and the bonded parts of a successful
    ``assess_risk`` result. Everything else the protocol asks for -- which
    horizons the agent *claimed*, what it *said* was missing, what level it
    finally asserted -- has to come out of the model's own answer, and this
    harness refuses to guess at it (spec 10, 18).
    """
    try:
        tool = ToolName(str(call.name))
    except ValueError:
        # The outcome is recorded so a "stop on failure" policy can act on it,
        # but no ToolCallRecord exists: the contract cannot name a tool that is
        # not in the frozen registry, and inventing one would be worse than
        # omitting it.
        return (
            f"error: unknown tool {call.name!r}; the Phase 0 registry holds only "
            f"{[t.value for t in ToolName]}",
            "unknown_tool",
        )

    arguments = _coerce_arguments(call.arguments)
    func = registry.get(tool)
    if func is None:
        summary = f"{tool.value}: error: not provided to this run"
        collector.record_tool_call(
            tool=tool,
            arguments=arguments,
            result_ref=None,
            result_summary=summary,
            status="error",
            error="tool is registered but was not passed to run_agent(tools=...)",
        )
        return summary, "tool_error"

    # Validate against the tool's own input contract before invoking anything.
    # The JSON schemas the harness advertises (tool_schema_dicts) are the same
    # models, so what the model was shown is what its arguments are checked
    # against; a malformed call is reported as a failed call with the contract's
    # own message rather than reaching a stub that cannot interpret it.
    try:
        from ..tools import get_tool_spec
    except ImportError:  # pragma: no cover - registry written by another layer
        input_model = None
    else:
        input_model = get_tool_spec(tool).input_model
    if input_model is not None:
        try:
            input_model.model_validate(arguments)
        except ValidationError as exc:
            # Field paths and messages only -- never ``exc`` itself. Pydantic's
            # own rendering embeds ``input_value=...``, and the value it would
            # embed here is the raw model-supplied argument: exactly the
            # credential-shaped payload the trace policy exists to keep out.
            problems = "; ".join(
                f"{'.'.join(str(part) for part in error['loc']) or '<root>'}: {error['msg']}"
                for error in exc.errors()
            )
            error = f"invalid arguments for {tool.value}: {problems}"
            collector.record_tool_call(
                tool=tool,
                arguments=arguments,
                result_ref=None,
                result_summary=error,
                status="error",
                error=error,
            )
            return error, "invalid_tool_call"

    try:
        result = func(**arguments)
    except NotImplementedError as exc:
        error = str(exc) or "not implemented in this phase"
        summary = f"{tool.value}: not_implemented ({error})"
        collector.record_tool_call(
            tool=tool,
            arguments=arguments,
            result_ref=None,
            result_summary=summary,
            status="not_implemented",
            error=error,
        )
        return summary, "not_implemented"
    except TypeError as exc:
        # By far the most common cause: the model emitted arguments that do not
        # match the tool signature (missing/unknown keyword).
        error = f"invalid arguments for {tool.value}: {exc}"
        collector.record_tool_call(
            tool=tool,
            arguments=arguments,
            result_ref=None,
            result_summary=error,
            status="error",
            error=error,
        )
        return error, "invalid_tool_call"
    except Exception as exc:
        error = f"{type(exc).__name__}: {exc}"
        summary = f"{tool.value}: error: {error}"
        collector.record_tool_call(
            tool=tool,
            arguments=arguments,
            result_ref=None,
            result_summary=summary,
            status="error",
            error=error,
        )
        return summary, "tool_error"

    summary = _summarize_result(tool, result)
    collector.record_tool_call(
        tool=tool,
        arguments=arguments,
        result_ref=None,
        result_summary=summary,
        status="ok",
        error=None,
    )
    if tool is ToolName.GET_WEATHER_FOR_ROUTE_TIME:
        # Spec 18 requires the weather source as trace content. The tool result
        # is the only place that fact exists -- there is no skill-side
        # bookkeeping to read it from -- so the harness copies the provider id of
        # the observation the model actually received. It copies the value, it
        # never invents or prefers one.
        collector.record_weather_source(getattr(result, "source", None))
    elif tool is ToolName.ASSESS_RISK:
        _record_risk_evidence(result, collector, weather_policy)
    return summary, "ok"
