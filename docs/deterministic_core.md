# The Deterministic Core (Phase 1)

Source of truth for the code: `trailguard/routes/`, `trailguard/risk_core/`,
`trailguard/tools/` and the two data files `trailguard/rules/rules_v1.yaml` +
`trailguard/rules/calibration_v1.yaml`. This page explains *how the numbers are
produced* and what each of them does and does not claim.

**No model, no key, no network, no GPU.** The chain below is pure arithmetic over
frozen assets. The LLM in TrailGuard reasons over its outputs and explains them; it
never contributes a number to them (spec 1, 20, 24).

```bash
.venv/bin/python -m trailguard.smoke     # runs the whole chain and prints every stage
.venv/bin/python -m pytest -q            # 544 tests, including the numbers below
```

---

## 1. The chain

```text
real GPX (replay/routes/scn_route_sample.gpx)
  -> get_route_context(route_id, position, timestamp)      RouteContext
  -> get_user_device_context(device_id)                    DeviceState   (replay feed, t=0 row)
  -> project_route_state(route, movement, h, fatigue)      RouteProjection (one call per horizon)
  -> get_weather_for_route_time(projected_position,
                                 projected_arrival_time)  WeatherForRouteTime
  -> assess_risk(route, weather, device, time, projection) RiskResult
```

The 30/60/120 loop above `project_route_state` belongs to the Skill (`SKILL.md`),
never to a tool: one call answers one horizon, and there is no `forecast_risk`
aggregate (spec 4.2). `python -m trailguard.smoke` runs the loop once, for the
S04 scenario, to demonstrate that a loop is *possible* -- it is not the loop.

## 2. Route geometry: geodesic metres, planar nearest-point

`trailguard/routes/geometry.py` never applies Pythagoras to latitude/longitude.
Two tools do two different jobs, and the split matters because it is where a hidden
flat-earth approximation would hide:

| job | tool | why |
| --- | --- | --- |
| every reported metre | `pyproj.Geod` (WGS-84) | distances between fixes and snapped points, per-segment lengths (`Geod.inv`), and the point reached by walking *d* metres (`Geod.fwd`) are all ellipsoidal |
| "which point of the polyline is closest?" | local azimuthal-equidistant projection + `shapely.LineString` | nearest-point-on-a-line is a planar operation; AEQD is exact at its centre and sub-centimetre in error over a hiking route |

The planar result is converted back and **re-measured geodesically**, so the
projection never leaks into a reported number -- it only decides which trackpoint
pair to interpolate in.

No DEM, no raster, no elevation service: elevation comes from the GPX itself, and
`remaining_ascent_m` sums the *positive* elevation deltas along the remaining
polyline, which is the honest meaning of "ascent still to climb" for a planned
route.

For the shipped asset (`scn_route_sample.gpx`, 12 trackpoints, 11 006.82 m), the
S01/S04 start position gives progress `0.34992459777308427`, remaining
`7.1552653262791` km, off-route `0.060984366915525015` m, remaining ascent
`726.0433618422851` m and altitude `1378.956638157715` m. Those numbers are pinned
in `tests/test_deterministic_core.py::test_the_real_gpx_route_context_is_the_pinned_geometry`.

`gpxpy`, `shapely` and `pyproj` are the `route` extra of `pyproject.toml` and are
imported lazily, so the contracts and the core import without them and the error
message names the extra.

## 3. The projection: one call, one horizon, clamped at the end

`project_route_state` walks the hiker `horizon_minutes x effective_speed` metres
along the **planned polyline** from where the route context says they are, and
reports:

* `projected_route_progress` / `projected_remaining_distance_km` / `projected_position`
  (with elevation interpolated geodesically between trackpoints);
* `projected_arrival_time` = `analysis_time + min(horizon, eta)`. This is the instant
  the projection **refers to**, not a predicted arrival: a hiker with a 400-minute ETA
  still has a +30-minute projection thirty minutes from now, and that is the instant
  its weather and its margins are evaluated at;
* `projection_confidence` = `route_data_quality x horizon_reliability x fatigue_term`,
  where `fatigue_term` is 1.0 when the caller disclosed a fatigue index and 0.5 when
  it did not (charged, not assumed).

A projection that overshoots the route's end stops **at** the end: "at the finish",
never "off the map". Two refusals are loud rather than silent: a `horizon_min` that is
not one of 0/30/60/120 raises, and a missing `analysis_time` raises rather than reading
the wall clock (spec 19).

## 4. Speed, ETA and the sunset margin

The frozen movement model (spec 6.3, `trailguard/risk_core/movement.py`):

```text
base_speed     = 0.5*avg_speed_10m + 0.3*avg_speed_30m + 0.2*current_speed
effective      = base_speed * (1 - min(fatigue_index, fatigue_penalty_cap))
eta_minutes    = remaining_km / effective * 60          # or None
pace_drop_pct  = max(avg_speed_30m - avg_speed_10m, 0) / avg_speed_30m * 100   # or None
```

