"""The deterministic safety core, end to end (spec 6, 7, 8, 10, 13-19, 22).

Phase 1's contract is a chain with no model in it::

    real GPX -> route context -> projection -> weather snapshot -> assess_risk

Every test here runs that chain against **real assets** -- the GPX polyline, the
replay device feeds, the replay weather snapshots, the frozen rule pack and the
frozen calibration -- and pins the numbers it produces, so the "deterministic" in
"deterministic safety core" is an assertion somebody can re-run rather than a
claim in a document.

What this module pins that the unit tests of ``test_risk_core.py`` cannot:

* the **frozen scenarios still produce their pinned verdicts** (spec 17), through
  the whole chain, at all four horizons;
* the **six rules fire exactly at their thresholds** -- at the threshold they do
  *not* fire, one thousandth past it they do (spec 7, 13, 14);
* the **hard rule sets a floor it never lowers** (spec 13) and the **compound
  rule escalates a level it never inflates** (spec 14), each of which is a
  property of the whole pipeline rather than of one function;
* the **ETA identity** ``time_margin_min == minutes_to_sunset -
  estimated_minutes_to_finish`` (spec 8) holds for a projection the test computes
  itself, at every horizon;
* **confidence is arithmetic about the inputs**, recomputed here from the
  calibration's own weights rather than copied out of a run (spec 16);
* **missing data is disclosed, not defaulted**, and no result ever carries NaN,
  infinity or a wall-clock instant (spec 10.3, 19).

Nothing in this module needs a model, a network connection or a GPU (spec 20, 24).
"""

from __future__ import annotations

import json
import math
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, NamedTuple

import pytest
import yaml

from conftest import (
    RULES_PATH,
    SCENARIO_FILES,
    make_device_state_dict,
    make_route_context_dict,
    make_time_state_dict,
    make_weather_dict,
)
from trailguard.replay.devices import load_device_feed
from trailguard.replay.weather import load_weather_snapshot
from trailguard.risk_core.calibration import CALIBRATION_PATH, Calibration, load_calibration
from trailguard.risk_core.engine import DeterministicRiskEngine
from trailguard.risk_core.hard_rules import RulePack, load_rule_pack
from trailguard.risk_core.movement import estimate_minutes, minutes_between
from trailguard.routes.geometry import RouteAssetError
from trailguard.schemas.risk import RiskLevel
from trailguard.schemas.route import Horizon, RouteProjection
from trailguard.schemas import (
    DataQualityReport,
    DeviceState,
    GeoPoint,
    RouteContext,
    Scenario,
    TimeState,
    WeatherForRouteTime,
)
from trailguard.tools import assess_risk, get_route_context, get_user_device_context, project_route_state


# --------------------------------------------------------------------------- #
# The chain every test below runs
# --------------------------------------------------------------------------- #

#: The replay instant the frozen scenarios are written against (spec 19: it is
#: passed in, never read from the system clock).
ANALYSIS = datetime(2026, 5, 18, 10, 30, tzinfo=timezone.utc)

#: The real GPX asset behind every scenario's route.
ROUTE_ID = "scn_route_sample"

#: Where the scenario that starts the chain stands (its own initial position).
START = GeoPoint(lat=46.08102, lon=7.77210, alt_m=1379.0)

#: The frozen horizon set, in minutes (spec 6.1).
HORIZONS: tuple[int, ...] = (0, 30, 60, 120)


@pytest.fixture(scope="module")
def pack() -> RulePack:
    """The frozen six-rule pack, exactly as shipped."""
    return load_rule_pack(RULES_PATH)


@pytest.fixture(scope="module")
def calibration(pack: RulePack) -> Calibration:
    """The frozen calibration, proven to cover that pack."""
    loaded = load_calibration(CALIBRATION_PATH)
    loaded.validate_against(pack)
    return loaded


@pytest.fixture(scope="module")
def engine(pack: RulePack, calibration: Calibration) -> DeterministicRiskEngine:
    """The Phase 1 engine, wired to the frozen pack and calibration."""
    return DeterministicRiskEngine(pack, calibration)


@pytest.fixture(scope="module")
def model(engine: DeterministicRiskEngine):
    """The frozen movement model the engine itself derives speeds with."""
    return engine.movement_model


#: Sentinel for "the caller said nothing about this", so that ``None`` can be
#: passed *on purpose* -- no weather snapshot at all, no quality report at all --
#: without being silently replaced by a default.
_UNSET: Any = object()


def device(**overrides: Any) -> DeviceState:
    """A device snapshot, with the frozen factory's defaults."""
    return DeviceState.model_validate(make_device_state_dict(**overrides))


def route(**overrides: Any) -> RouteContext:
    """A route context, with the frozen factory's defaults.

    The factory describes a hiker 8 m off the route with 7.15 km and a lot of
    ascent left -- a plausible mid-route state in which nothing fires.
    """
    return RouteContext.model_validate(make_route_context_dict(**overrides))


def report(**overrides: Any) -> DataQualityReport:
    """A data-quality disclosure, with good per-source scores by default."""
    payload: dict[str, Any] = {
        "gps_quality": 0.95,
        "route_data_quality": 0.92,
        "weather_data_quality": 0.9,
        "device_data_quality": 0.95,
        "overall": 0.93,
        "missing_fields": [],
        "stale_fields": [],
    }
    payload.update(overrides)
    return DataQualityReport.model_validate(payload)


def weather_snapshot(**overrides: Any) -> WeatherForRouteTime:
    """A weather row for the route-time instant, benign by default."""
    return WeatherForRouteTime.model_validate(make_weather_dict(**overrides))


def time_state(**overrides: Any) -> TimeState:
    """An analysis clock and a sunset instant, both explicit."""
    return TimeState.model_validate(make_time_state_dict(**overrides))


class HorizonRecord(NamedTuple):
    """One horizon of the chain: what was projected, what weather it read, what came back."""

    horizon: int
    projection: RouteProjection
    weather: WeatherForRouteTime
    result: Any


def assess(
    engine: DeterministicRiskEngine,
    *,
    route_state: RouteContext,
    device_state: DeviceState,
    time: TimeState | None = None,
    weather: WeatherForRouteTime | None = _UNSET,
    projection: RouteProjection | None = None,
    horizon_min: int | None = None,
    quality: DataQualityReport | None = _UNSET,
) -> Any:
    """One ``assess_risk`` call through the *tool*, with the frozen engine.

    The defaults are the boring ones: a benign weather row, an explicit clock, no
    projection (a NOW assessment) and a good quality report. A test that says
    nothing therefore gets "the hiker is mid-route, the weather is fine, the clock
    says mid-afternoon". Passing ``weather=None`` or ``quality=None`` passes *no*
    weather and *no* report, which is what the missing-data tests need.
    """
    resolved_weather = weather_snapshot() if weather is _UNSET else weather
    resolved_quality = report() if quality is _UNSET else quality
    return assess_risk(
        route_state=route_state,
        weather_state=resolved_weather,
        user_device_state=device_state,
        time_state=time if time is not None else time_state(),
        projected_state=projection,
        horizon_min=horizon_min,
        weather_freshness_min=(None if resolved_weather is None else resolved_weather.freshness_minutes),
        data_quality=resolved_quality,
        engine=engine,
    )


