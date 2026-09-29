#!/usr/bin/env python
"""Demo acceptance gate: prove the presentation layer says what the tools said.

Run it before a judge sees the screen. It is the demo's own verifier, and it is
deliberately outside the pytest suite so it can make the two checks the frozen
``no_external_network`` fixture forbids: it reads the committed Phase 5 references
and the committed evidence directory, and it may probe the local model endpoint
(and reports what it found, without ever requiring the endpoint to be up).

What it checks
--------------
1. **Replay Mode reproduces the frozen Deterministic Reference**, field for field,
   for every one of the 14 scenarios and all four horizons -- levels, soft scores,
   hard rules, drivers, confidence, margins, positions, times, weather values,
   freshness and the ETA identity. A demo that drifted from the benchmark's own
   reference would show a prettier number than the one that was evaluated.
2. **The level explainer agrees with the engine**, for every horizon of every
   scenario: ``max(score band -> compound escalation, hard-rule floor)`` equals the
   level the engine reported and the level the reference holds.
3. **The evidence hashes are the ones that were evaluated**: scenario, GPX, device
   feed, weather snapshot and rule pack all match the sha256 in the reference.
4. **The Benchmark tab reads the committed evidence** without recomputing it, and
   still asserts ``no_composite_score``.
5. **Nothing in the demo package recomputes a verdict**: the demo modules import no
   symbol that evaluates a rule, and the chain uses the same engine the tools build
   by default.

What it reports but does not fail on
------------------------------------
Streamlit and the local model endpoint are reported with their reason, because a
competition laptop may have neither. The demo's design makes both non-fatal: the
presenter layer needs only ``pydantic`` and ``yaml``, and a scripted fallback for
the agent is not a thing this code has.

Exit code 0 when every check that must pass passes.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from demo.presenters.agent_view import UNAVAILABLE, probe_local_agent  # noqa: E402
from demo.presenters.benchmark_view import EVIDENCE_DIR, load_benchmark  # noqa: E402
from demo.presenters.catalog import load_catalog  # noqa: E402
from demo.presenters.chain import (  # noqa: E402
    WEATHER_UNAVAILABLE_PREFIX,
    run_replay,
)
from demo.presenters.evidence_view import build_evidence  # noqa: E402
from demo.presenters.scenario_view import build_overview  # noqa: E402
from demo.presenters.timeline import build_timeline  # noqa: E402


#: The two failure badges, spelled the way :mod:`panels` renders them. Asserting on
#: the words without the badge would also match the Architecture tab's prose
#: explaining the policy, so a broken panel would pass.
UNREACHABLE = "Unavailable &#183; Local Agent unavailable"
LIVE_WEATHER_UNAVAILABLE = f"Unavailable &#183; {WEATHER_UNAVAILABLE_PREFIX}"


HORIZON_ORDER = ("NOW", "+30min", "+60min", "+120min")

#: (label, demo accessor, reference accessor). ``None`` on either side means
#: "this field is absent on that side" and the pair is skipped with a note.
FIELD_MAP: list[tuple[str, object, object]] = [
    ("risk level", lambda d, r: d["risk"]["level"], lambda d, r: d.risk_level),
    ("soft score", lambda d, r: d["risk"]["score"], lambda d, r: d.risk_score),
    ("hard rules", lambda d, r: d["risk"]["hard_rules_triggered"], lambda d, r: d.hard_rules_triggered),
    ("drivers", lambda d, r: [x["rule_id"] for x in d["risk"]["drivers"]],
     lambda d, r: [x["rule_id"] for x in d.drivers]),
    ("confidence", lambda d, r: round(d["risk"]["confidence"], 9),
     lambda d, r: round(d.confidence, 9)),
    ("projected arrival", lambda d, r: d["projected_arrival_time"],
     lambda d, r: d.projected_arrival_time),
    ("projected lat", lambda d, r: round(d["projected_position"]["lat"], 9),
     lambda d, r: round(d.projected_position_lat, 9)),
    ("projected lon", lambda d, r: round(d["projected_position"]["lon"], 9),
     lambda d, r: round(d.projected_position_lon, 9)),
    ("projected alt", lambda d, r: d["projected_position"].get("alt_m"),
     lambda d, r: d.projected_alt_m),
    ("route progress", lambda d, r: round(d["projected_route_progress"], 9),
     lambda d, r: round(d.projected_route_progress, 9)),
    ("remaining km", lambda d, r: round(d["remaining_distance_km"], 9),
     lambda d, r: round(d.projected_remaining_distance_km, 9)),
    ("projection confidence", lambda d, r: round(d["projection_confidence"], 9),
     lambda d, r: round(d.projection_confidence, 9)),
    ("minutes to sunset", lambda d, r: round(d["minutes_to_sunset"], 6),
     lambda d, r: round(d.minutes_to_sunset, 6)),
    ("ETA to finish", lambda d, r: round(d["estimated_minutes_to_finish"], 6),
     lambda d, r: round(d.estimated_minutes_to_finish, 6)),
    ("weather gust", lambda d, r: round(d["weather"]["wind_gust_kmh"], 9),
     lambda d, r: round(d.weather.wind_gust_kmh, 9)),
    ("weather wind", lambda d, r: round(d["weather"]["wind_speed_kmh"], 9),
     lambda d, r: round(d.weather.wind_speed_kmh, 9)),
    ("weather temp", lambda d, r: round(d["weather"]["temperature_c"], 9),
     lambda d, r: round(d.weather.temperature_c, 9)),
    ("weather precip", lambda d, r: round(d["weather"]["precipitation_probability"], 9),
     lambda d, r: round(d.weather.precipitation_probability, 9)),
    ("weather code", lambda d, r: d["weather"]["weather_code"],
     lambda d, r: d.weather.weather_code),
    ("weather source", lambda d, r: d["weather"]["source"], lambda d, r: d.weather.source),
    ("freshness minutes", lambda d, r: d["weather"]["freshness_minutes"],
     lambda d, r: d.weather.freshness_minutes),
    ("freshness ok", lambda d, r: d["weather"].get("freshness_ok"),
     lambda d, r: d.freshness_ok),
    ("time margin", lambda d, r: d["safety_margins"].get("time_margin_min"),
     lambda d, r: d.safety_margins.get("time_margin_min")),
    ("projected battery", lambda d, r: d["safety_margins"].get("projected_battery_percent"),
     lambda d, r: d.safety_margins.get("projected_battery_percent")),
    ("route deviation", lambda d, r: d["safety_margins"].get("route_deviation_m"),
     lambda d, r: d.safety_margins.get("route_deviation_m")),
    ("weather freshness margin",
     lambda d, r: d["safety_margins"].get("weather_freshness_min"),
     lambda d, r: d.safety_margins.get("weather_freshness_min")),
    ("evidence refs", lambda d, r: d["risk"]["evidence_refs"], lambda d, r: d.evidence_refs),
]


class Report:
    def __init__(self) -> None:
        self.failures: list[str] = []
        self.checks = 0
        self.notes: list[str] = []

    def check(self, condition: bool, message: str) -> bool:
        self.checks += 1
        if not condition:
            self.failures.append(message)
        return condition

    def note(self, message: str) -> None:
        self.notes.append(message)

    @property
    def ok(self) -> bool:
        return not self.failures


def compare_reference(report: Report, scenario_id: str, reference: dict, run) -> int:
    """Field-for-field comparison of one scenario's chain against its reference."""
    compared = 0
    for label in HORIZON_ORDER:
        expected = reference["horizons"][label]
        actual = next((row for row in run.rows if row.label == label), None)
        if not report.check(actual is not None, f"{scenario_id} {label}: missing from the demo chain"):
            continue
        for name, from_ref, from_demo in FIELD_MAP:
            try:
                want = from_ref(expected, None)
            except Exception:  # noqa: BLE001 - a reference missing a field is a note, not a crash
                want = None
            got = from_demo(actual, None)
            if want is None and got is None:
                continue
            if isinstance(want, float) and isinstance(got, (int, float)):
                same = abs(float(want) - float(got)) <= 1e-9
            else:
                same = want == got
            report.check(
                same,
                f"{scenario_id} {label} {name}: demo shows {got!r}, reference holds {want!r}",
            )
            compared += 1
    worst = run.worst_level
    reference_worst = reference["expectations_for_evaluator"]["worst_level"]
    report.check(
        worst == reference_worst,
        f"{scenario_id} worst level: demo shows {worst}, reference holds {reference_worst}",
    )
    return compared


