"""Route-aware weather providers, replay and live (spec 5.3, 8-14, 17, 19, 23, 24).

Phase 2's deliverable is one sentence: *weather is asked for at the projected
position and the projected arrival time, and a failure to answer is a visible
failure*. Everything in this module exists to keep that sentence true.

What is pinned here, and why each one is load-bearing:

* **the frozen Phase 0 snapshot is still the frozen Phase 0 snapshot** (spec 25).
  A byte hash, so "the replay asset was quietly re-normalised" cannot happen
  silently, plus a structural check that it is still an Open-Meteo-shaped document
  rather than a TrailGuard-native one;
* **the position and the instant actually reach the provider** (spec 7 R-C). A
  recording wrapper asserts the latitude, longitude and ``target_time`` a provider
  was handed are the *projection's*, not the current position's -- the difference
  between route-aware weather and a current-weather rule with a future label;
* **a live failure is never answered** (spec 11). Timeout, 5xx, non-JSON body,
  missing hazard variable and a date outside the forecast range each raise a named
  error, and a monkeypatched ``ReplayWeatherProvider`` proves no code path
  constructs the fallback while a live call is failing;
* **confidence is not invented** (spec 9). Open-Meteo declares none, so the reading
  carries the calibration's unknown value with ``confidence_is_assumed=True``
  rather than a confident-looking 0.9;
* **the future-weather demonstration** (spec 15): the same hiker, the same route,
  four horizons, where NOW/+30/+60 are safe and +120 fires R-C because the weather
  at the *projected* position two hours out is hazardous.

``httpx.MockTransport`` is the only transport used. Conftest's autouse fixture
closes the socket layer for the whole suite (spec 13), so any test that reached the
real network here would fail rather than quietly pass -- and the one path that may
touch the wire, ``scripts/test_live_weather.py``, is never collected by pytest.
"""

from __future__ import annotations

import hashlib
import inspect
import json
import socket
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import httpx
import pytest
import yaml

from conftest import REPO_ROOT, RULES_PATH, SCENARIO_FILES
from trailguard.replay.devices import load_device_feed
from trailguard.risk_core.calibration import CALIBRATION_PATH, load_calibration
from trailguard.risk_core.engine import DeterministicRiskEngine
from trailguard.risk_core.hard_rules import load_rule_pack
from trailguard.schemas import Scenario, WeatherPolicy
from trailguard.schemas.risk import RiskLevel
from trailguard.schemas.weather import WeatherForRouteTime
from trailguard.tools import assess_risk, get_route_context, project_route_state
from trailguard.tools.weather_route_time import (
    get_weather_for_route_time,
    weather_for_projection,
)
from trailguard.weather import (
    ReplayWeatherProvider,
    WeatherProvider,
    WeatherProviderError,
    WeatherProviderNotConfigured,
    WeatherProviderResponse,
    WeatherProviderUnavailable,
    WeatherTargetUnavailable,
    OpenMeteoWeatherProvider,
    reading_provenance,
)
from trailguard.weather.open_meteo import build_params

# --------------------------------------------------------------------------- #
# Assets and constants
# --------------------------------------------------------------------------- #

#: The Phase 0 weather asset. Its bytes are pinned: spec 25 forbids rewriting it,
#: and a new fixture is a new file (``scn_weather_future_hazard.json``).
PHASE_ZERO_SNAPSHOT: Path = REPO_ROOT / "replay" / "weather" / "scn_weather_snapshot.json"

#: sha256 of the Phase 0 asset, as frozen.
PHASE_ZERO_SNAPSHOT_SHA256 = "817543047a1b002d8fece6eaa9ca0cffe76500a5d964f46deb29fa5e97ee23bd"

#: The Phase 2 demo fixture, stored beside the frozen one but never over it.
FUTURE_HAZARD_SNAPSHOT: Path = REPO_ROOT / "replay" / "weather" / "scn_weather_future_hazard.json"

#: The analysis instant every frozen scenario is written against (spec 19).
ANALYSIS = datetime(2026, 5, 18, 10, 30, tzinfo=timezone.utc)

#: The instant the demo fixture says it was generated.
GENERATED_AT = datetime(2026, 5, 18, 10, 0, tzinfo=timezone.utc)

#: R-C's wind-gust threshold, read from the frozen pack rather than typed in here.
R_C_GUST_THRESHOLD = float(
    next(
        rule for rule in load_rule_pack(RULES_PATH).rules if rule.id == "R-C"
    ).thresholds["wind_gust_kmh"]
)

#: What an undisclosed provider confidence costs, per the frozen calibration.
UNKNOWN_CONFIDENCE = float(load_calibration(CALIBRATION_PATH).confidence_unknown_value)


def _open_meteo_payload(**overrides: Any) -> dict[str, Any]:
    """A live-shaped Open-Meteo forecast response, verified against the real API.

    Rows are naive wall-clock instants with ``utc_offset_seconds`` beside them,
    ``hourly_units`` names each variable's unit, ``precipitation_probability`` is in
    percent and the parcel carries no ``generated_at`` and no ``confidence`` --
    which is exactly what the live API does. Overrides let a test break one thing.
    """
    payload: dict[str, Any] = {
        "latitude": 46.08102,
        "longitude": 7.7721,
        "elevation": 1378.9,
        "utc_offset_seconds": 0,
        "timezone": "UTC",
        "timezone_abbreviation": "UTC",
        "hourly_units": {
            "time": "iso8601",
            "temperature_2m": "°C",
            "precipitation_probability": "%",
            "wind_speed_10m": "km/h",
            "wind_gusts_10m": "km/h",
            "weather_code": "wmo code",
        },
        "hourly": {
            "time": [
                "2026-05-18T10:00",
                "2026-05-18T11:00",
                "2026-05-18T12:00",
                "2026-05-18T13:00",
            ],
            "temperature_2m": [9.0, 10.5, 11.5, 11.0],
            "precipitation_probability": [0, 5, 20, 35],
            "wind_speed_10m": [6.2, 7.1, 14.8, 18.4],
            "wind_gusts_10m": [14.0, 16.0, 58.0, 72.0],
            "weather_code": [0, 1, 2, 3],
        },
    }
    payload.update(overrides)
    return payload


def _transport(
    payload: Any = None,
    *,
    status: int = 200,
    body: bytes | str | None = None,
    raises: BaseException | None = None,
    calls: list[Any] | None = None,
) -> httpx.MockTransport:
    """A mock transport answering with ``payload``, or failing the way told.

    ``raises`` makes the handler raise, which is how a timeout or a DNS failure is
    simulated; ``calls`` collects every request that was actually sent so a test can
    assert how many attempts were made (spec 12: at most one retry).
    """

    def handler(request: httpx.Request) -> httpx.Response:
        if calls is not None:
            calls.append(request)
        if raises is not None:
            raise raises
        if body is not None:
            content = body.encode() if isinstance(body, str) else body
        else:
            content = json.dumps(_open_meteo_payload() if payload is None else payload).encode()
        return httpx.Response(status, content=content, request=request)

    return httpx.MockTransport(handler)


