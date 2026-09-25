"""Route and future-projection contracts (spec sections 6, 5.2).

The projection objects are the heart of TrailGuard's "forward-looking" claim:
they express a conditional future state, never an accident probability
(spec 6.2 -- the banned phrasing is "in 120 minutes the accident probability is
82%"; what is predicted is a *Projected Risk State*).
"""

from datetime import datetime
from enum import Enum

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .context import GeoPoint


class Horizon(str, Enum):
    """Fixed analysis horizons (spec 6.1): NOW, +30min, +60min, +120min.

    The horizon set is frozen for the MVP. ``assess_risk`` is always called
    once per horizon; the 30/60/120 loop itself belongs to the Skill, never to
    a single tool (spec 4.2).
    """

    NOW = "NOW"
    PLUS_30 = "+30min"
    PLUS_60 = "+60min"
    PLUS_120 = "+120min"

    @property
    def minutes(self) -> int:
        """Offset of this horizon from the analysis instant, in minutes."""
        return _HORIZON_MINUTES[self.value]

    @classmethod
    def all_horizons(cls) -> list["Horizon"]:
        """All four frozen horizons in canonical order."""
        return [cls.NOW, cls.PLUS_30, cls.PLUS_60, cls.PLUS_120]


_HORIZON_MINUTES: dict[str, int] = {
    Horizon.NOW.value: 0,
    Horizon.PLUS_30.value: 30,
    Horizon.PLUS_60.value: 60,
    Horizon.PLUS_120.value: 120,
}


class MovementTrend(BaseModel):
    """Movement input for a projection (projection input only).

    Responsibility: pass the speed trend the frozen movement model weights
    (spec 6.3). Not responsible for: device context -- battery, network and the
    fatigue index are deliberately excluded here; they enter through the
    risk core where the fatigue penalty is applied and reported.

    Honest note on the three speeds: this contract declares exactly the same
    three numeric fields as :class:`MovementState` minus its optional
    ``pace_drop_percent``. What separates the two is *where they are used* -- the
    ``MovementTrend`` is the argument of ``project_route_state`` and the
    ``MovementState`` is the observed device/context value -- not a difference in
    what they carry.
    """

    model_config = ConfigDict(extra="forbid")

    current_speed_kmh: float = Field(..., description="Instantaneous speed in km/h.")
    avg_speed_10m: float = Field(..., description="Average speed over the last 10 minutes (km/h).")
    avg_speed_30m: float = Field(..., description="Average speed over the last 30 minutes (km/h).")


class RouteProjection(BaseModel):
    """Conditional future route state for one horizon (spec 6.2).

    Responsibility: describe where the hiker *would* be, and when, if they keep
    moving at the currently observable trend along the current route.
    Not responsible for: probability of any event, weather, or risk level --
    this is a projected state, never a Projected Risk State value scored as an
    accident probability. ``projection_confidence`` is a data-quality-flavoured
    trust score, not a likelihood of harm.
    """

    model_config = ConfigDict(extra="forbid")

    horizon: Horizon = Field(..., description="Which frozen horizon this projection belongs to.")
    horizon_min: int = Field(..., description="Offset from the analysis instant in minutes (0/30/60/120).")
    projected_route_progress: float = Field(..., ge=0, le=1, description="Projected route completion fraction.")
    projected_position: GeoPoint = Field(..., description="Projected point on the route at the horizon.")
    projected_arrival_time: datetime = Field(..., description="ISO8601 instant the projection refers to.")
    projected_remaining_distance_km: float = Field(
        ..., ge=0, description="Route distance still remaining at the horizon, in kilometers."
    )
    projection_confidence: float = Field(
        ..., ge=0, le=1, description="Trust score for this projection, driven by input data quality."
    )

    @model_validator(mode="after")
    def _horizon_min_matches_the_horizon(self) -> "RouteProjection":
        """Reject a projection whose two horizon spellings disagree.

        ``horizon_min`` is redundant with ``horizon`` (spec 6.1 fixes the
        offsets), and a redundant field that is allowed to drift silently breaks
        every horizon keyed lookup downstream. Responsibility: keep the pair
        consistent. Not responsible for: computing the projection itself (that
        is Phase 1 deterministic core work).
        """
        if self.horizon_min != self.horizon.minutes:
            raise ValueError(
                f"horizon_min={self.horizon_min} disagrees with horizon={self.horizon.value!r} "
                f"(expected {self.horizon.minutes}); the two spellings of the same horizon must match"
            )
        return self
