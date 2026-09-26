# Route-Aware Weather (Phase 2)

Source of truth for this page: `trailguard/weather/` plus
`trailguard/tools/weather_route_time.py`. Everything here answers one question: **how
does TrailGuard obtain weather for a future route position at a future arrival time,
and what does it do when it cannot?**

Phase 1 could read a frozen snapshot. That was enough to build and pin the
deterministic core, but it was not route-aware *weather*: the snapshot described one
point and one day, and rule R-C judged a gust at the projected position and projected
arrival time using values that were never asked for that position or that instant.
Phase 2 closes that gap without letting a plausible-looking number through.

## 1. The chain

```text
Current Route State        get_route_context(route_id, position, timestamp)
        |
        v
Future Route Projection    project_route_state(route_context, movement, H)
        |                         -> projected_position
        |                         -> projected_arrival_time
        v
Route-aware Weather        get_weather_for_route_time(lat, lon, target_time)
        |                         -> WeatherForRouteTime  (+ WeatherReading provenance)
        v
Deterministic Safety Core  assess_risk(...)  -> Projected Risk State for H
```

The invariant that makes this a *feature* rather than plumbing:

> **one horizon -> one route projection -> one weather lookup -> one risk assessment.**

Every word of that is load-bearing. "One horizon" is why there is no
`forecast_all_horizons()` and why `get_weather_for_route_time` accepts a single
`(lat, lon, target_time)` triple. "One route projection" is why the weather tool never
re-derives a position: it is handed `projection.projected_position`. "One weather
lookup" is why a failure raises instead of falling back. "One risk assessment" is why
the loop lives in `SKILL.md`, not in a tool.

## 2. Position x time, not position or time

Rule R-C asks: *is there a wind gust at the projected position, at the projected
arrival instant, above the demo threshold?* A hiking app answers "is it windy where
you are standing, now". Those are different questions, and the second one cannot
answer the first by relabelling.

So the tool's arguments are the projection's own outputs, and
`weather_for_projection()` exists so that is written once:

```python
projection = project_route_state(route_context=..., movement=..., horizon_min=120,
                                 analysis_time=analysis_time)
weather = weather_for_projection(projection, policy=policy)   # one call, one horizon
```

The projection supplies **both** halves. The position comes from
`projection.projected_position` -- never re-guessed from the GPX, so a rounding
difference in the geometry cannot move the hiker between the projection and the
weather. The instant comes from `projection.projected_arrival_time` -- the instant
the projection *refers to*, not a predicted finish, which is the instant the weather
and the safety margins must be evaluated at.

The same position asked about at +30 and at +120 is two different weather answers,
and `tests/test_weather_providers.py::test_each_horizon_reads_its_own_weather_rather_than_a_copy`
asserts exactly that (four horizons, four arrival instants, four rows, more than one
gust value).

## 3. The two acquisition modes (spec 17)

| | Replay Mode | Live Weather Mode |
| --- | --- | --- |
| Provider | `ReplayWeatherProvider` | `OpenMeteoWeatherProvider` |
| Source | one frozen JSON file named by `weather_policy.snapshot_ref` | `https://api.open-meteo.com/v1/forecast` |
| Network | none | HTTPS, no key, no token |
| Chosen by | `weather_policy.mode = "replay"` | `weather_policy.mode = "live"` **or** no policy at all |
| Age | measured at the instant asked about (Phase 1 semantics) | the retrieval instant, disclosed as such |
| Use case | the benchmark, the frozen scenarios, the demo | a real hiker asking a real question |

**Replay is a choice, including which fixture.** A policy that selects
`mode = "replay"` without naming a snapshot is refused rather than defaulted, and a
`snapshot_ref` that resolves to nothing is refused naming every candidate path tried.
A benchmark must be able to say *which* fixture it ran.

**Live is the default** when nothing is specified, because the real capability is the
real one and a caller that asked for nothing should get the real answer. That also
means: no code path may quietly turn a live request into a replay read. The guard is
behavioural -- `tests/test_weather_providers.py::test_a_failed_live_call_never_constructs_a_replay_provider`
makes `ReplayWeatherProvider.__init__` explode and then fails a live call on purpose;
if any path had substituted the fixture, the test would report the substitution
instead of the provider error.

