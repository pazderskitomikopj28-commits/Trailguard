"""Route-aware weather providers (spec 5.3, 17, 23).

This package holds the two ways TrailGuard can answer "what is the weather at *this
position* and *this instant*": a frozen snapshot (Replay Mode) and the live
Open-Meteo API (Live Weather Mode). Both normalise the same response shape through
:mod:`trailguard.weather.normalization`, and both return the same
:class:`~trailguard.weather.base.WeatherReading`, so nothing downstream can tell
which one answered -- except through the provenance that is recorded either way.

:func:`provider_for_policy` is the single place a ``WeatherPolicy`` turns into a
provider, which is what keeps spec 17's rule true in one location rather than in
every caller: *replay only when the caller chose replay*.

The provider classes are exported lazily through :pep:`562` ``__getattr__`` on
purpose. :mod:`trailguard.replay.weather` (the Phase 1 replay loader) imports the
shared normaliser from this package, so eagerly importing the providers here would
close the loop ``replay.weather -> weather.__init__ -> weather.replay ->
replay.weather`` and break the import of the frozen replay layer. Deferring them to
first attribute access breaks the cycle while keeping ``from trailguard.weather
import ReplayWeatherProvider`` working for every caller.
"""

from __future__ import annotations

from typing import Any

from ..schemas.context import WeatherPolicy
from .base import (
    WeatherMode,
    WeatherProvider,
    WeatherProviderError,
    WeatherProviderNotConfigured,
    WeatherProviderResponse,
    WeatherProviderUnavailable,
    WeatherReading,
    WeatherTargetUnavailable,
    reading_provenance,
    require_aware,
)
from .normalization import (
    OPEN_METEO_ENDPOINT,
    WEATHER_VARIABLES,
    WeatherPayloadError,
    WeatherRow,
    WeatherSeries,
    coverage_distance_m,
    nearest_row,
    parse_open_meteo_payload,
    read_json_document,
)

__all__ = [
    "OPEN_METEO_ENDPOINT",
    "REPLAY_WEATHER_DIR",
    "REPO_ROOT",
    "WEATHER_VARIABLES",
    "OpenMeteoWeatherProvider",
    "ReplayWeatherProvider",
    "WeatherMode",
    "WeatherPayloadError",
    "WeatherProvider",
    "WeatherProviderError",
    "WeatherProviderNotConfigured",
    "WeatherProviderResponse",
    "WeatherProviderUnavailable",
    "WeatherReading",
    "WeatherRow",
    "WeatherSeries",
    "WeatherTargetUnavailable",
    "coverage_distance_m",
    "nearest_row",
    "parse_open_meteo_payload",
    "provider_for_policy",
    "reading_provenance",
    "require_aware",
    "resolve_snapshot_path",
]


def provider_for_policy(
    policy: WeatherPolicy,
    *,
    live_transport: Any | None = None,
) -> WeatherProvider:
    """Build the provider a weather policy names.

    ``policy.mode == "replay"`` returns the frozen-snapshot provider, reading the
    snapshot ``policy.snapshot_ref`` names. ``policy.mode == "live"`` returns the
    Open-Meteo provider; ``policy.preferred_source`` must name open-meteo, because
    that is the only live provider implemented, and silently substituting a
    different one would make the policy's own preference a lie.

    Args:
        policy: The caller's weather policy (spec 9, ``weather_policy``).
        live_transport: Injected transport for the live provider. Kept as a keyword
            so a test can point "live" at a stub and stay offline (spec 13) while the
            production wiring stays untouched.

    Raises:
        WeatherProviderNotConfigured: replay names no snapshot, or live names a
            preferred source this build does not implement.

    Never returns: a replay provider standing in for a live request, or the reverse.
    """
    if policy.mode == "replay":
        from .replay import ReplayWeatherProvider

        return ReplayWeatherProvider(policy.snapshot_ref)
    if policy.mode == "live":
        from .open_meteo import OpenMeteoWeatherProvider

        if policy.preferred_source and policy.preferred_source != "open-meteo":
            raise WeatherProviderNotConfigured(
                f"weather policy asks for live source {policy.preferred_source!r}, which this build "
                "does not implement; the only live provider is 'open-meteo'. Refusing rather than "
                "answering from a different provider than the one named."
            )
        return OpenMeteoWeatherProvider(transport=live_transport)
    raise WeatherProviderNotConfigured(  # pragma: no cover - WeatherPolicy.mode is a closed Literal
        f"unknown weather policy mode {policy.mode!r}; spec 17 knows 'replay' and 'live'"
    )


#: Names resolved on first access, so importing the package never imports the
#: providers (see the module docstring on the import cycle this avoids).
_LAZY_EXPORTS: dict[str, str] = {
    "OpenMeteoWeatherProvider": "trailguard.weather.open_meteo",
    "ReplayWeatherProvider": "trailguard.weather.replay",
    "REPLAY_WEATHER_DIR": "trailguard.weather.replay",
    "REPO_ROOT": "trailguard.weather.replay",
    "resolve_snapshot_path": "trailguard.weather.replay",
}


def __getattr__(name: str) -> Any:
    """Resolve the provider exports lazily (:pep:`562`).

    Raises:
        AttributeError: for any name this package does not export, with the usual
            message, so ``hasattr`` and ``dir`` behave normally.
    """
    module_path = _LAZY_EXPORTS.get(name)
    if module_path is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    from importlib import import_module

    value = getattr(import_module(module_path), name)
    globals()[name] = value  # cache, so the second access does not re-import
    return value


def __dir__() -> list[str]:
    """Package attributes, lazy exports included, so ``dir()`` is complete."""
    return sorted(set(globals()) | set(_LAZY_EXPORTS))
