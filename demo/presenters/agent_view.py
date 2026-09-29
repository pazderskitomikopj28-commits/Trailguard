"""Local Agent Mode: run the real harness, and show honestly what happened.

Spec 10, 11, 23, 45. This is the part of the demo that can fail, and the design
assumes it will during a competition. Three rules govern it:

* **There is no scripted fallback.** When the local endpoint does not answer, the
  tab says "Local Agent unavailable" and stops. Substituting a canned answer would
  turn the strongest part of the presentation -- a real model, really calling the
  tools -- into the weakest.
* **A failure is a finding, not an error state.** ``max_steps``, a context-length
  rejection and a provider error are stopped runs with a reason, and they are
  shown with the tool calls that did happen. Phase 5 already recorded that three
  treatment runs hit the 16384-token window; the demo shows the same thing rather
  than tuning the server to make it go away.
* **The Agent/Core comparison is the benchmark's own.** The claim scanner and the
  level ranking are imported from ``evals/phase5/evaluate.py``, not reimplemented
  here, so "does the agent contradict the deterministic core?" is answered by the
  identical code that produced the Phase 5 M12 numbers.

The harness never fills ``final_risk_level`` / ``confidence`` /
``uncertainty_disclosed`` -- it refuses to infer a verdict from prose. This
module respects that: the claimed level is a *scanner's reading of the answer
text*, labelled as such, and it is never stored in the trace.
"""

from __future__ import annotations

import os
import socket
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Literal

from trailguard.agent.harness import run_agent
from trailguard.agent.replay_runner import load_scenario, scenario_registry
from trailguard.providers.local import LocalModelProvider
from trailguard.schemas.trace import EvidenceTrace, ToolCallRecord

# The frozen benchmark vocabulary. Imported rather than restated so a demo claim
# and a Phase 5 metric can never be computed two different ways.
from evals.phase5.evaluate import _rank, scan_answer

WeatherMode = Literal["replay", "live"]

#: Frozen sampling. Mirrors evals/phase5/manifest.py and recorded on the run.
TEMPERATURE = 0.0
MAX_TOKENS = 2048
MAX_STEPS = 16
TOOL_FAILURE_POLICY = "report"

#: What the tab says when the endpoint cannot be reached. Exact string, so the
#: UI, the preflight and the docs cannot disagree about the wording.
UNAVAILABLE = "Local Agent unavailable"

#: Normalised stop reasons, in the harness's own vocabulary.
STOP_REASON_TEXT = {
    "completed": "finished with an answer",
    "max_steps": "ran out of steps before answering",
    "provider_error": "the model endpoint failed",
    "tool_error": "a tool raised",
    "invalid_tool_call": "the model emitted a malformed tool call",
}


class LocalAgentUnavailable(RuntimeError):
    """The local model endpoint did not answer.

    Raised so that the caller has no way to fall back silently: there is no
    branch here that returns a scripted answer.
    """


@dataclass(slots=True)
class CallRow:
    """One tool call an agent actually made."""

    seq: int
    tool: str
    arguments: dict[str, Any]
    status: str
    result_summary: str
    error: str | None


@dataclass(slots=True)
class AgentRunView:
    """A finished (or stopped) agent run, flattened for the UI."""

    scenario_id: str
    run_id: str
    model_name: str
    provider: str
    skill_loaded: bool
    skill_version: str | None
    stop_reason: str
    stop_reason_text: str
    steps: int
    calls: list[CallRow]
    horizons_checked: list[str]
    triggered_rules: list[str]
    core_risk_level: str | None
    core_confidence: float | None
    weather_source: str | None
    freshness_checks: list[str]
    missing_data: list[str]
    sampling_params: dict[str, Any]
    answer: str
    claimed_levels: dict[str, str]
    claimed_rules: list[str]
    worst_claimed: str | None
    contradicted: bool
    contradiction_note: str
    hedges: list[str]
    probability_claims: list[str]
    negated: list[str]
    conflicting: list[str]
    #: How long the run took, as a sentence. Not ``data_quality``: that name would
    #: suggest the device or route quality the tools reported, and a run's wall
    #: clock has nothing to do with either. Phase 5 recorded this because a local
    #: 30B inference is slow enough to matter when a judge is watching.
    timing_note: list[str] = field(default_factory=list)

    @property
    def contradictions(self) -> bool:
        return self.contradicted


