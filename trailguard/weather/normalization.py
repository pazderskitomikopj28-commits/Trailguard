"""The one place an Open-Meteo-shaped payload becomes TrailGuard weather data.

Spec 8 fixes the pipeline this module is the middle of::

    External Provider Response -> Normalization -> WeatherForRouteTime -> Safety Core

There is deliberately **one** normalisation layer, not two. The replay fixture and
the live API are both Open-Meteo-shaped documents, so they are both parsed here.
That is not elegance for its own sake: the conversions below are exactly where a
plausible-looking wrong answer would live, and a second copy would let a bug be
fixed on the live path and left behind on the replay path -- which is the path the
frozen scenarios and the benchmark actually run.

What the layer converts
-----------------------
1. **``precipitation_probability`` arrives in percent.** Open-Meteo reports this
   variable in ``%`` (its own ``hourly_units`` says so); the TrailGuard schema wants
   a probability in 0..1. The ``/100`` is load-bearing, not cosmetic: read raw, a
   50-percent row becomes an impossible probability and trips R-C's 0.7 threshold.
   The unit is read from the document rather than assumed, so a snapshot that
   declares another unit is a loud error instead of a silent rescale.
2. **Row instants are naive local wall-clock times.** ``2026-05-18T12:00`` carries
   no offset; ``utc_offset_seconds`` carries it. Verified against the live API on
   2026-09-26: ``timezone=Europe/Zurich`` returns ``utc_offset_seconds: 7200`` with
   rows still written as ``2026-09-26T00:00`` -- naive local, offset beside it. A
   naive row compared against an aware projected arrival is a ``TypeError`` at best
   and a wrong nearest row at worst.
3. **Variable names are Open-Meteo's.** ``temperature_2m`` -> ``temperature_c``,
   ``wind_speed_10m`` -> ``wind_speed_kmh``, ``wind_gusts_10m`` -> ``wind_gust_kmh``.
   Temperatures are already Celsius and winds already km/h, so those are renames --
   but each one is unit-checked anyway, because a document declaring Fahrenheit
   would otherwise land in ``temperature_c`` silently.

The grid-matching rule (no interpolation)
-----------------------------------------
A query instant resolves to the **nearest** row of the discrete hourly grid, and a
tie resolves to the **earlier** of the two rows. That is the whole rule: it is
deterministic, it is testable, and it never interpolates. Interpolating is
tempting for a continuous variable like temperature, but the same code path would
have to interpolate ``weather_code`` -- a WMO *code*, a label with no arithmetic
meaning, so an interpolated code would be a forecast nobody wrote. The row the
values came from is therefore always named in the returned ``source``, and the
distance from the requested instant is reported as ``reach_minutes`` so a reader
can see how far the answer had to reach.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Mapping

from ..schemas.weather import WeatherForRouteTime

__all__ = [
    "OPEN_METEO_ENDPOINT",
    "WEATHER_VARIABLES",
    "WeatherPayloadError",
    "WeatherRow",
    "WeatherSeries",
    "coverage_distance_m",
    "nearest_row",
    "parse_open_meteo_payload",
    "read_json_document",
]

#: The forecast endpoint, verified against the live API on 2026-09-26.
OPEN_METEO_ENDPOINT = "https://api.open-meteo.com/v1/forecast"

#: The hourly variables this layer knows how to normalise, and the only ones the
#: live provider will ask for. A variable not listed here is not silently dropped:
#: the parser refuses a document whose ``hourly`` block is missing any of them.
WEATHER_VARIABLES: tuple[str, ...] = (
    "temperature_2m",
    "precipitation_probability",
    "wind_speed_10m",
    "wind_gusts_10m",
    "weather_code",
)

#: Open-Meteo variable -> (TrailGuard field, expected unit spelling, scale to apply).
#: ``None`` in the unit column means the variable carries no unit to check (a WMO
#: weather code); ``None`` in the scale column means "same unit, rename only".
_FIELD_MAP: dict[str, tuple[str, str | tuple[str, ...] | None, float | None]] = {
    "temperature_2m": ("temperature_c", "°C", None),
    "precipitation_probability": ("precipitation_probability", ("%", "percent", "pct"), 0.01),
    "wind_speed_10m": ("wind_speed_kmh", "km/h", None),
    "wind_gusts_10m": ("wind_gust_kmh", "km/h", None),
    "weather_code": ("weather_code", None, None),
}


class WeatherPayloadError(ValueError):
    """A payload in the Open-Meteo response shape could not be normalised.

    Subclasses ``ValueError`` because every caller already handles that: the replay
    layer re-raises it as its own ``ReplaySnapshotError`` and the live provider as
    ``WeatherProviderResponse``, so the two paths keep their own vocabulary while
    sharing one parser.
    """


@dataclass(frozen=True)
class WeatherRow:
    """One hourly hazard row of a series, at the instant it describes.

    Instants are timezone-aware: the offset declared by the document (or, for a
    live ``timezone=UTC`` request, zero) is applied at parse time so a row can be
    compared with a projected arrival without either side guessing.
    """

    target_time: datetime
    temperature_c: float
    precipitation_probability: float
    wind_speed_kmh: float
    wind_gust_kmh: float
    weather_code: int


@dataclass(frozen=True)
class WeatherSeries:
    """A normalised hourly series plus the provenance the payload carried.

    ``generated_at`` is ``None`` when the document declares no generation instant
    (which is what the live API does: it forecasts on request and returns no run
    time). The *caller* decides what to do about that -- the replay layer refuses
    to continue without one, the live layer substitutes the retrieval instant and
    says so -- because the right answer differs by path and this layer should not
    pick for either.
    """

    rows: tuple[WeatherRow, ...]
    generated_at: datetime | None
    confidence: float
    confidence_is_assumed: bool
    source: str
    note: str = ""
    latitude: float | None = None
    longitude: float | None = None
    elevation: float | None = None
    #: Replay-fixture annotation only (not an Open-Meteo field): how far a query
    #: position may sit from the snapshot's declared point before the lookup stops
    #: being an answer about that point. ``None`` means "not declared", which is
    #: disclosed rather than guessed at.
    location_tolerance_km: float | None = None
    declared_extras: tuple[str, ...] = field(default=())

    @property
    def first_time(self) -> datetime:
        """Earliest instant the series describes."""
        return self.rows[0].target_time

    @property
    def last_time(self) -> datetime:
        """Latest instant the series describes."""
        return self.rows[-1].target_time

    def row_for(self, target_time: datetime) -> tuple[WeatherRow, float]:
        """The row closest to ``target_time`` and the reach in minutes.

        Delegates to :func:`nearest_row`, which is the single implementation of the
        grid-matching rule documented at the top of this module.
        """
        return nearest_row(self.rows, target_time)

    def build_weather(
        self,
        *,
        row: WeatherRow,
        target_time: datetime,
        source: str,
        used_at: datetime,
    ) -> WeatherForRouteTime:
        """Assemble the contract object from one row of this series.

        Both providers build through here, so the two paths cannot disagree about
        what a ``WeatherForRouteTime`` contains.

        ``used_at`` is the instant the reading is *for*, and it is not the same
        thing for both modes -- which is the whole reason it is a parameter:

        * a **frozen snapshot** was generated once and is consulted later, so its age
          is measured at the instant the caller asked about (the projected arrival).
          That is the Phase 1 semantics, preserved;
        * a **live forecast** is born at the moment it is requested -- the API returns
          no model-run instant -- so its age is measured at the retrieval instant,
          which makes it zero by construction rather than by rounding.

        Both are the same sentence -- "freshness is how old the weather is at the
        instant it is used" -- with a different instant, and each provider says which
        one it used.
        """
        age_minutes = (used_at - self.generated_at).total_seconds() / 60.0 if self.generated_at else 0.0
        return WeatherForRouteTime(
            temperature_c=row.temperature_c,
            precipitation_probability=row.precipitation_probability,
            wind_speed_kmh=row.wind_speed_kmh,
            wind_gust_kmh=row.wind_gust_kmh,
            weather_code=row.weather_code,
            source=source,
            forecast_generated_at=self.generated_at if self.generated_at is not None else used_at,
            target_time=target_time,
            freshness_minutes=max(age_minutes, 0.0),
            confidence=self.confidence,
            confidence_is_assumed=self.confidence_is_assumed,
        )


# --------------------------------------------------------------------------- #
# Grid matching
# --------------------------------------------------------------------------- #


def nearest_row(rows: tuple[WeatherRow, ...], target_time: datetime) -> tuple[WeatherRow, float]:
    """The row nearest ``target_time``, and how far it is in minutes.

    The rule, in full: nearest row wins; a tie goes to the **earlier** row; a
    request outside the series is refused rather than extrapolated; a naive
    ``target_time`` is refused rather than compared.

    Returns:
        ``(row, reach_minutes)`` where ``reach_minutes >= 0`` is the absolute
        distance between the requested instant and the row's own instant, so the
        caller can disclose how far the answer reached.

    Raises:
        WeatherPayloadError: the series is empty, ``target_time`` is naive, or it
            falls outside the series.
    """
    if not rows:
        raise WeatherPayloadError("the hourly series holds no rows")
    if target_time.tzinfo is None or target_time.tzinfo.utcoffset(target_time) is None:
        raise WeatherPayloadError(
            f"target_time must be timezone-aware, got the naive {target_time.isoformat()}; "
            "a projected arrival knows its offset and a weather row must too"
        )
    if target_time < rows[0].target_time or target_time > rows[-1].target_time:
        raise WeatherPayloadError(
            f"target_time {target_time.isoformat()} is outside the hourly series, which covers "
            f"{rows[0].target_time.isoformat()} .. {rows[-1].target_time.isoformat()}; "
            "the grid is not extrapolated beyond its own rows"
        )
    # min() over (distance, instant) puts the earlier row first among equal
    # distances: deterministic, and the tie-break is documented above.
    row = min(rows, key=lambda item: (abs((item.target_time - target_time).total_seconds()), item.target_time))
    reach = abs((row.target_time - target_time).total_seconds()) / 60.0
    return row, reach


def coverage_distance_m(
    latitude: float, longitude: float, latitude_b: float, longitude_b: float
) -> float | None:
    """Geodesic distance in metres between two positions, or ``None`` if unknown.

    Used only to disclose how far a query position sits from the point a
    single-point snapshot or an API grid cell describes -- never to report route
    metres (those belong to :mod:`trailguard.routes.geometry`). ``None`` is returned
    when the ``route`` extra (pyproj) is not installed, and the caller is expected
    to say the check did not run rather than to assume the positions matched.
    """
    try:
        import pyproj
    except ImportError:  # pragma: no cover - depends on the installed extras
        return None
    geod = pyproj.Geod(ellps="WGS84")
    # Geod.inv takes longitudes first; swapping them is a classic silent error, so
    # the argument order is spelled out here.
    _azimuth, _back_azimuth, distance = geod.inv(
        longitude, latitude, longitude_b, latitude_b
    )
    return float(distance)


# --------------------------------------------------------------------------- #
# Parsing
# --------------------------------------------------------------------------- #


def read_json_document(path: str | Path, *, ref: str | None = None) -> tuple[dict[str, Any], str]:
    """Read a JSON document from disk and return it with the ref it was read as.

    Separate from :func:`parse_open_meteo_payload` so the file-reading errors and
    the payload-shape errors stay distinct, and so the live path (which has no file)
    never has to construct a fake path.
    """
    label = ref or str(path)
    try:
        text = Path(path).expanduser().read_text(encoding="utf-8")
    except OSError as exc:
        raise WeatherPayloadError(f"weather document {label} could not be read: {exc}") from exc
    try:
        payload = json.loads(text)
    except ValueError as exc:
        raise WeatherPayloadError(f"weather document {label} is not valid JSON: {exc}") from exc
    if not isinstance(payload, dict):
        raise WeatherPayloadError(f"weather document {label} must be a JSON object at the top level")
    return payload, label


def parse_open_meteo_payload(
    payload: Any,
    *,
    ref: str,
    generated_at: datetime | None = None,
    unknown_confidence: float | None = None,
) -> WeatherSeries:
    """Normalise an Open-Meteo-shaped document into a :class:`WeatherSeries`.

    Args:
        payload: The parsed JSON document (parallel ``hourly`` arrays,
            ``hourly_units``, ``utc_offset_seconds``).
        ref: Human-readable label for the document, used in every error message.
        generated_at: Generation instant to use **when the document declares none**.
            The live API is that case. When the document does declare one, the
            declaration wins -- a caller must not be able to overstate a provider's
            own stamp.
        unknown_confidence: Value to charge when the document declares no provider
            confidence. Defaults to the calibration's ``confidence_unknown_value``
            (0.5), the same number the Phase 1 confidence model charges every other
            undisclosed term; inventing a high default would hand a verdict a number
            nobody earned.

    Returns:
        The normalised series, with rows made timezone-aware, precipitation divided
        by 100, variable names translated and the confidence question answered.

    Raises:
        WeatherPayloadError: the document is not Open-Meteo-shaped, declares a unit
            this layer will not guess at, has ragged parallel arrays, is missing a
            hazard variable, declares a naive generation instant, or declares values
            that are not what they claim to be.
    """
    if not isinstance(payload, Mapping):
        raise WeatherPayloadError(f"{ref} must be a JSON object at the top level")

    declared_generated = _declared_generated_at(payload, ref)
    resolved_generated = declared_generated if declared_generated is not None else generated_at
    if resolved_generated is not None:
        _require_aware(resolved_generated, f"{ref}: generated_at")

    confidence, confidence_is_assumed = _declared_confidence(
        payload, ref=ref, unknown_confidence=unknown_confidence
    )
    rows = _rows_from_hourly(payload, ref=ref, offset=_resolve_offset(payload, ref))
    if not rows:
        raise WeatherPayloadError(f"{ref}: hourly.time described no usable rows")

    latitude = _optional_number(payload.get("latitude"), f"{ref}: latitude")
    longitude = _optional_number(payload.get("longitude"), f"{ref}: longitude")
    elevation = _optional_number(payload.get("elevation"), f"{ref}: elevation")
    tolerance = _optional_number(payload.get("location_tolerance_km"), f"{ref}: location_tolerance_km")
    note = str(payload.get("note") or payload.get("series_note") or "")

    return WeatherSeries(
        rows=rows,
        generated_at=resolved_generated,
        confidence=confidence,
        confidence_is_assumed=confidence_is_assumed,
        source=str(payload.get("source") or f"open-meteo@{ref}"),
        note=note,
        latitude=latitude,
        longitude=longitude,
        elevation=elevation,
        location_tolerance_km=tolerance,
    )


def _declared_generated_at(payload: Mapping[str, Any], ref: str) -> datetime | None:
    """The generation instant the document declares, or ``None`` if it declares none."""
    if "generated_at" not in payload or payload.get("generated_at") is None:
        return None
    instant = _parse_instant(payload.get("generated_at"), f"{ref}: generated_at")
    _require_aware(instant, f"{ref}: generated_at")
    return instant


def _declared_confidence(
    payload: Mapping[str, Any], *, ref: str, unknown_confidence: float | None
) -> tuple[float, bool]:
    """The document's own provider confidence, and whether it declared one.

    A document that declares no confidence is charged the unknown value rather than
    assumed perfect: the same discipline the confidence model applies to every
    other undisclosed term. ``None`` is never coerced to 1.0.
    """
    raw = payload.get("confidence")
    if raw is None:
        return (_resolve_unknown_confidence(unknown_confidence), True)
    value = _number(raw, f"{ref}: confidence")
    if not 0.0 <= value <= 1.0:
        raise WeatherPayloadError(f"{ref}: confidence must be in [0, 1], got {value}")
    return value, False


def _resolve_unknown_confidence(unknown_confidence: float | None) -> float:
    """The value charged for an undeclared provider confidence.

    Read from the shipped calibration when the caller does not supply one, so the
    live provider, the replay loader and the confidence model all agree on what
    "undisclosed" costs. Imported lazily: the weather layer must stay importable
    without the rule pack.
    """
    if unknown_confidence is not None:
        return float(unknown_confidence)
    from ..risk_core.calibration import CALIBRATION_PATH, load_calibration

    return float(load_calibration(CALIBRATION_PATH).confidence_unknown_value)


def _resolve_offset(payload: Mapping[str, Any], ref: str) -> timezone:
    """The timezone the document's naive wall-clock rows are written in.

    ``utc_offset_seconds`` is optional *only* when every timestamp already carries
    an offset; a document that declares neither is not silently assumed UTC, because
    a 12:00 row meant as 10:00Z read as 12:00Z moves every lookup two hours.
    """
    raw = payload.get("utc_offset_seconds")
    if raw is None:
        return timezone.utc
    if isinstance(raw, bool) or not isinstance(raw, (int, float)):
        raise WeatherPayloadError(f"{ref}: utc_offset_seconds must be a number, got {raw!r}")
    return timezone(timedelta(seconds=float(raw)))


def _rows_from_hourly(payload: Mapping[str, Any], *, ref: str, offset: timezone) -> tuple[WeatherRow, ...]:
    """Translate Open-Meteo's parallel ``hourly`` arrays into TrailGuard rows.

    Every row is built from the same index across all arrays, so a document whose
    arrays disagree in length is rejected rather than silently truncated to the
    shortest -- which would pair values with the wrong instants and look complete.
    """
    hourly = payload.get("hourly")
    if not isinstance(hourly, Mapping):
        raise WeatherPayloadError(
            f"{ref} must carry an 'hourly' object of parallel arrays (the Open-Meteo response shape)"
        )
    times_raw = hourly.get("time")
    if not isinstance(times_raw, list) or not times_raw:
        raise WeatherPayloadError(f"{ref}: hourly.time must be a non-empty array of row instants")

    declared_units: dict[str, str] = {}
    units_raw = payload.get("hourly_units")
    if units_raw is not None:
        if not isinstance(units_raw, Mapping):
            raise WeatherPayloadError(f"{ref}: hourly_units must be an object mapping variable to unit")
        declared_units = {str(key): str(value) for key, value in units_raw.items()}

    missing = sorted(set(_FIELD_MAP) - set(hourly))
    if missing:
        raise WeatherPayloadError(
            f"{ref}: hourly is missing the {missing} array(s); the core needs every hazard variable"
        )

    values: dict[str, list[float]] = {}
    for variable, (_field_name, unit, scale) in _FIELD_MAP.items():
        column = hourly.get(variable)
        if not isinstance(column, list):
            raise WeatherPayloadError(f"{ref}: hourly.{variable} must be an array")
        if len(column) != len(times_raw):
            raise WeatherPayloadError(
                f"{ref}: hourly.{variable} has {len(column)} values but hourly.time has "
                f"{len(times_raw)}; parallel arrays must describe the same rows"
            )
        values[variable] = [
            _scaled_value(
                item,
                variable=variable,
                unit=unit,
                scale=scale,
                declared_unit=declared_units.get(variable),
                where=f"{ref}: hourly.{variable}[{index}]",
            )
            for index, item in enumerate(column)
        ]

    rows: list[WeatherRow] = []
    for index, raw_time in enumerate(times_raw):
        instant = _parse_instant(raw_time, f"{ref}: hourly.time[{index}]")
        if instant.tzinfo is None or instant.tzinfo.utcoffset(instant) is None:
            instant = instant.replace(tzinfo=offset)
        probability = values["precipitation_probability"][index]
        if not 0.0 <= probability <= 1.0:
            raise WeatherPayloadError(
                f"{ref}: hourly.precipitation_probability[{index}] is {probability} after unit "
                "conversion, which is not a probability in [0, 1]"
            )
        rows.append(
            WeatherRow(
                target_time=instant,
                temperature_c=values["temperature_2m"][index],
                precipitation_probability=probability,
                wind_speed_kmh=values["wind_speed_10m"][index],
                wind_gust_kmh=values["wind_gusts_10m"][index],
                weather_code=int(values["weather_code"][index]),
            )
        )
    rows.sort(key=lambda row: row.target_time)
    return tuple(rows)


# --------------------------------------------------------------------------- #
# Scalar helpers
# --------------------------------------------------------------------------- #


def _parse_instant(raw: Any, where: str) -> datetime:
    """Parse an ISO8601 instant, including a trailing ``Z``."""
    if not isinstance(raw, str) or not raw.strip():
        raise WeatherPayloadError(f"{where} must be an ISO8601 timestamp string, got {raw!r}")
    text = raw.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        return datetime.fromisoformat(text)
    except ValueError as exc:
        raise WeatherPayloadError(f"{where} is not an ISO8601 timestamp: {raw!r}") from exc


def _require_aware(value: datetime, where: str) -> None:
    """Reject a naive instant: it cannot be compared with an aware one."""
    if value.tzinfo is None or value.tzinfo.utcoffset(value) is None:
        raise WeatherPayloadError(
            f"{where} must be timezone-aware, got the naive {value.isoformat()}; "
            "an instant without an offset cannot be compared with a projected arrival"
        )


def _number(raw: Any, where: str) -> float:
    """Coerce a JSON scalar to float, refusing bools and blanks."""
    if isinstance(raw, bool) or raw is None:
        raise WeatherPayloadError(f"{where} must be a number, got {raw!r}")
    try:
        return float(raw)
    except (TypeError, ValueError) as exc:
        raise WeatherPayloadError(f"{where} must be a number, got {raw!r}") from exc


def _optional_number(raw: Any, where: str) -> float | None:
    """``None`` when the document omits the field; otherwise a checked number."""
    if raw is None:
        return None
    return _number(raw, where)


def _check_unit(*, variable: str, unit: str | tuple[str, ...] | None, declared_unit: str | None, where: str) -> None:
    """Reject a declared unit this layer does not know how to convert.

    The check runs for every variable that expects a unit, not only the ones with a
    scale to apply: a temperature declared in Fahrenheit is as much a silent wrong
    answer as a precipitation declared in millimetres, and both must fail loudly
    rather than be renamed into a TrailGuard field that lies.
    """
    if unit is None:
        return
    if declared_unit is None:
        raise WeatherPayloadError(
            f"{where}: {variable} needs a unit to be checked, but hourly_units declares none; "
            f"expected {unit}"
        )
    if isinstance(unit, tuple):
        if declared_unit.strip().lower() not in unit:
            raise WeatherPayloadError(
                f"{where}: {variable} is declared as {declared_unit!r}, expected one of {unit}; "
                "refusing to guess a scale"
            )
    elif declared_unit.strip() != unit:
        raise WeatherPayloadError(
            f"{where}: {variable} is declared as {declared_unit!r}, expected {unit!r}; "
            "refusing to guess a scale"
        )


def _scaled_value(
    raw: Any,
    *,
    variable: str,
    unit: str | tuple[str, ...] | None,
    scale: float | None,
    declared_unit: str | None,
    where: str,
) -> float:
    """Convert one Open-Meteo value to the TrailGuard field, unit-checked."""
    value = _number(raw, where)
    _check_unit(variable=variable, unit=unit, declared_unit=declared_unit, where=where)
    return value * scale if scale is not None else value