def scenario_chain(
    engine: DeterministicRiskEngine, scenario: Scenario, horizons: tuple[int, ...] = HORIZONS
) -> tuple[RouteContext, list[HorizonRecord]]:
    """Run the real Phase 1 chain for one scenario, once per horizon.

    This is the sequence the TrailGuard Skill must run (spec 4.2, 10.2), written
    out here once so every fixture test and every horizon test runs *the same*
    code path rather than a re-implementation of it: a route context from the real
    GPX, the t=0 row of the scenario's own replay device feed, a projection to the
    horizon with the device's fatigue index threaded through it (so the projection
    and the core's ETA cannot disagree about speed), the weather row matching the
    projected arrival instant, and the assessment.
    """
    context = scenario.initial_context
    route_state = get_route_context(context.route.value.route_id, context.position.value, context.timestamp)
    device_state = get_user_device_context("scn_device_" + scenario.scenario_id)
    # The snapshot the scenario *names*, not whichever one happens to be the default.
    # A scenario whose weather_policy.snapshot_ref points at a different replay asset
    # must be evaluated against that asset: the model in a benchmark run is handed
    # the snapshot the scenario declares (the replay provider reads this very field),
    # so a chain that silently substituted the default would score the run against
    # weather it was never shown. The four frozen replay scenarios name the default
    # asset, so this changes nothing for them -- it only stops the four Phase 5
    # benchmark scenarios that deliberately name a different one from being graded
    # against a gust series they never see. test_scenario_fixtures.py already pins
    # that the declared ref resolves, so passing it through refuses rather than
    # defaults when a scenario names nothing.
    snapshot = load_weather_snapshot(context.weather_policy.snapshot_ref)
    records: list[HorizonRecord] = []

    for horizon in horizons:
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
        weather = snapshot.at(projection.projected_arrival_time)
        result = assess_risk(
            route_state=route_state,
            weather_state=weather,
            user_device_state=device_state,
            time_state=context.time_state,
            projected_state=projection,
            horizon_min=horizon,
            weather_freshness_min=weather.freshness_minutes,
            data_quality=context.data_quality,
            engine=engine,
        )
        records.append(HorizonRecord(horizon, projection, weather, result))

    return route_state, records


def _scenarios() -> list[Scenario]:
    """Every frozen scenario, validated from its YAML."""
    return [Scenario.model_validate(yaml.safe_load(path.read_text(encoding="utf-8"))) for path in SCENARIO_FILES]


def _rule_ids(result: Any) -> list[str]:
    """The rule ids that fired, in the order the engine reports them."""
    return [driver.rule_id for driver in result.drivers]


def _dump(value: Any) -> Any:
    """A JSON-mode dump, used to compare results and to scan for NaN/infinity."""
    return json.loads(json.dumps(value, default=str))


# --------------------------------------------------------------------------- #
# 1. The frozen replay fixtures still produce their pinned verdicts (spec 17)
# --------------------------------------------------------------------------- #


def test_the_real_gpx_route_context_is_the_pinned_geometry() -> None:
    """The geodesic route facts, pinned to the last digit.

    These are the numbers the whole Phase 1 chain stands on: progress, the
    remaining distance along the polyline, the off-route distance from the fix,
    the ascent still ahead and the current altitude, all from the real GPX rather
    than from a fixture somebody typed.
    """
    route_state = get_route_context(ROUTE_ID, START, ANALYSIS)

    assert route_state.route_id == ROUTE_ID
    assert route_state.route_progress == pytest.approx(0.34992459777308427)
    assert route_state.off_route_distance_m == pytest.approx(0.060984366915525015)
    assert route_state.remaining_distance_km == pytest.approx(7.1552653262791)
    assert route_state.remaining_ascent_m == pytest.approx(726.0433618422851)
    assert route_state.current_altitude_m == pytest.approx(1378.956638157715)
    assert route_state.data_quality == 1.0
    assert route_state.route_polyline_ref == "replay/routes/scn_route_sample.gpx"


@pytest.mark.parametrize(
    ("scenario_id", "horizon", "level", "score", "hard", "drivers", "confidence", "margin", "battery"),
    [
        # spec S01: a clear day, mid-route, well before sunset.
        ("S01_clear_day", 0, "GREEN", 0, [], [], 0.817143, 354.85900290331597, 68.9906001935544),
        ("S01_clear_day", 30, "GREEN", 0, [], [], 0.79925, 354.85900290331597, 70.9906001935544),
        ("S01_clear_day", 60, "GREEN", 0, [], [], 0.784333, 354.85900290331597, 72.9906001935544),
        ("S01_clear_day", 120, "GREEN", 0, [], [], 0.763182, 354.85900290331597, 76.9906001935544),
        # spec S04: the pace has dropped, the battery is low and sunset is close.
        ("S04_sunset_margin_pace_drop", 0, "RED", 65, ["R-B"], ["R-B", "R-D", "R-E", "R-F"], 0.746286, -55.01501846862823, 10.998998768758117),
        ("S04_sunset_margin_pace_drop", 30, "RED", 65, ["R-B"], ["R-B", "R-D", "R-E", "R-F"], 0.736711, -55.015018468628284, 12.998998768758113),
        ("S04_sunset_margin_pace_drop", 60, "RED", 65, ["R-B"], ["R-B", "R-D", "R-E", "R-F"], 0.727878, -55.015018468628284, 14.998998768758113),
        ("S04_sunset_margin_pace_drop", 120, "RED", 65, ["R-B"], ["R-B", "R-D", "R-E", "R-F"], 0.714872, -55.01501846862823, 18.998998768758117),
        # spec S11: a stale fix, a missing pace drop, no network, poor data quality.
        ("S11_stale_gps_missing_data", 0, "GREEN", 0, [], [], 0.366326, 74.38164959737091, 47.95877663982473),
        ("S11_stale_gps_missing_data", 30, "GREEN", 0, [], [], 0.358413, 74.38164959737091, 49.95877663982473),
        ("S11_stale_gps_missing_data", 60, "GREEN", 0, [], [], 0.350918, 74.38164959737094, 51.95877663982473),
        ("S11_stale_gps_missing_data", 120, "GREEN", 0, [], [], 0.339909, 74.38164959737091, 55.95877663982473),
    ],
)
def test_the_frozen_fixtures_produce_their_pinned_verdicts(
    engine: DeterministicRiskEngine,
    scenario_id: str,
    horizon: int,
    level: str,
    score: int,
    hard: list[str],
    drivers: list[str],
    confidence: float,
    margin: float,
    battery: float,
) -> None:
    """Each frozen scenario reproduces its pinned verdict at each frozen horizon.

    The pins were produced by running this chain against the shipped assets, then
    checked line by line against the arithmetic the spec freezes: the movement
    model's effective speed, the ETA that follows from it, the sunset margin, the
    battery projection and the confidence terms. A change to any of those shows up
    here as a changed number, which is the point.
    """
    scenario = next(item for item in _scenarios() if item.scenario_id == scenario_id)
    _, records = scenario_chain(engine, scenario, horizons=(horizon,))
    result = records[0].result

    assert result.horizon.value == next(h for h in Horizon.all_horizons() if h.minutes == horizon).value
    assert result.risk_level.value == level
    assert result.risk_score == score
    assert result.hard_rules_triggered == hard
    assert _rule_ids(result) == drivers
    assert result.confidence == pytest.approx(confidence)
    assert result.safety_margins.time_margin_min == pytest.approx(margin)
    assert result.safety_margins.projected_battery_percent == pytest.approx(battery)
    assert result.safety_margins.projection_confidence == pytest.approx(records[0].projection.projection_confidence)