class RecordingProvider(WeatherProvider):
    """A provider wrapper that records exactly what it was asked for.

    It delegates everything to the provider it wraps, so a test asserts the real
    answer *and* the real question. This is the seam that makes "the projected
    position and the projected arrival time reach the weather query" an assertion
    rather than a comment.
    """

    mode = "replay"

    def __init__(self, inner: WeatherProvider) -> None:
        self.inner = inner
        self.calls: list[dict[str, Any]] = []

    def describe(self) -> str:
        return f"recording({self.inner.describe()})"

    def get_weather(self, **kwargs: Any) -> Any:
        self.calls.append(dict(kwargs))
        return self.inner.get_weather(**kwargs)


def _scenario(path: Path) -> Scenario:
    """One frozen scenario, validated from its YAML."""
    return Scenario.model_validate(yaml.safe_load(path.read_text(encoding="utf-8")))


def _clear_day() -> Scenario:
    """The frozen clear-day scenario: the one the demo fixture is built around."""
    return _scenario(next(path for path in SCENARIO_FILES if path.stem == "scn_clear_day_normal"))


def _replay_provider(ref: str = "scn_weather_future_hazard.json") -> ReplayWeatherProvider:
    """The Phase 2 demo fixture as a provider."""
    return ReplayWeatherProvider(f"replay/weather/{ref}")


def _replay_policy(ref: str = "replay/weather/scn_weather_future_hazard.json") -> WeatherPolicy:
    """A policy that chose replay and named the fixture it chose."""
    return WeatherPolicy.model_validate(
        {
            "mode": "replay",
            "max_freshness_min": 180,
            "snapshot_ref": ref,
            "preferred_source": "open-meteo",
        }
    )


def _live_policy() -> WeatherPolicy:
    """A policy that chose live weather from the only live provider."""
    return WeatherPolicy.model_validate(
        {"mode": "live", "max_freshness_min": 180, "preferred_source": "open-meteo"}
    )


@pytest.fixture(scope="module")
def pack() -> Any:
    """The frozen six-rule pack."""
    return load_rule_pack(RULES_PATH)


@pytest.fixture(scope="module")
def engine(pack: Any) -> DeterministicRiskEngine:
    """The Phase 1 engine over the frozen pack and calibration."""
    return DeterministicRiskEngine(pack, load_calibration(CALIBRATION_PATH))


# --------------------------------------------------------------------------- #
# 1. The frozen assets (spec 25)
# --------------------------------------------------------------------------- #


def test_the_phase_zero_snapshot_is_byte_for_byte_the_frozen_asset() -> None:
    """``scn_weather_snapshot.json`` has not been rewritten since Phase 0.

    A hash, not a vibe: re-normalising the data file into a nicer shape would leave
    every other test passing while making the benchmark silently different from the
    run that was accepted. Spec 25 is explicit that a new fixture is a *new file*,
    so this is the guard that makes "add, don't edit" checkable.
    """
    assert PHASE_ZERO_SNAPSHOT.is_file(), "the Phase 0 weather asset is missing from the repository"

    digest = hashlib.sha256(PHASE_ZERO_SNAPSHOT.read_bytes()).hexdigest()

    assert digest == PHASE_ZERO_SNAPSHOT_SHA256


def test_the_phase_zero_snapshot_is_still_an_open_meteo_shaped_document() -> None:
    """The frozen asset stays in the provider's own response shape.

    If it were rewritten into TrailGuard-native rows, the replay path would stop
    exercising the normalisation the live path uses -- the unit and timezone
    conversions would become testable only against a live response nobody runs.
    """
    document = json.loads(PHASE_ZERO_SNAPSHOT.read_text(encoding="utf-8"))

    assert isinstance(document["hourly"], dict), "the asset must keep parallel hourly arrays"
    assert "series" not in document, "a TrailGuard-native 'series' is not this parser's shape"
    assert document["hourly_units"]["precipitation_probability"] == "%"
    assert isinstance(document["utc_offset_seconds"], int)
    # Rows are naive wall-clock instants: no offset written into the strings.
    assert all("+" not in row and not row.endswith("Z") for row in document["hourly"]["time"])


def test_the_new_demo_fixture_is_a_new_file_not_a_rewrite() -> None:
    """The future-hazard fixture sits *beside* the frozen one and says it is synthetic.

    The demo needs a series whose hazard worsens later in the day, which the frozen
    asset does not express. Per spec 25 that is a new file -- and per spec 26 it is
    marked as invented, so nobody can mistake it for Open-Meteo history.
    """
    assert FUTURE_HAZARD_SNAPSHOT.is_file(), "the Phase 2 demo fixture was not added"
    assert FUTURE_HAZARD_SNAPSHOT.read_bytes() != PHASE_ZERO_SNAPSHOT.read_bytes()

    document = json.loads(FUTURE_HAZARD_SNAPSHOT.read_text(encoding="utf-8"))

    assert document.get("synthetic") is True
    assert "synthetic" in document["note"].lower()
    assert "not open-meteo" in document["note"].lower()
    # Same response shape as the frozen asset, so one parser serves both.
    assert isinstance(document["hourly"], dict)
    assert document["hourly_units"]["precipitation_probability"] == "%"


# --------------------------------------------------------------------------- #
# 2. The replay provider: route-aware, and honestly so (spec 16, 17, 24)
# --------------------------------------------------------------------------- #


def test_replay_reads_the_snapshot_the_caller_named() -> None:
    """The provider answers from the fixture the policy pointed at, and says so."""
    provider = _replay_provider()

    reading = provider.get_weather(
        latitude=46.08102, longitude=7.7721, target_time=ANALYSIS, analysis_time=ANALYSIS
    )

    assert reading.mode == "replay"
    assert provider.describe().startswith("replay:ReplayWeatherProvider")
    assert provider.describe().count("scn_weather_future_hazard.json") == 1


def test_a_replay_provider_that_names_no_snapshot_refuses_rather_than_defaulting() -> None:
    """Replay is an explicit choice, including *which* fixture.

    Defaulting to the shipped asset would let a benchmark claim it ran fixture X
    while reading fixture Y. Spec 17's rule is that replay is reachable only when
    the caller chose it, and choosing includes naming.
    """
    with pytest.raises(WeatherProviderNotConfigured, match="snapshot_ref"):
        ReplayWeatherProvider(None)


def test_a_snapshot_ref_that_resolves_to_nothing_is_refused_with_the_candidates() -> None:
    """A missing fixture is an error naming every path tried, never a substitution."""
    with pytest.raises(WeatherProviderNotConfigured, match="was not found"):
        ReplayWeatherProvider("replay/weather/does_not_exist.json")


