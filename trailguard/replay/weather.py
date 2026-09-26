"""Replay weather snapshots: a deterministic stand-in for a provider response.

Replay Mode is a spec 17 requirement -- the demo and the benchmark must be
reproducible and independent of the weather on the day -- so this module reads a
fixed Open-Meteo-shaped file and nothing else: no network, no clock, no randomness
(spec 19).

The parser is the shared normalisation layer
--------------------------------------------
The frozen asset is stored **in Open-Meteo's response shape**: parallel arrays
under ``hourly``, ``utc_offset_seconds`` for the wall-clock rows, and
``hourly_units`` naming each variable's unit. It is not a TrailGuard-native
document, and this module deliberately does not pretend it is.

Phase 1 parsed that shape inside this file. Phase 2 moves the translation one
layer down, into :mod:`trailguard.weather.normalization`, so the **live** provider
and the replay provider share one implementation. The reason is not tidiness: the
conversions below are exactly where a plausible-looking wrong answer would live,
and a second copy would let a bug be fixed on the live path and left behind on the
replay path -- the path the frozen scenarios and the benchmark actually run.

What this layer still owns, because it is replay-specific: the *policy* question of
a missing or undeclared generation instant, the ``snapshot_ref`` that names the
file, and the lru_cache that lets a horizon loop ask the same question four times
without re-reading it. It also owns translating the shared parser's
:class:`~trailguard.weather.normalization.WeatherPayloadError` into this module's
own ``ReplaySnapshotError`` -- callers that catch the replay vocabulary keep working
while the parsing is shared.

Verified against the live API on 2026-09-26: a real response carries exactly these
fields, including ``timezone=Europe/Zurich`` returning ``utc_offset_seconds: 7200``
with rows still written as naive local wall-clock times.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from functools import lru_cache
from pathlib import Path
from typing import Any

from ..schemas.weather import WeatherForRouteTime
from ..weather.normalization import (
    WeatherPayloadError,
    WeatherRow,
    nearest_row,
    parse_open_meteo_payload,
    read_json_document,
)

__all__ = [
    "DEFAULT_SNAPSHOT_PATH",
    "REPLAY_DIR",
    "ReplaySnapshotError",
    "WeatherRow",
    "WeatherSnapshot",
    "load_weather_snapshot",
    "undeclared_snapshot_confidence",
]

#: Where replay snapshots live (spec 24).
REPLAY_DIR: Path = Path(__file__).resolve().parents[2] / "replay" / "weather"

#: The snapshot the frozen replay scenarios name in ``weather_policy.snapshot_ref``.
DEFAULT_SNAPSHOT_PATH: Path = REPLAY_DIR / "scn_weather_snapshot.json"


class ReplaySnapshotError(WeatherPayloadError):
    """A replay snapshot could not be read, parsed or answered from.

    Subclasses the shared parser's error so that the *parse* lives in one place
    while a caller catching ``ReplaySnapshotError`` keeps its meaning: this is
    about a frozen asset, not about a provider that is merely unreachable.
    """


def undeclared_snapshot_confidence() -> float:
    """The value charged for a snapshot that declares no provider confidence.

    Read from the shipped calibration rather than repeated here, so the replay
    loader, the live provider and the confidence model all agree on what
    "undisclosed" costs. A snapshot that says nothing about its own confidence is
    charged this -- never assumed perfect -- because a synthetic fixture with
    invented values has not earned a 1.0.
    """
    from ..risk_core.calibration import CALIBRATION_PATH, load_calibration

    return float(load_calibration(CALIBRATION_PATH).confidence_unknown_value)


@dataclass(frozen=True)
class WeatherSnapshot:
    """A frozen weather snapshot and the one lookup the core needs.

    Immutable and cacheable: the same file yields the same rows in every run
    (spec 19), and a horizon loop asks for the same snapshot four times without
    re-reading it.
    """

    snapshot_ref: str
    source: str
    generated_at: datetime
    confidence: float
    rows: tuple[WeatherRow, ...]
    note: str = ""
    #: True when the snapshot itself declared no provider confidence, so
    #: :attr:`confidence` is a charged unknown rather than a claim.
    confidence_is_assumed: bool = False
    #: The point the snapshot's rows describe, when it declares one (Open-Meteo's
    #: own response fields). ``None`` means "not declared".
    latitude: float | None = None
    longitude: float | None = None
    elevation: float | None = None
    #: Replay-fixture annotation only (not an Open-Meteo field): how far a query
    #: position may sit from :attr:`latitude`/:attr:`longitude` before the lookup
    #: stops being an answer about that point. ``None`` means "not declared".
    location_tolerance_km: float | None = None

    @property
    def first_time(self) -> datetime:
        """Earliest instant the snapshot describes."""
        return self.rows[0].target_time

    @property
    def last_time(self) -> datetime:
        """Latest instant the snapshot describes."""
        return self.rows[-1].target_time

    def row_for(self, target_time: datetime) -> tuple[WeatherRow, float]:
        """The row closest to ``target_time`` and its distance in minutes.

        Returns:
            The nearest :class:`WeatherRow` and how far the requested instant is
            from the row's own instant (>= 0), so the caller can disclose how far
            the answer had to reach.

        Raises:
            ReplaySnapshotError: ``target_time`` is outside the series, which is
                not extrapolated, or is naive.
        """
        return _replay_errors(lambda: nearest_row(self.rows, target_time))

    def at(self, target_time: datetime) -> WeatherForRouteTime:
        """The observation this snapshot offers for ``target_time``.

        The hazard values come from the nearest row; ``target_time`` is the
        instant the caller asked about; ``source`` names the row the values were
        read from; ``freshness_minutes`` is the age against that instant;
        ``confidence_is_assumed`` carries through whether ``confidence`` was
        declared or charged.
        """
        row, _reach = self.row_for(target_time)
        age_minutes = (target_time - self.generated_at).total_seconds() / 60.0
        return WeatherForRouteTime(
            temperature_c=row.temperature_c,
            precipitation_probability=row.precipitation_probability,
            wind_speed_kmh=row.wind_speed_kmh,
            wind_gust_kmh=row.wind_gust_kmh,
            weather_code=row.weather_code,
            source=f"{self.source}@{row.target_time.isoformat()}",
            forecast_generated_at=self.generated_at,
            target_time=target_time,
            freshness_minutes=max(age_minutes, 0.0),
            confidence=self.confidence,
            confidence_is_assumed=self.confidence_is_assumed,
        )


def _replay_errors(call: Any) -> Any:
    """Run ``call``, re-raising the shared parser's error in replay vocabulary.

    The parsing is shared but the error type a replay caller catches is not: a
    frozen asset that cannot answer is a different situation from a provider that
    is unreachable, and the two must stay distinguishable in a caller's except
    clause. The message is passed through unchanged so a ``match=`` pattern written
    against Phase 1 behaviour still matches.
    """
    try:
        return call()
    except WeatherPayloadError as exc:
        raise ReplaySnapshotError(str(exc)) from exc


def _parse_snapshot_payload(payload: Any, *, ref: str) -> WeatherSnapshot:
    """Build a snapshot from a parsed Open-Meteo-shaped JSON document."""
    series = _replay_errors(
        lambda: parse_open_meteo_payload(payload, ref=ref, unknown_confidence=undeclared_snapshot_confidence())
    )
    if series.generated_at is None:
        raise ReplaySnapshotError(
            f"{ref}: generated_at must be an ISO8601 timestamp string, got None; "
            "a replay snapshot must say when it was generated, or every age computed from it is a lie"
        )
    return WeatherSnapshot(
        snapshot_ref=ref,
        source=series.source,
        generated_at=series.generated_at,
        confidence=series.confidence,
        rows=series.rows,
        note=series.note,
        confidence_is_assumed=series.confidence_is_assumed,
        latitude=series.latitude,
        longitude=series.longitude,
        elevation=series.elevation,
        location_tolerance_km=series.location_tolerance_km,
    )


@lru_cache(maxsize=8)
def _cached_snapshot(path_key: str) -> WeatherSnapshot:
    """Read and parse (and remember) the snapshot at ``path_key``.

    Cached by resolved path because a horizon loop asks for the same snapshot four
    times and the parse is deterministic.
    """
    path = Path(path_key)
    if not path.is_file():
        raise ReplaySnapshotError(f"replay weather snapshot not found: {path}")
    try:
        payload, ref = read_json_document(path)
    except WeatherPayloadError as exc:
        raise ReplaySnapshotError(str(exc)) from exc
    return _parse_snapshot_payload(payload, ref=str(path))


def load_weather_snapshot(path: str | Path | None = None) -> WeatherSnapshot:
    """Load a replay weather snapshot in Open-Meteo's response shape.

    Args:
        path: Snapshot JSON to load. Defaults to
            :data:`DEFAULT_SNAPSHOT_PATH` -- the
            ``replay/weather/scn_weather_snapshot.json`` asset the frozen replay
            scenarios name.

    Returns:
        The parsed :class:`WeatherSnapshot`, with Open-Meteo's variable names,
        percent-scaled precipitation and local row instants already normalised.

    Raises:
        ReplaySnapshotError: the file is missing, unreadable, not JSON, not
            Open-Meteo-shaped, declares no generation instant, or declares a unit or
            offset this layer will not guess at.
    """
    resolved = Path(path) if path is not None else DEFAULT_SNAPSHOT_PATH
    return _cached_snapshot(str(resolved.expanduser().resolve()))
