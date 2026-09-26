"""Replay device-feed tests (spec 5.4, 17, 19, 24).

``get_user_device_context`` reads a replay feed, so these tests are about the
*asset*: that every shipped feed is the scenario it belongs to, that its rows are
step-addressable and never extrapolated, and that an unknown feed fails loudly
instead of returning a plausible battery level.

The load-bearing test is the cross-check against the scenario files. Each feed is
generated from its scenario's own ``device_timeline``; if the two drift apart, a
benchmark run would silently use device data the scenario does not describe --
which is exactly the kind of quiet disagreement a data-only asset cannot detect
about itself.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

from conftest import REPO_ROOT, SCENARIO_FILES
from trailguard.replay.devices import (
    DEFAULT_DEVICE_FEED_DIR,
    DeviceFeed,
    DeviceFeedError,
    known_device_feeds,
    load_device_feed,
)
from trailguard.schemas import Scenario
from trailguard.tools import get_user_device_context


# --------------------------------------------------------------------------- #
# The shipped assets (spec 24)
# --------------------------------------------------------------------------- #


def _scenarios() -> list[Scenario]:
    """Every frozen scenario, validated."""
    return [Scenario.model_validate(yaml.safe_load(path.read_text(encoding="utf-8"))) for path in SCENARIO_FILES]


def test_the_replay_directory_ships_one_feed_per_scenario() -> None:
    """Each scenario owns exactly one device feed, named after its id."""
    feeds = known_device_feeds()

    expected = sorted(f"scn_device_{scenario.scenario_id}" for scenario in _scenarios())
    assert feeds == expected, "a scenario without a feed cannot be replayed, and a feed without a scenario is dead data"


def test_every_feed_names_a_scenario_file_that_exists() -> None:
    """``scenario_ref`` must resolve inside this repository."""
    for device_id in known_device_feeds():
        feed = load_device_feed(device_id)
        assert (REPO_ROOT / feed.scenario_ref).is_file(), f"{device_id} points at {feed.scenario_ref!r}"


@pytest.mark.parametrize("scenario", _scenarios(), ids=lambda scenario: scenario.scenario_id)
def test_the_feed_t_zero_row_is_the_scenario_device_state(scenario: Scenario) -> None:
    """The feed's t=0 row is byte-identical to the scenario's own device value.

    This is the cross-check the feed's own provenance note promises. It compares
    the *validated contracts* rather than the JSON text, so a reordering of keys
    does not fail a test that is about the data.
    """
    feed = load_device_feed(f"scn_device_{scenario.scenario_id}")

    assert feed.current == scenario.initial_context.device.value
    assert feed.current.model_dump(mode="json") == scenario.initial_context.device.value.model_dump(mode="json")


@pytest.mark.parametrize("scenario", _scenarios(), ids=lambda scenario: scenario.scenario_id)
def test_every_scenario_row_is_answerable_from_the_feed(scenario: Scenario) -> None:
    """Every device row the scenario names is the row the feed returns for that offset.

    The comparison is through ``state_at`` rather than against the timeline
    directly, because the feed stores the scenario's t=0 row as ``current`` while
    the scenario lists it as its first timeline point. Asserting through the one
    lookup the core actually uses means a feed cannot drift from the scenario by
    reshuffling that boundary.
    """
    feed = load_device_feed(f"scn_device_{scenario.scenario_id}")

    assert feed.current == scenario.initial_context.device.value, "the t=0 row is the scenario's initial device state"

    for point in scenario.device_timeline:
        assert feed.state_at(point.t_offset_min) == point.value, (
            f"offset {point.t_offset_min}: the feed disagrees with the scenario's own row"
        )
    # And the feed adds nothing the scenario does not describe.
    covered = {point.t_offset_min for point in scenario.device_timeline}
    for offset, _ in feed.timeline:
        assert offset in covered, f"the feed carries a row at +{offset:g} minutes its scenario never describes"


@pytest.mark.parametrize("device_id", known_device_feeds())
def test_every_feed_discloses_that_it_is_simulated(device_id: str) -> None:
    """A simulated reading must never be mistakable for a real one (spec 5.4, 27)."""
    document = json.dumps(_raw_feed(device_id)).lower()

    assert "not a real device reading" in document
    assert "not a real incident record" in document
    assert "medical" in document, "the fatigue index must carry its non-medical disclaimer"


def _raw_feed(device_id: str) -> dict:
    """The feed's JSON document as parsed, for prose assertions."""
    path = DEFAULT_DEVICE_FEED_DIR / f"{device_id}.json"
    return json.loads(path.read_text(encoding="utf-8"))


# --------------------------------------------------------------------------- #
# The lookups the core and the replay walker use
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("device_id", known_device_feeds())
def test_loading_the_same_feed_twice_gives_the_same_state(device_id: str) -> None:
    """A cached read must be indistinguishable from a fresh one (spec 19)."""
    first = load_device_feed(device_id)
    second = load_device_feed(device_id)

    assert first == second
    assert first.current.model_dump(mode="json") == second.current.model_dump(mode="json")


