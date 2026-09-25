"""Pydantic contract tests for the frozen TrailGuard schemas (spec sections 5-9).

Responsibility: prove that the Phase 0 contracts accept valid plain dicts, parse
ISO8601 strings, enforce their bounds and reject values outside the frozen
vocabularies -- before any business logic exists to hide behind them.

Explicit non-responsibility: nothing here computes a projection, an ETA, a
sunset margin, a weather lookup or a risk verdict. Where a value would have to be
derived (``minutes_to_sunset``, ``pace_drop_percent``), the tests only check that
the field exists, is optional and is documented as such.
"""

from __future__ import annotations

from datetime import datetime

import pytest
from pydantic import ValidationError

from conftest import (
    HORIZON_VALUES,
    make_context_dict,
    make_device_state_dict,
    make_geo_point_dict,
    make_movement_state_dict,
    make_risk_result_dict,
    make_route_projection_dict,
    make_weather_dict,
)

from trailguard.schemas import (
    DataFreshnessStatus,
    DataQualityReport,
    DeviceState,
    GeoPoint,
    Horizon,
    HorizonRiskAssessment,
    MovementState,
    MovementTrend,
    Observed,
    RiskLevel,
    RiskResult,
    RouteProjection,
    SafetyMargins,
    Scenario,
    ScenarioCategory,
    Severity,
    TimeState,
    TrailGuardContext,
    WeatherForRouteTime,
    WeatherPolicy,
)

VALID_LAT = 46.08102
VALID_LON = 7.77210


# --------------------------------------------------------------------------- #
# TrailGuardContext -- the one shared snapshot (spec 9)
# --------------------------------------------------------------------------- #


def test_valid_context_from_plain_dict(context_dict: dict) -> None:
    """A plain dict with ISO8601 strings validates into the shared context."""
    context = TrailGuardContext.model_validate(context_dict)

    assert context.scenario_id == "unit_test_scenario"
    assert context.position.value.lat == pytest.approx(VALID_LAT)
    assert context.route.value.remaining_distance_km == pytest.approx(7.15)
    assert context.movement.value.avg_speed_10m == pytest.approx(3.6)
    assert context.device.value.network_available is True
    assert context.time_state.minutes_to_sunset == pytest.approx(490.0)
    assert context.weather_policy.mode == "replay"
    assert context.data_quality.overall == pytest.approx(0.93)


def test_context_datetime_strings_parse(context_dict: dict) -> None:
    """ISO8601 ``Z`` timestamps become timezone-aware datetimes."""
    context = TrailGuardContext.model_validate(context_dict)

    assert isinstance(context.timestamp, datetime)
    assert isinstance(context.position.observed_at, datetime)
    assert context.timestamp.tzinfo is not None
    assert context.time_state.analysis_time < context.time_state.sunset_time


def test_context_json_round_trip(context: TrailGuardContext) -> None:
    """The snapshot survives ``model_dump_json`` / ``model_validate_json``."""
    restored = TrailGuardContext.model_validate_json(context.model_dump_json())

    assert restored == context


def test_context_rejects_unknown_field(context_dict: dict) -> None:
    """``extra="forbid"`` keeps the frozen contract closed."""
    context_dict["surprise"] = True

    with pytest.raises(ValidationError):
        TrailGuardContext.model_validate(context_dict)


def test_context_requires_every_observed_envelope(context_dict: dict) -> None:
    """Dropping a required envelope fails loudly instead of defaulting it."""
    del context_dict["device"]

    with pytest.raises(ValidationError):
        TrailGuardContext.model_validate(context_dict)


def test_context_factory_is_deterministic() -> None:
    """Two factory calls build equal, independently mutable snapshots."""
    first = TrailGuardContext.model_validate(make_context_dict())
    second = TrailGuardContext.model_validate(make_context_dict())

    assert first == second

    first.scenario_id = "mutated"
    assert second.scenario_id == "unit_test_scenario"


