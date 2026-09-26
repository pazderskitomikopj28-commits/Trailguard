"""``python -m trailguard.weather_smoke`` -- route-aware weather, run for real (spec 5.3, 15).

Phase 2's demonstration, printed rather than asserted in prose: the same hiker, the
same route and the same weather fixture, asked about at four horizons, where the
weather at the **projected position and projected arrival time** is hazard-free at
NOW and hazardous two hours out::

    route context -> project_route_state(H) -> projected position x projected arrival
                                                      |
                                        get_weather_for_route_time  <- the real tool
                                                      |
                                                assess_risk

What makes it a demonstration of *route-aware* weather is that nothing but the
horizon changes between the four blocks below. The GPX, the device feed, the rule
pack and the fixture are identical throughout; only the position and the instant the
weather was asked about move, and that is enough to turn three GREEN verdicts into a
triggered R-C.

Three things it deliberately is *not*:

* **not a test.** The assertions live in ``tests/test_weather_providers.py``; this
  module prints the same chain so a reviewer can see it, and it never asserts.
* **not a production tool.** The four-horizon walk below is a *demo harness* written
  out in a loop because a terminal is a bad place to hide a sequence (spec 27, 4.2).
  In the Skill the horizon loop belongs to SKILL.md, and each iteration calls the
  single-horizon tool once -- there is no ``forecast_all_horizons()`` here or
  anywhere else in the package.
* **not live and not silent.** It runs in ``mode = "replay"`` against
  ``replay/weather/scn_weather_future_hazard.json``, a synthetic fixture marked as
  such: no socket is opened, no key is read, no model is called. To exercise the
  live Open-Meteo provider, run ``scripts/test_live_weather.py``, which prints
  ``LIVE WEATHER NETWORK UNAVAILABLE`` instead of pretending when it cannot reach
  the API.

Run it from the repository root (or anywhere: the assets are located relative to
this package, not to the working directory).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

from .replay.devices import load_device_feed
from .risk_core.calibration import CALIBRATION_PATH, load_calibration
from .risk_core.engine import DeterministicRiskEngine
from .risk_core.hard_rules import DEFAULT_PACK_PATH, load_rule_pack
from .schemas.route import Horizon
from .schemas.scenario import Scenario
from .tools import assess_risk, get_route_context, project_route_state
from .tools.weather_route_time import get_weather_for_route_time
from .weather import ReplayWeatherProvider, reading_provenance

#: The synthetic demo fixture. A *new* file (spec 25): the frozen Phase 0 asset is
#: never edited to make a demonstration work.
DEMO_SNAPSHOT_REF = "replay/weather/scn_weather_future_hazard.json"

#: The scenario whose projected positions the fixture was written around.
SMOKE_SCENARIO = "S01_clear_day"

#: The horizons the demo walks, in minutes (spec 6.1).
SMOKE_HORIZONS = (0, 30, 60, 120)


class _RememberingReplayProvider(ReplayWeatherProvider):
    """A replay provider that remembers the reading it just returned.

    The tool hands back only the contract object, and a demo wants to print the row,
    the grid reach and the disclosures beside it. Recording at the provider rather
    than calling it twice keeps the run at **one** lookup per horizon -- which is the
    property being demonstrated, so wasting a second lookup to print it would be the
    wrong trade.
    """

    def __init__(self, snapshot_ref: str) -> None:
        super().__init__(snapshot_ref)
        self.last_reading: Any = None

    def get_weather(self, **kwargs: Any) -> Any:
        self.last_reading = super().get_weather(**kwargs)
        return self.last_reading


def _load_scenario(scenario_id: str) -> Scenario:
    """Find and validate the scenario file whose own id is ``scenario_id``.

    The file name is not the id (``scn_clear_day_normal.yaml`` holds
    ``scenario_id: S01_clear_day``), so the lookup reads the field.
    """
    scenarios_dir = Path(__file__).resolve().parents[1] / "scenarios"
    payloads = [
        yaml.safe_load(path.read_text(encoding="utf-8")) for path in sorted(scenarios_dir.rglob("*.yaml"))
    ]
    for payload in payloads:
        if isinstance(payload, dict) and payload.get("scenario_id") == scenario_id:
            return Scenario.model_validate(payload)
    known = sorted(payload.get("scenario_id") for payload in payloads if isinstance(payload, dict))
    raise SystemExit(f"no scenario with id {scenario_id!r} under {scenarios_dir}; known ids: {known}")


def _header(title: str) -> None:
    """A section rule, so the output reads as stages rather than a wall of text."""
    print()
    print(title)
    print("-" * len(title))


def run() -> int:
    """Drive the route-aware weather chain once per horizon and print it."""
    pack = load_rule_pack(DEFAULT_PACK_PATH)
    calibration = load_calibration(CALIBRATION_PATH)
    calibration.validate_against(pack)
    engine = DeterministicRiskEngine(pack, calibration)
    scenario = _load_scenario(SMOKE_SCENARIO)
    context = scenario.initial_context
    analysis_time = context.time_state.analysis_time
    provider = _RememberingReplayProvider(DEMO_SNAPSHOT_REF)

    _header("TrailGuard Phase 2 route-aware weather (replay mode, no LLM, no key, no network)")
    print(f"scenario      : {scenario.scenario_id}")
    print(f"description   : {scenario.description}")
    print(f"analysis time : {analysis_time.isoformat()} (explicit: the system clock is never read)")
    print(f"weather mode  : replay (chosen, not defaulted -- live is the default and needs a policy)")
    print(f"snapshot      : {DEMO_SNAPSHOT_REF} (synthetic competition fixture, not live data)")
    print(f"provider      : {provider.describe()}")
    print()
    print("the chain per horizon, one call each:")
    print("  project_route_state(H) -> projected position x projected arrival")
    print("    -> get_weather_for_route_time(lat, lon, target_time)  <- the tool, one horizon")
    print("      -> assess_risk(...)")

    route_state = get_route_context(
        context.route.value.route_id, context.position.value, context.timestamp
    )
    device_state = load_device_feed("scn_device_" + scenario.scenario_id).current

    _header("current route state (what every horizon projects forward from)")
    print(f"route         : {route_state.route_id} <- {route_state.route_polyline_ref}")
    print(f"position      : lat {context.position.value.lat:.5f} lon {context.position.value.lon:.5f}")
    print(f"progress      : {route_state.route_progress:.6f} of the polyline")
    print(f"remaining     : {route_state.remaining_distance_km:.4f} km, "
          f"{route_state.remaining_ascent_m:.1f} m ascent")

    for horizon_min in SMOKE_HORIZONS:
        horizon = next(item for item in Horizon.all_horizons() if item.minutes == horizon_min)

        projection = project_route_state(
            route_context=route_state,
            movement={
                "current_speed_kmh": device_state.movement.current_speed_kmh,
                "avg_speed_10m": device_state.movement.avg_speed_10m,
                "avg_speed_30m": device_state.movement.avg_speed_30m,
            },
            horizon_min=horizon_min,
            analysis_time=analysis_time,
            fatigue_penalty=device_state.fatigue_index,
        )
        position = projection.projected_position

        weather = get_weather_for_route_time(
            position.lat,
            position.lon,
            projection.projected_arrival_time,
            provider=provider,
            analysis_time=analysis_time,
        )
        # One lookup, through the real tool: the provider kept the reading so the
        # provenance beside the contract object is the same answer, not a second one.
        reading = provider.last_reading
        assert reading is not None
        assert reading.weather is weather

        result = assess_risk(
            route_state=route_state,
            weather_state=weather,
            user_device_state=device_state,
            time_state=context.time_state,
            projected_state=projection,
            horizon_min=horizon_min,
            weather_freshness_min=weather.freshness_minutes,
            data_quality=context.data_quality,
            engine=engine,
        )

        provenance = reading_provenance(reading)
        _header(f"horizon {horizon.value} -- projected position x projected arrival time")
        print(f"Horizon               : {horizon.value} ({horizon_min} min ahead)")
        print(f"Projected Position    : lat {position.lat:.5f} lon {position.lon:.5f} "
              f"at {position.alt_m:.1f} m")
        print(f"Projected Arrival Time: {projection.projected_arrival_time.isoformat()} "
              f"(the instant the weather was asked about)")
        print(f"Weather Source        : {weather.source}")
        print(f"Weather Timestamp     : row {reading.row_time.isoformat()}, "
              f"{reading.reach_minutes:.1f} min from the requested instant; "
              f"generated {weather.forecast_generated_at.isoformat()}")
        print(f"Weather Values        : gust {weather.wind_gust_kmh} km/h, "
              f"wind {weather.wind_speed_kmh} km/h, temp {weather.temperature_c} C, "
              f"precip {weather.precipitation_probability}, code {weather.weather_code}")
        print(f"Freshness             : {weather.freshness_minutes:.0f} min old -> "
              f"{weather.freshness_status(max_freshness_min=context.weather_policy.max_freshness_min).value}")
        print(f"Confidence            : {weather.confidence} "
              f"({'charged: provider declared none' if reading.confidence_is_assumed else 'provider-declared'})")
        print(f"Risk Level            : {result.risk_level.value}  (score {result.risk_score}/100, "
              f"hard rules {result.hard_rules_triggered})")
        if result.drivers:
            for driver in result.drivers:
                print(f"Risk Driver           : {driver.rule_id} [{driver.severity.value}] {driver.type}")
                print(f"                        {driver.detail}")
        else:
            print("Risk Driver           : none -- no rule fired at this horizon")
        for note in reading.note_lines():
            print(f"disclosure            : {note}")
        print(f"grid reach            : {provenance['weather_grid_reach_min']} min")

    _header("what this did and did not do")
    print("the four blocks above are this module's demonstration loop, not a tool: each")
    print("iteration made ONE get_weather_for_route_time call for ONE horizon, and the")
    print("loop itself belongs to SKILL.md (spec 4.2). No forecast_all_horizons() exists.")
    print()
    print("only the horizon changed between the blocks -- same GPX, same device feed, same")
    print("fixture, same rule pack. The verdict changed because the weather was asked about")
    print("at a different position and a different instant, which is the whole of spec 5.3.")
    print()
    print("no LLM was called, no API key was read, no network request was made and no GPU")
    print("was touched. To exercise the live provider, run scripts/test_live_weather.py.")
    return 0


def main() -> int:
    """Entry point for ``python -m trailguard.weather_smoke``."""
    return run()


if __name__ == "__main__":  # pragma: no cover - exercised by the command, not by pytest
    raise SystemExit(main())