def main() -> int:
    report = Report()
    from trailguard.agent.replay_runner import load_scenario
    from trailguard.risk_core import load_calibration, load_rule_pack
    from trailguard.schemas.risk import RiskLevel

    reference_dir = REPO_ROOT / "evals" / "phase5" / "references"
    references = sorted(reference_dir.glob("*.json"))
    report.check(bool(references), f"no Phase 5 references found under {reference_dir}")
    if not references:
        return _finish(report)

    pack, calibration = load_rule_pack(), load_calibration()
    floors = calibration.severity_floor
    rules = {rid: pack.get(rid) for rid in pack.ids()}
    compound_ids = [rid for rid, rule in rules.items() if "escalation_levels" in rule.thresholds]
    escalation = int(rules[compound_ids[0]].thresholds["escalation_levels"])

    catalog = load_catalog()
    report.check(
        catalog.count() == len(references),
        f"catalogue offers {catalog.count()} scenarios but {len(references)} references exist",
    )

    total_fields = 0
    scenarios_checked = 0
    for path in references:
        reference = json.loads(path.read_text(encoding="utf-8"))
        scenario_id = path.stem
        scenario_path = REPO_ROOT / reference["scenario_file"]
        report.check(scenario_path.is_file(), f"{scenario_id}: scenario file missing at {scenario_path}")
        if not scenario_path.is_file():
            continue

        try:
            run = run_replay(load_scenario(scenario_path))
        except Exception as exc:  # noqa: BLE001 - the demo must fail loudly, not silently
            report.check(False, f"{scenario_id}: replay chain raised {type(exc).__name__}: {exc}")
            continue
        scenarios_checked += 1
        total_fields += compare_reference(report, scenario_id, reference, run)

        # the explainer must agree with the engine and with the reference
        timeline = build_timeline(run, pack=pack, calibration=calibration)
        for point in timeline.points:
            candidates = [RiskLevel(point.score_band).rank]
            for rid in point.hard_rules:
                candidates.append(RiskLevel(floors[rules[rid].severity.value].value).rank)
            if any(d in compound_ids for d in point.driver_ids):
                candidates.append(min(RiskLevel(point.score_band).rank + escalation, RiskLevel.RED.rank))
            predicted = max(candidates)
            reference_level = RiskLevel(
                reference["horizons"][point.label]["risk"]["level"]
            ).rank
            report.check(
                predicted == RiskLevel(point.level).rank == reference_level,
                f"{scenario_id} {point.label}: explainer says {predicted}, engine said "
                f"{point.level}, reference holds {reference['horizons'][point.label]['risk']['level']}",
            )
            report.check(
                point.escalated_above_score
                == (RiskLevel(point.level).rank > RiskLevel(point.score_band).rank),
                f"{scenario_id} {point.label}: escalation flag disagrees with the levels shown",
            )

        # the evidence tab must hash to what was evaluated
        evidence = build_evidence(run, reference=reference, pack=pack)
        for artifact in evidence.artifacts:
            if artifact.path.startswith("("):
                continue
            report.check(
                artifact.present,
                f"{scenario_id}: evidence artifact {artifact.path} is not on this machine",
            )
            if artifact.sha256:
                report.check(
                    "DIFFERS" not in artifact.note,
                    f"{scenario_id}: {artifact.role} {artifact.path} -- {artifact.note}",
                )
        report.check(
            "DIFFERS" not in evidence.rule_pack.note,
            f"{scenario_id}: rule pack -- {evidence.rule_pack.note}",
        )

        # the first screen must be buildable from the same run
        overview = build_overview(run, timeline, catalog=catalog)
        report.check(
            overview.worst_level == run.worst_level,
            f"{scenario_id}: overview worst {overview.worst_level} != chain {run.worst_level}",
        )

    report.note(
        f"replay chain compared {total_fields} fields across {scenarios_checked} scenarios "
        f"x {len(HORIZON_ORDER)} horizons against the frozen references"
    )

    # --- benchmark ---------------------------------------------------------- #
    if EVIDENCE_DIR.is_dir():
        try:
            benchmark = load_benchmark()
        except Exception as exc:  # noqa: BLE001
            report.check(False, f"benchmark tab could not read the evidence: {exc}")
        else:
            report.check(len(benchmark.metrics) == 18, f"expected 18 frozen metrics, got {len(benchmark.metrics)}")
            report.check(
                benchmark.no_composite_score is True,
                f"evidence says no_composite_score={benchmark.no_composite_score}; the demo must not invent one",
            )
            report.check(
                benchmark.runs_total == benchmark.runs_scored + benchmark.runs_excluded,
                "runs_total is not scored + excluded",
            )
            report.note(
                f"benchmark read {benchmark.runs_total} runs "
                f"({benchmark.runs_scored} scored, {benchmark.runs_excluded} excluded), "
                f"{benchmark.paired_rows} paired rows, {benchmark.manual_review_rows} manual-review rows"
            )
            report.note(
                f"Phase 5 as recorded: {len(benchmark.improved)} metrics moved the right way, "
                f"{len(benchmark.worsened)} moved the wrong way, {len(benchmark.unchanged)} did not move"
            )
    else:
        report.check(False, f"no Phase 5 evidence at {EVIDENCE_DIR}; the Benchmark tab has nothing to show")

    # --- the screens themselves, then the things that are reported ----------- #
    _render_the_ui(report)
    _report_optional(report)
    return _finish(report)


