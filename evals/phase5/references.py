"""Deterministic reference generation for the Phase 5 benchmark.

WHAT THIS PRODUCES
==================
One JSON file per benchmark scenario under ``evals/phase5/references/`` holding
the answer the *real* deterministic safety core gives, at each of the four
frozen horizons, obtained by calling the five atomic tools a model would be
given -- ``get_route_context``, ``project_route_state``,
``get_weather_for_route_time``, ``get_user_device_context``, ``assess_risk`` --
in the order the TrailGuard Skill prescribes.

WHO ANSWERS IT
==============
No language model, at any point. There is no expected answer in this package
that a model generated, judged, paraphrased or approved. Every number below is
the arithmetic of the frozen rule pack, the frozen calibration, the frozen
movement model and the frozen replay assets, reached through the public tool
contract. A reviewer who disagrees with an expected answer does not need a model
to settle it: they can re-run this script and read the tool outputs themselves.

WHY IT IS NOT INLINE IN THE EVALUATOR
=====================================
The evaluator scores a *run* against a *reference*, and the reference has to
exist before the first scored run is ever made -- which is the whole point of
pre-registration. Keeping the generator separate also keeps the evaluator honest:
it cannot quietly recompute "expected" to match what it just observed, because
that file is already committed and hashed in the manifest.

DETERMINISM
===========
``analysis_time`` comes from the scenario, never from the wall clock, and every
asset is read from its committed path. Re-running this script rewrites the same
bytes; ``--check`` proves it by regenerating into a temporary directory and
diffing, so "deterministic" is a checked claim rather than a vocabulary word.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))

from trailguard.risk_core.calibration import CALIBRATION_PATH, load_calibration  # noqa: E402
from trailguard.risk_core.engine import DeterministicRiskEngine  # noqa: E402
from trailguard.risk_core.hard_rules import DEFAULT_PACK_PATH, load_rule_pack  # noqa: E402
from trailguard.routes.geometry import load_route_geometry  # noqa: E402
from trailguard.schemas.route import Horizon  # noqa: E402
from trailguard.schemas.scenario import Scenario  # noqa: E402
from trailguard.tools import (  # noqa: E402
    assess_risk,
    get_route_context,
    get_user_device_context,
    get_weather_for_route_time,
    project_route_state,
)

BENCHMARK_VERSION = "phase5-benchmark-v1"
REFERENCES_DIR = Path(__file__).resolve().parent / "references"

#: The scenarios this benchmark runs. S01 / S04 / S11 are the three frozen Phase 2
#: scenarios reused unchanged (S04 for its hard R-B rule and its stale weather,
#: S11 for missing data); B01..B11 were authored for this phase and live in the
#: same three partitions so no frozen schema or test had to move.
BENCHMARK_SCENARIOS: tuple[str, ...] = (
    "S01_clear_day",                       # safe control 1
    "B01_mid_route_all_clear",             # safe control 2
    "B02_gps_drift_warn",                 # R-A
    "B03_route_deviation_under_threshold",  # R-A false-alarm probe
    "B04_tight_sunset_margin",            # R-B, compact
    "S04_sunset_margin_pace_drop",        # R-B stress (reused)
    "B05_late_gust_hazard",               # R-C late hazard
    "B06_future_gust_front",              # R-C NOW-safe / future-hazardous
    "B07_pace_drop_warn",                 # R-D
    "B08_gust_band_clearing",             # R-C band that clears
    "B09_battery_shortfall",              # R-E
    "B10_compound_soft_pair",             # R-F
    "B11_multi_driver_storm",             # multi-driver
    "S11_stale_gps_missing_data",         # missing / degraded (reused)
)

#: How a frozen ``Horizon`` label maps to the minute offset the tools take.
#: Read off the enum itself rather than restated, so a fifth horizon could not
#: silently appear in one place and not the other.
HORIZON_MINUTES: dict[str, int] = {h.value: h.minutes for h in Horizon.all_horizons()}


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _iso(instant: datetime) -> str:
    return instant.astimezone(tz=None).utcnow().strftime("%Y-%m-%dT%H:%M:%SZ") if instant.tzinfo is None \
        else instant.astimezone(instant.tzinfo).strftime("%Y-%m-%dT%H:%M:%SZ")


def load_scenarios() -> list[tuple[Path, Scenario]]:
    """Every benchmark scenario with the file it was validated from.

    The path is carried rather than re-derived from the id: the three reused
    Phase 2 scenarios are named for their subject (``scn_clear_day_normal.yaml``)
    and not for their benchmark id, so guessing the filename from the id would
    silently hash nothing.
    """
    found: dict[str, tuple[Path, Scenario]] = {}
    for path in sorted((REPO_ROOT / "scenarios").rglob("scn_*.yaml")):
        scenario = Scenario.model_validate(yaml.safe_load(path.read_text(encoding="utf-8")))
        found[scenario.scenario_id] = (path, scenario)
    missing = [sid for sid in BENCHMARK_SCENARIOS if sid not in found]
    if missing:
        raise SystemExit(f"benchmark names {len(missing)} scenario(s) that do not exist: {missing}")
    return [found[sid] for sid in BENCHMARK_SCENARIOS]


def _movement_trend(device) -> dict[str, float]:
    """The three speed fields the projection's frozen contract accepts.

    ``pace_drop_percent`` is deliberately excluded: ``project_route_state``
    refuses it, because the projection is about speed over ground and the pace
    drop is the rule's own reading of that trend.
    """
    return {
        "current_speed_kmh": device.movement.current_speed_kmh,
        "avg_speed_10m": device.movement.avg_speed_10m,
        "avg_speed_30m": device.movement.avg_speed_30m,
    }


def build_reference(scenario_path: Path, scenario: Scenario) -> dict[str, Any]:
    """Run the five atomic tools for one scenario and return its reference."""
    context = scenario.initial_context
    policy = context.weather_policy
    time_state = context.time_state

    # --- tool 1: where is the hiker on the planned route -------------------- #
    route_state = get_route_context(
        context.route.value.route_id, context.position.value, context.timestamp
    )

    # --- tool 4: what does the phone/watch say ------------------------------- #
    device_state = get_user_device_context("scn_device_" + scenario.scenario_id)

    # The horizon set is the frozen four, and a scenario that declares anything
    # else is a broken input rather than something to interpret: M4 scores a run
    # against the frozen horizons, not against whatever one fixture happened to
    # list.
    frozen_horizons = [h.value for h in Horizon.all_horizons()]
    declared = [h.value for h in scenario.expected_required_horizons]
    if declared != frozen_horizons:
        raise SystemExit(
            f"{scenario.scenario_id} declares {declared} but the benchmark is frozen on {frozen_horizons}"
        )

    # The engine is built once per scenario from the frozen pack, exactly as the
    # tool itself builds it by default -- no alternative pack, no tuned weights.
    pack = load_rule_pack(None)
    engine = DeterministicRiskEngine(pack)

    horizons: dict[str, Any] = {}
    for label in frozen_horizons:
        minutes = HORIZON_MINUTES[label]

        # --- tool 2: where will they be, and when --------------------------- #
        projection = project_route_state(
            route_context=route_state,
            movement=_movement_trend(device_state),
            horizon_min=minutes,
            analysis_time=time_state.analysis_time,
            fatigue_penalty=device_state.fatigue_index,
        )

        # --- tool 3: what is the weather THERE, THEN ------------------------ #
        weather = get_weather_for_route_time(
            projection.projected_position.lat,
            projection.projected_position.lon,
            projection.projected_arrival_time,
            policy=policy,
            analysis_time=time_state.analysis_time,
        )

        # --- tool 5: the deterministic verdict ------------------------------ #
        result = assess_risk(
            route_state=route_state,
            weather_state=weather,
            user_device_state=device_state,
            time_state=time_state,
            projected_state=projection,
            horizon_min=minutes,
            weather_freshness_min=weather.freshness_minutes,
            data_quality=context.data_quality,
            engine=engine,
        )

        freshness_ok: bool | None = None
        if weather.freshness_minutes is not None:
            freshness_ok = weather.freshness_minutes <= policy.max_freshness_min

        # The frozen ETA identity, solved the other way round so the reference
        # carries the ETA the core actually used without re-deriving it: the core
        # computes time_margin_min == minutes_to_sunset - estimated_minutes_to_finish
        # (pinned by tests/test_deterministic_core.py), so the ETA is the two
        # numbers the reference already has. Recomputed here, not invented.
        minutes_to_sunset = (time_state.sunset_time - time_state.analysis_time).total_seconds() / 60.0
        eta = minutes_to_sunset - result.safety_margins.time_margin_min

        horizons[str(label)] = {
            "horizon_min": minutes,
            "projected_arrival_time": projection.projected_arrival_time.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "projected_position": {
                "lat": projection.projected_position.lat,
                "lon": projection.projected_position.lon,
                "alt_m": projection.projected_position.alt_m,
            },
            "minutes_to_sunset": minutes_to_sunset,
            "estimated_minutes_to_finish": eta,
            "projected_route_progress": projection.projected_route_progress,
            "remaining_distance_km": projection.projected_remaining_distance_km,
            "projection_confidence": projection.projection_confidence,
            "weather": {
                "source": weather.source,
                "forecast_generated_at": weather.forecast_generated_at.strftime("%Y-%m-%dT%H:%M:%SZ"),
                "wind_gust_kmh": weather.wind_gust_kmh,
                "wind_speed_kmh": weather.wind_speed_kmh,
                "temperature_c": weather.temperature_c,
                "precipitation_probability": weather.precipitation_probability,
                "weather_code": weather.weather_code,
                "freshness_minutes": weather.freshness_minutes,
                "freshness_ok": freshness_ok,
                "freshness_budget_min": policy.max_freshness_min,
            },
            "risk": {
                "level": result.risk_level.value,
                "score": result.risk_score,
                "hard_rules_triggered": list(result.hard_rules_triggered),
                "drivers": [
                    {
                        "rule_id": driver.rule_id,
                        "type": driver.type,
                        "severity": _severity(driver),
                        "detail": driver.detail,
                    }
                    for driver in result.drivers
                ],
                "confidence": result.confidence,
                "evidence_refs": list(result.evidence_refs),
            },
            "safety_margins": {
                "time_margin_min": result.safety_margins.time_margin_min,
                "projected_battery_percent": result.safety_margins.projected_battery_percent,
                "route_deviation_m": result.safety_margins.route_deviation_m,
                "weather_freshness_min": result.safety_margins.weather_freshness_min,
                "projection_confidence": result.safety_margins.projection_confidence,
            },
        }

    levels = [entry["risk"]["level"] for entry in horizons.values()]
    drivers = sorted({d["rule_id"] for entry in horizons.values() for d in entry["risk"]["drivers"]})
    hard_rules = sorted({r for entry in horizons.values() for r in entry["risk"]["hard_rules_triggered"]})

    # Which horizon a hazard first appears at, and whether it clears again. The
    # evaluator uses these to know where a hazard exists without re-deriving them.
    # A hazard *clears* when it is present at some horizon and absent at a later
    # one -- not merely when the first and last hazard horizons differ, which is
    # also true of a hazard that runs to the end. The distinction matters because
    # B08 (a gust band that clears) and B04 (a hazard that never clears) would
    # otherwise be indistinguishable, and the two need opposite reviews.
    order = list(horizons)
    hazard_at = [label for label, entry in horizons.items()
                 if entry["risk"]["drivers"] or entry["risk"]["hard_rules_triggered"]]
    safe_at = [label for label in order if label not in hazard_at]
    first_hazard = hazard_at[0] if hazard_at else None
    last_hazard = hazard_at[-1] if hazard_at else None
    settles = bool(hazard_at) and bool(safe_at) and order.index(first_hazard) > 0
    clears = bool(hazard_at) and bool(safe_at) and order.index(last_hazard) < len(order) - 1

    return {
        "benchmark_version": BENCHMARK_VERSION,
        "generator": "evals/phase5/references.py",
        "llm_involved": False,
        "deterministic": True,
        "scenario_id": scenario.scenario_id,
        "category": scenario.category.value,
        "scenario_file": _rel(scenario_path),
        "scenario_sha256": _sha256(scenario_path),
        "route_ref": scenario.route_ref,
        "route_sha256": _sha256(REPO_ROOT / scenario.route_ref),
        "device_feed_ref": _rel(REPO_ROOT / "replay" / "devices"
                                / f"scn_device_{scenario.scenario_id}.json"),
        "device_feed_sha256": _sha256(REPO_ROOT / "replay" / "devices"
                                      / f"scn_device_{scenario.scenario_id}.json"),
        "weather_snapshot_ref": policy.snapshot_ref,
        "weather_snapshot_sha256": _sha256(REPO_ROOT / policy.snapshot_ref),
        "rules_sha256": _sha256(DEFAULT_PACK_PATH),
        "calibration_sha256": _sha256(CALIBRATION_PATH),
        "rule_pack_id": pack.pack_id,
        "calibration_pack_id": load_calibration().pack_id,
        "analysis_time": time_state.analysis_time.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "sunset_time": time_state.sunset_time.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "freshness_budget_min": policy.max_freshness_min,
        "required_tools": [t.value for t in scenario.expected_required_tools],
        "required_horizons": [h.value for h in scenario.expected_required_horizons],
        "route_context": {
            "route_id": route_state.route_id,
            "route_progress": route_state.route_progress,
            "off_route_distance_m": route_state.off_route_distance_m,
            "remaining_distance_km": route_state.remaining_distance_km,
            "remaining_ascent_m": route_state.remaining_ascent_m,
            "current_altitude_m": route_state.current_altitude_m,
            "data_quality": route_state.data_quality,
        },
        "device_context": {
            "battery_percent": device_state.battery_percent,
            "network_available": device_state.network_available,
            "fatigue_index": device_state.fatigue_index,
            "movement": _movement_trend(device_state),
            "pace_drop_percent": device_state.movement.pace_drop_percent,
        },
        "horizons": horizons,
        "coverage": {
            "levels": levels,
            "levels_are_uniform": len(set(levels)) == 1,
            "uniform_level": levels[0] if len(set(levels)) == 1 else None,
            "hard_rules": hard_rules,
            "drivers": drivers,
            "hazard_at": hazard_at,
            "first_hazard_horizon": first_hazard,
            "last_hazard_horizon": last_hazard,
            "hazard_settles_later_horizon": settles,
            "hazard_clears_again": clears,
            "missing_data": [
                f"{path}"
                for path in sorted({*(context.data_quality.missing_fields or []),
                                    *(scenario.expected_safety_core and [])})
                if isinstance(path, str)
            ],
            "stale_fields": list(context.data_quality.stale_fields or []),
            "weather_is_stale_at": [label for label, entry in horizons.items()
                                    if entry["weather"]["freshness_ok"] is False],
            # What the context says is absent or out of date, as field dot-paths.
            # M-unsupported-claim and the missing-data protocol read these: a run
            # that fills one of these gaps with an invented value has not been
            # conservative, it has been wrong.
            "missing_data": list(context.data_quality.missing_fields or []),
            "stale_fields": list(context.data_quality.stale_fields or []),
        },
        "expectations_for_evaluator": {
            # What a correct run must report, per horizon. The evaluator compares
            # a run's tool-obtained evidence against these; it never invents them.
            "per_horizon_level": {label: horizons[label]["risk"]["level"] for label in horizons},
            "per_horizon_hard_rules": {
                label: horizons[label]["risk"]["hard_rules_triggered"] for label in horizons
            },
            "per_horizon_drivers": {
                label: [d["rule_id"] for d in horizons[label]["risk"]["drivers"]] for label in horizons
            },
            "union_drivers": drivers,
            "union_hard_rules": hard_rules,
            "worst_level": _worst_level(levels),
            "safe_horizons": [label for label, entry in horizons.items()
                              if not entry["risk"]["drivers"]
                              and not entry["risk"]["hard_rules_triggered"]],
            "false_alarm_probe": _is_false_alarm_probe(drivers, hard_rules),
            "late_hazard_probe": bool(settles and not clears),
            "clearing_hazard_probe": clears,
        },
    }


def _severity(driver: Any) -> str:
    value = getattr(driver.severity, "value", driver.severity)
    return str(value)


def _level_rank(level: str) -> int:
    order = ["GREEN", "YELLOW", "ORANGE", "RED", "INSUFFICIENT_DATA", "ERROR"]
    return order.index(level) if level in order else -1


def _worst_level(levels: list[str]) -> str:
    return max(levels, key=_level_rank) if levels else None


def _is_false_alarm_probe(drivers: list[str], hard_rules: list[str]) -> bool:
    """True when nothing fires anywhere: any rule a run names here is invented."""
    return not drivers and not hard_rules


def _rel(path: Path) -> str:
    try:
        return str(path.relative_to(REPO_ROOT))
    except ValueError:
        return str(path)


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _canonical(document: dict[str, Any]) -> str:
    return json.dumps(document, indent=2, sort_keys=True, ensure_ascii=False) + "\n"


def write_references(target_dir: Path) -> list[Path]:
    target_dir.mkdir(parents=True, exist_ok=True)
    written = []
    for scenario_path, scenario in load_scenarios():
        reference = build_reference(scenario_path, scenario)
        path = target_dir / f"{scenario.scenario_id}.json"
        path.write_text(_canonical(reference), encoding="utf-8")
        written.append(path)
    return written


def check_references() -> int:
    """Regenerate into a scratch directory and diff against the committed files."""
    import tempfile

    with tempfile.TemporaryDirectory() as scratch:
        fresh = write_references(Path(scratch))
        drift = []
        for path in fresh:
            committed = REFERENCES_DIR / path.name
            if not committed.is_file():
                drift.append(f"{path.name}: MISSING from {REFERENCES_DIR}")
            elif committed.read_text(encoding="utf-8") != path.read_text(encoding="utf-8"):
                drift.append(f"{path.name}: DIFFERS from the committed reference")
    if drift:
        print("DETERMINISM CHECK FAILED")
        for line in drift:
            print("  " + line)
        return 1
    print(f"deterministic: {len(fresh)} references regenerate byte-for-byte")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--check", action="store_true",
                        help="verify the committed references regenerate identically, write nothing")
    args = parser.parse_args()

    if args.check:
        return check_references()

    for scenario_path, scenario in load_scenarios():
        reference = build_reference(scenario_path, scenario)
        path = REFERENCES_DIR / f"{scenario.scenario_id}.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(_canonical(reference), encoding="utf-8")
        cov = reference["coverage"]
        print(f"{scenario.scenario_id:38s} {','.join(cov['levels']):34s} "
              f"hard={cov['hard_rules']} drivers={cov['drivers']}")
    print(f"\n{len(BENCHMARK_SCENARIOS)} references written to {REFERENCES_DIR}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
