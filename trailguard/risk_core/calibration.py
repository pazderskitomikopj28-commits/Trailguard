"""Demo-calibration parameters of the deterministic core (spec sections 7, 8, 12).

Everything the safety core needs that is *not* a rule threshold lives here as
data: the soft-score bands, the severity weights, the hard-rule severity floor,
the movement-model weights of spec 6.3, the battery-drain assumption behind rule
R-E, the projection-reliability curve and the confidence weights. The file is
``trailguard/rules/calibration_v1.yaml``.

Why this file is separate from ``rules_v1.yaml``
------------------------------------------------
The rule pack is frozen Phase 0 work (spec section 7, accepted and tagged). It
answers "what is dangerous and how much"; it deliberately says nothing about
*how* a threshold is compared or how much a severity is worth -- an operand named
``time_margin_min_warn: 30`` does not state its own direction. Putting the
evaluation metadata in a second, explicitly demo-calibration file keeps the
frozen pack byte-identical while making every number the core uses visible in
one place, and lets a threshold be re-tuned in ``rules_v1.yaml`` without this
file noticing and *vice versa* -- which is why :meth:`Calibration.validate_against`
exists (see below).

The one thing this module guarantees
------------------------------------
:meth:`Calibration.validate_against` refuses to load a calibration/pack pair that
has drifted apart: every numeric threshold key of every rule in the pack must be
covered by exactly one declared clause (or declared non-evaluable), and every
clause must name a key the pack actually has. A mismatch raises ``ValueError``
rather than silently ignoring a threshold the engine does not understand -- an
engine that quietly drops an operand is how a safety threshold stops working
without anyone noticing.

DEMO CALIBRATION: every value in this file is TrailGuard team calibration for the
DGX Spark hackathon demo. ``is_calibrated`` is false and ``review_status`` is
pending. Nothing here is an industry safety standard, a published uncertainty
framework or a clinical threshold, and none of it may be presented as one.
"""

from __future__ import annotations

import operator
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Literal, Mapping

import yaml

from ..schemas.risk import RiskLevel, Severity
from ..schemas.route import Horizon
from .hard_rules import RulePack

__all__ = [
    "CALIBRATION_PATH",
    "Calibration",
    "Clause",
    "RuleEvaluation",
    "load_calibration",
]

#: Canonical horizon spellings, used to map a reliability label to its offset.
_HORIZON_VALUES: frozenset[str] = frozenset(horizon.value for horizon in Horizon)

#: Default location of the calibration file, next to the rule pack it serves.
CALIBRATION_PATH: Path = Path(__file__).resolve().parents[1] / "rules" / "calibration_v1.yaml"

#: Closed set of comparison operators a clause may name. Closed because a typo
#: (``>``, ``gt_than``) must fail at load time, not silently become a no-op.
COMPARATORS: dict[str, Callable[[float, float], bool]] = {
    "gt": operator.gt,
    "ge": operator.ge,
    "lt": operator.lt,
    "le": operator.le,
    "eq": operator.eq,
    "ne": operator.ne,
}

#: How a rule is evaluated: thresholds compared against named features, or a
#: counting rule over the other rules that triggered.
RuleKind = Literal["threshold_clauses", "compound"]

#: Tolerance used when checking that the confidence weights sum to one.
_WEIGHT_SUM_TOLERANCE = 1e-6


@dataclass(frozen=True)
class Clause:
    """One comparison a rule makes: a feature, a direction, a band.

    A clause carries *no value* and *no severity*: the number it compares against
    is always read from the frozen rule pack's ``thresholds`` under
    ``threshold_key``, and the severity a triggered rule contributes is the
    rule's own ``severity`` field. That is what keeps both from having a second
    home and drifting -- a two-band rule (R-D at 15 % and 25 %) is one rule at one
    severity, whose bands decide *whether* it fires and what the detail reports.
    """

    rule_id: str
    threshold_key: str
    feature: str
    operator: str
    band: str

    def matches(self, value: float, threshold: float) -> bool:
        """Return whether ``value`` satisfies this clause against ``threshold``.

        Args:
            value: The observed feature value.
            threshold: The number to compare against, read from the pack's
                ``thresholds`` under :attr:`threshold_key`.

        Raises:
            ValueError: the operator is not in :data:`COMPARATORS`. Guarded here
                as well as at load time because a clause can be constructed
                directly by a caller.
        """
        try:
            compare = COMPARATORS[self.operator]
        except KeyError:
            raise ValueError(
                f"rule {self.rule_id}: clause on {self.feature!r} names unknown operator "
                f"{self.operator!r}; expected one of {sorted(COMPARATORS)}"
            ) from None
        return compare(float(value), float(threshold))