@pytest.mark.parametrize("scenario", _scenarios(), ids=lambda scenario: scenario.scenario_id)
def test_every_fixture_satisfies_the_expectations_its_own_file_declares(
    engine: DeterministicRiskEngine, scenario: Scenario
) -> None:
    """The scenario's ``expected_safety_core`` block is met, not merely plausible.

    S01 pins a level, S04 pins the hard rule and the drivers it expects, S11 pins
    that nothing fires at all despite a degraded fix. Each of those is an
    expectation the scenario file itself states, so the test reads the file rather
    than restating the table above.
    """
    expected = scenario.expected_safety_core
    assert expected is not None, "every frozen scenario pins an expectation"

    _, records = scenario_chain(engine, scenario)
    for record in records:
        result = record.result
        if expected.risk_level is not None:
            assert result.risk_level.value == expected.risk_level
        assert result.hard_rules_triggered == list(expected.hard_rules_triggered)
        assert set(_rule_ids(result)) >= set(expected.drivers_expected), (
            "a scenario expects at least the drivers it names; extra ones must be explainable"
        )


def test_the_horizon_loop_belongs_to_the_caller_not_the_tool(engine: DeterministicRiskEngine) -> None:
    """Four horizons are four calls: no tool answers for more than one (spec 4.2)."""
    scenario = next(item for item in _scenarios() if item.scenario_id == "S01_clear_day")
    _, records = scenario_chain(engine, scenario)

    assert [record.horizon for record in records] == list(HORIZONS)
    assert {record.result.horizon.minutes for record in records} == set(HORIZONS)
    # Each record is its own call, so its weather row and its projection differ.
    assert len({record.weather.target_time for record in records}) == len(HORIZONS)
    assert len({record.projection.projected_position.lat for record in records}) == len(HORIZONS)

    # And the horizons a rule may be evaluated at are the frozen set.
    for rule in engine.pack.rules:
        assert sorted(horizon.minutes for horizon in rule.applies_to_horizons) == list(HORIZONS)


# --------------------------------------------------------------------------- #
# 2. The six rules, each fired on purpose (spec 7)
# --------------------------------------------------------------------------- #


def test_rule_a_fires_exactly_past_the_off_route_threshold(engine: DeterministicRiskEngine) -> None:
    """R-A is a hard rule: it sets a floor and contributes no points (spec 13)."""
    at_threshold = assess(engine, route_state=route(off_route_distance_m=50.0), device_state=device())
    just_past = assess(engine, route_state=route(off_route_distance_m=50.0001), device_state=device())
    clearly_past = assess(engine, route_state=route(off_route_distance_m=75.0), device_state=device())

    assert _rule_ids(at_threshold) == []
    assert at_threshold.hard_rules_triggered == []
    assert _rule_ids(just_past) == ["R-A"]
    assert _rule_ids(clearly_past) == ["R-A"]
    for result in (just_past, clearly_past):
        assert result.hard_rules_triggered == ["R-A"]
        assert result.risk_score == 0, "a hard rule contributes to no score at all"
        assert result.risk_level == RiskLevel.ORANGE, "HIGH severity floors the level at ORANGE"
        assert result.safety_margins.route_deviation_m == pytest.approx(
            50.0001 if result is just_past else 75.0
        )


def test_rule_b_fires_exactly_below_the_sunset_margin(engine: DeterministicRiskEngine, model) -> None:
    """R-B is margin < 30, and the margin is derived, never read from a field.

    The test builds the sunset instant from an ETA it computes itself, so the
    boundary is a real crossing of the rule's threshold rather than a value copied
    out of a previous run. It also pins that the core derives the margin from the
    two instants and ignores a stale ``minutes_to_sunset`` field.
    """
    steady = device(
        battery_percent=90.0,
        fatigue_index=0.5,
        movement={"current_speed_kmh": 3.0, "avg_speed_10m": 3.0, "avg_speed_30m": 3.0, "pace_drop_percent": 0.0},
    )
    effective = model.effective_speed_kmh(
        model.base_speed_kmh(current_speed_kmh=3.0, avg_speed_10m=3.0, avg_speed_30m=3.0), 0.5
    )
    eta = estimate_minutes(7.15, effective)
    assert eta == 286.0, "7.15 km at 1.5 km/h is 286 minutes"

    def at(margin_offset: float, *, field_minutes: float | None = None) -> Any:
        sunset = ANALYSIS + timedelta(minutes=eta + margin_offset)
        payload = {"sunset_time": sunset.isoformat()}
        if field_minutes is not None:
            payload["minutes_to_sunset"] = field_minutes
        return assess(engine, route_state=route(), device_state=steady, time=time_state(**payload))

    roomy = at(31.0)
    exactly_at = at(30.0)
    just_inside = at(29.999)
    exactly_zero = at(0.0)
    overdue = at(-0.001)

    for result, expected_margin in ((roomy, 31.0), (exactly_at, 30.0), (just_inside, 29.998999999999967), (exactly_zero, 0.0), (overdue, -0.0010000000000331966)):
        assert result.safety_margins.time_margin_min == pytest.approx(expected_margin)

    assert _rule_ids(roomy) == []
    assert _rule_ids(exactly_at) == [], "at exactly the warn threshold the rule does not fire"
    assert _rule_ids(just_inside) == ["R-B"]
    assert _rule_ids(exactly_zero) == ["R-B"]
    assert _rule_ids(overdue) == ["R-B"]

    for result in (just_inside, exactly_zero, overdue):
        assert result.hard_rules_triggered == ["R-B"]
        assert result.risk_score == 0
        assert result.risk_level == RiskLevel.RED, "R-B carries CRITICAL severity and floors at RED"

    warn_only = [driver for driver in just_inside.drivers if driver.rule_id == "R-B"][0]
    assert "time_margin_min_warn" in (warn_only.detail or "")
    assert "time_margin_min_critical" not in (warn_only.detail or "")

    overdue_detail = [driver for driver in overdue.drivers if driver.rule_id == "R-B"][0]
    assert "time_margin_min_warn" in (overdue_detail.detail or "")
    assert "time_margin_min_critical" in (overdue_detail.detail or "")


def test_the_margin_is_derived_from_the_instants_not_from_a_field(
    engine: DeterministicRiskEngine, model
) -> None:
    """A ``minutes_to_sunset`` field that disagrees with the instants is ignored.

    ``time_state`` carries both the sunset instant and a pre-computed minute count.
    Reading the count would make the margin a fact about a stale field; the core
    subtracts the two instants instead, so the reported margin is
    ``minutes_to_sunset(sunset_time) - estimated_minutes_to_finish``.
    """
    eta = estimate_minutes(7.15, model.effective_speed_kmh(3.61, 0.12))
    shifted = assess(
        engine,
        route_state=route(),
        device_state=device(),
        time=time_state(sunset_time="2026-05-18T16:30:00Z", minutes_to_sunset=490.0),
    )
    plain = assess(engine, route_state=route(), device_state=device(), time=time_state(sunset_time="2026-05-18T16:30:00Z"))

    assert shifted.safety_margins.time_margin_min == plain.safety_margins.time_margin_min
    assert shifted.safety_margins.time_margin_min == pytest.approx(360.0 - eta)