def test_the_exact_row_instant_is_served_with_zero_reach() -> None:
    """A target that lands on a row reaches nothing: it is that row's weather."""
    provider = _replay_provider()

    reading = provider.get_weather(
        latitude=46.08102, longitude=7.7721, target_time=datetime(2026, 5, 18, 11, 0, tzinfo=timezone.utc)
    )

    assert reading.row_time == datetime(2026, 5, 18, 11, 0, tzinfo=timezone.utc)
    assert reading.reach_minutes == pytest.approx(0.0)
    assert reading.weather.wind_gust_kmh == pytest.approx(16.0)


def test_the_nearest_row_wins_and_a_tie_goes_to_the_earlier_row() -> None:
    """The documented grid rule, on the demo fixture: nearest row, ties earlier.

    10:30Z sits exactly between the 10:00Z and 11:00Z rows. Resolving it to the
    later row instead would hand a +30-minute horizon weather that has not happened
    yet, and it would do so deterministically -- which is what makes the tie-break
    worth pinning.
    """
    provider = _replay_provider()

    tie = provider.get_weather(
        latitude=46.08102, longitude=7.7721, target_time=datetime(2026, 5, 18, 10, 30, tzinfo=timezone.utc)
    )
    closer = provider.get_weather(
        latitude=46.08102, longitude=7.7721, target_time=datetime(2026, 5, 18, 10, 20, tzinfo=timezone.utc)
    )
    later = provider.get_weather(
        latitude=46.08102, longitude=7.7721, target_time=datetime(2026, 5, 18, 10, 40, tzinfo=timezone.utc)
    )

    assert tie.row_time == datetime(2026, 5, 18, 10, 0, tzinfo=timezone.utc)
    assert tie.reach_minutes == pytest.approx(30.0)
    assert closer.row_time == datetime(2026, 5, 18, 10, 0, tzinfo=timezone.utc)
    assert later.row_time == datetime(2026, 5, 18, 11, 0, tzinfo=timezone.utc)


def test_a_discrete_code_is_never_interpolated() -> None:
    """``weather_code`` comes from a row, whole, or the call fails.

    Interpolating temperature is arguable; interpolating a WMO *code* produces a
    label nobody wrote. The rule is applied by taking the row, so a code in the
    answer is always a code some row actually carried.
    """
    provider = _replay_provider()

    reading = provider.get_weather(
        latitude=46.08102, longitude=7.7721, target_time=datetime(2026, 5, 18, 12, 30, tzinfo=timezone.utc)
    )

    assert reading.weather.weather_code in (0, 1, 2, 3, 61)
    assert reading.weather.weather_code == 2, "the 12:00Z row's own code, not an average of neighbours"


def test_a_target_time_outside_the_series_is_refused() -> None:
    """A discrete grid is not extrapolated in either direction (spec 17)."""
    provider = _replay_provider()

    with pytest.raises(WeatherTargetUnavailable, match="no row of the replay snapshot"):
        provider.get_weather(
            latitude=46.08102, longitude=7.7721, target_time=datetime(2026, 5, 18, 9, 0, tzinfo=timezone.utc)
        )


def test_a_naive_target_time_is_refused_before_any_lookup() -> None:
    """A naive instant cannot be compared with aware rows, so it is refused."""
    provider = _replay_provider()

    with pytest.raises(WeatherProviderError, match="timezone-aware"):
        provider.get_weather(latitude=46.08102, longitude=7.7721, target_time=datetime(2026, 5, 18, 12, 30))


# --------------------------------------------------------------------------- #
# 3. The position and the instant reach the query (spec 7 R-C, 18)
# --------------------------------------------------------------------------- #


@pytest.fixture()
def clear_day_chain() -> tuple[Any, Any, Any]:
    """The frozen clear-day scenario: its context, route context and device feed."""
    scenario = _clear_day()
    context = scenario.initial_context
    route_state = get_route_context(
        context.route.value.route_id, context.position.value, context.timestamp
    )
    device_state = load_device_feed("scn_device_" + scenario.scenario_id).current
    return context, route_state, device_state


def _projections(route_state: Any, device_state: Any) -> dict[int, Any]:
    """One projection per frozen horizon, from the real chain."""
    context = _clear_day().initial_context
    return {
        horizon: project_route_state(
            route_context=route_state,
            movement={
                "current_speed_kmh": device_state.movement.current_speed_kmh,
                "avg_speed_10m": device_state.movement.avg_speed_10m,
                "avg_speed_30m": device_state.movement.avg_speed_30m,
            },
            horizon_min=horizon,
            analysis_time=context.time_state.analysis_time,
            fatigue_penalty=device_state.fatigue_index,
        )
        for horizon in (0, 30, 60, 120)
    }


def test_the_projected_position_reaches_the_weather_query(
    clear_day_chain: tuple[Any, Any, Any]
) -> None:
    """The provider is asked about where the hiker will *be*, not where they are.

    R-C judges the hazard at the projected position. A provider handed the current
    position would turn that rule into a current-weather rule with a future label,
    and nothing downstream could tell.
    """
    context, route_state, device_state = clear_day_chain
    current_position = context.position.value
    recorder = RecordingProvider(_replay_provider())
    projections = _projections(route_state, device_state)

    for horizon, projection in projections.items():
        weather_for_projection(
            projection, provider=recorder, analysis_time=context.time_state.analysis_time
        )
        asked = recorder.calls[-1]

        assert asked["latitude"] == pytest.approx(projection.projected_position.lat)
        assert asked["longitude"] == pytest.approx(projection.projected_position.lon)
        # ...and it is genuinely not the current position once the horizon is not NOW.
        if horizon:
            assert asked["latitude"] != pytest.approx(current_position.lat), (
                "a future horizon must not be asked about the hiker's current position"
            )
            assert asked["longitude"] != pytest.approx(current_position.lon)


def test_the_target_arrival_time_reaches_the_weather_query(
    clear_day_chain: tuple[Any, Any, Any]
) -> None:
    """``target_time`` is the projection's ``projected_arrival_time``, not the clock.

    This is the half of route-awareness that a current-weather provider cannot copy:
    the same position asked about at +30 and +120 is two different weather answers.
    """
    context, route_state, device_state = clear_day_chain
    recorder = RecordingProvider(_replay_provider())

    asked_times: list[datetime] = []
    for horizon, projection in _projections(route_state, device_state).items():
        weather_for_projection(projection, provider=recorder, analysis_time=context.time_state.analysis_time)
        asked = recorder.calls[-1]
        assert asked["target_time"] == projection.projected_arrival_time
        assert asked["analysis_time"] == context.time_state.analysis_time
        asked_times.append(asked["target_time"])

    assert asked_times == sorted(asked_times), "the horizons are asked in arrival order"
    assert len(set(asked_times)) == 4, "four horizons mean four distinct arrival instants"


def test_the_position_comes_from_the_projection_and_is_never_re_guessed() -> None:
    """``weather_for_projection`` reads the projection it was given, nothing else.

    It takes no route, no GPX and no speed, so there is no input from which a
    different position could be derived (spec 18).
    """
    parameters = inspect.signature(weather_for_projection).parameters

    assert list(parameters) == ["projection", "policy", "provider", "analysis_time"]


