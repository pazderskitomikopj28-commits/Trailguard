"""The small pieces the tabs are assembled from: badges, verdicts, tables and the
failure box that is the whole point of this demo's honesty.

Two rules are enforced here rather than described:

**Nothing a model produced is ever rendered as HTML.** A model's answer is
untrusted input, so it goes through :func:`fenced`, which wraps it in a code fence
longer than any backtick run inside it. Streamlit does not interpret markdown or
HTML inside a fenced block, and a fence that cannot be closed from the inside
cannot be used to break out of.

**Nothing renders a traceback.** Every failure is a sentence the viewer can act
on, produced by the module that raised it, and every caller that can fail catches
and re-renders. ``st.exception`` exists; this file never calls it.
"""

from __future__ import annotations

from typing import Iterable

import streamlit as st

from demo.presenters.timeline import LEVEL_STYLE
from demo.streamlit.components import charts
from demo.streamlit.components import text as textbox
from demo.streamlit.components.charts import GOOD, MUTED


def _esc(value: object) -> str:
    """Escaping for SVG attributes and inline styles."""
    return textbox.escape(value)


# --------------------------------------------------------------------------- #
# Badges and verdicts
# --------------------------------------------------------------------------- #

_BADGE_CSS = (
    "display:inline-block;padding:2px 10px;border-radius:11px;font-size:12px;"
    "font-weight:700;letter-spacing:.04em;color:#fff;vertical-align:middle"
)


def badge(level: str, label: str | None = None) -> str:
    """A level as a coloured chip. ``label`` is the words shown beside the colour,
    which are the level's own name rather than a rewording of it."""
    style = LEVEL_STYLE.get(level, LEVEL_STYLE["GREEN"])
    return (
        f'<span style="{_BADGE_CSS};background:{style["colour"]}">{_esc(label or level)}</span>'
    )


def level_chip(level: str) -> str:
    style = LEVEL_STYLE.get(level, LEVEL_STYLE["GREEN"])
    return (
        f'<span style="{_BADGE_CSS};background:{style["colour"]}">'
        f'{_esc(level)} &#183; {_esc(style["badge"])}</span>'
    )


def verdict(run) -> None:
    """The one thing a judge must see in the first second: the worst level, its
    label, and the fact that the level came from a rule rather than a vibe.

    ``run`` is a ``ReplayRun``; the level is the core's, not the UI's, and the
    sentence quoting the hazard is the core's own driver detail.
    """
    style = LEVEL_STYLE.get(run.worst_level, LEVEL_STYLE["GREEN"])
    hazard = run.hazard_labels
    detail = f' <span style="opacity:.75">&#183; triggered by {_esc(", ".join(hazard))}</span>' if hazard else ""
    st.markdown(
        f'<div style="border-left:6px solid {style["colour"]};background:{style["colour"]}14;'
        f'padding:14px 18px;border-radius:6px;margin:6px 0 14px">'
        f'<div style="font-size:13px;color:#6b7785;letter-spacing:.08em;'
        f'text-transform:uppercase">worst level across the next two hours</div>'
        f'<div style="font-size:34px;font-weight:800;color:{style["colour"]};line-height:1.15">'
        f'{_esc(run.worst_level)} &#183; {_esc(style["badge"])}</div>'
        f'<div style="font-size:14px;color:#1c2530">{detail}</div>'
        f'</div>',
        unsafe_allow_html=True,
    )


# --------------------------------------------------------------------------- #
# The honesty box
# --------------------------------------------------------------------------- #

_KINDS = {
    "unavailable": ("#b00020", "#fdf2f4", "Unavailable"),
    "warn": ("#b8860b", "#fdf8ec", "Worth knowing"),
    "info": ("#2f6fa8", "#f2f7fb", "Note"),
    "ok": ("#1b7f3b", "#f1f8f3", "Confirmed"),
}


