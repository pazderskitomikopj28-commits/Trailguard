"""``get_weather_for_route_time`` tool -- route-aware weather (spec 5.3, 7 R-C).

Phase 2 gives this tool a real body. It answers one question -- *what is the weather
at this position, at this instant* -- and it answers it for the position and instant
the caller hands it, which in the Skill's horizon loop are always the horizon's
projected position and projected arrival time (spec 7, R-C)::

    route context -> project_route_state(H) -> projected_position x projected_arrival
                                                        |
                                          get_weather_for_route_time  <- this tool
                                                        |
                                            Deterministic Safety Core

That is the specific way TrailGuard differs from a hiking app that checks the
weather where you are standing. The hazard in R-C is judged **where the hiker is
projected to be**, and the only way to get that right is to ask for the projected
position at the projected arrival -- never to reuse the current position's weather
and re-label it for a future horizon.

Mode selection
--------------
``policy`` names the acquisition mode (spec 17). ``mode = "replay"`` reads the frozen
snapshot the policy names; ``mode = "live"`` asks Open-Meteo. When **neither** a
policy nor a provider is supplied the tool goes live: replay is only used when the
caller chose it, so a frozen fixture can never be substituted for a real question by
accident. A caller that wants determinism passes ``WeatherPolicy(mode="replay",
snapshot_ref=...)``; a caller that wants neither opens nothing and gets an error.

The declared input contract is unchanged -- ``lat``, ``lon``, ``target_time`` -- so
the agent path (spec 15) still validates the same three arguments it did in Phase 0.
``policy``, ``provider`` and ``analysis_time`` are keyword-only extras that the
contract does not declare: the argument binder passes non-contract keywords through
untouched, which is what lets a Python caller supply the mode without widening the
model-facing schema.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from ..schemas.context import WeatherPolicy
from ..schemas.weather import WeatherForRouteTime
from ._arguments import binds_input_contract
from .contracts import GetWeatherForRouteTimeInput
from ..weather import provider_for_policy
from ..weather.base import WeatherProvider, WeatherReading

__all__ = ["weather_for_projection"]


@binds_input_contract(GetWeatherForRouteTimeInput)
def get_weather_for_route_time(
    lat: float,
    lon: float,
    target_time: datetime,
    *,
    policy: WeatherPolicy | None = None,
    provider: WeatherProvider | None = None,
    analysis_time: datetime | None = None,
) -> WeatherForRouteTime:
    """Fetch weather for a target position and target time (spec 5.3).

    Responsibilities (spec 5.3):
      * query the weather for the ``(lat, lon) x target_time`` triple, which in
        practice means the *projected* position and projected arrival time of a
        horizon, not the hiker's current location (spec 7, R-C);
      * report provenance: provider ``source``, ``forecast_generated_at`` and
        ``target_time``, plus whether ``confidence`` is the provider's own figure or
        a charged unknown (``confidence_is_assumed``);
      * report freshness: ``freshness_minutes`` so the caller can run the freshness
        protocol (spec 10.4) and classify the snapshot with
        :meth:`~trailguard.schemas.weather.WeatherForRouteTime.freshness_status`.

    NOT responsible for (spec 5.3):
      * outputting safety levels -- a ``weather_code`` or ``wind_gust_kmh`` is an
        observation, never a risk level; interpretation is rule R-C's job in the
        deterministic core;
      * deciding whether stale weather is acceptable, or fetching a fresher snapshot
        -- that is the Skill's freshness protocol;
      * inventing, interpolating or best-guessing values when no snapshot exists; the
        call must surface the gap instead;
      * projecting a position, running the safety core, or walking the 30/60/120
        loop -- one call answers one horizon (spec 4.2).

    Args:
        lat: Decimal degrees of the query position.
        lon: Decimal degrees of the query position.
        target_time: The ISO8601 instant the weather must describe. Timezone-aware;
            a naive instant is refused rather than compared.
        policy: How weather may be obtained. ``None`` with no ``provider`` means
            live Open-Meteo.
        provider: An already-built provider. Takes precedence over ``policy``, which
            is how a test or a demo pins a mode without touching the policy schema.
        analysis_time: The horizon's analysis clock, recorded in the reading.

    Returns:
        A :class:`~trailguard.schemas.weather.WeatherForRouteTime`.

    Raises:
        WeatherProviderError: the provider could not answer -- a timeout, a 5xx, a
            malformed response, a target time outside the provider's range, or a
            replay policy that names no snapshot. No failure substitutes another
            mode's answer.
    """
    resolved = provider if provider is not None else _provider_for(policy)
    reading: WeatherReading = resolved.get_weather(
        latitude=float(lat),
        longitude=float(lon),
        target_time=target_time,
        analysis_time=analysis_time,
    )
    return reading.weather


def _provider_for(policy: WeatherPolicy | None) -> WeatherProvider:
    """The provider for a policy, or the documented live default.

    No policy means live: the caller asked for nothing and the real capability is
    the real one. Choosing replay has to be an act, which is what spec 17 requires
    and what makes "the benchmark used a frozen fixture" an auditable statement
    rather than something that happened quietly.
    """
    if policy is None:
        from ..weather.open_meteo import OpenMeteoWeatherProvider

        return OpenMeteoWeatherProvider()
    return provider_for_policy(policy)


def weather_for_projection(
    projection: Any,
    *,
    policy: WeatherPolicy | None = None,
    provider: WeatherProvider | None = None,
    analysis_time: datetime | None = None,
) -> WeatherForRouteTime:
    """Weather for **one** horizon, taken from that horizon's own projection.

    A wiring helper, not a tool: it exists so the "position comes from the
    projection, never re-guessed" rule (spec 7 R-C) is written once instead of being
    re-derived at every call site. It reads ``projection.projected_position`` and
    ``projection.projected_arrival_time`` and makes exactly one provider call.

    It deliberately cannot loop: it takes a single projection and returns a single
    reading, so there is no shape here that could grow into ``forecast_all_horizons``
    (spec 20, 4.2). The horizon walk stays in the Skill.

    Args:
        projection: A route projection with ``projected_position`` (``lat``/``lon``)
            and ``projected_arrival_time``.
        policy, provider: As :func:`get_weather_for_route_time`.
        analysis_time: Overrides the projection's own analysis clock when the caller
            wants to record a different one; the projection's arrival time is always
            what the weather is asked about.
    """
    return get_weather_for_route_time(
        projection.projected_position.lat,
        projection.projected_position.lon,
        projection.projected_arrival_time,
        policy=policy,
        provider=provider,
        analysis_time=analysis_time,
    )