The endpoint and every parameter name were read off the live API on 2026-09-26 rather
than recalled: `https://api.open-meteo.com/v1/forecast` with `latitude`, `longitude`,
`hourly` (comma-separated), `timezone=UTC`, `start_date` / `end_date`. A date outside
the API's own forecast range comes back as HTTP 400 with a `reason` field, which is
surfaced as `WeatherTargetUnavailable` and never as a silent substitution.

## 4. Normalisation: one layer, two paths (spec 8)

```text
External Provider Response -> Normalization -> WeatherForRouteTime -> Safety Core
```

There is exactly **one** normaliser, `trailguard/weather/normalization.py`, and both
paths go through it. That is not tidiness: the conversions below are precisely where a
plausible-looking wrong answer lives, and a second copy would let a bug be fixed on
the live path and left behind on the replay path -- which is the path the frozen
scenarios and the benchmark actually run.

1. **`precipitation_probability` arrives in percent.** The API's own `hourly_units`
   says `%`; the schema wants a probability in 0..1. The `/100` decides whether R-C
   fires: read raw, a 50-percent row looks like an impossible probability and trips
   the 0.7 threshold. The unit is *read from the document*, so a snapshot declaring
   `mm` or no unit at all is a loud error rather than a silent rescale.
2. **Row instants are naive local wall-clock times.** `2026-05-18T12:00` carries no
   offset; `utc_offset_seconds` carries it. Verified against the live API: a
   `timezone=Europe/Zurich` request returns `utc_offset_seconds: 7200` with rows still
   written `2026-09-26T00:00`. A naive row compared against an aware projected arrival
   is a `TypeError` at best and a wrong nearest row at worst, so every row is made
   aware at parse time. Requesting `timezone=UTC` keeps the offset at zero and the
   conversion explicit.
3. **Variable names are Open-Meteo's.** `temperature_2m` -> `temperature_c`,
   `wind_speed_10m` -> `wind_speed_kmh`, `wind_gusts_10m` -> `wind_gust_kmh`.
   Temperatures are already Celsius and winds already km/h, so those are renames --
   but each one is unit-checked anyway, because a document declaring Fahrenheit would
   otherwise land in `temperature_c` silently.
4. **Ragged parallel arrays are refused.** Every row is built from the same index
   across all arrays; truncating to the shortest would pair values with the wrong
   instants and look complete.

The live provider additionally names its answer `open-meteo@<row time>` so the row the
values came from is visible in `source`, not just in a log.

## 5. The discrete grid rule (spec 17)

A query instant resolves to the **nearest** row of the discrete hourly grid, and a tie
resolves to the **earlier** of the two rows. That is the whole rule: no interpolation,
no extrapolation, no runtime randomness, and the same function
(`normalization.nearest_row`) implements it for both paths.

The refusal cases matter as much as the selection:

* a request **outside** the series is refused -- the grid is not extrapolated in
  either direction;
* a **naive** target instant is refused before any lookup runs;
* the distance between the requested instant and the row that answered is reported as
  `reach_minutes`, so a reader can see how far the answer had to reach.

Interpolation is tempting for a continuous variable like temperature. It is refused
anyway, because the same code path would have to interpolate `weather_code` -- a WMO
*code*, a label with no arithmetic meaning, and an interpolated code would be a
forecast nobody wrote. The rule is documented, deterministic and pinned by tests in
both directions (`test_the_nearest_row_wins_and_a_tie_goes_to_the_earlier_row`,
`test_a_discrete_code_is_never_interpolated`).

## 6. Position coverage, disclosed rather than guessed

