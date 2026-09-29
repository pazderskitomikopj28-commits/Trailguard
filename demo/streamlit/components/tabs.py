"""The five tabs, and nothing else.

Five is the ceiling the brief sets, and the ceiling is the point: a demo with
twelve tabs is a demo where the judge has to find the value. So each tab answers
exactly one question, in the order a judge asks them:

1. **Overview** -- what is this, and what is the answer for this scenario?
2. **Risk Timeline** -- where does that answer come from, and what happens next?
3. **Evidence** -- why should I believe it?
4. **Benchmark** -- what was measured, including what went the wrong way?
5. **Architecture** -- how is it built, and what was frozen to build it?

Agent Mode is not a sixth tab. It lives inside Risk Timeline as a switch between
who reads the five tools -- the deterministic core, or a real local Qwen driving
them over the harness protocol -- because that contrast *is* the answer to the
question that tab is about. It is also the only part of the demo that can fail,
and putting it one click deeper than the demo's core value is deliberate: the
core value must not depend on a model being up.
"""

from __future__ import annotations

import streamlit as st

from demo.presenters.timeline import LEVEL_STYLE
from demo.presenters.weather_view import weather_rows
from demo.streamlit.components import charts, panels
from demo.streamlit.components.panels import callout, svg
from demo.streamlit.components.text import fenced

#: The five atomic tools, in the order the frozen Skill calls them, with what
#: each one is allowed to decide. Taken from the tool registry, not invented here.
TOOL_CHAIN = [
    ("get_route_context", "where is the hiker on the planned route",
     "progress, off-route distance, remaining distance and ascent"),
    ("project_route_state", "where would they be at one horizon, and when",
     "position, arrival time, and the confidence in that projection"),
    ("get_weather_for_route_time", "what is the weather at that projected point, at that projected time",
     "the forecast for a future place, never the weather at the start"),
    ("get_user_device_context", "what is the device and the hiker doing",
     "battery, network, speeds, pace drop, fatigue -- and whether the feed was stale"),
    ("assess_risk", "what level does that add up to, at this horizon",
     "GREEN / YELLOW / ORANGE / RED, from the frozen rule pack"),
]

#: What Phase 6 is forbidden to touch, quoted from the brief. Shown so a judge can
#: ask "what did you change to make the demo work?" and get the answer from the
#: screen rather than from a conversation.
FROZEN = [
    "SKILL.md -- the agent protocol, hash-pinned to the build Phase 5 evaluated",
    "trailguard/rules/rules_v1.yaml -- the six rules and their thresholds",
    "trailguard/rules/calibration_v1.yaml -- the bands, floors and weights",
    "the deterministic safety core -- engine, escalation and hard-rule floor",
    "the five atomic tools -- one question each, no one-shot tool",
    "the 14 benchmark scenarios and their frozen inputs",
    "Phase 5's metric definitions, raw runs and evaluation results",
    "Qwen3-30B-A3B in BF16 at max_model_len = 16384",
]


# --------------------------------------------------------------------------- #
# 1. Overview
# --------------------------------------------------------------------------- #


def overview(run, overview_view) -> None:
    panels.verdict(run)

    st.markdown(f"### {overview_view.headline}")
    st.markdown(f"**{overview_view.subhead}**")

    left, right = st.columns([1.35, 1])
    with left:
        st.markdown("**What it does**")
        for line in overview_view.what_it_does:
            st.markdown(f"- {line}")
        st.markdown("**What it is not**")
        for line in overview_view.what_it_is_not:
            st.markdown(f"- {line}")
    with right:
        _scenario_card(run, overview_view)

    st.divider()
    st.markdown("**The four horizons, at a glance**")
    _horizon_strip(run)

    st.divider()
    st.markdown("**How a judge should walk this demo**")
    for step in overview_view.journey:
        st.markdown(
            f"**{step.get('step', '')}.** {step.get('what', '')} "
            f"<span style='color:#6b7785'>{step.get('where', '')}</span>",
            unsafe_allow_html=True,
        )

    callout(
        "Three demos, three different reasons for a level",
        "The three headline scenarios are not three flavours of the same thing. "
        "Each isolates one of the three ways a TrailGuard level gets produced: the "
        "soft score alone, a hard rule floor alone, and a compound escalation alone. "
        "The green scenario is the control -- nothing triggers, and the demo still "
        "shows the whole chain.",
        "info",
    )