# --------------------------------------------------------------------------- #
# Observed -- the generic provenance envelope
# --------------------------------------------------------------------------- #


def test_observed_generic_round_trip() -> None:
    """``Observed[T]`` survives validation, JSON and re-validation."""
    observed = Observed[GeoPoint].model_validate(
        {
            "value": make_geo_point_dict(),
            "observed_at": "2026-05-18T10:29:40Z",
            "source": "gps",
            "confidence": 0.95,
        }
    )
    restored = Observed[GeoPoint].model_validate_json(observed.model_dump_json())

    assert observed.value == restored.value
    assert observed.observed_at == restored.observed_at
    assert observed.source == restored.source
    assert observed.confidence == pytest.approx(restored.confidence)
    assert restored.value.alt_m == pytest.approx(1379.0)


def test_observed_defaults_confidence_to_one() -> None:
    """A source that cannot report confidence still validates (default 1.0)."""
    observed = Observed[MovementState].model_validate(
        {
            "value": make_movement_state_dict(),
            "observed_at": "2026-05-18T10:29:45Z",
            "source": "device_pedometer",
        }
    )

    assert observed.confidence == 1.0
    assert observed.value.pace_drop_percent == pytest.approx(2.7)


def test_observed_rejects_out_of_range_confidence() -> None:
    """Confidence is a probability in [0, 1]."""
    payload = make_context_dict()
    payload["position"]["confidence"] = 1.5

    with pytest.raises(ValidationError):
        TrailGuardContext.model_validate(payload)


def test_observed_rejects_missing_timestamp() -> None:
    """``observed_at`` is mandatory -- without it freshness is unknowable."""
    payload = make_context_dict()
    del payload["route"]["observed_at"]

    with pytest.raises(ValidationError):
        TrailGuardContext.model_validate(payload)


# --------------------------------------------------------------------------- #
# GeoPoint bounds
# --------------------------------------------------------------------------- #


def test_invalid_latitude_is_rejected() -> None:
    """Latitude outside [-90, 90] is a contract violation, not a clamp."""
    with pytest.raises(ValidationError):
        GeoPoint.model_validate({"lat": 99.0, "lon": VALID_LON})

    with pytest.raises(ValidationError):
        GeoPoint.model_validate({"lat": -120.0, "lon": VALID_LON})


def test_invalid_longitude_is_rejected() -> None:
    """Longitude outside [-180, 180] is a contract violation, not a clamp."""
    with pytest.raises(ValidationError):
        GeoPoint.model_validate({"lat": VALID_LAT, "lon": 181.0})


def test_geo_point_altitude_is_optional() -> None:
    """A fix without altitude (edge case S11/S12) is valid; ``alt_m`` is None."""
    point = GeoPoint.model_validate({"lat": VALID_LAT, "lon": VALID_LON})

    assert point.alt_m is None


# --------------------------------------------------------------------------- #
# Enum domains
# --------------------------------------------------------------------------- #


def test_invalid_enum_values_are_rejected() -> None:
    """Every closed vocabulary rejects a value outside it.

    ``ValueError`` (not only pydantic's ``ValidationError``) is asserted because
    a direct enum lookup raises plain ``ValueError`` while a field assignment
    raises a ``ValidationError`` -- which is itself a ``ValueError``.
    """
    with pytest.raises(ValueError):
        Horizon("+15min")

    with pytest.raises(ValueError):
        Severity("EXTREME")

    with pytest.raises(ValueError):
        DataFreshnessStatus("OLD")

    with pytest.raises(ValueError):
        ScenarioCategory("live")

    with pytest.raises(ValueError):
        WeatherPolicy.model_validate({"mode": "sideways"})

    with pytest.raises(ValueError):
        DataQualityReport.model_validate(
            {**make_context_dict()["data_quality"], "overall": "high"}
        )


