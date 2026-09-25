"""Shared paths, factories and fixtures for the TrailGuard Phase 0 test suite.

Responsibility of this module: give every test module the same repo layout
constants (``REPO_ROOT``, ``RULES_PATH``, ``SKILL_PATH``, ``SCENARIOS_DIR``) and
the same small factories that build *valid* plain-dict payloads for the frozen
contracts, so the schemas are exercised from raw dicts exactly the way a caller
(a scenario YAML file, a provider, a benchmark runner) would build them.

Explicit non-responsibility: this file holds no assertions of its own and no
business logic. It never constructs a risk verdict, runs a projection or loads a
model -- those are Phase 1..5 work and the corresponding tests assert that the
entry points still refuse to run in Phase 0.
"""

from __future__ import annotations

import copy
from pathlib import Path
from typing import Any

import pytest

from trailguard.schemas import (
    DataQualityReport,
    DeviceState,
    GeoPoint,
    Horizon,
    MovementState,
    MovementTrend,
    Observed,
    RouteContext,
    RouteProjection,
    RiskResult,
    RuleDriver,
    SafetyMargins,
    TimeState,
    TrailGuardContext,
    WeatherForRouteTime,
    WeatherPolicy,
)

# --------------------------------------------------------------------------- #
# Repository layout (single source of truth for every test that touches disk)
# --------------------------------------------------------------------------- #

REPO_ROOT: Path = Path(__file__).resolve().parents[1]

#: Frozen six-rule pack of the deterministic safety core (spec section 7).
RULES_PATH: Path = REPO_ROOT / "trailguard" / "rules" / "rules_v1.yaml"

#: The skill file loaded by the harness in the with-skill condition (spec 3.1).
SKILL_PATH: Path = REPO_ROOT / "SKILL.md"

#: Replay scenario dataset (spec sections 13, 17).
SCENARIOS_DIR: Path = REPO_ROOT / "scenarios"

#: Replay Mode assets the scenario files reference.
REPLAY_ROUTES_DIR: Path = REPO_ROOT / "replay" / "routes"
REPLAY_WEATHER_DIR: Path = REPO_ROOT / "replay" / "weather"

#: Every scenario YAML file in the dataset, in a stable order.
SCENARIO_FILES: list[Path] = sorted(SCENARIOS_DIR.rglob("*.yaml"))

#: Frozen horizons (spec 6.1) in canonical order.
HORIZON_VALUES: list[str] = [horizon.value for horizon in Horizon.all_horizons()]

#: Frozen tool registry (spec 4.1) in canonical order.
EXPECTED_TOOL_NAMES: list[str] = [
    "get_route_context",
    "project_route_state",
    "get_weather_for_route_time",
    "get_user_device_context",
    "assess_risk",
]

#: Rule ids the v1 pack must contain exactly once each (spec section 7).
EXPECTED_RULE_IDS: list[str] = ["R-A", "R-B", "R-C", "R-D", "R-E", "R-F"]

#: Metadata keys every rule in the pack must carry (spec section 7).
FROZEN_RULE_METADATA_KEYS: tuple[str, ...] = (
    "id",
    "name",
    "condition",
    "thresholds",
    "units",
    "severity",
    "rationale",
    "source",
    "source_type",
    "version",
)

#: Closed ``source_type`` domain (spec section 7).
SOURCE_TYPE_DOMAIN: tuple[str, ...] = (
    "external_reference",
    "competition_demo_calibration",
    "derived_rule",
)

#: Secret-shaped argument keys the trace contract must redact.
SECRET_KEY_SAMPLES: tuple[str, ...] = (
    "api_key",
    "token",
    "password",
    "authorization",
    "secret",
    "credential",
    "X-Api-Key",
    "auth_token",
)


# --------------------------------------------------------------------------- #
# Deep-merge helper for the factories
# --------------------------------------------------------------------------- #


def _deep_update(base: dict[str, Any], overrides: dict[str, Any]) -> dict[str, Any]:
    """Return ``base`` with ``overrides`` applied, recursing into nested dicts."""
    merged = dict(base)
    for key, value in overrides.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = _deep_update(merged[key], value)
        else:
            merged[key] = value
    return merged


# --------------------------------------------------------------------------- #
# Valid plain-dict payload templates
# --------------------------------------------------------------------------- #
# Values are internally consistent with replay/routes/scn_route_sample.gpx and
# replay/weather/scn_weather_snapshot.json, so a test that loads both a factory
# payload and a scenario file sees the same magnitudes.