def callout(title: str, body: str, kind: str = "info", fix: str = "") -> None:
    """A bordered box. Used for every disclosure, and for every failure.

    The ``unavailable`` kind is the one that keeps the demo honest when the local
    model is down: it names what is missing, says what the demo will *not* do
    about it, and — where one exists — the one command that would fix it. It never
    says "retrying" and never swaps in a stand-in.
    """
    ink, wash, label = _KINDS.get(kind, _KINDS["info"])
    fix_line = (
        f'<div style="margin-top:8px;font-size:13px;color:#1c2530">'
        f'<strong>What to do:</strong> <code>{_esc(fix)}</code></div>'
        if fix
        else ""
    )
    st.markdown(
        f'<div style="border-left:5px solid {ink};background:{wash};padding:12px 16px;'
        f'border-radius:6px;margin:10px 0">'
        f'<div style="font-size:12px;font-weight:700;color:{ink};letter-spacing:.06em;'
        f'text-transform:uppercase">{_esc(label)} &#183; {_esc(title)}</div>'
        f'<div style="font-size:14.5px;color:#1c2530;margin-top:4px">{_esc(body)}</div>'
        f'{fix_line}</div>',
        unsafe_allow_html=True,
    )


def unavailable(title: str, reason: str, detail: str = "", fix: str = "") -> None:
    """The "Local Agent unavailable" box, in the wording the spec fixes."""
    # A reason is a sentence, and the refusal that follows it is another one, so the
    # two need a full stop between them. With an empty detail -- which is the case for
    # every box the agent panel draws when the model is down -- ``body`` is the bare
    # reason, and without this punctuation the screen reads "...is not reachable The
    # demo will not substitute...", which is the first thing a judge reads on failure.
    body = f"{reason}." if not detail else f"{reason}. {detail}"
    callout(
        title,
        f"{body} The demo will not substitute a scripted answer, and nothing else on "
        "screen depends on it.",
        "unavailable",
        fix,
    )


# --------------------------------------------------------------------------- #
# Untrusted text
# --------------------------------------------------------------------------- #


def model_answer(answer: str) -> None:
    """The agent's answer, verbatim and inert (see :func:`text.fenced`)."""
    st.markdown(textbox.fenced(answer))


# --------------------------------------------------------------------------- #
# Tables
# --------------------------------------------------------------------------- #


def assembly(point) -> None:
    """Why this horizon's level is what it is, in the core's own three steps.

    Rendered as three lines rather than a table because there are exactly three
    sources of a level and a judge should be able to count them.
    """
    for step in point.assembly:
        colour = GOOD if "no effect" not in step.outcome else MUTED
        marker = "&#9654;" if "no effect" not in step.outcome else "&#9679;"
        st.markdown(
            f'<div style="padding:7px 0;border-bottom:1px solid #eef1f4">'
            f'<span style="color:{colour};font-weight:700">{marker}</span> '
            f'<strong>{_esc(step.source)}</strong> &#8594; <strong>{_esc(step.outcome)}</strong>'
            f'<div style="font-size:13.5px;color:#4a5563;margin-left:18px">'
            f'{_esc(step.detail)}</div></div>',
            unsafe_allow_html=True,
        )


def driver_table(drivers: Iterable[dict], rule_catalog: dict) -> None:
    """The drivers of one horizon, each with the pack's own definition beside it.

    The ``detail`` column is the core's own sentence, copied character for
    character -- it is the only place the observed value and the threshold both
    appear, because ``RuleDriver`` has no structured field for either.
    """
    rows = []
    for driver in drivers:
        rule_id = driver.get("rule_id", "?")
        meta = rule_catalog.get(rule_id, {})
        rows.append(
            {
                "rule": rule_id,
                "name": meta.get("name", ""),
                "kind": driver.get("type", ""),
                "severity": meta.get("severity", ""),
                "what the core measured": driver.get("detail", ""),
                "why it is a rule": meta.get("condition", ""),
            }
        )
    if not rows:
        st.markdown("_No rule triggered at this horizon._")
        return
    st.dataframe(rows, hide_index=True, width="stretch")