def test_frozen_enum_wire_values() -> None:
    """The frozen enum members keep their exact wire values."""
    assert HORIZON_VALUES == ["NOW", "+30min", "+60min", "+120min"]
    assert [severity.value for severity in Severity] == ["LOW", "MEDIUM", "HIGH", "CRITICAL"]


# --------------------------------------------------------------------------- #
# Horizon
# --------------------------------------------------------------------------- #


def test_horizon_minutes_offsets() -> None:
    """Each horizon maps to its frozen offset in minutes."""
    assert Horizon.NOW.minutes == 0
    assert Horizon.PLUS_30.minutes == 30
    assert Horizon.PLUS_60.minutes == 60
    assert Horizon.PLUS_120.minutes == 120


def test_horizon_all_horizons_canonical_order() -> None:
    """``all_horizons`` is the frozen, ordered four-horizon set (spec 6.1)."""
    horizons = Horizon.all_horizons()

    assert horizons == [Horizon.NOW, Horizon.PLUS_30, Horizon.PLUS_60, Horizon.PLUS_120]
    assert [h.minutes for h in horizons] == [0, 30, 60, 120]


# --------------------------------------------------------------------------- #
# MovementTrend excludes device state
# --------------------------------------------------------------------------- #


def test_movement_trend_excludes_device_state() -> None:
    """Projection input carries speeds only; battery/fatigue are rejected here."""
    trend = MovementTrend.model_validate(
        {
            "current_speed_kmh": 3.5,
            "avg_speed_10m": 3.6,
            "avg_speed_30m": 3.7,
        }
    )

    assert trend.avg_speed_30m == pytest.approx(3.7)

    with pytest.raises(ValidationError):
        MovementTrend.model_validate(
            {
                "current_speed_kmh": 3.5,
                "avg_speed_10m": 3.6,
                "avg_speed_30m": 3.7,
                "battery_percent": 78.0,
            }
        )


def test_device_state_fatigue_index_bounds() -> None:
    """``fatigue_index`` is bounded in [0, 1] and documented as simulated."""
    device = TrailGuardContext.model_validate(make_context_dict()).device

    assert 0.0 <= device.value.fatigue_index <= 1.0
    docstring = (DeviceState.__doc__ or "").lower()
    assert "not a medical model output" in docstring
    assert "rule-constructed simulated" in docstring

    with pytest.raises(ValidationError):
        TrailGuardContext.model_validate(
            make_context_dict(device={"value": {"fatigue_index": 1.4}})
        )


# --------------------------------------------------------------------------- #
# TimeState
# --------------------------------------------------------------------------- #


def test_time_state_optional_margin() -> None:
    """``minutes_to_sunset`` is optional: Phase 1 derives it, not the schema."""
    state = TimeState.model_validate(
        {"analysis_time": "2026-05-18T10:30:00Z", "sunset_time": "2026-05-18T18:40:00Z"}
    )

    assert state.minutes_to_sunset is None


# --------------------------------------------------------------------------- #
# WeatherForRouteTime
# --------------------------------------------------------------------------- #


def test_weather_validation_from_plain_dict() -> None:
    """Weather features plus provenance validate, with probability bounded."""
    weather = WeatherForRouteTime.model_validate(make_weather_dict())

    assert weather.source == "open-meteo"
    assert weather.target_time > weather.forecast_generated_at
    assert 0.0 <= weather.precipitation_probability <= 1.0


@pytest.mark.parametrize("bad_field", ["precipitation_probability", "confidence", "freshness_minutes"])
def test_weather_rejects_out_of_range_values(bad_field: str) -> None:
    """Probability-like and age fields have hard bounds."""
    with pytest.raises(ValidationError):
        WeatherForRouteTime.model_validate(make_weather_dict(**{bad_field: -0.1}))