Every weight and cap is a `competition_demo_calibration` value in
`calibration_v1.yaml`. No gradient-aware pace, no Naismith adjustment, no
personalisation, no learned component.

**A degenerate ratio returns `None`, never 0, `inf` or NaN.** A stationary hiker has
no finish time, so `estimated_minutes_to_finish`, `time_margin_min` and
`projected_battery_percent` are all `None`, the engine discloses each one in the
evidence trace, and no rule fires on a feature that could not be computed. (Returning
0.0 would be worse than useless: `0 < 30` disables a threshold while `0 > 50` also
disables one, so the direction of the lie would depend on the comparison.)

The margin of spec 8 is exactly the identity, measured from the instant the
assessment refers to:

```text
time_margin_min = minutes_to_sunset(projected_arrival_time)
                - estimated_minutes_to_finish(projected_remaining_distance, effective_speed)
```

At NOW the two instants coincide, which is where the plain formula is written. Two
consequences a reader should know:

* the margin is derived from the **instants** (`time_state.sunset_time` and the
  projected arrival), not from `time_state.minutes_to_sunset`; a stale count in that
  field is ignored, and `tests/test_deterministic_core.py` pins this;
* on a clear day the margin is nearly constant across horizons, and that is correct:
  the hiker walks in an hour exactly the distance that shortens the ETA by an hour.

`time_margin_min` is **minutes of daylight slack**, not a probability and not an
accident risk. It is never re-used as one.

## 5. Rule execution is data, not a chain of `if`s

`rules_v1.yaml` holds the six rules, their thresholds and their `trigger_type`
(`hard` for R-A and R-B, `soft` for R-C to R-F). `calibration_v1.yaml` holds the
*evaluation metadata*: which feature each threshold is compared against, in which
direction, at which band.

```yaml
# rules_v1.yaml -- R-A only; the other five follow the same shape
- id: R-A
  thresholds: {off_route_distance_m: 50, off_route_distance_m_severe: 150, persistence_observations: 3}
  severity: HIGH
  trigger_type: hard            # -> sets a severity floor, excluded from the soft score

# calibration_v1.yaml -- the matching evaluation for R-A
R-A:
  kind: threshold_clauses       # -> "evaluate my clauses"; R-F is kind: compound and has none
  clauses:
    - {feature: off_route_distance_m, operator: gt, band: warn,   threshold_key: off_route_distance_m}
    - {feature: off_route_distance_m, operator: gt, band: severe, threshold_key: off_route_distance_m_severe}
  non_evaluable_thresholds:
    persistence_observations: >
      a stateless single-horizon assessment holds one observation, so the deviation
      is reported as measured and persistence is not asserted
```

The split is the point: **thresholds are policy, evaluation is mechanism.** A threshold
never appears twice, and the engine contains no rule id in its control flow beyond
looking up this table. `Calibration.validate_against(pack)` proves the two files cover
each other -- every numeric threshold in the pack must be claimed by exactly one
clause, every clause must point at a threshold that exists, and every threshold has
either a clause or an explicit `non_evaluable_thresholds` reason. Re-tuning a band is
a data edit.

`FeatureVector` is the single place a contract field becomes a feature, and a feature
that cannot be computed is `None` -- which the engine turns into an explicit
"this rule could not be evaluated at this horizon" disclosure rather than a default
that would silently arm or disarm a threshold.

## 6. How a level is produced, in order

```text
features -> triggered rules (clause table)
        -> hard rules set a severity floor      (max by severity rank)
        -> soft rules aggregate a score         (hard rules and R-F excluded)
        -> score -> band                        (GREEN 0-24 / YELLOW 25-49 / ORANGE 50-74 / RED 75-100)
        -> R-F compound escalation              (+1 level, clipped at RED)
        -> final level = max(band, floor, escalated)   -- nothing may dial a level down
```

**Soft score.** Severity weights are LOW 10 / MEDIUM 25 / HIGH 40 / CRITICAL 50,
saturating at 100. One triggered soft rule contributes its own weight; the same rule
crossing two bands contributes once (the higher severity). Two rules fire, R-D
MEDIUM and R-E HIGH, and the score is 25 + 40 = 65 -- the ORANGE band.

**Hard rules set a floor and contribute nothing to the score.** R-A alone (75 m off
route) reports `ORANGE / score 0`: the level comes from its HIGH severity, and
counting those points as well would charge the same danger twice while making the
band invisible. R-B alone reports `RED / score 0`.

**R-F (compound) escalates a level and adds no points of its own.** It fires when two
or more *independent soft* rules at or above `min_severity` (MEDIUM) have fired, and
adds `escalation_levels` (1) to the level. Because it contributes no score, the pair
below is the proof that the escalation is what moved the level:

| `escalation_levels` | level | score |
| --- | --- | --- |
| 0 | ORANGE (the band, alone) | 65 |
| 1 | RED | 65 |
| 2 | RED (clipped, not wrapped) | 65 |

Exemptions from the score are derived from data -- the pack's `trigger_type` and the
calibration's `kind: compound` -- never from a rule id spelled out in the engine.

**Nothing lowers a level.** A hard rule can only raise it; the highest applicable
floor wins; the compound step only adds.

## 7. Confidence is an engineering statement, not a probability

`compute_confidence` (spec 10.3, 10.4, 8) answers "how much can this *assessment* be
trusted, given the data behind it?" -- never "how likely is harm". A 0.9-confidence RED
verdict does not mean 90 % dangerous; it means the inputs behind the verdict are good
ones.

Five weighted terms, all data in `calibration_v1.yaml`:

| term | weight | value |
| --- | --- | --- |
| `route_data_quality` | 0.20 | from the disclosed report |
| `device_data_quality` | 0.20 | from the disclosed report |
| `weather` | 0.25 | `1/(1 + age/180) x snapshot_confidence` |
| `projection` | 0.15 | the projection's own confidence at this horizon |
| `reported_overall_quality` | 0.20 | the caller's disclosed overall score |

Then a penalty per disclosed field: 0.05 for each missing one, 0.03 for each stale
one. An **undisclosed** term is charged `confidence_unknown_value = 0.5`, never assumed
perfect and never assumed absent -- which is why a caller who discloses no report at
all, with the same 30-minute weather and a NOW projection, lands on
`0.1 + 0.1 + 0.192857 + 0.15 + 0.1 = 0.642857` instead of being handed a number they
never earned. It is still an input-transparent number: change the weather age and it
moves.

Worked example (S01 at NOW, weather 30 minutes old, projection confidence 1.0,
report 0.92/0.95/0.93):

```text
0.20*0.92 + 0.20*0.95 + 0.25*(6/7*0.50) + 0.15*1.00 + 0.20*0.93 = 0.817143
```

The snapshot contributes **0.50**, not the 0.9 a reader might expect, because
`replay/weather/scn_weather_snapshot.json` declares no provider confidence of its
own: the loader charges the calibration's `confidence_unknown_value` rather than
assuming a perfect one. That is the same rule the model applies to every other
undisclosed term, and it is why the frozen fixtures land lower than a hand-written
snapshot would.

`tests/test_deterministic_core.py::test_confidence_is_the_calibrations_own_weights_over_the_inputs`
recomputes this from the loaded calibration, term by term, instead of copying a number
out of a run.

## 8. Determinism

* no business function reads the wall clock: every instant is passed in, and
  `RiskResult.computed_at` **is** the analysis instant, so a replay reproduces it byte
  for byte;
* the rule pack, the calibration, the GPX and the replay assets are parsed once and
  cached by resolved path;
* two identical `assess_risk` calls produce identical `model_dump(mode="json")`
  output, evidence trace included -- asserted in the test module;
* no result may carry NaN or infinity: the test scans every scenario's every horizon
  for a non-finite number.

## 9. What the frozen fixtures produce

| scenario | verdict | score | hard | drivers | margin (min) | confidence |
| --- | --- | --- | --- | --- | --- | --- |
| `S01_clear_day` | GREEN | 0 | -- | -- | +354.859 | 0.817143 |
| `S04_sunset_margin_pace_drop` | RED | 65 | R-B | R-D, R-E, R-F | -55.015 | 0.746286 |
| `S11_stale_gps_missing_data` | GREEN | 0 | -- | -- | +74.382 | 0.366326 |

S04's `hard` (R-B) and its two soft drivers (R-D, R-E) are what its own scenario file
declares; the core additionally reports R-B itself as a driver and the compound
escalation R-F that the two soft rules earn. S11's 0.366326 is the point of that
fixture: a stale fix, a missing pace drop and no network are disclosed loudly
(`pace_drop_percent is derived, not observed ... 8.824 %`,
`weather snapshot is 250 minutes old, beyond the 180-minute budget`) without being
invented into a risk. S11 also sits 42.09 m off route -- below R-A's 50 m threshold,
so nothing fires, and the off-route distance is still reported in the margins.

Every rule boundary is pinned at the threshold itself: **at** the threshold the rule
does *not* fire, one thousandth past it it does (R-A 50 m, R-B 30 min, R-C 45 km/h,
R-D 15 %, R-E 20 %).

## 10. Three real bugs this phase found in its own core

Recorded because each was invisible to the test suite that existed and would have been
invisible in a demo:

1. **hard rules were inflating the soft score.** R-A alone reached ORANGE *through the
   band* (40 points) as well as through its severity floor, contradicting
   `docs/contracts.md` and the engine's own documented pipeline. Fixed by exempting
   `trigger_type: hard` rules from `aggregate_soft_score`.