def _scenario_card(run, view) -> None:
    weather = (
        f"**{view.weather_mode}** -- weather snapshot `{view.weather_snapshot_ref or 'n/a'}`, "
        f"freshness budget {view.freshness_budget_min:.0f} min"
    )
    st.markdown(
        f'<div style="border:1px solid #e2e6ea;border-radius:8px;padding:14px 16px">'
        f'<div style="font-size:12px;color:#6b7785;letter-spacing:.06em;'
        f'text-transform:uppercase">scenario</div>'
        f'<div style="font-size:19px;font-weight:700;color:#1c2530">{panels._esc(view.scenario_id)}</div>'
        f'<div style="font-size:14px;color:#4a5563;margin:4px 0 10px">{panels._esc(view.description)}</div>'
        f'<div style="font-size:13.5px;color:#1c2530">{weather}</div>'
        f'<div style="font-size:13.5px;color:#1c2530;margin-top:4px">'
        f'<strong>Rule pack:</strong> {panels._esc(view.rule_pack)}</div>'
        f'<div style="font-size:13.5px;color:#1c2530;margin-top:4px">'
        f'<strong>Sunset:</strong> {panels._esc(run.sunset_time)} &#183; '
        f'<strong>analysis:</strong> {panels._esc(run.analysis_time)}</div>'
        f'</div>',
        unsafe_allow_html=True,
    )
    st.caption(view.simulation_note)


def _horizon_strip(run) -> None:
    columns = st.columns(len(run.rows))
    for column, row in zip(columns, run.rows):
        style = LEVEL_STYLE.get(row.risk_level, LEVEL_STYLE["GREEN"])
        with column:
            st.markdown(
                f'<div style="border-top:5px solid {style["colour"]};background:#fafbfc;'
                f'padding:10px 12px;border-radius:0 0 6px 6px;min-height:132px">'
                f'<div style="font-size:12.5px;color:#6b7785">{panels._esc(row.label)}</div>'
                f'<div style="font-size:20px;font-weight:800;color:{style["colour"]}">'
                f'{panels._esc(row.risk_level)}</div>'
                f'<div style="font-size:12px;color:#4a5563">{panels._esc(style["badge"])}</div>'
                f'<div style="font-size:12px;color:#4a5563;margin-top:6px">'
                f'arrival {panels._esc(row.projected_arrival_time)}</div>'
                f'<div style="font-size:12px;color:#4a5563">'
                f'{(row.projected_route_progress * 100):.0f}% of the route &#183; '
                f'{row.projected_remaining_distance_km:.1f} km to go</div>'
                f'<div style="font-size:12px;color:#4a5563">'
                f'gust {row.weather.wind_gust_kmh:.0f} km/h &#183; '
                f'rain {row.weather.precipitation_probability * 100:.0f}%</div>'
                f'</div>',
                unsafe_allow_html=True,
            )


# --------------------------------------------------------------------------- #
# 2. Risk Timeline -- and the agent switch
# --------------------------------------------------------------------------- #


def risk_timeline(run, timeline, *, rule_catalog: dict, agent_state) -> None:
    mode = st.radio(
        "Who reads the five tools?",
        ("Deterministic core (no model in the loop)", "Local Qwen agent (real model, real protocol)"),
        horizontal=True,
        help="Both drive the same five atomic tools over the same frozen scenario. "
        "The core is deterministic and always available; the agent is a real local "
        "model and may be unavailable.",
    )

    if mode.startswith("Local"):
        _agent_section(run, agent_state)
        st.divider()

    st.markdown(f"### {timeline.story}")
    st.caption(timeline.score_note)
    panels.level_timeline(timeline, run)

    if timeline.transitions:
        st.markdown("**Where the level changes**")
        for transition in timeline.transitions:
            st.markdown(f"- {transition}")

    st.divider()
    st.markdown("### Each horizon, and why its level is what it is")
    chosen = st.selectbox(
        "Horizon",
        [point.label for point in timeline.points],
        index=len(timeline.points) - 1,
    )
    point = next(p for p in timeline.points if p.label == chosen)
    row = next(r for r in run.rows if r.label == chosen)

    _horizon_detail(point, row, rule_catalog)
    st.divider()
    st.markdown("### The route, and where the hiker will be")
    panels.route_panel(agent_state["route"])
    st.divider()
    st.markdown("### The weather at that projected place and time")
    _weather_panel(run, rule_catalog)