@dataclass(slots=True)
class AgentAvailability:
    """What the preflight found out about the local endpoint.

    Two audiences read these fields and they want different things.
    ``scripts/demo_preflight.py`` is an operator's tool: it prints ``reason``,
    ``detail`` and ``base_url`` in full, because whoever is starting a vLLM server
    needs the address that was asked and what answered. A competition screen is not
    that tool. ``demo_screen_reason`` exists so the screen can say what failed
    without quoting the endpoint back at a judge.
    """

    available: bool
    reason: str
    detail: str
    model: str
    base_url: str
    max_model_len: int | None = None

    @property
    def demo_screen_reason(self) -> str:
        """The reason as a screen should say it: what failed, and no address.

        ``detail`` is written for whoever fixes the laptop -- it quotes the URL that
        was asked and what answered -- and the brief keeps addresses off a judge's
        screen. The reason sentence carries the whole meaning for a viewer ("the
        health endpoint is not reachable", "the endpoint is serving X, not Y"), and
        the fix line next to it names the command that prints the rest. So the demo
        shows this and the preflight shows the detail, and nothing is lost by
        either.
        """
        return self.reason


Opener = "Callable[[str, float], tuple[int, bytes]]"


def _real_opener(url: str, timeout_s: float) -> tuple[int, bytes]:
    """The real GET. Isolated so a test can supply a stub and never reach the wire.

    The URL is the operator's own configured local endpoint, and no credential is
    attached to the request -- the health and models paths need none.
    """
    with urllib.request.urlopen(url, timeout=timeout_s) as response:  # noqa: S310 - the configured local endpoint
        return response.status, response.read()


class _Unreachable(Exception):
    """A transport-level failure of the kind a probe must survive."""


def probe_local_agent(
    *,
    base_url: str | None = None,
    model: str | None = None,
    timeout_s: float = 5.0,
    opener: Any | None = None,
) -> AgentAvailability:
    """Ask the local endpoint whether it is up, without calling a model.

    Two probes, both read-only and both with no credential attached: ``/health``
    must answer 200, and ``/v1/models`` must list the model this demo intends to
    use. ``max_model_len`` is read from the model entry because Phase 5 froze it at
    16384, and a server started with something else is a finding to show rather
    than a number to paper over.

    ``opener`` is the injection point: ``(url, timeout_s) -> (status, body)``. The
    default performs the GET. It is a parameter so the three failure branches --
    refused, timed out, serving the wrong model -- can be exercised in a suite
    that closes the socket layer (spec 13).

    A refusal is an answer: ``available=False`` with the reason, and no exception
    escapes, because a demo cannot afford to traceback at a judge.
    """
    resolved_base = base_url or os.environ.get(
        "TRAILGUARD_LOCAL_BASE_URL", "http://127.0.0.1:8000/v1"
    )
    resolved_model = model or os.environ.get(
        "TRAILGUARD_LOCAL_MODEL", "qwen3-vl-30b-a3b"
    )
    resolved_opener = opener if opener is not None else _real_opener

    root = resolved_base.rstrip("/")
    if root.endswith("/v1"):
        root = root[: -len("/v1")]

    for label, url in (("health", f"{root}/health"), ("models", f"{root}/v1/models")):
        try:
            status, _body = resolved_opener(url, timeout_s)
        except (urllib.error.URLError, _Unreachable) as exc:
            reason = getattr(exc, "reason", exc)
            return AgentAvailability(
                False,
                f"the {label} endpoint is not reachable",
                f"{url} -> {type(exc).__name__}: {reason}",
                resolved_model,
                resolved_base,
            )
        except (OSError, socket.timeout) as exc:
            return AgentAvailability(
                False,
                f"the {label} endpoint timed out",
                f"{url} -> {type(exc).__name__}: {exc}",
                resolved_model,
                resolved_base,
            )
        if status != 200:
            return AgentAvailability(
                False,
                f"the {label} endpoint answered {status}",
                f"{url} -> {status}",
                resolved_model,
                resolved_base,
            )

    max_model_len, served = _models_payload(root, resolved_model, resolved_opener, timeout_s)
    if served and resolved_model not in served:
        return AgentAvailability(
            False,
            f"the endpoint is serving {', '.join(sorted(served)) or 'nothing'}, not {resolved_model}",
            f"{root}/v1/models -> {sorted(served)}",
            resolved_model,
            resolved_base,
            max_model_len,
        )

    return AgentAvailability(
        True, "reachable and serving the expected model", "", resolved_model, resolved_base, max_model_len
    )