def test_rule_c_fires_exactly_past_the_gust_threshold(engine: DeterministicRiskEngine) -> None:
    """R-C reads the *projected* gust: a hazard two hours out still counts (spec 6.2)."""
    below = assess(engine, route_state=route(), device_state=device(), weather=weather_snapshot(wind_gust_kmh=44.999))
    at_threshold = assess(engine, route_state=route(), device_state=device(), weather=weather_snapshot(wind_gust_kmh=45.0))
    above = assess(engine, route_state=route(), device_state=device(), weather=weather_snapshot(wind_gust_kmh=45.0001))
    severe = assess(engine, route_state=route(), device_state=device(), weather=weather_snapshot(wind_gust_kmh=70.0))

    assert _rule_ids(below) == []
    assert _rule_ids(at_threshold) == []
    assert _rule_ids(above) == ["R-C"]
    assert _rule_ids(severe) == ["R-C"]
    for result in (above, severe):
        assert result.hard_rules_triggered == []
        assert result.risk_score == 40, "one HIGH soft rule contributes its severity weight"
        assert result.risk_level == RiskLevel.YELLOW, "40 lands in the YELLOW band"

    driver = severe.drivers[0]
    assert driver.severity.value == "HIGH"
    assert "wind_gust_kmh=70 gt 45" in (driver.detail or "")
    assert "wind_gust_kmh_severe" in (driver.detail or ""), "both bands are reported, not just the first"


def test_rule_d_fires_exactly_past_the_pace_drop_threshold(engine: DeterministicRiskEngine) -> None:
    """R-D is the pace trend; a *gain* reports 0 %, never a negative drop."""
    slower = lambda drop: device(
        battery_percent=90.0,
        fatigue_index=0.5,
        movement={"current_speed_kmh": 1.6, "avg_speed_10m": 1.8, "avg_speed_30m": 3.0, "pace_drop_percent": drop},
    )

    steady = assess(engine, route_state=route(), device_state=slower(15.0))
    dropping = assess(engine, route_state=route(), device_state=slower(15.001))
    collapsed = assess(engine, route_state=route(), device_state=slower(40.0))

    assert _rule_ids(steady) == []
    assert _rule_ids(dropping) == ["R-D"]
    assert _rule_ids(collapsed) == ["R-D"]
    for result in (dropping, collapsed):
        assert result.risk_score == 25, "one MEDIUM soft rule contributes its severity weight"
        assert result.risk_level == RiskLevel.YELLOW

    detail = collapsed.drivers[0].detail or ""
    assert "pace_drop_percent_warn" in detail and "pace_drop_percent_high" in detail


def test_rule_e_fires_on_the_projected_battery_not_the_current_one(engine: DeterministicRiskEngine, model, calibration: Calibration) -> None:
    """R-E is not "battery < 20": it projects to the finish and then compares.

    The construction puts the projected battery *exactly* on the threshold: 1.0 km
    left at 1.5 km/h is 40 minutes, and the calibrated 4 %/h drain removes exactly
    2.6667 % in that time, so a 22.6667 % battery projects to 20.0 % -- at the
    threshold, where the rule must not fire.
    """
    effective = model.effective_speed_kmh(
        model.base_speed_kmh(current_speed_kmh=3.0, avg_speed_10m=3.0, avg_speed_30m=3.0), 0.5
    )
    assert effective == 1.5
    eta = estimate_minutes(1.0, effective)
    assert eta == 40.0
    drain = calibration.battery_drain_percent_per_hour * eta / 60.0
    assert drain == pytest.approx(2.6666666666666665)

    short_leg = route(remaining_distance_km=1.0, remaining_ascent_m=40.0, route_progress=0.98)

    def walking(battery_percent: float) -> DeviceState:
        return device(
            battery_percent=battery_percent,
            fatigue_index=0.5,
            movement={"current_speed_kmh": 3.0, "avg_speed_10m": 3.0, "avg_speed_30m": 3.0, "pace_drop_percent": 0.0},
        )

    exactly_at = assess(engine, route_state=short_leg, device_state=walking(drain + 20.0))
    just_below = assess(engine, route_state=short_leg, device_state=walking(drain + 20.0 - 0.001))
    just_above = assess(engine, route_state=short_leg, device_state=walking(drain + 20.0 + 0.001))

    assert exactly_at.safety_margins.projected_battery_percent == pytest.approx(20.0)
    assert _rule_ids(exactly_at) == [], "at exactly the warn threshold the rule does not fire"
    assert just_below.safety_margins.projected_battery_percent == pytest.approx(19.999)
    assert _rule_ids(just_below) == ["R-E"]
    assert _rule_ids(just_above) == []

    # The current battery is what differs between the last two, and it is nowhere
    # near 20 %: the rule is about the projection.
    assert just_above.safety_margins.projected_battery_percent > 20.0
    driver = just_below.drivers[0]
    assert "projected_battery_percent=19.999 lt 20" in (driver.detail or "")


def test_the_battery_projection_reports_the_route_it_was_projected_over(engine: DeterministicRiskEngine) -> None:
    """The driver's context names the remaining distance and the ETA behind it."""
    slow = device(
        battery_percent=38.0,
        fatigue_index=0.5,
        movement={"current_speed_kmh": 2.0, "avg_speed_10m": 2.7, "avg_speed_30m": 2.8, "pace_drop_percent": 3.6},
    )
    result = assess(engine, route_state=route(), device_state=slow)

    assert _rule_ids(result) == ["R-E"]
    assert result.risk_score == 40
    assert result.safety_margins.projected_battery_percent == pytest.approx(15.915057915057911)
    detail = result.drivers[0].detail or ""
    assert "battery_percent=38" in detail
    assert "remaining_distance_km=7.15" in detail
    assert "network_available=yes" in detail


def test_a_low_battery_on_a_short_route_is_not_a_risk(engine: DeterministicRiskEngine) -> None:
    """The same battery, a short route left, no rule: distance is half the rule."""
    slow = device(
        battery_percent=38.0,
        fatigue_index=0.5,
        movement={"current_speed_kmh": 2.0, "avg_speed_10m": 2.7, "avg_speed_30m": 2.8, "pace_drop_percent": 3.6},
    )
    long_leg = assess(engine, route_state=route(), device_state=slow)
    short_leg = assess(
        engine,
        route_state=route(remaining_distance_km=0.5, remaining_ascent_m=30.0, route_progress=0.95),
        device_state=slow,
    )

    assert long_leg.safety_margins.projected_battery_percent == pytest.approx(15.915057915057911)
    assert _rule_ids(long_leg) == ["R-E"]
    assert short_leg.safety_margins.projected_battery_percent == pytest.approx(36.455598455598455)
    assert _rule_ids(short_leg) == []
    assert short_leg.risk_level == RiskLevel.GREEN


def test_rule_f_needs_two_independent_soft_rules(engine: DeterministicRiskEngine) -> None:
    """One soft rule never compounds; two do, and the detail names both (spec 14)."""
    shrinking = lambda battery: device(
        battery_percent=battery,
        fatigue_index=0.5,
        movement={"current_speed_kmh": 1.6, "avg_speed_10m": 1.8, "avg_speed_30m": 3.0, "pace_drop_percent": 40.0},
    )

    one_rule = assess(engine, route_state=route(), device_state=shrinking(90.0))
    two_rules = assess(engine, route_state=route(), device_state=shrinking(38.0))

    assert _rule_ids(one_rule) == ["R-D"], "a single MEDIUM soft rule is not a compound"
    assert one_rule.hard_rules_triggered == []
    assert one_rule.risk_score == 25
    assert one_rule.risk_level == RiskLevel.YELLOW

    assert _rule_ids(two_rules) == ["R-D", "R-E", "R-F"]
    assert two_rules.risk_score == 65, "R-F adds no points of its own to the total it escalates"
    compound = [driver for driver in two_rules.drivers if driver.rule_id == "R-F"][0]
    assert compound.severity.value == "MEDIUM"
    detail = compound.detail or ""
    assert "R-D(MEDIUM)" in detail and "R-E(HIGH)" in detail
    assert "escalates by 1" in detail