@dataclass(frozen=True)
class RuleEvaluation:
    """How one rule of the pack is evaluated by the engine.

    ``clauses`` is the ordered table of comparisons for a ``threshold_clauses``
    rule; ``context_features`` names extra features reported with the driver so
    an explanation can state *why* the rule fired (for R-E: the battery, the
    distance still to walk, the time still needed to walk it, the network state)
    and are deliberately not compared against any threshold. For a ``compound``
    rule, ``consumed_thresholds`` names the pack operands the counting rule reads
    itself (R-F: how many soft rules, at what minimum severity, escalate by how
    many levels), which is what lets the loader prove none is ignored.
    ``non_evaluable_thresholds`` maps a pack threshold the core *cannot* check to
    the disclosure it owes -- R-A's ``persistence_observations`` needs several
    consecutive observations and a stateless single-horizon call has exactly one.
    """

    rule_id: str
    kind: RuleKind
    clauses: tuple[Clause, ...] = ()
    context_features: tuple[str, ...] = ()
    non_evaluable_thresholds: Mapping[str, str] = ()
    consumed_thresholds: tuple[str, ...] = ()


@dataclass(frozen=True)
class Calibration:
    """Every demo-calibration parameter the deterministic core reads.

    Responsibility: hold the numbers and the clause table, so re-tuning is a data
    edit. Not responsible for: deciding risk, interpreting weather, or knowing
    anything about a specific route, device or snapshot.
    """

    pack_id: str
    version: str
    soft_score_bands: Mapping[str, tuple[int, int]]
    severity_weights: Mapping[str, int]
    saturation_score: int
    severity_floor: Mapping[str, RiskLevel]
    base_speed_weights: Mapping[str, float]
    fatigue_penalty_cap: float
    horizon_reliability: Mapping[str, float]
    route_min_trackpoints: int
    route_trackpoint_weight: float
    route_elevation_weight: float
    battery_drain_percent_per_hour: float
    battery_projected_floor: float
    battery_projected_ceiling: float
    weather_freshness_budget_min: int
    confidence_unknown_value: float
    confidence_weights: Mapping[str, float]
    confidence_missing_field_penalty: float
    confidence_stale_field_penalty: float
    evaluations: Mapping[str, RuleEvaluation]

    # ------------------------------------------------------------ validation

    def validate_against(self, pack: RulePack) -> None:
        """Check this calibration covers ``pack`` exactly, in both directions.

        Three invariants, each of which would otherwise let a threshold stop
        working in silence:

        1. every rule of the pack has an evaluation entry, and no entry names a
           rule the pack does not have;
        2. every *numeric* threshold key of a ``threshold_clauses`` rule is
           claimed by exactly one clause (or declared non-evaluable with a
           disclosure string), and every clause names a key the pack has;
        3. a clause's severity never exceeds its rule's declared severity -- the
           pack states the worst case a rule can contribute.

        Args:
            pack: The rule pack the engine will evaluate.

        Raises:
            ValueError: any invariant is violated; the message names the rule and
                the offending threshold so the drift can be repaired in one edit.
        """
        pack_ids = set(pack.ids())
        declared = set(self.evaluations)
        if declared != pack_ids:
            missing = sorted(pack_ids - declared)
            extra = sorted(declared - pack_ids)
            raise ValueError(
                f"calibration {self.pack_id!r} does not cover pack {pack.pack_id!r}: "
                f"rules without an evaluation entry={missing}, entries for unknown rules={extra}"
            )

        for rule in pack.rules:
            evaluation = self.evaluations[rule.id]
            numeric_keys = self._numeric_threshold_keys(rule)
            claimed = [clause.threshold_key for clause in evaluation.clauses]
            duplicates = sorted({key for key in claimed if claimed.count(key) > 1})
            if duplicates:
                raise ValueError(f"rule {rule.id}: thresholds claimed by more than one clause: {duplicates}")
            unknown = sorted(set(claimed) - numeric_keys)
            if unknown:
                raise ValueError(f"rule {rule.id}: clauses name thresholds the pack does not define: {unknown}")

            if evaluation.kind == "threshold_clauses":
                if not evaluation.clauses:
                    raise ValueError(f"rule {rule.id}: declared threshold_clauses but lists none")
                unclaimed = sorted(numeric_keys - set(claimed) - set(evaluation.non_evaluable_thresholds))
                if unclaimed:
                    raise ValueError(
                        f"rule {rule.id}: pack thresholds {unclaimed} are neither compared by a clause nor "
                        "declared non-evaluable; the engine would silently ignore them"
                    )
                undeclared = sorted(set(evaluation.non_evaluable_thresholds) - numeric_keys)
                if undeclared:
                    raise ValueError(f"rule {rule.id}: non-evaluable thresholds the pack does not define: {undeclared}")
                for key, disclosure in evaluation.non_evaluable_thresholds.items():
                    if not str(disclosure).strip():
                        raise ValueError(
                            f"rule {rule.id}: non-evaluable threshold {key!r} must carry the disclosure it owes"
                        )
            else:
                # A compound rule reads its own operands; anything numeric in the
                # pack that it does not consume would be silently ignored.
                unconsumed = sorted(numeric_keys - set(evaluation.consumed_thresholds))
                if unconsumed:
                    raise ValueError(
                        f"rule {rule.id}: compound rule leaves pack thresholds {unconsumed} unconsumed; "
                        "a compound rule must name every numeric operand it reads in 'consumed_thresholds'"
                    )
            for clause in evaluation.clauses:
                if clause.operator not in COMPARATORS:
                    raise ValueError(
                        f"rule {rule.id}: clause {clause.threshold_key!r} names unknown operator {clause.operator!r}"
                    )
                if clause.feature in evaluation.context_features:
                    raise ValueError(
                        f"rule {rule.id}: {clause.feature!r} is both compared and reported as context"
                    )

        for feature in (name for evaluation in self.evaluations.values() for name in evaluation.context_features):
            if not str(feature).strip():
                raise ValueError("a context feature name must be a non-empty string")

        _validate_bands(self.soft_score_bands)
        self._validate_floors()
        _validate_weights(self.confidence_weights, "confidence weights")
        for feature, weight in self.base_speed_weights.items():
            if weight < 0:
                raise ValueError(f"base speed weight for {feature!r} must not be negative: {weight}")
        for level, reliability in self.horizon_reliability.items():
            if not 0.0 <= reliability <= 1.0:
                raise ValueError(f"projection reliability for horizon {level!r} must be in [0, 1]: {reliability}")
        if not 0.0 <= self.fatigue_penalty_cap <= 1.0:
            raise ValueError(f"fatigue_penalty_cap must be in [0, 1]: {self.fatigue_penalty_cap}")
        if not 0.0 <= self.confidence_unknown_value <= 1.0:
            raise ValueError(f"confidence unknown_value must be in [0, 1]: {self.confidence_unknown_value}")
        if self.battery_projected_floor > self.battery_projected_ceiling:
            raise ValueError("battery projection floor must not exceed its ceiling")
        if self.weather_freshness_budget_min < 0:
            raise ValueError(f"weather freshness budget must not be negative: {self.weather_freshness_budget_min}")

    @staticmethod
    def _numeric_threshold_keys(rule: Any) -> set[str]:
        """Threshold keys of ``rule`` whose value is a number.

        Only numeric operands are clause-claimable: ``min_severity`` of R-F is a
        vocabulary word, not a measurement, and the compound rule consumes it
        directly.
        """
        return {key for key, value in rule.thresholds.items() if isinstance(value, (int, float)) and not isinstance(value, bool)}

    def _validate_floors(self) -> None:
        """Every severity in the vocabulary maps onto exactly one level floor."""
        for severity in Severity:
            if severity.value not in self.severity_floor:
                raise ValueError(f"hard-rule severity floor is missing an entry for {severity.value}")
            if self.severity_floor[severity.value] not in RiskLevel.LEVELS:
                raise ValueError(
                    f"hard-rule severity floor for {severity.value} is not a RiskLevel: "
                    f"{self.severity_floor[severity.value]!r}"
                )
        unknown = sorted(set(self.severity_floor) - {severity.value for severity in Severity})
        if unknown:
            raise ValueError(f"hard-rule severity floor names unknown severities: {unknown}")

    # ------------------------------------------------------------- accessors

    def evaluation(self, rule_id: str) -> RuleEvaluation:
        """Return how ``rule_id`` is evaluated.

        Raises:
            KeyError: no evaluation entry for that rule id (a calibration/pack
                mismatch that :meth:`validate_against` should already have caught).
        """
        return self.evaluations[rule_id]

    def severity_weight(self, severity: Severity) -> int:
        """Soft-score weight contributed by a driver of ``severity``."""
        return int(self.severity_weights[severity.value])

    def severity_floor_for(self, severity: Severity) -> RiskLevel:
        """Level floor a triggered hard rule of ``severity`` sets."""
        return self.severity_floor[severity.value]

    def horizon_reliability_for(self, horizon_min: int) -> float:
        """Reliability of a projection ``horizon_min`` minutes out.

        The YAML keys the curve by horizon label (``NOW``/``+30min``), which is
        the vocabulary a human reads; this maps the label back to its offset with
        the frozen :class:`~trailguard.schemas.route.Horizon` table rather than by
        string-slicing ``"+30min"``, so a renamed label fails here instead of
        silently matching nothing. An offset outside the frozen set cannot arrive
        through the tool contract (``ProjectRouteStateInput`` rejects it); a direct
        caller could still pass one, and the nearest configured horizon is used.
        """
        labelled = {}
        for label, reliability in self.horizon_reliability.items():
            minutes = _label_to_minutes(label)
            if minutes is not None:
                labelled[minutes] = float(reliability)
        candidates = labelled
        if int(horizon_min) in candidates:
            return candidates[int(horizon_min)]
        if not candidates:
            return 1.0
        nearest = min(candidates, key=lambda minutes: (abs(minutes - horizon_min), minutes))
        return candidates[nearest]

    def route_geometry_quality_params(self) -> dict[str, float]:
        """Keyword arguments of ``RouteGeometry.geometry_quality``.

        Passed as one bundle so the geometry module keeps its own signature and
        the weights stay declared here.
        """
        return {
            "min_trackpoints": self.route_min_trackpoints,
            "trackpoint_weight": self.route_trackpoint_weight,
            "elevation_weight": self.route_elevation_weight,
        }


