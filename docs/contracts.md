# Tool Contracts

Source of truth for everything on this page: `trailguard/tools/__init__.py`
(`ToolSpec`, `TOOL_SPECS`, `TOOL_FUNCTIONS`, `tool_schema_dicts()`). The specs there
are machine-readable and are what the agent harness advertises to a model; this page
is the human-readable mirror of the same content.

Frozen rule (spec 4): **five atomic tools, one capability each.** No tool may answer
the 30/60/120 question in one call, and the registry must not grow during the MVP.

All five now have real bodies: four implemented in Phase 1, `get_weather_for_route_time`
in Phase 2. Five bodies are not five decisions -- the tools stay atomic, the horizon
loop still belongs to the Skill, and the deterministic core still needs no LLM, no key
and, in `mode = "replay"`, no network at all. See
[`docs/route_aware_weather.md`](docs/route_aware_weather.md) for how the weather body
acquires and normalises a reading.

---

## `get_route_context`

* **Implemented in:** Phase 1 (`trailguard/tools/route_context.py`, spec 5.1) -- real
  geodesic geometry over `replay/routes/*.gpx`
* **Responsibility:** return where the hiker currently is on a known route -- progress,
  nearest point on the planned polyline, remaining distance and ascent, off-route
  distance in metres and route data quality.
* **Input:** `route_id: str`, `current_position: GeoPoint`, `timestamp: datetime`
  (`GetRouteContextInput`)
* **Output:** `RouteContext` (`trailguard/schemas/context.py`) -- `route_progress`,
  `off_route_distance_m`, `remaining_distance_km`, `remaining_ascent_m`,
  `current_altitude_m`, `route_polyline_ref`, `data_quality`
* **Prohibited uses:**
  * must not output risk levels or safety verdicts
  * must not query weather or judge freshness
  * must not project a future position; that is `project_route_state`
* **Geometry note:** every reported metre is a WGS-84 geodesic distance
  (`pyproj.Geod`); the local azimuthal-equidistant + Shapely projection is used only to
  find *which* point of the polyline is nearest, and the answer is then re-measured
  geodesically. No DEM, no raster: elevation comes from the GPX itself.

## `project_route_state`

* **Implemented in:** Phase 1 (`trailguard/tools/route_projection.py`, spec 5.2, 6.2)
* **Responsibility:** project the hiker's future route state at **one** horizon,
  conditional on continuing at the currently observable movement trend: projected
  progress, projected position, projected arrival time, projected remaining distance
  and projection confidence.
* **Input:** `route_context: RouteContext`, `movement: MovementTrend`,
  `horizon_min: int` -- movement carries only speeds (`current_speed_kmh`,
  `avg_speed_10m`, `avg_speed_30m`); device state is deliberately excluded and enters
  through the safety core
* **Output:** `RouteProjection` (`trailguard/schemas/route.py`)
* **Prohibited uses:**
  * must not query weather; that is `get_weather_for_route_time`
  * must not judge risk or return a risk level -- a projection is a state, and the
    domain concept is a *Projected Risk State*, never an accident probability
  * must not answer the 30/60/120 horizon set in one call; the horizon loop belongs to
    the Skill
  * must not apply medical or injury thresholds; the fatigue penalty is a configurable
    demo-calibration parameter (spec 6.3)
* **Two honest refusals:** a `horizon_min` outside 0/30/60/120 raises rather than
  silently accepting an unmodelled horizon, and a projection requested without an
  explicit `analysis_time` raises rather than reading the wall clock.

## `get_weather_for_route_time`

* **Implemented in:** Phase 2 (`trailguard/tools/weather_route_time.py`, spec 5.3) --
  the last of the five bodies; the other four were implemented in Phase 1
* **Responsibility:** return weather for a target position and target time -- typically
  the projected position and projected arrival time of a horizon -- with provider
  provenance, freshness in minutes and confidence.
* **Input:** `lat: float`, `lon: float`, `target_time: datetime`
* **Output:** `WeatherForRouteTime` (`trailguard/schemas/weather.py`) -- hazard
  features plus `source`, `forecast_generated_at`, `target_time`,
  `freshness_minutes`, `confidence`, `confidence_is_assumed`, and
  `freshness_status(max_freshness_min)`
* **How the weather is obtained (spec 17):** the acquisition mode is a *choice*, and
  it is the caller's. `provider=` injects a built provider; otherwise `policy=`
  selects one, and **no policy means live** -- Open-Meteo, the real capability -- so a
  frozen fixture can never be substituted for a real question by accident. A policy
  with `mode = "replay"` reads the snapshot its `snapshot_ref` names and refuses
  rather than defaulting to another file. A live failure never falls back to replay.