def test_the_compound_rule_escalates_a_level_without_hiding_the_score(engine: DeterministicRiskEngine) -> None:
    """R-F moves the level one step and leaves the score it moved alone (spec 14)."""
    compound_device = device(
        battery_percent=38.0,
        fatigue_index=0.5,
        movement={"current_speed_kmh": 2.0, "avg_speed_10m": 2.1, "avg_speed_30m": 3.5, "pace_drop_percent": 40.0},
    )
    result = assess(engine, route_state=route(), device_state=compound_device)

    # 40 (R-D MEDIUM) + 25 (R-E HIGH) = 65, which is the ORANGE band; one
    # escalation step lands on RED, and the score still says 65.
    assert result.risk_score == 65
    assert result.risk_level == RiskLevel.RED
    assert result.hard_rules_triggered == []


# --------------------------------------------------------------------------- #
# 3. The hard rule overrides a soft band, and never the other way round (spec 13)
# --------------------------------------------------------------------------- #


def test_a_hard_rule_raises_a_band_it_could_not_reach_alone(engine: DeterministicRiskEngine) -> None:
    """A HIGH hard rule floors the level at ORANGE whatever the soft score says.

    The pair is the proof: the same soft rule alone reaches YELLOW with score 25,
    and adding a hard rule leaves the score at 25 while the level becomes ORANGE.
    A floor that lowered a level, or a hard rule that also scored, would break one
    side of this assertion.
    """
    pace_dropping = device(
        battery_percent=90.0,
        fatigue_index=0.5,
        movement={"current_speed_kmh": 1.6, "avg_speed_10m": 1.8, "avg_speed_30m": 3.0, "pace_drop_percent": 40.0},
    )
    soft_only = assess(
        engine,
        route_state=route(remaining_distance_km=0.3, remaining_ascent_m=20.0, route_progress=0.95),
        device_state=pace_dropping,
    )
    with_floor = assess(
        engine,
        route_state=route(off_route_distance_m=75.0, remaining_distance_km=0.3, remaining_ascent_m=20.0, route_progress=0.95),
        device_state=pace_dropping,
    )

    assert _rule_ids(soft_only) == ["R-D"]
    assert soft_only.hard_rules_triggered == []
    assert soft_only.risk_score == 25
    assert soft_only.risk_level == RiskLevel.YELLOW

    assert _rule_ids(with_floor) == ["R-A", "R-D"]
    assert with_floor.hard_rules_triggered == ["R-A"]
    assert with_floor.risk_score == 25, "the hard rule changed the level, not the score"
    assert with_floor.risk_level == RiskLevel.ORANGE


def test_a_hard_rule_never_lowers_a_level(engine: DeterministicRiskEngine) -> None:
    """A CRITICAL hard rule cannot pull a RED back down to anything lower."""
    critical = assess(
        engine,
        route_state=route(off_route_distance_m=75.0),
        device_state=device(
            battery_percent=38.0,
            fatigue_index=0.5,
            movement={"current_speed_kmh": 2.0, "avg_speed_10m": 2.1, "avg_speed_30m": 3.5, "pace_drop_percent": 40.0},
        ),
        time=time_state(
            sunset_time=(ANALYSIS + timedelta(minutes=200.0)).isoformat(),
        ),
    )

    assert set(critical.hard_rules_triggered) == {"R-A", "R-B"}
    assert critical.risk_level == RiskLevel.RED, "the highest floor wins, and no floor may dial a level down"


# --------------------------------------------------------------------------- #
# 4. The escalation is data the engine reads, not a step written in code (spec 14)
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("steps,expected_level", [(0, "ORANGE"), (1, "RED"), (2, "RED")])
def test_the_escalation_is_the_packs_own_number(
    pack: RulePack, calibration: Calibration, steps: int, expected_level: str
) -> None:
    """Re-tuning ``escalation_levels`` moves the level, so the engine reads it.

    ``escalation_levels=0`` is the proving case: the soft score still reaches the
    ORANGE band, but nothing escalates, so the compound rule fires with no visible
    effect. Raising it to 1 turns the same inputs RED, and 2 shows the escalation
    is clipped at the top of the scale rather than running off it.
    """
    payload = pack.model_dump(mode="json")
    for rule in payload["rules"]:
        if rule["id"] == "R-F":
            rule["thresholds"]["escalation_levels"] = steps
    re_tuned = RulePack.model_validate(payload)

    engine = DeterministicRiskEngine(re_tuned, calibration)
    result = assess(
        engine,
        route_state=route(),
        device_state=device(
            battery_percent=38.0,
            fatigue_index=0.5,
            movement={"current_speed_kmh": 2.0, "avg_speed_10m": 2.1, "avg_speed_30m": 3.5, "pace_drop_percent": 40.0},
        ),
    )

    assert _rule_ids(result) == ["R-D", "R-E", "R-F"], "the same rules fire in every variant"
    assert result.risk_score == 65
    assert result.risk_level.value == expected_level
    if steps == 0:
        assert "escalates by 0" in ([driver.detail for driver in result.drivers if driver.rule_id == "R-F"][0] or "")


def test_a_mid_severity_compound_still_counts_a_critical_partner(engine: DeterministicRiskEngine) -> None:
    """R-F's floor is MEDIUM, so a HIGH soft rule qualifies to be counted.

    This is the comparison the severity ordering exists for. ``"HIGH" < "MEDIUM"``
    as strings, so a pack filter compared on the raw member would silently drop the
    HIGH partner and the compound rule would never fire -- which is exactly what
    happened before :attr:`~trailguard.schemas.risk.Severity.rank` existed. The
    scenario-level test above is what catches it.
    """
    from trailguard.schemas.risk import Severity

    assert Severity.HIGH.rank > Severity.MEDIUM.rank
    assert [member.value for member in Severity.LEVELS] == ["LOW", "MEDIUM", "HIGH", "CRITICAL"]

    triple = assess(
        engine,
        route_state=route(),
        device_state=device(
            battery_percent=38.0,
            fatigue_index=0.5,
            movement={"current_speed_kmh": 2.0, "avg_speed_10m": 2.1, "avg_speed_30m": 3.5, "pace_drop_percent": 40.0},
        ),
        weather=weather_snapshot(wind_gust_kmh=70.0),
    )

    assert _rule_ids(triple) == ["R-C", "R-D", "R-E", "R-F"]
    assert triple.risk_score == 100, "HIGH 40 + MEDIUM 25 + HIGH 40 saturate at the ceiling"
    assert triple.risk_level == RiskLevel.RED


# --------------------------------------------------------------------------- #
# 5. The projection: one call, one horizon, an identity the test recomputes
# --------------------------------------------------------------------------- #


@pytest.fixture(scope="module")
def demo_route() -> RouteContext:
    """The S01 hiker's real route context, from the GPX."""
    return get_route_context(ROUTE_ID, START, ANALYSIS)