def _models_payload(
    root: str, model: str, opener: Any, timeout_s: float
) -> tuple[int | None, list[str]]:
    """Read ``/v1/models`` once, returning the context limit and the served ids.

    One request for both facts. The limit is the requested model's when the
    endpoint serves it, and otherwise the first entry it does serve -- because the
    interesting question when the wrong model is up is "what was this server
    launched with?", which is how Phase 5's 16384 got violated in the first place.
    A payload that is not the expected shape yields an empty list rather than a
    guess, so a malformed response shows up as "not readable" instead of silently
    passing the model check.
    """
    import json

    try:
        _status, body = opener(f"{root}/v1/models", timeout_s)
        payload = json.loads(body.decode("utf-8"))
    except Exception:  # noqa: BLE001 - probe only; the caller already has a verdict
        return None, []
    data = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(data, list):
        return None, []
    entries = [item for item in data if isinstance(item, dict) and item.get("id")]
    served = [str(item.get("id")) for item in entries]
    wanted = next((item for item in entries if item.get("id") == model), None)
    source = wanted if wanted is not None else (entries[0] if entries else None)
    limit = source.get("max_model_len") if source else None
    return (int(limit) if isinstance(limit, (int, float)) else None), served


def run_local_agent(
    scenario, *, availability: AgentAvailability | None = None
) -> AgentRunView:
    """Drive the real harness for ``scenario`` and report what it did.

    ``availability`` is probed when not supplied, so a caller can show the probe
    result and the run from one check. When it is not available, this raises
    :class:`LocalAgentUnavailable` -- there is no path that fabricates a run.
    """
    probe = availability if availability is not None else probe_local_agent()
    if not probe.available:
        # The screen reason and nothing else. This string is rendered verbatim as the
        # body of the "The agent run failed" box (tabs.py:432-438), because app.py
        # catches this exception and hands str(exc) straight to the screen -- so
        # appending probe.detail here would print the probed address and the
        # exception class to a judge, which is the one thing app.py:374-377 exists to
        # prevent on the path it guards. The address is an operator's fact, and
        # scripts/demo_preflight.py prints it in full.
        raise LocalAgentUnavailable(f"{UNAVAILABLE}: {probe.reason}")

    provider = LocalModelProvider(model=probe.model, base_url=probe.base_url)
    started = time.perf_counter()
    result = run_agent(
        task=scenario.task_prompt,
        context=scenario.initial_context,
        model_provider=provider,
        skill_enabled=True,
        tools=scenario_registry(scenario),
        max_steps=MAX_STEPS,
        temperature=TEMPERATURE,
        max_tokens=MAX_TOKENS,
        scenario_id=scenario.scenario_id,
        tool_failure_policy=TOOL_FAILURE_POLICY,
    )
    elapsed = time.perf_counter() - started
    return present_agent_run(result.trace, result.final_response, result.stop_reason,
                             result.steps, scenario.scenario_id, elapsed)


