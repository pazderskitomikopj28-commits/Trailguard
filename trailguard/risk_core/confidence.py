"""Confidence model for the deterministic safety core (spec sections 10.3, 10.4, 8).

Confidence is the verdict's honesty channel: it says how much the assessment
can be trusted given the data behind it. Two rules from the spec drive the
design and must survive every later change:

* Stale or missing time-sensitive data **must** reduce confidence (freshness
  protocol, spec 10.4) -- weather, GPS and network readings age, and a verdict
  built on a six-hour-old weather snapshot is not the same verdict.
* Missing critical inputs must be disclosed rather than defaulted
  (missing-data protocol, spec 10.3) -- the caller passes ``None`` for a
  missing weather freshness or projection confidence, and the model must treat
  ``None`` as "unknown", never as "perfect".

Every weight and every penalty in the Phase 2 model is a demo-calibration
value: TrailGuard team calibration for the DGX Spark hackathon demonstration,
kept as data, replaceable, and never presented as a published uncertainty
framework. The output is a calibrated-looking trust score in [0, 1], not a
probability of harm and not an accident probability.
"""


def compute_confidence(
    data_quality: float,
    weather_freshness_min: float | None,
    projection_confidence: float | None,
) -> float:
    """Combine input trust signals into a confidence in [0, 1].

    Responsibility: read the aggregate data quality, the age of the weather
    snapshot used for a projected position/time, and the confidence of the
    projection itself, and produce one number that falls when the inputs are
    stale, missing or low quality. Not responsible for: fetching or judging
    freshness (that is the weather/provenance layer, which reports
    ``freshness_status`` and ``freshness_minutes``), deciding what is missing,
    or changing the risk level -- confidence only accompanies the verdict.

    Args:
        data_quality: Overall data quality of the context snapshot in [0, 1].
        weather_freshness_min: Age of the weather snapshot in minutes;
            ``None`` means unknown/missing and must be penalised, not ignored.
        projection_confidence: Confidence of the route projection used at the
            horizon; ``None`` when no projection could be computed.

    Returns:
        Confidence in [0, 1].

    Raises:
        NotImplementedError: always, in Phase 0 -- the weights, the freshness
            decay curve and the missing-data penalty are Phase 2 work and are
            demo-calibration parameters.
    """
    raise NotImplementedError(
        "Phase 2: confidence model combining data quality, weather freshness (stale or missing "
        "time-sensitive data must reduce confidence) and projection confidence; all weights are "
        "demo calibration"
    )


__all__ = ["compute_confidence"]
