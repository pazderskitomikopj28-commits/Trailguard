"""Deterministic rule pack contracts plus the real YAML loader (spec section 7).

This module owns the *data* side of the deterministic safety core:

* :class:`RuleDefinition` -- one rule's frozen metadata block (id, name,
  condition, thresholds, units, severity, rationale, source, source_type,
  version) plus the three mechanical extension keys the engine needs
  (``trigger_type``, ``applies_to_horizons``, ``calibration``).
* :class:`RulePack` -- the six-rule pack for the MVP.
* :func:`load_rule_pack` -- the one piece of real code here: it parses
  ``rules/rules_v1.yaml`` with ``yaml.safe_load``, validates every rule, and
  additionally checks the two places where a *value inside* ``thresholds`` has to
  agree with a closed vocabulary (``min_severity`` of R-F) so a mis-cased word
  fails loudly at load time instead of silently never matching.

Condition evaluation is deliberately NOT implemented here; that is Phase 1
work (see ``risk_core/engine.py``). The declarative condition language is
documented on :class:`RuleDefinition` so the engine and this loader agree on
the contract: a condition is a human-readable sentence whose *machine-readable*
part is a list of ``feature path + operator + value`` clauses over named
features (e.g. ``route.off_route_distance_m > 50``) stored in ``thresholds``.

Calibration policy (spec section 7, "不得把 demo 参数伪装成行业标准"): every
rule in the shipped pack is marked ``source_type:
competition_demo_calibration`` AND ``calibration: true``. A rule may only
escape that marking if it cites a real external reference (``external_reference``)
or is derived from one (``derived_rule``); :func:`load_rule_pack` enforces the
consistency of the pair so a mislabelled rule fails loudly instead of being
quietly presented as an industry safety standard.
"""

from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field

from ..schemas.risk import Severity
from ..schemas.route import Horizon

#: Rule ids the frozen MVP pack must contain, exactly once each, in order.
EXPECTED_RULE_IDS: tuple[str, ...] = ("R-A", "R-B", "R-C", "R-D", "R-E", "R-F")

#: Closed domain for ``source_type`` (spec section 7).
SourceType = Literal["external_reference", "competition_demo_calibration", "derived_rule"]

#: Whether a rule sets a severity floor (hard) or only contributes score (soft).
TriggerType = Literal["hard", "soft"]


class RuleDefinition(BaseModel):
    """One rule of the deterministic safety core, mirrored from the YAML.

    Fields mirror the frozen metadata block of spec section 7 plus the three
    extension keys used by the Phase 1 engine.

    Responsibility: carry a rule's identity, its declarative condition, its
    thresholds with units, its severity, and its provenance/calibration marking
    -- so thresholds can be re-tuned as data without touching engine or prompt.
    Not responsible for: being evaluated. Evaluation of ``condition`` against a
    feature vector is Phase 1 (``risk_core/engine.py``).

    Declarative condition DSL (documented for Phase 1, not implemented here):
    ``condition`` is the human-readable sentence shown to explainers and
    auditors; ``thresholds`` holds the machine-readable operands keyed by the
    feature path they apply to (``off_route_distance_m``,
    ``time_margin_min_warn``, ``pace_drop_percent_warn``, ...) and ``units``
    gives the physical unit of each operand. A clause is
    ``feature_path <operator> value``; ``value`` comes from ``thresholds``. The
    operator is implied by the feature's direction (exceeds / falls below /
    equals) and is stated in the ``condition`` sentence, keeping the YAML free
    of logic. Feature paths are dotted lookups over the values
    ``assess_risk`` receives (route state, weather state, device state, time
    state) plus derived features named in the sentence (for example
    ``time_margin_min = minutes_to_sunset - estimated_minutes_to_finish``).
    """

    model_config = ConfigDict(extra="forbid")

    id: str = Field(..., description="Stable rule identifier, e.g. 'R-A'.")
    name: str = Field(..., description="Short human-readable rule name.")
    condition: str = Field(
        ...,
        description=(
            "Declarative, human-readable condition: feature path + operator + value "
            "clauses over named features. Evaluated by the Phase 1 engine, never here."
        ),
    )
    thresholds: dict[str, Any] = Field(
        ..., description="Machine-readable operands keyed by feature path; values are calibration parameters."
    )
    units: dict[str, str] = Field(..., description="Physical unit of each threshold operand, e.g. 'meters'.")
    severity: Severity = Field(
        ...,
        description=(
            "Severity this rule contributes, in the shared Severity vocabulary "
            "(LOW/MEDIUM/HIGH/CRITICAL) that RiskDriver consumes."
        ),
    )
    rationale: str = Field(..., description="Why the rule exists and why these thresholds were chosen.")
    source: str = Field(..., description="Provenance of the thresholds; never presented as an industry standard unless external_reference.")
    source_type: SourceType = Field(
        ...,
        description="Provenance class: external_reference, competition_demo_calibration or derived_rule.",
    )
    version: str = Field(..., description="Rule version, e.g. '1.0.0'.")
    trigger_type: TriggerType = Field(
        ...,
        description="'hard' rules set a severity floor; 'soft' rules only contribute to the soft score.",
    )
    applies_to_horizons: list[Horizon] = Field(
        ..., description="Frozen analysis horizons this rule may be evaluated at (spec 6.1)."
    )
    calibration: bool = Field(
        ...,
        description="True when the thresholds are demo calibration values pending review (see source_type).",
    )