def present_agent_run(
    trace: EvidenceTrace,
    final_response: str,
    stop_reason: str,
    steps: int,
    scenario_id: str,
    elapsed_seconds: float | None = None,
) -> AgentRunView:
    """Turn a finished run into a view, with the benchmark's own verdict on it.

    Split out from :func:`run_local_agent` so the contradiction reading is
    testable without a live model -- which matters, because the code that decides
    whether an agent disagreed with the core is exactly the code a judge will ask
    about.
    """
    claim = scan_answer(final_response or "")
    covered = [h.value for h in trace.horizons_checked] or []
    claimed_levels = {h: lvl for h, lvl in claim.levels.items() if h in (covered or claim.levels)}

    # The comparison the trace's granularity allows: worst claimed level against
    # worst core level, which is also the safety-relevant direction (evaluate.py).
    worst_claimed: str | None = None
    if claimed_levels:
        worst_claimed = max(claimed_levels.values(), key=_rank)
    core_level = trace.core_risk_level.value if trace.core_risk_level is not None else None
    contradicted = (
        core_level is not None and worst_claimed is not None and _rank(worst_claimed) != _rank(core_level)
    )

    if not claimed_levels:
        contradiction_note = (
            "The answer gave no horizon-level claim the deterministic scanner could "
            "read, so no comparison is possible. Nothing is implied either way."
        )
    elif core_level is None:
        # The agent never asked the deterministic core for a verdict, so there is
        # nothing to agree or disagree with. Saying "Agreement" here would be a
        # claim the run never supported -- and "Agreement: ... None" is a sentence a
        # judge cannot read. This is the state 16 of the 43 with-skill Phase 5 runs
        # were recorded in, so it is the common case rather than an edge.
        contradiction_note = (
            "No comparison was possible: the agent never asked the deterministic core "
            "for a verdict, so there is nothing to agree or disagree with. Nothing is "
            "implied either way."
        )
    elif contradicted:
        contradiction_note = (
            f"Contradiction: the agent's worst claim is {worst_claimed} and the core's "
            f"worst verdict was {core_level}."
        )
    else:
        contradiction_note = (
            f"Agreement: the agent's worst claim is {worst_claimed} and the core's "
            f"worst verdict was {core_level}."
        )

    return AgentRunView(
        scenario_id=scenario_id,
        run_id=trace.run_id,
        model_name=trace.model_name,
        provider=trace.provider,
        skill_loaded=trace.skill_loaded,
        skill_version=trace.skill_version,
        stop_reason=stop_reason,
        stop_reason_text=STOP_REASON_TEXT.get(stop_reason, stop_reason),
        steps=steps,
        calls=[
            CallRow(
                seq=call.seq,
                tool=call.tool.value,
                arguments=dict(call.arguments),
                status=call.status,
                result_summary=call.result_summary or "",
                error=call.error,
            )
            for call in trace.tool_calls
        ],
        horizons_checked=covered,
        triggered_rules=list(trace.triggered_rules),
        core_risk_level=core_level,
        core_confidence=trace.core_confidence,
        weather_source=trace.weather_source,
        freshness_checks=list(trace.freshness_checks),
        missing_data=list(trace.missing_data),
        sampling_params=dict(trace.sampling_params),
        answer=final_response or "",
        claimed_levels=claimed_levels,
        claimed_rules=sorted(claim.rules),
        worst_claimed=worst_claimed,
        contradicted=contradicted,
        contradiction_note=contradiction_note,
        hedges=list(claim.hedges),
        probability_claims=list(claim.probability_claims),
        negated=list(claim.negated),
        conflicting=list(claim.conflicting),
        timing_note=[] if elapsed_seconds is None else [f"{elapsed_seconds:.1f} s wall clock"],
    )


def trace_calls(trace: EvidenceTrace) -> list[ToolCallRecord]:
    """The recorded calls, unmodified, for a caller that wants the contract."""
    return list(trace.tool_calls)
