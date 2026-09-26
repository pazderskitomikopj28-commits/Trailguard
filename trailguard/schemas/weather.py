"""Weather contract for a projected position and time (spec sections 5.3, 7 R-C).

Route-aware weather is the whole point of R-C: the hazard is judged at the
*projected* position and *projected* arrival time, not at the hiker's current
location. This module only describes the returned data, never its danger.
"""

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

from .context import DataFreshnessStatus


class WeatherForRouteTime(BaseModel):
    """Weather observation for one (latitude, longitude, target_time) triple.

    Responsibility: carry the raw hazard features plus full provenance --
    provider, forecast generation instant, target instant, age in minutes and a
    confidence score -- so the skill can run the freshness protocol (spec 10.4)
    before using the values.
    Not responsible for: safety levels, hazard scoring or policy decisions.
    A ``weather_code`` or ``wind_gust_kmh`` is an observation, never a risk
    level; interpreting it is R-C's job in the deterministic core.

    ``freshness_status`` classifies the age of the snapshot against a caller
    supplied budget; it does not fetch or refresh anything.
    """

    model_config = ConfigDict(extra="forbid")

    temperature_c: float = Field(..., description="Air temperature in degrees Celsius at the target time.")
    precipitation_probability: float = Field(..., ge=0, le=1, description="Precipitation probability in [0, 1].")
    wind_speed_kmh: float = Field(..., ge=0, description="Mean wind speed in km/h.")
    wind_gust_kmh: float = Field(..., ge=0, description="Wind gust speed in km/h.")
    weather_code: int = Field(..., description="Provider weather code (e.g. WMO code for open-meteo).")
    source: str = Field(..., description="Provider identifier the observation came from.")
    forecast_generated_at: datetime = Field(..., description="ISO8601 instant the provider produced this forecast.")
    target_time: datetime = Field(..., description="ISO8601 instant the forecast describes.")
    freshness_minutes: float = Field(..., ge=0, description="Age of the forecast in minutes at use time.")
    confidence: float = Field(..., ge=0, le=1, description="Provider confidence in [0, 1].")
    #: Whether ``confidence`` is the provider's own number or the calibration's
    #: unknown value charged in its absence (spec 9). ``False`` means the provider
    #: declared the figure; ``True`` means it did not and the number is a charged
    #: unknown, not a claim. Without this flag a 0.5 the provider asserted is
    #: indistinguishable from a 0.5 nobody earned, and "Open-Meteo confidence 0.9"
    #: could quietly become a constant.
    confidence_is_assumed: bool = Field(
        default=False, description="True when the provider declared no confidence and the value is charged."
    )

    def freshness_status(self, max_freshness_min: int) -> DataFreshnessStatus:
        """Classify this snapshot against ``max_freshness_min``.

        Responsibility: pure comparison -- FRESH when the age is within budget,
        STALE when it exceeds it, UNKNOWN when the *budget* itself is unusable
        (a negative budget is a configuration error, not a verdict), MISSING when
        the *age* cannot be judged, i.e. the age is negative and therefore is not
        an age any producer could have reported.
        Not responsible for: fetching fresher weather or deciding whether to
        continue the analysis; the skill's freshness protocol does that.

        MISSING and UNKNOWN are deliberately distinct (spec 10.4): MISSING says
        "there is no usable snapshot age here", UNKNOWN says "there is an age but
        no budget to judge it against". A caller that hides both behind one code
        cannot report the honesty the skill's freshness protocol owes.

        The ``freshness_minutes`` field is declared ``ge=0``, so a negative age
        can only reach this method through a value validated before that
        constraint existed; it is handled rather than trusted.
        """
        if max_freshness_min < 0:
            # A negative budget is a configuration error: "not checkable", which
            # is not the same claim as "there is no snapshot".
            return DataFreshnessStatus.UNKNOWN
        if self.freshness_minutes < 0:
            return DataFreshnessStatus.MISSING
        if self.freshness_minutes > float(max_freshness_min):
            return DataFreshnessStatus.STALE
        return DataFreshnessStatus.FRESH
