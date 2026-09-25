"""Rule pack loader tests (spec section 7).

Responsibility: prove that ``rules_v1.yaml`` loads through
:func:`load_rule_pack`, holds exactly the six frozen rules R-A..R-F, that every
rule carries the frozen metadata block, that ``source_type`` stays inside its
closed domain, and that every numeric threshold is explicitly calibration-marked
-- so no demo parameter can masquerade as an industry safety standard.

Explicit non-responsibility: nothing here evaluates a rule against a feature
vector. Condition evaluation is Phase 1 (``risk_core/engine.py``); these tests
only assert that the *data* is complete, honest and loadable.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from conftest import (
    EXPECTED_RULE_IDS,
    FROZEN_RULE_METADATA_KEYS,
    RULES_PATH,
    SKILL_PATH,
    SOURCE_TYPE_DOMAIN,
)

from trailguard.risk_core import RuleDefinition, RulePack, load_rule_pack
from trailguard.schemas.risk import RuleDriver, Severity
from trailguard.schemas.route import Horizon

#: Band suffixes the pack appends to a base operand name. A threshold named
#: ``<base>_warn`` is interpreted in the unit declared for ``<base>``.
_BAND_SUFFIXES: tuple[str, ...] = ("_severe", "_critical", "_warn", "_high")


def _base_operand(threshold_key: str) -> str:
    """Return the unit-bearing base operand a threshold key refers to."""
    for suffix in _BAND_SUFFIXES:
        if threshold_key.endswith(suffix):
            return threshold_key[: -len(suffix)]
    return threshold_key


@pytest.fixture(scope="module")
def pack() -> RulePack:
    """The validated v1 rule pack."""
    return load_rule_pack(RULES_PATH)


@pytest.fixture(scope="module")
def raw_rules() -> list[dict]:
    """The raw YAML rule mappings, before any validation."""
    return yaml.safe_load(RULES_PATH.read_text(encoding="utf-8"))["rules"]


# --------------------------------------------------------------------------- #
# Loading
# --------------------------------------------------------------------------- #


def test_rule_pack_loads_from_yaml(pack: RulePack) -> None:
    """The YAML file parses into the validated pack contract."""
    assert pack.pack_id == "trailguard_rules_v1"
    assert pack.version == "1.0.0"
    assert pack.status == "calibration"
    assert isinstance(pack.rules, list)


def test_pack_level_calibration_block_is_honest(pack: RulePack) -> None:
    """The pack declares itself uncalibrated and pending review."""
    calibration = pack.calibration

    assert calibration["is_calibrated"] is False
    assert "competition demo" in calibration["calibrated_for"]
    assert calibration["review_status"] == "pending"


def test_missing_rule_pack_file_raises(tmp_path: Path) -> None:
    """A missing YAML file fails loudly instead of returning an empty pack."""
    with pytest.raises(FileNotFoundError):
        load_rule_pack(tmp_path / "does_not_exist.yaml")


# --------------------------------------------------------------------------- #
# Exactly six rules
# --------------------------------------------------------------------------- #


def test_pack_holds_exactly_the_six_frozen_rules(pack: RulePack) -> None:
    """``R-A`` .. ``R-F``, once each, in canonical order."""
    assert pack.ids() == EXPECTED_RULE_IDS
    assert len(pack.rules) == 6
    assert len({rule.id for rule in pack.rules}) == 6


def test_loader_rejects_a_wrong_rule_id_set(tmp_path: Path) -> None:
    """A pack that is missing a frozen rule id is a ValueError, not a warning."""
    payload = yaml.safe_load(RULES_PATH.read_text(encoding="utf-8"))
    payload["rules"] = [rule for rule in payload["rules"] if rule["id"] != "R-F"]
    broken = tmp_path / "broken_rules.yaml"
    broken.write_text(yaml.safe_dump(payload), encoding="utf-8")

    with pytest.raises(ValueError) as excinfo:
        load_rule_pack(broken)

    assert "R-F" in str(excinfo.value)


def test_loader_rejects_duplicate_rule_ids(tmp_path: Path) -> None:
    """Duplicated rule ids are refused, so a threshold cannot be applied twice."""
    payload = yaml.safe_load(RULES_PATH.read_text(encoding="utf-8"))
    payload["rules"].append(dict(payload["rules"][0]))
    broken = tmp_path / "duplicated_rules.yaml"
    broken.write_text(yaml.safe_dump(payload), encoding="utf-8")

    with pytest.raises(ValueError) as excinfo:
        load_rule_pack(broken)

    assert "duplicate" in str(excinfo.value).lower()


def test_rule_pack_get_returns_the_rule_or_raises(pack: RulePack) -> None:
    """``RulePack.get`` is the sanctioned id lookup."""
    rule = pack.get("R-B")

    assert isinstance(rule, RuleDefinition)
    assert rule.name == "Sunset Margin"

    with pytest.raises(KeyError):
        pack.get("R-Z")


# --------------------------------------------------------------------------- #
# Frozen metadata block
# --------------------------------------------------------------------------- #


def test_every_rule_carries_the_frozen_metadata_keys(raw_rules: list[dict]) -> None:
    """Each YAML rule has id/name/condition/thresholds/units/severity/rationale/source/source_type/version."""
    for rule in raw_rules:
        missing = [key for key in FROZEN_RULE_METADATA_KEYS if key not in rule]
        assert missing == [], f"rule {rule.get('id')!r} is missing {missing}"


def test_every_rule_carries_the_engine_extension_keys(raw_rules: list[dict]) -> None:
    """trigger_type / applies_to_horizons / calibration are present and non-empty."""
    for rule in raw_rules:
        assert rule["trigger_type"] in {"hard", "soft"}
        assert rule["applies_to_horizons"], "a rule must declare the horizons it applies to"
        assert isinstance(rule["calibration"], bool)


def test_every_rule_has_non_empty_text_fields(pack: RulePack) -> None:
    """Identity and provenance fields are not blank strings."""
    for rule in pack.rules:
        assert rule.id and rule.name and rule.condition
        assert rule.rationale.strip()
        assert rule.source.strip()
        assert rule.version


def test_thresholds_and_units_cover_each_other(pack: RulePack) -> None:
    """A numeric threshold without a unit would be uninterpretable in an explanation.

    The frozen pack declares the unit of each *operand* (``wind_gust_kmh``,
    ``time_margin_min``) and reuses it for the band variants of the same operand
    (``wind_gust_kmh_severe``, ``time_margin_min_warn``), so a threshold key is
    covered either by its own unit or by its base operand's unit.
    """
    for rule in pack.rules:
        assert rule.thresholds, f"{rule.id} declares no thresholds"
        assert rule.units, f"{rule.id} declares no units"
        unlabelled = [
            key
            for key, value in rule.thresholds.items()
            if isinstance(value, (int, float))
            and not isinstance(value, bool)
            and key not in rule.units
            and _base_operand(key) not in rule.units
        ]
        assert unlabelled == [], f"{rule.id} has unlabelled thresholds: {unlabelled}"


# --------------------------------------------------------------------------- #
# source_type domain and calibration marking
# --------------------------------------------------------------------------- #


def test_source_type_stays_inside_the_closed_domain(pack: RulePack) -> None:
    """``source_type`` is one of the three frozen provenance classes."""
    for rule in pack.rules:
        assert rule.source_type in SOURCE_TYPE_DOMAIN


def test_every_numeric_threshold_is_calibration_marked(pack: RulePack) -> None:
    """A rule with numeric thresholds must be marked demo calibration (spec 7)."""
    for rule in pack.rules:
        numeric = [
            value
            for value in rule.thresholds.values()
            if isinstance(value, (int, float)) and not isinstance(value, bool)
        ]
        if not numeric:
            continue
        assert rule.source_type == "competition_demo_calibration", (
            f"{rule.id} has numeric thresholds but source_type={rule.source_type!r}; "
            "only an external reference may escape the calibration marking"
        )
        assert rule.calibration is True, f"{rule.id} has numeric thresholds without calibration: true"


def test_every_rule_source_disclaims_industry_standard(pack: RulePack) -> None:
    """The source string never claims to be an industry safety standard."""
    for rule in pack.rules:
        assert "not an industry standard" in rule.source.lower()


def test_every_rationale_marks_the_threshold_as_demo_placeholder(pack: RulePack) -> None:
    """Rationales state that the numbers are conservative demo placeholders."""
    for rule in pack.rules:
        rationale = rule.rationale.lower()
        assert "demo placeholder" in rationale
        assert "pending review" in rationale


def test_rule_trigger_types_match_the_frozen_split(pack: RulePack) -> None:
    """R-A and R-B are hard (severity floor); R-C..R-F are soft (score only)."""
    trigger_types = {rule.id: rule.trigger_type for rule in pack.rules}

    assert trigger_types == {
        "R-A": "hard",
        "R-B": "hard",
        "R-C": "soft",
        "R-D": "soft",
        "R-E": "soft",
        "R-F": "soft",
    }


def test_every_rule_applies_to_all_frozen_horizons(pack: RulePack) -> None:
    """The horizon set is frozen: all four, for all six rules (spec 6.1)."""
    expected = Horizon.all_horizons()
    for rule in pack.rules:
        assert rule.applies_to_horizons == expected


# --------------------------------------------------------------------------- #
# Rule-specific expectations
# --------------------------------------------------------------------------- #


def test_route_deviation_thresholds(pack: RulePack) -> None:
    """R-A: 50 m warn / 150 m severe / 3 observations of persistence."""
    thresholds = pack.get("R-A").thresholds

    assert thresholds["off_route_distance_m"] == 50
    assert thresholds["off_route_distance_m_severe"] == 150
    assert thresholds["persistence_observations"] == 3
    assert pack.get("R-A").units["off_route_distance_m"] == "meters"


def test_sunset_margin_condition_is_declared_as_a_margin(pack: RulePack) -> None:
    """R-B: the time margin is ``minutes_to_sunset - estimated_minutes_to_finish``."""
    rule = pack.get("R-B")

    assert "time_margin_min" in rule.condition
    assert "minutes_to_sunset" in rule.condition
    assert "estimated_minutes_to_finish" in rule.condition
    assert rule.thresholds["time_margin_min_warn"] == 30
    assert rule.thresholds["time_margin_min_critical"] == 0


def test_future_weather_rule_targets_the_projected_point(pack: RulePack) -> None:
    """R-C: hazards are evaluated at the projected position x time, not now."""
    rule = pack.get("R-C")
    condition = rule.condition.lower()

    assert "projected" in condition
    assert "not current-location weather" in condition
    assert rule.thresholds["wind_gust_kmh"] == 45
    assert rule.thresholds["wind_gust_kmh_severe"] == 65
    assert rule.thresholds["precipitation_probability"] == pytest.approx(0.7)
    assert rule.thresholds["temperature_c_low"] == -5


def test_pace_degradation_rule_is_trend_based_not_medical(pack: RulePack) -> None:
    """R-D compares 10-minute and 30-minute windows, with no medical thresholds."""
    rule = pack.get("R-D")

    assert "avg_speed_10m" in rule.condition
    assert "avg_speed_30m" in rule.condition
    assert "no medical" in rule.condition.lower()
    assert rule.thresholds["pace_drop_percent_warn"] == 15
    assert rule.thresholds["pace_drop_percent_high"] == 25


def test_battery_rule_combines_projection_and_network(pack: RulePack) -> None:
    """R-E is a projected battery x remaining route test, not a bare threshold."""
    rule = pack.get("R-E")
    condition = rule.condition.lower()

    assert "network availability" in condition
    assert "not a bare battery threshold" in condition
    assert rule.thresholds["projected_battery_percent_warn"] == 20
    assert rule.thresholds["projected_battery_percent_critical"] == 10


def test_compound_rule_states_the_combination_is_configurable(pack: RulePack) -> None:
    """R-F: the escalation combination is configured in the pack, not the prompt."""
    rule = pack.get("R-F")
    rationale = rule.rationale.lower()

    assert rule.thresholds["min_independent_soft_rules"] == 2
    assert rule.thresholds["min_severity"] == "MEDIUM"
    assert rule.thresholds["escalation_levels"] == 1

    # The threshold value must be a Severity member: R-F compares it against
    # RuleDriver.severity, so a lowercase spelling would never match any driver.
    assert rule.thresholds["min_severity"] == Severity.MEDIUM.value
    assert rule.severity is Severity.MEDIUM

    assert "configur" in rationale, "R-F must say the combination is configurable"
    assert "prompt" in rationale, "R-F must say it is not hardcoded in the LLM prompt"
    assert "never hardcoded in the llm prompt" in rationale


def test_compound_rule_is_not_hardcoded_in_the_skill_prompt() -> None:
    """SKILL.md does not restate the escalation thresholds: the pack owns them."""
    skill_text = SKILL_PATH.read_text(encoding="utf-8").lower()

    assert "two or more" not in skill_text
    assert "escalation_levels" not in skill_text
    assert "min_independent_soft_rules" not in skill_text
