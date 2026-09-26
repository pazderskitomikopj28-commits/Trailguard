"""Feature extraction: from frozen contracts to the named features rules compare.

The deterministic core never reads a contract directly -- every rule compares a
*named feature* (``time_margin_min``, ``pace_drop_percent``, ...) against a
threshold from the rule pack, and this module is the only place a contract field
becomes such a feature. That is what makes the rule execution in
:mod:`trailguard.risk_core.engine` data-driven instead of a chain of
``if rule_id == ...`` blocks: the clause table says *which* feature to compare,
this module says where that feature comes from, and nothing downstream cares
which is which.

Three rules govern everything extracted here, and they are the whole point of the
module:

1. **a feature that cannot be computed is ``None``, never a default.** ``None``
   means "not computable from the available data", which the engine turns into an
   explicit "this rule could not be evaluated at this horizon" disclosure
   (spec 10.3). A fabricated 0.0 would silently *disable* a threshold, because
   ``0 < 30`` is true and ``0 > 50`` is false -- the direction of the lie depends
   on the comparison, so the core never gets to choose one.
2. **every derived quantity is a pure function of its arguments.** No clock is
   read here: instants are passed in, so the same inputs give the same features in
   every run (spec 19).
3. **derivations are disclosed.** When the core had to compute something the
   feed left blank (a pace drop from the speed windows), the feature vector says
   so in :attr:`FeatureVector.notes`, so a reader can tell an observation from a
   derivation.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Mapping

from ..schemas.context import DeviceState, RouteContext, TimeState
from ..schemas.route import RouteProjection
from ..schemas.weather import WeatherForRouteTime
from .calibration import Calibration
from .movement import MovementModel, derive_pace_drop_percent, estimate_minutes, minutes_between

__all__ = ["FEATURE_NAMES", "FeatureVector", "extract_features"]


#: Every feature this module can produce. A rule's clause may only name a name
#: from here (:meth:`Calibration.validate_against` cannot check this -- it does
#: not know the feature vocabulary -- so the engine checks it at evaluation time).
FEATURE_NAMES: frozenset[str] = frozenset(
    {
        # route (from the route context)
        "off_route_distance_m",
        "remaining_distance_km",
        "remaining_ascent_m",
        "route_progress",
        "route_data_quality",
        "current_altitude_m",
        # movement and the ETA it yields
        "base_speed_kmh",
        "effective_speed_kmh",
        "estimated_minutes_to_finish",
        "pace_drop_percent",
        # daylight
        "minutes_to_sunset",
        "time_margin_min",
        # device
        "battery_percent",
        "fatigue_index",
        "network_available",
        "device_data_quality",
        "minutes_since_network_contact",
        "projected_battery_percent",
        # weather
        "temperature_c",
        "wind_speed_kmh",
        "wind_gust_kmh",
        "precipitation_probability",
        "weather_code",
        "weather_data_quality",
        "weather_freshness_min",
        # assessment context
        "horizon_min",
        "projection_confidence",
    }
)

#: Features that are reported next to a driver but never compared with a
#: threshold. Declared separately because they are referenced by name in the
#: clause table's ``context_features`` and must exist even when they carry no
#: number at all (``projected_position`` is a point, not a measurement).
CONTEXT_FEATURE_NAMES: frozenset[str] = frozenset({"projected_position", "projected_arrival_time"})


@dataclass(frozen=True)
class FeatureVector:
    """The named features one ``assess_risk`` call compares thresholds against.

    ``values`` maps a feature name to its value, or to ``None`` when it is not
    computable from the inputs supplied. ``missing`` is derived from that map (the
    names whose value is ``None``) so a caller cannot forget to disclose one, and
    ``notes`` carries the derivations and the disclosures that have no field of
    their own -- the engine writes both into the risk result's evidence.
    """

    values: Mapping[str, Any] = field(default_factory=dict)
    missing: tuple[str, ...] = ()
    notes: tuple[str, ...] = ()

    def get(self, name: str) -> Any:
        """Value of ``name``, or ``None`` when it is absent or not computable."""
        value = self.values.get(name)
        return value

    def __contains__(self, name: str) -> bool:
        """Whether ``name`` holds a usable (non-``None``) value."""
        return self.values.get(name) is not None

    def number(self, name: str) -> float | None:
        """Value of ``name`` as a float, or ``None`` when it is not numeric."""
        value = self.values.get(name)
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return None
        return float(value)


def extract_features(
    *,
    route_state: RouteContext,
    user_device_state: DeviceState,
    time_state: TimeState,
    movement_model: MovementModel,
    calibration: Calibration,
    projected_state: RouteProjection | None = None,
    weather_state: WeatherForRouteTime | None = None,
    weather_freshness_min: float | None = None,
    horizon_min: int = 0,
) -> FeatureVector:
    """Build the feature vector for one horizon's inputs.

    Args:
        route_state: Route context (progress, off-route distance, remaining
            distance/ascent, current altitude).
        user_device_state: Device snapshot: battery, network, fatigue index, the
            observed movement window and the feed's own quality score.
        time_state: Analysis clock and the day's sunset instant. Both instants
            are read from here; nothing calls ``datetime.now()``.
        movement_model: Frozen movement model supplying the base-speed weights
            and the fatigue cap.
        calibration: Demo-calibration parameters (battery drain, projection
            reliability, freshness budget).
        projected_state: The horizon's projection, when the caller has one. Its
            remaining distance, arrival time and confidence override the
            current-state values, because a rule about +30 minutes must be
            evaluated at +30 minutes, not at now.
        weather_state: Weather snapshot to read hazards from, or ``None`` when
            the caller has none -- every weather feature is then not computable
            and R-C cannot fire.
        weather_freshness_min: Age of the weather snapshot in minutes, when the
            caller has already computed it. Defaults to
            ``weather_state.freshness_minutes``.
        horizon_min: Offset this assessment refers to (0/30/60/120). Used for the
            projection-reliability term, never to look up the wall clock.

    Returns:
        A :class:`FeatureVector` whose ``missing`` and ``notes`` name every
        feature that could not be computed and every derivation the core made.
    """
    # Seeded with every name the module can produce, so ``missing`` is exhaustive:
    # a feature nobody filled in is disclosed as missing rather than simply being
    # absent from the map and therefore invisible to a reader.
    values: dict[str, Any] = dict.fromkeys(sorted(FEATURE_NAMES))
    values.update(dict.fromkeys(sorted(CONTEXT_FEATURE_NAMES)))
    notes: list[str] = []

    # ------------------------------------------------------------------ route
    # The projected remaining distance replaces the current one whenever a
    # projection was supplied: "how far is left" is a question about the instant
    # being assessed, not about now.
    remaining_km = (
        float(projected_state.projected_remaining_distance_km)
        if projected_state is not None
        else float(route_state.remaining_distance_km)
    )
    values.update(
        {
            "off_route_distance_m": float(route_state.off_route_distance_m),
            "remaining_distance_km": remaining_km,
            "remaining_ascent_m": float(route_state.remaining_ascent_m),
            "route_progress": float(route_state.route_progress),
            "route_data_quality": float(route_state.data_quality),
            "current_altitude_m": float(route_state.current_altitude_m),
        }
    )

    # -------------------------------------------------------------- movement
    movement = user_device_state.movement
    base_speed = movement_model.base_speed_kmh(
        current_speed_kmh=movement.current_speed_kmh,
        avg_speed_10m=movement.avg_speed_10m,
        avg_speed_30m=movement.avg_speed_30m,
    )
    effective_speed = movement_model.effective_speed_kmh(base_speed, user_device_state.fatigue_index)
    values["base_speed_kmh"] = base_speed
    values["effective_speed_kmh"] = effective_speed

    # ------------------------------------------------------------------ ETA
    estimated_minutes = estimate_minutes(remaining_km, effective_speed)
    values["estimated_minutes_to_finish"] = estimated_minutes

    # --------------------------------------------------------------- daylight
    # The margin is measured from the instant the assessment refers to: the
    # projected arrival instant when there is a projection, the analysis clock
    # otherwise. At NOW the two coincide, which is why the plain formula
    # minutes_to_sunset - estimated_minutes_to_finish of spec 8 holds for the
    # NOW horizon it is written for.
    assessed_instant = projected_state.projected_arrival_time if projected_state is not None else time_state.analysis_time
    minutes_to_sunset = minutes_between(assessed_instant, time_state.sunset_time)
    values["minutes_to_sunset"] = minutes_to_sunset
    values["time_margin_min"] = (
        None
        if estimated_minutes is None
        else minutes_to_sunset - estimated_minutes
    )
    if estimated_minutes is None:
        notes.append(
            "estimated_minutes_to_finish is not computable: the effective speed is "
            f"{effective_speed:.4g} km/h, so no duration divides out of "
            f"{remaining_km:.4g} km remaining (time_margin_min is not reported rather than invented)"
        )

    # ----------------------------------------------------------------- device
    battery = float(user_device_state.battery_percent)
    values.update(
        {
            "battery_percent": battery,
            "fatigue_index": float(user_device_state.fatigue_index),
            "network_available": bool(user_device_state.network_available),
            "device_data_quality": float(user_device_state.data_quality),
            "minutes_since_network_contact": _minutes_since_network_contact(
                user_device_state, time_state.analysis_time
            ),
        }
    )

    # --- projected battery at the route finish (rule R-E, spec 7)
    projected_battery = _projected_battery_percent(
        battery_percent=battery,
        estimated_minutes=estimated_minutes,
        calibration=calibration,
    )
    values["projected_battery_percent"] = projected_battery
    if estimated_minutes is None:
        notes.append(
            "projected_battery_percent is not computable: with no estimated time to the finish there is "
            "no horizon to project the battery to, so the threshold is disclosed rather than guessed"
        )

    # --- pace drop (rule R-D, spec 7): reported first, derived only as a fallback
    reported_pace_drop = movement.pace_drop_percent
    if reported_pace_drop is not None:
        values["pace_drop_percent"] = float(reported_pace_drop)
    else:
        derived_pace_drop = derive_pace_drop_percent(movement.avg_speed_10m, movement.avg_speed_30m)
        values["pace_drop_percent"] = derived_pace_drop
        if derived_pace_drop is not None:
            notes.append(
                "pace_drop_percent is derived, not observed: the device feed reported none, so it was "
                f"recomputed from the 10- and 30-minute windows as {derived_pace_drop:.4g} %"
            )
        else:
            notes.append(
                "pace_drop_percent is not computable: the 30-minute window carries no usable speed, so "
                "the pace-degradation rule is disclosed as not evaluated rather than assumed zero"
            )

    # ---------------------------------------------------------------- weather
    if weather_state is not None:
        freshness = (
            float(weather_freshness_min)
            if weather_freshness_min is not None
            else float(weather_state.freshness_minutes)
        )
        values.update(
            {
                "temperature_c": float(weather_state.temperature_c),
                "wind_speed_kmh": float(weather_state.wind_speed_kmh),
                "wind_gust_kmh": float(weather_state.wind_gust_kmh),
                "precipitation_probability": float(weather_state.precipitation_probability),
                "weather_code": int(weather_state.weather_code),
                "weather_data_quality": float(weather_state.confidence),
                "weather_freshness_min": freshness,
            }
        )
        status = weather_state.freshness_status(calibration.weather_freshness_budget_min)
        if status.value == "STALE":
            notes.append(
                f"weather snapshot is {freshness:.4g} minutes old, beyond the "
                f"{calibration.weather_freshness_budget_min}-minute demo freshness budget; the hazard "
                "values are used as given and the age is reported alongside them"
            )
        elif status.value == "MISSING":
            notes.append("weather snapshot age is negative, so its freshness cannot be judged")
    else:
        notes.append(
            "no weather snapshot was supplied: every weather feature is not computable and the future "
            "weather rule is disclosed as not evaluated at this horizon"
        )

    # --------------------------------------------------------- the projection
    if projected_state is not None:
        values["projection_confidence"] = float(projected_state.projection_confidence)
        values["projected_position"] = _position_dict(projected_state)
        values["projected_arrival_time"] = projected_state.projected_arrival_time
    elif horizon_min > 0:
        # A claim about a future horizon made without a projection is weaker than
        # the calibration's own reliability for that horizon, so the reliability
        # is used as the term and the absence is disclosed.
        values["projection_confidence"] = calibration.horizon_reliability_for(horizon_min)
        notes.append(
            "no projection was supplied for this future horizon: the horizon's calibrated reliability "
            f"({calibration.horizon_reliability_for(horizon_min):.4g}) stands in for a projection there "
            "is none of"
        )
    values["horizon_min"] = int(horizon_min)

    # Features that are not numbers at all (a point, an instant) live outside
    # ``missing`` by construction: they are reported next to a driver, never
    # compared with a threshold, so "not computable" does not apply to them.
    missing = tuple(
        sorted(name for name, value in values.items() if value is None and name in FEATURE_NAMES)
    )
    return FeatureVector(values=values, missing=missing, notes=tuple(notes))


def _minutes_since_network_contact(device: DeviceState, analysis_time: datetime) -> float | None:
    """Minutes since the last successful network contact, if the feed records one.

    A feed with no ``last_network_time`` has no contact to measure from, which is
    disclosed as missing rather than assumed to be "just now".
    """
    if device.last_network_time is None:
        return None
    return minutes_between(device.last_network_time, analysis_time)


def _projected_battery_percent(
    *,
    battery_percent: float,
    estimated_minutes: float | None,
    calibration: Calibration,
) -> float | None:
    """Battery projected to the route finish at the calibrated drain rate.

    The single calibrated rate is a deliberate limitation, recorded in
    ``calibration_v1.yaml``: the frozen device contract carries no per-device
    drain rate, so one demo rate is used for every device and a caller with a
    real rate builds the engine with a calibration that differs.
    """
    if estimated_minutes is None:
        return None
    hours = max(float(estimated_minutes), 0.0) / 60.0
    projected = battery_percent - calibration.battery_drain_percent_per_hour * hours
    return min(max(projected, calibration.battery_projected_floor), calibration.battery_projected_ceiling)


def _position_dict(projection: RouteProjection) -> dict[str, float | None]:
    """Projected position as a plain mapping, so it can travel as a context value."""
    point = projection.projected_position
    return {"lat": point.lat, "lon": point.lon, "alt_m": point.alt_m}