def test_weather_freshness_status_against_budget() -> None:
    """Freshness is a pure comparison of age against the caller's budget."""
    weather = WeatherForRouteTime.model_validate(make_weather_dict(freshness_minutes=30.0))

    assert weather.freshness_status(180) is DataFreshnessStatus.FRESH
    assert weather.freshness_status(30) is DataFreshnessStatus.FRESH  # boundary is inclusive
    assert weather.freshness_status(29) is DataFreshnessStatus.STALE
    assert weather.freshness_status(0) is DataFreshnessStatus.STALE


def test_weather_freshness_status_unknown_without_a_budget() -> None:
    """Without a usable freshness budget the status is UNKNOWN, never "fresh".

    ``freshness_minutes`` itself is constrained to be non-negative by the
    contract, so an unknowable freshness is expressed by a caller that cannot
    supply a budget (a negative or missing ``max_freshness_min``).
    """
    weather = WeatherForRouteTime.model_validate(make_weather_dict(freshness_minutes=30.0))

    assert weather.freshness_status(-1) is DataFreshnessStatus.UNKNOWN


# --------------------------------------------------------------------------- #
# RiskResult / SafetyMargins
# --------------------------------------------------------------------------- #


def test_risk_result_from_plain_dict() -> None:
    """A plain dict builds a full, explainable verdict."""
    risk = RiskResult.model_validate(make_risk_result_dict())

    assert risk.risk_level.value == "GREEN"
    assert risk.risk_score == 12
    assert risk.drivers[0].rule_id == "R-C"
    assert risk.hard_rules_triggered == []
    assert len(risk.evidence_refs) == 2
    assert risk.safety_margins.time_margin_min == pytest.approx(355.0)


def test_risk_result_rejects_out_of_range_score() -> None:
    """The aggregate score lives in [0, 100]."""
    with pytest.raises(ValidationError):
        RiskResult.model_validate(make_risk_result_dict(risk_score=101))

    with pytest.raises(ValidationError):
        RiskResult.model_validate(make_risk_result_dict(risk_score=-1))


def test_risk_result_defaults_are_empty_not_none() -> None:
    """Empty lists default to ``[]``; the verdict is still explainable."""
    risk = RiskResult.model_validate(make_risk_result_dict(drivers=[], evidence_refs=[]))

    assert risk.drivers == []
    assert risk.evidence_refs == []
    assert risk.horizon is Horizon.NOW


def test_safety_margins_default_to_not_computable() -> None:
    """Absent margins are ``None`` ("not computable"), never invented numbers."""
    margins = SafetyMargins()

    assert margins.time_margin_min is None
    assert margins.projected_battery_percent is None
    assert margins.route_deviation_m is None
    assert margins.weather_freshness_min is None
    assert margins.projection_confidence is None


# --------------------------------------------------------------------------- #
# Scenario container
# --------------------------------------------------------------------------- #


def test_scenario_requires_replay_metadata() -> None:
    """A scenario without ``simulation_note``/``route_ref`` cannot be replayed."""
    from trailguard.schemas import ToolName

    payload = {
        "scenario_id": "S99_probe",
        "description": "probe",
        "category": "baseline",
        "is_synthetic": True,
        "simulation_note": "synthetic probe fixture",
        "task_prompt": "assess",
        "route_ref": "replay/routes/scn_route_sample.gpx",
        "initial_context": make_context_dict(),
        "expected_required_tools": ["get_route_context"],
        "expected_required_horizons": ["NOW"],
        "forbidden_behaviors": ["HALLUCINATE_WEATHER"],
    }

    scenario = Scenario.model_validate(payload)
    assert scenario.category is ScenarioCategory.BASELINE
    assert scenario.expected_required_tools == [ToolName.GET_ROUTE_CONTEXT]
    assert scenario.movement_timeline == []

    del payload["simulation_note"]
    with pytest.raises(ValidationError):
        Scenario.model_validate(payload)


