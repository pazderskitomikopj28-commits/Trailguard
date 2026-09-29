"""Drive the real TrailGuard chain for one replay scenario.

Spec sections 5, 17. The demo's Replay Mode is not a re-implementation of the
Phase 1-2 chain: it calls the same five atomic tools, in the same order, over the
same frozen GPX, weather snapshot and device feed, with the same engine the tools
build by default. ``evals/phase5/references.py`` does the identical walk to
generate the frozen Deterministic Reference, and a test asserts this module's
output agrees with that committed file horizon by horizon -- so a demo that
started drifting from the benchmark's own reference would fail loudly instead of
showing a prettier number.

Weather is the one choice a caller makes. ``weather_mode="replay"`` is the
default and is what the brief pins for the competition floor: no network, no key,
reproducible. ``"live"`` goes to Open-Meteo and is an explicit opt-in that has to
be labelled in the UI, because a live reading and a replay snapshot are different
evidence. There is deliberately no third option and no fallback: if live weather
cannot be reached, :class:`WeatherUnavailable` propagates and the UI says so.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

from trailguard.risk_core import (
    DeterministicRiskEngine,
    load_calibration,
    load_rule_pack,
)
from trailguard.schemas.route import Horizon
from trailguard.schemas.scenario import Scenario
from trailguard.schemas.weather import WeatherForRouteTime
from trailguard.tools import (
    assess_risk,
    get_route_context,
    get_user_device_context,
    get_weather_for_route_time,
    project_route_state,
)

WeatherMode = Literal["replay", "live"]

#: The frozen four, in canonical order. A scenario declaring anything else is a
#: broken input rather than something to interpret -- the same guard
#: ``evals/phase5/references.py`` applies.
HORIZON_MINUTES: tuple[int, ...] = (0, 30, 60, 120)

#: Prefixed onto any weather failure so a caller cannot mistake a transport
#: problem for an empty result. It says *weather*, and not the name of whatever
#: else happens to be unavailable: the first version of this constant read
#: "Local Agent unavailable", and a live forecast that refused the date then told
#: a judge the local model was down -- which named the wrong broken thing, and a
#: demo whose failure message names the wrong component is a demo nobody can
#: diagnose from the screen.
WEATHER_UNAVAILABLE_PREFIX = "Live weather unavailable"


class WeatherUnavailable(RuntimeError):
    """Live weather could not be reached; the replay snapshot was not substituted.

    Raised rather than swallowed so no caller can quietly fall back to a
    different evidence source than the one it asked for (brief 8, 38).
    """


@dataclass(slots=True)
class HorizonRow:
    """One horizon, already computed, with the objects the UI needs to show it.

    Every field is a value a tool returned. Nothing is derived here except
    ``freshness_ok``, which is the single comparison the harness itself makes
    (``age <= policy.max_freshness_min``) and which the trace records the same
    way.
    """

    label: str
    horizon_min: int
    projected_position_lat: float
    projected_position_lon: float
    projected_alt_m: float | None
    projected_arrival_time: str
    projected_route_progress: float
    projected_remaining_distance_km: float
    projection_confidence: float
    minutes_to_sunset: float
    #: Sunset distance the core used, derived from its own two numbers by the
    #: identity ``tests/test_deterministic_core.py`` pins -- never recomputed here.
    estimated_minutes_to_finish: float
    weather: WeatherForRouteTime
    freshness_ok: bool | None
    #: The engine's own verdict for this horizon. The demo never recomputes it.
    risk_level: str
    risk_score: int
    confidence: float
    hard_rules_triggered: list[str]
    drivers: list[dict[str, Any]]
    safety_margins: dict[str, Any]
    evidence_refs: list[str]


@dataclass(slots=True)
class ReplayRun:
    """The whole deterministic chain for one scenario, ready to present.

    ``weather_mode`` is carried on the result so the UI can label the evidence
    source on every screen without the caller having to remember what it asked
    for.
    """

    scenario_id: str
    description: str
    simulation_note: str
    analysis_time: str
    sunset_time: str
    minutes_to_sunset: float
    rule_pack_id: str
    rule_pack_version: str
    route: dict[str, Any]
    device: dict[str, Any]
    rows: list[HorizonRow]
    weather_mode: WeatherMode
    weather_snapshot_ref: str | None
    #: The policy's own staleness budget, shown beside the reading's real age so
    #: "fresh" is a comparison rather than an adjective.
    freshness_budget_min: float
    data_quality: dict[str, Any]
    provenance: list[str] = field(default_factory=list)

    @property
    def worst_level(self) -> str:
        """The most severe level any horizon reached, by the core's own rank.

        Rank comes from ``RiskLevel`` -- the enum the core uses -- so a display
        order cannot drift from the core's.
        """
        from trailguard.schemas.risk import RiskLevel

        worst = max(self.rows, key=lambda row: RiskLevel(row.risk_level).rank)
        return worst.risk_level

    @property
    def now_level(self) -> str:
        return self.rows[0].risk_level

    @property
    def hazard_labels(self) -> list[str]:
        """Horizons where any rule fired, in canonical order."""
        return [row.label for row in self.rows if row.hard_rules_triggered or row.drivers]


def _policy_for(scenario: Scenario, weather_mode: WeatherMode):
    """The scenario's own policy for replay, a live copy for live.

    The scenario context is never mutated: ``WeatherPolicy`` is a frozen
    contract, and a demo that edited a frozen scenario to get a different
    weather source would be changing the benchmark's inputs.
    """
    policy = scenario.initial_context.weather_policy
    if weather_mode == "replay":
        return policy
    return policy.model_copy(update={"mode": "live"})


def run_replay(scenario: Scenario, *, weather_mode: WeatherMode = "replay") -> ReplayRun:
    """Run the five atomic tools over ``scenario`` and return the view of it.

    The order is the Skill's order -- route, device, then per horizon
    projection, weather and verdict -- which is also the order
    ``evals/phase5/references.py`` uses, so the two produce the same numbers.
    """
    context = scenario.initial_context
    time_state = context.time_state
    policy = _policy_for(scenario, weather_mode)
    analysis_time = time_state.analysis_time

    # ---- tool 1: where the hiker is on the planned route ------------------ #
    route_state = get_route_context(
        context.route.value.route_id, context.position.value, context.timestamp
    )
    # ---- tool 4: what the phone says -------------------------------------- #
    device_state = get_user_device_context("scn_device_" + scenario.scenario_id)

    # The engine is built once from the frozen pack, exactly as the tool builds
    # it by default. No alternative pack, no tuned weights, no second core.
    pack = load_rule_pack(None)
    engine = DeterministicRiskEngine(pack)

    movement = {
        "current_speed_kmh": device_state.movement.current_speed_kmh,
        "avg_speed_10m": device_state.movement.avg_speed_10m,
        "avg_speed_30m": device_state.movement.avg_speed_30m,
    }

    rows: list[HorizonRow] = []
    for label in Horizon.all_horizons():
        minutes = label.minutes

        # ---- tool 2: where they will be, and when ------------------------- #
        projection = project_route_state(
            route_context=route_state,
            movement=movement,
            horizon_min=minutes,
            analysis_time=analysis_time,
            fatigue_penalty=device_state.fatigue_index,
        )

        # ---- tool 3: what the weather is THERE, THEN ---------------------- #
        try:
            weather = get_weather_for_route_time(
                projection.projected_position.lat,
                projection.projected_position.lon,
                projection.projected_arrival_time,
                policy=policy,
                analysis_time=analysis_time,
            )
        except Exception as exc:  # noqa: BLE001 - deliberately re-raised, labelled
            # A live call that cannot be reached is a fact to display. It is never
            # answered from the replay snapshot instead (brief 8).
            raise WeatherUnavailable(
                f"{WEATHER_UNAVAILABLE_PREFIX}: {type(exc).__name__}: {exc}"
            ) from exc

        # ---- tool 5: the deterministic verdict ---------------------------- #
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

        # Not a re-derivation. The core computes
        # time_margin_min == minutes_to_sunset - estimated_minutes_to_finish (the
        # identity tests/test_deterministic_core.py pins), so the ETA the core
        # used is the two numbers it already returned, solved the same way
        # references.py solves it.
        minutes_to_sunset = (
            time_state.sunset_time - analysis_time
        ).total_seconds() / 60.0
        minutes_to_finish = minutes_to_sunset - result.safety_margins.time_margin_min

        rows.append(
            HorizonRow(
                label=label.value,
                horizon_min=minutes,
                projected_position_lat=projection.projected_position.lat,
                projected_position_lon=projection.projected_position.lon,
                projected_alt_m=projection.projected_position.alt_m,
                projected_arrival_time=projection.projected_arrival_time.strftime(
                    "%Y-%m-%dT%H:%M:%SZ"
                ),
                projected_route_progress=projection.projected_route_progress,
                projected_remaining_distance_km=projection.projected_remaining_distance_km,
                projection_confidence=projection.projection_confidence,
                minutes_to_sunset=minutes_to_sunset,
                estimated_minutes_to_finish=minutes_to_finish,
                weather=weather,
                freshness_ok=freshness_ok,
                risk_level=result.risk_level.value,
                risk_score=result.risk_score,
                confidence=result.confidence,
                hard_rules_triggered=list(result.hard_rules_triggered),
                drivers=[
                    {
                        "rule_id": driver.rule_id,
                        "type": driver.type,
                        "severity": driver.severity.value,
                        "detail": driver.detail,
                    }
                    for driver in result.drivers
                ],
                safety_margins=result.safety_margins.model_dump(),
                evidence_refs=list(result.evidence_refs),
            )
        )

    calibration = load_calibration(None)
    return ReplayRun(
        scenario_id=scenario.scenario_id,
        description=scenario.description,
        simulation_note=scenario.simulation_note,
        analysis_time=analysis_time.strftime("%Y-%m-%dT%H:%M:%SZ"),
        sunset_time=time_state.sunset_time.strftime("%Y-%m-%dT%H:%M:%SZ"),
        minutes_to_sunset=(time_state.sunset_time - analysis_time).total_seconds() / 60.0,
        rule_pack_id=pack.pack_id,
        rule_pack_version=str(pack.version),
        route={
            "route_id": route_state.route_id,
            "polyline_ref": route_state.route_polyline_ref,
            "progress": route_state.route_progress,
            "off_route_distance_m": route_state.off_route_distance_m,
            "remaining_distance_km": route_state.remaining_distance_km,
            "remaining_ascent_m": route_state.remaining_ascent_m,
            "current_altitude_m": route_state.current_altitude_m,
            "data_quality": route_state.data_quality,
            "position": {
                "lat": context.position.value.lat,
                "lon": context.position.value.lon,
                "alt_m": context.position.value.alt_m,
                "source": context.position.source,
                "observed_at": context.position.observed_at.strftime("%Y-%m-%dT%H:%M:%SZ"),
                "confidence": context.position.confidence,
            },
        },
        device={
            "device_id": "scn_device_" + scenario.scenario_id,
            "battery_percent": device_state.battery_percent,
            "network_available": device_state.network_available,
            "fatigue_index": device_state.fatigue_index,
            "current_speed_kmh": device_state.movement.current_speed_kmh,
            "avg_speed_10m": device_state.movement.avg_speed_10m,
            "avg_speed_30m": device_state.movement.avg_speed_30m,
            "pace_drop_percent": device_state.movement.pace_drop_percent,
            "data_quality": device_state.data_quality,
        },
        rows=rows,
        weather_mode=weather_mode,
        weather_snapshot_ref=policy.snapshot_ref,
        freshness_budget_min=policy.max_freshness_min,
        data_quality=context.data_quality.model_dump(),
        provenance=[
            f"rule pack {pack.pack_id}@{pack.version} (calibration {calibration.version})",
            "five atomic tools, called directly; no model in the loop",
            f"weather mode {weather_mode}"
            + (f" <- {policy.snapshot_ref}" if weather_mode == "replay" else " <- Open-Meteo"),
        ],
    )