_GEO_POINT_DICT: dict[str, Any] = {
    "lat": 46.08102,
    "lon": 7.77210,
    "alt_m": 1379.0,
}

_ROUTE_CONTEXT_DICT: dict[str, Any] = {
    "route_id": "scn_route_sample",
    "route_progress": 0.35,
    "off_route_distance_m": 8.0,
    "remaining_distance_km": 7.15,
    "remaining_ascent_m": 726.0,
    "current_altitude_m": 1379.0,
    "route_polyline_ref": "replay/routes/scn_route_sample.gpx",
    "data_quality": 0.92,
}

_MOVEMENT_STATE_DICT: dict[str, Any] = {
    "current_speed_kmh": 3.5,
    "avg_speed_10m": 3.6,
    "avg_speed_30m": 3.7,
    "pace_drop_percent": 2.7,
}

#: The device feed is the full spec 5.4 block: it repeats the movement window
#: (deliberately -- the tool output has to stand on its own in the transcript) and
#: adds the feed's own quality score, which is not the same number as the
#: aggregated ``DataQualityReport.device_data_quality``.
_DEVICE_STATE_DICT: dict[str, Any] = {
    "battery_percent": 78.0,
    "network_available": True,
    "fatigue_index": 0.12,
    "movement": _MOVEMENT_STATE_DICT,
    "data_quality": 0.95,
    "last_network_time": "2026-05-18T10:28:00Z",
}

_TIME_STATE_DICT: dict[str, Any] = {
    "analysis_time": "2026-05-18T10:30:00Z",
    "sunset_time": "2026-05-18T18:40:00Z",
    "minutes_to_sunset": 490.0,
}

_WEATHER_DICT: dict[str, Any] = {
    "temperature_c": 12.0,
    "precipitation_probability": 0.05,
    "wind_speed_kmh": 8.4,
    "wind_gust_kmh": 17.5,
    "weather_code": 1,
    "source": "open-meteo",
    "forecast_generated_at": "2026-05-18T10:00:00Z",
    "target_time": "2026-05-18T11:30:00Z",
    "freshness_minutes": 30.0,
    "confidence": 0.9,
}

_ROUTE_PROJECTION_DICT: dict[str, Any] = {
    "horizon": "NOW",
    "horizon_min": 0,
    "projected_route_progress": 0.35,
    "projected_position": _GEO_POINT_DICT,
    "projected_arrival_time": "2026-05-18T10:30:00Z",
    "projected_remaining_distance_km": 7.15,
    "projection_confidence": 0.9,
}

_RISK_RESULT_DICT: dict[str, Any] = {
    "risk_level": "GREEN",
    "risk_score": 12,
    "drivers": [
        {
            "rule_id": "R-C",
            "type": "future_weather",
            "severity": "HIGH",
            "detail": "wind_gust_kmh 17.5 below the demo threshold 45",
        }
    ],
    "hard_rules_triggered": [],
    "confidence": 0.8,
    "evidence_refs": [
        "route_context:scn_route_sample@2026-05-18T10:30:00Z",
        "weather:open-meteo@2026-05-18T11:30:00Z",
    ],
    "safety_margins": {
        "time_margin_min": 355.0,
        "projected_battery_percent": 69.0,
        "route_deviation_m": 8.0,
        "weather_freshness_min": 30.0,
        "projection_confidence": 0.9,
    },
    "horizon": "NOW",
    "computed_at": "2026-05-18T10:31:00Z",
}


# --------------------------------------------------------------------------- #
# Factories (module level, so parametrised tests can call them directly)
# --------------------------------------------------------------------------- #


def make_geo_point_dict(**overrides: Any) -> dict[str, Any]:
    """Return a valid plain-dict :class:`GeoPoint` payload."""
    return _deep_update(copy.deepcopy(_GEO_POINT_DICT), overrides)


def make_route_context_dict(**overrides: Any) -> dict[str, Any]:
    """Return a valid plain-dict :class:`RouteContext` payload."""
    return _deep_update(copy.deepcopy(_ROUTE_CONTEXT_DICT), overrides)


def make_movement_state_dict(**overrides: Any) -> dict[str, Any]:
    """Return a valid plain-dict :class:`MovementState` payload."""
    return _deep_update(copy.deepcopy(_MOVEMENT_STATE_DICT), overrides)


