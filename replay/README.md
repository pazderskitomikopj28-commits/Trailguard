# Replay Mode — frozen demo and benchmark inputs

Source of truth: spec sections 13, 17 (and the labelling rule of section 27).

Replay Mode is what makes a TrailGuard demo and a TrailGuard benchmark
**reproducible**: the route, the weather and the movement/device timelines are
fixed on disk, so a run depends only on the model and the skill condition — never
on the weather on the day, on a live provider being up, or on an API rate limit.

> 目的: Demo 可復現；不依賴現場天氣；不依賴臨時 API 波動；Benchmark 可重複。

Live Weather Mode stays an optional add-on (spec 17); nothing in the code depends
on it.

---

## 1. What lives here

```text
replay/
├── README.md                              this contract
├── routes/
│   └── scn_route_sample.gpx               synthetic sample route (fixed geometry)
└── weather/
    └── scn_weather_snapshot.json          synthetic hourly snapshot (fixed weather)
```

| Asset | Fixes | Consumed by |
| --- | --- | --- |
| `routes/scn_route_sample.gpx` | Route geometry: 12 synthetic WGS-84 trackpoints, elevations 1180 m → 2105 m, ~11.0 km along the polyline, ~925 m ascent | `get_route_context` (spec 5.1) → `RouteContext.route_polyline_ref` |
| `weather/scn_weather_snapshot.json` | One Open-Meteo-shaped hourly response for 2026-05-18, 06:00–19:00 local, 14 hours, with a fixed `generated_at` | `get_weather_for_route_time` (spec 5.3) in `mode: "replay"` → `WeatherForRouteTime` |

The v1 movement and device timelines are **not** separate files: they are carried
inline in each scenario YAML (`Scenario.movement_timeline`,
`Scenario.device_timeline`), which keeps one scenario self-contained and
byte-identical between the two skill conditions. The `replay/timelines/`
directory reserved by spec 24 is for externalised timelines in a later phase and
is intentionally not created yet.

### Synthetic labelling (spec 27)

Both assets are invented fixtures and say so in-band: the GPX carries
`name="TrailGuard synthetic sample route"` and a metadata description stating it
is not a real trail and not a recorded track, the JSON carries
`"synthetic": true` and `"source": "synthetic fixture, not live data"`. Neither
asset contains real personal data. Neither may be presented as a real trail, a
real forecast or a real incident record.

---

## 2. How a scenario references these assets

All references are **repo-root-relative strings**, so a scenario file stays valid
wherever the repo is checked out:

```yaml
route_ref: "replay/routes/scn_route_sample.gpx"          # Scenario.route_ref
weather_snapshot_ref: "replay/weather/scn_weather_snapshot.json"   # Scenario.weather_snapshot_ref
initial_context:
  weather_policy:
    mode: "replay"                       # never "live" for a replay scenario
    snapshot_ref: "replay/weather/scn_weather_snapshot.json"
    max_freshness_min: 180
  route:
    value:
      route_polyline_ref: "replay/routes/scn_route_sample.gpx"
```

`WeatherPolicy.mode` must be `"replay"` for these scenarios; `max_freshness_min`
is the freshness budget the skill's freshness protocol (spec 10.4) checks the
snapshot's age against.

---

## 3. Weather freshness in replay mode

A static snapshot has no live timestamp, so its age is
`analysis_time - snapshot.generated_at` (both ISO8601, timezone-aware).
`generated_at` is pinned to `2026-05-18T10:00:00Z` (= 12:00 local, `Europe/Zurich`),
which makes the three shipped scenarios exercise the whole freshness vocabulary
against the 180-minute budget:

| Scenario | `analysis_time` (local) | Snapshot age | `freshness_status` |
| --- | --- | --- | --- |
| `scenarios/baseline/scn_clear_day_normal.yaml` | 12:30 | 30 min | `FRESH` |
| `scenarios/compound/scn_sunset_margin_pace_drop.yaml` | 14:50 | 170 min | `FRESH` (inside budget) |
| `scenarios/edge_cases/scn_stale_gps_missing_fix.yaml` | 16:10 | 250 min | `STALE` (outside budget) |

Replay weather is deliberately never refreshed: a stale snapshot is *data*, and
the run's job is to disclose it (spec 10.4), not to quietly fetch fresher values.
The classification itself is `WeatherForRouteTime.freshness_status`; this README
only documents the expected ages.

### Field-shape note

`precipitation_probability` is reported in percent (`%` in `hourly_units`), exactly
as Open-Meteo does. A replay consumer must divide by 100 to obtain the
`precipitation_probability` in `[0, 1]` that `WeatherForRouteTime` requires.
`weather_code` is a WMO-style code (0 clear … 3 overcast).

### What the fixture does *not* contain

The snapshot's hazard values never cross the R-C demo-calibration thresholds
(wind gust ≤ 27.4 km/h vs `wind_gust_kmh: 45`; precipitation ≤ 20% vs
`precipitation_probability: 0.7`; temperature ≥ 6.8 °C vs `temperature_c_low: -5`).
That is intentional: R-C is exercised by later scenarios, not by this one.

---

## 4. Boundaries

Replay Mode is **responsible for**: providing byte-stable inputs so that the same
model + the same SKILL.md produce the same evidence trace, and so a demo never
depends on a live provider.

Replay Mode is **not responsible for**:

* running a scenario — that is `evals/runner.py` (Phase 5) through
  `trailguard.agent.harness.run_agent` (spec 15);
* deciding whether a snapshot is fresh enough — `WeatherPolicy.max_freshness_min`
  plus the skill's freshness protocol (spec 10.4) do that;
* route math, ETA or sunset math — Phase 1 deterministic core;
* fetching anything over the network — `WeatherPolicy.mode: "live"` and Phase 2.