# --------------------------------------------------------------------------- #
# 4. Spatial coverage: a disclosure, not a guess (spec 17)
# --------------------------------------------------------------------------- #


def test_the_distance_to_the_snapshot_point_is_disclosed() -> None:
    """A single-point snapshot says how far it is from the projected position.

    The demo fixture declares its point and a tolerance; the reading's notes state
    the geodesic distance so a reviewer can see the lookup was spatial rather than
    assumed to be exact.
    """
    provider = _replay_provider()

    reading = provider.get_weather(
        latitude=46.09448, longitude=7.779, target_time=datetime(2026, 5, 18, 11, 0, tzinfo=timezone.utc)
    )

    disclosure = " ".join(reading.note_lines())
    assert "m from it" in disclosure
    assert "46.081" in disclosure


def test_a_query_beyond_the_declared_tolerance_is_refused() -> None:
    """The fixture declares ``location_tolerance_km``; beyond it there is no answer.

    Refusing is the honest alternative to interpolating weather across 40 km of
    Alps, which is exactly the kind of plausible-looking number this phase exists to
    avoid.
    """
    provider = _replay_provider()

    with pytest.raises(WeatherTargetUnavailable, match="coverage tolerance"):
        provider.get_weather(latitude=46.6, longitude=8.0, target_time=datetime(2026, 5, 18, 11, 0, tzinfo=timezone.utc))


# --------------------------------------------------------------------------- #
# 5. Freshness semantics, preserved from Phase 1 (spec 10.4, 24)
# --------------------------------------------------------------------------- #


def test_freshness_is_measured_at_the_projected_arrival_not_the_analysis_clock() -> None:
    """A two-hour-out horizon reports a two-hour-old snapshot.

    The age term has to describe the instant the values are *used*, because that is
    the age the confidence model charges and the Skill's freshness protocol judges.
    """
    provider = _replay_provider()

    now = provider.get_weather(
        latitude=46.08102, longitude=7.7721, target_time=datetime(2026, 5, 18, 10, 30, tzinfo=timezone.utc)
    )
    later = provider.get_weather(
        latitude=46.08102, longitude=7.7721, target_time=datetime(2026, 5, 18, 12, 30, tzinfo=timezone.utc)
    )

    assert now.weather.freshness_minutes == pytest.approx(30.0)
    assert later.weather.freshness_minutes == pytest.approx(150.0)
    assert later.weather.forecast_generated_at == GENERATED_AT


def test_a_snapshot_within_budget_is_fresh_and_one_past_it_is_stale() -> None:
    """The classification is a pure comparison against the caller's own budget."""
    provider = _replay_provider()
    reading = provider.get_weather(
        latitude=46.08102, longitude=7.7721, target_time=datetime(2026, 5, 18, 12, 30, tzinfo=timezone.utc)
    )

    assert reading.weather.freshness_status(max_freshness_min=180).value == "FRESH"
    assert reading.weather.freshness_status(max_freshness_min=60).value == "STALE"


def test_stale_weather_is_reported_as_stale_and_never_as_fresh() -> None:
    """A stale reading keeps its age; nothing re-labels it to stay inside a budget."""
    provider = _replay_provider()

    reading = provider.get_weather(
        latitude=46.08102, longitude=7.7721, target_time=datetime(2026, 5, 18, 12, 30, tzinfo=timezone.utc)
    )

    assert reading.weather.freshness_minutes > 60
    assert reading.weather.freshness_status(max_freshness_min=60).value == "STALE", (
        "a snapshot older than the budget must be classified STALE; the caller decides what "
        "to do about it, not the provider"
    )


# --------------------------------------------------------------------------- #
# 6. Confidence honesty (spec 9, 24)
# --------------------------------------------------------------------------- #


def test_a_provider_that_declares_no_confidence_is_charged_and_flagged() -> None:
    """Open-Meteo says nothing about its own quality, so TrailGuard says so too.

    ``confidence`` carries the calibration's unknown value and
    ``confidence_is_assumed`` says it is charged rather than earned. The alternative
    -- assuming 0.9 because a hiking app would -- hands every verdict a number no
    provider asserted (spec 9).
    """
    provider = OpenMeteoWeatherProvider(transport=_transport())

    reading = provider.get_weather(
        latitude=46.08102, longitude=7.7721, target_time=datetime(2026, 5, 18, 11, 0, tzinfo=timezone.utc)
    )

    assert reading.weather.confidence == pytest.approx(UNKNOWN_CONFIDENCE)
    assert reading.confidence_is_assumed is True
    assert reading.weather.confidence_is_assumed is True
    assert reading.weather.confidence != pytest.approx(0.9)


def test_a_declared_confidence_is_reported_as_the_providers_own() -> None:
    """When a document does declare a confidence, it is passed through uncharged."""
    payload = _open_meteo_payload(confidence=0.72)
    provider = OpenMeteoWeatherProvider(transport=_transport(payload))

    reading = provider.get_weather(
        latitude=46.08102, longitude=7.7721, target_time=datetime(2026, 5, 18, 11, 0, tzinfo=timezone.utc)
    )

    assert reading.weather.confidence == pytest.approx(0.72)
    assert reading.confidence_is_assumed is False


def test_the_replay_fixture_charges_its_unknown_confidence_too() -> None:
    """The demo fixture declares none either, so both paths charge the same value.

    One number for "undisclosed", whichever mode answered -- otherwise the benchmark
    would compare two confidences computed on different scales.
    """
    provider = _replay_provider()

    reading = provider.get_weather(
        latitude=46.08102, longitude=7.7721, target_time=datetime(2026, 5, 18, 11, 0, tzinfo=timezone.utc)
    )

    assert reading.weather.confidence == pytest.approx(UNKNOWN_CONFIDENCE)
    assert reading.confidence_is_assumed is True


# --------------------------------------------------------------------------- #
# 7. The live provider: real requests, loud failures (spec 11-14, 24)
# --------------------------------------------------------------------------- #


def test_the_request_asks_for_the_projected_position_date_and_utc() -> None:
    """The query names the position, the target's date, UTC and the five variables.

    ``timezone=UTC`` is what makes every returned row instant unambiguous: the API
    answers with ``utc_offset_seconds: 0`` and naive wall-clock rows, which the
    normalisation layer then reads as UTC instead of guessing.
    """
    calls: list[Any] = []
    provider = OpenMeteoWeatherProvider(transport=_transport(calls=calls))
    target = datetime(2026, 5, 18, 12, 30, tzinfo=timezone.utc)

    provider.get_weather(latitude=46.13003, longitude=7.81087, target_time=target)

    query = dict(httpx.QueryParams(calls[0].url.params))
    assert query["latitude"] == "46.130030"
    assert query["longitude"] == "7.810870"
    assert query["start_date"] == "2026-05-18"
    assert query["end_date"] == "2026-05-18"
    assert query["timezone"] == "UTC"
    assert set(query["hourly"].split(",")) == {
        "temperature_2m",
        "precipitation_probability",
        "wind_speed_10m",
        "wind_gusts_10m",
        "weather_code",
    }
    assert calls[0].url.host == "api.open-meteo.com"