def make_movement_trend_dict(**overrides: Any) -> dict[str, Any]:
    """Return a valid plain-dict :class:`MovementTrend` payload (speeds only)."""
    payload = {
        "current_speed_kmh": 3.5,
        "avg_speed_10m": 3.6,
        "avg_speed_30m": 3.7,
    }
    return _deep_update(payload, overrides)


def make_device_state_dict(**overrides: Any) -> dict[str, Any]:
    """Return a valid plain-dict :class:`DeviceState` payload."""
    return _deep_update(copy.deepcopy(_DEVICE_STATE_DICT), overrides)


def make_time_state_dict(**overrides: Any) -> dict[str, Any]:
    """Return a valid plain-dict :class:`TimeState` payload."""
    return _deep_update(copy.deepcopy(_TIME_STATE_DICT), overrides)


def make_weather_dict(**overrides: Any) -> dict[str, Any]:
    """Return a valid plain-dict :class:`WeatherForRouteTime` payload."""
    return _deep_update(copy.deepcopy(_WEATHER_DICT), overrides)


def make_route_projection_dict(**overrides: Any) -> dict[str, Any]:
    """Return a valid plain-dict :class:`RouteProjection` payload."""
    return _deep_update(copy.deepcopy(_ROUTE_PROJECTION_DICT), overrides)


def make_risk_result_dict(**overrides: Any) -> dict[str, Any]:
    """Return a valid plain-dict :class:`RiskResult` payload."""
    return _deep_update(copy.deepcopy(_RISK_RESULT_DICT), overrides)


def make_context_dict(**overrides: Any) -> dict[str, Any]:
    """Return a valid plain-dict :class:`TrailGuardContext` payload.

    Every field of the spec section 9 snapshot is present, so a caller that
    wants to exercise a missing field has to remove it explicitly.
    """
    payload: dict[str, Any] = {
        "scenario_id": "unit_test_scenario",
        "timestamp": "2026-05-18T10:30:00Z",
        "position": {
            "value": make_geo_point_dict(),
            "observed_at": "2026-05-18T10:29:40Z",
            "source": "gps",
            "confidence": 0.95,
        },
        "route": {
            "value": make_route_context_dict(),
            "observed_at": "2026-05-18T10:29:50Z",
            "source": "route_planner",
            "confidence": 0.95,
        },
        "movement": {
            "value": make_movement_state_dict(),
            "observed_at": "2026-05-18T10:29:45Z",
            "source": "device_pedometer",
            "confidence": 0.9,
        },
        "device": {
            "value": make_device_state_dict(),
            "observed_at": "2026-05-18T10:29:55Z",
            "source": "device",
            "confidence": 0.95,
        },
        "time_state": make_time_state_dict(),
        "weather_policy": {
            "mode": "replay",
            "max_freshness_min": 180,
            "snapshot_ref": "replay/weather/scn_weather_snapshot.json",
            "preferred_source": "open-meteo",
        },
        "data_quality": {
            "gps_quality": 0.95,
            "route_data_quality": 0.92,
            "weather_data_quality": 0.9,
            "device_data_quality": 0.95,
            "overall": 0.93,
            "missing_fields": [],
            "stale_fields": [],
        },
    }
    return _deep_update(copy.deepcopy(payload), overrides)


# --------------------------------------------------------------------------- #
# Validated model helpers
# --------------------------------------------------------------------------- #


def make_context(**overrides: Any) -> TrailGuardContext:
    """Return a valid validated :class:`TrailGuardContext`."""
    return TrailGuardContext.model_validate(make_context_dict(**overrides))


def make_risk_result(**overrides: Any) -> RiskResult:
    """Return a valid validated :class:`RiskResult`."""
    return RiskResult.model_validate(make_risk_result_dict(**overrides))


def make_weather(**overrides: Any) -> WeatherForRouteTime:
    """Return a valid validated :class:`WeatherForRouteTime`."""
    return WeatherForRouteTime.model_validate(make_weather_dict(**overrides))


def make_route_context(**overrides: Any) -> RouteContext:
    """Return a valid validated :class:`RouteContext`."""
    return RouteContext.model_validate(make_route_context_dict(**overrides))


def make_device_state(**overrides: Any) -> DeviceState:
    """Return a valid validated :class:`DeviceState`."""
    return DeviceState.model_validate(make_device_state_dict(**overrides))


def make_time_state(**overrides: Any) -> TimeState:
    """Return a valid validated :class:`TimeState`."""
    return TimeState.model_validate(make_time_state_dict(**overrides))


