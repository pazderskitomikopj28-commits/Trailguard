"""TrailGuard shared contracts (frozen spec v2.1, section 9).

All key data objects are wrapped in :class:`Observed` so that every observation
carries ``value``, ``observed_at``, ``source`` and ``confidence``. That envelope
is what makes freshness checks, uncertainty disclosure and evidence traces
possible; it is not decoration.

Objects defined here:

* :class:`DataFreshnessStatus` -- freshness vocabulary shared by weather and trace.
* :class:`GeoPoint` -- WGS84 point.
* :class:`Observed` -- generic provenance envelope.
* :class:`MovementState` / :class:`MovementTrend` lives in ``route.py``.
* :class:`DeviceState`, :class:`WeatherPolicy`, :class:`RouteContext`,
  :class:`TimeState`, :class:`DataQualityReport`, :class:`TrailGuardContext`.

Phase 0 note: these are plain data contracts. Nothing here computes anything.
"""

from datetime import datetime
from enum import Enum
from typing import Generic, Literal, TypeVar

from pydantic import BaseModel, ConfigDict, Field

T = TypeVar("T")


class DataFreshnessStatus(str, Enum):
    """Freshness verdict for a time-sensitive observation.

    Computed by the weather/provenance layer from the age of the underlying
    snapshot; consumers (skill protocol, confidence model) only read it.
    """

    FRESH = "FRESH"
    STALE = "STALE"
    MISSING = "MISSING"
    UNKNOWN = "UNKNOWN"


class GeoPoint(BaseModel):
    """A WGS84 geographic point.

    Responsibility: carry a position (optionally altitude) with hard bounds so
    that malformed input fails fast at the contract boundary.
    Not responsible for: route geometry, projection or datum conversion.
    """

    model_config = ConfigDict(extra="forbid")

    lat: float = Field(..., ge=-90, le=90, description="Latitude in decimal degrees.")
    lon: float = Field(..., ge=-180, le=180, description="Longitude in decimal degrees.")
    alt_m: float | None = Field(default=None, description="Altitude in meters above sea level, when known.")


class Observed(BaseModel, Generic[T]):
    """Provenance envelope around any single observation.

    Every measurement flows through this wrapper so the agent can answer
    "how old is this, where did it come from, how much do I trust it?".
    Responsibility: provenance and staleness metadata only.
    Not responsible for: interpreting the value, or deciding risk from it.
    """

    model_config = ConfigDict(extra="forbid")

    value: T
    observed_at: datetime = Field(..., description="ISO8601 instant the value was observed.")
    source: str = Field(..., description="Human-readable provenance, e.g. 'gps', 'open-meteo', 'device'.")
    confidence: float = Field(default=1.0, ge=0, le=1, description="Source self-reported confidence in [0, 1].")


class MovementState(BaseModel):
    """Observed hiker movement window (device-derived trend features).

    Responsibility: current and short-window average speeds plus an optional
    pace drop percentage, i.e. what the pace-degradation rule consumes.
    Not responsible for: fatigue interpretation (that is a rule-constructed
    simulated feature on :class:`DeviceState`) or medical inference.
    """

    model_config = ConfigDict(extra="forbid")

    current_speed_kmh: float = Field(..., description="Instantaneous speed in km/h.")
    avg_speed_10m: float = Field(..., description="Average speed over the last 10 minutes (km/h).")
    avg_speed_30m: float = Field(..., description="Average speed over the last 30 minutes (km/h).")
    pace_drop_percent: float | None = Field(
        default=None,
        description="Percent drop of the 10-minute average below the 30-minute average, when derivable.",
    )


class DeviceState(BaseModel):
    """Device and hiker-state snapshot from the (simulated) phone/watch feed.

    Responsibility: the whole device feed of spec 5.4 -- battery, connectivity,
    the fatigue index used by the frozen movement model, the observed
    :class:`MovementState` window that the pace-degradation rule and the
    projection model consume, and the feed's own ``data_quality`` score.
    Not responsible for: medical assessment.

    ``fatigue_index`` is a rule-constructed simulated feature produced by the
    demo rules (see ``rules/rules_v1.yaml``). It is NOT a medical model output
    and must never be presented as one (spec sections 5.4, 22).

    Two deliberate duplications live in this contract, both mandated by spec 5.4:

    * ``movement`` repeats the movement window the shared
      :class:`TrailGuardContext` already carries under ``movement`` -- the tool
      output has to stand on its own in the transcript;
    * ``data_quality`` is *this feed's* self-assessment, which is not the same
      number as ``DataQualityReport.device_data_quality`` computed for the whole
      context snapshot (one is a source claim, the other an aggregation).
    """

    model_config = ConfigDict(extra="forbid")

    battery_percent: float = Field(..., ge=0, le=100, description="Remaining battery in percent.")
    network_available: bool = Field(..., description="Whether a usable network link is currently available.")
    fatigue_index: float = Field(
        ...,
        ge=0,
        le=1,
        description=(
            "Rule-constructed simulated fatigue feature in [0, 1]; not a medical model output."
        ),
    )
    movement: MovementState = Field(
        ...,
        description=(
            "Observed movement window (current/10-minute/30-minute speeds and pace drop) that the "
            "frozen movement model of spec 6.3 and the pace-degradation rule consume."
        ),
    )
    data_quality: float = Field(
        ...,
        ge=0,
        le=1,
        description="Quality of the device feed itself in [0, 1]; not a risk score.",
    )
    last_network_time: datetime | None = Field(
        default=None, description="ISO8601 instant of the last successful network contact, if any."
    )


