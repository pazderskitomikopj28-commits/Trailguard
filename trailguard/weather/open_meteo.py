"""The live provider: real Open-Meteo forecasts for a projected position and time.

Spec 17 keeps Live Weather Mode as an *additional* capability beside Replay Mode,
and this is it. It is the only part of TrailGuard that opens a socket, so it is
also where the failure policy has to be loud rather than convenient.

The endpoint and every parameter name below was read off the live API on
2026-09-26, not recalled: ``https://api.open-meteo.com/v1/forecast`` with
``latitude``, ``longitude``, ``hourly`` (comma-separated variable names),
``timezone=UTC`` and ``start_date`` / ``end_date``. A live response carries exactly
the shape the shared normalisation layer already parses -- parallel ``hourly``
arrays, ``hourly_units``, ``utc_offset_seconds``, naive local wall-clock rows, and
``precipitation_probability`` in percent.

No credentials
--------------
The forecast API is public and needs no key, so this provider sends none: no header
carries a token, no API key is read from the environment, and nothing here would
serialise one into a trace. That is a deliberate boundary, not an oversight -- the
``providers`` extra (StepFun, local model) is where credentials live, and it is a
different subsystem that Phase 2 must not touch (spec 23).

What it will not do
-------------------
It will not fall back to a replay snapshot when it fails. A frozen fixture
substituted for a live call is a fabricated answer wearing a reproducible costume,
and the caller would have no way to tell -- so every failure mode below raises a
subclass of :class:`~trailguard.weather.base.WeatherProviderError` naming what was
attempted, and ``mode == "replay"`` remains reachable only when the caller chooses
it (spec 17).
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Callable

from .base import (
    WeatherMode,
    WeatherProvider,
    WeatherProviderNotConfigured,
    WeatherProviderResponse,
    WeatherProviderUnavailable,
    WeatherReading,
    WeatherTargetUnavailable,
    require_aware,
)
from .normalization import (
    OPEN_METEO_ENDPOINT,
    WEATHER_VARIABLES,
    WeatherPayloadError,
    nearest_row,
    parse_open_meteo_payload,
)

__all__ = ["DEFAULT_CONNECT_TIMEOUT_S", "DEFAULT_READ_TIMEOUT_S", "OpenMeteoWeatherProvider"]

#: Connect timeout in seconds. Finite by construction (spec 12): an unbounded
#: connect would hang a horizon loop on a black-holed network rather than failing
#: it.
DEFAULT_CONNECT_TIMEOUT_S: float = 5.0

#: Read timeout in seconds, also finite. Open-Meteo answers a single-day hourly
#: query in well under a second; a read that takes ten is a failure, not a wait.
DEFAULT_READ_TIMEOUT_S: float = 10.0

#: Total attempts, including the first one. Spec 12 allows at most one retry, so
#: the default is two attempts and there is no path to an unbounded retry.
DEFAULT_MAX_ATTEMPTS: int = 2

#: The provider identifier recorded in a reading.
PROVIDER_NAME = "open-meteo"

#: ``timezone=UTC`` makes every row instant unambiguous: the API returns
#: ``utc_offset_seconds: 0`` with rows written ``2026-09-26T00:00``, which the
#: normalisation layer reads as UTC. Requesting a named zone instead would only
#: move the conversion into the caller's hands.
REQUEST_TIMEZONE = "UTC"

#: A transport callable: anything that turns an :class:`httpx.Request` into an
#: :class:`httpx.Response`. ``httpx.MockTransport`` satisfies this, and so does a
#: hand-written stub, which is what the tests use to keep pytest offline.
Transport = Callable[[Any], Any]


class OpenMeteoWeatherProvider(WeatherProvider):
    """Ask the live Open-Meteo forecast API for one position and one instant.

    The query is for the **date** of ``target_time``, because that is the granularity
    the API offers: the nearest hourly row is then matched locally by the shared
    grid rule, and the distance between the requested instant and that row is
    disclosed rather than hidden behind an interpolation.

    Args:
        transport: A transport callable or ``httpx.BaseTransport`` to send through.
            Injecting one is what lets the timeout, 5xx and malformed-response paths
            be tested with no network (spec 13). ``None`` means the real network.
        endpoint: API base URL. Overridable so a test can point at a stub server, or
            an operator at a mirror.
        connect_timeout_s / read_timeout_s: Finite timeouts (spec 12).
        max_attempts: Total attempts including the first. Default 2 -- one retry.
        client: An already-built ``httpx.Client``. Takes precedence over
            ``transport`` for a caller that wants its own connection pooling.
    """

    mode: WeatherMode = "live"

    def __init__(
        self,
        *,
        transport: Transport | None = None,
        endpoint: str = OPEN_METEO_ENDPOINT,
        connect_timeout_s: float = DEFAULT_CONNECT_TIMEOUT_S,
        read_timeout_s: float = DEFAULT_READ_TIMEOUT_S,
        max_attempts: int = DEFAULT_MAX_ATTEMPTS,
        client: Any | None = None,
    ) -> None:
        if connect_timeout_s <= 0 or read_timeout_s <= 0:
            raise WeatherProviderNotConfigured(
                f"timeouts must be positive, got connect={connect_timeout_s}s read={read_timeout_s}s; "
                "an infinite timeout is not a configuration this provider accepts (spec 12)"
            )
        if max_attempts < 1 or max_attempts > 2:
            raise WeatherProviderNotConfigured(
                f"max_attempts must be 1 or 2, got {max_attempts}; spec 12 allows at most one retry "
                "and forbids an unbounded retry loop"
            )
        self._transport = transport
        self._endpoint = endpoint
        self._connect_timeout_s = float(connect_timeout_s)
        self._read_timeout_s = float(read_timeout_s)
        self._max_attempts = int(max_attempts)
        self._client = client

    # ------------------------------------------------------------------ #
    # WeatherProvider
    # ------------------------------------------------------------------ #

    def describe(self) -> str:
        """One line naming this provider, its endpoint and its retry budget."""
        return (
            f"live:OpenMeteoWeatherProvider endpoint={self._endpoint} "
            f"timeout={self._connect_timeout_s}s/{self._read_timeout_s}s "
            f"attempts={self._max_attempts}"
        )

    def get_weather(
        self,
        *,
        latitude: float,
        longitude: float,
        target_time: datetime,
        analysis_time: datetime | None = None,
    ) -> WeatherReading:
        """The live forecast for ``(latitude, longitude)`` at ``target_time``.

        Raises:
            WeatherProviderUnavailable: transport failure (timeout, DNS, refused,
                5xx) after the configured attempts.
            WeatherTargetUnavailable: the API will not describe that date at all --
                it answers a request for a date outside its forecast range with HTTP
                400 ``start_date is out of allowed range``.
            WeatherProviderResponse: the response arrived but is not usable
                (non-JSON body, missing ``hourly`` block, a missing hazard variable,
                an unparseable instant, a unit this layer will not guess at).
            WeatherProviderNotConfigured: httpx is not installed.
        """
        require_aware(target_time, "target_time")
        if analysis_time is not None:
            require_aware(analysis_time, "analysis_time")

        retrieved_at = datetime.now(timezone.utc)
        response = self._fetch(latitude=latitude, longitude=longitude, target_time=target_time)
        payload = _decode_body(response)
        series = _parse_series(payload, target_time=target_time, retrieved_at=retrieved_at)
        row, reach = nearest_row(series.rows, target_time)

        notes = [
            "open-meteo returns no model-run instant, so forecast_generated_at is the retrieval "
            "instant and freshness_minutes measures the retrieval age, not the upstream model age"
        ]
        notes.append(
            f"requested rows for {target_time.date().isoformat()}; the discrete hourly grid row "
            f"{row.target_time.isoformat()} was {reach:.1f} min from the requested instant"
        )
        if series.latitude is not None and series.longitude is not None:
            notes.append(
                f"open-meteo answered for its grid point ({series.latitude}, {series.longitude}), "
                f"not the projected position ({latitude}, {longitude})"
            )
        if series.confidence_is_assumed:
            notes.append(
                "open-meteo declares no provider confidence, so confidence is the calibration's "
                "unknown value charged in its absence rather than a figure the provider asserted"
            )

        return WeatherReading(
            weather=series.build_weather(
                row=row,
                target_time=target_time,
                source=f"{PROVIDER_NAME}@{row.target_time.isoformat()}",
                used_at=retrieved_at,
            ),
            provider=PROVIDER_NAME,
            mode="live",
            row_time=row.target_time,
            reach_minutes=reach,
            confidence_is_assumed=series.confidence_is_assumed,
            analysis_time=analysis_time,
            notes=tuple(notes),
        )

    # ------------------------------------------------------------------ #
    # Transport (the only place a socket is opened)
    # ------------------------------------------------------------------ #

    def _fetch(self, *, latitude: float, longitude: float, target_time: datetime) -> Any:
        """Send the forecast request, retrying at most once, and return the response.

        Retries cover transport failures and 5xx only. A 4xx is the API saying no
        deterministically, so retrying it would only double the latency of a
        certain failure.
        """
        httpx = _import_httpx()
        params = build_params(latitude, longitude, target_time)

        for attempt in range(1, self._max_attempts + 1):
            request = httpx.Request("GET", self._endpoint, params=params)
            try:
                response = self._client_or(httpx).send(request)
                if self._retryable(response, attempt):
                    continue
                return response
            except httpx.TimeoutException as exc:
                if self._retrying(attempt):
                    continue
                raise WeatherProviderUnavailable(
                    f"open-meteo did not answer {self._endpoint} within "
                    f"{self._connect_timeout_s}s connect / {self._read_timeout_s}s read after "
                    f"{attempt} attempt(s); a timeout is not a licence to guess weather"
                ) from exc
            except httpx.TransportError as exc:
                if self._retrying(attempt):
                    continue
                raise WeatherProviderUnavailable(
                    f"open-meteo could not be reached at {self._endpoint} after {attempt} attempt(s): "
                    f"{type(exc).__name__}: {exc}; replay is not substituted for a failed live call"
                ) from exc
        raise WeatherProviderUnavailable(  # pragma: no cover - loop guards above always return or raise
            f"open-meteo did not answer {self._endpoint} within {self._max_attempts} attempt(s)"
        )

    def _retryable(self, response: Any, attempt: int) -> bool:
        """Whether a 5xx response should be retried on this attempt."""
        return int(getattr(response, "status_code", 0)) >= 500 and self._retrying(attempt)

    def _retrying(self, attempt: int) -> bool:
        """Whether another attempt remains after this one (spec 12: at most one retry)."""
        return attempt < self._max_attempts

    def _client_or(self, httpx: Any) -> Any:
        """The client to send through, built lazily on first use.

        Built lazily so importing this module never requires httpx: the whole live
        path is optional, and a build without the weather extra must still be able to
        import the package and run in replay mode.
        """
        if self._client is not None:
            return self._client
        transport = self._transport if self._transport is not None else httpx.HTTPTransport(retries=0)
        return httpx.Client(
            transport=transport,
            timeout=httpx.Timeout(
                connect=self._connect_timeout_s, read=self._read_timeout_s, write=5.0, pool=5.0
            ),
            follow_redirects=True,
        )


# --------------------------------------------------------------------------- #
# Module-level helpers (pure, so a test can call them without a transport)
# --------------------------------------------------------------------------- #


def _import_httpx() -> Any:
    """Import httpx on demand, naming the extra that provides it when absent."""
    try:
        import httpx
    except ImportError as exc:  # pragma: no cover - depends on installed extras
        raise WeatherProviderNotConfigured(
            "live weather needs httpx; install the 'weather' extra "
            "(pip install -e '.[weather]') or choose weather_policy.mode = 'replay'"
        ) from exc
    return httpx


def _decode_body(response: Any) -> Any:
    """The response body as parsed JSON, or a loud error naming what arrived.

    A non-2xx status is turned into the error that fits it: a 400 whose body says
    the date is out of range is a target the provider cannot describe, and anything
    else non-2xx is a response that arrived but cannot be used.
    """
    status = int(getattr(response, "status_code", 0))
    text = str(getattr(response, "text", "") or "")
    payload: Any = None
    decode_error: Exception | None = None
    try:
        payload = response.json()
    except Exception as exc:  # httpx raises json.JSONDecodeError / ValueError
        decode_error = exc

    if status >= 400:
        reason = _error_reason(payload) or (decode_error.args[0] if decode_error else text[:200])
        if status in (400, 404):
            raise WeatherTargetUnavailable(
                f"open-meteo refused the request with HTTP {status}: {reason}; "
                "the requested instant is outside what this provider can describe"
            )
        raise WeatherProviderResponse(
            f"open-meteo returned HTTP {status} for the query: {reason}"
        )
    if decode_error is not None:
        raise WeatherProviderResponse(
            f"the open-meteo response body is not JSON ({decode_error}); "
            f"first 200 bytes: {text[:200]!r}"
        )
    return payload


def _error_reason(payload: Any) -> str:
    """The ``reason`` an Open-Meteo error document carries, or a dump of the body."""
    if isinstance(payload, dict):
        reason = payload.get("reason") or payload.get("error")
        if isinstance(reason, str) and reason.strip():
            return reason.strip()
        return str(payload)[:300]
    return str(payload)[:300]


def _parse_series(payload: Any, *, target_time: datetime, retrieved_at: datetime) -> Any:
    """Normalise the payload, declaring the retrieval instant as its fallback stamp.

    The fallback is used only because the live API declares no generation instant of
    its own; a payload that *does* declare one keeps it (the shared parser prefers
    the declaration).
    """
    try:
        return parse_open_meteo_payload(payload, ref="open-meteo response", generated_at=retrieved_at)
    except WeatherPayloadError as exc:
        raise WeatherProviderResponse(f"the open-meteo response could not be normalised: {exc}") from exc


def build_params(latitude: float, longitude: float, target_time: datetime) -> list[tuple[str, str]]:
    """The query parameters for one (position, instant) request.

    Exposed so a test can assert exactly what is asked for -- which date, which
    timezone, which variables -- without going through a transport.
    """
    return [
        ("latitude", f"{latitude:.6f}"),
        ("longitude", f"{longitude:.6f}"),
        ("hourly", ",".join(WEATHER_VARIABLES)),
        ("timezone", REQUEST_TIMEZONE),
        ("start_date", target_time.date().isoformat()),
        ("end_date", target_time.date().isoformat()),
    ]