def test_scenario_rejects_unknown_tool_name() -> None:
    """``expected_required_tools`` is bounded by the frozen registry."""
    payload = {
        "scenario_id": "S99_probe",
        "description": "probe",
        "category": "baseline",
        "is_synthetic": True,
        "simulation_note": "synthetic probe fixture",
        "task_prompt": "assess",
        "route_ref": "replay/routes/scn_route_sample.gpx",
        "initial_context": make_context_dict(),
        "expected_required_tools": ["forecast_risk"],
        "expected_required_horizons": ["NOW"],
        "forbidden_behaviors": [],
    }

    with pytest.raises(ValidationError):
        Scenario.model_validate(payload)


# --------------------------------------------------------------------------- #
# DeviceState carries the movement window the core consumes (spec 5.4, 6.3)
# --------------------------------------------------------------------------- #


def test_device_state_carries_the_observed_movement_window() -> None:
    """The device feed must stand on its own in the transcript (spec 5.4)."""
    payload = set(DeviceState.model_json_schema()["properties"])

    assert "movement" in payload, "the frozen movement model of spec 6.3 needs a window inside the tool output"
    assert {"battery_percent", "network_available", "fatigue_index", "data_quality"} <= payload
    assert "last_network_time" in payload


def test_device_state_data_quality_is_bounded_and_not_a_risk_score() -> None:
    """``data_quality`` is the feed's own assessment in [0, 1] -- never a score."""
    device = make_device_state_dict()

    assert 0.0 <= device["data_quality"] <= 1.0

    description = DeviceState.model_fields["data_quality"].description or ""
    assert "not a risk score" in description

    with pytest.raises(ValidationError):
        TrailGuardContext.model_validate(make_context_dict(device={"value": {"data_quality": 1.4}}))


def test_device_state_movement_matches_the_shared_window() -> None:
    """The duplicated window is the same :class:`MovementState` shape, enforced."""
    context = TrailGuardContext.model_validate(make_context_dict())
    device = context.device.value

    assert device.movement.current_speed_kmh == pytest.approx(device.movement.avg_speed_10m - 0.1)
    assert set(MovementState.model_json_schema()["properties"]) == set(type(device.movement).model_json_schema()["properties"])


def test_device_state_rejects_an_unknown_field() -> None:
    """The tool output stays closed, like every other contract here."""
    with pytest.raises(ValidationError):
        TrailGuardContext.model_validate(
            make_context_dict(device={"value": {"accident_probability": 0.02}})
        )


# --------------------------------------------------------------------------- #
# RouteProjection's redundant horizon spelling must not drift (spec 6.1)
# --------------------------------------------------------------------------- #


def test_a_projection_whose_two_horizon_spellings_agree_is_accepted() -> None:
    """``horizon`` and ``horizon_min`` are redundant by design; agreement is required."""
    for horizon_value, offset in (("NOW", 0), ("+30min", 30), ("+60min", 60), ("+120min", 120)):
        projection = RouteProjection.model_validate(
            make_route_projection_dict(horizon=horizon_value, horizon_min=offset)
        )

        assert projection.horizon is Horizon(horizon_value)
        assert projection.horizon_min == offset


@pytest.mark.parametrize(
    ("horizon_value", "wrong_offset"),
    [
        ("+60min", 120),
        ("NOW", 30),
        ("+120min", 60),
        ("+30min", 0),
    ],
)
def test_a_projection_with_mismatched_horizon_spellings_is_rejected(
    horizon_value: str, wrong_offset: int
) -> None:
    """A drifting ``horizon_min`` would break every horizon-keyed lookup downstream."""
    with pytest.raises(ValidationError) as excinfo:
        RouteProjection.model_validate(
            make_route_projection_dict(horizon=horizon_value, horizon_min=wrong_offset)
        )

    message = str(excinfo.value)
    assert "horizon_min" in message
    assert horizon_value in message


