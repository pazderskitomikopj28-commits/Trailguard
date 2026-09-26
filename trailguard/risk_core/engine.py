"""Deterministic risk engine and the pipeline order (spec sections 7, 8, 5.5).

The safety core is the reason TrailGuard does not let a language model feel
its way to a safety verdict: risk level, score, drivers and confidence come
from rules and arithmetic, and the LLM may only explain them (spec section 2,
"LLM 不能 ... 自行修改 Safety Core risk level / 覆盖 hard rule").

Pipeline order (fixed, and the order Phase 1 implements):

1. **Feature extraction** -- flatten the caller-supplied inputs (route state,
   weather state, device state, time state, and the horizon's projection when
   the caller passed one) into the named features the rule pack refers to,
   plus the derived features the rules name (``time_margin_min``,
   ``pace_drop_percent``, ``projected_battery_percent``).
2. **Hard rules set a severity floor** -- each triggered hard rule (R-A route
   deviation, R-B sunset margin) raises the level floor; a level may never be
   lowered below the floor, which is what makes hard rules non-overridable.
3. **Soft score** -- triggered soft rules (R-C future weather, R-D pace
   degradation, R-E battery x remaining route) contribute to the aggregate
   soft score in [0, 100] (see ``risk_core/scoring.py``).
4. **Level mapping** -- the score maps onto the calibration bands.
5. **R-F compound escalation** -- two or more independent soft rules at or above
   the pack's configured minimum severity (``min_severity: MEDIUM`` in the frozen
   pack) at the same horizon escalate the verdict by exactly one level, as
   configured in the pack (never hardcoded in the prompt).
6. **Confidence** -- data quality, weather freshness and projection confidence
   combine into ``confidence`` (see ``risk_core/confidence.py``).

Steps 5 and 6 interact in one direction only, and the direction is the point of
the design: the compound escalation and the hard-rule floor can *raise* the
level the soft score produced, never lower it (spec 10.5). A soft score that
lands in YELLOW with a triggered HIGH-severity hard rule reports ORANGE; a
soft score that lands in ORANGE with no hard rule reports ORANGE. Nothing in
the pipeline may dial a level down, which is what makes a hard rule
non-overridable by construction rather than by convention.

Non-responsibilities of the engine, all of which belong to the skill or the
tools: fetching inputs, projecting future positions, fetching future weather,
running the 30/60/120 horizon loop, and writing the user-facing explanation.
The engine reads no clock either (spec 19): the instant a verdict is stamped
with is the caller's ``analysis_time``.

No language model is reachable from here. This module imports schemas, the
rule-pack loader, the calibration loader, the movement model, the feature
extractor, the scoring module and the confidence module -- and nothing else.
``assess_risk`` therefore runs with ``STEPFUN_API_KEY`` unset and with no
network, which is the condition the Phase 1 tests assert.
"""

from __future__ import annotations

from abc import ABC, abstractmethod

from ..schemas.context import DataQualityReport, DeviceState, RouteContext, TimeState
from ..schemas.risk import RiskLevel, RiskResult, RuleDriver, SafetyMargins, Severity
from ..schemas.route import Horizon, RouteProjection
from ..schemas.weather import WeatherForRouteTime
from .calibration import CALIBRATION_PATH, Calibration, Clause, load_calibration
from .confidence import compute_confidence
from .features import CONTEXT_FEATURE_NAMES, FEATURE_NAMES, FeatureVector, extract_features
from .hard_rules import RuleDefinition, RulePack
from .movement import MovementModel
from .scoring import aggregate_soft_score, level_for_score

#: Phase label used by every not-yet-implemented engine entry point, so the
#: failure names the phase that owes the work (Phase 0 writes no risk logic).
#: The wording stops at the compound-escalation step on purpose: it lists the
#: steps a *placeholder* refuses to run, and a reader looking for the confidence
#: step is sent to ``risk_core/confidence.py``, which Phase 1 implements, rather
#: than told the engine owes it.
_PHASE1_MESSAGE = (
    "Phase 1: deterministic safety core (feature extraction -> hard rules set severity "
    "floor -> soft score -> level mapping -> R-F compound escalation)"
)