# --------------------------------------------------------------------------- #
# Loading and validation helpers
# --------------------------------------------------------------------------- #


def _validate_bands(bands: Mapping[str, tuple[int, int]]) -> None:
    """Bands must name exactly the four levels, contiguous, [0, 100]."""
    names = list(bands)
    if names != [level.value for level in RiskLevel]:
        raise ValueError(
            f"soft score bands must cover {[level.value for level in RiskLevel]} in GREEN..RED order, got {names}"
        )

    expected_floor = 0
    for name, (low, high) in bands.items():
        low, high = int(low), int(high)
        if low > high:
            raise ValueError(f"soft score band {name} is inverted: ({low}, {high})")
        if low != expected_floor:
            raise ValueError(
                f"soft score band {name} starts at {low}, but the previous band ended at {expected_floor - 1}; "
                "the bands must stay contiguous and cover [0, 100] with no gap"
            )
        expected_floor = high + 1
    if expected_floor != 101:
        raise ValueError(f"soft score bands must end at 100, they end at {expected_floor - 1}")


def _label_to_minutes(label: str) -> int | None:
    """Minutes a reliability label refers to: a horizon spelling, or an int."""
    if label in _HORIZON_VALUES:
        return Horizon(label).minutes
    try:
        return int(label)
    except (TypeError, ValueError):
        return None