class RulePack(BaseModel):
    """The loaded rule pack: identity plus exactly the six frozen rules.

    Responsibility: bundle the pack id/version and its rules, and offer
    id-based lookup. Not responsible for: evaluating rules or producing risk;
    the Phase 1 engine consumes the pack and owns evaluation.
    """

    model_config = ConfigDict(extra="forbid")

    pack_id: str = Field(..., description="Pack identifier, e.g. 'trailguard_rules_v1'.")
    version: str = Field(..., description="Pack version string.")
    rules: list[RuleDefinition] = Field(..., description="Rule definitions, in canonical id order.")
    status: str = Field(default="calibration", description="Pack lifecycle status, e.g. 'calibration'.")
    description: str | None = Field(default=None, description="Human-readable pack description.")
    calibration: dict[str, Any] = Field(
        default={},
        description="Pack-level calibration block (is_calibrated / calibrated_for / review_status).",
    )

    def get(self, rule_id: str) -> RuleDefinition:
        """Return the rule with ``rule_id``.

        Raises:
            KeyError: no rule with that id exists in this pack.
        """
        for rule in self.rules:
            if rule.id == rule_id:
                return rule
        raise KeyError(f"unknown rule id {rule_id!r} in pack {self.pack_id!r}")

    def ids(self) -> list[str]:
        """Rule ids in canonical order."""
        return [rule.id for rule in self.rules]


def _validate_calibration_marking(rule: RuleDefinition) -> None:
    """Enforce the calibration-marking policy for one rule.

    A rule marked ``calibration: true`` must declare
    ``competition_demo_calibration`` (a real external reference or a derived
    rule is not a demo parameter); conversely a rule declaring
    ``competition_demo_calibration`` must set ``calibration: true``. Anything
    else is a provenance error that would let demo thresholds masquerade as
    industry safety standards (spec section 7).
    """
    if rule.calibration and rule.source_type != "competition_demo_calibration":
        raise ValueError(
            f"rule {rule.id}: calibration=true requires source_type=competition_demo_calibration, "
            f"got {rule.source_type!r}"
        )
    if rule.source_type == "competition_demo_calibration" and not rule.calibration:
        raise ValueError(
            f"rule {rule.id}: source_type=competition_demo_calibration requires calibration=true"
        )
    if rule.source_type == "external_reference" and not rule.source.strip():
        raise ValueError(f"rule {rule.id}: source_type=external_reference requires a non-empty source citation")
    if "min_severity" in rule.thresholds:
        _validate_severity_threshold(rule)


#: Threshold keys whose value must be a member of the ``Severity`` vocabulary.
#: R-F compares a triggered rule's severity against ``min_severity``, so a value
#: that is not a ``Severity`` member would never match any driver and would
#: silently disable the compound escalation.
_SEVERITY_THRESHOLD_KEYS: tuple[str, ...] = ("min_severity",)