class RiskEngine(ABC):
    """Deterministic safety core: rules in, explainable risk verdict out.

    Responsibility: execute the loaded rule pack against one horizon's inputs
    and return a :class:`RiskResult` -- level, integer score, drivers, hard
    rules triggered, confidence, evidence refs and physical-unit safety
    margins. Not responsible for: acquiring its own inputs, projecting future
    positions, fetching weather for a projected time, iterating horizons, or
    phrasing the explanation. ``assess_risk`` is one atomic call for one
    horizon (spec 4.2, 5.5).

    Phase 0 provides only the boundary: the concrete evaluation is Phase 1.
    """

    def __init__(self, pack: RulePack | None = None) -> None:
        """Store the rule pack this engine evaluates.

        Args:
            pack: Rule pack to evaluate. Concrete Phase 1 implementations are
                expected to accept and require it; the Phase 0 placeholder
                accepts ``None`` so it can be constructed before the pack is
                loaded.
        """
        self._pack = pack

    @property
    def pack(self) -> RulePack | None:
        """The rule pack this engine evaluates, if one was supplied."""
        return self._pack

    @abstractmethod
    def assess_risk(
        self,
        route_state: RouteContext,
        weather_state: WeatherForRouteTime,
        user_device_state: DeviceState,
        time_state: TimeState,
        projected_state: RouteProjection | None = None,
    ) -> RiskResult:
        """Assess one horizon and return an explainable verdict.

        Args:
            route_state: Current route position facts (spec 5.1).
            weather_state: Weather valid for the assessed position/time (spec 5.3).
            user_device_state: Device snapshot including battery/network (spec 5.4).
            time_state: Analysis clock and sunset instant.
            projected_state: The horizon's projection, when the caller made one
                (never for NOW). Supplies ``projected_arrival_time`` and
                ``projection_confidence`` for the projection-derived safety
                margins of spec 8; ``None`` means those margins stay unknown.

        Returns:
            A :class:`RiskResult` for this horizon.

        Raises:
            NotImplementedError: always, in Phase 0 -- the pipeline is Phase 1.
        """
        raise NotImplementedError(_PHASE1_MESSAGE)

    @classmethod
    def from_rule_pack(cls, pack: RulePack) -> "RiskEngine":
        """Build an engine bound to ``pack``.

        The pack is data, not code: the engine keeps thresholds out of its own
        source so they can be re-tuned without touching engine or prompt text.

        Called on the abstract base it returns the Phase 0 placeholder, which
        carries the pack and raises ``NotImplementedError`` on assessment. Called
        on a concrete subclass (``Phase1Engine.from_rule_pack(pack)``) it returns
        that subclass: the base cannot be instantiated -- it has an abstract
        method -- so a hardcoded ``_DeferredRiskEngine(pack)`` here would
        silently hand back the placeholder no matter which engine a caller asked
        for, defeating the factory entirely. Phase 1 replaces the placeholder.

        Args:
            pack: The validated rule pack to evaluate.

        Returns:
            An engine bound to ``pack``.
        """
        if cls is RiskEngine:
            return _DeferredRiskEngine(pack)
        return cls(pack)


