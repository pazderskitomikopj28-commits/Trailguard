"""The weather provider contract (spec 5.3, 17, 23).

Two providers answer the same question -- "what is the weather at *this position*
and *this instant*" -- and they answer it in deliberately different ways:

* :class:`~trailguard.weather.replay.ReplayWeatherProvider` reads a frozen
  Open-Meteo-shaped snapshot. No network, no clock, same bytes every run (spec 17,
  19). The benchmark and the frozen scenarios run this way.
* :class:`~trailguard.weather.open_meteo.OpenMeteoWeatherProvider` asks the live
  API for the target date. Finite timeout, at most one retry, and no fallback:
  when it cannot answer it raises, which is the only behaviour that keeps "live"
  from being a quieter spelling of "made up".

Both return a :class:`WeatherReading`, so a caller cannot accidentally depend on
one provider's shape. The safety core sees only the
:class:`~trailguard.schemas.weather.WeatherForRouteTime` inside it.

What a provider is NOT for
--------------------------
A provider answers a weather question. It does not decide whether the weather is
dangerous (that is rule R-C in the deterministic core), does not loop horizons
(spec 4.2 puts that in the Skill), does not project a position (the caller passes
one in), and does not decide whether a stale snapshot is acceptable (the Skill's
freshness protocol does). A provider that started doing any of those would be a
second, weaker safety core.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Literal

from ..schemas.weather import WeatherForRouteTime

__all__ = [
    "WeatherProvider",
    "WeatherProviderError",
    "WeatherProviderNotConfigured",
    "WeatherProviderResponse",
    "WeatherProviderUnavailable",
    "WeatherReading",
    "WeatherTargetUnavailable",
]

#: The two acquisition modes of spec 17. ``replay`` is a frozen snapshot; ``live``
#: is a real provider call.
WeatherMode = Literal["replay", "live"]


class WeatherProviderError(RuntimeError):
    """A provider could not answer, and the caller must not carry on regardless.

    Deliberately **not** a ``ValueError``: a bad argument is the caller's contracted
    error, while this says "the weather is unavailable", which is a state the caller
    has to handle. Subclasses name which state, and every message says what was
    attempted -- a silent fallback would turn this class into decoration.
    """


class WeatherProviderUnavailable(WeatherProviderError):
    """The provider could not be reached: timeout, DNS failure, connection refused, 5xx.

    The one error a live lookup is allowed to raise for transport reasons, and the
    one that must never be answered by falling back to the replay fixture: a frozen
    snapshot substituted for a live call is a fabricated answer wearing a
    reproducible costume.
    """


class WeatherProviderResponse(WeatherProviderError):
    """The provider answered, but the answer is not usable: malformed JSON, a missing
    hazard variable, an unparseable instant, or a unit this layer will not guess at."""


class WeatherTargetUnavailable(WeatherProviderError):
    """The provider cannot describe this instant at all.

    For the live API this is a date outside the forecast range (the API answers a
    request for e.g. 2019 with HTTP 400 ``start_date is out of allowed range``); for
    replay it is a target time outside the snapshot's own rows.
    """


class WeatherProviderNotConfigured(WeatherProviderError):
    """The caller asked for a provider this build cannot supply: a replay run that
    names no snapshot, or live weather whose transport library is not installed."""


@dataclass(frozen=True)
class WeatherReading:
    """A normalised weather answer plus the provenance a schema field cannot carry.

    The schema object is what the safety core consumes; everything beside it here is
    the disclosure a reviewer (or the Skill's freshness protocol) needs and that the
    frozen ten-field weather contract has no room for:

    * ``reach_minutes`` -- how far the requested instant is from the discrete grid
      row the values actually came from. ``0`` when the request landed exactly on a
      row.
    * ``confidence_is_assumed`` -- whether ``confidence`` is the provider's own
      number or the calibration's unknown value charged in its absence (spec 9).
      Without this flag a 0.5 is indistinguishable from a provider that said 0.5.
    * ``notes`` -- every disclosure that could not become a field: the position is
      N metres from the snapshot's declared point, the live API supplied no model-run
      instant, the spatial coverage check did not run.
    * ``analysis_time`` -- the instant the query belongs to, echoed back so a
      horizon's reading can be read as "asked at T_analysis for T_target".
    """

    weather: WeatherForRouteTime
    provider: str
    mode: WeatherMode
    row_time: datetime | None = None
    reach_minutes: float = 0.0
    confidence_is_assumed: bool = False
    analysis_time: datetime | None = None
    notes: tuple[str, ...] = field(default=())

    def note_lines(self) -> list[str]:
        """The disclosures as a list, for a trace or a printed smoke run."""
        return list(self.notes)


class WeatherProvider(ABC):
    """One weather acquisition mode, answering one (position, instant) question.

    The interface is keyword-only and takes the *projected* position and the
    *projected* arrival instant, because that is the whole point of route-aware
    weather (spec 7, R-C): the hazard is read where the hiker is projected to be,
    not where they are now. A provider that silently substituted the current
    position would make R-C a current-weather rule.
    """

    #: Which mode of spec 17 this provider implements. Declared as a class
    #: attribute so a caller can tell the two apart without isinstance gymnastics.
    mode: WeatherMode = "replay"

    @abstractmethod
    def get_weather(
        self,
        *,
        latitude: float,
        longitude: float,
        target_time: datetime,
        analysis_time: datetime | None = None,
    ) -> WeatherReading:
        """Weather for ``(latitude, longitude)`` at ``target_time``.

        Args:
            latitude, longitude: The position to answer for -- in practice the
                :attr:`~trailguard.schemas.route.RouteProjection.projected_position`
                of the horizon being assessed.
            target_time: The instant to answer for -- in practice the projection's
                ``projected_arrival_time``. Must be timezone-aware; a naive instant
                is refused so it cannot be silently compared against aware rows.
            analysis_time: Optional. The analysis clock the horizon belongs to,
                echoed back in the reading and used by nothing else.

        Returns:
            A :class:`WeatherReading` whose ``weather`` is the contract object.

        Raises:
            WeatherProviderError: a subclass naming why the answer is unavailable.
            Never a substituted or invented answer.
        """

    def describe(self) -> str:
        """One line naming this provider and its mode, for traces and smoke output."""
        return f"{self.mode}:{type(self).__name__}"


def require_aware(value: datetime, where: str) -> None:
    """Refuse a naive instant before it reaches a provider's grid search.

    Shared by both providers so the refusal message is identical whichever path a
    caller takes, and so the check happens before any lookup rather than inside one.
    """
    if value is None or value.tzinfo is None or value.tzinfo.utcoffset(value) is None:
        raise WeatherProviderError(
            f"{where} must be timezone-aware, got {value.isoformat() if value else value!r}; "
            "sunset times, arrivals and weather rows all carry offsets -- an instant without one "
            "cannot be compared with them"
        )


def reading_provenance(reading: WeatherReading) -> dict[str, Any]:
    """A reading flattened to trace-shaped keys (spec 18).

    The evidence trace records weather provenance as a handful of scalars; this is
    the single mapping from a reading to those keys, so the smoke run, the trace and
    a test cannot disagree about what "weather provenance" contains.
    """
    weather = reading.weather
    return {
        "weather_source": weather.source,
        "weather_mode": reading.mode,
        "weather_provider": reading.provider,
        "weather_target_time": weather.target_time.isoformat(),
        "weather_row_time": reading.row_time.isoformat() if reading.row_time else None,
        "weather_grid_reach_min": round(reading.reach_minutes, 6),
        "weather_freshness_min": weather.freshness_minutes,
        "weather_confidence": weather.confidence,
        "weather_confidence_is_assumed": reading.confidence_is_assumed,
        "weather_notes": list(reading.notes),
    }