@pytest.mark.parametrize("device_id", known_device_feeds())
def test_state_at_is_a_step_function(device_id: str) -> None:
    """The row at or before the offset wins; nothing is interpolated."""
    feed = load_device_feed(device_id)
    offsets = [offset for offset, _ in feed.timeline]

    assert feed.state_at(0) == feed.current
    for offset in offsets:
        assert feed.state_at(offset) is feed.timeline[offsets.index(offset)][1]
        if offset > 0:
            # Just before the row: still the previous known state.
            previous = feed.current if offsets.index(offset) == 0 else feed.timeline[offsets.index(offset) - 1][1]
            assert feed.state_at(offset - 0.001) == previous


@pytest.mark.parametrize("device_id", known_device_feeds())
def test_a_feed_is_not_extrapolated_past_its_last_row(device_id: str) -> None:
    """An offset beyond the last row raises rather than inventing a reading."""
    feed = load_device_feed(device_id)
    last = feed.timeline[-1][0]

    with pytest.raises(DeviceFeedError) as excinfo:
        feed.state_at(last + 1.0)

    message = str(excinfo.value)
    assert "not extrapolated" in message
    assert device_id in message


@pytest.mark.parametrize("device_id", known_device_feeds())
def test_a_negative_offset_is_refused(device_id: str) -> None:
    """A feed describes the future of its t=0 row, never its past."""
    feed = load_device_feed(device_id)

    with pytest.raises(DeviceFeedError):
        feed.state_at(-1.0)


def test_an_unknown_feed_names_the_feeds_that_exist() -> None:
    """The error is actionable: it lists what is available (spec 10.3)."""
    with pytest.raises(DeviceFeedError) as excinfo:
        load_device_feed("scn_device_nobody")

    message = str(excinfo.value)
    assert "scn_device_nobody" in message
    for device_id in known_device_feeds():
        assert device_id in message


def test_a_path_is_not_a_feed_id() -> None:
    """A feed is looked up by its file stem, never by a path."""
    with pytest.raises(DeviceFeedError) as excinfo:
        load_device_feed("../devices/scn_device_S01_clear_day")

    assert "never by a path" in str(excinfo.value)


def test_an_empty_feed_id_is_refused() -> None:
    """An id nobody can name must not resolve to anything."""
    with pytest.raises(DeviceFeedError):
        load_device_feed("   ")


def _write_feed(directory: Path, payload: dict) -> None:
    """Write one feed document into ``directory``."""
    directory.mkdir(parents=True, exist_ok=True)
    (directory / f"{payload['device_id']}.json").write_text(json.dumps(payload), encoding="utf-8")


def test_duplicate_offsets_are_rejected(tmp_path: Path) -> None:
    """Two rows for the same offset describe two different devices at once."""
    base = load_device_feed("scn_device_S01_clear_day")
    _write_feed(
        tmp_path,
        {
            "device_id": "scn_device_duplicate_rows",
            "scenario_ref": base.scenario_ref,
            "current": base.current.model_dump(mode="json"),
            "timeline": [
                {"t_offset_min": 30.0, "value": base.timeline[0][1].model_dump(mode="json")},
                {"t_offset_min": 30.0, "value": base.timeline[1][1].model_dump(mode="json")},
            ],
        },
    )

    with pytest.raises(DeviceFeedError) as excinfo:
        load_device_feed("scn_device_duplicate_rows", directory=tmp_path)

    assert "duplicate t_offset_min" in str(excinfo.value)


def test_an_unreadable_device_state_is_rejected(tmp_path: Path) -> None:
    """A row that is not a DeviceState names the row that failed."""
    _write_feed(
        tmp_path,
        {
            "device_id": "scn_device_broken_row",
            "current": {"battery_percent": 140.0},
        },
    )

    with pytest.raises(DeviceFeedError) as excinfo:
        load_device_feed("scn_device_broken_row", directory=tmp_path)

    message = str(excinfo.value)
    assert "scn_device_broken_row.json: current is not a usable DeviceState" in message, message


def test_the_documents_own_device_id_is_what_the_feed_carries(tmp_path: Path) -> None:
    """The file stem is only the lookup key; the field names the feed.

    This pins the loader's actual rule so a later cross-check of the two is a
    decision rather than an accident. All shipped feeds agree, which
    ``test_the_replay_directory_ships_one_feed_per_scenario`` covers by
    constructing the id from the scenario.
    """
    base = load_device_feed("scn_device_S01_clear_day")
    payload = {
        "device_id": "some_other_id",
        "scenario_ref": base.scenario_ref,
        "current": base.current.model_dump(mode="json"),
        "timeline": [],
    }
    _write_feed(tmp_path, payload)

    feed = load_device_feed("some_other_id", directory=tmp_path)

    assert isinstance(feed, DeviceFeed)
    assert feed.device_id == "some_other_id"


# --------------------------------------------------------------------------- #
# The tool reads the t=0 row (spec 5.4)
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("scenario", _scenarios(), ids=lambda scenario: scenario.scenario_id)
def test_the_tool_returns_the_feed_t_zero_row(scenario: Scenario) -> None:
    """``get_user_device_context`` answers for now, so it returns ``current``."""
    state = get_user_device_context(f"scn_device_{scenario.scenario_id}")

    assert state == load_device_feed(f"scn_device_{scenario.scenario_id}").current
    assert state == scenario.initial_context.device.value
    assert state.battery_percent == scenario.initial_context.device.value.battery_percent
    assert state.movement.pace_drop_percent == scenario.initial_context.device.value.movement.pace_drop_percent
