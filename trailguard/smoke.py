"""``python -m trailguard.smoke`` -- the Phase 1 deterministic chain, run for real.

This is the cheapest possible end-to-end proof that Phase 1 exists: it drives the
whole chain the Skill drives, against the real shipped assets, with no LLM, no key
and no network, and prints what came back::

    real GPX -> route context -> projection -> replay weather -> risk verdict

Three things it deliberately is *not*:

* **not a test.** The assertions live in ``tests/test_deterministic_core.py``; this
  module runs the same chain so a reviewer can see output, and it never asserts.
* **not hardcoded.** Every number printed is read out of the assets or computed
  during this run -- there is no literal latitude, progress or verdict in the
  source. If the chain changed, the output changes with it.
* **not the 30/60/120 loop.** That belongs to the Skill (spec 4.2); this module
  runs it for one scenario to show a loop is *possible*, and says so in its header.

Run it from the repository root (or anywhere: the assets are located relative to
this package, not to the working directory).
"""

from __future__ import annotations

from pathlib import Path

import yaml

from .replay.devices import load_device_feed
from .replay.weather import load_weather_snapshot
from .risk_core.calibration import CALIBRATION_PATH, load_calibration
from .risk_core.engine import DeterministicRiskEngine
from .risk_core.hard_rules import DEFAULT_PACK_PATH, load_rule_pack
from .schemas.scenario import Scenario
from .schemas.route import Horizon
from .tools import assess_risk, get_route_context, project_route_state

#: The scenario the smoke run drives. Chosen because it is the one that makes every
#: part of the chain visible: it is mid-route, its pace has dropped, its battery is
#: low and its sunset margin is already negative.
SMOKE_SCENARIO = "S04_sunset_margin_pace_drop"

#: The horizons the smoke run walks, in minutes (spec 6.1).
SMOKE_HORIZONS = (0, 30, 60, 120)


def _load_scenario(scenario_id: str) -> Scenario:
    """Find and validate the scenario file whose own id is ``scenario_id``.

    The file name is not the id (``scn_sunset_margin_pace_drop.yaml`` holds
    ``scenario_id: S04_sunset_margin_pace_drop``), so the lookup reads the field
    rather than guessing at a stem.
    """
    scenarios_dir = Path(__file__).resolve().parents[1] / "scenarios"
    for path in sorted(scenarios_dir.rglob("*.yaml")):
        payload = yaml.safe_load(path.read_text(encoding="utf-8"))
        if isinstance(payload, dict) and payload.get("scenario_id") == scenario_id:
            return Scenario.model_validate(payload)
    known = sorted(
        payload.get("scenario_id")
        for payload in (yaml.safe_load(path.read_text(encoding="utf-8")) for path in scenarios_dir.rglob("*.yaml"))
        if isinstance(payload, dict)
    )
    raise SystemExit(f"no scenario with id {scenario_id!r} under {scenarios_dir}; known ids: {known}")


def _header(title: str) -> None:
    """A section rule, so the output reads as stages rather than a wall of text."""
    print()
    print(title)
    print("-" * len(title))