class WeatherPolicy(BaseModel):
    """How weather may be obtained for this run.

    Responsibility: pin the weather acquisition mode (replay vs live), the
    freshness budget and the preferred provider so the skill and the
    benchmark behave deterministically.
    Not responsible for: fetching weather or judging hazard levels.
    """

    model_config = ConfigDict(extra="forbid")

    mode: Literal["replay", "live"] = Field(
        ..., description="'replay' uses a frozen snapshot for reproducibility, 'live' calls a provider."
    )
    max_freshness_min: int = Field(
        default=180,
        description=(
            "Maximum tolerated age of a weather snapshot in minutes. "
            "source_type=competition_demo_calibration, calibration=true: the 180-minute default is a "
            "conservative demo placeholder tuned for this benchmark, NOT an external standard or a "
            "meteorological validity limit."
        ),
    )
    snapshot_ref: str | None = Field(default=None, description="Replay snapshot reference when mode == 'replay'.")
    preferred_source: str = Field(default="open-meteo", description="Preferred weather provider identifier.")


class RouteContext(BaseModel):
    """Static-to-current route position facts.

    Responsibility: route progress, off-route distance, remaining
    distance/ascent, current altitude and a pointer to the route polyline.
    Not responsible for: risk levels, weather or future projection
    (projection lives in ``tools/route_projection.py``).
    """

    model_config = ConfigDict(extra="forbid")

    route_id: str = Field(..., description="Identifier of the planned route.")
    route_progress: float = Field(..., ge=0, le=1, description="Fraction of the route already completed in [0, 1].")
    off_route_distance_m: float = Field(..., ge=0, description="Distance from the planned polyline in meters.")
    remaining_distance_km: float = Field(..., ge=0, description="Remaining route distance in kilometers.")
    remaining_ascent_m: float = Field(..., ge=0, description="Remaining ascent in meters.")
    current_altitude_m: float = Field(..., description="Current altitude in meters.")
    route_polyline_ref: str = Field(..., description="Reference to the route geometry (GPX/polyline asset).")
    data_quality: float = Field(..., ge=0, le=1, description="Route data quality score in [0, 1].")


class TimeState(BaseModel):
    """Analysis clock and the day's sunset instant.

    Responsibility: ``analysis_time``, ``sunset_time`` and (optionally) the
    ``minutes_to_sunset`` margin. Not responsible for: computing the estimated
    finish time or the sunset margin -- that is Phase 1 deterministic core work.
    """

    model_config = ConfigDict(extra="forbid")

    analysis_time: datetime = Field(..., description="ISO8601 instant the analysis is anchored to.")
    sunset_time: datetime = Field(..., description="ISO8601 sunset instant for the route area.")
    minutes_to_sunset: float | None = Field(
        default=None, description="Minutes from analysis_time to sunset_time, when already computed."
    )


class DataQualityReport(BaseModel):
    """Aggregated, field-level data quality for one context snapshot.

    Responsibility: report per-source quality, an overall score and which
    fields are missing or stale. Not responsible for: deciding what to do about
    missing data -- that is the skill's missing-data protocol (spec 10.3).
    """

    model_config = ConfigDict(extra="forbid")

    gps_quality: float | None = Field(default=None, description="GPS quality in [0, 1] when a fix exists.")
    route_data_quality: float = Field(..., description="Route data quality in [0, 1].")
    weather_data_quality: float | None = Field(default=None, description="Weather data quality in [0, 1] when available.")
    device_data_quality: float = Field(..., description="Device data quality in [0, 1].")
    overall: float = Field(..., ge=0, le=1, description="Overall data quality in [0, 1].")
    missing_fields: list[str] = []
    stale_fields: list[str] = []


class TrailGuardContext(BaseModel):
    """The ONE shared context snapshot (spec section 9).

    Responsibility: assemble everything the Agent, the Tools and the Benchmark
    need at a single instant -- position, route, movement, device, time, weather
    policy and data quality -- so that both skill conditions see byte-identical
    input. Not responsible for: deriving future states, weather or risk; it is
    the current-state snapshot only.
    """

    model_config = ConfigDict(extra="forbid")

    scenario_id: str = Field(..., description="Scenario this context belongs to, or 'live'.")
    timestamp: datetime = Field(..., description="ISO8601 instant this snapshot was frozen at.")
    position: Observed[GeoPoint]
    route: Observed[RouteContext]
    movement: Observed[MovementState]
    device: Observed[DeviceState]
    time_state: TimeState
    weather_policy: WeatherPolicy
    data_quality: DataQualityReport