2. **severity was compared as strings.** `"HIGH" < "MEDIUM"` alphabetically, so R-F's
   `min_severity: MEDIUM` filter silently excluded HIGH and CRITICAL partners and the
   compound rule could never fire. Fixed with `Severity.rank` (LOW 0 .. CRITICAL 3),
   mirroring `RiskLevel.rank`.
3. **`_compound_detail` was called but never written**, so once R-F *could* fire,
   `assess_risk` raised `AttributeError`. Found only because bug 2 was fixed first --
   which is the argument for fixing the ordering before the crash.

A fourth, cosmetic: driver details formatted values with four significant digits, so a
projected battery of 19.999 % printed as "20" and the detail read
`projected_battery_percent=20 lt 20` -- a sentence no reader could verify. `_format`
now uses six.

## 11. Known limitations (deliberate, not defects)

* **A fix far beyond the route's ends snaps to the nearer end.** Nearest-point
  snapping is unconditional: a fix 2 667.7 m past `scn_route_sample.gpx`'s last
  trackpoint snaps to that trackpoint, so the core reports `route_progress 1.0`,
  `remaining 0.0 km` and an off-route distance of 2 667.7 m. The verdict stays
  honest -- R-A fires and the risk level is ORANGE with score 0 -- but "route
  finished" would be a misreading of the progress number, and the sunset margin
  computed from that snapped position (here 490 min) describes a hiker standing at
  the end of a route they are not on. Deciding "this hiker is no longer on this
  route" needs a persistence model the MVP does not have, and inventing a distance
  cut-off would be a fabricated threshold. Verified in this run rather than assumed:
  `get_route_context("scn_route_sample", (46.15683, 7.82043)) -> progress 1.0`.
* **Battery drain is one calibrated rate for every device.** `DeviceState` cannot carry
  a per-device rate, so R-E projects at 4 %/h from `calibration_v1.yaml`; a caller who
  knows better builds the engine with a different calibration.
* **Weather is no longer replay-only.** Phase 1 read `replay/weather/scn_weather_snapshot.json`
  through a stub tool; Phase 2 gave `get_weather_for_route_time` a real body with two
  acquisition modes (spec 17), replay and live Open-Meteo. What did not change is the
  part that matters here: the snapshot is still stored in **open-meteo's response
  shape** -- parallel `hourly` arrays, `utc_offset_seconds` for its naive local row
  instants, `hourly_units` naming each variable's unit -- and live responses are parsed
  by that same parser, `trailguard/weather/normalization.py`. Three conversions are
  load-bearing and live in the parser, not in the data: precipitation arrives as
  **percent** and becomes a probability (so `8` percent does not read as an impossible
  probability that trips R-C's 0.7 threshold), the row instants are **naive local
  wall-clock times** made aware using the declared offset (so a 12:00 row means 10:00Z,
  not 12:00Z), and a declared unit the parser does not know is a loud error rather than
  a guess. The asset declares **no provider confidence**, so its confidence term is
  charged `confidence_unknown_value` (0.5) and flagged `confidence_is_assumed`, never
  assumed perfect. Nearest row wins, nothing is extrapolated past the series, and stale
  snapshots are used *as given* with their age reported alongside (180-minute demo
  freshness budget). `tests/test_replay_weather.py` still pins all of it, and
  `tests/test_weather_providers.py` pins the same behaviour on the live path. The
  acquisition modes, the grid rule and the failure contract are documented in
  `docs/route_aware_weather.md`.

  The asset was rewritten once during this phase into a TrailGuard-native shape, which
  would have changed the frozen data silently (dropped the 06:00-09:00 rows, altered
  every temperature, added a confidence the file never declared). It was reverted to
  its Phase 0 bytes and the normalization moved into the parser, where a reviewer can
  diff it. See the final report.
* **The projection is trend-extrapolation, not a forecast.** It assumes the hiker keeps
  the currently observable trend along the planned route, and says so through
  `projection_confidence`; it does not model terrain, fatigue dynamics or rest stops.
* **One observation, not persistence.** R-A's `persistence_observations` threshold
  cannot be checked by a stateless single-horizon call, so the driver detail states
  that persistence is not asserted rather than silently assuming it.

## 12. Where to change things

| to change | edit | never |
| --- | --- | --- |
| a rule threshold | `trailguard/rules/rules_v1.yaml` | never the engine |
| which feature a threshold compares, or its direction | `trailguard/rules/calibration_v1.yaml` | never a naming convention |
| score bands, severity weights, floors, movement weights, battery drain, confidence weights | `calibration_v1.yaml` | never a constant in code |
| the 30/60/120 loop, freshness policy, missing-data protocol | `SKILL.md` | never a tool |