class DeterministicRiskEngine(RiskEngine):
    """The Phase 1 engine: it evaluates the pack, and only the pack.

    Responsibility: run the six documented steps for one horizon's inputs and
    return a :class:`RiskResult`. Not responsible for: acquiring inputs, reading
    the clock, projecting a future position, calling a model, or deciding what to
    *do* about the verdict.

    How the pack drives the engine
    ------------------------------
    No rule is named in this class's conditionals. What each rule compares
    against, in which direction and at which band comes from
    ``calibration_v1.yaml``'s clause table, and the numbers come from the pack's
    own ``thresholds``; the loader refuses to build an engine for a
    calibration/pack pair that does not cover each other exactly
    (:meth:`~trailguard.risk_core.calibration.Calibration.validate_against`).
    The consequence is that re-tuning a threshold is a YAML edit, and a rule
    whose input feature is missing is *disclosed* rather than silently skipped.
    """

    def __init__(
        self,
        pack: RulePack | None = None,
        calibration: Calibration | None = None,
        movement: MovementModel | None = None,
    ) -> None:
        """Bind this engine to a pack, a calibration and a movement model.

        Args:
            pack: The validated rule pack to evaluate.
            calibration: Demo-calibration parameters. Defaults to
                ``calibration_v1.yaml``, validated against ``pack``.
            movement: Frozen movement model. Defaults to the one the calibration's
                ``movement`` block declares.

        Raises:
            ValueError: the pack is missing, or the calibration and the pack do
                not cover each other.
        """
        super().__init__(pack)
        if pack is None:
            raise ValueError("the deterministic engine needs a rule pack: it evaluates nothing on its own")
        self._calibration = calibration if calibration is not None else load_calibration(CALIBRATION_PATH)
        self._calibration.validate_against(pack)
        self._movement = (
            movement
            if movement is not None
            else MovementModel(
                base_speed_weights=self._calibration.base_speed_weights,
                fatigue_penalty_cap=self._calibration.fatigue_penalty_cap,
            )
        )

    # -------------------------------------------------------------- accessors

    @property
    def calibration(self) -> Calibration:
        """The demo-calibration parameters this engine evaluates with."""
        return self._calibration

    @property
    def movement_model(self) -> MovementModel:
        """The frozen movement model this engine derives speeds with."""
        return self._movement

    def _rule(self, rule_id: str) -> RuleDefinition:
        """The rule ``rule_id`` of this engine's pack.

        Centralised so the pack is checked for presence in one place rather than
        guarded at every use; a pack-less engine cannot be built.
        """
        pack = self._pack
        if pack is None:  # pragma: no cover - __init__ rejects a pack-less engine
            raise ValueError("this engine was built without a rule pack and cannot evaluate anything")
        return pack.get(rule_id)

    # ------------------------------------------------------------- evaluation

    def assess_risk(
        self,
        route_state: RouteContext,
        weather_state: WeatherForRouteTime | None,
        user_device_state: DeviceState,
        time_state: TimeState,
        projected_state: RouteProjection | None = None,
        *,
        horizon_min: int | None = None,
        weather_freshness_min: float | None = None,
        data_quality: DataQualityReport | float | None = None,
    ) -> RiskResult:
        """Assess one horizon and return an explainable verdict.

        Args:
            route_state: Current route position facts (spec 5.1).
            weather_state: Weather valid for the assessed position/time
                (spec 5.3). ``None`` is accepted because a caller with no
                snapshot must still be able to ask -- every weather feature is
                then not computable and R-C is disclosed as not evaluated.
            user_device_state: Device snapshot including battery/network (spec 5.4).
            time_state: Analysis clock and sunset instant. ``analysis_time`` is
                also the instant the verdict is stamped with; the engine never
                reads the wall clock (spec 19).
            projected_state: The horizon's projection, when the caller made one
                (never for NOW).
            horizon_min: Offset this assessment refers to. Defaults to the
                projection's own horizon, or 0 when there is no projection.
            weather_freshness_min: Age of the weather snapshot, when the caller
                computed it already. Defaults to
                ``weather_state.freshness_minutes``.
            data_quality: Caller-disclosed data-quality report (or a bare overall
                score). It feeds the confidence's reported-overall term and its
                missing/stale penalties; when it is absent, the route and device
                terms take the calibration's unknown value instead of being read
                off the inputs, because a confidence may only state what the
                caller disclosed -- an undisclosed quality is a gap, not a
                perfect one.

        Returns:
            A :class:`RiskResult` for this horizon: level, soft score, one driver
            per triggered rule, the triggered hard rules, confidence, the
            evidence references behind every number, and the physical-unit safety
            margins of spec 8.

        Raises:
            ValueError: a clause names a feature the extractor does not produce,
                or a clause names a threshold the pack does not define. Both are
                drift between the pack and the calibration.
        """
        horizon_offset = (
            int(projected_state.horizon_min)
            if projected_state is not None and horizon_min is None
            else int(horizon_min if horizon_min is not None else 0)
        )
        horizon = projected_state.horizon if projected_state is not None else Horizon.NOW

        features = extract_features(
            route_state=route_state,
            user_device_state=user_device_state,
            time_state=time_state,
            movement_model=self._movement,
            calibration=self._calibration,
            projected_state=projected_state,
            weather_state=weather_state,
            weather_freshness_min=weather_freshness_min,
            horizon_min=horizon_offset,
        )

        triggers = self._evaluate_rules(features)
        drivers: list[RuleDriver] = []
        hard_rule_ids: list[str] = []
        soft_rule_ids: list[str] = []

        for rule_id, trigger in triggers.items():
            if not trigger:
                continue
            rule = self._rule(rule_id)
            drivers.append(
                RuleDriver(
                    rule_id=rule_id,
                    type=rule.trigger_type,
                    severity=rule.severity,
                    detail=self._driver_detail(rule_id, trigger, features),
                )
            )
            (hard_rule_ids if rule.trigger_type == "hard" else soft_rule_ids).append(rule_id)

        compound_rule_ids = {
            rule_id
            for rule_id, evaluation in self._calibration.evaluations.items()
            if evaluation.kind == "compound"
        }
        compound_rule_id = self._apply_compound_escalation(
            soft_rule_ids, candidate_rule_ids=compound_rule_ids
        )
        if compound_rule_id is not None:
            rule = self._rule(compound_rule_id)
            drivers.append(
                RuleDriver(
                    rule_id=compound_rule_id,
                    type=rule.trigger_type,
                    severity=rule.severity,
                    detail=self._compound_detail(soft_rule_ids, compound_rule_id),
                )
            )
            soft_rule_ids.append(compound_rule_id)

        # The score aggregates the SOFT rules and nothing else. A hard rule
        # already acts through the severity floor, and counting it here as well
        # would charge the same danger twice -- once as points against the band
        # and once as a floor -- while making the compound rule's own escalation
        # invisible in exactly the way the R-F exemption below prevents. Both
        # exemptions arrive as data (the pack's ``trigger_type`` and the
        # calibration's ``kind: compound``), never as a rule id spelled out here.
        unscored_rule_ids = set(compound_rule_ids) | set(hard_rule_ids)
        score = aggregate_soft_score(drivers, exempt_rule_ids=unscored_rule_ids)
        band_level = level_for_score(score, bands=self._calibration.soft_score_bands)
        escalated_level = self._escalate(band_level, compound_rule_id)
        floor_level = self._hard_rule_floor(hard_rule_ids)
        level = max(escalated_level, floor_level, key=lambda candidate: candidate.rank)
        confidence = self._confidence_for(
            features=features,
            data_quality=data_quality,
            horizon_offset=horizon_offset,
            projection_used=projected_state is not None,
        )

        return RiskResult(
            risk_level=level,
            risk_score=score,
            drivers=drivers,
            hard_rules_triggered=hard_rule_ids,
            confidence=confidence,
            evidence_refs=self._evidence_refs(
                features=features,
                triggers=triggers,
                hard_rule_ids=hard_rule_ids,
                horizon=horizon,
                confidence=confidence,
                level=level,
                score=score,
            ),
            safety_margins=self._safety_margins(features, projected_state),
            horizon=horizon,
            computed_at=time_state.analysis_time,
        )

    # ------------------------------------------------------------ rule tables

    def _evaluate_rules(self, features: FeatureVector) -> dict[str, _Trigger]:
        """Evaluate every ``threshold_clauses`` rule of the pack against features.

        A rule fires when at least one of its clauses holds. A clause whose
        feature is not computable cannot hold or fail: it is recorded under
        ``not_evaluable`` so the verdict can say the rule was not evaluated
        rather than leaving a reader to assume it was checked and passed.

        Returns:
            Rule id -> :class:`_Trigger`, for every rule of the pack that the
            clause table covers.
        """
        triggers: dict[str, _Trigger] = {}
        for rule in self._pack.rules if self._pack is not None else []:
            evaluation = self._calibration.evaluations[rule.id]
            if evaluation.kind != "threshold_clauses":
                continue  # the compound rule is applied once the others are known
            matched: list[Clause] = []
            not_evaluable: list[tuple[str, str]] = []
            for clause in evaluation.clauses:
                self._check_feature_known(clause)
                value = features.number(clause.feature)
                if value is None:
                    not_evaluable.append(
                        (clause.threshold_key, f"{clause.feature} is not computable from the supplied inputs")
                    )
                    continue
                threshold = rule.thresholds.get(clause.threshold_key)
                if threshold is None:
                    raise ValueError(
                        f"rule {rule.id}: clause names threshold {clause.threshold_key!r}, which the pack "
                        "does not define; the calibration and the pack have drifted apart"
                    )
                if clause.matches(value, float(threshold)):
                    matched.append(clause)
            triggers[rule.id] = _Trigger(
                rule_id=rule.id,
                clauses=matched,
                not_evaluable=not_evaluable,
                non_evaluable_disclosures=dict(evaluation.non_evaluable_thresholds),
                context_features=evaluation.context_features,
            )
        return triggers

    def _check_feature_known(self, clause: Clause) -> None:
        """Fail loudly if a clause names a feature the extractor cannot produce.

        A typo in the clause table (``pace_drop_pct``) would otherwise read as
        "feature missing" and quietly disable a rule for the rest of the
        project's life; naming it here makes the typo a loud error instead.
        """
        if clause.feature in FEATURE_NAMES or clause.feature in CONTEXT_FEATURE_NAMES:
            return
        raise ValueError(
            f"rule {clause.rule_id}: clause {clause.threshold_key!r} compares unknown feature "
            f"{clause.feature!r}; the feature extractor produces {sorted(FEATURE_NAMES)}"
        )

    def _driver_detail(self, rule_id: str, trigger: _Trigger, features: FeatureVector) -> str:
        """A short, quotable sentence naming what fired, with its numbers."""
        rule = self._rule(rule_id)
        comparisons = [
            f"{clause.feature}={_format(features.number(clause.feature))} {clause.operator} "
            f"{_format(rule.thresholds.get(clause.threshold_key))} "
            f"({clause.threshold_key}, band {clause.band})"
            for clause in trigger.clauses
        ]
        detail = f"{rule.name}: " + "; ".join(comparisons)
        for threshold_key, disclosure in trigger.non_evaluable_disclosures.items():
            detail += f" | {threshold_key} not checked -- {disclosure}"
        context = [
            f"{name}={_format(features.get(name))}"
            for name in trigger.context_features
            if features.get(name) is not None
        ]
        if context:
            detail += " | context: " + ", ".join(context)
        return detail

    def _compound_detail(self, soft_rule_ids: list[str], compound_rule_id: str) -> str:
        """A short, quotable sentence naming the combination that fired.

        A combination rule has no threshold to compare, so its detail states the
        count it required, the severity floor it required, which rules satisfied
        both and how many levels the pack escalates by -- everything a reader
        needs to re-derive the escalation without opening the YAML.
        """
        rule = self._rule(compound_rule_id)
        minimum = rule.thresholds.get("min_independent_soft_rules", 0)
        raw_severity = rule.thresholds.get("min_severity", Severity.LOW.value)
        floor = raw_severity if isinstance(raw_severity, Severity) else Severity(str(raw_severity))
        steps = rule.thresholds.get("escalation_levels", 0)
        counted = [
            f"{candidate}({self._rule(candidate).severity.value})"
            for candidate in soft_rule_ids
            if candidate != compound_rule_id and self._rule(candidate).severity.rank >= floor.rank
        ]
        return (
            f"{rule.name}: {len(counted)} independent soft rules at or above {floor.value} "
            f"({', '.join(counted)}) meet the configured minimum of {minimum}, so the level "
            f"escalates by {steps}"
        )

    def _apply_compound_escalation(
        self, soft_rule_ids: list[str], *, candidate_rule_ids: set[str]
    ) -> str | None:
        """Return the compound rule's id when the pack's combination is met.

        The rule fires when at least ``min_independent_soft_rules`` *other* soft
        rules (it never counts itself) triggered at severity at or above
        ``min_severity``. Everything it applies -- the count, the severity floor
        and, later, the number of levels -- is read from the pack, so the
        escalation policy is auditable data (spec 7, R-F).

        The severity floor is compared by :attr:`Severity.rank`, never by the
        member itself: ``"HIGH" < "MEDIUM"`` as strings, so a raw ``>=`` would
        silently exclude the two loudest severities from ever qualifying.
        """
        for rule_id in sorted(candidate_rule_ids):
            rule = self._rule(rule_id)
            minimum = float(rule.thresholds.get("min_independent_soft_rules", 0))
            raw_severity = rule.thresholds.get("min_severity", Severity.LOW.value)
            floor = raw_severity if isinstance(raw_severity, Severity) else Severity(str(raw_severity))
            qualifying = [
                candidate
                for candidate in soft_rule_ids
                if candidate != rule_id and self._rule(candidate).severity.rank >= floor.rank
            ]
            if len(qualifying) >= minimum:
                return rule_id
        return None

    def _escalate(self, level: RiskLevel, compound_rule_id: str | None) -> RiskLevel:
        """Raise ``level`` by the compound rule's configured number of levels."""
        if compound_rule_id is None:
            return level
        steps = int(self._rule(compound_rule_id).thresholds.get("escalation_levels", 0))
        index = RiskLevel.LEVELS.index(level) + steps
        return RiskLevel.LEVELS[min(max(index, 0), len(RiskLevel.LEVELS) - 1)]

    def _hard_rule_floor(self, hard_rule_ids: list[str]) -> RiskLevel:
        """The level floor the triggered hard rules set.

        A hard rule can only raise a level, never lower one (spec 10.5), so the
        floor is the *maximum* of the floors its rules demand. With no hard rule
        triggered the floor is GREEN, which every level already satisfies.
        """
        floor = RiskLevel.GREEN
        for rule_id in hard_rule_ids:
            demanded = self._calibration.severity_floor_for(self._rule(rule_id).severity)
            if demanded.rank > floor.rank:
                floor = demanded
        return floor

    # ------------------------------------------------------------- disclosure

    def _confidence_for(
        self,
        *,
        features: FeatureVector,
        data_quality: DataQualityReport | float | None,
        horizon_offset: int,
        projection_used: bool,
    ) -> float:
        """The verdict's confidence, from the same inputs that produced it.

        The projection term deserves its own sentence: when a projection was
        used, it is that projection's own confidence; when a *future* horizon was
        assessed without one, it is the horizon's calibrated reliability, because
        a claim about +30 minutes made from unprojected data is a weaker
        statement than the data itself; and at NOW there is no projection being
        relied upon, so the term is perfect rather than penalised for its
        absence.

        Only the caller's *disclosed* missing and stale fields cost the
        per-field penalty. The core's own not-computable features are handled by
        the term-level unknown substitution, so charging them a penalty as well
        would pay the same gap twice. A caller that discloses no report at all
        therefore lands on the unknown value for the route and device terms: the
        confidence reports what was disclosed, and the skill's protocol always
        discloses it.
        """
        if projection_used:
            projection_term: float | None = features.number("projection_confidence")
        elif horizon_offset > 0:
            projection_term = self._calibration.horizon_reliability_for(horizon_offset)
        else:
            projection_term = 1.0

        report = data_quality if isinstance(data_quality, DataQualityReport) else None
        return compute_confidence(
            data_quality,
            weather_freshness_min=features.number("weather_freshness_min"),
            projection_confidence=projection_term,
            weather_quality=features.number("weather_data_quality"),
            missing_fields=report.missing_fields if report is not None else (),
            stale_fields=report.stale_fields if report is not None else (),
        )

    def _evidence_refs(
        self,
        *,
        features: FeatureVector,
        triggers: dict[str, _Trigger],
        hard_rule_ids: list[str],
        horizon: Horizon,
        confidence: float,
        level: RiskLevel,
        score: int,
    ) -> list[str]:
        """Pointers to everything behind the verdict (spec 18, evidence trace).

        Every clause that fired, every rule that could not be evaluated and why,
        every feature that was not computable, and the pack/calibration pair the
        verdict was computed with -- so a reader can re-derive the verdict from
        the refs alone.
        """
        refs: list[str] = [
            f"pack:{self._pack.pack_id}@{self._pack.version}",
            f"calibration:{self._calibration.pack_id}@{self._calibration.version}",
            f"horizon:{horizon.value}",
            f"soft_score:{score}",
            f"risk_level:{level.value}",
            f"confidence:{confidence}",
        ]
        for rule_id, trigger in triggers.items():
            if not trigger:
                continue
            for clause in trigger.clauses:
                refs.append(f"rule:{rule_id}:{clause.threshold_key}:{clause.feature}")
            for threshold_key, reason in trigger.not_evaluable:
                refs.append(f"rule:{rule_id}:{threshold_key}:not_evaluable:{reason}")
        for name in features.missing:
            refs.append(f"feature:{name}:not_computable")
        for note in features.notes:
            refs.append(f"note:{note}")
        if hard_rule_ids:
            refs.append("hard_rules:" + ",".join(hard_rule_ids))
        return refs

    def _safety_margins(
        self, features: FeatureVector, projected_state: RouteProjection | None
    ) -> SafetyMargins:
        """The physical-unit margins of spec 8, read straight off the features.

        ``projection_confidence`` is reported as ``None`` when no projection was
        used -- "there is no projection to trust" -- even though the confidence
        term still needs a value for the horizon it was asked about; that value is
        computed separately and is not a claim about a projection that exists.
        """
        return SafetyMargins(
            time_margin_min=features.number("time_margin_min"),
            projected_battery_percent=features.number("projected_battery_percent"),
            route_deviation_m=features.number("off_route_distance_m"),
            weather_freshness_min=features.number("weather_freshness_min"),
            projection_confidence=(
                float(projected_state.projection_confidence) if projected_state is not None else None
            ),
        )