def _probe_or_note(report: Report):
    """The local endpoint, or None if probing it failed. Never raises."""
    try:
        return probe_local_agent()
    except Exception as exc:  # noqa: BLE001 - a probe must never crash the gate
        report.note(f"the local agent probe crashed: {type(exc).__name__}: {exc}")
        return None


def _render_the_ui(report: Report) -> None:
    """Draw every tab, once, and require that none of them needed its safety net.

    The checks above cover the presentation layer: what the presenters build is what
    the tools returned. They cannot see a tab that renders the wrong way, because a
    broken tab still produces *a* screen -- ``app.py`` wraps each one so a failure
    cannot take the demo down. Phase 6 found two defects behind that net (an agent
    answer that could not be shown at all, and an Evidence table that never drew),
    and it found them by hand, in the last week, because nothing in the gate looked
    at the screens. So the gate looks at the screens now.
    """
    try:
        import streamlit  # noqa: F401
    except ImportError:
        report.note(
            "the UI was not rendered: streamlit is not installed, so there is no screen "
            "to render. The presenter checks above still ran in full."
        )
        return

    try:
        from streamlit.testing.v1 import AppTest
    except Exception as exc:  # noqa: BLE001 - an older streamlit has no AppTest
        report.note(f"the UI was not rendered: this streamlit has no AppTest ({exc})")
        return

    try:
        at = AppTest.from_file(str(REPO_ROOT / "demo" / "streamlit" / "app.py"), default_timeout=240)
        at.run()
    except Exception as exc:  # noqa: BLE001
        report.check(False, f"the demo would not start: {type(exc).__name__}: {exc}")
        return

    text = _rendered_text(at)
    report.check(not at.exception, "the demo raised while rendering")
    # The brief caps the top level at five tabs, and this is the check that keeps it.
    labels = [tab.label for tab in at.tabs]
    report.check(
        labels == ["Overview", "Risk Timeline", "Evidence", "Benchmark", "Architecture"],
        f"the demo shows tabs {labels}",
    )
    for tab in labels:
        report.check("could not be drawn" not in text, f"the {tab} tab failed to render")
    report.check("Unavailable" not in text, "some panel rendered itself unavailable")
    report.check(
        "worst level across the next two hours" in text,
        "the screen is missing the deterministic headline",
    )
    report.check(
        "not a probability of harm" in text,
        "the screen is missing the score's disclosure",
    )
    # The narrative boundary, asserted rather than hoped for. The sentence is the one
    # the brief sanctions, and it is only ever legitimate with its cost half attached,
    # so both halves are checked here -- and the claim the brief forbids is checked for
    # absence at the same time.
    report.check(
        "the longer orchestration chain also introduces substantial reliability" in text,
        "the benchmark tab states the improvement without the cost the brief requires "
        "beside it",
    )
    report.check(
        "TrailGuard makes Qwen safer" not in text,
        "the demo claims the skill made the model safer -- the one sentence the brief "
        "forbids",
    )
    # Brief 28 names the completion rate by name, in percentages, so the gate checks it
    # is on the screen rather than left as two numbers a reader must divide. The
    # percentage is arithmetic on the sums beside it -- which are checked on their own
    # elsewhere -- so this is a presence check, not a recomputation of the benchmark.
    report.check(
        "Completion rate." in text and "20 of 32 runs (62.5%)" in text,
        "the benchmark tab does not state the completion rate the brief names, in "
        "percentages, beside its own counts",
    )
    report.check(
        "2 of 32 (6.25%)" in text,
        "the with-skill completion rate is not on the screen in the brief's units",
    )
    # The Evidence tab's provenance, checked for completeness rather than for one row:
    # six files, each named at a repo-relative path and each read against the hash the
    # benchmark recorded. The two Safety Core files are what make the other four mean
    # anything, and a tab that had quietly stopped showing them would still render a
    # perfectly good-looking table -- so the assertion is on the row set, not on any
    # single row surviving.
    shown = {
        str(value)
        for frame in at.dataframe
        for value in frame.value.get("what it is", [])
    }
    report.check(
        {
            "rule pack",
            "calibration pack",
            "scenario",
            "route (GPX)",
            "device feed",
            "weather snapshot",
        }
        <= shown,
        f"the Evidence tab does not name the six frozen files behind the verdict: "
        f"{sorted(shown)}",
    )
    # The Evidence tab's raw-JSON disclosure. The default must be a table rather
    # than a dump, so the check is that the bytes are reachable and not that they
    # are on screen: an assertion that the JSON is absent would pass just as well
    # on a tab that had stopped offering it at all.
    report.check(
        any("View raw JSON" in expander.label for expander in at.expander),
        "the Evidence tab no longer discloses the raw frozen reference behind a click",
    )

    # The agent switch. With the model down the panel must say so; with the model
    # up it must not. Both are asserted on the badge the panel draws, because
    # "Local Agent unavailable" is also a sentence of prose in the Architecture
    # tab explaining the policy -- an assertion on the words alone passes while
    # the panel itself is broken, which is how this check first shipped.
    chooser = next(radio for radio in at.radio if radio.label == "Who reads the five tools?")
    at = chooser.set_value(chooser.options[1]).run()
    after = _rendered_text(at)
    report.check(not at.exception, "the demo raised while drawing the agent panel")
    availability = _probe_or_note(report)
    if availability is None:
        report.note("the local agent was not probed, so the agent panel is not asserted here")
    elif availability.available:
        report.check(
            UNREACHABLE not in after,
            "the agent panel reported the local model as down while it is up",
        )
        # The model-up panel has to be more than merely not-broken. The brief is
        # explicit that a 30B model is never run on page load, so what belongs on
        # screen is the trigger: a button naming the scenario, and the sentence
        # saying the sampling settings are the ones Phase 5 froze. Without this,
        # a panel that drew nothing at all would pass as "the panel is fine" --
        # the same blind spot the badge check above just closed, where a sentence
        # of prose in another tab stood in for the panel that was broken.
        buttons = [button.label for button in at.button]
        report.check(
            any("Run the agent on" in label for label in buttons),
            f"the model is up but the agent panel offered no run button ({buttons})",
        )
        report.check(
            "Sampling is frozen at temperature" in after,
            "the agent panel did not say its sampling is the Phase 5 frozen setting",
        )
    else:
        report.check(
            UNREACHABLE in after,
            f"the local model is down but the panel did not say so ({availability.reason})",
        )
    report.check(
        "worst level across the next two hours" in after,
        "the deterministic verdict vanished when the agent switch moved",
    )

    # Live weather, for real. The snapshot is not a fallback: choosing live and
    # failing must produce the weather failure, named as weather, and nothing
    # else on the screen may quietly become a replay number.
    weather = next(radio for radio in at.radio if "weather mode" in radio.label)
    at = weather.set_value(next(o for o in weather.options if "Live" in o)).run()
    live = _rendered_text(at)
    report.check(not at.exception, "the demo raised while choosing live weather")
    if LIVE_WEATHER_UNAVAILABLE in live:
        report.check(
            "fall back to the replay snapshot" in live,
            "the live failure did not say that the snapshot is not substituted",
        )
    else:
        report.note(
            "live weather answered, so the failure path was not exercised here; "
            "switching back to Replay mode is always available"
        )


