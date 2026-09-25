"""Deterministic safety core boundary tests (spec sections 7, 8, 5.5).

Responsibility: pin the three things the rest of the system leans on, without
implementing any risk logic:

1. the rule pack loads from YAML into ``RuleDefinition`` objects whose severity is
   expressed in the *same* :class:`Severity` vocabulary a :class:`RuleDriver`
   consumes -- so a triggered rule can be explained as a driver with no
   translation step, and a mis-cased severity fails at load time;
2. the engine factory respects the class it is called on: ``RiskEngine`` yields the
   honest Phase 0 placeholder, a concrete subclass yields itself;
3. the demo-calibration score bands are marked as calibration, cover the whole
   [0, 100] range contiguously, and never speak of probability.

Explicit non-responsibility: these tests do not evaluate a rule, do not compute a
score and do not choose a level -- feature extraction, hard-rule floors, soft
scoring, level mapping and R-F escalation are Phase 1 work (spec 25), and every
one of those entry points is asserted to raise ``NotImplementedError`` naming its
phase rather than to return a value.
"""

from __future__ import annotations

import pytest
import yaml

from conftest import RULES_PATH, make_route_projection_dict

from trailguard.risk_core.engine import RiskEngine, _PHASE1_MESSAGE
from trailguard.risk_core.hard_rules import RuleDefinition, RulePack, load_rule_pack
from trailguard.risk_core import scoring
from trailguard.risk_core.scoring import SOFT_SCORE_BANDS, aggregate_soft_score
from trailguard.schemas.route import RouteProjection
from trailguard.schemas import RiskLevel, RuleDriver, Severity


def _normalised_prose(module: object) -> str:
    """Lower-cased docstring with line breaks collapsed, for prose assertions.

    A module docstring is wrapped for readability, so an English phrase can span a
    line break; matching on the raw text would make an honest docstring look like a
    missing one.
    """
    return " ".join((module.__doc__ or "").split()).lower()


@pytest.fixture
def rule_pack() -> RulePack:
    """The frozen six-rule pack, loaded from ``rules/rules_v1.yaml``."""
    return load_rule_pack(RULES_PATH)

# --------------------------------------------------------------------------- #
# The loaded pack's severities are RuleDriver severities (spec 7)
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("rule_id", ["R-A", "R-B", "R-C", "R-D", "R-E", "R-F"])
def test_every_rule_severity_builds_a_rule_driver(rule_id: str, rule_pack: RulePack) -> None:
    """A triggered rule can be reported as a driver with its own severity verbatim.

    This is what keeps the explanation honest: the driver a run reports carries
    the severity the *rule pack* declared, not a level the explainer invented, so
    the pack can be re-tuned without touching explanation code.
    """
    rule = rule_pack.get(rule_id)

    driver = RuleDriver(rule_id=rule.id, type=rule.trigger_type, severity=rule.severity, detail=rule.name)

    assert driver.rule_id == rule_id
    assert driver.severity is rule.severity
    assert driver.type in {"hard", "soft"}
    assert driver.detail == rule.name


def test_the_severity_vocabulary_is_shared(rule_pack: RulePack) -> None:
    """Rules and drivers name their severities with the same four words."""
    rule_severities = {rule.severity.value for rule in rule_pack.rules}

    assert rule_severities <= {severity.value for severity in Severity}
    assert rule_severities, "a pack without severities could not be explained"


def test_a_mis_cased_severity_fails_at_load_time(tmp_path) -> None:
    """``min_severity: medium`` must not load: the closed check is case-sensitive."""
    original = load_rule_pack(RULES_PATH)
    payload = original.model_dump(mode="json")
    for rule in payload["rules"]:
        if rule["id"] == "R-F":
            rule["thresholds"]["min_severity"] = "medium"
    mis_cased = tmp_path / "rules_v1.yaml"
    mis_cased.write_text(yaml.safe_dump(payload), encoding="utf-8")

    with pytest.raises(ValueError) as excinfo:
        load_rule_pack(mis_cased)

    message = str(excinfo.value)
    assert "R-F" in message and "min_severity" in message and "medium" in message
    assert "Severity" in message, "the message must name the closed vocabulary it violated"


