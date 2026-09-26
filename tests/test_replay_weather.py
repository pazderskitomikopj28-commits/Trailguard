"""Replay weather-snapshot tests (spec 5.3, 17, 19, 24).

The frozen snapshot ``replay/weather/scn_weather_snapshot.json`` is stored in the
**open-meteo response shape**: parallel ``hourly`` arrays, ``utc_offset_seconds``
for its naive row instants, and ``hourly_units`` naming each variable's unit.
Phase 2 will parse that same shape from the live API, so these tests are about the
*parser*: that it converts what the file says rather than what a TrailGuard-native
file would have said.

The load-bearing cases are the unit and timezone conversions, because each one is a
place a plausible-looking wrong answer would live:

* precipitation arrives as **percent** and the schema wants a probability, so the
  ``/100`` is what keeps ``8`` percent from tripping R-C's 0.7 threshold;
* the row instants are **naive local wall-clock times**, so the offset is what
  keeps a 12:00 row from being read as 12:00Z when it means 10:00Z.

A snapshot declaring no provider confidence is charged the calibration's unknown
value rather than assumed perfect -- the same rule the confidence model applies to
every other undisclosed term, and the reason the shipped asset yields lower
confidence than a hand-written one would.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from conftest import REPO_ROOT
from trailguard.risk_core.calibration import CALIBRATION_PATH, load_calibration
from trailguard.replay.weather import (
    DEFAULT_SNAPSHOT_PATH,
    ReplaySnapshotError,
    WeatherSnapshot,
    load_weather_snapshot,
)
from trailguard.schemas import WeatherForRouteTime

#: The instant the shipped snapshot says it was generated.
GENERATED_AT = datetime(2026, 5, 18, 10, 0, tzinfo=timezone.utc)


def _write(payload: object, path: Path) -> WeatherSnapshot:
    """Write ``payload`` as the snapshot at ``path`` and load it."""
    path.write_text(json.dumps(payload), encoding="utf-8")
    return load_weather_snapshot(path)


def _open_meteo_document(**overrides: object) -> dict:
    """A minimal valid open-meteo-shaped snapshot, in the shipped asset's shape.

    Rows are naive local wall-clock times at ``utc_offset_seconds = 7200``
    (Europe/Zurich, GMT+2), exactly as the frozen asset writes them.
    """
    document: dict = {
        "source": "replay-test-snapshot",
        "generated_at": "2026-05-18T10:00:00Z",
        "confidence": 0.8,
        "note": "test fixture",
        "utc_offset_seconds": 7200,
        "hourly_units": {
            "time": "iso8601",
            "temperature_2m": "°C",
            "precipitation_probability": "%",
            "wind_speed_10m": "km/h",
            "wind_gusts_10m": "km/h",
        },
        "hourly": {
            # Local wall-clock times at GMT+2: 12:00 local is 10:00Z, which is the
            # generation instant, so these three rows bracket it in UTC.
            "time": ["2026-05-18T12:00", "2026-05-18T13:00", "2026-05-18T14:00"],
            "temperature_2m": [1.0, 2.0, 3.0],
            "precipitation_probability": [0, 50, 100],
            "wind_speed_10m": [4.0, 5.0, 6.0],
            "wind_gusts_10m": [7.0, 8.0, 9.0],
            "weather_code": [1, 2, 3],
        },
    }
    document.update(overrides)
    return document


# --------------------------------------------------------------------------- #
# The shipped asset (spec 24)
# --------------------------------------------------------------------------- #


def test_the_shipped_snapshot_is_the_one_the_scenarios_name() -> None:
    """The default path is the asset every frozen scenario points at."""
    assert DEFAULT_SNAPSHOT_PATH == REPO_ROOT / "replay" / "weather" / "scn_weather_snapshot.json"
    assert DEFAULT_SNAPSHOT_PATH.is_file(), "the replay scenarios name a snapshot that is not shipped"


def test_the_shipped_snapshot_declares_no_provider_confidence() -> None:
    """The frozen asset says nothing about its own confidence, honestly.

    The loader must therefore charge the calibration's unknown value instead of
    assuming a perfect one -- a synthetic fixture with invented values has not
    earned a 1.0. This is pinned because the alternative (defaulting to 1.0)
    quietly hands every verdict a higher confidence number than the file supports.
    """
    snapshot = load_weather_snapshot()

    assert snapshot.confidence_is_assumed is True
    assert snapshot.confidence == pytest.approx(load_calibration(CALIBRATION_PATH).confidence_unknown_value)


def test_the_shipped_snapshot_rows_are_local_wall_clock_times_made_aware(tmp_path: Path) -> None:
    """``2026-05-18T12:00`` at ``utc_offset_seconds: 7200`` is 10:00Z, not 12:00Z.

    Reading the rows as UTC would move every lookup two hours and silently change
    which row serves a projected arrival -- and, through ``freshness_minutes``,
    the confidence of every verdict built on it.
    """
    snapshot = _write(_open_meteo_document(), tmp_path / "local.json")

    assert snapshot.rows[0].target_time == GENERATED_AT
    assert snapshot.rows[0].target_time.tzinfo is not None
    assert snapshot.last_time == GENERATED_AT + timedelta(hours=2)
    assert snapshot.generated_at == GENERATED_AT


# --------------------------------------------------------------------------- #
# Unit conversion (spec 5.3)
# --------------------------------------------------------------------------- #


def test_percent_precipitation_becomes_a_probability(tmp_path: Path) -> None:
    """``hourly_units`` says ``%``, so 50 is a probability of 0.5.

    This is the conversion that decides whether R-C fires: read raw, a 50 percent
    row looks like an impossible probability and trips the 0.7 threshold; divided,
    it is an ordinary 0.5.
    """
    snapshot = _write(_open_meteo_document(), tmp_path / "pct.json")

    assert [row.precipitation_probability for row in snapshot.rows] == [0.0, 0.5, 1.0]


def test_a_unit_the_parser_will_not_guess_is_refused(tmp_path: Path) -> None:
    """A precipitation unit that is not a spelling of percent is a loud error.

    ``mm`` (a depth, not a probability) would silently re-scale the rule's
    threshold by a factor of 100 in an untraceable direction.
    """
    document = _open_meteo_document()
    document["hourly_units"]["precipitation_probability"] = "mm"

    with pytest.raises(ReplaySnapshotError, match="precipitation_probability"):
        _write(document, tmp_path / "mm.json")


def test_a_snapshot_declaring_a_unitless_probability_is_refused(tmp_path: Path) -> None:
    """Without ``hourly_units`` the scale is unknowable, so it is not assumed."""
    document = _open_meteo_document()
    del document["hourly_units"]["precipitation_probability"]

    with pytest.raises(ReplaySnapshotError, match="precipitation_probability"):
        _write(document, tmp_path / "nounit.json")


def test_variable_names_are_translated_and_units_checked(tmp_path: Path) -> None:
    """``temperature_2m`` -> ``temperature_c`` and the winds are renamed, not rescaled.

    The rename to ``_c`` and ``_kmh`` is only honest because the source unit was
    checked: a snapshot declaring Fahrenheit would otherwise become Celsius
    silently.
    """
    snapshot = _write(_open_meteo_document(), tmp_path / "names.json")

    assert [row.temperature_c for row in snapshot.rows] == [1.0, 2.0, 3.0]
    assert [row.wind_speed_kmh for row in snapshot.rows] == [4.0, 5.0, 6.0]
    assert [row.wind_gust_kmh for row in snapshot.rows] == [7.0, 8.0, 9.0]
    assert [row.weather_code for row in snapshot.rows] == [1, 2, 3]


def test_a_temperature_declared_in_the_wrong_unit_is_refused(tmp_path: Path) -> None:
    """Nothing is converted between incompatible temperature units -- it is a loud error."""
    document = _open_meteo_document()
    document["hourly_units"]["temperature_2m"] = "°F"

    with pytest.raises(ReplaySnapshotError, match="temperature_2m"):
        _write(document, tmp_path / "fahrenheit.json")


# --------------------------------------------------------------------------- #
# Shape validation
# --------------------------------------------------------------------------- #


def test_mismatched_array_lengths_are_refused(tmp_path: Path) -> None:
    """Parallel arrays must describe the same number of rows.

    Truncating to the shortest would produce a snapshot that looks complete while
    silently dropping its last rows -- and, for a temperature array, would pair
    values with the wrong instants.
    """
    document = _open_meteo_document()
    document["hourly"]["wind_gusts_10m"] = [7.0, 8.0]

    with pytest.raises(ReplaySnapshotError, match="wind_gusts_10m"):
        _write(document, tmp_path / "ragged.json")


def test_a_missing_hazard_array_names_what_is_absent(tmp_path: Path) -> None:
    """A snapshot without one hazard variable fails loudly and names it."""
    document = _open_meteo_document()
    del document["hourly"]["wind_gusts_10m"]

    with pytest.raises(ReplaySnapshotError, match="wind_gusts_10m"):
        _write(document, tmp_path / "missing.json")


def test_a_trailguard_native_document_is_refused(tmp_path: Path) -> None:
    """A ``series`` of objects is not this parser's shape -- and must fail loudly.

    This is a regression guard against quietly re-normalising the *data file*
    again: had the frozen asset been rewritten into a TrailGuard-native ``series``,
    this parser would stop being able to read it, and the honest answer is to say
    so rather than to change the asset.
    """
    document = {
        "snapshot_ref": "x",
        "source": "y",
        "generated_at": "2026-05-18T10:00:00Z",
        "confidence": 0.9,
        "series": [{"target_time": "2026-05-18T10:00:00Z", "temperature_c": 12.0,
                    "precipitation_probability": 0.05, "wind_speed_kmh": 8.0,
                    "wind_gust_kmh": 18.0, "weather_code": 1}],
    }

    with pytest.raises(ReplaySnapshotError, match="hourly"):
        _write(document, tmp_path / "native.json")


def test_a_declared_confidence_outside_the_unit_interval_is_refused(tmp_path: Path) -> None:
    """A provider confidence is a fraction, so 1.4 is a configuration error."""
    document = _open_meteo_document(confidence=1.4)

    with pytest.raises(ReplaySnapshotError, match="confidence"):
        _write(document, tmp_path / "overconfident.json")


def test_a_naive_generated_at_is_refused(tmp_path: Path) -> None:
    """The generation instant must carry its offset, or the ages are lies."""
    document = _open_meteo_document(generated_at="2026-05-18T10:00:00")

    with pytest.raises(ReplaySnapshotError, match="generated_at"):
        _write(document, tmp_path / "naive_gen.json")


def test_a_naive_target_time_is_refused(tmp_path: Path) -> None:
    """Comparing a naive target against aware rows must fail, not mis-compare."""
    snapshot = _write(_open_meteo_document(), tmp_path / "aware.json")

    with pytest.raises(ReplaySnapshotError, match="timezone-aware"):
        snapshot.at(datetime(2026, 5, 18, 10, 30))


# --------------------------------------------------------------------------- #
# Reading a row (spec 10.3, 17)
# --------------------------------------------------------------------------- #


def test_the_nearest_row_wins_and_names_itself(tmp_path: Path) -> None:
    """A request between two rows resolves to the closer one and says which.

    10:30Z is exactly between the 10:00Z row (temperature 1.0) and the 11:00Z row
    (2.0), so the tie is broken by the earlier row -- deterministically, and
    documented. 10:20Z is unambiguous and picks the 10:00Z row.
    """
    snapshot = _write(_open_meteo_document(), tmp_path / "near.json")

    tie = snapshot.at(GENERATED_AT + timedelta(minutes=30))
    assert tie.temperature_c == 1.0, "a tie between two rows resolves to the earlier one, every time"
    assert tie.target_time == GENERATED_AT + timedelta(minutes=30)

    closer = snapshot.at(GENERATED_AT + timedelta(minutes=20))
    assert closer.temperature_c == 1.0

    # 11:20Z is 20 minutes past the 11:00Z row and 40 before the 12:00Z one.
    later = snapshot.at(GENERATED_AT + timedelta(minutes=80))
    assert later.temperature_c == 2.0
    assert snapshot.row_for(later.target_time)[0].target_time == GENERATED_AT + timedelta(minutes=60)

    # ...and 11:40Z is closer to the 12:00Z row, so nearest-row really is nearest.
    nearest_last = snapshot.at(GENERATED_AT + timedelta(minutes=100))
    assert nearest_last.temperature_c == 3.0


def test_freshness_is_measured_against_the_instant_requested(tmp_path: Path) -> None:
    """``freshness_minutes`` is target - generated_at, not the caller's analysis clock.

    A projected arrival two hours out therefore reports a two-hour-old snapshot,
    which is what the age term of the confidence model needs.
    """
    snapshot = _write(_open_meteo_document(), tmp_path / "fresh.json")

    reading = snapshot.at(GENERATED_AT + timedelta(minutes=45))
    assert reading.freshness_minutes == pytest.approx(45.0)
    assert reading.forecast_generated_at == GENERATED_AT


def test_a_request_before_the_series_is_not_extrapolated(tmp_path: Path) -> None:
    """The first row is not extended backwards."""
    snapshot = _write(_open_meteo_document(), tmp_path / "before.json")

    with pytest.raises(ReplaySnapshotError, match="not extrapolated"):
        snapshot.at(GENERATED_AT - timedelta(minutes=1))


def test_a_request_after_the_series_is_not_extrapolated(tmp_path: Path) -> None:
    """The last row is not extended forwards, however close the caller is."""
    snapshot = _write(_open_meteo_document(), tmp_path / "after.json")

    with pytest.raises(ReplaySnapshotError, match="not extrapolated"):
        snapshot.at(GENERATED_AT + timedelta(hours=2, minutes=1))


def test_the_last_row_is_still_served(tmp_path: Path) -> None:
    """The boundary itself is inside the series, not outside it."""
    snapshot = _write(_open_meteo_document(), tmp_path / "last.json")
    last = snapshot.last_time

    assert snapshot.at(last).target_time == last


def test_a_converted_reading_is_a_valid_weather_contract(tmp_path: Path) -> None:
    """What comes back crosses the schema boundary as a normal contract object."""
    snapshot = _write(_open_meteo_document(), tmp_path / "valid.json")

    reading = snapshot.at(GENERATED_AT + timedelta(minutes=30))

    assert isinstance(reading, WeatherForRouteTime)
    assert 0.0 <= reading.precipitation_probability <= 1.0
    assert reading.source.startswith("replay-test-snapshot@")
    assert reading.confidence == pytest.approx(0.8)


def test_reloading_the_same_file_yields_the_same_snapshot(tmp_path: Path) -> None:
    """Same asset, same rows -- the replay contract (spec 19)."""
    path = tmp_path / "same.json"
    first = _write(_open_meteo_document(), path)
    second = load_weather_snapshot(path)

    assert [row.model_dump() if hasattr(row, "model_dump") else row for row in first.rows] == [
        row.model_dump() if hasattr(row, "model_dump") else row for row in second.rows
    ]
    assert first.generated_at == second.generated_at
    assert first.confidence == second.confidence