class _Trigger:
    """What one rule's evaluation produced for one horizon.

    ``clauses`` lists the comparisons that held (empty means the rule did not
    fire), ``not_evaluable`` the comparisons that could not be made because their
    feature was not computable, ``non_evaluable_disclosures`` the pack thresholds
    a single-horizon call cannot check at all, and ``context_features`` the extra
    values reported with the driver. Truthiness follows ``clauses``: a rule with
    nothing matched did not trigger.
    """

    __slots__ = ("clauses", "context_features", "non_evaluable_disclosures", "not_evaluable", "rule_id")

    def __init__(
        self,
        *,
        rule_id: str,
        clauses: list[Clause],
        not_evaluable: list[tuple[str, str]],
        non_evaluable_disclosures: dict[str, str],
        context_features: tuple[str, ...] = (),
    ) -> None:
        self.rule_id = rule_id
        self.clauses = clauses
        self.not_evaluable = not_evaluable
        self.non_evaluable_disclosures = non_evaluable_disclosures
        self.context_features = context_features

    def __bool__(self) -> bool:
        """The rule triggered iff at least one of its comparisons held."""
        return bool(self.clauses)

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"_Trigger({self.rule_id}, clauses={len(self.clauses)}, not_evaluable={len(self.not_evaluable)})"