def test_a_hard_rule_and_a_soft_rule_are_distinguishable(rule_pack: RulePack) -> None:
    """R-A/R-B set floors; R-C/R-D/R-F-escalated soft rules contribute score."""
    trigger_types = {rule.id: rule.trigger_type for rule in rule_pack.rules}

    assert trigger_types["R-A"] == "hard"
    assert trigger_types["R-B"] == "hard"
    assert trigger_types["R-C"] == "soft"
    assert trigger_types["R-D"] == "soft"


# --------------------------------------------------------------------------- #
# The engine factory honours its own class (spec 5.5)
# --------------------------------------------------------------------------- #


def test_the_base_factory_returns_the_phase_zero_placeholder(rule_pack: RulePack) -> None:
    """``RiskEngine.from_rule_pack`` cannot instantiate an abstract method set."""
    engine = RiskEngine.from_rule_pack(rule_pack)

    assert isinstance(engine, RiskEngine)
    assert engine.pack is rule_pack, "the placeholder must carry the loaded pack"


def test_a_concrete_subclass_factory_returns_that_subclass(rule_pack: RulePack) -> None:
    """The factory must not hardcode the placeholder, or Phase 1 could never land."""
    class _Phase1Engine(RiskEngine):
        """Minimal concrete engine used only to prove the factory honours ``cls``."""

        def assess_risk(self, route_state, weather_state, user_device_state, time_state, projected_state=None):  # type: ignore[override]
            raise AssertionError("not reached: only construction is under test")

    engine = _Phase1Engine.from_rule_pack(rule_pack)

    assert type(engine) is _Phase1Engine
    assert engine.pack is rule_pack


def test_the_placeholder_refuses_to_assess_and_names_its_phase(rule_pack: RulePack) -> None:
    """No Phase 0 code path may return a risk verdict."""
    engine = RiskEngine.from_rule_pack(rule_pack)

    with pytest.raises(NotImplementedError) as excinfo:
        engine.assess_risk(
            route_state=...,
            weather_state=...,
            user_device_state=...,
            time_state=...,
        )

    assert "Phase 1" in str(excinfo.value)
    assert _PHASE1_MESSAGE in str(excinfo.value)


def test_the_phase_one_message_stops_before_confidence() -> None:
    """Confidence is Phase 2 (``risk_core/confidence.py``); Phase 1 owns steps 1-5.

    A message that promised the confidence model here would tell a Phase 1
    implementer to write Phase 2 code, so the wording is asserted rather than
    trusted.
    """
    assert "compound escalation" in _PHASE1_MESSAGE
    assert "confidence" not in _PHASE1_MESSAGE.lower()
    assert _PHASE1_MESSAGE.startswith("Phase 1:")


def test_assess_risk_accepts_a_projection_for_future_horizons(rule_pack: RulePack) -> None:
    """The signature carries ``projected_state``, so spec 8's projection margins
    can reach the core at +30/+60/+120 without a second method."""
    parameters = RiskEngine.assess_risk.__code__.co_varnames[
        : RiskEngine.assess_risk.__code__.co_argcount
    ]

    assert "projected_state" in parameters
    assert "route_state" in parameters and "time_state" in parameters
    assert "projection" not in str(parameters).replace("projected_state", "")


def test_the_projection_argument_is_optional(rule_pack: RulePack) -> None:
    """NOW has no projection, and the core must accept that (spec 6.2)."""
    projection = RouteProjection.model_validate(
        make_route_projection_dict()
    )

    engine = RiskEngine.from_rule_pack(rule_pack)

    for projected_state in (None, projection):
        with pytest.raises(NotImplementedError):
            engine.assess_risk(
                route_state=...,
                weather_state=...,
                user_device_state=...,
                time_state=...,
                projected_state=projected_state,
            )


