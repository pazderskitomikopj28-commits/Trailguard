"""``get_user_device_context`` tool -- device and hiker-state feed (spec 5.4, Phase 1).

The tool is the seam between "a device id" and the device half of a context
snapshot. It reads a *replay device feed* -- a frozen JSON asset under
``replay/devices/`` generated from a frozen scenario's own timeline
(:mod:`trailguard.replay.devices`) -- and reports it. It contacts no real device,
opens no platform API and reads no clock, so the same id gives the same state in
every run (spec 19).

What it deliberately does not do: decide whether the battery is enough. That
combination belongs to rule R-E inside the deterministic core, and a tool that
judged sufficiency would duplicate a threshold in two places that could then
disagree.
"""

from __future__ import annotations

from ..replay.devices import DeviceFeed, DeviceFeedError, load_device_feed
from ..schemas.context import DeviceState

from ._arguments import binds_input_contract
from .contracts import GetUserDeviceContextInput

__all__ = ["get_user_device_context"]


@binds_input_contract(GetUserDeviceContextInput)
def get_user_device_context(device_id: str) -> DeviceState:
    """Read the phone/watch state used by the deterministic core (spec 5.4).

    Responsibilities (spec 5.4):
      * expose battery level, network availability and the last successful
        network contact instant for ``device_id``;
      * expose ``fatigue_index`` plus the movement window the pace-degradation
        rule R-D consumes -- whose fields (``current_speed_kmh``,
        ``avg_speed_10m``, ``avg_speed_30m``, ``pace_drop_percent``) the
        :class:`~trailguard.schemas.context.MovementState` block does carry;
      * expose the device feed's own ``data_quality`` score, which is the feed's
        self-assessment and is not the same number as the aggregated
        ``DataQualityReport.device_data_quality`` of a context snapshot.

    NOT responsible for (spec 5.4, 22):
      * any medical or physiological conclusion -- ``fatigue_index`` is a
        rule-constructed simulated feature, never a medical model output, and
        must never be presented as one;
      * risk levels, projections or weather;
      * judging whether the remaining battery is "enough" -- that combination
        belongs to rule R-E.

    Simulated inputs: the first version may read simulated device inputs
    (spec 5.4). Every feed this reads says so in its own ``provenance_note``, so
    a simulated reading can never be mistaken for a real one downstream.

    Args:
        device_id: Identifier of the hiker's device / feed, i.e. the file stem of
            an asset under ``replay/devices/`` (e.g.
            ``scn_device_S01_clear_day``).

    Returns:
        A :class:`~trailguard.schemas.context.DeviceState` with
        ``battery_percent``, ``network_available``, ``fatigue_index``, the
        observed ``movement`` window, the feed's ``data_quality`` and
        ``last_network_time`` -- the complete device feed of spec 5.4, at the
        feed's t=0 row.

    Raises:
        DeviceFeedError: no replay feed matches ``device_id`` (the message lists
            the feeds that do), or the asset is unreadable. An unknown device is
            missing data (spec 10.3), and the honest answer is a named failure --
            never a plausible default battery level.
    """
    feed: DeviceFeed = load_device_feed(device_id)
    return feed.current