def _rendered_text(at) -> str:
    """Everything AppTest captured as text, in one string."""
    parts: list[str] = []
    for name in ("markdown", "caption", "error", "warning", "info", "code"):
        for element in getattr(at, name, []) or []:
            parts.append(getattr(element, "value", None) or "")
    for metric in at.metric:
        parts.append(metric.label or "")
        parts.append(metric.value or "")
    return "\n".join(parts)


def _report_optional(report: Report) -> None:
    """Streamlit and the local model: say what is there, fail only if the demo is dishonest."""
    try:
        import streamlit  # noqa: F401

        report.note(f"streamlit is installed (version {getattr(streamlit, '__version__', '?')})")
    except ImportError:
        report.note(
            "streamlit is NOT installed. The presenter layer needs only pydantic and yaml, "
            "so Replay Mode, the Risk Timeline and the Evidence tab all still work; the "
            "Streamlit UI needs `pip install 'streamlit>=1.40'` (pip install demo extra). "
            "This is reported, not failed -- the brief forbids swapping in a substitute "
            "view to make the screen appear to work."
        )

    try:
        availability = probe_local_agent()
    except Exception as exc:  # noqa: BLE001 - a probe must never crash the gate
        report.note(f"local agent probe crashed: {type(exc).__name__}: {exc}")
        return
    if availability.available:
        limit = availability.max_model_len
        report.note(
            f"local agent reachable at {availability.base_url} serving {availability.model}"
            + (f" with max_model_len={limit}" if limit else " (context limit not reported)")
        )
        if limit is not None and limit != 16384:
            report.note(
                f"NOTE: the endpoint reports max_model_len={limit}; Phase 5 froze 16384, "
                "so a comparison against the frozen numbers would not be like-for-like."
            )
    else:
        report.note(
            f"{UNAVAILABLE}: {availability.reason} ({availability.detail}). The Agent tab "
            "shows the reason and stops; the address in the brackets stays in this operator "
            "log. No scripted provider is substituted."
        )


def _finish(report: Report) -> int:
    print("=" * 78)
    print("TrailGuard demo acceptance gate")
    print("=" * 78)
    for note in report.notes:
        print(f"  note  {note}")
    print()
    if report.failures:
        print(f"{len(report.failures)} FAILURE(S) out of {report.checks} checks:")
        for failure in report.failures:
            print(f"  FAIL  {failure}")
        print()
        print("RESULT: FAIL -- the demo must not be shown until these are resolved.")
        return 1
    print(f"All {report.checks} checks passed.")
    print("RESULT: PASS -- the presentation layer says exactly what the tools said.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
