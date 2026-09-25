"""Explicit ``RiskLevel`` domain tests (spec sections 7, 8, 6.2).

Responsibility: pin the four-level safety scale -- that an illegal level is
rejected rather than silently coerced, that a ``RiskResult`` accepts a valid
level verbatim, and that the ordering GREEN < YELLOW < ORANGE < RED is the one
the compound-escalation rule R-F relies on.

Explicit non-responsibility: these tests do not decide which level a scenario
deserves (that is the deterministic core, Phase 1) and they never express a level
as a probability of harm -- the domain concept is a *Projected Risk State*
(spec 6.2).
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from conftest import make_risk_result_dict

from trailguard.schemas import RiskLevel, RiskResult


# --------------------------------------------------------------------------- #
# Illegal levels are rejected
# --------------------------------------------------------------------------- #


def test_illegal_risk_level_is_rejected() -> None:
    """``RiskLevel("PURPLE")`` raises: the scale has exactly four members."""
    with pytest.raises(ValueError):
        RiskLevel("PURPLE")


def test_risk_result_rejects_illegal_risk_level() -> None:
    """A verdict cannot be built with a level outside the frozen scale."""
    with pytest.raises(ValidationError):
        RiskResult.model_validate(make_risk_result_dict(risk_level="PURPLE"))


@pytest.mark.parametrize("illegal", ["red", "Red", "", "GREENISH", "5"])
def test_case_and_case_variants_are_not_levels(illegal: str) -> None:
    """Level values are exact, upper-case wire strings."""
    assert illegal not in {level.value for level in RiskLevel}


# --------------------------------------------------------------------------- #
# Valid levels are accepted verbatim
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("level", ["GREEN", "YELLOW", "ORANGE", "RED"])
def test_risk_result_accepts_every_legal_level(level: str) -> None:
    """A plain dict with any legal level builds a complete verdict."""
    risk = RiskResult.model_validate(make_risk_result_dict(risk_level=level))

    assert risk.risk_level is RiskLevel(level)
    assert risk.risk_level.value == level


def test_risk_result_accepts_red_level() -> None:
    """The highest level is a first-class value, not a special case."""
    risk = RiskResult.model_validate(
        make_risk_result_dict(
            risk_level="RED",
            risk_score=88,
            hard_rules_triggered=["R-B"],
            confidence=0.6,
        )
    )

    assert risk.risk_level is RiskLevel.RED
    assert risk.risk_score == 88
    assert risk.hard_rules_triggered == ["R-B"]
    assert risk.safety_margins.time_margin_min is not None


def test_risk_level_enum_values_are_stable() -> None:
    """The wire values are the frozen four words, in severity order."""
    assert [level.value for level in RiskLevel] == ["GREEN", "YELLOW", "ORANGE", "RED"]


# --------------------------------------------------------------------------- #
# Ordering used by the compound-escalation rule R-F
# --------------------------------------------------------------------------- #


def test_rank_ordering_is_strictly_increasing() -> None:
    """GREEN < YELLOW < ORANGE < RED, as the escalation arithmetic needs."""
    assert RiskLevel.GREEN.rank < RiskLevel.YELLOW.rank < RiskLevel.ORANGE.rank < RiskLevel.RED.rank


def test_rank_values_are_zero_based() -> None:
    """Green is 0 and red is 3, so escalation is an integer offset."""
    assert RiskLevel.GREEN.rank == 0
    assert RiskLevel.YELLOW.rank == 1
    assert RiskLevel.ORANGE.rank == 2
    assert RiskLevel.RED.rank == 3


def test_levels_class_variable_is_ordered_low_to_high() -> None:
    """``RiskLevel.LEVELS`` is the frozen, ordered scale."""
    assert RiskLevel.LEVELS == [
        RiskLevel.GREEN,
        RiskLevel.YELLOW,
        RiskLevel.ORANGE,
        RiskLevel.RED,
    ]


def test_rank_matches_position_in_levels() -> None:
    """``rank`` is the index in ``LEVELS`` for every member."""
    for index, level in enumerate(RiskLevel.LEVELS):
        assert level.rank == index