def _horizon_detail(point, row, rule_catalog) -> None:
    style = LEVEL_STYLE.get(point.level, LEVEL_STYLE["GREEN"])
    st.markdown(
        f'<div style="display:flex;gap:14px;align-items:baseline;flex-wrap:wrap">'
        f'<span style="font-size:24px;font-weight:800;color:{style["colour"]}">'
        f'{panels._esc(point.label)} &#183; {panels._esc(point.level)}</span>'
        f'{panels.badge(point.level, point.badge)}'
        f'<span style="color:#6b7785;font-size:14px">soft score {point.score}</span>'
        f'<span style="color:#6b7785;font-size:14px">confidence {row.confidence:.2f}</span>'
        f'</div>',
        unsafe_allow_html=True,
    )
    st.markdown(f"**{point.headline}**")

    left, right = st.columns([1, 1])
    with left:
        st.markdown("**How the level was assembled**")
        panels.assembly(point)
    with right:
        st.markdown("**Where the numbers came from**")
        st.dataframe(
            [
                {"quantity": "projected position", "value": f"{row.projected_position_lat:.6f}, {row.projected_position_lon:.6f}"},
                {"quantity": "projected altitude", "value": f"{row.projected_alt_m:.0f} m" if row.projected_alt_m is not None else "not reported"},
                {"quantity": "route progress", "value": f"{row.projected_route_progress * 100:.1f} %"},
                {"quantity": "remaining distance", "value": f"{row.projected_remaining_distance_km:.2f} km"},
                {"quantity": "projection confidence", "value": f"{row.projection_confidence:.2f}"},
                {"quantity": "minutes to sunset", "value": f"{row.minutes_to_sunset:.1f}"},
                {"quantity": "minutes to finish", "value": f"{row.estimated_minutes_to_finish:.1f}"},
                {"quantity": "time margin", "value": f"{row.safety_margins.get('time_margin_min')}"},
                {"quantity": "projected battery", "value": f"{row.safety_margins.get('projected_battery_percent')}"},
                {"quantity": "route deviation", "value": f"{row.safety_margins.get('route_deviation_m')}"},
                {"quantity": "weather freshness", "value": f"{row.weather.freshness_minutes:.0f} min old"},
            ],
            hide_index=True,
            width="stretch",
        )
        if row.evidence_refs:
            st.caption("evidence refs: " + ", ".join(row.evidence_refs))

    st.markdown("**Rules that fired at this horizon**")
    panels.driver_table(row.drivers, rule_catalog)


def _weather_panel(run, rule_catalog) -> None:
    rows = weather_rows(run)
    table = [
        {
            "horizon": row.label,
            "arrival": row.arrival_time,
            "gust": f"{row.wind_gust_kmh:.0f} km/h",
            "wind": f"{row.wind_speed_kmh:.0f} km/h",
            "rain": f"{row.precipitation_probability * 100:.0f} %",
            "temperature": f"{row.temperature_c:.1f} C",
            "weather code": row.weather_code,
            "forecast generated": row.forecast_generated_at,
            "reading age": f"{row.freshness_minutes:.0f} min",
            "within budget": ("yes" if row.freshness_ok else "no") if row.freshness_ok is not None else "not stated",
        }
        for row in rows
    ]
    st.dataframe(table, hide_index=True, width="stretch")
    st.caption(
        "Every weather figure is for the horizon's *projected* position at its "
        "*projected* arrival time -- not the weather where the hiker started. The "
        "threshold notes beside each column come from rule R-C's own frozen thresholds."
    )
    for row in rows:
        notes = row.gust_threshold_notes + row.precip_threshold_notes + row.cold_threshold_notes
        if notes:
            st.markdown(f"**{row.label}** -- " + "; ".join(notes))


