"""Confidence model for the deterministic safety core (spec 10.3, 10.4, 8, 12).

Confidence is the verdict's honesty channel: it says how much the *assessment* can
be trusted given the data behind it. Two rules from the spec drive the design and
must survive every later change:

* stale or missing time-sensitive data **must** reduce confidence (freshness
  protocol, spec 10.4) -- a verdict built on a six-hour-old weather snapshot is
  not the same verdict as one built on a six-minute-old one;
* missing critical inputs must be disclosed rather than defaulted (missing-data
  protocol, spec 10.3) -- ``None`` means "unknown", never "perfect", so a caller
  hiding a gap cannot buy confidence by hiding it.

What the number means
---------------------
Confidence is an **engineering statement about the inputs**: how well the route is
known, how well the device is reporting, how old the weather is, how reliable the
projection is, and how many fields the caller disclosed as missing or stale. It is
*not* a probability of harm, and it is not comparable with any probability: a
0.9-confidence RED verdict does not mean "90 % dangerous", it means "the inputs
behind this verdict are good ones". The domain concept is a Projected Risk State
(spec 6.2) and the banned identifier ``accident_probability`` is not used as a name
anywhere in this package.

The five terms and their weights are TrailGuard team demo calibration, kept as data
in ``calibration_v1.yaml`` (``competition_demo_calibration``, review pending), and
replaceable without touching this module. They are not a published uncertainty
framework and must not be presented as one.
"""

from __future__ import annotations

from functools import lru_cache
from typing import Iterable, Mapping

from ..schemas.context import DataQualityReport
from .calibration import CALIBRATION_PATH, Calibration, load_calibration

__all__ = ["compute_confidence", "freshness_term"]


def freshness_term(freshness_min: float | None, budget_min: int) -> float:
    """How much a weather snapshot of age ``freshness_min`` is worth, in [0, 1].

    ``1 / (1 + age / budget)``: a snapshot just generated is worth 1.0, one as old
    as the freshness budget is worth 0.5, twice the budget 0.33, and the term
    decays without ever going negative. The form is chosen because it is monotone,
    has no cliff at the budget boundary, and -- crucially -- never calls a snapshot
    the freshness protocol classifies FRESH worth almost nothing, which a linear
    ``1 - age/budget`` would do at 179 of 180 minutes.
    """
    if freshness_min is None or budget_min <= 0:
        return 0.0
    ratio = max(float(freshness_min), 0.0) / float(budget_min)
    return 1.0 / (1.0 + ratio)


