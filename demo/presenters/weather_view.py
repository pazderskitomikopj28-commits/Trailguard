"""Future weather as it lands on the route, per horizon.

Spec 9. The whole point of TrailGuard's weather step is that it asks about the
*hiker's* future position, not the weather at the phone's current location -- so
the table below pairs each horizon's projected arrival time with the forecast for
that instant and place, and shows the freshness that came with it. The rows are
verbatim values from ``WeatherForRouteTime``; the only choice here is which of
that contract's fields to put on screen and in what order.

Thresholds stay in the rule pack. A gust value that is above or below R-C's 45 and
65 km/h bands is shown as the value plus a note naming the pack's own threshold,
read from the pack's ``thresholds`` dict -- so if the pack is ever re-tuned the
annotation moves with it instead of the demo showing a stale number.
"""

from __future__ import annotations

from dataclasses import dataclass

from trailguard.risk_core import load_rule_pack

from .chain import HorizonRow, ReplayRun
from .timeline import LEVEL_STYLE


@dataclass(slots=True)
class WeatherRow:
    """One horizon's forecast, flat and display-ready."""

    label: str
    arrival_time: str
    level: str
    badge: str
    colour: str
    temperature_c: float
    wind_speed_kmh: float
    wind_gust_kmh: float
    precipitation_probability: float
    weather_code: int
    source: str
    forecast_generated_at: str
    target_time: str
    freshness_minutes: float
    freshness_ok: bool | None
    freshness_budget_min: float
    #: Pack-sourced annotations, e.g. "wind_gust_kmh_severe=65 km/h".
    gust_threshold_notes: list[str]
    precip_threshold_notes: list[str]
    cold_threshold_notes: list[str]


def _weather_code_note(code: int) -> str:
    """The WMO code in words, for a judge who does not read WMO codes."""
    table = {
        0: "clear",
        1: "mainly clear",
        2: "partly cloudy",
        3: "overcast",
        45: "fog",
        48: "depositing rime fog",
        51: "light drizzle",
        53: "moderate drizzle",
        55: "dense drizzle",
        61: "slight rain",
        63: "moderate rain",
        65: "heavy rain",
        71: "slight snow",
        73: "moderate snow",
        75: "heavy snow",
        80: "rain showers",
        81: "moderate rain showers",
        82: "violent rain showers",
        85: "snow showers",
        95: "thunderstorm",
        96: "thunderstorm with slight hail",
        99: "thunderstorm with heavy hail",
    }
    return table.get(code, "reported by the provider")


def weather_rows(run: ReplayRun, *, pack=None) -> list[WeatherRow]:
    """Flatten the run's horizons into weather rows, annotated from the pack."""
    resolved = pack if pack is not None else load_rule_pack()
    try:
        future_weather = resolved.get("R-C")
    except KeyError:  # pragma: no cover - the pack is frozen at six rules
        future_weather = None
    thresholds = dict(future_weather.thresholds) if future_weather is not None else {}
    units = dict(future_weather.units) if future_weather is not None else {}

    def note(key: str) -> str:
        if key not in thresholds:
            return ""
        return f"{key}={thresholds[key]} {units.get(key, '')}".strip()

    gust_notes = [n for n in (note("wind_gust_kmh"), note("wind_gust_kmh_severe")) if n]
    precip_notes = [n for n in (note("precipitation_probability"),) if n]
    cold_notes = [n for n in (note("temperature_c_low"),) if n]

    rows: list[WeatherRow] = []
    for row in run.rows:
        style = LEVEL_STYLE[row.risk_level]
        weather = row.weather
        rows.append(
            WeatherRow(
                label=row.label,
                arrival_time=row.projected_arrival_time,
                level=row.risk_level,
                badge=style["badge"],
                colour=style["colour"],
                temperature_c=weather.temperature_c,
                wind_speed_kmh=weather.wind_speed_kmh,
                wind_gust_kmh=weather.wind_gust_kmh,
                precipitation_probability=weather.precipitation_probability,
                weather_code=weather.weather_code,
                source=weather.source,
                forecast_generated_at=weather.forecast_generated_at.strftime("%Y-%m-%dT%H:%M:%SZ"),
                target_time=weather.target_time.strftime("%Y-%m-%dT%H:%M:%SZ"),
                freshness_minutes=weather.freshness_minutes,
                freshness_ok=row.freshness_ok,
                freshness_budget_min=run.freshness_budget_min,
                gust_threshold_notes=list(gust_notes),
                precip_threshold_notes=list(precip_notes),
                cold_threshold_notes=list(cold_notes),
            )
        )
    return rows


def weather_summary(run: ReplayRun) -> str:
    """One line on where the weather came from and how fresh it is.

    This is the sentence that has to be on screen whenever weather is shown
    (spec 38): a judge is entitled to know whether the forecast is a frozen
    replay snapshot or a live call, without having to infer it.
    """
    if run.weather_mode == "replay":
        source = "replay snapshot"
    else:
        source = "live provider"
    freshest = min(
        (row.weather.freshness_minutes for row in run.rows), default=0.0
    )
    return (
        f"Weather mode: {run.weather_mode} ({source}). Freshest reading used: "
        f"{freshest:.0f} minutes old, against a staleness budget of "
        f"{run.freshness_budget_min:.0f} minutes."
    )


def horizon_weather(row: HorizonRow) -> dict[str, object]:
    """The weather of one horizon, as a dict for a detail panel."""
    weather = row.weather
    return {
        "horizon": row.label,
        "arrival_time": row.projected_arrival_time,
        "temperature_c": weather.temperature_c,
        "wind_speed_kmh": weather.wind_speed_kmh,
        "wind_gust_kmh": weather.wind_gust_kmh,
        "precipitation_probability": weather.precipitation_probability,
        "weather_code": weather.weather_code,
        "weather_code_note": _weather_code_note(weather.weather_code),
        "source": weather.source,
        "forecast_generated_at": weather.forecast_generated_at.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "target_time": weather.target_time.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "freshness_minutes": weather.freshness_minutes,
        "freshness_ok": row.freshness_ok,
        "confidence_is_assumed": weather.confidence_is_assumed,
    }