def _validate_severity_threshold(rule: RuleDefinition) -> None:
    """Check that a severity-valued threshold is spelled as a ``Severity`` member.

    ``thresholds`` is a free-form ``dict[str, Any]``, so nothing in pydantic
    catches ``min_severity: medium``. The comparison the Phase 1 engine performs
    is by :attr:`~trailguard.schemas.risk.Severity.rank`, so only an exact
    uppercase member is usable. Responsibility: reject anything else at load
    time. Not responsible for: evaluating the comparison.
    """
    for key in _SEVERITY_THRESHOLD_KEYS:
        if key not in rule.thresholds:
            continue
        value = rule.thresholds[key]
        if not isinstance(value, str) or value not in {member.value for member in Severity}:
            allowed = ", ".join(member.value for member in Severity)
            raise ValueError(
                f"rule {rule.id}: threshold {key!r}={value!r} must be one of the Severity members "
                f"({allowed}); it is compared against RuleDriver.severity"
            )


#: Location of the frozen pack that ships with the package.
DEFAULT_PACK_PATH: Path = Path(__file__).resolve().parents[1] / "rules" / "rules_v1.yaml"


def load_rule_pack(path: str | Path | None = None) -> RulePack:
    """Load and validate a rule pack YAML file into a :class:`RulePack`.

    This is the only real code in this module: parse with ``yaml.safe_load``
    and validate every rule against :class:`RuleDefinition`. Condition
    evaluation is Phase 1.

    Args:
        path: Path to the rule pack YAML (e.g. ``trailguard/rules/rules_v1.yaml``).
            Defaults to the frozen pack that ships beside this module, so a
            caller wanting *the* pack does not have to reconstruct its location
            and cannot accidentally load a different one.

    Returns:
        The validated :class:`RulePack`.

    Raises:
        FileNotFoundError: the YAML file does not exist.
        ValueError: the file is not a mapping, a rule fails validation, the rule
            id set is not exactly :data:`EXPECTED_RULE_IDS` (missing, extra or
            duplicated ids), the pack lacks a ``rules`` list, or a rule's
            calibration marking is inconsistent with its ``source_type``, or a
            severity-valued threshold is not a ``Severity`` member.
    """
    pack_path = Path(path) if path is not None else DEFAULT_PACK_PATH
    if not pack_path.is_file():
        raise FileNotFoundError(f"rule pack file not found: {pack_path}")

    raw = yaml.safe_load(pack_path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError(f"rule pack {pack_path} must be a YAML mapping at the top level")

    rules_raw = raw.get("rules")
    if not isinstance(rules_raw, list) or not rules_raw:
        raise ValueError(f"rule pack {pack_path} must contain a non-empty 'rules' list")

    rules: list[RuleDefinition] = []
    for index, rule_raw in enumerate(rules_raw):
        if not isinstance(rule_raw, dict):
            raise ValueError(f"rule pack {pack_path}: rule #{index} is not a mapping")
        try:
            rule = RuleDefinition.model_validate(rule_raw)
        except Exception as exc:  # pydantic ValidationError -> actionable message
            rule_id = rule_raw.get("id", f"#{index}")
            raise ValueError(f"rule pack {pack_path}: rule {rule_id!r} failed validation: {exc}") from exc
        _validate_calibration_marking(rule)
        rules.append(rule)

    seen: set[str] = set()
    duplicates: list[str] = []
    for rule in rules:
        if rule.id in seen:
            duplicates.append(rule.id)
        seen.add(rule.id)
    if duplicates:
        raise ValueError(f"rule pack {pack_path}: duplicate rule ids {sorted(set(duplicates))}")

    missing = [rule_id for rule_id in EXPECTED_RULE_IDS if rule_id not in seen]
    extra = sorted(seen - set(EXPECTED_RULE_IDS))
    if missing or extra:
        raise ValueError(
            f"rule pack {pack_path}: rule ids must be exactly {list(EXPECTED_RULE_IDS)}; "
            f"missing={missing}, unexpected={extra}"
        )

    pack_payload = {
        "pack_id": raw.get("pack_id"),
        "version": raw.get("version"),
        "status": raw.get("status", "calibration"),
        "description": raw.get("description"),
        "calibration": raw.get("calibration", {}),
        "rules": rules,
    }
    try:
        return RulePack.model_validate(pack_payload)
    except Exception as exc:
        raise ValueError(f"rule pack {pack_path}: pack-level validation failed: {exc}") from exc


__all__ = [
    "DEFAULT_PACK_PATH",
    "EXPECTED_RULE_IDS",
    "RuleDefinition",
    "RulePack",
    "load_rule_pack",
]