def compute_confidence(
    data_quality: float | DataQualityReport | None = None,
    weather_freshness_min: float | None = None,
    projection_confidence: float | None = None,
    *,
    weather_quality: float | None = None,
    weights: Mapping[str, float] | None = None,
    freshness_budget_min: int | None = None,
    unknown_value: float | None = None,
    missing_fields: Iterable[str] = (),
    stale_fields: Iterable[str] = (),
    missing_field_penalty: float | None = None,
    stale_field_penalty: float | None = None,
) -> float:
    """Combine input trust signals into a confidence in [0, 1].

    Responsibility: read five trust signals -- route data quality, device data
    quality, weather age together with the snapshot's own quality, projection
    reliability and the caller's reported overall quality -- combine them with the
    configured weights, then charge a penalty for every field the caller disclosed
    as missing or stale. Not responsible for: judging freshness (the weather layer
    reports ``freshness_minutes`` and ``freshness_status``), deciding what is
    missing, or changing the risk level -- confidence accompanies a verdict, it
    never moves one.

    Args:
        data_quality: Either an overall quality in [0, 1] or a
            :class:`~trailguard.schemas.context.DataQualityReport` carrying the
            per-source breakdown. A bare float fills only the
            ``reported_overall_quality`` term; ``None`` fills none.
        weather_freshness_min: Age of the weather snapshot in minutes. ``None``
            means unknown and is penalised as unknown, never treated as fresh.
        projection_confidence: Confidence of the projection used at this horizon;
            ``None`` when no projection was computed.
        weather_quality: The weather provider's own confidence in the snapshot, if
            it reports one. Multiplied with the age term, because both have to
            hold for a snapshot to be worth much.
        weights: Term -> weight, summing to 1.0. Defaults to the calibration file.
        freshness_budget_min: Age at which a snapshot is worth exactly half, and
            beyond which it is stale. Defaults to the calibration file.
        unknown_value: Value a term takes when it cannot be determined, in [0, 1].
            Defaults to the calibration file (0.5): unknown is penalised, not
            assumed perfect.
        missing_fields: Field dot-paths the caller disclosed as missing. Each
            costs ``missing_field_penalty``.
        stale_fields: Field dot-paths the caller disclosed as stale. Each costs
            ``stale_field_penalty``.
        missing_field_penalty: Per-field cost of a disclosed missing field.
            Defaults to the calibration file.
        stale_field_penalty: Per-field cost of a disclosed stale field. Defaults
            to the calibration file.

    Returns:
        Confidence in [0, 1].

    Raises:
        ValueError: a weight table does not sum to 1.0, an ``unknown_value`` or
            penalty is outside [0, 1], or a term's weight is negative. These are
            configuration errors and must fail loudly rather than silently
            renormalise, because a silently renormalised table reports confidence
            nobody configured.
    """
    resolved_weights = dict(weights) if weights is not None else dict(load_calibration(CALIBRATION_PATH).confidence_weights)
    calibration = _default_calibration()
    budget = int(freshness_budget_min if freshness_budget_min is not None else calibration.weather_freshness_budget_min)
    unknown = float(unknown_value if unknown_value is not None else calibration.confidence_unknown_value)
    missing_penalty = float(
        missing_field_penalty if missing_field_penalty is not None else calibration.confidence_missing_field_penalty
    )
    stale_penalty = float(
        stale_field_penalty if stale_field_penalty is not None else calibration.confidence_stale_field_penalty
    )

    _validate(
        weights=resolved_weights,
        unknown=unknown,
        missing_penalty=missing_penalty,
        stale_penalty=stale_penalty,
        budget=budget,
    )

    report = data_quality if isinstance(data_quality, DataQualityReport) else None
    overall = data_quality if isinstance(data_quality, (int, float)) and not isinstance(data_quality, bool) else None

    route_quality = report.route_data_quality if report is not None else None
    device_quality = report.device_data_quality if report is not None else None
    if report is not None:
        overall = report.overall

    age_value = freshness_term(weather_freshness_min, budget)
    quality_value = None
    if isinstance(weather_quality, (int, float)) and not isinstance(weather_quality, bool):
        quality_value = _clamp01(float(weather_quality))
    if weather_freshness_min is None and weather_quality is None:
        weather = unknown
    elif quality_value is None:
        weather = age_value
    elif weather_freshness_min is None:
        weather = min(quality_value, unknown)
    else:
        weather = age_value * quality_value

    terms: dict[str, float] = {
        "route_data_quality": _term(route_quality, unknown),
        "device_data_quality": _term(device_quality, unknown),
        "weather": _clamp01(weather),
        "projection": _term(projection_confidence, unknown),
        "reported_overall_quality": _term(overall, unknown),
    }

    total = 0.0
    for name, weight in resolved_weights.items():
        if name not in terms:
            raise ValueError(
                f"confidence weight for unknown term {name!r}; expected one of {sorted(terms)}"
            )
        total += float(weight) * terms[name]
    uncovered = sorted(set(terms) - set(resolved_weights))
    if uncovered:
        raise ValueError(
            f"confidence weights do not cover every term: {uncovered} would be silently dropped"
        )

    confidence = _clamp01(
        total
        - missing_penalty * len(list(missing_fields))
        - stale_penalty * len(list(stale_fields))
    )
    return round(confidence, 6)


@lru_cache(maxsize=1)
def _default_calibration() -> Calibration:
    """Load the default calibration once; it is immutable, read-only data."""
    return load_calibration(CALIBRATION_PATH)


def _term(value: float | None, unknown: float) -> float:
    """A term's value, or the unknown penalty when it cannot be determined."""
    if value is None:
        return unknown
    return _clamp01(float(value))


def _clamp01(value: float) -> float:
    """Clamp to the [0, 1] interval every quality/confidence field is bounded by."""
    return min(max(float(value), 0.0), 1.0)


def _validate(
    *,
    weights: Mapping[str, float],
    unknown: float,
    missing_penalty: float,
    stale_penalty: float,
    budget: int,
) -> None:
    """Fail loudly on a configuration that would produce a meaningless number."""
    if not weights:
        raise ValueError("confidence weights must not be empty")
    for name, weight in weights.items():
        if float(weight) < 0:
            raise ValueError(f"confidence weight for {name!r} must not be negative: {weight}")
    total = sum(float(weight) for weight in weights.values())
    if abs(total - 1.0) > 1e-6:
        raise ValueError(f"confidence weights must sum to 1.0, they sum to {total}")
    for label, value in (("unknown_value", unknown), ("missing_field_penalty", missing_penalty), ("stale_field_penalty", stale_penalty)):
        if not 0.0 <= value <= 1.0:
            raise ValueError(f"confidence {label} must be in [0, 1], got {value}")
    if budget < 0:
        raise ValueError(f"confidence freshness_budget_min must not be negative, got {budget}")
