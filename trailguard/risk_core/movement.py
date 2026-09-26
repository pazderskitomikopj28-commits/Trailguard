"""The frozen movement model and its division-safe arithmetic (spec 6.3, 6.4).

Two things live here, both of them pure functions of their arguments:

* :class:`MovementModel` -- the frozen default form of spec 6.3,
  ``base_speed = 0.5*avg_speed_10m + 0.3*avg_speed_30m + 0.2*current_speed`` and
  ``effective_speed = base_speed * (1 - fatigue_penalty)``. The weights and the
  cap on the fatigue penalty are read from ``calibration_v1.yaml``, so re-tuning
  is a data edit and no coefficient is buried in a conditional.
* the ETA / pace-drop arithmetic -- "how many minutes from here", deliberately
  the simplest honest answer: distance divided by speed, with every way that
  division can degenerate handled by *returning None* rather than zero, infinity
  or NaN (spec 6.4).

What this module is not: not a pace model and not a physiological one. There is
no gradient-aware correction, no Naismith adjustment, no personalisation and no
learned component, and none of the coefficients are claimed to come from real
accident or injury models -- they are the spec's frozen MVP form, marked
``competition_demo_calibration``.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Mapping

__all__ = [
    "MovementModel",
    "derive_pace_drop_percent",
    "estimate_minutes",
    "minutes_between",
]


def _finite(value: float) -> float:
    """Replace a non-finite value with ``0.0``.

    A NaN speed must never reach a comparison: ``nan < 30`` is ``False``, which
    would silently disable a rule instead of disclosing that its input was
    unusable. Zero is the honest stand-in here because "no usable speed" is
    exactly what a non-finite input means, and the ETA functions below turn zero
    speed into an explicit "not computable".
    """
    number = float(value)
    if math.isnan(number) or math.isinf(number):
        return 0.0
    return number


def estimate_minutes(distance_km: float, speed_kmh: float) -> float | None:
    """Minutes needed to cover ``distance_km`` at ``speed_kmh``, or ``None``.

    The only honest answer when the speed does not let us compute a duration is
    that there is no duration: a stationary hiker's finish time is unbounded, and
    a negative distance or speed is not a physical input at all. Returning
    ``None`` keeps every downstream consumer (the sunset margin, the battery
    projection) inside the "not computable, disclose it" protocol of spec 10.3
    instead of forcing a fabricated number into a rule comparison.

    Args:
        distance_km: Distance still to cover, in kilometres.
        speed_kmh: Effective speed in km/h at the instant of the estimate.

    Returns:
        Minutes, ``>= 0``, or ``None`` when the ratio is not defined.
    """
    distance = _finite(distance_km)
    speed = _finite(speed_kmh)
    if distance < 0.0 or speed <= 0.0:
        return None
    return distance / speed * 60.0


def derive_pace_drop_percent(avg_speed_10m: float, avg_speed_30m: float) -> float | None:
    """Percent the 10-minute window has fallen behind the 30-minute one.

    Spec 7 R-D's kinematic trend feature::

        pace_drop_percent = max(avg_speed_30m - avg_speed_10m, 0) / avg_speed_30m * 100

    The ``max(..., 0)`` clamp makes it a *drop* measure -- a hiker going faster
    now reports 0%, not a negative "drop". The result is clipped at 100% so the
    number stays a percentage of the long window, and ``None`` is returned when
    the long window carries no speed at all (nothing has been walked yet, or the
    device reported zero), because no ratio is defined and the rule must be told
    the feature is missing rather than handed 0%.

    Deliberately absent: any physiological or medical threshold. The windows are
    10 and 30 minutes (``calibration.pace``), the arithmetic is one division, and
    nothing here says what a human body can or cannot do.
    """
    short_window = _finite(avg_speed_10m)
    long_window = _finite(avg_speed_30m)
    if long_window <= 0.0:
        return None
    drop = long_window - short_window
    if drop <= 0.0:
        return 0.0
    return min(drop / long_window * 100.0, 100.0)


def minutes_between(start: datetime, end: datetime) -> float:
    """Minutes from ``start`` to ``end``; negative when ``end`` is earlier.

    Spec 19 forbids the core from calling ``datetime.now()``: both instants are
    always passed in, so the same inputs give the same number in every run. A
    naive instant mixed with an aware one cannot be subtracted (Python raises),
    so both are compared on their wall clock in that case rather than one being
    silently assumed to be UTC.
    """
    if (start.tzinfo is None) != (end.tzinfo is None):
        start, end = start.replace(tzinfo=None), end.replace(tzinfo=None)
    return (end - start).total_seconds() / 60.0


@dataclass(frozen=True)
class MovementModel:
    """The frozen movement model, with every coefficient supplied as data.

    Responsibility: turn three observed speeds into one effective speed, and
    apply the fatigue penalty once, in one place, so a projection and an ETA
    never disagree about how fast the hiker is walking. Not responsible for:
    judging whether the resulting pace is bad -- that is the risk core's job.

    Attributes:
        base_speed_weights: Weight of each of the three observed speeds
            (``avg_speed_10m``, ``avg_speed_30m``, ``current_speed_kmh``).
            DEMO CALIBRATION: the spec's frozen MVP form, not an industry pace
            model.
        fatigue_penalty_cap: Largest fraction of the base speed the fatigue
            penalty may remove, in [0, 1]. DEMO CALIBRATION.
    """

    base_speed_weights: Mapping[str, float]
    fatigue_penalty_cap: float

    # ------------------------------------------------------------------ speeds

    def base_speed_kmh(
        self,
        *,
        current_speed_kmh: float,
        avg_speed_10m: float,
        avg_speed_30m: float,
    ) -> float:
        """The weighted base speed of the frozen model, in km/h.

        Non-finite inputs become 0.0 (see :func:`_finite`) and the result is
        clamped at zero, so no NaN or negative speed can reach the ETA functions
        or a rule comparison.
        """
        weighted = 0.0
        for field_name, value in (
            ("current_speed_kmh", current_speed_kmh),
            ("avg_speed_10m", avg_speed_10m),
            ("avg_speed_30m", avg_speed_30m),
        ):
            weighted += float(self.base_speed_weights.get(field_name, 0.0)) * _finite(value)
        return max(weighted, 0.0)

    def effective_speed_kmh(self, base_speed_kmh: float, fatigue_index: float | None) -> float:
        """Base speed with the fatigue penalty applied, in km/h.

        ``fatigue_index`` is the rule-constructed simulated feature on
        :class:`~trailguard.schemas.context.DeviceState` (spec 5.4, 22). It is a
        demo feature, not a medical measurement, and the penalty it applies is
        capped by ``fatigue_penalty_cap`` so an extreme index cannot drive the
        effective speed below zero. A ``fatigue_index`` of ``None`` applies no
        penalty at all and is reported as such.
        """
        base = max(_finite(base_speed_kmh), 0.0)
        if fatigue_index is None:
            return base
        penalty = min(max(_finite(fatigue_index), 0.0), max(self.fatigue_penalty_cap, 0.0))
        return max(base * (1.0 - penalty), 0.0)

    def effective_from_trend(self, trend: Any, fatigue_index: float | None) -> float:
        """Effective speed from a :class:`MovementTrend`-shaped object.

        Used by ``project_route_state``, whose input is the projection contract
        rather than the device contract, so the fatigue penalty -- which the
        core governs -- must be composable with either input.
        """
        return self.effective_speed_kmh(
            self.base_speed_kmh(
                current_speed_kmh=trend.current_speed_kmh,
                avg_speed_10m=trend.avg_speed_10m,
                avg_speed_30m=trend.avg_speed_30m,
            ),
            fatigue_index,
        )

    def from_calibration(self, calibration: Any) -> "MovementModel":
        """Build the model declared by a loaded calibration.

        Args:
            calibration: A
                :class:`~trailguard.risk_core.calibration.Calibration`.
        """
        return MovementModel(
            base_speed_weights=calibration.base_speed_weights,
            fatigue_penalty_cap=calibration.fatigue_penalty_cap,
        )