def make_geo_point(**overrides: Any) -> GeoPoint:
    """Return a valid validated :class:`GeoPoint`."""
    return GeoPoint.model_validate(make_geo_point_dict(**overrides))


def make_route_projection(**overrides: Any) -> RouteProjection:
    """Return a valid validated :class:`RouteProjection`."""
    return RouteProjection.model_validate(make_route_projection_dict(**overrides))


# --------------------------------------------------------------------------- #
# Fixtures
# --------------------------------------------------------------------------- #


@pytest.fixture
def repo_root() -> Path:
    """Repository root every path constant is derived from."""
    return REPO_ROOT


@pytest.fixture
def rules_path() -> Path:
    """Path to the frozen rule pack YAML."""
    return RULES_PATH


@pytest.fixture
def skill_path() -> Path:
    """Path to the SKILL.md file the with-skill condition injects."""
    return SKILL_PATH


@pytest.fixture
def scenarios_dir() -> Path:
    """Root of the replay scenario dataset."""
    return SCENARIOS_DIR


@pytest.fixture
def context_dict() -> dict[str, Any]:
    """A valid plain-dict context snapshot (spec section 9)."""
    return make_context_dict()


@pytest.fixture
def context(context_dict: dict[str, Any]) -> TrailGuardContext:
    """A valid validated :class:`TrailGuardContext`."""
    return TrailGuardContext.model_validate(context_dict)


@pytest.fixture
def risk_result_dict() -> dict[str, Any]:
    """A valid plain-dict risk verdict."""
    return make_risk_result_dict()


@pytest.fixture
def risk_result() -> RiskResult:
    """A valid validated :class:`RiskResult`."""
    return make_risk_result()


@pytest.fixture
def weather() -> WeatherForRouteTime:
    """A valid validated :class:`WeatherForRouteTime`."""
    return make_weather()


@pytest.fixture
def route_context() -> RouteContext:
    """A valid validated :class:`RouteContext`."""
    return make_route_context()


@pytest.fixture
def device_state() -> DeviceState:
    """A valid validated :class:`DeviceState`."""
    return make_device_state()


@pytest.fixture
def time_state() -> TimeState:
    """A valid validated :class:`TimeState`."""
    return make_time_state()


@pytest.fixture
def geo_point() -> GeoPoint:
    """A valid validated :class:`GeoPoint`."""
    return make_geo_point()


@pytest.fixture
def weather_policy() -> WeatherPolicy:
    """A valid validated :class:`WeatherPolicy` (replay mode)."""
    return WeatherPolicy.model_validate(
        {
            "mode": "replay",
            "max_freshness_min": 180,
            "snapshot_ref": "replay/weather/scn_weather_snapshot.json",
            "preferred_source": "open-meteo",
        }
    )


@pytest.fixture
def data_quality() -> DataQualityReport:
    """A valid validated :class:`DataQualityReport`."""
    return DataQualityReport.model_validate(
        {
            "gps_quality": 0.95,
            "route_data_quality": 0.92,
            "weather_data_quality": 0.9,
            "device_data_quality": 0.95,
            "overall": 0.93,
            "missing_fields": [],
            "stale_fields": [],
        }
    )


@pytest.fixture
def movement_state() -> MovementState:
    """A valid validated :class:`MovementState`."""
    return MovementState.model_validate(make_movement_state_dict())


@pytest.fixture
def movement_trend() -> MovementTrend:
    """A valid validated :class:`MovementTrend` (no device fields)."""
    return MovementTrend.model_validate(make_movement_trend_dict())


@pytest.fixture
def observed_geo_point(geo_point: GeoPoint) -> Observed[GeoPoint]:
    """A valid validated :class:`Observed` envelope around a :class:`GeoPoint`."""
    return Observed[GeoPoint].model_validate(
        {
            "value": make_geo_point_dict(),
            "observed_at": "2026-05-18T10:29:40Z",
            "source": "gps",
            "confidence": 0.95,
        }
    )


@pytest.fixture
def safety_margins() -> SafetyMargins:
    """A valid validated :class:`SafetyMargins` block."""
    return SafetyMargins.model_validate(make_risk_result_dict()["safety_margins"])


@pytest.fixture
def rule_driver() -> RuleDriver:
    """A valid validated :class:`RuleDriver`."""
    return RuleDriver.model_validate(make_risk_result_dict()["drivers"][0])


@pytest.fixture
def scenario_files() -> list[Path]:
    """Every scenario YAML file in the dataset."""
    return list(SCENARIO_FILES)