@pytest.mark.parametrize(
    ("horizon", "progress", "remaining", "latitude", "longitude", "altitude", "arrival", "confidence"),
    [
        (0, 0.34992459777308427, 7.1552653262791, 46.081020196878505, 7.772099264089963, 1378.956638157715, "2026-05-18T10:30:00+00:00", 1.0),
        (30, 0.4942350770931011, 5.5668653262791, 46.0944760998617, 7.7789956017899895, 1478.1755499218734, "2026-05-18T11:00:00+00:00", 0.97),
        (60, 0.6385455564131178, 3.978465326279099, 46.1079131112723, 7.7859344654082765, 1599.6193381736161, "2026-05-18T11:30:00+00:00", 0.94),
        (120, 0.9271665150531514, 0.8016653262790987, 46.13003059636659, 7.81087496495898, 1983.0216128396899, "2026-05-18T12:30:00+00:00", 0.9),
    ],
)
def test_one_call_projects_one_horizon_along_the_real_polyline(
    demo_route: RouteContext,
    horizon: int,
    progress: float,
    remaining: float,
    latitude: float,
    longitude: float,
    altitude: float,
    arrival: str,
    confidence: float,
) -> None:
    """The projection walks the planned polyline geodesically, one horizon at a time."""
    projection = project_route_state(
        route_context=demo_route,
        movement={"current_speed_kmh": 3.5, "avg_speed_10m": 3.6, "avg_speed_30m": 3.7},
        horizon_min=horizon,
        analysis_time=ANALYSIS,
        fatigue_penalty=0.12,
    )

    assert projection.horizon.minutes == horizon
    assert projection.horizon_min == horizon
    assert projection.projected_route_progress == pytest.approx(progress)
    assert projection.projected_remaining_distance_km == pytest.approx(remaining)
    assert projection.projected_position.lat == pytest.approx(latitude)
    assert projection.projected_position.lon == pytest.approx(longitude)
    assert projection.projected_position.alt_m == pytest.approx(altitude)
    assert projection.projected_arrival_time.isoformat() == arrival
    assert projection.projection_confidence == pytest.approx(confidence)


def test_the_projection_and_the_core_agree_about_eta(engine: DeterministicRiskEngine, demo_route: RouteContext) -> None:
    """``time_margin_min`` is the spec's identity, recomputed here independently.

    The test derives the effective speed from the frozen model, walks the
    projection's own remaining distance through ``estimate_minutes``, and checks
    that the core's margin is exactly ``minutes_to_sunset - that ETA`` measured to
    the *projected arrival instant*. At NOW the two instants coincide, which is
    where the plain formula of spec 8 is written.
    """
    scenario = next(item for item in _scenarios() if item.scenario_id == "S01_clear_day")
    _, records = scenario_chain(engine, scenario)

    for record in records:
        projection, result = record.projection, record.result
        effective = engine.movement_model.effective_speed_kmh(
            engine.movement_model.base_speed_kmh(
                current_speed_kmh=3.5, avg_speed_10m=3.6, avg_speed_30m=3.7
            ),
            0.12,
        )
        eta = estimate_minutes(projection.projected_remaining_distance_km, effective)
        assert eta is not None
        to_sunset = minutes_between(
            projection.projected_arrival_time, scenario.initial_context.time_state.sunset_time
        )

        assert result.safety_margins.time_margin_min == pytest.approx(to_sunset - eta)
        # ...and the horizon costs exactly as much as the hiker walks in it,
        # which is why the clear-day margin holds steady across the loop.
        assert result.safety_margins.time_margin_min == pytest.approx(354.85900290331597)


def test_a_projection_beyond_the_routes_end_stops_at_the_end(demo_route: RouteContext) -> None:
    """A finished route projects to the end, not off the map or off the clock."""
    finished = route(route_progress=1.0, remaining_distance_km=0.0, remaining_ascent_m=0.0)

    projection = project_route_state(
        route_context=finished,
        movement={"current_speed_kmh": 3.5, "avg_speed_10m": 3.6, "avg_speed_30m": 3.7},
        horizon_min=30,
        analysis_time=ANALYSIS,
        fatigue_penalty=0.12,
    )

    assert projection.projected_route_progress == 1.0
    assert projection.projected_remaining_distance_km == 0.0
    assert projection.projected_arrival_time == ANALYSIS, "already there: the projection refers to now"
    assert projection.projection_confidence == pytest.approx(0.8924)


def test_a_stationary_hiker_is_not_given_a_finish_time(engine: DeterministicRiskEngine, demo_route: RouteContext) -> None:
    """Zero speed means "no duration divides out", so the core discloses it (spec 10.3)."""
    zero = device(movement={"current_speed_kmh": 0.0, "avg_speed_10m": 0.0, "avg_speed_30m": 0.0, "pace_drop_percent": None})
    result = assess(engine, route_state=demo_route, device_state=zero)

    projection = project_route_state(
        route_context=demo_route,
        movement={"current_speed_kmh": 0.0, "avg_speed_10m": 0.0, "avg_speed_30m": 0.0},
        horizon_min=30,
        analysis_time=ANALYSIS,
    )

    assert result.safety_margins.time_margin_min is None
    assert result.safety_margins.projected_battery_percent is None
    assert result.risk_level == RiskLevel.GREEN, "no rule fires on a feature that is not computable"
    assert _rule_ids(result) == []
    assert "feature:time_margin_min:not_computable" in result.evidence_refs
    assert "feature:estimated_minutes_to_finish:not_computable" in result.evidence_refs
    assert any("time_margin_min is not reported rather than invented" in ref for ref in result.evidence_refs)

    assert projection.projected_arrival_time == ANALYSIS + timedelta(minutes=30)
    assert projection.projected_position.lat == pytest.approx(46.081020196878505)
    assert projection.projected_position.lon == pytest.approx(7.772099264089963)
    assert projection.projected_position.alt_m == pytest.approx(1378.956638157715)
    assert projection.projection_confidence == pytest.approx(0.485), "no fatigue disclosed is charged, not assumed"


def test_a_projection_refuses_a_horizon_that_does_not_exist(demo_route: RouteContext) -> None:
    """One call, one horizon: a fifth horizon is a loud error (spec 4.2, 6.1)."""
    for horizon in (7, 45, 90, -30, 240):
        with pytest.raises(ValueError) as excinfo:
            project_route_state(
                route_context=demo_route,
                movement={"current_speed_kmh": 3.0, "avg_speed_10m": 3.0, "avg_speed_30m": 3.0},
                horizon_min=horizon,
                analysis_time=ANALYSIS,
            )

        assert "not one of the frozen horizons" in str(excinfo.value)


def test_a_projection_refuses_to_read_the_wall_clock(demo_route: RouteContext) -> None:
    """The anchor instant is the caller's; its absence is an error, not a ``now()`` (spec 19)."""
    with pytest.raises(ValueError) as excinfo:
        project_route_state(
            route_context=demo_route,
            movement={"current_speed_kmh": 3.0, "avg_speed_10m": 3.0, "avg_speed_30m": 3.0},
            horizon_min=30,
        )

    assert "analysis_time" in str(excinfo.value)
    assert "system clock is never read" in str(excinfo.value)


def test_a_projection_names_the_route_asset_it_could_not_find() -> None:
    """An unknown route fails loudly and lists where it looked (spec 10.3)."""
    with pytest.raises(RouteAssetError) as excinfo:
        get_route_context("no_such_route", START, ANALYSIS)

    assert "no_such_route" in str(excinfo.value)


# --------------------------------------------------------------------------- #
# 6. Future weather: a hazard at the projected arrival instant (spec 6.2)
# --------------------------------------------------------------------------- #