def _agent_section(run, agent_state) -> None:
    """The Local Qwen layer, or the sentence that says why there is not one.

    Three states, in the order a judge should meet them: the endpoint is up and
    nothing has run yet; the endpoint is up and a run exists; the endpoint is down
    and nothing will be improvised. The third is the one the brief cares about --
    the demo says "Local Agent unavailable" and stops, because the strongest part
    of the presentation is that a real model is really calling the tools, and a
    scripted stand-in would turn that into the weakest part.
    """
    availability = agent_state["availability"]
    if not availability.available:
        panels.unavailable(
            "Local Agent unavailable",
            availability.demo_screen_reason,
            "",
            "start the vLLM server, then rerun scripts/demo_preflight.py",
        )
        st.caption(
            "Everything else in this demo -- the verdict, the timeline, the route, the "
            "weather, the evidence and the benchmark -- comes from the same "
            "deterministic chain and does not depend on the model."
        )
        return

    view = agent_state["view"]
    error = agent_state["error"]

    if view is None:
        _agent_trigger(run, agent_state, error)
        return

    # Who produced this, and which half of it is not theirs. The brief's rule: the
    # verdict is the core's, the prose is the model's, and the screen must not let
    # a viewer confuse the two.
    st.markdown(
        f"**Model:** {panels._esc(view.model_name)} (via {panels._esc(view.provider)}) "
        f"&#183; **Skill loaded:** "
        f"{panels._esc(view.skill_version or 'no') if view.skill_loaded else 'no'}"
    )
    st.caption(
        "The risk levels, rule triggers and margins on this screen were computed by "
        "the deterministic safety core. Everything in the answer below is the local "
        "model's own explanation of what it did, and nothing in it changes a level."
    )

    left, right = st.columns([1, 1])
    with left:
        st.metric("stop reason", view.stop_reason_text)
        st.metric("tool calls made", len(view.calls))
        st.metric("horizons the agent checked", len(view.horizons_checked))
        st.metric("wall clock", view.timing_note[0] if view.timing_note else "not recorded")
    with right:
        st.metric("core's worst verdict", view.core_risk_level or "not recorded")
        st.metric("agent's worst claim", view.worst_claimed or "no readable claim")
        st.metric(
            "verdict",
            "contradicted the core" if view.contradicted else "did not contradict the core",
        )

    st.caption(view.contradiction_note)

    # The one place the two "core" numbers on this screen could be mistaken for
    # each other, so it is said out loud. The agent's core verdict is the worst
    # level among the horizons that agent chose to assess; the headline at the top
    # of this tab is the core's worst over all four. When the agent checks one
    # horizon the two differ, and that gap is a finding rather than a defect to
    # smooth over.
    if view.horizons_checked and view.core_risk_level:
        st.caption(
            f"'Core' above is the worst level the core returned to the agent for the "
            f"{len(view.horizons_checked)} horizon(s) it assessed. The headline at the "
            "top of this tab is the core's worst over all four horizons, which is the "
            "verdict the safety core actually owns."
        )

    if view.stop_reason != "completed":
        panels.callout(
            f"This run stopped: {view.stop_reason}",
            view.stop_reason_text.capitalize()
            + ". A stopped run is a result and is shown as one -- Phase 5 recorded the "
            "same thing happening there, which is why the benchmark has a metric for it.",
            "warn",
        )

    st.markdown("**Every tool call the agent made**")
    st.dataframe(
        [
            {
                "seq": call.seq,
                "tool": call.tool,
                "arguments": call.arguments,
                "status": call.status,
                "summary": call.result_summary,
                "error": call.error or "",
            }
            for call in view.calls
        ],
        hide_index=True,
        width="stretch",
    )

    st.markdown("**The agent's answer, verbatim**")
    panels.model_answer(view.answer)

    st.markdown("**What the frozen scanner read in that answer**")
    if view.claimed_levels:
        st.dataframe(
            [
                {"horizon": horizon, "level the scanner read": level}
                for horizon, level in view.claimed_levels.items()
            ],
            hide_index=True,
            width="stretch",
        )
    else:
        st.markdown(
            "_The scanner read no horizon-level claim in this answer, so no comparison "
            "was made. Nothing is implied either way._"
        )

    if view.hedges or view.negated or view.conflicting:
        st.markdown("**Ambiguities the scanner flagged**")
        st.dataframe(
            [
                {"kind": "hedged", "where": ", ".join(value) or "—"}
                for value in [view.hedges]
            ]
            + [
                {"kind": "negated", "where": ", ".join(view.negated) or "—"}
                for _ in [0]
            ]
            + [
                {"kind": "conflicting horizons", "where": ", ".join(view.conflicting) or "—"}
                for _ in [0]
            ]
            + [
                {"kind": "probability of harm claimed", "where": p}
                for p in view.probability_claims
            ],
            hide_index=True,
            width="stretch",
        )

    st.caption(
        "The claim reading is Phase 5's own scanner, imported rather than "
        "reimplemented -- so a contradiction flagged here is the same flag the "
        "benchmark's M12 metric counted."
    )

    if st.button("Run the agent again"):
        _agent_trigger(run, agent_state, error, rerun=True)


