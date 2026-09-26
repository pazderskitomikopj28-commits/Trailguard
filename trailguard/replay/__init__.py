"""TrailGuard replay assets: deterministic inputs the offline runs are made of (spec 17, 10.1).

Two families of asset live here, and both exist so that a run can be repeated
byte-for-byte on a machine with no network:

* ``replay/routes/*.gpx`` -- real (synthetic but geometrically valid) GPX routes,
  parsed by :mod:`trailguard.routes.geometry`. The frozen scenarios all point at
  ``replay/routes/scn_route_sample.gpx``; every route asset here is synthetic and
  marked as such (spec 27, Phase 1 brief section 18), and no private location
  data is stored in any of them.
* ``replay/weather/*.json`` -- frozen weather snapshots read by
  :mod:`trailguard.replay.weather`. They are *not* open-meteo responses and not
  real weather records; a replay fixture cannot honestly claim a per-coordinate
  forecast, so each snapshot declares itself synthetic in its ``provider_note``.

Phase 1 reads these files and nothing else. Live weather (open-meteo for a
projected position at a target time) is Phase 2 work and is deliberately absent
here: this package has no HTTP client, no API key and no provider client.
"""

from __future__ import annotations

__all__: list[str] = []