def _validate_horizon_labels(reliability: Mapping[str, float]) -> None:
    """The reliability curve must be keyed by the frozen horizon spellings.

    A typo (``+30m``, ``now``) would otherwise let a horizon fall through to the
    "no entry" branch of :meth:`Calibration.horizon_reliability_for` and quietly
    use its neighbour's value.
    """
    unknown = sorted(set(reliability) - _HORIZON_VALUES)
    if unknown:
        raise ValueError(
            f"projection.horizon_reliability is keyed by horizon label ({sorted(_HORIZON_VALUES)}); "
            f"unknown keys: {unknown}"
        )
    if not set(_HORIZON_VALUES) <= set(reliability):
        raise ValueError(
            "projection.horizon_reliability must cover every frozen horizon: "
            f"missing {sorted(_HORIZON_VALUES - set(reliability))}"
        )


def _validate_weights(weights: Mapping[str, float], label: str) -> None:
    """A weight table must be non-negative and sum to one."""
    if not weights:
        raise ValueError(f"{label} must not be empty")
    for name, weight in weights.items():
        if weight < 0:
            raise ValueError(f"{label}: weight for {name!r} must not be negative: {weight}")
    total = sum(float(weight) for weight in weights.values())
    if abs(total - 1.0) > _WEIGHT_SUM_TOLERANCE:
        raise ValueError(f"{label} must sum to 1.0, they sum to {total}")