def _format(value: object) -> str:
    """Format a value for a human-readable driver detail, without decimal noise.

    Six significant digits, not four, for one reason found by testing: a strict
    comparison must never *print* as an equality. At four digits a projected battery
    of 19.999 % rendered as "20", and the driver detail read
    ``projected_battery_percent=20 lt 20`` -- a sentence a reader cannot verify and
    would rightly distrust. Six digits keeps the common cases compact (``7.15527``,
    ``135.142``, ``19.999``) while leaving the comparison visibly true.
    """
    if value is None:
        return "unknown"
    if isinstance(value, bool):
        return "yes" if value else "no"
    if isinstance(value, (int, float)):
        number = float(value)
        if number == int(number) and abs(number) < 1e6:
            return str(int(number))
        return f"{number:.6g}"
    return str(value)


class _DeferredRiskEngine(RiskEngine):
    """Placeholder engine returned by :meth:`RiskEngine.from_rule_pack`.

    Responsibility: hold the loaded pack so callers can wire the interface now
    and get an honest, phase-labelled failure instead of a silent wrong answer.
    Not responsible for: any evaluation. Phase 1 implements the real engine.
    """

    def __init__(self, pack: RulePack | None) -> None:
        """Store the pack this placeholder carries on behalf of Phase 1."""
        super().__init__(pack)

    def assess_risk(
        self,
        route_state: RouteContext,
        weather_state: WeatherForRouteTime,
        user_device_state: DeviceState,
        time_state: TimeState,
        projected_state: RouteProjection | None = None,
    ) -> RiskResult:
        """Refuse to assess and say which phase owes the implementation."""
        raise NotImplementedError(_PHASE1_MESSAGE)


__all__ = ["DeterministicRiskEngine", "RiskEngine"]
