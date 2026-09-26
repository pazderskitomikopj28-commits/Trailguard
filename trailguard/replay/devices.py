"""Replay device feeds: a deterministic stand-in for a phone/watch reading.

Phase 1 needs *some* device input for ``get_user_device_context``, and the frozen
replay scenarios already carry one -- each scenario's ``device_timeline``. This
module reads exactly that and nothing else: no real device is contacted, no
platform API is queried, no clock is read.

What a feed is
--------------
A JSON document holding a ``current`` device state (the t=0 row) plus a
``timeline`` of later rows keyed by ``t_offset_min``. Every asset in
``replay/devices/`` was generated from a frozen scenario's own timeline, so its
t=0 row is byte-identical to that scenario's ``initial_context.device.value`` --
which :mod:`tests.test_device_feeds` cross-checks, so a hand-edited feed cannot
quietly disagree with the scenario that owns it.

What the tool reads
-------------------
``get_user_device_context`` answers for *now*, so it returns the feed's ``current``
row. The timeline walk (``state_at(30)``, ``state_at(60)``, ...) is a replay-run
concern -- the benchmark harness steps a scenario forward, and the atomic tool
stays atomic. Both paths read the same file, so they cannot diverge about what
the feed says.

Three honest choices worth stating
----------------------------------
1. **an unknown feed fails loudly.** A feed id that resolves to no asset raises
   :class:`DeviceFeedError` listing the feeds that *do* exist, rather than
   returning a plausible default -- a device nobody can name must not produce a
   battery reading.
2. **an out-of-range offset fails loudly too.** ``state_at`` does not extrapolate
   a feed beyond its last row, for the same reason the weather snapshot does not:
   the number a caller would get is not a number anyone wrote.
3. **the row that covers an offset is the row at or before it.** A feed is a step
   function, not an interpolation: between two rows the latest known state is the
   honest answer, and interpolating a battery level would produce a reading the
   device never reported.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from functools import lru_cache
from pathlib import Path
from typing import Any, Mapping

from ..schemas.context import DeviceState

__all__ = [
    "DEFAULT_DEVICE_FEED_DIR",
    "DeviceFeed",
    "DeviceFeedError",
    "known_device_feeds",
    "load_device_feed",
]

#: Where replay device feeds live (spec 24).
DEFAULT_DEVICE_FEED_DIR: Path = Path(__file__).resolve().parents[2] / "replay" / "devices"


class DeviceFeedError(ValueError):
    """A replay device feed could not be read, parsed or answered from."""


@dataclass(frozen=True)
class DeviceFeed:
    """A frozen device feed and the two lookups the core needs.

    Immutable and cacheable: the same file yields the same rows in every run
    (spec 19), so a horizon loop asking for four offsets re-reads nothing.
    """

    device_id: str
    scenario_ref: str
    current: DeviceState
    timeline: tuple[tuple[float, DeviceState], ...]
    note: str = ""

    def state_at(self, offset_min: float) -> DeviceState:
        """The device state in force ``offset_min`` after the feed's t=0 row.

        The latest row at or before ``offset_min`` wins (step function, not
        interpolation). Offset 0 or any offset before the first row returns
        ``current``; an offset past the last row raises, because a feed is not
        extrapolated.

        Raises:
            DeviceFeedError: ``offset_min`` is beyond the feed's last row.
        """
        offset = float(offset_min)
        if offset < 0.0:
            raise DeviceFeedError(
                f"feed {self.device_id}: t_offset_min must be >= 0, got {offset_min}"
            )
        if self.timeline and offset > self.timeline[-1][0]:
            raise DeviceFeedError(
                f"t_offset_min {offset_min} is beyond feed {self.device_id}, whose last row is at "
                f"+{self.timeline[-1][0]:g} minutes; a replay feed is not extrapolated beyond its own rows"
            )
        chosen = self.current
        for row_offset, state in self.timeline:
            if row_offset > offset:
                break
            chosen = state
        return chosen


def known_device_feeds(directory: str | Path | None = None) -> list[str]:
    """Device feed ids discoverable under ``directory``, sorted.

    Used to make an unknown-id error actionable: the message can name the feeds
    that exist instead of only saying this one does not.
    """
    return sorted(
        path.stem for path in _feed_dir(directory).glob("*.json") if path.is_file()
    )


def load_device_feed(device_id: str, directory: str | Path | None = None) -> DeviceFeed:
    """Load the device feed named ``device_id``.

    Args:
        device_id: Feed identifier, i.e. the asset's file stem
            (``scn_device_S01_clear_day``).
        directory: Directory to look in. Defaults to :data:`DEFAULT_DEVICE_FEED_DIR`,
            the ``replay/devices`` assets that ship with the package.

    Returns:
        The parsed :class:`DeviceFeed`.

    Raises:
        DeviceFeedError: no asset matches ``device_id`` (the message lists the
            feeds that do exist), the file is unreadable or not JSON, or a row
            cannot be read as a :class:`~trailguard.schemas.context.DeviceState`.
    """
    return _cached_feed(str(_resolve(device_id, directory).expanduser().resolve()))


def _resolve(device_id: str, directory: str | Path | None) -> Path:
    """The JSON asset for ``device_id``, or a loud error naming what exists."""
    if not isinstance(device_id, str) or not device_id.strip():
        raise DeviceFeedError(f"device_id must be a non-empty string, got {device_id!r}")
    safe = Path(device_id).name
    if safe != device_id:
        raise DeviceFeedError(
            f"device_id {device_id!r} is not a feed name: a feed is looked up by its file stem under "
            "replay/devices, never by a path"
        )
    candidate = _feed_dir(directory) / f"{safe}.json"
    if not candidate.is_file():
        available = known_device_feeds(directory)
        raise DeviceFeedError(
            f"no replay device feed named {device_id!r} under {_feed_dir(directory)}; "
            f"known feeds: {', '.join(available) if available else '(none)'}"
        )
    return candidate


def _feed_dir(directory: str | Path | None) -> Path:
    return DEFAULT_DEVICE_FEED_DIR if directory is None else Path(directory)


@lru_cache(maxsize=8)
def _cached_feed(path_key: str) -> DeviceFeed:
    """Read and parse (and remember) the feed at ``path_key``.

    Cached by resolved path because a horizon loop asks for the same feed at four
    offsets and the parse is deterministic.
    """
    path = Path(path_key)
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise DeviceFeedError(f"replay device feed {path} could not be read: {exc}") from exc
    return _feed_from_payload(payload, ref=path.name)


def _feed_from_payload(payload: Mapping[str, Any], *, ref: str) -> DeviceFeed:
    """Build a feed from a parsed JSON document, validating what it claims."""
    if not isinstance(payload, Mapping):
        raise DeviceFeedError(f"device feed {ref} must be a JSON object at the top level")

    device_id = payload.get("device_id")
    if not isinstance(device_id, str) or not device_id.strip():
        raise DeviceFeedError(f"device feed {ref} must carry a non-empty 'device_id'")

    current = _state(payload.get("current"), f"{ref}: current")

    timeline_raw = payload.get("timeline", [])
    if not isinstance(timeline_raw, list):
        raise DeviceFeedError(f"device feed {ref} must carry a 'timeline' list")
    timeline: list[tuple[float, DeviceState]] = []
    for index, row in enumerate(timeline_raw):
        where = f"{ref}: timeline[{index}]"
        if not isinstance(row, Mapping):
            raise DeviceFeedError(f"{where} must be an object")
        offset = row.get("t_offset_min")
        if isinstance(offset, bool) or not isinstance(offset, (int, float)):
            raise DeviceFeedError(f"{where}: t_offset_min must be a number, got {offset!r}")
        timeline.append((float(offset), _state(row.get("value"), f"{where}: value")))
    timeline.sort(key=lambda item: item[0])
    offsets = [offset for offset, _ in timeline]
    if len(set(offsets)) != len(offsets):
        raise DeviceFeedError(f"device feed {ref} has duplicate t_offset_min rows: {offsets}")

    return DeviceFeed(
        device_id=device_id,
        scenario_ref=str(payload.get("scenario_ref") or ""),
        current=current,
        timeline=tuple(timeline),
        note=str(payload.get("feed_note") or ""),
    )


def _state(raw: Any, where: str) -> DeviceState:
    """Validate one device-state row, naming the row that failed."""
    if not isinstance(raw, Mapping):
        raise DeviceFeedError(f"{where} must be a JSON object")
    try:
        return DeviceState.model_validate(dict(raw))
    except Exception as exc:  # pydantic ValidationError -> actionable message
        raise DeviceFeedError(f"{where} is not a usable DeviceState: {exc}") from exc
