"""``get_weather_for_route_time`` tool -- route-aware weather (spec 5.3, 7 R-C).

Phase 0 scope: contract only. The function body is a stub that raises
``NotImplementedError`` naming the phase that owns the implementation.
"""

from datetime import datetime

from ..schemas.weather import WeatherForRouteTime


def get_weather_for_route_time(lat: float, lon: float, target_time: datetime) -> WeatherForRouteTime:
    """Fetch weather for a target position and target time (spec 5.3).

    Responsibilities (spec 5.3):
      * query the weather for the ``(lat, lon) x target_time`` triple, which in
        practice means the *projected* position and projected arrival time of a
        horizon, not the hiker's current location (spec 7, R-C);
      * report provenance: provider ``source``, ``forecast_generated_at`` and
        ``target_time``;
      * report freshness: ``freshness_minutes`` so the caller can run the
        freshness protocol (spec 10.4) and classify the snapshot with
        :meth:`~trailguard.schemas.weather.WeatherForRouteTime.freshness_status`;
      * report provider ``confidence``.

    NOT responsible for (spec 5.3):
      * outputting safety levels -- a ``weather_code`` or ``wind_gust_kmh`` is
        an observation, never a risk level; interpretation is rule R-C's job in
        the deterministic core;
      * deciding whether stale weather is acceptable, or fetching a fresher
        snapshot -- that is the Skill's freshness protocol;
      * inventing, interpolating or best-guessing values when no snapshot
        exists; the call must surface the gap instead.

    Inputs:
      * ``lat``, ``lon`` -- decimal degrees of the query position.
      * ``target_time`` -- ISO8601 instant the forecast must describe.

    Outputs:
      A :class:`~trailguard.schemas.weather.WeatherForRouteTime` with
      temperature, precipitation probability, mean and gust wind speed, weather
      code and full provenance.

    Phase 0 stub: the Open-Meteo provider call and the replay/freshness
    handling belong to Phase 2 (weather and future projection), so this raises
    ``NotImplementedError``.
    """
    raise NotImplementedError("Phase 2: route-aware weather provider lookup (Open-Meteo), freshness reporting")
