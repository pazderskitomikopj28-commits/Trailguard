"""The replay provider: a frozen Open-Meteo-shaped snapshot, nothing else (spec 17).

Replay Mode is what makes the benchmark repeatable and the demo independent of the
weather on the day, so this provider is deliberately boring: it reads one file, it
performs no I/O beyond that read, it reads no clock, and it holds no cached state
across horizons beyond the parsed rows themselves (spec 19 -- the same snapshot
asked four times yields four identical readings).

Route-aware, and honestly so
----------------------------
The frozen snapshot is a **single-point** series: it declares ``latitude`` /
``longitude`` (Open-Meteo's own response fields, naming the point the rows describe)
and has no second spatial dimension. A position lookup therefore cannot vary
spatially, and this provider refuses to pretend otherwise:

* when the document declares a point, the geodesic distance from the query position
  is computed and **disclosed in the reading's notes** -- "the projected position is
  1 830 m from the snapshot's declared point";
* when the document also declares ``location_tolerance_km``, a query position beyond
  that tolerance is a :class:`~trailguard.weather.base.WeatherTargetUnavailable`
  error rather than an answer about a place the snapshot does not describe;
* when it declares no tolerance, the distance is disclosed and nothing is refused,
  because inventing a coverage radius would be inventing a threshold.

What makes the lookup route-aware is the **time**: the instant requested is the
horizon's ``projected_arrival_time``, so NOW, +30, +60 and +120 are four different
lookups against the same series rather than one lookup re-labelled four times.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

from ..replay.weather import DEFAULT_SNAPSHOT_PATH, load_weather_snapshot
from .base import (
    WeatherMode,
    WeatherProvider,
    WeatherProviderError,
    WeatherProviderNotConfigured,
    WeatherProviderUnavailable,
    WeatherReading,
    WeatherTargetUnavailable,
    require_aware,
)
from .normalization import WeatherPayloadError, coverage_distance_m, read_json_document

__all__ = ["REPLAY_WEATHER_DIR", "REPO_ROOT", "ReplayWeatherProvider", "resolve_snapshot_path"]

#: Repository root, derived from this file so the provider works from any cwd.
REPO_ROOT: Path = Path(__file__).resolve().parents[2]

#: Where replay weather snapshots live (spec 24).
REPLAY_WEATHER_DIR: Path = REPO_ROOT / "replay" / "weather"


def resolve_snapshot_path(snapshot_ref: str | None) -> Path:
    """Resolve a scenario's ``snapshot_ref`` to a file on disk.

    Frozen scenarios write the reference the way ``weather_policy.snapshot_ref``
    does -- ``replay/weather/scn_weather_snapshot.json`` -- so a bare snapshot name
    is also accepted and looked up in the replay weather directory. A reference that
    resolves to nothing is an error naming every candidate tried, rather than a
    silent fall back to the default snapshot: a caller that asked for a specific
    fixture must be told when it is not there.

    Raises:
        WeatherProviderNotConfigured: ``snapshot_ref`` is empty, or no candidate
            path exists.
    """
    if not snapshot_ref or not str(snapshot_ref).strip():
        raise WeatherProviderNotConfigured(
            "weather policy mode 'replay' requires snapshot_ref to name the snapshot to read; "
            "replay is only used when the caller names the fixture (spec 17)"
        )
    ref = str(snapshot_ref).strip()
    candidates: list[Path] = []
    direct = Path(ref).expanduser()
    if direct.is_absolute():
        candidates.append(direct)
    else:
        candidates.append(REPO_ROOT / ref)
        candidates.append(REPLAY_WEATHER_DIR / ref)
        candidates.append(REPLAY_WEATHER_DIR / Path(ref).name)
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    raise WeatherProviderNotConfigured(
        f"replay snapshot {ref!r} was not found; tried {[str(path) for path in candidates]}. "
        "A replay run never silently substitutes another snapshot."
    )


class ReplayWeatherProvider(WeatherProvider):
    """Read weather for a position and instant from a frozen snapshot.

    The snapshot is parsed by the shared normalisation layer, so the percent/offset
    conversions a live response gets are the same ones a frozen fixture gets: a bug
    in the interpretation of ``hourly_units`` cannot be live-only.

    Construction:
        * ``snapshot_ref`` -- a path or replay-directory-relative name. ``None``
          falls back to the shipped scenario snapshot, which is what the frozen
          replay scenarios name.
        * ``snapshot`` -- an already parsed snapshot, for a caller (a test, a
          fixture that lives outside the repo) that built it itself.
    """

    mode: WeatherMode = "replay"

    def __init__(
        self,
        snapshot_ref: str | None = None,
        *,
        snapshot: object | None = None,
    ) -> None:
        self._path = resolve_snapshot_path(snapshot_ref) if snapshot is None else None
        self._ref = str(snapshot_ref or DEFAULT_SNAPSHOT_PATH.name)
        if snapshot is not None:
            self._snapshot = snapshot
        else:
            try:
                self._snapshot = load_weather_snapshot(self._path)  # type: ignore[arg-type]
            except WeatherPayloadError as exc:
                raise WeatherProviderUnavailable(
                    f"the replay weather snapshot {self._ref!r} could not be read: {exc}"
                ) from exc

    # ------------------------------------------------------------------ #
    # WeatherProvider
    # ------------------------------------------------------------------ #

    def describe(self) -> str:
        """One line naming this provider, the snapshot and its generation instant."""
        snapshot = self._snapshot
        generated = getattr(snapshot, "generated_at", None)
        return (
            f"replay:ReplayWeatherProvider snapshot={getattr(snapshot, 'snapshot_ref', self._ref)} "
            f"generated_at={generated.isoformat() if generated else 'unknown'}"
        )

    def get_weather(
        self,
        *,
        latitude: float,
        longitude: float,
        target_time: datetime,
        analysis_time: datetime | None = None,
    ) -> WeatherReading:
        """The snapshot's answer for ``(latitude, longitude)`` at ``target_time``.

        Time handling: the requested instant is matched to the nearest row of the
        discrete hourly grid, ties going to the earlier row, with no interpolation
        and no extrapolation outside the series.

        Freshness handling: ``freshness_minutes`` is ``target_time - generated_at``,
        exactly the Phase 1 semantics. A projected arrival two hours out therefore
        reports a two-hour-old snapshot -- the age term the confidence model needs,
        and a disclosure the Skill's freshness protocol reads.

        Raises:
            WeatherProviderError: a naive instant, a target time outside the series,
                or a query position further from the snapshot's point than a declared
                tolerance allows.
        """
        require_aware(target_time, "target_time")
        if analysis_time is not None:
            require_aware(analysis_time, "analysis_time")

        snapshot = self._snapshot
        notes: list[str] = []
        try:
            row, reach = snapshot.row_for(target_time)
        except WeatherPayloadError as exc:
            raise WeatherTargetUnavailable(
                f"{self._ref}: no row of the replay snapshot describes "
                f"{target_time.isoformat()}: {exc}"
            ) from exc
        except WeatherProviderError:
            raise
        except Exception as exc:  # pragma: no cover - defensive: an unparsed snapshot
            raise WeatherProviderUnavailable(
                f"{self._ref}: the replay snapshot could not answer for {target_time.isoformat()}: {exc}"
            ) from exc

        notes.extend(self._coverage_notes(latitude, longitude))

        weather = snapshot.at(target_time)
        return WeatherReading(
            weather=weather,
            provider=str(getattr(snapshot, "source", self._ref)),
            mode="replay",
            row_time=row.target_time,
            reach_minutes=reach,
            confidence_is_assumed=bool(getattr(snapshot, "confidence_is_assumed", False)),
            analysis_time=analysis_time,
            notes=tuple(notes),
        )

    # ------------------------------------------------------------------ #
    # Spatial coverage (a disclosure, not a guess)
    # ------------------------------------------------------------------ #

    def _coverage_notes(self, latitude: float, longitude: float) -> list[str]:
        """Disclose how the query position relates to the snapshot's own point.

        This is the only position-aware thing a single-point snapshot can honestly
        do, so it says it rather than implying a spatial lookup happened.
        """
        snapshot = self._snapshot
        declared_lat = getattr(snapshot, "latitude", None)
        declared_lon = getattr(snapshot, "longitude", None)
        if declared_lat is None or declared_lon is None:
            return [
                "the snapshot declares no position, so it cannot state how far the projected "
                "position is from what it describes; treat it as single-point weather"
            ]
        distance = coverage_distance_m(latitude, longitude, float(declared_lat), float(declared_lon))
        if distance is None:
            return [
                f"the snapshot declares the point ({declared_lat}, {declared_lon}) but the route extra "
                "(pyproj) is not installed, so the distance from the projected position was not checked"
            ]
        tolerance_km = getattr(snapshot, "location_tolerance_km", None)
        if tolerance_km is not None and distance > float(tolerance_km) * 1000.0:
            raise WeatherTargetUnavailable(
                f"the projected position ({latitude}, {longitude}) is {distance:.1f} m from the "
                f"position {self._ref!r} describes ({declared_lat}, {declared_lon}), which declares a "
                f"coverage tolerance of {tolerance_km} km; answering anyway would be weather for a "
                "place this snapshot does not describe"
            )
        return [
            f"the replay snapshot is single-point weather for ({declared_lat}, {declared_lon}); the "
            f"projected position is {distance:.1f} m from it"
            + (
                f" and within the declared {tolerance_km} km tolerance"
                if tolerance_km is not None
                else "; no coverage tolerance was declared, so the distance is disclosed rather than refused"
            )
        ]


def provider_from_ref(snapshot_ref: str | None) -> ReplayWeatherProvider:
    """Build the replay provider a scenario's ``snapshot_ref`` names.

    Exposed so the tool and the demo harness construct the same thing from a policy.
    """
    return ReplayWeatherProvider(snapshot_ref)