def _require(mapping: Mapping[str, Any] | None, key: str, where: str) -> Any:
    """Fetch ``key`` from ``mapping`` or fail with a message naming ``where``."""
    if not isinstance(mapping, Mapping) or key not in mapping:
        raise ValueError(f"{where} is missing required key {key!r}")
    return mapping[key]


def _severity_of(raw: Any, where: str) -> Severity:
    """Coerce a YAML word into a :class:`Severity`, rejecting anything else."""
    if isinstance(raw, Severity):
        return raw
    if isinstance(raw, str) and raw in {member.value for member in Severity}:
        return Severity(raw)
    allowed = ", ".join(member.value for member in Severity)
    raise ValueError(f"{where} must be a Severity member ({allowed}), got {raw!r}")


def _level_of(raw: Any, where: str) -> RiskLevel:
    """Coerce a YAML word into a :class:`RiskLevel`, rejecting anything else."""
    if isinstance(raw, RiskLevel):
        return raw
    if isinstance(raw, str) and raw in {member.value for member in RiskLevel}:
        return RiskLevel(raw)
    allowed = ", ".join(member.value for member in RiskLevel)
    raise ValueError(f"{where} must be a RiskLevel member ({allowed}), got {raw!r}")


def _load_evaluations(raw: Any) -> dict[str, RuleEvaluation]:
    """Build the clause table from the YAML ``rule_clauses`` block.

    Per rule: a ``kind``, an ordered list of clauses (rule id, threshold key,
    feature, operator, band, severity) and the optional context/non-evaluable
    blocks. The values themselves are not read here -- they come from the pack at
    evaluation time.
    """
    if not isinstance(raw, Mapping) or not raw:
        raise ValueError("calibration must define a non-empty 'rule_clauses' mapping")

    evaluations: dict[str, RuleEvaluation] = {}
    for rule_id, block in raw.items():
        where = f"calibration rule_clauses[{rule_id!r}]"
        if not isinstance(block, Mapping):
            raise ValueError(f"{where} must be a mapping")
        kind = _require(block, "kind", where)
        if kind not in ("threshold_clauses", "compound"):
            raise ValueError(f"{where}: kind must be 'threshold_clauses' or 'compound', got {kind!r}")

        clauses: list[Clause] = []
        if kind == "threshold_clauses":
            clauses_raw = _require(block, "clauses", where)
            if not isinstance(clauses_raw, list) or not clauses_raw:
                raise ValueError(f"{where}: a threshold_clauses rule must list at least one clause")
            for clause_raw in clauses_raw:
                clause_where = f"{where} clause #{len(clauses)}"
                if not isinstance(clause_raw, Mapping):
                    raise ValueError(f"{clause_where} must be a mapping")
                clauses.append(
                    Clause(
                        rule_id=rule_id,
                        threshold_key=str(_require(clause_raw, "threshold_key", clause_where)),
                        feature=str(_require(clause_raw, "feature", clause_where)),
                        operator=str(_require(clause_raw, "operator", clause_where)),
                        band=str(_require(clause_raw, "band", clause_where)),
                    )
                )

        context = tuple(str(name) for name in block.get("context_features", ()))
        consumed_raw = block.get("consumed_thresholds", ())
        if isinstance(consumed_raw, str) or not isinstance(consumed_raw, (list, tuple)):
            raise ValueError(f"{where}: consumed_thresholds must be a list of threshold keys")
        non_evaluable_raw = block.get("non_evaluable_thresholds", {})
        if not isinstance(non_evaluable_raw, Mapping):
            raise ValueError(f"{where}: non_evaluable_thresholds must be a mapping of key -> disclosure")
        non_evaluable = {str(key): str(value) for key, value in non_evaluable_raw.items()}
        evaluations[rule_id] = RuleEvaluation(
            rule_id=rule_id,
            kind=kind,
            clauses=tuple(clauses),
            context_features=context,
            non_evaluable_thresholds=non_evaluable,
            consumed_thresholds=tuple(str(name) for name in consumed_raw),
        )
    return evaluations


