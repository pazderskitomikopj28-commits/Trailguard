"""``get_user_device_context`` tool -- device and hiker-state feed (spec 5.4).

Phase 0 scope: contract only. The function body is a stub that raises
``NotImplementedError`` naming the phase that owns the implementation.
"""

from ..schemas.context import DeviceState


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
    (spec 5.4), and the frozen contract therefore carries the provenance
    disclosure needed to tell a simulated feed from a real one.

    Inputs:
      * ``device_id`` -- identifier of the hiker's device / feed.

    Outputs:
      A :class:`~trailguard.schemas.context.DeviceState` with
      ``battery_percent``, ``network_available``, ``fatigue_index``, the
      observed ``movement`` window, the feed's ``data_quality`` and
      ``last_network_time`` -- the complete device feed of spec 5.4.

    Phase 0 stub: the deterministic device-feed implementation belongs to
    Phase 1 (deterministic core, "battery / network") and therefore raises
    ``NotImplementedError``.
    """
    raise NotImplementedError("Phase 1: deterministic device feed (battery, network, simulated fatigue feature)")