* **Confidence (spec 9):** `confidence_is_assumed` is `True` when the provider
  declared no figure and the value is the calibration's `confidence_unknown_value`
  charged in its place. Open-Meteo declares none, so a live reading always carries a
  *charged* confidence, never an invented 0.9.
* **Disclosed extras:** the returned `WeatherReading` (`trailguard/weather/base.py`)
  carries `row_time`, `reach_minutes` (how far the discrete grid had to reach),
  `confidence_is_assumed` and `notes` -- the disclosures with no field of their own,
  such as "the projected position is N m from the point this snapshot describes".
  `reading_provenance(reading)` flattens one reading to the trace's keys, so the
  smoke run, the trace and a test cannot disagree about what weather provenance is.
* **Prohibited uses:**
  * must not output safety levels; hazard interpretation is rule R-C's job
  * must not fabricate, interpolate or best-guess weather when no snapshot exists;
    report the gap instead
  * must not decide whether stale weather is acceptable; that is the Skill's freshness
    protocol
  * must not project a position, run the safety core, or answer more than one horizon
    in a call -- one call answers one `(position, instant)` pair (spec 4.2)
* **One helper, not a sixth tool:** `weather_for_projection(projection, ...)` reads
  `projection.projected_position` and `projection.projected_arrival_time` and makes
  exactly one provider call. It exists so "the position comes from the projection,
  never re-guessed" is written once; it takes a single projection and cannot loop.

## `get_user_device_context`

* **Implemented in:** Phase 1 (`trailguard/tools/user_device.py`, spec 5.4) -- reads
  the frozen per-scenario feed in `replay/devices/`
* **Responsibility:** return the hiker device state -- battery percent, network
  availability, last network contact and the movement/fatigue features the
  deterministic rules consume.
* **Input:** `device_id: str`
* **Output:** `DeviceState` (`trailguard/schemas/context.py`) -- the whole device feed
  of spec 5.4: `battery_percent`, `network_available`, `fatigue_index`,
  `last_network_time`, and the observed movement window
  (`movement: MovementState` with `current_speed_kmh` / `avg_speed_10m` /
  `avg_speed_30m` / `pace_drop_percent`) plus `data_quality`.
  Two duplications are deliberate and spec-mandated: `movement` repeats the window the
  shared context already carries, so the tool output stands on its own in the
  transcript; `data_quality` is *this feed's* self-assessment, which is not
  `DataQualityReport.device_data_quality` (one is a source claim, the other an
  aggregation of the whole snapshot).
* **Why the window is inside:** the frozen movement model of spec 6.3
  (`base_speed = 0.5*avg_speed_10m + 0.3*avg_speed_30m + 0.2*current_speed`) and the
  pace-degradation rule R-D consume exactly these fields, so the deterministic core can
  derive an effective speed, a projected finish and a time margin from the tool output
  alone.
* **Prohibited uses:**
  * must not present `fatigue_index` as a medical model output; it is a
    rule-constructed simulated feature
  * must not infer health, exhaustion or injury
  * must not output risk levels or decide whether the remaining battery is sufficient
  * must not report `data_quality` as a confidence in the verdict -- it is the feed's
    own quality claim, not a risk score
* **Feed semantics:** a feed is a frozen list of `(t_offset_min, DeviceState)` rows. It
  is addressed by id, never by a path, and a request past its last row is refused
  rather than extrapolated -- a device feed is a replay asset, not a model.

## `assess_risk`

* **Implemented in:** Phase 1 (`trailguard/tools/risk_assessment.py`, spec 5.5),
  delegating to `trailguard/risk_core/engine.py`, with thresholds from
  `rules/rules_v1.yaml` and evaluation metadata from `rules/calibration_v1.yaml`
* **Responsibility:** run the deterministic safety core for **one** horizon over the
  supplied states -- hard rules set a severity floor, soft rules aggregate into a
  score, compound soft signals escalate one level -- and return level, score, drivers,
  triggered hard rules, confidence, evidence references and explainable safety margins
  with physical units.
* **Input:** `route_state: RouteContext`, `weather_state: WeatherForRouteTime | None`,
  `user_device_state: DeviceState`, `time_state: TimeState`, and optionally
  `projected_state: RouteProjection | None`, `horizon_min`, `weather_freshness_min`,
  `data_quality` and a pre-built `engine`