def run() -> int:
    """Drive the chain once and print every stage. Returns a process exit code."""
    # ---------------------------------------------------------------- load
    pack = load_rule_pack(DEFAULT_PACK_PATH)
    calibration = load_calibration(CALIBRATION_PATH)
    calibration.validate_against(pack)
    engine = DeterministicRiskEngine(pack, calibration)
    scenario = _load_scenario(SMOKE_SCENARIO)
    context = scenario.initial_context
    analysis_time = context.time_state.analysis_time

    _header("TrailGuard Phase 1 deterministic core (no LLM, no key, no network)")
    print(f"scenario      : {scenario.scenario_id}")
    print(f"description   : {scenario.description}")
    print(f"analysis time : {analysis_time.isoformat()} (explicit: the system clock is never read)")
    print(f"sunset        : {context.time_state.sunset_time.isoformat()}")
    print(f"rule pack     : {pack.pack_id}@{pack.version} ({len(pack.rules)} rules, status={pack.status})")
    print(f"calibration   : @{calibration.version} ({len(calibration.evaluations)} clause tables)")
    print(f"movement      : base_speed weights {dict(calibration.base_speed_weights)}, "
          f"fatigue penalty cap {calibration.fatigue_penalty_cap}")

    # --------------------------------------------------------------- route
    route_state = get_route_context(
        context.route.value.route_id, context.position.value, context.timestamp
    )
    _header("1. route context (get_route_context over the real GPX)")
    print(f"route         : {route_state.route_id} <- {route_state.route_polyline_ref}")
    print(f"position      : lat {context.position.value.lat:.5f} lon {context.position.value.lon:.5f}")
    print(f"progress      : {route_state.route_progress:.6f} of the polyline")
    print(f"remaining     : {route_state.remaining_distance_km:.4f} km, {route_state.remaining_ascent_m:.1f} m ascent")
    print(f"off route     : {route_state.off_route_distance_m:.3f} m")
    print(f"altitude      : {route_state.current_altitude_m:.1f} m")
    print(f"data quality  : {route_state.data_quality}")

    # -------------------------------------------------------------- device
    device_state = load_device_feed("scn_device_" + scenario.scenario_id).current
    _header("2. device context (get_user_device_context over the replay feed)")
    print(f"feed          : scn_device_{scenario.scenario_id}")
    print(f"battery       : {device_state.battery_percent} %")
    print(f"network       : {device_state.network_available}")
    print(f"fatigue index : {device_state.fatigue_index} (simulated, never a medical measurement)")
    print(f"speeds km/h   : current {device_state.movement.current_speed_kmh}, "
          f"10m {device_state.movement.avg_speed_10m}, 30m {device_state.movement.avg_speed_30m}")
    print(f"data quality  : {device_state.data_quality}")

    snapshot = load_weather_snapshot()

    # ------------------------------------------------------------ horizons
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
        weather = snapshot.at(projection.projected_arrival_time)
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

        _header(f"3. horizon {horizon.value} (project_route_state -> weather at arrival -> assess_risk)")
        print(f"projected to  : lat {projection.projected_position.lat:.5f} lon {projection.projected_position.lon:.5f} "
              f"at {projection.projected_position.alt_m:.1f} m")
        print(f"progress      : {projection.projected_route_progress:.6f}")
        print(f"remaining     : {projection.projected_remaining_distance_km:.4f} km")
        print(f"arrival       : {projection.projected_arrival_time.isoformat()} "
              f"(the instant this projection refers to)")
        print(f"proj. trust   : {projection.projection_confidence}")
        print(f"weather       : {weather.target_time.isoformat()} gust {weather.wind_gust_kmh} km/h, "
              f"precip {weather.precipitation_probability}, {weather.freshness_minutes:.0f} min old")
        print(f"time margin   : {result.safety_margins.time_margin_min:.4f} min "
              f"(minutes_to_sunset - estimated_minutes_to_finish)")
        print(f"projected batt: {result.safety_margins.projected_battery_percent:.4f} %")
        print()
        print(f"  RISK        : {result.risk_level.value}  score {result.risk_score}/100  "
              f"hard rules {result.hard_rules_triggered}  confidence {result.confidence}")
        for driver in result.drivers:
            print(f"    - {driver.rule_id} [{driver.severity.value}] {driver.type}")
            print(f"      {driver.detail}")
        if not result.drivers:
            print("    - no rule fired")
        disclosed = [ref for ref in result.evidence_refs if ref.startswith(("note:", "feature:"))]
        for ref in disclosed:
            print(f"    disclosed: {ref}")

    _header("what this did not do")
    print("no LLM was called, no API key was read, no network request was made, and no")
    print("GPU was touched: the safety core is deterministic arithmetic over the assets.")
    print("the 30/60/120 loop above is this module's demonstration only -- in the Skill")
    print("it belongs to SKILL.md, never to a tool (spec 4.2).")
    return 0


def main() -> int:
    """Entry point for ``python -m trailguard.smoke``."""
    return run()


if __name__ == "__main__":  # pragma: no cover - exercised by the command, not by pytest
    raise SystemExit(main())
