"""Soft score and level mapping (spec sections 7, 8, 10.5, 12).

The soft half of a verdict: every rule that triggered contributes the weight of
its severity, the total saturates, and the saturating total maps onto a level
through a band table. Both halves are demo-calibration, not an industry safety
scale: the weights and the bands are TrailGuard team parameters
(``source_type: competition_demo_calibration``, ``calibration: true``, review
status pending) and they are replaceable data -- re-tuned by editing
``trailguard/rules/calibration_v1.yaml`` rather than this module, which is why
:data:`SOFT_SCORE_BANDS` is loaded from that file at import time instead of being
restated as a literal here.

What the score is and is not
------------------------------
The score is an aggregate of *how many rules fired and how loudly*, which is a
bookkeeping convenience for a human reader. It is not a probability of harm, and
the framework never gives it one: a 63 is "a high-severity soft rule and a
medium one fired", not "63 % chance of anything". The domain concept is a
Projected Risk State (spec 6.2); the banned identifier
``accident_probability`` is not used as a name anywhere in this package -- it
appears here only to disclaim it.

A soft score is also not the final level. Three things decide the level, in this
order: the band the soft score lands in, the compound escalation of rule R-F, and
the floor set by any hard rule that triggered. A hard rule can only raise the
level, never lower it (spec 10.5), and the compound rule is the one rule that
contributes no points of its own -- it consumes the score the others produced and
moves the level by its configured number of levels, because counting a combination
rule in the very total it escalates would make its own escalation invisible.
"""

from __future__ import annotations

from typing import Iterable, Mapping

from ..schemas.risk import RiskLevel, Severity
from .calibration import CALIBRATION_PATH, load_calibration

__all__ = [
    "SOFT_SCORE_BANDS",
    "aggregate_soft_score",
    "level_for_score",
    "score_band",
]


def _load_bands() -> dict[str, tuple[int, int]]:
    """Read the band table from the calibration file, in GREEN..RED order.

    Loaded rather than declared so the table has one home. The module is imported
    by the contract layer's consumers without the ``route`` extra installed, and
    the calibration file needs nothing but ``yaml``, so this import is safe in the
    base install (``tests/test_packaging.py`` polices exactly that).
    """
    return dict(load_calibration(CALIBRATION_PATH).soft_score_bands)


#: Score bands as loaded from ``calibration_v1.yaml``. An ordered mapping of
#: level name -> ``(low, high)``: contiguous, covering [0, 100], with no gap and no
#: overlap, so every integer score lands in exactly one band.
SOFT_SCORE_BANDS: dict[str, tuple[int, int]] = _load_bands()


def _default_weights() -> dict[str, int]:
    """Severity weights from the same calibration file as the bands."""
    return dict(load_calibration(CALIBRATION_PATH).severity_weights)


def _default_saturation() -> int:
    """Saturation ceiling from the same calibration file as the bands."""
    return load_calibration(CALIBRATION_PATH).saturation_score


def aggregate_soft_score(
    drivers: Iterable[object],
    *,
    weights: Mapping[str, int] | None = None,
    saturation: int | None = None,
    exempt_rule_ids: Iterable[str] = (),
) -> int:
    """Sum the severity weights of the triggered rules, saturating at ``saturation``.

    Args:
        drivers: Triggered rules as :class:`~trailguard.schemas.risk.RuleDriver`
            objects (anything carrying ``rule_id`` and ``severity``).
        weights: Severity -> score weight. Defaults to the calibration file's
            ``soft_score.severity_weights``.
        saturation: Ceiling the total is clipped to. Defaults to the calibration
            file's ``soft_score.saturation_score``.
        exempt_rule_ids: Rules that fire but contribute nothing -- the compound
            rule R-F, which escalates the level instead. Supplied by the engine
            from the calibration's clause table (a ``compound`` rule is exempt by
            construction), so the exemption is data rather than a rule id spelled
            out in this function.

    Returns:
        The aggregate soft score in ``[0, saturation]``.

    Raises:
        ValueError: a driver names a severity with no configured weight. A
            misspelled severity must fail here rather than silently contributing
            zero, which would make a rule vanish from the score.
    """
    resolved_weights = weights if weights is not None else _default_weights()
    ceiling = saturation if saturation is not None else _default_saturation()
    exempt = set(exempt_rule_ids)

    total = 0
    for driver in drivers:
        rule_id = getattr(driver, "rule_id", None)
        if rule_id in exempt:
            continue
        severity = getattr(driver, "severity", None)
        key = severity.value if isinstance(severity, Severity) else str(severity)
        if key not in resolved_weights:
            raise ValueError(
                f"driver {rule_id!r} carries severity {key!r}, for which the calibration declares no "
                f"soft-score weight; expected one of {sorted(resolved_weights)}"
            )
        total += int(resolved_weights[key])

    if ceiling < 0:
        raise ValueError(f"saturation must not be negative: {ceiling}")
    return max(0, min(total, int(ceiling)))


def level_for_score(
    score: float,
    *,
    bands: Mapping[str, tuple[int, int]] | None = None,
) -> RiskLevel:
    """The level whose band contains ``score``.

    A score outside ``[0, 100]`` is clamped onto the outermost band rather than
    rejected: a caller that saturated at 100 and then escalated still needs a
    level, and the scale's ends are well defined. A score that is not a number at
    all raises, because "which level is NaN?" has no honest answer.
    """
    resolved = bands if bands is not None else SOFT_SCORE_BANDS
    number = float(score)
    if number != number:  # NaN
        raise ValueError(f"a soft score must be a number, got {score!r}")
    for level, (low, high) in resolved.items():
        if low <= number <= high:
            return RiskLevel(level)
    floors = [band[0] for band in resolved.values()]
    return RiskLevel(list(resolved)[0]) if number < min(floors) else RiskLevel(list(resolved)[-1])


def score_band(
    score: float,
    *,
    bands: Mapping[str, tuple[int, int]] | None = None,
) -> str:
    """Name of the band ``score`` lands in -- same lookup, spelled out for a reader."""
    return level_for_score(score, bands=bands).value