def _hazard_snapshot(path: Path) -> Any:
    """Write a synthetic-but-valid snapshot: calm now, 70 km/h gusts from 12:00Z.

    Nearest-row-wins and no extrapolation, so the 11:30 arrival reads the 11:00
    row (18 km/h) and the 12:30 arrival reads the 12:00 row (70 km/h).

    Stored in the **open-meteo response shape** -- parallel ``hourly`` arrays,
    ``utc_offset_seconds`` for the naive row instants, ``hourly_units`` naming
    each variable's unit -- because that is the shape the frozen asset
    ``replay/weather/scn_weather_snapshot.json`` uses and the shape the Phase 2
    provider will parse. Writing the fixture in a private shape would test the
    parser's bypass instead of the parser.

    Note the precipitation value: **18, meaning 18 percent.** The loader converts
    it to 0.18 using ``hourly_units``. It is deliberately above R-C's 0.7
    threshold *only after* the /100, which is what the parser is for.

    ``utc_offset_seconds`` is 0 on purpose: this fixture wants its hour labels to
    *be* UTC, which is what the projected arrivals of ``demo_route`` are. The
    frozen asset uses 7200 and naive local rows, so a fixture that copied that
    offset would shift its gusts two hours and green/red the wrong horizons.
    """
    hours = list(range(10, 14))
    document = {
        "snapshot_ref": "replay/weather/test_hazard_fixture.json",
        "source": "replay-hazard-fixture",
        "generated_at": "2026-05-18T10:00:00Z",
        "confidence": 0.85,
        "note": "Synthetic hazard fixture for the R-C test: benign before noon, strong gusts after.",
        "utc_offset_seconds": 0,
        "hourly_units": {
            "time": "iso8601",
            "temperature_2m": "°C",
            "precipitation_probability": "%",
            "wind_speed_10m": "km/h",
            "wind_gusts_10m": "km/h",
        },
        "hourly": {
            "time": [f"2026-05-18T{hour:02d}:00:00" for hour in hours],
            "temperature_2m": [12.0] * len(hours),
            "precipitation_probability": [18] * len(hours),
            "wind_speed_10m": [8.4] * len(hours),
            "wind_gusts_10m": [18.0 if hour <= 11 else 70.0 for hour in hours],
            "weather_code": [1] * len(hours),
        },
    }
    path.write_text(json.dumps(document, indent=2), encoding="utf-8")
    return load_weather_snapshot(path)


def test_a_future_hazard_only_shows_up_when_the_hiker_arrives_in_it(
    engine: DeterministicRiskEngine, demo_route: RouteContext, tmp_path: Path
) -> None:
    """The same hiker, the same snapshot: GREEN for three horizons, YELLOW at +120.

    This is the horizon loop's whole point, in one test: a hazard the hiker is not
    in yet must not be reported as one they are in, and the gusts they *do* reach
    two hours out must be.
    """
    snapshot = _hazard_snapshot(tmp_path / "hazard.json")
    verdicts: dict[int, RiskLevel] = {}

    for horizon in HORIZONS:
        projection = project_route_state(
            route_context=demo_route,
            movement={"current_speed_kmh": 3.5, "avg_speed_10m": 3.6, "avg_speed_30m": 3.7},
            horizon_min=horizon,
            analysis_time=ANALYSIS,
            fatigue_penalty=0.12,
        )
        weather = snapshot.at(projection.projected_arrival_time)
        result = assess(
            engine,
            route_state=demo_route,
            device_state=device(),
            weather=weather,
            projection=projection,
            horizon_min=horizon,
        )

        verdicts[horizon] = result.risk_level
        assert result.safety_margins.weather_freshness_min == pytest.approx(
            (projection.projected_arrival_time - snapshot.generated_at).total_seconds() / 60.0
        )
        if horizon < 120:
            assert _rule_ids(result) == []
            assert weather.wind_gust_kmh == 18.0
        else:
            assert weather.wind_gust_kmh == 70.0, "the 12:30 arrival reads the 12:00 row"
            assert _rule_ids(result) == ["R-C"]
            assert result.risk_score == 40
            driver = result.drivers[0]
            assert driver.severity.value == "HIGH"
            detail = driver.detail or ""
            assert "projected_position" in detail and "projected_arrival_time" in detail
            assert "weather_freshness_min=150" in detail

    assert verdicts == {0: RiskLevel.GREEN, 30: RiskLevel.GREEN, 60: RiskLevel.GREEN, 120: RiskLevel.YELLOW}


def test_weather_that_cannot_be_read_disables_the_weather_rule_rather_than_guessing(
    engine: DeterministicRiskEngine,
) -> None:
    """No snapshot means every weather feature is not computable, and R-C is disclosed."""
    result = assess(engine, route_state=route(), device_state=device(), weather=None)

    assert _rule_ids(result) == []
    assert result.safety_margins.weather_freshness_min is None
    assert result.confidence == pytest.approx(0.835)
    for feature in (
        "temperature_c",
        "wind_speed_kmh",
        "wind_gust_kmh",
        "precipitation_probability",
        "weather_code",
        "weather_data_quality",
        "weather_freshness_min",
    ):
        assert f"feature:{feature}:not_computable" in result.evidence_refs
    assert any("no weather snapshot was supplied" in ref for ref in result.evidence_refs)


def test_a_stale_snapshot_is_used_as_given_and_its_age_reported(engine: DeterministicRiskEngine) -> None:
    """Beyond the freshness budget the values are still used, with the age attached."""
    scenario = next(item for item in _scenarios() if item.scenario_id == "S04_sunset_margin_pace_drop")
    _, records = scenario_chain(engine, scenario)

    for record in records:
        assert record.result.safety_margins.weather_freshness_min == record.weather.freshness_minutes
        if record.weather.freshness_minutes > engine.calibration.weather_freshness_budget_min:
            assert any("beyond the 180-minute demo freshness budget" in ref for ref in record.result.evidence_refs)
    fresh = [record for record in records if record.weather.freshness_minutes <= engine.calibration.weather_freshness_budget_min]
    assert [record.horizon for record in fresh] == [0]


# --------------------------------------------------------------------------- #
# 7. Confidence is arithmetic about the inputs, not a feeling (spec 16)
# --------------------------------------------------------------------------- #


def test_confidence_is_the_calibrations_own_weights_over_the_inputs(
    engine: DeterministicRiskEngine, calibration: Calibration
) -> None:
    """The five terms recomputed here from the calibration, term by term.

    Nothing in this test copies a number out of a run: the weights come from the
    loaded calibration, the inputs from the factories, and the arithmetic is
    written out longhand -- so a re-tuned weight or a changed term shows up as a
    disagreement rather than as a new magic number.
    """
    weights = dict(calibration.confidence_weights)
    age = 1.0 / (1.0 + 30.0 / calibration.weather_freshness_budget_min)
    terms = {
        "route_data_quality": 0.92,
        "device_data_quality": 0.95,
        "weather": age * 0.9,
        "projection": 1.0,
        "reported_overall_quality": 0.93,
    }
    expected = round(sum(weights[name] * value for name, value in terms.items()), 6)

    result = assess(engine, route_state=route(), device_state=device())

    assert result.confidence == pytest.approx(expected)
    assert result.confidence == pytest.approx(0.902857)
    assert f"confidence:{result.confidence}" in result.evidence_refs