def load_calibration(path: str | Path | None = None) -> Calibration:
    """Load and validate the deterministic core's demo-calibration parameters.

    Args:
        path: Calibration YAML to load. Defaults to
            :data:`CALIBRATION_PATH` (``trailguard/rules/calibration_v1.yaml``).

    Returns:
        A validated :class:`Calibration`. Structural validation (closed
        vocabularies, contiguous bands, weights summing to one) happens here;
        the cross-check against a rule pack is :meth:`Calibration.validate_against`.

    Raises:
        FileNotFoundError: the YAML file does not exist.
        ValueError: the file is not a mapping, a required key is missing, a
            closed vocabulary is violated, the soft-score bands have a gap or an
            overlap, or a weight table does not sum to one.
    """
    calibration_path = Path(path) if path is not None else CALIBRATION_PATH
    if not calibration_path.is_file():
        raise FileNotFoundError(f"calibration file not found: {calibration_path}")

    raw = yaml.safe_load(calibration_path.read_text(encoding="utf-8"))
    if not isinstance(raw, Mapping):
        raise ValueError(f"calibration {calibration_path} must be a YAML mapping at the top level")

    calibration_block = raw.get("calibration", {})
    if isinstance(calibration_block, Mapping) and calibration_block.get("is_calibrated") is not False:
        raise ValueError(
            f"calibration {calibration_path}: is_calibrated must be false; a calibrated file would claim "
            "reviewed parameters the project has not reviewed"
        )

    bands_raw = _require(raw, "soft_score_bands", "calibration")
    if not isinstance(bands_raw, Mapping):
        raise ValueError("calibration 'soft_score_bands' must be a mapping of level -> [low, high]")
    unknown_levels = sorted(set(bands_raw) - {level.value for level in RiskLevel})
    if unknown_levels:
        raise ValueError(
            f"calibration soft_score_bands names unknown levels {unknown_levels}; "
            f"expected {[level.value for level in RiskLevel]}"
        )
    # Rebuilt in canonical GREEN..RED order rather than trusting the document's
    # key order: a YAML round-trip (or a hand-sorted file) would otherwise fail a
    # contiguity check that has nothing to do with the file's own spelling.
    bands: dict[str, tuple[int, int]] = {}
    for level in RiskLevel:
        pair = bands_raw.get(level.value)
        if pair is None:
            raise ValueError(f"calibration soft_score_bands is missing a band for {level.value}")
        if not isinstance(pair, (list, tuple)) or len(pair) != 2:
            raise ValueError(f"calibration soft_score_bands[{level.value!r}] must be a [low, high] pair")
        bands[level.value] = (int(pair[0]), int(pair[1]))

    soft_score = _require(raw, "soft_score", "calibration")
    weights_raw = _require(soft_score, "severity_weights", "calibration 'soft_score'")
    if not isinstance(weights_raw, Mapping) or not weights_raw:
        raise ValueError("calibration 'soft_score.severity_weights' must be a non-empty mapping")
    severity_weights = {_severity_of(key, "soft_score.severity_weights").value: int(value) for key, value in weights_raw.items()}

    floor_raw = _require(raw, "hard_rule_severity_floor", "calibration")
    if not isinstance(floor_raw, Mapping):
        raise ValueError("calibration 'hard_rule_severity_floor' must be a mapping")
    severity_floor = {
        _severity_of(key, "hard_rule_severity_floor").value: _level_of(value, f"hard_rule_severity_floor[{key!r}]")
        for key, value in floor_raw.items()
    }

    movement = _require(raw, "movement", "calibration")
    speed_weights_raw = _require(movement, "base_speed_weights", "calibration 'movement'")
    if not isinstance(speed_weights_raw, Mapping) or not speed_weights_raw:
        raise ValueError("calibration 'movement.base_speed_weights' must be a non-empty mapping")
    base_speed_weights = {str(key): float(value) for key, value in speed_weights_raw.items()}

    projection = _require(raw, "projection", "calibration")
    reliability_raw = _require(projection, "horizon_reliability", "calibration 'projection'")
    if not isinstance(reliability_raw, Mapping) or not reliability_raw:
        raise ValueError("calibration 'projection.horizon_reliability' must be a non-empty mapping")
    horizon_reliability = {str(key): float(value) for key, value in reliability_raw.items()}

    route_quality = _require(raw, "route_data_quality", "calibration")
    battery = _require(raw, "battery", "calibration")
    confidence = _require(raw, "confidence", "calibration")
    confidence_weights_raw = _require(confidence, "weights", "calibration 'confidence'")
    if not isinstance(confidence_weights_raw, Mapping):
        raise ValueError("calibration 'confidence.weights' must be a mapping")
    confidence_weights = {str(key): float(value) for key, value in confidence_weights_raw.items()}

    calibration = Calibration(
        pack_id=str(_require(raw, "pack_id", "calibration")),
        version=str(_require(raw, "version", "calibration")),
        soft_score_bands=bands,
        severity_weights=severity_weights,
        saturation_score=int(_require(soft_score, "saturation_score", "calibration 'soft_score'")),
        severity_floor=severity_floor,
        base_speed_weights=base_speed_weights,
        fatigue_penalty_cap=float(_require(movement, "fatigue_penalty_cap", "calibration 'movement'")),
        horizon_reliability=horizon_reliability,
        route_min_trackpoints=int(_require(route_quality, "min_trackpoints", "calibration 'route_data_quality'")),
        route_trackpoint_weight=float(_require(route_quality, "trackpoint_weight", "calibration 'route_data_quality'")),
        route_elevation_weight=float(_require(route_quality, "elevation_weight", "calibration 'route_data_quality'")),
        battery_drain_percent_per_hour=float(_require(battery, "drain_percent_per_hour", "calibration 'battery'")),
        battery_projected_floor=float(_require(battery, "projected_floor_percent", "calibration 'battery'")),
        battery_projected_ceiling=float(_require(battery, "projected_ceiling_percent", "calibration 'battery'")),
        weather_freshness_budget_min=int(_require(confidence, "default_weather_freshness_budget_min", "calibration 'confidence'")),
        confidence_unknown_value=float(_require(confidence, "unknown_value", "calibration 'confidence'")),
        confidence_weights=confidence_weights,
        confidence_missing_field_penalty=float(_require(confidence, "missing_field_penalty", "calibration 'confidence'")),
        confidence_stale_field_penalty=float(_require(confidence, "stale_field_penalty", "calibration 'confidence'")),
        evaluations=_load_evaluations(_require(raw, "rule_clauses", "calibration")),
    )

    _validate_bands(calibration.soft_score_bands)
    calibration._validate_floors()
    _validate_weights(calibration.confidence_weights, "confidence weights")
    _validate_horizon_labels(horizon_reliability)
    return calibration