A replay snapshot is a **single-point** series: it declares `latitude` / `longitude`
(Open-Meteo's own fields, naming the point its rows describe) and has no second
spatial dimension. So a replay lookup cannot vary spatially, and the provider refuses
to pretend otherwise:

* when the snapshot declares a point, the geodesic distance from the query position is
  computed and written into the reading's notes ("the projected position is 1 592.6 m
  from it and within the declared 7.0 km tolerance");
* when the snapshot also declares `location_tolerance_km`, a query beyond that
  tolerance raises `WeatherTargetUnavailable` rather than answering about a place the
  snapshot does not describe;
* when it declares no tolerance, the distance is disclosed and nothing is refused,
  because inventing a coverage radius would be inventing a threshold.

The live API behaves the same way for the same reason and says so itself: a real
request for the projected +120 position comes back answered for the API's *grid
point*, and the provider discloses both coordinates rather than letting the projected
position be mistaken for the grid point.

## 7. Freshness and confidence (spec 9, 10.4)

**Freshness.** `freshness_minutes` measures how old the weather is *at the instant it
is used*, and that instant differs by mode -- disclosed, not hidden:

* a **frozen snapshot** was generated once and is consulted later, so its age is
  measured at the instant the caller asked about (the projected arrival). A +120
  horizon reports a two-hour-old snapshot, which is exactly the age term the
  confidence model needs and the Skill's freshness protocol judges against
  `weather_policy.max_freshness_min`;
* a **live forecast** is born at the moment it is requested -- the API returns no
  model-run instant -- so its age is measured at the retrieval instant and is zero by
  construction. The notes say so, rather than letting "zero" be read as "the upstream
  model just ran".

Nothing re-labels a stale reading as fresh. `freshness_status(max_freshness_min)`
remains a pure comparison, and a snapshot older than the caller's budget is classified
`STALE` whether or not that is convenient.

**Confidence.** Open-Meteo declares no provider confidence, so TrailGuard does not
invent one. A reading carries the calibration's `confidence_unknown_value` (0.5) and
`confidence_is_assumed=True`: the number is *charged*, not earned. `WeatherForRouteTime`
carries that flag as its eleventh field so a 0.5 a provider asserted is never
indistinguishable from a 0.5 nobody did. There is no "Open-Meteo confidence = 0.9"
constant anywhere in this repository, and a test pins the value to the calibration's
own unknown figure.

The field is additive with a default, so no frozen contract changed shape and the
schema's `extra="forbid"` still rejects anything else.

## 8. Failure handling (spec 11, 12)

Every failure raises a subclass of `WeatherProviderError`, and every message says what
was attempted. There is no code path that answers a failure.

| Failure | Class | Why |
| --- | --- | --- |
| connect/read timeout, DNS, connection refused | `WeatherProviderUnavailable` | the provider could not be reached; a timeout is not a licence to guess |
| HTTP 5xx (after at most one retry) | `WeatherProviderResponse` | it answered, and the answer is not usable |
| HTTP 400/404 -- e.g. a date outside the forecast range | `WeatherTargetUnavailable` | the provider cannot describe that instant at all; deterministic, so it is not retried |
| non-JSON body | `WeatherProviderResponse` | an HTML error page is not weather |
| missing hazard variable, ragged arrays, unknown unit, bad instant | `WeatherProviderResponse` | the response arrived but cannot be normalised |
| replay naming no or an unresolvable snapshot | `WeatherProviderNotConfigured` | the caller asked for something this build cannot supply |
| httpx not installed | `WeatherProviderNotConfigured` | names the `weather` extra that provides it |

Timeouts are finite by construction and validated at construction time (a zero timeout
or a third attempt is a configuration error, not a runtime surprise). Retries cover
transport failures and 5xx only -- at most one retry, never an unbounded loop -- and
never a 4xx, because the API saying no deterministically means retrying only doubles
the latency of a certain failure.

## 9. The future-weather demonstration (spec 15)

Run it:

```bash
.venv/bin/python -m trailguard.weather_smoke
```

Replay only, no network, no LLM, no key. It walks the frozen clear-day scenario
through all four horizons, calling the real `get_weather_for_route_time` once per
horizon, and prints for each one: Horizon, Projected Position, Projected Arrival Time,
Weather Source, Weather Timestamp, Weather Values, Freshness, Confidence, Risk Level
and Risk Driver. A representative run:

| Horizon | Projected position | Projected arrival | Row read | Gust | Verdict |
| --- | --- | --- | --- | --- | --- |
| NOW | 46.08102, 7.77210 | 10:30Z | 10:00Z | 14.0 km/h | GREEN, score 0, no rule fired |
| +30min | 46.09448, 7.77900 | 11:00Z | 11:00Z | 16.0 km/h | GREEN, score 0, no rule fired |
| +60min | 46.10791, 7.78593 | 11:30Z | 11:00Z | 16.0 km/h | GREEN, score 0, no rule fired |
| +120min | 46.13003, 7.81087 | 12:30Z | 12:00Z | **58.0 km/h** | **YELLOW, score 40, R-C triggered** |

Only the horizon changed: same GPX, same device feed, same rule pack, same fixture.
The verdict changed because the weather was asked about at a different position and a
different instant, and 58 km/h crosses R-C's 45 km/h demo threshold while 14 and 16 do
not. The four-blocks-of-structure are the demonstration; the equivalent assertions
live in `test_now_is_safe_and_the_projected_position_two_hours_out_is_hazardous` so a
reviewer can re-run them rather than take the table's word for it.

The demo fixture is `replay/weather/scn_weather_future_hazard.json`, a **new file**:
`spec 25` forbids rewriting the Phase 0 asset, and `tests/test_weather_providers.py`
pins the Phase 0 snapshot's sha256 so an edit fails the suite. The new fixture is
marked `"synthetic": true` with a note stating in capitals that it is not Open-Meteo
historical data and not a real forecast -- `spec 26`'s requirement that invented data
never wear a real provider's clothes. Its values were chosen against the measured
projected positions (the +120 position really is 6 223.8 m from the fixture's declared
point, which is why it declares a 7 km tolerance) rather than against invented
coordinates.

The four-horizon walk in the smoke module is a **test/demonstration harness**, not a
production tool, and it says so at the top of its output: each iteration makes one
single-horizon call, and the loop itself belongs to `SKILL.md`.

## 10. Live verification, and what it does not prove

```bash
.venv/bin/python scripts/test_live_weather.py            # the projected +120 position, now
.venv/bin/python scripts/test_live_weather.py --at 2026-09-26T12:30:00Z
```

This is the only path in the repository that opens a socket, and pytest never collects
it: `tests/conftest.py` lists it in `collect_ignore` and `testpaths` is `tests` only, so
neither a normal run nor `pytest scripts/` can reach it. When the endpoint cannot be
reached it prints `LIVE WEATHER NETWORK UNAVAILABLE`, prints the reason and exits
non-zero -- it never fabricates success.

A real run on 2026-09-26 did answer: the request was built for the projected +120
position, the response was normalised by the same layer the replay path uses, and the
disclosure that the API answered for **its own grid point** (46.12, 7.7999997) rather
than the projected position came from the response itself. That is the honest shape of
live weather at this resolution: a point a few hundred metres away, said out loud.

Phase 2's verdict does not depend on that run succeeding. What Phase 2 is accepted on
is the provider implementation, the normalisation, the mocked integration and the
deterministic replay behaviour -- not on competition-day network quality (`spec 28`).

## 11. Known limitations (deliberate, not defects)

* **Hourly granularity is all the forecast API offers.** A projected arrival at 12:37
  is answered from the 12:00 row, 37 minutes away, and `reach_minutes` says so. A
  finer grid or an interpolation is a future decision, not an oversight.
* **A live query covers one date.** The request asks for `start_date = end_date = the
  target's date`; a horizon that crosses midnight UTC would have to be re-asked. The
  replay path is unaffected, because a snapshot carries whatever rows it carries.
* **A replay snapshot is single-point.** It cannot vary spatially, so it discloses the
  distance instead of simulating a gradient. Only a declared tolerance turns that
  disclosure into a refusal.
* **`weather_code` is passed through, not interpreted.** The core consumes the numeric
  fields; a WMO code is a label, and turning it into a hazard would be a seventh rule
  the spec does not authorise.
* **Confidence is charged, never provider-asserted.** A live reading's confidence is
  therefore always the unknown value. That is a statement about what Open-Meteo
  publishes, not a defect in the provider.
* **No caching, no retry budget beyond one, no circuit breaker.** A horizon loop makes
  four requests in live mode and each is on the critical path. Acceptable for a demo
  and for the benchmark (which is replay), and the timeouts keep the worst case
  bounded.
* **The local Qwen model is not connected to any of this** (`spec 22`). Phase 2's code
  and the DGX Spark service at `http://127.0.0.1:8000/v1` are entirely separate
  subsystems.

## 12. Where to change things

| To change | Edit |
| --- | --- |
| which variables are requested, or the endpoint | `trailguard/weather/normalization.py` (`WEATHER_VARIABLES`, `OPEN_METEO_ENDPOINT`) and `trailguard/weather/open_meteo.py` |
| the grid-matching rule | `normalization.nearest_row` -- and its tests, in both directions |
| the percent/offset conversions | `normalization.parse_open_meteo_payload` and `_FIELD_MAP` |
| timeouts or the retry budget | `open_meteo.py` module constants; both are validated at construction |
| what an undisclosed confidence costs | `rules/calibration_v1.yaml` -> `confidence_unknown_value` |
| the R-C gust threshold | `rules/rules_v1.yaml` (Phase 0 frozen; a change is a spec amendment) |
| the demo fixture | add a **new** file; `scn_weather_snapshot.json` is hash-pinned |