* **Output:** `RiskResult` (`trailguard/schemas/risk.py`) -- `risk_level`,
  `risk_score`, `drivers`, `hard_rules_triggered`, `confidence`, `evidence_refs`,
  `safety_margins`, `horizon`, `computed_at`
* **Prohibited uses:**
  * must not project future positions; callers supply the horizon's route state
  * must not fetch weather; callers supply the horizon's weather state
  * must not run the 30/60/120 loop or aggregate horizons
  * must not override or downgrade a triggered hard rule
  * must not return an accident probability; the concept is a Projected Risk State
* **Two honest states rather than two defaults:**
  * `weather_state=None` legalises a *missing* weather reading. Every weather feature
    becomes not-computable, rule R-C is disclosed as not evaluated at this horizon, and
    confidence drops accordingly. It is never silently skipped and never substituted
    with a default.
  * `projected_state=None` legalises a caller that declined to project (the NOW
    horizon). The projection-derived margins -- projected battery, projection
    confidence, time margin -- are then reported as unknown rather than invented.
* **Confidence is an engineering statement, not a probability** (spec 10.3, 10.4): it
  describes the trustworthiness of the *inputs* -- route data quality, device data
  quality, weather age and snapshot quality, projection reliability, and the caller's
  disclosed missing/stale fields -- weighted by the demo-calibration table in
  `calibration_v1.yaml`. A 0.9-confidence RED verdict does not mean "90 % dangerous";
  it means the inputs behind that verdict are good ones. Confidence accompanies a
  verdict; it never moves one.

---

## The banned `forecast_risk` pattern

Spec 4.2 forbids a tool shaped like this:

```python
def forecast_risk(...) -> dict:
    return {"30m": "YELLOW", "60m": "ORANGE", "120m": "RED"}
```

and it is absent from `TOOL_SPECS`, `TOOL_FUNCTIONS` and `tool_schema_dicts()` by
construction -- there are exactly five entries and no "convenience" aggregate.

Why the ban exists: if one call already produced the whole answer, the Skill would
have nothing left to do, and the experiment "does teaching a model the professional
protocol improve it?" would be measuring the tool, not the skill. The horizon loop,
the tool combination, the freshness checks and the re-query of future weather are
exactly the reasoning TrailGuard claims to teach. Keeping the loop in `SKILL.md` makes
that reasoning visible in the evidence trace (one `project_route_state` ->
`get_weather_for_route_time` -> `assess_risk` triple per horizon) and keeps the
deterministic core auditable and re-tunable as data rather than as prompt text.

The same reasoning excludes `find_safe_points` and `find_escape_option` from the
Phase 0 registry: they are optional later-phase capabilities (spec 4.1) and adding
them now would blur the atomic-tool contract.

## Fairness: both conditions get identical tools

The baseline ("without skill") and the treatment ("with skill") receive the *same*
registry, the *same* `tool_schema_dicts()` output and the same execution path
(`trailguard/agent/harness.py::_resolve_registry`). `skill_enabled` changes only the
system prompt: it appends the `SKILL.md` text when true and adds nothing when false. The
prompt it appends *to* -- `HARNESS_BASE_SYSTEM_PROMPT` -- is byte-identical in both
conditions and deliberately neutral; `tests/test_harness.py` pins both halves of that.

In particular the baseline must not be handicapped: no "only look at current weather"
hint, no reduced tool set, no degraded schema. The baseline is a normal general-purpose
agent facing the same task, the same context snapshot, the same temperature, the same
`max_tokens` and the same `max_steps`. Anything else would make the comparison
unfalsifiable (spec 3.1); `evals/compare.py` enforces these preconditions before it
aggregates anything.

### What the fairness audit compares -- and what it must not

`evals/compare.compare_runs` checks four preconditions: the **same scenario ids**, the
**same model**, the **same offered tool registry** and the **same sampling parameters**
(within each condition *and* between them).

The tool-set check compares `EvalRunResult.tools_offered` -- the registry the caller
handed to `run_scenario` -- and nothing else. It deliberately does **not** compare the
tools the runs *called*: a with-skill run calling `project_route_state` three times where
the baseline called nothing is the effect the benchmark exists to measure, not a
configuration violation. Comparing observed calls would reject every legitimate result;
comparing nothing at all would wave through two runs that both called nothing. A run that
recorded no offered registry is therefore a hard error ("cannot be audited"), never a
silent pass. As a second guard, a trace that records a call the run was never offered
fails the audit: the trace and its registry must describe the same run.
