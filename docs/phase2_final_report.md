# TrailGuard Phase 2 Final Report

**Phase 2 — Route-aware Weather & Future Projection.**
Every claim below was produced by a command run in this repository during this phase;
nothing is carried over from a workflow summary. Where a run could not be performed, it
says so.

Reproduce the verification in this order:

```bash
.venv/bin/python -m pytest -v                          # 544 passed
.venv/bin/python -m trailguard.weather_smoke           # replay, offline
.venv/bin/python scripts/test_live_weather.py          # live Open-Meteo, optional
git status --porcelain ; git diff --stat ; git diff
sha256sum SKILL.md trailguard/rules/rules_v1.yaml replay/weather/scn_weather_snapshot.json
```

---

## 1. Repository State

```
branch   main (no remote configured)
HEAD     5a93fd7  feat: complete TrailGuard Phase 1 deterministic core
tags     phase0-freeze (3c41ae3), phase1-freeze (5a93fd7)
remotes  none -- nothing has been pushed, and nothing will be
```

**A disclosure that has to come first, because it is the one thing wrong with this
repository's history.** §1 of the brief instructed me to freeze Phase 1 with
`git add .` on a clean tree. I ran that command on a working tree that **already
contained the Phase 2 weather scaffolding** — `trailguard/weather/{base,normalization,
replay,open_meteo}.py`, the real body of `get_weather_for_route_time`, the additive
`WeatherForRouteTime.confidence_is_assumed` field, and the synthetic demo fixture — all
written in the previous session before the Phase 1 freeze was requested. `git add .`
staged them, so **`phase1-freeze` (5a93fd7) is not a Phase-1-only commit**: it carries
that Phase 2 code inside it.

Consequences, stated plainly rather than minimised:

* `git diff HEAD -- trailguard/tools/weather_route_time.py trailguard/schemas/weather.py trailguard/weather/` is **empty**. The Phase 2 implementation is identical to what the "Phase 1 freeze" tag already contains, so "what Phase 2 changed in the implementation" cannot be read off that tag.
* What this phase actually added on top of `5a93fd7` is: `tests/test_weather_providers.py` (57 tests), `trailguard/weather_smoke.py`, `scripts/test_live_weather.py`, `docs/route_aware_weather.md`, four documentation updates, the `weather` dependency extra, and one production-code change (`ReplayWeatherProvider`'s constructor, below).
* The Phase 1 freeze is therefore contaminated as a *labelling* matter. It is **not** contaminated as a *data* matter: see §11 — `SKILL.md`, `rules_v1.yaml`, `calibration_v1.yaml` and `replay/weather/scn_weather_snapshot.json` are all byte-identical to their Phase 0 versions, verified both by `git diff` against `3c41ae3` and by sha256.
* The fix available now is honest labelling rather than history rewriting: this report records it, and the Phase 2 commit below contains the rest. I did not rewrite or re-tag the Phase 1 commit, because rewriting a frozen tag would destroy the evidence of what happened.

**Working tree at the time of writing (before this phase's commit):**

```
 M README.md                       M docs/architecture.md      M docs/contracts.md
 M docs/deterministic_core.md      M pyproject.toml            M requirements.txt
 M tests/conftest.py               M tests/test_packaging.py   M trailguard/weather/replay.py
?? docs/route_aware_weather.md     ?? scripts/                 ?? tests/test_weather_providers.py
?? trailguard/weather_smoke.py
```

No sensitive file appears anywhere in that list: no `.env`, no key, no token, no
`~/.claude/settings.json`, no credential. Nothing of the kind was read, printed or
committed at any point in this phase.

**The one production-code change since the freeze tag** is in
[replay.py](../trailguard/weather/replay.py): the constructor no longer falls back to
the shipped scenario snapshot when `snapshot_ref` is `None`. It now refuses, and names
the candidate paths it tried. Reason: §17 says replay answers from the fixture the
caller named, and a benchmark must be able to say *which* fixture it ran; a silent
default lets a policy that forgot `snapshot_ref` read a file nobody chose. Test:
`test_a_replay_provider_that_names_no_snapshot_refuses_rather_than_defaulting`.

---

## 2. Weather Provider Architecture

```text
                        ┌──────────────────────────────┐
  weather_policy ──────▶│  provider_for_policy()       │  mode="replay" -> ReplayWeatherProvider
  provider=<injected> ─▶│  (the only place a mode is   │  mode="live"   -> OpenMeteoWeatherProvider
                        │   turned into a provider)    │  neither        -> live
                        └──────────────┬───────────────┘
                                       │  both implement WeatherProvider
                                       ▼
                          get_weather_for_route_time(lat, lon, target_time)
                                       │  one (position, instant) pair
                                       ▼
                    normalization.parse_open_meteo_payload()   ◄── THE ONE normaliser
                                       │
                                       ▼
                          WeatherForRouteTime  (+ WeatherReading provenance)
                                       │
                                       ▼
                       Deterministic Safety Core  (cannot tell which provider ran)
```

Files, and what each one owns:

| File | Responsibility |
| --- | --- |
| `trailguard/weather/base.py` | the `WeatherProvider` ABC, the `WeatherReading` provenance wrapper, `reading_provenance()`, and the `WeatherProviderError` hierarchy (`WeatherProviderUnavailable`, `WeatherProviderResponse`, `WeatherTargetUnavailable`, `WeatherProviderNotConfigured`) |
| `trailguard/weather/normalization.py` | **the one normaliser**: `parse_open_meteo_payload`, `nearest_row`, `WEATHER_VARIABLES`, `OPEN_METEO_ENDPOINT`, `build_params`. Both providers end here. |
| `trailguard/weather/replay.py` | `ReplayWeatherProvider`: reads a named frozen JSON file, no socket |
| `trailguard/weather/open_meteo.py` | `OpenMeteoWeatherProvider`: HTTPS to Open-Meteo, finite timeouts, one retry, no key |
| `trailguard/weather/__init__.py` | lazy re-exports (PEP 562 `__getattr__`) to break the `weather ↔ replay.weather` import cycle, plus `provider_for_policy()` |

The dependency inversion matters for §11's guarantee: the tool depends on the ABC and
on `normalization`, never on a concrete provider, so a failure can be injected and a
substitution can be detected (see Q5's test in §12).

**The invariant, implemented rather than documented:** one horizon → one
`project_route_state` → one `get_weather_for_route_time` → one `assess_risk`. The tool
takes a single `(lat, lon, target_time)`; there is no list-taking variant, no
`horizon_min` argument, and no loop anywhere in `trailguard/`. The loop lives in
`SKILL.md`, which was not modified.

---

## 3. Replay Weather

`ReplayWeatherProvider` reads exactly one frozen JSON file. It is a choice, including
*which* file:

* `mode = "replay"` **without** `snapshot_ref` → `WeatherProviderNotConfigured`. It does not default to the shipped snapshot.
* `snapshot_ref` that resolves to nothing → refused, and the message names every candidate path tried.
* An injected parsed `snapshot=` is accepted for callers (tests, out-of-repo fixtures) that built it themselves; the reading then says so instead of implying a file backs it.

Determinism is structural: no `random`, no clock read, no network, no environment
variable. The same snapshot and the same query produce the same row and the same
reading, and `tests/test_replay_weather.py` (Phase 1) plus the new
`tests/test_weather_providers.py` both pin it.

**The grid rule** (`normalization.nearest_row`, one implementation for both paths):
nearest row wins; a tie resolves to the **earlier** row; a request outside the series is
refused in both directions; a naive target instant is refused before any lookup; the
distance from the requested instant to the row that answered is reported as
`reach_minutes`. No interpolation of any variable, and specifically never of
`weather_code` — a WMO code is a label, and an interpolated code is a forecast nobody
wrote.

**Position coverage is disclosed, not simulated.** A replay snapshot is a *single-point*
series: it declares `latitude`/`longitude` (Open-Meteo's own field names) naming the
point its rows describe, and has no second spatial dimension. So the provider computes
the geodesic distance from the query position to that declared point and writes it into
the reading's notes; when the snapshot also declares `location_tolerance_km`, a query
beyond the tolerance raises `WeatherTargetUnavailable` rather than answering about a
place the snapshot does not describe; when it declares no tolerance, the distance is
disclosed and nothing is refused, because inventing a coverage radius would be inventing
a threshold.

**Freshness in replay keeps Phase 1's semantics** (spec 10.4): the age is measured at the
instant the caller asked about — the projected arrival — so a +120 horizon reports a
150-minute-old snapshot, which is the age term the confidence model and the Skill's
freshness protocol judge against `weather_policy.max_freshness_min`. A snapshot past the
caller's budget is `STALE`; nothing re-labels it.

---

## 4. Live Open-Meteo Provider

Endpoint and parameters were **read off the live API on 2026-09-26**, not recalled:
`https://api.open-meteo.com/v1/forecast` with `latitude`, `longitude`,
`hourly` (comma-separated), `timezone=UTC`, `start_date`/`end_date`. Response shape:
parallel `hourly` arrays, `hourly_units`, `utc_offset_seconds`, naive local row
instants. No key, no token, no account — `test_build_params_never_sends_a_credential`
asserts the parameter set contains no credential-shaped key.

* `timezone=UTC` is requested explicitly, so the offset is zero and every conversion is visible rather than implied.
* `start_date = end_date = <target's UTC date>`; a date outside the API's own forecast range comes back HTTP 400 with a `reason`, which surfaces as `WeatherTargetUnavailable` — never as a substitution.
* Timeouts are `DEFAULT_CONNECT_TIMEOUT_S = 5.0` and `DEFAULT_READ_TIMEOUT_S = 10.0`, and `DEFAULT_MAX_ATTEMPTS = 2` (one initial call, at most one retry). All three are validated at construction: a zero timeout or a third attempt is a configuration error, not a runtime surprise (`test_an_infinite_timeout_is_refused_as_configuration`, `test_a_third_attempt_is_refused_as_configuration`).
* Retries cover transport failures and 5xx only, never 4xx — the API saying no deterministically means a retry doubles the latency of a certain failure.
* `httpx` is imported lazily inside the provider, so replay Mode and the whole test suite run on the base install alone; absent `httpx` is `WeatherProviderNotConfigured` naming the `weather` extra, not an `ImportError` from a caller's stack.

**Live run, actually performed (2026-09-26, exit 0):**

```
asked about   : lat 46.130031 lon 7.810875 at 2026-09-26T14:24:54+00:00   <- the S01 +120 projection
answered      : yes
weather       : gust 15.1 km/h, wind 12.6 km/h, temp 14.8 C, precip 0.0, code 0
row matched   : 2026-09-26T14:00:00+00:00 (24.9 min from the requested instant)
freshness     : 0.0 min  (the API returns no model-run instant; this is the retrieval age)
confidence    : 0.5 (charged: the provider declares none)
disclosure    : open-meteo answered for its grid point (46.12, 7.7999997), not the projected position
```

and at a pinned instant (`--at 2026-09-26T12:30:00Z`): row `12:00:00Z`, 30.0 min reach,
gust 13.7 km/h, temp 15.0 °C, code 3. The grid-point disclosure came from the response
itself — the API answered for its own grid point, and the provider says so rather than
letting the projected position be mistaken for it.

The unavailable path was exercised too: `scripts/test_live_weather.py` reports
`LIVE WEATHER NETWORK UNAVAILABLE` with the underlying reason and a non-zero exit. It
never prints a success it did not get.

---

## 5. Route-aware Position × Time Semantics

This is the phase's technical claim, and it is worth stating precisely.

Rule R-C asks: *is there a wind gust at the projected position, at the projected arrival
instant, above the demo threshold?* A hiking app answers "is it windy where you are
standing, now". Those are different questions, and the second cannot answer the first by
relabelling. So:

* **the position comes from `RouteProjection.projected_position`** — never re-guessed from the GPX, so a rounding difference in the geometry cannot move the hiker between the projection and the weather;
* **the instant comes from `RouteProjection.projected_arrival_time`** — the instant the projection *refers to*, not a predicted finish; that is the instant the weather and the safety margins are evaluated at;
* `weather_for_projection(projection, ...)` exists so both halves are written once. It takes a single projection and makes exactly one provider call; it cannot loop;
* internal time is **UTC-aware everywhere**. Naive instants are refused at the parser and at the live provider, before any lookup or request;
* the tool's declared contract is unchanged — `lat`, `lon`, `target_time` — with `policy`, `provider` and `analysis_time` as keyword-only extras the model-facing schema does not widen.

One consequence worth naming: the same position asked about at +30 and at +120 is two
different weather answers, and `test_each_horizon_reads_its_own_weather_rather_than_a_copy`
asserts four horizons produce four arrival instants, four matched rows and more than one
gust value.

The projection tool still refuses a horizon outside 0/30/60/120 and still refuses to run
without an explicit `analysis_time`; no wall clock is read anywhere in the projection,
the weather tool, or the safety core.

---

## 6. Normalization

One layer, `trailguard/weather/normalization.py`, used by both paths. This is not
tidiness: these conversions are exactly where a plausible-looking wrong answer lives,
and a second copy would let a bug be fixed on the live path and left behind on the replay
path — which is the path the frozen scenarios and the benchmark actually run.

| # | Conversion | Why it decides a verdict |
| --- | --- | --- |
| 1 | `precipitation_probability` arrives as **percent** and becomes a probability in 0..1 (`hourly_units` says `%`) | read raw, a 50-percent row is an impossible probability that trips R-C's 0.7 threshold; the unit is *read from the document*, so an unknown unit is a loud error, not a silent rescale |
| 2 | row instants are **naive local wall-clock** and are made aware with the declared `utc_offset_seconds` | a naive row compared with an aware projected arrival is a `TypeError` at best and a wrong nearest row at worst; verified against the live API — a `timezone=Europe/Zurich` request returns `utc_offset_seconds: 7200` with rows still written `2026-09-26T00:00` |
| 3 | Open-Meteo names map to contract names (`temperature_2m`→`temperature_c`, `wind_speed_10m`→`wind_speed_kmh`, `wind_gusts_10m`→`wind_gust_kmh`) | temperatures are already °C and winds already km/h, so these are renames — but each is unit-checked anyway, or a document declaring Fahrenheit would land in `temperature_c` silently |
| 4 | every row is built from the **same index** across all parallel arrays | truncating to the shortest would pair values with the wrong instants and look complete; ragged arrays raise instead |
| 5 | the live provider names its answer `open-meteo@<row time>` | the row the values came from is visible in `source`, not only in a log |

The Safety Core's contract was **not** widened for any of this: it still consumes a
`WeatherForRouteTime` and still cannot tell which provider produced it (spec 8). The
disclosures that have no field of their own travel on `WeatherReading`
(`row_time`, `reach_minutes`, `confidence_is_assumed`, `notes`), and
`reading_provenance()` flattens one reading to the trace's keys so the smoke output, the
evidence trace and a test cannot disagree about what weather provenance is.

---

## 7. Freshness & Confidence

**Freshness is measured at the instant the weather is used, and that instant differs by
mode — disclosed, not hidden:**

* a **frozen snapshot** was generated once and is consulted later, so its age is measured at the instant asked about (the projected arrival): +30 → 60 min, +60 → 90 min, +120 → 150 min in the demo run;
* a **live forecast** is born when requested — the API returns no model-run instant — so its age is measured at the retrieval instant and is 0.0 by construction, with a note saying exactly that, so "zero" is not read as "the upstream model just ran";
* when a document *does* declare `generated_at`, that declared instant wins over the retrieval instant (`test_the_generated_instant_a_document_declares_wins_over_the_retrieval_instant`);
* `freshness_status(max_freshness_min)` remains a pure comparison. A snapshot past the caller's budget is `STALE` whether or not that is convenient.

**Confidence is charged, never invented.** Open-Meteo declares no provider confidence,
and there is no `0.9` constant anywhere in `trailguard/weather/` or in the weather tool
(verified by grep: zero hits). A reading carries the calibration's
`confidence_unknown_value` — **0.5** — with `confidence_is_assumed=True`: a number
charged in the absence of an asserted one, never passed off as an official provider
score. `WeatherForRouteTime` carries that flag as an **additive** field with a default,
so no frozen contract changed shape and `extra="forbid"` still rejects anything else.
When a provider *does* declare a figure, it is reported as the provider's own and the
flag is `False` (`test_a_declared_confidence_is_reported_as_the_providers_own`).

The Safety Core's confidence model was **not** touched. A live reading's confidence is
therefore always the unknown value — a statement about what Open-Meteo publishes, not a
defect in the provider.

---

## 8. Failure Handling

Every failure raises a subclass of `WeatherProviderError`, and every message says what
was attempted. There is no code path that answers a failure.

| Failure | Class | Retried? |
| --- | --- | --- |
| connect timeout / read timeout / DNS / connection refused | `WeatherProviderUnavailable` | yes, once |
| HTTP 5xx | `WeatherProviderResponse` | yes, once |
| HTTP 4xx — e.g. a date outside the forecast range | `WeatherTargetUnavailable` | **no** (deterministic) |
| non-JSON body (an HTML error page) | `WeatherProviderResponse` | no |
| missing hazard variable / ragged arrays / unknown unit / bad instant | `WeatherProviderResponse` | no |
| replay naming no snapshot, or an unresolvable one | `WeatherProviderNotConfigured` | n/a |
| `httpx` not installed | `WeatherProviderNotConfigured` (names the `weather` extra) | n/a |
| a naive target instant | `WeatherTargetUnavailable`, refused before a request is sent | n/a |
| zero/infinite timeout, or a third attempt | `WeatherProviderNotConfigured` (configuration error, at construction) | n/a |

The specific forbidden behaviours are absent and tested as absent:

* **no silent live→replay fallback.** `test_a_failed_live_call_never_constructs_a_replay_provider` makes `ReplayWeatherProvider.__init__` raise `AssertionError` and then fails a live call on purpose; the error that escapes is the provider error, so any substitution would have announced itself.
* **no fake weather, no copied NOW values, no zero-filled features.** A missing reading stays missing: the Safety Core's own `weather_state=None` path is what handles an absent reading, and it is unchanged from Phase 1.
* **no infinite retry**: `max_attempts=2` maximum, validated at construction.
* **no nondeterminism from failure**: a timeout is raised, not answered.

---

## 9. Future Weather Demonstration (spec 15)

`.venv/bin/python -m trailguard.weather_smoke` — **exit 0**, replay only, no network, no
LLM, no key, no GPU. It runs the frozen S01 chain through all four horizons, calling the
real `get_weather_for_route_time` once per horizon (a `_RememberingReplayProvider`
subclass asserts exactly one lookup per horizon and that the reading it saw *is* the
object the tool returned).

Real output:

| Horizon | Projected position | Projected arrival | Row read | Gust | Verdict |
| --- | --- | --- | --- | --- | --- |
| NOW | 46.08102, 7.77210 | 10:30Z | 10:00Z | 14.0 km/h | GREEN, score 0, no rule fired |
| +30min | 46.09448, 7.77900 | 11:00Z | 11:00Z | 16.0 km/h | GREEN, score 0, no rule fired |
| +60min | 46.10791, 7.78593 | 11:30Z | 11:00Z | 16.0 km/h | GREEN, score 0, no rule fired |
| +120min | 46.13003, 7.81087 | 12:30Z | 12:00Z | **58.0 km/h** | **YELLOW, score 40, R-C fired** |

Each block prints Horizon, Projected Position, Projected Arrival Time, Weather Source,
Weather Timestamp, Weather Values, Freshness, Confidence, Risk Level, Risk Driver, the
single-point disclosure and the grid reach. Only the horizon changed — same GPX, same
device feed, same fixture, same rule pack — and the verdict changed because the weather
was asked about at a different position and a different instant: 58 km/h crosses R-C's
45 km/h `wind_gust_kmh` demo threshold while 14 and 16 do not. The R-C driver detail
names both the projected position and the projected arrival time, so a reader can check
the claim.

The equivalent assertions exist so nobody has to take the table's word for it:
`test_now_is_safe_and_the_projected_position_two_hours_out_is_hazardous`,
`test_the_future_verdict_is_driven_by_the_projected_positions_own_weather`.

The demo fixture `replay/weather/scn_weather_future_hazard.json` is a **new file** (§26):
`synthetic: true`, `source: "synthetic weather fixture, not live data"`, and a note in
capitals stating it is not Open-Meteo historical data and not a real forecast. It declares
no `confidence`, so it charges the unknown value like any other provider that declares
none. Its gusts (14, 16, 58, 72, 64 km/h at 10:00–14:00) were chosen against the measured
projected positions — the +120 position really is 6 223.8 m from the fixture's declared
point, which is why the fixture declares a 7 km tolerance.

The four-horizon walk is labelled in its own output as a **demonstration harness, not a
tool**: each iteration makes one single-horizon call and the loop itself belongs to
`SKILL.md`. `No forecast_all_horizons() exists.`

---

## 10. Tests

```
$ .venv/bin/python -m pytest -v
collected 544 items
============================= 544 passed in 4.94s ==============================
failed    0
skipped   0
warnings  none reported (no warnings summary section)
time      4.94s  (4.92s / 4.89s on repeat runs)
exit      0
```

Phase 1 shipped 487; **Phase 2 adds 57**, all in `tests/test_weather_providers.py`
(1 347 lines). No Phase 0 or Phase 1 test was deleted, weakened or skipped; the only edit
to an existing test file is `tests/test_packaging.py`, which was **strengthened**
(`httpx` added to the later-phase library set, and `trailguard.weather` named explicitly
in the import-footprint check so "importing the weather package leaves httpx unimported"
is asserted rather than incidental) and `tests/conftest.py`, which gained
`collect_ignore` for the live script.

Per-file counts: `test_deterministic_core.py` 60, **`test_weather_providers.py` 57**,
`test_schemas.py` 49, `test_trace_schema.py` 47, `test_tool_contracts.py` 42,
`test_scenario_fixtures.py` 42, `test_evals.py` 37, `test_harness.py` 36,
`test_device_feeds.py` 32, `test_risk_core.py` 27, `test_rules_pack.py` 24,
`test_providers.py` 22, `test_secrets_git.py` 21, `test_replay_weather.py` 21,
`test_risk_level.py` 17, `test_packaging.py` 10.

**§24 coverage map** (item → test):

| Required coverage | Test |
| --- | --- |
| replay exact lookup | `test_the_exact_row_instant_is_served_with_zero_reach` |
| replay time selection (nearest, tie→earlier) | `test_the_nearest_row_wins_and_a_tie_goes_to_the_earlier_row` |
| no interpolation of a discrete code | `test_a_discrete_code_is_never_interpolated` |
| route position reaches the weather query | `test_the_projected_position_reaches_the_weather_query`, `test_the_position_comes_from_the_projection_and_is_never_re_guessed` |
| target arrival time reaches the weather query | `test_the_target_arrival_time_reaches_the_weather_query` |
| live normalization | `test_a_live_response_is_normalised_into_the_contract_object` |
| HTTP timeout | `test_a_timeout_is_raised_rather_than_answered`, `test_a_read_timeout_is_raised_rather_than_answered`, `test_an_unreachable_host_is_raised_with_the_reason` |
| HTTP 5xx | `test_a_5xx_is_retried_at_most_once_and_then_fails_loudly`, `test_a_5xx_followed_by_a_good_answer_succeeds_on_the_retry` |
| HTTP 4xx (a date the API cannot describe) | `test_a_4xx_is_not_retried_and_names_the_unavailable_instant` |
| malformed response | `test_a_non_json_body_is_a_visible_failure`, `test_ragged_parallel_arrays_are_a_visible_failure`, `test_a_unit_the_normaliser_will_not_guess_is_a_visible_failure` |
| missing required field | `test_a_missing_hazard_variable_is_a_visible_failure` |
| timezone conversion | `test_a_response_written_in_a_named_timezone_is_converted_to_utc`, `test_the_generated_instant_a_document_declares_wins_over_the_retrieval_instant` |
| naive instant refused | `test_a_naive_target_time_is_refused_before_any_lookup`, `test_a_naive_target_time_is_refused_before_a_request_is_sent` |
| stale | `test_stale_weather_is_reported_as_stale_and_never_as_fresh` |
| fresh | `test_a_snapshot_within_budget_is_fresh_and_one_past_it_is_stale`, `test_the_demo_horizons_are_fresh_enough_to_assess` |
| replay freshness measured at the projected arrival | `test_freshness_is_measured_at_the_projected_arrival_not_the_analysis_clock` |
| provider confidence absent | `test_a_provider_that_declares_no_confidence_is_charged_and_flagged` |
| `confidence_is_assumed` semantics | `test_a_provider_that_declares_no_confidence_is_charged_and_flagged` (asserts `!= 0.9` and `is_assumed`), `test_a_declared_confidence_is_reported_as_the_providers_own`, `test_the_replay_fixture_charges_its_unknown_confidence_too` |
| position coverage disclosed / tolerated | `test_the_distance_to_the_snapshot_point_is_disclosed`, `test_a_query_beyond_the_declared_tolerance_is_refused` |
| future hazard at a later horizon | `test_the_future_verdict_is_driven_by_the_projected_positions_own_weather`, `test_each_horizon_reads_its_own_weather_rather_than_a_copy` |
| NOW safe + +120 hazardous | `test_now_is_safe_and_the_projected_position_two_hours_out_is_hazardous` |
| no hidden network in pytest | autouse `no_external_network` in `tests/conftest.py` (closes the socket layer for every test) + `test_a_replay_lookup_opens_no_socket` (counts socket constructions) + `collect_ignore` |
| no live→replay fallback | `test_a_failed_live_call_never_constructs_a_replay_provider`, `test_replay_is_reachable_only_when_the_caller_chose_replay`, `test_a_live_policy_naming_a_source_this_build_lacks_is_refused`, `test_no_policy_means_live_weather` |
| replay never touched by the frozen scenarios' live expectation | `test_the_live_provider_is_never_touched_by_the_frozen_scenarios` |
| no one-shot future-risk tool | `test_the_weather_tool_cannot_be_handed_more_than_one_instant`, `test_one_projection_yields_exactly_one_provider_call`, `test_two_projections_yield_two_calls_because_the_loop_belongs_to_the_caller` — plus Phase 0's `tests/test_tool_contracts.py` guard, unchanged |
| §25 the frozen fixture is not rewritten | `test_the_phase_zero_snapshot_is_byte_for_byte_the_frozen_asset` (sha256 `817543047a1b…`), `test_the_phase_zero_snapshot_is_still_an_open_meteo_shaped_document`, `test_the_new_demo_fixture_is_a_new_file_not_a_rewrite` |
| credential never sent | `test_build_params_never_sends_a_credential` |
| httpx absent is a configuration error | `test_httpx_absent_is_a_configuration_error_not_a_crash` |
| configuration validated at construction | `test_an_infinite_timeout_is_refused_as_configuration`, `test_a_third_attempt_is_refused_as_configuration` |

`python -m pytest` makes **zero** external HTTP calls, and the guarantee is structural:
an autouse fixture replaces `socket.socket` with a function that raises naming spec 13, so
a test that reached the wire would fail rather than quietly pass. The live provider is
exercised through `httpx.MockTransport` only. `pytest scripts/` collects **0 items**
(`testpaths = ["tests"]` plus `collect_ignore`), so the live script cannot be reached by
a normal run or by pointing pytest at `scripts/`.

---

## 11. Frozen Asset Verification

| Asset | sha256 now | Status |
| --- | --- | --- |
| `SKILL.md` | `98fd007eb23aee18f98bf67c295d7bad3c8e48f7a722022c852218fb0c649769` | **unmodified** — byte-identical to `3c41ae3` (Phase 0) and to `HEAD` |
| `trailguard/rules/rules_v1.yaml` | `b2630f3d32e50d6533a01172fbdd4f5db670fcdc474a2c8ebbf1bcebda71a5d4` | **unmodified** — byte-identical since Phase 0 |
| `trailguard/rules/calibration_v1.yaml` | `05bb20a16d7315c7693b0eb8b642ac3fa810555c259add43833c90d77c553ec5` | **unmodified** — added by Phase 1, untouched in Phase 2 |
| `replay/weather/scn_weather_snapshot.json` (Phase 0) | `817543047a1b002d8fece6eaa9ca0cffe76500a5d964f46deb29fa5e97ee23bd` | **unmodified** — matches the pin, and `git diff 3c41ae3 HEAD` on it is empty |

`git diff 3c41ae3 HEAD -- SKILL.md trailguard/rules/ replay/weather/scn_weather_snapshot.json`
returns exactly one entry: the Phase 1 **addition** of `calibration_v1.yaml` (281 lines,
a new file). Nothing under those paths was edited.

The Phase 2 demo fixture is a new file, `replay/weather/scn_weather_future_hazard.json`,
and a test pins that it is new rather than a rewrite.

**The one deviation, restated from §1:** `phase1-freeze` (5a93fd7) contains the Phase 2
*code* (the weather package, the implemented tool body, the additive schema field, the
synthetic fixture). It does **not** contain any modification to the four frozen assets
above — all four are byte-identical to their Phase 0 bytes, and that is the property the
freeze exists to protect.

---

## 12. Adversarial Review (spec 30, Q1–Q14)

**Q1 — Does the weather query really use `projected_position`?**
Yes, and it is asserted rather than trusted. `weather_for_projection()` reads
`projection.projected_position` and passes it as `lat`/`lon`;
`test_the_projected_position_reaches_the_weather_query` pins the values the provider
received against the projection's own, and `test_the_position_comes_from_the_projection_and_is_never_re_guessed`
refuses a path that re-derives a position. The live run asked about
`lat 46.130031 lon 7.810875` — the S01 +120 projection, printed by the smoke as
`lat 46.13003 lon 7.81087`.

**Q2 — Does it really use `projected_arrival_time`?**
Yes. The same helper passes `projection.projected_arrival_time` as `target_time`;
`test_the_target_arrival_time_reaches_the_weather_query` pins the instant. In the live run
the matched row (`14:00Z`, 24.9 min away) and the request date were both derived from
that instant, not from the clock.

**Q3 — Is any future horizon secretly using current weather?**
No. One horizon → one projection → one lookup, so a future horizon cannot reuse the
current reading without calling the tool again. `test_each_horizon_reads_its_own_weather_rather_than_a_copy`
runs all four horizons and requires four arrival instants, four matched rows and more
than one gust value; the real smoke output shows 14.0 / 16.0 / 16.0 / 58.0 km/h. There is
no caching layer anywhere in `trailguard/weather/`.

**Q4 — Are replay and live strictly separated?**
Yes. They are two classes behind one ABC, and the mode is chosen in exactly one place,
`provider_for_policy()`. Replay is reachable only through
`WeatherPolicy(mode="replay", snapshot_ref=...)`, and live through `mode="live"` or no
policy at all: `test_replay_is_reachable_only_when_the_caller_chose_replay`,
`test_no_policy_means_live_weather`. Both share the normaliser, which is deliberate — a
second parser would let a bug be fixed on one path and left on the other — but they share
no state, no fixture lookup and no fallback.

**Q5 — Does a live failure silently fall back?**
No, and this is the strongest test in the suite for it: it monkeypatches
`ReplayWeatherProvider.__init__` to raise `AssertionError`, then fails a live call on
purpose. If any path had substituted the fixture, the test would report the substitution
instead of the provider error. Every failure mode in §8 raises, and
`test_replay_is_reachable_only_when_the_caller_chose_replay` covers the policy level too.

**Q6 — Is pytest completely free of external network?**
Yes, structurally. An autouse fixture in `tests/conftest.py` replaces the socket layer
with a raiser that names spec 13, so the attempt is impossible rather than merely
unlikely; the live provider is exercised through `httpx.MockTransport`;
`testpaths = ["tests"]` and `collect_ignore = ["../scripts/test_live_weather.py"]` keep
the one socket-opening script out (`pytest scripts/` → 0 items). Verified: 544 tests pass
with the socket layer closed, and `test_a_replay_lookup_opens_no_socket` counts socket
constructions to prove a replay lookup makes none.

**Q7 — Is confidence fabricated as a provider's official figure?**
No. Open-Meteo declares none, so the reading carries the calibration's
`confidence_unknown_value` (0.5) with `confidence_is_assumed=True`. A test asserts the
value is **not** `0.9`, and grep finds no `0.9` in `trailguard/weather/` or in the weather
tool. When a provider does declare a figure it is reported as the provider's own with the
flag `False`. The Safety Core's confidence model was not touched, so a charged 0.5 and an
asserted 0.5 remain distinguishable at the point of use.

**Q8 — Is the timezone explicit everywhere?**
Yes. Internal instants are UTC-aware; `timezone=UTC` is sent explicitly on live requests;
naive instants are refused both in the parser and at the live provider *before* a request
is built; a document's declared `utc_offset_seconds` is what makes its naive rows aware,
and a document in a named timezone is converted to UTC
(`test_a_response_written_in_a_named_timezone_is_converted_to_utc`). The two legitimate
wall-clock reads in the package are the live provider's `retrieved_at` — the instant a
forecast was actually fetched, which is the age a live reading is honestly measured
against and is disclosed in the reading's notes — and the live smoke's `--at` default.
Neither is in the replay or core paths.

**Q9 — Is the frozen weather snapshot byte-unchanged?**
Yes: sha256 `817543047a1b002d8fece6eaa9ca0cffe76500a5d964f46deb29fa5e97ee23bd`, identical
to the Phase 0/Phase 1 pin; `git diff 3c41ae3 HEAD -- replay/weather/scn_weather_snapshot.json`
is empty; and `test_the_phase_zero_snapshot_is_byte_for_byte_the_frozen_asset` fails the
suite if it ever moves. Phase 1's earlier mistake (rewriting it into a TrailGuard-native
shape and reverting) is not repeated: the demo fixture is a new file.

**Q10 — Is `get_weather_for_route_time` still atomic?**
Yes. Its declared contract is unchanged (`lat`, `lon`, `target_time`); it returns one
`WeatherForRouteTime` for one `(position, instant)` pair; it has no `horizon_min`, no list
argument and no loop. `test_the_weather_tool_cannot_be_handed_more_than_one_instant`
pins the shape, `test_one_projection_yields_exactly_one_provider_call` pins the single
lookup, and the registry still holds exactly five tools. It returns weather, never a risk
level (`test_the_tool_returns_weather_and_never_a_risk_level`).
**Q11 — Has a one-shot 30/60/120 tool appeared?**
No. `grep -rnE "^\s*(def|class)\s+(forecast_risk|forecast_all_horizons|predict_30_60_120|future_risk_bundle)"`
finds no definition anywhere in the repository; the strings appear only in the ban
statement, in Phase 0's illegal-name guards and in docstrings explaining their absence.
`test_two_projections_yield_two_calls_because_the_loop_belongs_to_the_caller` asserts the
opposite behaviour: a caller that wants four horizons makes four calls. The smoke's
four-horizon walk is a demonstration harness that says so in its own output.

**Q12 — Does the Safety Core still not depend on an LLM?**
No dependency, and it cannot acquire one: the core consumes a `WeatherForRouteTime` and
has no way to tell which provider produced it, so there is nothing for a model to
influence. The whole chain — GPX → projection → weather → verdict — ran in this phase
with no key, no network, no GPU and no model (`python -m trailguard.smoke` and
`python -m trailguard.weather_smoke`, both exit 0). The Phase 1 test that deletes
`STEPFUN_API_KEY` and runs the core still passes, unchanged.

**Q13 — Is `SKILL.md` still frozen?**
Yes — byte-identical to Phase 0 and to the working tree (`git diff` empty both ways,
sha256 above). No line of the protocol text was touched to accommodate the new weather
code; the horizon loop, freshness checks and re-query of future weather remain the
Skill's job (spec 21).

**Q14 — Has StepFun or the local Qwen model been integrated early?**
No. `grep -rni "qwen|stepfun|127.0.0.1|8000/v1"` across `trailguard/weather/`,
`trailguard/tools/`, `trailguard/weather_smoke.py`, `tests/test_weather_providers.py` and
`scripts/` returns two hits, both prose in docstrings explaining that they are *not*
used. `trailguard/providers/` is untouched by this phase, the DGX Spark service at
`http://127.0.0.1:8000/v1` was never contacted, and no model was called anywhere in the
Phase 2 chain (spec 22, 23).

---

## 13. Known Limitations

1. **`phase1-freeze` carries Phase 2 code** (§1, §11). Labelling, not data — but it is on the record because "the Phase 1 tag is Phase 1 only" would be false.
2. **Hourly granularity is all the forecast API offers.** A projected arrival at 12:37 is answered from the 12:00 row, 37 minutes away; `reach_minutes` discloses it. Finer grids or interpolation are future decisions.
3. **A live query covers one UTC date.** `start_date = end_date = <target date>`; a horizon crossing midnight UTC would need a second request. Replay is unaffected — a snapshot carries whatever rows it carries.
4. **A replay snapshot is single-point.** It cannot vary spatially, so the distance to its declared point is disclosed, and only a *declared* `location_tolerance_km` turns that disclosure into a refusal. Inventing a coverage radius would be inventing a threshold.
5. **Live weather answers for the API's grid point.** The real run disclosed `(46.12, 7.7999997)` for a projected position of `(46.13003, 7.81087)`. The provider says so; it does not pretend the projection was the point measured.
6. **Confidence is charged, never provider-asserted.** A live reading's confidence is therefore always 0.5 with `confidence_is_assumed=True`. That is a statement about what Open-Meteo publishes.
7. **`weather_code` is passed through, not interpreted.** The core consumes the numeric fields; turning a WMO code into a hazard would be a seventh rule the spec does not authorise.
8. **No caching, no retry budget beyond one, no circuit breaker.** A four-horizon loop in live mode makes four requests on the critical path. Acceptable for the benchmark (which is replay) and for the demo; the timeouts bound the worst case.
9. **One `WeatherReading` per call, with no aggregation.** If a future phase wants a horizon sweep, the loop must live in the Skill or in an explicitly-labelled harness — never inside the tool.
10. **The live smoke's `--at` default reads the wall clock** (`datetime.now(timezone.utc)`), documented in the script as the one legitimate use, since a live query is by definition about now). It is a developer script, never collected by pytest, and pytest itself makes no clock read for weather.
11. **`httpx` is installed in this environment (0.28.1) but not pinned in `requirements.txt`.** The pin list is append-only and owned by the orchestrator; the `weather` extra declares `httpx>=0.27`, and the installed version satisfies it.

---

## 14. Final Verdict

**PASS — Ready for Phase 3.**

Evidence, all re-run in this phase rather than recalled:

* `python -m pytest -v` → **collected 544, passed 544, failed 0, skipped 0, no warnings, 4.94s, exit 0.** The 57 new tests cover every item §24 requires, mapped item-by-item in §10.
* `python -m trailguard.weather_smoke` → **exit 0**, replay only, no network, no LLM, no key: NOW/+30/+60 all GREEN with gusts 14.0/16.0/16.0 km/h and +120 YELLOW with R-C firing on 58.0 km/h at the projected position and projected arrival time.
* `scripts/test_live_weather.py` → **exit 0** against the real Open-Meteo API (and the unavailable path prints `LIVE WEATHER NETWORK UNAVAILABLE` and exits non-zero rather than faking success).
* `pytest scripts/` → **0 items collected**: the suite cannot reach the socket.
* The four frozen assets are **byte-identical** to their Phase 0 bytes (sha256 and `git diff` in §11); `SKILL.md` and `rules_v1.yaml` were not touched.
* The five atomic tools remain five and atomic; no `forecast_risk` / `forecast_all_horizons` / `predict_30_60_120` / `future_risk_bundle` exists; the horizon loop stays in `SKILL.md`; the Safety Core still uses no LLM and no clock; no credential was read, printed or committed; nothing was pushed and no remote exists.
* The one thing this phase got wrong is disclosed rather than buried: the Phase 1 freeze commit already contained the Phase 2 scaffolding (§1). No frozen *data* was affected, and the deviation is recorded here instead of being papered over by re-tagging history.

Stopping here, per §33: Phase 3, Phase 4 and Phase 5 are **not** started, and nothing has
been pushed.