def test_the_projection_documents_that_it_is_not_a_probability() -> None:
    """A projected state has no likelihood of harm attached to it (spec 6.2)."""
    docstring = (RouteProjection.__doc__ or "").lower()

    assert "never a projected risk state value scored as an" in docstring
    assert "accident probability" in docstring


# --------------------------------------------------------------------------- #
# Weather freshness: MISSING and UNKNOWN are different claims (spec 10.4)
# --------------------------------------------------------------------------- #


def test_freshness_status_reports_missing_for_an_unusable_age() -> None:
    """A negative age is not an age any producer could have reported."""
    weather = WeatherForRouteTime.model_validate(make_weather_dict(freshness_minutes=0.0))
    # ``freshness_minutes`` is constrained ``ge=0``, so an unusable age can only
    # arrive through a value validated before that constraint existed: build the
    # model and set the attribute directly, as a later constraint change would.
    object.__setattr__(weather, "freshness_minutes", -5.0)

    assert weather.freshness_status(180) is DataFreshnessStatus.MISSING
    assert weather.freshness_status(-1) is DataFreshnessStatus.UNKNOWN


def test_freshness_status_uses_zero_as_a_real_budget() -> None:
    """A zero-minute budget is a legitimate (brutal) configuration, not an error."""
    weather = WeatherForRouteTime.model_validate(make_weather_dict(freshness_minutes=0.0))

    assert weather.freshness_status(0) is DataFreshnessStatus.FRESH


def test_freshness_status_distinguishes_the_two_unknowns() -> None:
    """UNKNOWN = no budget to judge with; MISSING = no age to judge."""
    docstring = WeatherForRouteTime.freshness_status.__doc__ or ""

    assert "MISSING" in docstring and "UNKNOWN" in docstring
    assert "deliberately distinct" in docstring


# --------------------------------------------------------------------------- #
# HorizonRiskAssessment -- the skill-level per-horizon bundle (spec 10.2)
# --------------------------------------------------------------------------- #


def test_horizon_risk_assessment_bundles_projection_weather_and_verdict() -> None:
    """One horizon's project -> weather -> assess record, assembled by the skill."""
    assessment = HorizonRiskAssessment.model_validate(
        {
            "horizon": "+60min",
            "projected_state": make_route_projection_dict(horizon="+60min", horizon_min=60),
            "weather_freshness_min": 30.0,
            "risk": make_risk_result_dict(),
            "insufficient_data": ["device.movement.avg_speed_30m"],
        }
    )

    assert assessment.horizon is Horizon.PLUS_60
    assert assessment.projected_state is not None
    assert assessment.projected_state.horizon_min == 60
    assert assessment.weather_freshness_min == pytest.approx(30.0)
    assert assessment.risk.risk_level is RiskLevel.GREEN
    assert assessment.insufficient_data == ["device.movement.avg_speed_30m"]


def test_horizon_risk_assessment_allows_a_missing_projection() -> None:
    """A horizon whose projection could not be computed is a legitimate record."""
    assessment = HorizonRiskAssessment.model_validate(
        {"horizon": "NOW", "weather_freshness_min": None, "risk": make_risk_result_dict(), "insufficient_data": []}
    )

    assert assessment.projected_state is None
    assert assessment.weather_freshness_min is None
    assert assessment.insufficient_data == []


def test_horizon_risk_assessment_is_not_a_tool_output() -> None:
    """No single tool may answer all four horizons (spec 4.2): this is assembled."""
    docstring = HorizonRiskAssessment.__doc__ or ""

    assert "Not responsible for: being a tool output" in docstring
    assert HorizonRiskAssessment.model_json_schema()["title"] != "ToolCallRecord"


def test_horizon_risk_assessment_requires_a_verdict_per_horizon() -> None:
    """A horizon record without its verdict is not evidence of anything."""
    with pytest.raises(ValidationError):
        HorizonRiskAssessment.model_validate({"horizon": "NOW"})