def _agent_trigger(run, agent_state, error: str | None, *, rerun: bool = False) -> None:
    """The button that makes a real model call, and the failure it may produce."""
    if error:
        panels.unavailable(
            "The agent run failed",
            error,
            "A model or transport failure is a result, not a reason to substitute a "
            "canned answer. The deterministic verdict above is unaffected.",
            "check the local endpoint, then try again",
        )

    st.markdown(f"**Model:** {panels._esc(agent_state['availability'].model)}")
    st.caption(
        f"Sampling is frozen at temperature {agent_state['sampling']['temperature']}, "
        f"max_tokens {agent_state['sampling']['max_tokens']} and "
        f"max_steps {agent_state['sampling']['max_steps']} -- the settings Phase 5 "
        "froze, so a run here is comparable to one there."
    )
    if st.button("Run the agent again" if rerun else f"Run the agent on {run.scenario_id}"):
        with st.spinner("The agent is calling the five tools. Nothing is scripted."):
            agent_state["runner"]()
        st.rerun()


# --------------------------------------------------------------------------- #
# 3. Evidence
# --------------------------------------------------------------------------- #


def evidence(evidence_view, timeline) -> None:
    st.markdown("### Every number on the screen hashes to a file that was evaluated")
    st.markdown(
        "The five inputs below are the exact files the Phase 5 benchmark hashed. "
        "The demo re-reads and re-hashes them on every load; it never writes to them, "
        "and it never copies a cached answer into place."
    )
    panels.artifact_table(evidence_view)

    callout(
        "Artifact provenance",
        " | ".join(evidence_view.provenance) if evidence_view.provenance else "not recorded",
        "info",
    )

    st.divider()
    st.markdown("### The chain, horizon by horizon")
    st.markdown(
        "Each row is one horizon's full path: the projection, the weather it used, "
        "the verdict that came out, and every safety margin the core computed. "
        "Nothing here is reformatted beyond display."
    )
    panels.chain_table(evidence_view)

    if evidence_view.gaps:
        st.markdown("**What this demo does not know**")
        for gap in evidence_view.gaps:
            st.markdown(f"- {gap}")
    else:
        callout(
            "No gaps",
            "Every input this run needed was present, fresh and inside its budget.",
            "ok",
        )

    # Brief 26, in both halves at once: the default is a table and not a 5 KB dump,
    # and the raw bytes are still one click away. What is behind the disclosure is
    # the frozen Phase 5 reference for this scenario -- the file the benchmark
    # hashed -- so a judge who wants the source reads the evidence rather than a
    # reconstruction of it.
    if evidence_view.raw_reference:
        with st.expander("View raw JSON — the frozen Phase 5 reference for this scenario"):
            st.markdown(fenced(evidence_view.raw_reference, "json"))

    st.divider()
    st.markdown("### The rule pack behind every verdict")
    #: Every rule id that fired anywhere in this run, so the table can say which
    #: of the six a judge is actually looking at.
    fired = {rule_id for point in timeline.points for rule_id in point.driver_ids}
    st.dataframe(
        [
            {
                "rule": row["rule"],
                "name": row.get("name", ""),
                "kind": row.get("kind", ""),
                "severity": row.get("severity", ""),
                "condition": row.get("condition", ""),
                "frozen thresholds": row.get("thresholds", ""),
                "provenance": row.get("provenance", ""),
                "demo calibration": row.get("demo calibration", ""),
                "version": row.get("version", ""),
                "fired in this run": "yes" if row["rule"] in fired else "no",
            }
            for row in evidence_view.rule_catalog
        ],
        hide_index=True,
        width="stretch",
    )


