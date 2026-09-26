"""Trace collector that assembles a structured evidence trace (spec 18, 15).

The collector is the only writer of :class:`~trailguard.schemas.trace.EvidenceTrace`.
It is intentionally dumb: it appends what the harness observed, in order, and
stamps nothing on its own. The interesting safety content -- which horizons were
analysed, whether freshness passed, which rules fired -- is handed to it by the
caller as :class:`~trailguard.schemas.trace.HorizonCheckRecord`,
:class:`~trailguard.schemas.risk.RuleDriver` and rule-id lists, so the trace
remains a record of evidence rather than a place where behaviour is re-derived.

Redaction policy: every argument dict is funnelled through
:class:`~trailguard.schemas.trace.ToolCallRecord`, whose validator applies
:func:`~trailguard.schemas.trace.EvidenceTrace.redact_mapping`; the trace
contract additionally runs the text policy over its free-text and mapping
fields, so nothing the harness writes can park a credential in a summary, an
error message, a freshness log line or the weather source. This module
therefore never stores a credential-shaped value, and it never logs to stdout.

Provenance note: ``weather_source`` is the one trace field this collector writes
from data a *tool* returned rather than from the skill's own bookkeeping. The
harness that executes ``get_weather_for_route_time`` is the only code that sees
the provider id of the observation the model actually used, so it hands that
id to :meth:`TraceCollector.record_weather_source` instead of leaving the field
permanently empty (spec 18 requires the weather source as trace content).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from ..schemas.risk import RuleDriver, RiskLevel
from ..schemas.route import Horizon
from ..schemas.trace import (
    EvidenceTrace,
    HorizonCheckRecord,
    ToolCallRecord,
)

#: Status vocabulary of a recorded tool call (mirrors ToolCallRecord.status).
ToolCallStatus = str


def _now() -> datetime:
    """Current UTC instant (timezone-aware, so ISO8601 output is unambiguous)."""
    return datetime.now(timezone.utc)


@dataclass
class TraceCollector:
    """Mutable accumulator turned into an :class:`EvidenceTrace` by :meth:`finish`.

    Responsibility: collect tool calls, horizon checks, triggered rules, risk
    drivers, missing data and the final verdict for exactly one agent run, and
    emit them as the frozen trace contract. Not responsible for: executing
    tools, deciding whether a horizon is required, judging freshness, or
    scoring anything -- those belong to the harness, the skill protocol and the
    eval layer respectively.
    """

    run_id: str
    model_name: str = ""
    provider: str = ""
    skill_loaded: bool = False
    scenario_id: str | None = None
    sampling_params: dict[str, Any] = field(default_factory=dict)
    tool_calls: list[ToolCallRecord] = field(default_factory=list)
    horizon_checks: list[HorizonCheckRecord] = field(default_factory=list)
    weather_source: str | None = None
    triggered_rules: list[str] = field(default_factory=list)
    risk_drivers: list[RuleDriver] = field(default_factory=list)
    missing_data: list[str] = field(default_factory=list)
    final_risk_level: RiskLevel | None = None
    confidence: float | None = None
    uncertainty_disclosed: bool = False
    #: Per-horizon verdicts the deterministic core returned, in the order the
    #: agent asked for them. Kept as a list so the aggregate below is a
    #: documented selection over real values rather than a running guess.
    core_verdicts: list[tuple[Horizon, RiskLevel, float | None]] = field(default_factory=list)
    skill_version: str | None = None
    skill_sha256: str | None = None
    started_at: datetime = field(default_factory=_now)
    finished_at: datetime | None = None

    @classmethod
    def new(
        cls,
        run_id: str,
        scenario_id: str | None = None,
        model_name: str = "",
        provider: str = "",
        skill_loaded: bool = False,
        sampling_params: dict[str, Any] | None = None,
        started_at: datetime | None = None,
    ) -> "TraceCollector":
        """Create a collector for one run.

        Args:
            run_id: Unique run identifier.
            scenario_id: Scenario the run belongs to, when replaying a dataset.
            model_name: Model identifier reported by the provider.
            provider: Provider identifier (provider class name by convention).
            skill_loaded: Whether SKILL.md text was injected in this condition.
            sampling_params: Decoding parameters recorded for reproducibility.
            started_at: Run start instant; defaults to the current UTC instant.
        """
        return cls(
            run_id=run_id,
            scenario_id=scenario_id,
            model_name=model_name,
            provider=provider,
            skill_loaded=skill_loaded,
            sampling_params=dict(sampling_params or {}),
            started_at=started_at or _now(),
        )

    # ------------------------------------------------------------------ tools

    def record_tool_call(
        self,
        tool: str,
        arguments: dict[str, Any],
        result_ref: str | None,
        result_summary: str | None,
        status: ToolCallStatus,
        error: str | None = None,
    ) -> ToolCallRecord:
        """Append one tool invocation and return the stored record.

        ``arguments`` are recorded exactly as received; secret-shaped keys are
        replaced with ``"<redacted>"`` by the contract's own validator, so this
        method is safe to call with provider-shaped payloads.
        """
        record = ToolCallRecord(
            seq=len(self.tool_calls) + 1,
            tool=tool,
            arguments=arguments or {},
            result_ref=result_ref,
            result_summary=result_summary,
            called_at=_now(),
            status=status,
            error=error,
        )
        self.tool_calls.append(record)
        return record

    # --------------------------------------------------------------- horizons

    def record_horizon(self, record: HorizonCheckRecord) -> HorizonCheckRecord:
        """Append one per-horizon check (freshness and missing-data evidence).

        Responsibility: store the evidence that horizon ``record.horizon`` was
        actually analysed. Not responsible for: verifying the horizon set -- the
        required-horizon comparison is an eval metric (Phase 5).
        """
        self.horizon_checks.append(record)
        return record

    # ----------------------------------------------------------------- rules

    def record_triggered_rules(self, rule_ids: list[str]) -> None:
        """Record rule ids reported as triggered, preserving first-seen order."""
        for rule_id in rule_ids:
            if rule_id not in self.triggered_rules:
                self.triggered_rules.append(rule_id)

    def record_risk_drivers(self, drivers: list[RuleDriver]) -> None:
        """Record risk drivers reported by the agent, de-duplicated exactly."""
        for driver in drivers:
            if driver not in self.risk_drivers:
                self.risk_drivers.append(driver)

    def record_missing_data(self, missing: list[str]) -> None:
        """Record field dot-paths reported as missing or stale."""
        for field_path in missing:
            if field_path not in self.missing_data:
                self.missing_data.append(field_path)

    # -------------------------------------------------------------- provenance

    def record_weather_source(self, source: str | None) -> None:
        """Record the weather provider the run actually used (spec 18).

        Responsibility: keep the ``weather_source`` of the trace a fact about
        this run instead of a declared intention -- the value comes from the
        ``source`` field of the observation the weather tool returned. Not
        responsible for: judging whether that provider is acceptable (the
        weather policy owns that), or overwriting a recorded source with a
        later, emptier one: a ``None`` argument is ignored, because "no
        observation seen yet" must not erase one that was.
        """
        if source is None:
            return
        self.weather_source = source

    # ----------------------------------------------------------------- final

    def set_final(
        self,
        risk_level: RiskLevel | None,
        confidence: float | None,
        uncertainty_disclosed: bool,
    ) -> None:
        """Record the final verdict claimed by the agent.

        Responsibility: store what the agent *claimed*, so an eval can compare
        it with what the deterministic safety core produced. Not responsible
        for: validating or overriding the claim (spec 10.5 forbids the LLM from
        editing the core's level, and forbids this layer from hiding that it did).
        """
        self.final_risk_level = risk_level
        self.confidence = confidence
        self.uncertainty_disclosed = uncertainty_disclosed

    # ------------------------------------------------------------------ core

    def record_core_verdict(
        self,
        horizon: Horizon,
        risk_level: RiskLevel,
        confidence: float | None,
    ) -> None:
        """Record one verdict the deterministic core actually produced.

        Responsibility: keep what the *tools* returned separate from what the
        agent claimed, so a benchmark can measure the gap between them (spec 18).
        Not responsible for: choosing which horizon the agent should have
        assessed, or re-deriving a level -- every value here is copied verbatim
        out of an :class:`~trailguard.schemas.risk.RiskResult` the model's own
        ``assess_risk`` call returned.

        Which horizons are recorded is entirely the model's doing: a run that
        never called ``assess_risk`` records nothing here, and ``core_risk_level``
        stays ``None``. That is an honest "no assessment happened", not a missing
        field to be filled in.
        """
        self.core_verdicts.append((horizon, risk_level, confidence))

    def worst_core_verdict(self) -> tuple[RiskLevel, float | None] | None:
        """Return the worst verdict the core produced, and its confidence.

        A projected-risk-state answer is only as safe as its worst horizon, so
        the trace's headline ``core_risk_level`` is the highest severity rank
        among the horizons the agent actually assessed -- using the frozen
        :attr:`~trailguard.schemas.risk.RiskLevel.rank` order, never a
        locally-invented severity ladder. Ties keep the first-seen order, and the
        reported confidence is the one attached to that same verdict, so the two
        numbers always describe one assessment rather than a blend.

        This is a *selection* among values the deterministic core already
        produced, not a risk computation: no threshold, rule or score is applied
        here, and nothing may lower a level.
        """
        if not self.core_verdicts:
            return None
        # ``max`` returns the FIRST maximal element, which is what makes the
        # first-seen tie-break above true by construction. A stable ``sorted(...)[-1]``
        # would also order by rank -- and silently resolve a tie to the LAST horizon,
        # so the docstring and the code would disagree about which assessment the
        # reported confidence belongs to. Written as ``max`` so the tie behaviour is
        # the documented one rather than an accident of list indexing.
        horizon, level, confidence = max(self.core_verdicts, key=lambda verdict: verdict[1].rank)
        del horizon
        return level, confidence

    # ----------------------------------------------------------------- output

    def freshness_log(self) -> list[str]:
        """Human-readable freshness entries derived from the horizon checks."""
        entries: list[str] = []
        for check in self.horizon_checks:
            if check.weather_freshness_min is None:
                entries.append(f"{check.horizon.value}: weather freshness not reported")
                continue
            if check.freshness_ok is None:
                verdict = "not checked"
            elif check.freshness_ok:
                verdict = "within freshness budget"
            else:
                verdict = "STALE - outside freshness budget"
            entries.append(
                f"{check.horizon.value}: weather age {check.weather_freshness_min} min ({verdict})"
            )
        return entries

    def horizons_seen(self) -> list[Horizon]:
        """Horizons covered by the recorded checks, in first-seen order."""
        seen: list[Horizon] = []
        for check in self.horizon_checks:
            if check.horizon not in seen:
                seen.append(check.horizon)
        return seen

    def finish(self, finished_at: datetime) -> EvidenceTrace:
        """Freeze the collector into the :class:`EvidenceTrace` contract.

        Derived (never re-decided) values: ``horizons_checked`` comes from the
        recorded checks, ``weather_freshness_min`` is the age of the *freshest*
        weather actually used, and ``freshness_checks`` is the readable log of
        the same checks. ``weather_source`` is passed through exactly as
        :meth:`record_weather_source` stored it. ``core_risk_level`` /
        ``core_confidence`` come from :meth:`worst_core_verdict`, i.e. from the
        core's own output -- never from the agent's prose.
        """
        ages = [
            check.weather_freshness_min
            for check in self.horizon_checks
            if check.weather_freshness_min is not None
        ]
        worst = self.worst_core_verdict()
        trace = EvidenceTrace(
            run_id=self.run_id,
            scenario_id=self.scenario_id,
            model_name=self.model_name,
            provider=self.provider,
            skill_loaded=self.skill_loaded,
            # Provenance only: the version the skill's own frontmatter declares,
            # read by trailguard.agent.skill_loader.skill_identity(). No protocol
            # content is read out of the skill text (spec 3.1, 15).
            skill_version=self.skill_version,
            skill_sha256=self.skill_sha256,
            started_at=self.started_at,
            finished_at=finished_at,
            tool_calls=list(self.tool_calls),
            horizons_checked=self.horizons_seen(),
            horizon_checks=list(self.horizon_checks),
            weather_source=self.weather_source,
            weather_freshness_min=min(ages) if ages else None,
            freshness_checks=self.freshness_log(),
            triggered_rules=list(self.triggered_rules),
            risk_drivers=list(self.risk_drivers),
            final_risk_level=self.final_risk_level,
            confidence=self.confidence,
            core_risk_level=worst[0] if worst else None,
            core_confidence=worst[1] if worst else None,
            missing_data=list(self.missing_data),
            uncertainty_disclosed=self.uncertainty_disclosed,
            sampling_params=dict(self.sampling_params),
        )
        self.finished_at = finished_at
        return trace