def test_build_params_never_sends_a_credential() -> None:
    """The public forecast endpoint needs no key, so none is asked for or sent."""
    params = build_params(46.08102, 7.7721, datetime(2026, 5, 18, 12, 30, tzinfo=timezone.utc))

    joined = " ".join(f"{key}={value}" for key, value in params).lower()
    for secret in ("key", "token", "secret", "password", "authorization"):
        assert secret not in joined


def test_a_live_response_is_normalised_into_the_contract_object() -> None:
    """Percent, renames and the row choice all happen on the live path too.

    Same parser as replay, so the conversion that keeps 20 percent from tripping
    R-C's 0.7 threshold cannot be a live-only bug.
    """
    provider = OpenMeteoWeatherProvider(transport=_transport())

    reading = provider.get_weather(
        latitude=46.08102, longitude=7.7721, target_time=datetime(2026, 5, 18, 12, 30, tzinfo=timezone.utc)
    )

    weather = reading.weather
    assert isinstance(weather, WeatherForRouteTime)
    assert weather.temperature_c == pytest.approx(11.5)
    assert weather.precipitation_probability == pytest.approx(0.20)
    assert weather.wind_speed_kmh == pytest.approx(14.8)
    assert weather.wind_gust_kmh == pytest.approx(58.0)
    assert weather.weather_code == 2
    assert weather.source.startswith("open-meteo@")
    assert weather.target_time == datetime(2026, 5, 18, 12, 30, tzinfo=timezone.utc)
    assert reading.row_time == datetime(2026, 5, 18, 12, 0, tzinfo=timezone.utc)
    assert reading.reach_minutes == pytest.approx(30.0)


def test_a_live_forecast_is_zero_minutes_old_by_construction_and_says_why() -> None:
    """The API returns no model-run instant, so the age is the retrieval age.

    Disclosed in the notes rather than implied: reporting a live forecast as fresh
    because nothing said otherwise would be the stale-data failure mode in reverse.
    """
    provider = OpenMeteoWeatherProvider(transport=_transport())

    reading = provider.get_weather(
        latitude=46.08102, longitude=7.7721, target_time=datetime(2026, 5, 18, 11, 0, tzinfo=timezone.utc)
    )

    assert reading.weather.freshness_minutes == pytest.approx(0.0)
    assert "no model-run instant" in " ".join(reading.note_lines())


def test_a_response_written_in_a_named_timezone_is_converted_to_utc() -> None:
    """``12:00`` at ``utc_offset_seconds: 7200`` is 10:00Z, on the live path too.

    Reading the rows as UTC would move every live lookup two hours and quietly
    change which row serves a projected arrival -- the same conversion the frozen
    asset is tested for, run against a live-shaped response.
    """
    payload = _open_meteo_payload(
        utc_offset_seconds=7200,
        timezone="Europe/Zurich",
        timezone_abbreviation="CEST",
        hourly={
            "time": ["2026-05-18T12:00", "2026-05-18T13:00"],
            "temperature_2m": [9.0, 10.5],
            "precipitation_probability": [0, 5],
            "wind_speed_10m": [6.2, 7.1],
            "wind_gusts_10m": [14.0, 16.0],
            "weather_code": [0, 1],
        },
    )
    provider = OpenMeteoWeatherProvider(transport=_transport(payload))

    reading = provider.get_weather(
        latitude=46.08102, longitude=7.7721, target_time=datetime(2026, 5, 18, 10, 0, tzinfo=timezone.utc)
    )

    assert reading.row_time == datetime(2026, 5, 18, 10, 0, tzinfo=timezone.utc)


def test_the_generated_instant_a_document_declares_wins_over_the_retrieval_instant() -> None:
    """A provider that does stamp its run time is quoted, not overridden.

    The live API does not, so the retrieval instant is the fallback -- a fallback the
    caller cannot use to overstate how fresh an upstream model actually was.
    """
    payload = _open_meteo_payload(generated_at="2026-05-18T08:00:00Z")
    provider = OpenMeteoWeatherProvider(transport=_transport(payload))

    reading = provider.get_weather(
        latitude=46.08102, longitude=7.7721, target_time=datetime(2026, 5, 18, 11, 0, tzinfo=timezone.utc)
    )

    assert reading.weather.forecast_generated_at == datetime(2026, 5, 18, 8, 0, tzinfo=timezone.utc)


# --------------------------------------------------------------------------- #
# 8. Live failures are visible failures (spec 11, 12, 24)
# --------------------------------------------------------------------------- #


def test_a_timeout_is_raised_rather_than_answered() -> None:
    """A connect timeout is not a licence to guess weather."""
    provider = OpenMeteoWeatherProvider(transport=_transport(raises=httpx.ConnectTimeout("timed out")))

    with pytest.raises(WeatherProviderUnavailable, match="timeout"):
        provider.get_weather(
            latitude=46.08102, longitude=7.7721, target_time=datetime(2026, 5, 18, 11, 0, tzinfo=timezone.utc)
        )


def test_a_read_timeout_is_raised_rather_than_answered() -> None:
    """The read side times out independently of the connect side."""
    provider = OpenMeteoWeatherProvider(transport=_transport(raises=httpx.ReadTimeout("slow")))

    with pytest.raises(WeatherProviderUnavailable, match="timeout"):
        provider.get_weather(
            latitude=46.08102, longitude=7.7721, target_time=datetime(2026, 5, 18, 11, 0, tzinfo=timezone.utc)
        )


def test_an_unreachable_host_is_raised_with_the_reason() -> None:
    """DNS and refusal failures name what happened, not just that something did."""
    provider = OpenMeteoWeatherProvider(transport=_transport(raises=httpx.ConnectError("dns fail")))

    with pytest.raises(WeatherProviderUnavailable, match="ConnectError"):
        provider.get_weather(
            latitude=46.08102, longitude=7.7721, target_time=datetime(2026, 5, 18, 11, 0, tzinfo=timezone.utc)
        )


def test_a_5xx_is_retried_at_most_once_and_then_fails_loudly() -> None:
    """Two attempts total: spec 12 allows one retry and no unbounded loop."""
    calls: list[Any] = []
    provider = OpenMeteoWeatherProvider(
        transport=_transport(status=503, payload={"error": True, "reason": "busy"}, calls=calls)
    )

    with pytest.raises(WeatherProviderResponse, match="HTTP 503"):
        provider.get_weather(
            latitude=46.08102, longitude=7.7721, target_time=datetime(2026, 5, 18, 11, 0, tzinfo=timezone.utc)
        )

    assert len(calls) == 2, "one retry means two attempts, not one and not three"


