"""Tool input contracts (spec sections 4, 5).

One pydantic model per tool so the harness can expose a JSON schema and validate
arguments before an implementation is invoked. These mirror the tool signatures
one-to-one; the tool bodies own the semantics.

They live in their own module rather than beside the registry because the
implementations import them: ``trailguard/tools/route_context.py`` binds its
arguments through :func:`~trailguard.tools._arguments.binds_input_contract`, and
a model that lived in ``trailguard/tools/__init__.py`` would make every tool
import the package that imports the tool. :mod:`trailguard.tools` re-exports all
of them, so ``from trailguard.tools import GetRouteContextInput`` keeps working.
"""

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field, field_validator

from ..schemas.context import DeviceState, GeoPoint, RouteContext, TimeState
from ..schemas.route import Horizon, MovementTrend, RouteProjection
from ..schemas.weather import WeatherForRouteTime




class GetRouteContextInput(BaseModel):
    """Input contract for :func:`get_route_context` (spec 5.1)."""

    model_config = ConfigDict(extra="forbid")

    route_id: str = Field(..., description="Identifier of the planned route (GPX asset reference).")
    current_position: GeoPoint = Field(..., description="GPS fix the route position facts are computed against.")
    timestamp: datetime = Field(..., description="ISO8601 instant the fix refers to.")


class ProjectRouteStateInput(BaseModel):
    """Input contract for :func:`project_route_state` (spec 5.2)."""

    model_config = ConfigDict(extra="forbid")

    route_context: RouteContext = Field(..., description="Current route position facts.")
    movement: MovementTrend = Field(..., description="Movement trend the frozen projection model weights.")
    horizon_min: int = Field(..., description="Horizon offset in minutes (0/30/60/120).")
    analysis_time: datetime | None = Field(
        default=None,
        description=(
            "Instant the projection is anchored to (spec 5.2). Optional in the contract because "
            "spec 5.2 freezes exactly three fields; the caller that owns a clock passes it, and its "
            "absence is an error rather than a read of the system clock (spec 19)."
        ),
    )

    @field_validator("horizon_min")
    @classmethod
    def _frozen_horizon_set(cls, value: int) -> int:
        """Reject any offset outside the frozen horizon set (spec 6.1).

        The horizon set (NOW / +30min / +60min / +120min) is frozen for the MVP,
        and every horizon-keyed consumer -- the skill's loop, the scenario's
        ``expected_required_horizons``, the trace's ``horizons_checked`` -- is
        keyed on exactly those four values. An unconstrained ``int`` would let a
        model ask for ``horizon_min=15`` and get a projection nothing downstream
        can record or compare, so the contract refuses it here instead of
        silently answering a question the benchmark cannot score.
        """
        allowed = sorted(member.minutes for member in Horizon)
        if value not in allowed:
            raise ValueError(f"horizon_min must be one of the frozen horizons {allowed} minutes (spec 6.1), got {value}")
        return value


class GetWeatherForRouteTimeInput(BaseModel):
    """Input contract for :func:`get_weather_for_route_time` (spec 5.3)."""

    model_config = ConfigDict(extra="forbid")

    lat: float = Field(..., ge=-90, le=90, description="Query latitude in decimal degrees.")
    lon: float = Field(..., ge=-180, le=180, description="Query longitude in decimal degrees.")
    target_time: datetime = Field(..., description="ISO8601 instant the forecast must describe.")


class GetUserDeviceContextInput(BaseModel):
    """Input contract for :func:`get_user_device_context` (spec 5.4)."""

    model_config = ConfigDict(extra="forbid")

    device_id: str = Field(..., description="Identifier of the hiker device / feed.")


class AssessRiskInput(BaseModel):
    """Input contract for :func:`assess_risk` (spec 5.5).

    The three required states are the caller's *inputs*: spec 5.5 forbids the
    core from projecting or fetching, so the Skill projects and then hands the
    horizon's route state over. ``projected_state`` is the optional mirror of
    that hand-off for the horizon's *projected* route state, without which the
    core could not report the projection-derived safety margins of spec 8
    (``time_margin_min`` from ``projected_arrival_time``,
    ``projection_confidence`` from ``RouteProjection.projection_confidence``) for
    any horizon after NOW. It is optional precisely because the NOW horizon has
    no projection to pass.
    """

    model_config = ConfigDict(extra="forbid")

    route_state: RouteContext = Field(..., description="Route state for the horizon under assessment.")
    weather_state: WeatherForRouteTime = Field(..., description="Weather for the same horizon, with provenance.")
    user_device_state: DeviceState = Field(..., description="Device state for the hiker.")
    time_state: TimeState = Field(..., description="Analysis clock and sunset instant for the time margin.")
    projected_state: RouteProjection | None = Field(
        default=None,
        description=(
            "Projected route state for this horizon, from project_route_state, when one exists (not for NOW). "
            "Supplies the projected arrival time and projection confidence the safety margins of spec 8 report."
        ),
    )


__all__ = [
    "AssessRiskInput",
    "GetRouteContextInput",
    "GetUserDeviceContextInput",
    "GetWeatherForRouteTimeInput",
    "ProjectRouteStateInput",
]