# --------------------------------------------------------------------------- #
# The demo-calibration score bands (spec 7, 8, 12)
# --------------------------------------------------------------------------- #


def test_the_score_bands_are_marked_as_demo_calibration() -> None:
    """A band dict is a threshold table and carries the same honesty duty as a rule.

    The bands cannot carry a YAML ``source_type`` key -- they are a Python
    constant -- so the marking lives in the module docstring, and it must say all
    three things: that the numbers are TrailGuard demo calibration, that they are
    not an industry safety scale, and that they agree with the pack's
    ``calibration: true`` marking.
    """
    docstring = _normalised_prose(scoring)

    assert "demo-calibration" in docstring, "the bands must be labelled TrailGuard demo calibration"
    assert "not an industry safety scale" in docstring, "the same honesty duty a rule carries"
    assert "calibration: true" in docstring, "and they must agree with the pack's marking"
    assert "re-tuned" in docstring, "they are replaceable data, not a fixed standard"


def test_the_scoring_module_carries_the_source_type_vocabulary() -> None:
    """A reader of the constants finds the same provenance words as in the YAML."""
    docstring = _normalised_prose(scoring)

    assert "competition_demo_calibration" in docstring or "demo-calibration" in docstring
    assert "review" in docstring, "the pack's review_status: pending must be reflected here"


def test_the_bands_cover_the_whole_score_range_without_gaps() -> None:
    """Every integer score in [0, 100] maps onto exactly one level."""
    covered: list[int] = []
    for low, high in SOFT_SCORE_BANDS.values():
        assert low <= high, f"inverted band ({low}, {high})"
        covered.extend(range(low, high + 1))

    assert sorted(covered) == list(range(0, 101))


def test_the_bands_are_ordered_like_the_risk_scale() -> None:
    """GREEN < YELLOW < ORANGE < RED in the band table, as in :class:`RiskLevel`."""
    floors = [band[0] for band in SOFT_SCORE_BANDS.values()]

    assert floors == sorted(floors)
    assert list(SOFT_SCORE_BANDS) == [level.value for level in RiskLevel]


def test_the_band_names_are_frozen_level_words() -> None:
    """A band key that is not a RiskLevel value could never be reported."""
    for name in SOFT_SCORE_BANDS:
        assert name in {level.value for level in RiskLevel}


def test_the_bands_never_speak_of_probability() -> None:
    """The banned identifier and the whole probability framing stay out."""
    docstring = scoring.__doc__ or ""

    # The banned identifier may be *named* only to disclaim it, as it is in
    # ``evals/metrics.py``; what must never happen is its use.
    for line in docstring.splitlines():
        if "accident_probability" in line:
            assert "not used" in line.lower(), f"the identifier is allowed only in a disclaimer: {line!r}"
    assert "projected risk state" in _normalised_prose(scoring), (
        "the domain concept is a Projected Risk State, not a chance of harm"
    )


def test_soft_score_aggregation_defers_to_phase_one() -> None:
    """Weights and saturation are Phase 1 arithmetic, not a Phase 0 guess."""
    from trailguard.schemas.risk import RuleDriver

    drivers = [RuleDriver(rule_id="R-D", type="soft", severity=Severity.MEDIUM)]

    with pytest.raises(NotImplementedError) as excinfo:
        aggregate_soft_score(drivers)

    assert "Phase 1" in str(excinfo.value)
    assert "demo-calibration" in str(excinfo.value)


def test_the_pack_is_data_not_code(rule_pack: RulePack) -> None:
    """Re-tuning a threshold edits YAML; the engine source stays untouched."""
    re_tuned = RulePack.model_validate(rule_pack.model_dump())

    assert re_tuned.rules[0].thresholds == rule_pack.rules[0].thresholds
    assert isinstance(rule_pack.rules[0], RuleDefinition)