def test_a_5xx_followed_by_a_good_answer_succeeds_on_the_retry() -> None:
    """The retry is real work, not decoration: a recovered call returns weather."""
    attempts = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        attempts["n"] += 1
        if attempts["n"] == 1:
            return httpx.Response(503, json={"error": True, "reason": "busy"}, request=request)
        return httpx.Response(200, json=_open_meteo_payload(), request=request)

    provider = OpenMeteoWeatherProvider(transport=httpx.MockTransport(handler))

    reading = provider.get_weather(
        latitude=46.08102, longitude=7.7721, target_time=datetime(2026, 5, 18, 11, 0, tzinfo=timezone.utc)
    )

    assert attempts["n"] == 2
    assert reading.weather.wind_gust_kmh == pytest.approx(16.0)


def test_a_4xx_is_not_retried_and_names_the_unavailable_instant() -> None:
    """The API refusing a date is deterministic, so retrying only doubles the wait.

    Verified against the live API on 2026-09-26: a date outside the forecast range
    is HTTP 400 with ``{"error": true, "reason": "..."}``.
    """
    calls: list[Any] = []
    provider = OpenMeteoWeatherProvider(
        transport=_transport(
            status=400,
            payload={"error": True, "reason": "Parameter 'start_date' is out of allowed range"},
            calls=calls,
        )
    )

    with pytest.raises(WeatherTargetUnavailable, match="out of allowed range"):
        provider.get_weather(
            latitude=46.08102, longitude=7.7721, target_time=datetime(2027, 5, 18, 11, 0, tzinfo=timezone.utc)
        )

    assert len(calls) == 1


def test_a_non_json_body_is_a_visible_failure() -> None:
    """An HTML error page from a proxy is not weather."""
    provider = OpenMeteoWeatherProvider(transport=_transport(body="<html>502 Bad Gateway</html>"))

    with pytest.raises(WeatherProviderResponse, match="not JSON"):
        provider.get_weather(
            latitude=46.08102, longitude=7.7721, target_time=datetime(2026, 5, 18, 11, 0, tzinfo=timezone.utc)
        )


def test_a_missing_hazard_variable_is_a_visible_failure() -> None:
    """A response without gusts cannot answer R-C, so it is not partially accepted."""
    payload = _open_meteo_payload()
    del payload["hourly"]["wind_gusts_10m"]
    provider = OpenMeteoWeatherProvider(transport=_transport(payload))

    with pytest.raises(WeatherProviderResponse, match="wind_gusts_10m"):
        provider.get_weather(
            latitude=46.08102, longitude=7.7721, target_time=datetime(2026, 5, 18, 11, 0, tzinfo=timezone.utc)
        )


def test_ragged_parallel_arrays_are_a_visible_failure() -> None:
    """Truncating to the shortest array would pair values with the wrong instants."""
    payload = _open_meteo_payload()
    payload["hourly"]["wind_gusts_10m"] = [14.0, 16.0]
    provider = OpenMeteoWeatherProvider(transport=_transport(payload))

    with pytest.raises(WeatherProviderResponse, match="parallel arrays"):
        provider.get_weather(
            latitude=46.08102, longitude=7.7721, target_time=datetime(2026, 5, 18, 11, 0, tzinfo=timezone.utc)
        )


def test_a_unit_the_normaliser_will_not_guess_is_a_visible_failure() -> None:
    """A Fahrenheit temperature must fail loudly rather than be renamed to Celsius."""
    payload = _open_meteo_payload()
    payload["hourly_units"]["temperature_2m"] = "°F"
    provider = OpenMeteoWeatherProvider(transport=_transport(payload))

    with pytest.raises(WeatherProviderResponse, match="temperature_2m"):
        provider.get_weather(
            latitude=46.08102, longitude=7.7721, target_time=datetime(2026, 5, 18, 11, 0, tzinfo=timezone.utc)
        )


def test_a_naive_target_time_is_refused_before_a_request_is_sent() -> None:
    """The refusal happens before the wire, not after a wasted call."""
    calls: list[Any] = []
    provider = OpenMeteoWeatherProvider(transport=_transport(calls=calls))

    with pytest.raises(WeatherProviderError, match="timezone-aware"):
        provider.get_weather(latitude=46.08102, longitude=7.7721, target_time=datetime(2026, 5, 18, 11, 0))

    assert calls == []


def test_an_infinite_timeout_is_refused_as_configuration() -> None:
    """Spec 12 wants finite timeouts, so they are checked at construction."""
    with pytest.raises(WeatherProviderNotConfigured, match="timeouts must be positive"):
        OpenMeteoWeatherProvider(connect_timeout_s=0.0)


def test_a_third_attempt_is_refused_as_configuration() -> None:
    """There is no configuration that turns the retry budget into a loop."""
    with pytest.raises(WeatherProviderNotConfigured, match="max_attempts"):
        OpenMeteoWeatherProvider(max_attempts=3)


