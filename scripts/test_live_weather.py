#!/usr/bin/env python
"""``python scripts/test_live_weather.py`` -- the one path that may touch the wire.

Spec 14 draws a hard line: ``python -m pytest`` must make **zero** external HTTP
calls (spec 13, enforced structurally by ``tests/conftest.py``), and the live
Open-Meteo provider is therefore exercised in the suite only through an injected
transport. That leaves the real endpoint unproven, and this script is where it gets
proven.

So it is deliberately *not* a test:

* pytest never collects it. ``tests/conftest.py`` lists it in ``collect_ignore`` and
  pytest's ``testpaths`` is ``tests`` only, so no run of the suite can reach it
  either by accident or by ``pytest scripts/``;
* it asserts nothing about *weather*. It proves the plumbing -- that a real request
  is built for a real position and instant, that a real response is normalised by the
  same layer the replay path uses, and that a real failure is reported as a failure;
* it never fakes success. When the API cannot be reached it prints
  ``LIVE WEATHER NETWORK UNAVAILABLE`` and exits non-zero. When the API answers but
  cannot describe the requested instant, it prints the API's own reason and says so.

The endpoint needs no credential (spec 9, 23): the public forecast API takes no key,
so no key is read, no header carries a token and nothing here touches ``.env`` or the
model-provider extras. That boundary is the point -- Phase 2 must not acquire a
dependency on any credentialed subsystem.

Usage::

    python scripts/test_live_weather.py                       # the demo position, now
    python scripts/test_live_weather.py --at 2026-09-26T12:30:00Z
    python scripts/test_live_weather.py --lat 46.13 --lon 7.81 --at 2026-09-26T12:30:00Z

Exit codes: ``0`` answered, ``2`` network unavailable, ``3`` the API refused the
instant, ``4`` the response could not be normalised.
"""

from __future__ import annotations

import argparse
import sys
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:  # allow running from any working directory
    sys.path.insert(0, str(REPO_ROOT))

from trailguard.replay.devices import load_device_feed  # noqa: E402
from trailguard.risk_core.calibration import CALIBRATION_PATH, load_calibration  # noqa: E402
from trailguard.schemas import Scenario  # noqa: E402
from trailguard.tools import get_route_context, project_route_state  # noqa: E402
from trailguard.tools.weather_route_time import get_weather_for_route_time  # noqa: E402
from trailguard.weather import (  # noqa: E402
    OpenMeteoWeatherProvider,
    WeatherProviderResponse,
    WeatherProviderUnavailable,
    WeatherTargetUnavailable,
    reading_provenance,
)
from trailguard.weather.open_meteo import (  # noqa: E402
    DEFAULT_CONNECT_TIMEOUT_S,
    DEFAULT_READ_TIMEOUT_S,
    OPEN_METEO_ENDPOINT,
)

#: The scenario whose projected +120 minutes position this script asks about, so a
#: live call is made for somewhere the deterministic chain actually goes.
SCENARIO_STEM = "scn_clear_day_normal"

#: The horizon position to ask about, in minutes.
HORIZON_MIN = 120

#: Printed verbatim when the endpoint cannot be reached (spec 14).
NETWORK_UNAVAILABLE = "LIVE WEATHER NETWORK UNAVAILABLE"


def _projected_position() -> tuple[float, float, datetime]:
    """The clear-day scenario's projected +120 position and its analysis clock.

    Computed from the shipped assets rather than hard-coded, so the live call is
    about a place the replay chain really visits -- and so a change in the GPX or the
    movement model changes what is asked for instead of silently going stale.
    """
    import yaml

    path = next(
        candidate
        for candidate in sorted((REPO_ROOT / "scenarios").rglob("*.yaml"))
        if candidate.stem == SCENARIO_STEM
    )
    scenario = Scenario.model_validate(yaml.safe_load(path.read_text(encoding="utf-8")))
    context = scenario.initial_context
    route_state = get_route_context(
        context.route.value.route_id, context.position.value, context.timestamp
    )
    device_state = load_device_feed("scn_device_" + scenario.scenario_id).current
    projection = project_route_state(
        route_context=route_state,
        movement={
            "current_speed_kmh": device_state.movement.current_speed_kmh,
            "avg_speed_10m": device_state.movement.avg_speed_10m,
            "avg_speed_30m": device_state.movement.avg_speed_30m,
        },
        horizon_min=HORIZON_MIN,
        analysis_time=context.time_state.analysis_time,
        fatigue_penalty=device_state.fatigue_index,
    )
    return projection.projected_position.lat, projection.projected_position.lon, projection.projected_arrival_time


class _RememberingProvider(OpenMeteoWeatherProvider):
    """The live provider, remembering the reading it just returned.

    The tool hands back only the contract object; this script wants to print the row,
    the grid reach and the disclosures beside it. Recording at the provider keeps the
    run at **one** HTTP request, which for a script whose subject is the wire is the
    right number.
    """

    last_reading: object = None

    def get_weather(self, **kwargs: object) -> object:
        self.last_reading = super().get_weather(**kwargs)  # type: ignore[attr-defined]
        return self.last_reading