# --------------------------------------------------------------------------- #
# 4. Benchmark
# --------------------------------------------------------------------------- #


def benchmark(view, *, available: bool) -> None:
    if not available:
        panels.unavailable(
            "Phase 5 benchmark evidence is not on this machine",
            "The four files under docs/phase5_evidence/ that this tab reads are absent.",
            "Nothing here can be reconstructed -- the numbers were produced by a "
            "benchmark run, not by this screen.",
            "restore docs/phase5_evidence/ from the Phase 5 commit",
        )
        return

    st.markdown("### Phase 5, as it was actually recorded")
    st.markdown(
        f"`{view.runs_total}` runs, `{view.runs_scored}` scored and `{view.runs_excluded}` "
        f"excluded, `{view.paired_rows}` paired comparisons, `{view.manual_review_rows}` rows "
        f"sent to manual review. Read from the committed evidence files and recomputed "
        "nowhere."
    )
    callout(
        "The finding was MIXED, and the mixed result is the point",
        f"{len(view.improved)} metrics moved the way their own direction calls better, "
        f"{len(view.worsened)} moved the wrong way, and {len(view.unchanged)} did not move. "
        "There is no composite score, no net score and no 'overall', because the evidence "
        "itself records that none exists. Both sides are on this screen together on purpose: "
        "a project that only shows its wins is a project a judge cannot check.",
        "warn",
    )
    # The sentence this tab exists in order to say. The brief's narrative boundary is
    # exact: the finding is written as a change in the agent's orchestration behaviour,
    # never as a claim that the model became safer, and the cost half of it rides in
    # the same paragraph rather than in a footnote a judge will not read. The wording
    # is the brief's own, kept verbatim so a reviewer can diff it against the sentence
    # they approved.
    st.markdown(
        "TrailGuard changes the agent's orchestration behavior and increases proactive "
        "future-risk investigation in several benchmark dimensions. On the current local "
        "30B model and 16K context window, the longer orchestration chain also introduces "
        "substantial reliability and context-budget costs."
    )

    # Brief 28 names this one number by name, in percentages, so it gets its own line
    # rather than a column a judge has to find. It is the same arithmetic as the
    # column below it -- two sums and two run counts, all four already on screen --
    # and it is stated with its numerator and denominator beside it, the way Phase 5's
    # own report states it, so a reader can check the percentage without trusting it.
    completion = view.completion_rate
    if completion is not None and completion.rate_text:
        st.markdown(
            f"**Completion rate.** Without the skill, "
            f"{completion.sum_baseline:g} of {completion.n_runs_baseline} runs "
            f"({completion.baseline_rate}) finished with a real answer; with the skill, "
            f"{completion.sum_treatment:g} of {completion.n_runs_treatment} "
            f"({completion.treatment_rate})."
        )

    left, middle, right = st.columns(3)
    with left:
        st.metric("moved the right way", len(view.improved))
        st.caption(", ".join(m.metric_id for m in view.improved))
    with middle:
        st.metric("moved the wrong way", len(view.worsened))
        st.caption(", ".join(m.metric_id for m in view.worsened))
    with right:
        st.metric("did not move", len(view.unchanged))
        st.caption(", ".join(m.metric_id for m in view.unchanged))

    st.divider()
    svg(
        charts.metric_chart(view),
        "One bar per metric: the mean paired delta exactly as the evidence recorded it, "
        "in the direction each metric calls better. Red bars are the four Phase 5 "
        "regressions, shown at full length.",
    )

    st.divider()
    st.markdown("### All eighteen frozen metrics, both conditions beside each")
    panels.metric_table(view)

    st.divider()
    st.markdown("### Which runs were excluded, and why")
    st.dataframe(
        [{"run": exclusion.run_id, "reason": exclusion.reason} for exclusion in view.exclusions],
        hide_index=True,
        width="stretch",
    )

    st.markdown("**Where each number came from**")
    st.dataframe(
        [{"file": name, "role": role} for name, role in view.source_files.items()],
        hide_index=True,
        width="stretch",
    )
    for note in view.trace_notes:
        st.caption(note)


