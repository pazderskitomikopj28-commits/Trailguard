"""Soft-score aggregation for the deterministic safety core (spec sections 7, 8).

Scoring is the *soft* half of the verdict: triggered soft rules (R-C future
weather, R-D pace degradation, R-E battery x remaining route) contribute to an
aggregate integer score in [0, 100], which then maps onto a risk level. Hard
rules (R-A, R-B) do not score -- they set a severity floor the score can never
undercut (see ``risk_core/engine.py``).

DEMO CALIBRATION BANDS -- replaceable, not a standard
----------------------------------------------------
The mapping from aggregate soft score to level used by the MVP is:

* ``GREEN``   -- score 0-24
* ``YELLOW``  -- score 25-49
* ``ORANGE``  -- score 50-74
* ``RED``     -- score 75-100

These bands are TrailGuard team demo-calibration values chosen for the DGX
Spark hackathon demonstration, kept here as data so they can be re-tuned
without touching engine or prompt text. They are NOT an industry safety
scale, not a published risk index and not derived from incident statistics;
``rules/rules_v1.yaml`` marks the whole pack ``calibration: true`` with
``review_status: pending``. The level words describe a *Projected Risk State*
under the stated conditional assumption (spec 6.2) -- never an accident
probability, and the banned identifier ``accident_probability`` is not used
anywhere in this package.
"""

from ..schemas.risk import RuleDriver

#: Demo-calibration score bands (see the module docstring). Replaceable data.
SOFT_SCORE_BANDS: dict[str, tuple[int, int]] = {
    "GREEN": (0, 24),
    "YELLOW": (25, 49),
    "ORANGE": (50, 74),
    "RED": (75, 100),
}


def aggregate_soft_score(drivers: list[RuleDriver]) -> int:
    """Aggregate triggered soft-rule drivers into an integer score in [0, 100].

    Responsibility: turn the list of triggered soft rules (rule id, severity,
    detail) into the single score the level-mapping step consumes, so that
    equal evidence always produces an equal score. Not responsible for:
    choosing the final level (hard-rule floors and R-F compound escalation
    happen in the engine), deciding which rules fired, or any explanation.

    Args:
        drivers: Triggered soft-rule drivers for one horizon.

    Returns:
        Aggregate score in [0, 100].

    Raises:
        NotImplementedError: always, in Phase 0 -- the weights, severity
            contributions and saturation/clamping rules are Phase 1 work.
    """
    raise NotImplementedError(
        "Phase 1: deterministic safety core soft-score aggregation over triggered soft-rule "
        "drivers (severity weights, per-rule caps and clamping are demo-calibration parameters)"
    )


__all__ = ["SOFT_SCORE_BANDS", "aggregate_soft_score"]