def test_httpx_absent_is_a_configuration_error_not_a_crash(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Without the weather extra the failure names the extra that provides it.

    ``sys.modules["httpx"] = None`` is how an import is made to fail, and it runs the
    *real* ``_import_httpx`` -- so what is tested is the conversion of a missing
    dependency into a provider error, not a stub of that conversion.
    """
    provider = OpenMeteoWeatherProvider(transport=_transport())
    monkeypatch.setitem(sys.modules, "httpx", None)

    with pytest.raises(WeatherProviderNotConfigured, match="'weather' extra"):
        provider.get_weather(
            latitude=46.08102,
            longitude=7.7721,
            target_time=datetime(2026, 5, 18, 11, 0, tzinfo=timezone.utc),
        )


# --------------------------------------------------------------------------- #
# 9. No silent fallback (spec 11, 17, 24)
# --------------------------------------------------------------------------- #


def test_a_failed_live_call_never_constructs_a_replay_provider(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A live failure must not reach for the frozen fixture, ever.

    The guard is behavioural rather than textual: ``ReplayWeatherProvider`` is made
    to explode on construction, and the failing live call still raises the provider
    error. If any code path substituted replay for live, this test would fail with
    the substitution's own message instead.
    """

    def explode(self: Any, *args: Any, **kwargs: Any) -> None:
        raise AssertionError("a live failure must not construct a replay provider (spec 11, 17)")

    monkeypatch.setattr(ReplayWeatherProvider, "__init__", explode)
    provider = OpenMeteoWeatherProvider(transport=_transport(raises=httpx.ConnectError("offline")))

    with pytest.raises(WeatherProviderUnavailable):
        provider.get_weather(
            latitude=46.08102, longitude=7.7721, target_time=datetime(2026, 5, 18, 11, 0, tzinfo=timezone.utc)
        )


def test_replay_is_reachable_only_when_the_caller_chose_replay() -> None:
    """The policy's mode is the only thing that selects the mode that answers."""
    from trailguard.weather import provider_for_policy

    replay_provider = provider_for_policy(_replay_policy())
    live_provider = provider_for_policy(_live_policy())

    assert replay_provider.mode == "replay"
    assert isinstance(replay_provider, ReplayWeatherProvider)
    assert live_provider.mode == "live"
    assert isinstance(live_provider, OpenMeteoWeatherProvider)


def test_a_live_policy_naming_a_source_this_build_lacks_is_refused() -> None:
    """The policy's preference is honoured or refused, never quietly swapped."""
    from trailguard.weather import provider_for_policy

    policy = WeatherPolicy.model_validate(
        {"mode": "live", "max_freshness_min": 180, "preferred_source": "meteoblue"}
    )

    with pytest.raises(WeatherProviderNotConfigured, match="meteoblue"):
        provider_for_policy(policy)


# --------------------------------------------------------------------------- #
# 10. The tool boundary (spec 18, 20)
# --------------------------------------------------------------------------- #


def test_no_policy_means_live_weather() -> None:
    """The documented default is the real capability; replay has to be chosen.

    A caller that passes no policy gets the live provider, so a frozen fixture can
    never be substituted for a real question by accident.
    """
    from trailguard.tools.weather_route_time import _provider_for

    assert _provider_for(None).mode == "live"


def test_the_weather_tool_cannot_be_handed_more_than_one_instant() -> None:
    """One call answers one (position, instant) pair -- structurally, not by policy.

    ``lat``, ``lon`` and ``target_time`` are scalars in the signature, so there is no
    shape here that could grow into ``forecast_all_horizons()`` (spec 20, 4.2).
    """
    parameters = inspect.signature(get_weather_for_route_time).parameters

    assert list(parameters)[:3] == ["lat", "lon", "target_time"]
    # Annotations are strings here (``from __future__ import annotations``), so the
    # check is on the declared spelling: three scalars, no collection, no horizon set.
    for name in ("lat", "lon"):
        assert parameters[name].annotation == "float"
    assert parameters["target_time"].annotation == "datetime"


def test_one_projection_yields_exactly_one_provider_call(
    clear_day_chain: tuple[Any, Any, Any]
) -> None:
    """One horizon, one projection, one weather lookup, one verdict (spec 20)."""
    _context, route_state, device_state = clear_day_chain
    recorder = RecordingProvider(_replay_provider())

    projection = project_route_state(
        route_context=route_state,
        movement={
            "current_speed_kmh": device_state.movement.current_speed_kmh,
            "avg_speed_10m": device_state.movement.avg_speed_10m,
            "avg_speed_30m": device_state.movement.avg_speed_30m,
        },
        horizon_min=30,
        analysis_time=ANALYSIS,
        fatigue_penalty=device_state.fatigue_index,
    )
    weather_for_projection(projection, provider=recorder)

    assert len(recorder.calls) == 1


def test_two_projections_yield_two_calls_because_the_loop_belongs_to_the_caller() -> None:
    """The tool does not own the horizon walk: the caller asks once per horizon.

    Written as a property rather than a prohibition -- the absence of a loop is
    visible as *one call per call site*, so a four-horizon run is four explicit
    calls in the Skill's code, which is what spec 4.2 asks for.
    """
    recorder = RecordingProvider(_replay_provider())

    for hour in (10, 11):
        weather_for_projection(
            _projection_at(hour), provider=recorder, analysis_time=ANALYSIS
        )

    assert len(recorder.calls) == 2
    assert [call["target_time"].hour for call in recorder.calls] == [10, 11]


def _projection_at(hour: int) -> Any:
    """A route projection whose arrival time is ``hour`` o'clock."""
    from trailguard.schemas.route import Horizon, RouteProjection
    from conftest import make_geo_point

    return RouteProjection(
        horizon=Horizon.NOW,
        horizon_min=0,
        projected_route_progress=0.35,
        projected_position=make_geo_point(lat=46.08102, lon=7.7721),
        projected_arrival_time=datetime(2026, 5, 18, hour, 30, tzinfo=timezone.utc),
        projected_remaining_distance_km=7.15,
        projection_confidence=0.9,
    )


def test_the_tool_returns_weather_and_never_a_risk_level(
    clear_day_chain: tuple[Any, Any, Any]
) -> None:
    """A weather answer carries no verdict: interpretation is the core's job."""
    _context, route_state, device_state = clear_day_chain
    projection = project_route_state(
        route_context=route_state,
        movement={
            "current_speed_kmh": device_state.movement.current_speed_kmh,
            "avg_speed_10m": device_state.movement.avg_speed_10m,
            "avg_speed_30m": device_state.movement.avg_speed_30m,
        },
        horizon_min=120,
        analysis_time=ANALYSIS,
        fatigue_penalty=device_state.fatigue_index,
    )

    weather = get_weather_for_route_time(
        projection.projected_position.lat,
        projection.projected_position.lon,
        projection.projected_arrival_time,
        policy=_replay_policy(),
        analysis_time=ANALYSIS,
    )

    assert isinstance(weather, WeatherForRouteTime)
    assert not hasattr(weather, "risk_level")
    assert not hasattr(weather, "risk_score")


def test_a_reading_carries_the_provenance_a_trace_records() -> None:
    """``reading_provenance`` flattens a reading the way the evidence trace needs."""
    provider = _replay_provider()

    reading = provider.get_weather(
        latitude=46.08102, longitude=7.7721, target_time=datetime(2026, 5, 18, 11, 0, tzinfo=timezone.utc)
    )
    provenance = reading_provenance(reading)

    assert provenance["weather_mode"] == "replay"
    assert provenance["weather_target_time"] == "2026-05-18T11:00:00+00:00"
    assert provenance["weather_row_time"] == "2026-05-18T11:00:00+00:00"
    assert provenance["weather_grid_reach_min"] == pytest.approx(0.0)
    assert provenance["weather_freshness_min"] == pytest.approx(60.0)
    assert provenance["weather_confidence_is_assumed"] is True


# --------------------------------------------------------------------------- #
# 11. The future-weather demonstration (spec 15, 24)
# --------------------------------------------------------------------------- #


def _demo_records(engine: DeterministicRiskEngine) -> list[Any]:
    """The whole Phase 2 chain for the clear-day scenario, once per horizon.

    Real GPX, real device feed, real projections and the demo fixture -- the exact
    sequence the Skill will run in Phase 3, with the weather tool in the middle
    instead of beside it.
    """
    scenario = _clear_day()
    context = scenario.initial_context
    route_state = get_route_context(
        context.route.value.route_id, context.position.value, context.timestamp
    )
    device_state = load_device_feed("scn_device_" + scenario.scenario_id).current
    provider = _replay_provider()
    records: list[Any] = []

    for horizon in (0, 30, 60, 120):
        projection = project_route_state(
            route_context=route_state,
            movement={
                "current_speed_kmh": device_state.movement.current_speed_kmh,
                "avg_speed_10m": device_state.movement.avg_speed_10m,
                "avg_speed_30m": device_state.movement.avg_speed_30m,
            },
            horizon_min=horizon,
            analysis_time=context.time_state.analysis_time,
            fatigue_penalty=device_state.fatigue_index,
        )
        reading = provider.get_weather(
            latitude=projection.projected_position.lat,
            longitude=projection.projected_position.lon,
            target_time=projection.projected_arrival_time,
            analysis_time=context.time_state.analysis_time,
        )
        result = assess_risk(
            route_state=route_state,
            weather_state=reading.weather,
            user_device_state=device_state,
            time_state=context.time_state,
            projected_state=projection,
            horizon_min=horizon,
            weather_freshness_min=reading.weather.freshness_minutes,
            data_quality=context.data_quality,
            engine=engine,
        )
        records.append((horizon, projection, reading, result))

    return records


def test_now_is_safe_and_the_projected_position_two_hours_out_is_hazardous(
    engine: DeterministicRiskEngine,
) -> None:
    """The Phase 2 demonstration, as assertions rather than prose (spec 15).

    Same hiker, same route, same fixture: NOW, +30 and +60 are GREEN with no rule
    firing, and +120 fires R-C alone because the weather *at the projected position
    two hours out* crosses the gust threshold. Only the horizon changed, so the only
    thing that can have changed the verdict is where-and-when the weather was asked
    for.
    """
    records = _demo_records(engine)
    verdicts = {horizon: result for horizon, _projection, _reading, result in records}

    for horizon in (0, 30, 60):
        assert verdicts[horizon].risk_level == RiskLevel.GREEN
        assert verdicts[horizon].risk_score == 0
        assert verdicts[horizon].drivers == []

    future = verdicts[120]
    assert future.risk_level != RiskLevel.GREEN
    assert [driver.rule_id for driver in future.drivers] == ["R-C"]
    # R-C is a soft rule, so its driver is typed 'soft'; what makes it a *future*
    # weather driver is that it was evaluated on the +120 horizon's own reading.
    assert future.drivers[0].type == "soft"
    assert str(int(R_C_GUST_THRESHOLD)) in future.drivers[0].detail
    assert future.horizon.value == "+120min"


def test_the_future_verdict_is_driven_by_the_projected_positions_own_weather(
    engine: DeterministicRiskEngine,
) -> None:
    """The +120 gust is read at +120, and it is the only crossing of the threshold."""
    records = _demo_records(engine)
    gusts = {horizon: reading.weather.wind_gust_kmh for horizon, _p, reading, _r in records}

    assert gusts[0] < R_C_GUST_THRESHOLD
    assert gusts[30] < R_C_GUST_THRESHOLD
    assert gusts[60] < R_C_GUST_THRESHOLD
    assert gusts[120] > R_C_GUST_THRESHOLD
    assert gusts[120] == pytest.approx(58.0)


def test_each_horizon_reads_its_own_weather_rather_than_a_copy(
    engine: DeterministicRiskEngine,
) -> None:
    """Four horizons, four arrival instants, four rows and four distinct readings.

    This is the assertion that the chain is route-*aware*: copying NOW's weather to
    every horizon would still produce four verdicts, but they would all carry the
    same values, the same row and the same target instant.
    """
    records = _demo_records(engine)

    arrivals = [projection.projected_arrival_time for _h, projection, _r, _v in records]
    rows = [reading.row_time for _h, _p, reading, _v in records]
    targets = [reading.weather.target_time for _h, _p, reading, _v in records]
    positions = [projection.projected_position.lat for _h, projection, _p, _v in records]
    gusts = [reading.weather.wind_gust_kmh for _h, _p, reading, _v in records]

    assert len(set(arrivals)) == 4
    assert len(set(targets)) == 4
    assert len(set(positions)) == 4, "the projected position moves with the horizon"
    assert len(set(gusts)) > 1, "the weather read per horizon is not one snapshot re-labelled"
    assert rows[0] != rows[3], "NOW and +120 resolve to different rows of the series"


def test_the_demo_horizons_are_fresh_enough_to_assess(engine: DeterministicRiskEngine) -> None:
    """Every horizon's reading is inside the scenario's own freshness budget."""
    records = _demo_records(engine)
    context = _clear_day().initial_context
    budget = context.weather_policy.max_freshness_min

    statuses = {
        horizon: reading.weather.freshness_status(max_freshness_min=budget).value
        for horizon, _p, reading, _v in records
    }

    assert set(statuses.values()) == {"FRESH"}


# --------------------------------------------------------------------------- #
# 12. Offline (spec 13)
# --------------------------------------------------------------------------- #


def test_a_replay_lookup_opens_no_socket(
    monkeypatch: pytest.MonkeyPatch, clear_day_chain: tuple[Any, Any, Any]
) -> None:
    """Replay needs no network, and this proves it rather than asserting it.

    Conftest already closes the socket layer for every test; this one opens a
    *counter* instead, so the guarantee is stated positively for the replay path
    that the benchmark actually runs.
    """
    _context, route_state, device_state = clear_day_chain
    opened: list[Any] = []
    real_socket = socket.socket

    class Counting(real_socket):  # type: ignore[misc, valid-type]
        def connect(self, *args: Any, **kwargs: Any) -> None:
            opened.append(args)
            raise AssertionError("a replay lookup must not open a connection")

    monkeypatch.setattr(socket, "socket", Counting)
    _context, route_state, device_state = clear_day_chain
    projection = project_route_state(
        route_context=route_state,
        movement={
            "current_speed_kmh": device_state.movement.current_speed_kmh,
            "avg_speed_10m": device_state.movement.avg_speed_10m,
            "avg_speed_30m": device_state.movement.avg_speed_30m,
        },
        horizon_min=120,
        analysis_time=ANALYSIS,
        fatigue_penalty=device_state.fatigue_index,
    )

    weather = get_weather_for_route_time(
        projection.projected_position.lat,
        projection.projected_position.lon,
        projection.projected_arrival_time,
        policy=_replay_policy(),
        analysis_time=ANALYSIS,
    )

    assert opened == []
    assert weather.wind_gust_kmh == pytest.approx(58.0)


def test_the_live_provider_is_never_touched_by_the_frozen_scenarios() -> None:
    """The frozen scenarios choose replay, so the benchmark cannot reach the wire.

    Reading each scenario's own ``weather_policy`` rather than this module's helpers:
    the guarantee is about the assets as shipped.
    """
    for path in SCENARIO_FILES:
        document = yaml.safe_load(path.read_text(encoding="utf-8"))
        policy = document["initial_context"]["weather_policy"]
        assert policy["mode"] == "replay", f"{path.name} must run offline"
        # Replay, but not necessarily the one default asset. Phase 5's R-C coverage
        # needs a gust front this default snapshot cannot produce: its steepest gust
        # is 27.4 km/h against R-C's 45 km/h threshold, so a scenario pinned to it can
        # never reach the future-weather rule at any horizon. The guarantee this test
        # exists for is that the chain cannot reach the wire, and that guarantee is
        # about `mode`; so the reference may name any snapshot that ships under
        # replay/weather/, which is exactly what cannot be a live provider. Asserting
        # it resolves also keeps a typo from silently selecting the default.
        ref = REPO_ROOT / policy["snapshot_ref"]
        assert ref.is_relative_to(REPO_ROOT / "replay" / "weather"), (
            f"{path.name} names weather outside the replay directory"
        )
        assert ref.is_file(), f"{path.name} names a snapshot that does not resolve"