# --------------------------------------------------------------------------- #
# 5. Architecture
# --------------------------------------------------------------------------- #


def architecture(run, *, rule_catalog: dict, skill_sha256: str | None,
                 streamlit_version: str | None, agent_available: bool) -> None:
    st.markdown("### Five tools, one skill, one deterministic core")
    st.markdown(
        "A model -- or the demo itself -- drives exactly five atomic tools. No tool "
        "answers more than one question, and there is deliberately no "
        "``forecast_risk`` that would answer all four horizons in a single call. "
        "The 30/60/120 loop belongs to the Skill, so the loop is auditable and the "
        "tools stay small."
    )
    st.dataframe(
        [
            {"#": index + 1, "tool": name, "question it answers": question, "what it returns": returns}
            for index, (name, question, returns) in enumerate(TOOL_CHAIN)
        ],
        hide_index=True,
        width="stretch",
    )

    st.divider()
    st.markdown("### A level is assembled, not scored")
    st.markdown(
        "Three and only three things can set a risk level, and the demo shows all three "
        "for every horizon. The soft score is a count-and-weight bookkeeping sum with a "
        "ceiling of 100 -- it is **not** a probability of harm, and the demo never "
        "presents it as one."
    )
    for step in (
        "1. **Soft-score band** -- the weighted sum of the soft rules that fired, mapped to GREEN / YELLOW / ORANGE / RED.",
        "2. **Compound escalation** -- R-F lifts the band by one level when two or more independent soft rules fire at MEDIUM or above, clamped at RED.",
        "3. **Hard-rule floor** -- any triggered hard rule imposes its own floor from its severity, and the largest of the three wins.",
    ):
        st.markdown(step)

    st.divider()
    st.markdown("### What is frozen, and therefore what the demo could not have changed")
    st.dataframe(
        [{"frozen asset or decision": line} for line in FROZEN],
        hide_index=True,
        width="stretch",
    )

    st.divider()
    st.markdown("### The failure philosophy, because this demo is designed to fail visibly")
    for title, body in (
        (
            "No silent fallback",
            "If the local model is down, the demo says 'Local Agent unavailable' and "
            "stops. There is no scripted provider waiting behind it, because the whole "
            "value of Agent Mode is that a real model is really calling the tools.",
        ),
        (
            "A stopped run is a result",
            "Running out of steps, hitting the context window, or a provider error are "
            "recorded with the tool calls that did happen, and shown. Phase 5 measured "
            "these as metrics rather than hiding them.",
        ),
        (
            "A failure is a sentence",
            "No screen in this demo renders a traceback, an exception object, a host "
            "address or an absolute path.",
        ),
        (
            "Live weather is opt-in and labelled",
            "Replay is the default and is labelled as replay on every screen that uses "
            "it. Switching to live weather is an explicit action; if it fails, the demo "
            "reports the failure and waits for the viewer rather than switching back on "
            "its own.",
        ),
    ):
        st.markdown(f"**{title}.** {body}")

    st.divider()
    st.markdown("### What this run actually used")
    st.dataframe(
        [
            {"item": "rule pack", "value": f"{run.rule_pack_id} @ {run.rule_pack_version}"},
            {"item": "weather mode", "value": run.weather_mode},
            {"item": "weather snapshot", "value": run.weather_snapshot_ref or "live"},
            {"item": "SKILL.md sha256", "value": (skill_sha256 or "not available")[:32]},
            {"item": "local agent", "value": "available" if agent_available else "unavailable"},
            {"item": "UI", "value": f"Streamlit {streamlit_version}" if streamlit_version else "not installed"},
        ],
        hide_index=True,
        width="stretch",
    )