def _parse_instant(text: str) -> datetime:
    """Parse an ISO8601 instant, requiring an offset (spec 6)."""
    value = text.strip()
    if value.endswith("Z"):
        value = value[:-1] + "+00:00"
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        raise SystemExit(f"--at must carry a timezone offset (e.g. 2026-09-26T12:30:00Z), got {text!r}")
    return parsed


def main(argv: list[str] | None = None) -> int:
    """Ask the live API for one position and instant, and report exactly what happened."""
    parser = argparse.ArgumentParser(
        description="Prove the live Open-Meteo provider against the real API (never run by pytest)."
    )
    parser.add_argument("--lat", type=float, default=None, help="latitude in decimal degrees")
    parser.add_argument("--lon", type=float, default=None, help="longitude in decimal degrees")
    parser.add_argument(
        "--at",
        type=str,
        default=None,
        help="ISO8601 instant with an offset to ask about; defaults to the projected arrival of +120 min",
    )
    args = parser.parse_args(argv)

    default_lat, default_lon, default_at = _projected_position()
    latitude = args.lat if args.lat is not None else default_lat
    longitude = args.lon if args.lon is not None else default_lon
    # One legitimate wall-clock use in the whole package: the *instant to ask the live
    # API about*. Everything else in TrailGuard takes its instants from analysis_time,
    # a projection or a document (spec 6) -- but a live provider has no document, so
    # the caller that owns the clock has to supply one, and this script supplies it
    # explicitly rather than letting the provider invent it.
    target_time = _parse_instant(args.at) if args.at else datetime.now(timezone.utc)

    print("TrailGuard Phase 2 live weather smoke (real network, no LLM, no API key)")
    print(f"endpoint      : {OPEN_METEO_ENDPOINT}")
    print(f"timeouts      : {DEFAULT_CONNECT_TIMEOUT_S}s connect / {DEFAULT_READ_TIMEOUT_S}s read, one retry")
    print(f"asked about   : lat {latitude:.6f} lon {longitude:.6f} at {target_time.isoformat()}")
    print()

    provider = _RememberingProvider()  # no transport: the real one
    try:
        weather = get_weather_for_route_time(
            latitude,
            longitude,
            target_time,
            provider=provider,
            analysis_time=target_time,
        )
    except WeatherProviderUnavailable as exc:
        # Named verbatim, with the reason: a run that cannot prove the endpoint must
        # not look like a run that did (spec 14).
        print(NETWORK_UNAVAILABLE)
        print(f"reason        : {exc}")
        print()
        print("the deterministic core is unaffected: mode='replay' needs neither this endpoint")
        print("nor any network. Phase 2's verdict does not depend on this script succeeding.")
        return 2
    except WeatherTargetUnavailable as exc:
        print("LIVE WEATHER ENDPOINT REFUSED THE REQUESTED INSTANT")
        print(f"reason        : {exc}")
        print()
        print("this is the API saying no, not a network failure: the forecast endpoint only")
        print("describes a bounded date range. Use --at with an instant inside that range.")
        return 3
    except WeatherProviderResponse as exc:
        print("LIVE WEATHER RESPONSE COULD NOT BE NORMALISED")
        print(f"reason        : {exc}")
        return 4

    # The reading the tool's own call produced -- one request, recorded at the provider.
    reading = provider.last_reading
    provenance = reading_provenance(reading)

    print("answered      : yes")
    print(f"weather       : gust {weather.wind_gust_kmh} km/h, wind {weather.wind_speed_kmh} km/h, "
          f"temp {weather.temperature_c} C, precip {weather.precipitation_probability}, "
          f"code {weather.weather_code}")
    print(f"source        : {weather.source}")
    print(f"row matched   : {provenance['weather_row_time']} "
          f"({provenance['weather_grid_reach_min']} min from the requested instant)")
    print(f"target time   : {weather.target_time.isoformat()}")
    print(f"generated at  : {weather.forecast_generated_at.isoformat()} "
          f"(the API returns no model-run instant, so this is the retrieval instant)")
    print(f"freshness     : {weather.freshness_minutes:.1f} min")
    print(f"confidence    : {weather.confidence} "
          f"({'charged: the provider declares none' if reading.confidence_is_assumed else 'provider-declared'})")
    for note in reading.note_lines():
        print(f"disclosure    : {note}")
    print()
    print("no API key was read and no token was sent: the forecast endpoint is public.")
    print("this script is not run by pytest (spec 13, 14); the suite uses a mock transport.")
    return 0


if __name__ == "__main__":  # pragma: no cover - exercised by the command, not by pytest
    raise SystemExit(main())