def metric_table(view) -> None:
    """Every frozen metric, with both conditions beside it.

    Both sums and both counts are shown even when one is zero or null, because a
    metric that moved by a lot from a small denominator is a different claim from
    one that moved a little from a large one, and the difference is visible only
    if the denominators stay on screen. The rate column is the only derived thing
    on this screen, it is populated only where the evidence's own denominator makes
    a rate meaningful, and it sits beside the two sums it is arithmetic on rather
    than replacing them.
    """
    rows = []
    for metric in view.metrics:
        rows.append(
            {
                "metric": metric.metric_id,
                "what it counts": metric.definition,
                "direction": metric.direction_text,
                "baseline": metric.sum_baseline,
                "treatment": metric.sum_treatment,
                "rate of its own runs": metric.rate_text or "--",
                "mean paired delta": metric.mean_paired_delta,
                "pairs": metric.n_paired_deltas,
                "baseline runs": metric.n_runs_baseline,
                "treatment runs": metric.n_runs_treatment,
                "manual review": "yes" if metric.manual_review else "no",
            }
        )
    st.dataframe(rows, hide_index=True, width="stretch")


def artifact_table(evidence) -> None:
    """Every artifact behind one run, each with the hash that was evaluated.

    Six rows: the two Safety Core files and the four inputs the run consumed. The
    rule and calibration packs come first because they are what make the other four
    mean anything -- a scenario file proves what was fed in, and the rule pack proves
    what decided the level. All six are checked against the hash the benchmark
    recorded, so a judge reads "matches" six times or learns that something moved.
    """
    rows = []
    for artifact in [evidence.rule_pack, evidence.calibration] + evidence.artifacts:
        rows.append(
            {
                "what it is": artifact.role,
                "file": artifact.path,
                "sha256 (first 16)": (artifact.sha256 or "")[:16],
                "matches the evaluated hash": "yes" if "DIFFERS" not in (artifact.note or "") else "no",
                "note": artifact.note,
            }
        )
    st.dataframe(rows, hide_index=True, width="stretch")


def chain_table(evidence) -> None:
    """One row per horizon: the projection, the weather, the verdict and the margins,
    each as the tool that produced it returned them."""
    rows = []
    for row in evidence.chain:
        verdict = row.verdict or {}
        rows.append(
            {
                "horizon": row.horizon,
                "arrival": row.arrival_time,
                "projection": row.projection,
                "weather used": row.weather,
                "level": verdict.get("level"),
                "confidence": verdict.get("confidence"),
                "margins": row.margins,
                "evidence refs": row.evidence_refs,
            }
        )
    st.dataframe(rows, hide_index=True, width="stretch")


# --------------------------------------------------------------------------- #
# Charts, through a single door so every rendering decision is in one place
# --------------------------------------------------------------------------- #


def svg(markup: str, caption: str | None = None) -> None:
    """Draw an SVG, optionally with a caption that says what the picture is."""
    if caption:
        st.markdown(
            f'<div style="font-size:13px;color:#6b7785;margin:2px 0 6px">{_esc(caption)}</div>',
            unsafe_allow_html=True,
        )
    st.markdown(markup, unsafe_allow_html=True)


def level_timeline(timeline, run) -> None:
    notes = [row.projected_arrival_time for row in run.rows]
    svg(
        charts.level_chart(timeline, notes),
        "The solid line is the level the core reported. The dashed line is the level "
        "the soft score alone would have given. Where they differ, a hard rule or a "
        "compound rule moved the level, and the table below says which.",
    )


def route_panel(profile) -> None:
    left, right = st.columns([1.05, 1.5])
    with left:
        svg(
            charts.route_sketch(profile),
            "The track, north up. Each circle is where the projection puts the hiker "
            "when that horizon's verdict applies.",
        )
    with right:
        svg(
            charts.elevation_chart(profile),
            "Altitude against distance, from the GPX itself. The dashed lines are the "
            "same four projected positions.",
        )