def test_worse_data_lowers_confidence_and_never_changes_the_verdict(
    engine: DeterministicRiskEngine,
) -> None:
    """Confidence accompanies a verdict; it never moves one (spec 8, 16)."""
    good = assess(engine, route_state=route(), device_state=device(), quality=report())
    undisclosed = assess(engine, route_state=route(), device_state=device(), quality=None)
    poor = assess(
        engine,
        route_state=route(),
        device_state=device(),
        quality=report(
            gps_quality=None,
            route_data_quality=0.55,
            weather_data_quality=0.35,
            device_data_quality=0.8,
            overall=0.42,
            missing_fields=["gps_quality", "movement.value.pace_drop_percent"],
            stale_fields=["position", "route", "weather"],
        ),
    )

    # Unknown is penalised at 0.5, not assumed perfect: 0.1 + 0.1 + weather + 0.15 + 0.1.
    assert undisclosed.confidence == pytest.approx(0.642857)
    assert poor.confidence == pytest.approx(0.506857)
    assert good.confidence == pytest.approx(0.902857)
    assert poor.confidence < undisclosed.confidence < good.confidence

    for result in (good, undisclosed, poor):
        assert result.risk_level == RiskLevel.GREEN
        assert _rule_ids(result) == []


def test_a_missing_report_still_reports_the_terms_it_could_read(engine: DeterministicRiskEngine) -> None:
    """With nothing disclosed, the route and device terms land on the unknown value."""
    result = assess(engine, route_state=route(), device_state=device(), quality=None)

    assert f"confidence:{result.confidence}" in result.evidence_refs
    assert result.confidence == pytest.approx(0.642857), (
        "0.2*0.5 + 0.2*0.5 + 0.25*(6/7*0.9) + 0.15*1.0 + 0.2*0.5"
    )


def test_a_derived_pace_drop_is_disclosed_as_derived(engine: DeterministicRiskEngine) -> None:
    """A feed that reports no pace drop gets one derived, and says so (spec 10.3)."""
    scenario = next(item for item in _scenarios() if item.scenario_id == "S11_stale_gps_missing_data")
    _, records = scenario_chain(engine, scenario)

    for record in records:
        assert any("pace_drop_percent is derived, not observed" in ref for ref in record.result.evidence_refs)
        assert any("recomputed from the 10- and 30-minute windows as 8.824 %" in ref for ref in record.result.evidence_refs)


def test_a_pace_drop_is_clamped_at_zero_rather_than_reported_negative() -> None:
    """A hiker speeding up reports 0 %, and no drop at all is ``None`` (spec 6.3)."""
    from trailguard.risk_core.movement import derive_pace_drop_percent

    assert derive_pace_drop_percent(3.0, 2.0) == 0.0
    assert derive_pace_drop_percent(1.5, 3.0) == pytest.approx(50.0)
    assert derive_pace_drop_percent(1.0, 0.0) is None
    assert derive_pace_drop_percent(0.0, 0.0) is None


# --------------------------------------------------------------------------- #
# 8. Determinism: the same inputs, the same verdict, every time (spec 19)
# --------------------------------------------------------------------------- #


def test_two_identical_calls_produce_identical_verdicts(engine: DeterministicRiskEngine) -> None:
    """Byte-for-byte, including the evidence trace."""
    first = assess(engine, route_state=route(), device_state=device())
    second = assess(engine, route_state=route(), device_state=device())

    assert _dump(first) == _dump(second)
    assert first.evidence_refs == second.evidence_refs


def test_the_verdict_carries_the_callers_clock_not_the_wall_clock(engine: DeterministicRiskEngine) -> None:
    """``computed_at`` is the analysis instant, so a replay reproduces it exactly."""
    result = assess(engine, route_state=route(), device_state=device())

    assert result.computed_at == time_state().analysis_time
    assert result.computed_at == ANALYSIS
    assert result.computed_at.tzinfo is not None


@pytest.mark.parametrize("scenario", _scenarios(), ids=lambda scenario: scenario.scenario_id)
def test_no_result_ever_carries_a_non_finite_number(engine: DeterministicRiskEngine, scenario: Scenario) -> None:
    """A NaN margin would silently disable a rule, so it may never appear (spec 19)."""

    def scan(node: Any, where: str) -> None:
        if isinstance(node, float):
            assert math.isfinite(node), f"{scenario.scenario_id}: {where} is not finite"
        elif isinstance(node, dict):
            for key, value in node.items():
                scan(value, f"{where}.{key}")
        elif isinstance(node, list):
            for index, value in enumerate(node):
                scan(value, f"{where}[{index}]")

    _, records = scenario_chain(engine, scenario)
    for record in records:
        scan(_dump(record.result), f"{scenario.scenario_id}+{record.horizon}")
        scan(_dump(record.projection), f"{scenario.scenario_id}+{record.horizon}.projection")


def test_the_core_needs_no_model_no_network_and_no_gpu(
    engine: DeterministicRiskEngine, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The chain runs with every model credential removed from the environment.

    Removing the key here *for the duration of the test only* (``monkeypatch``
    restores it) is the honest way to check what spec 20 requires: the
    deterministic core must not need a model. If any part of it did, this call
    would raise rather than return a verdict.
    """
    for name in ("STEPFUN_API_KEY", "ANTHROPIC_API_KEY", "OPENAI_API_KEY"):
        monkeypatch.delenv(name, raising=False)

    result = assess(engine, route_state=route(), device_state=device())

    assert result.risk_level == RiskLevel.GREEN
    assert result.risk_score == 0


# --------------------------------------------------------------------------- #
# 9. The evidence trace: what a reader can check without re-running anything
# --------------------------------------------------------------------------- #


def test_a_quiet_verdict_still_says_what_it_was_checked_against(engine: DeterministicRiskEngine) -> None:
    """GREEN with nothing fired is a result, and its evidence names its inputs."""
    result = assess(engine, route_state=route(), device_state=device())

    refs = result.evidence_refs
    assert "pack:trailguard_rules_v1@1.0.0" in refs
    assert refs[1].startswith("calibration:")
    assert "horizon:NOW" in refs
    assert "soft_score:0" in refs
    assert "risk_level:GREEN" in refs
    assert any(ref.startswith("confidence:") for ref in refs)
    assert result.drivers == []
    assert result.hard_rules_triggered == []


def test_a_noisy_verdict_names_the_rule_the_feature_and_the_band(engine: DeterministicRiskEngine) -> None:
    """Every driver carries the value it compared and the threshold it crossed."""
    result = assess(
        engine,
        route_state=route(off_route_distance_m=75.0, remaining_distance_km=0.3, remaining_ascent_m=20.0, route_progress=0.95),
        device_state=device(
            battery_percent=90.0,
            fatigue_index=0.5,
            movement={"current_speed_kmh": 1.6, "avg_speed_10m": 1.8, "avg_speed_30m": 3.0, "pace_drop_percent": 40.0},
        ),
    )

    assert result.hard_rules_triggered == ["R-A"]
    assert "hard_rules:R-A" in result.evidence_refs
    assert "rule:R-A:off_route_distance_m:off_route_distance_m" in result.evidence_refs

    driver = result.drivers[0]
    assert driver.type == "hard"
    assert "off_route_distance_m=75 gt 50" in (driver.detail or "")
    assert "band warn" in (driver.detail or "")
    assert driver.severity.value == "HIGH"


def test_the_severity_ordering_is_rank_not_the_alphabet(engine: DeterministicRiskEngine) -> None:
    """``HIGH`` is above ``MEDIUM``; if it were not, R-F could never fire.

    The floors are read from the calibration, and they must be ordered like the
    risk scale. A string comparison here would put HIGH below MEDIUM, which is how
    the compound rule was silently disabled before the rank property existed.
    """
    from trailguard.schemas.risk import Severity

    floors = {severity.value: engine.calibration.severity_floor_for(severity) for severity in Severity}
    assert [level.value for level in floors.values()] == ["GREEN", "YELLOW", "ORANGE", "RED"]
    assert floors["HIGH"].rank > floors["MEDIUM"].rank
