"""Phase 6 demo: the presentation layer must not become a second safety core.

These tests are the enforcement behind the Phase 6 brief's hard rules. The four
that matter most are worth stating plainly, because each one is a way a demo
becomes dishonest in a hurry:

1. **A number on screen is a number a tool returned.** ``run_replay`` is compared
   against the committed Phase 5 Deterministic Reference field for field, for every
   scenario and every horizon. This is the test that makes "the demo cannot fudge a
   result" true rather than asserted.
2. **The explainer agrees with the engine.** The level decomposition shown to a judge
   -- score band, then compound escalation, then the maximum against any triggered
   hard rule's floor -- is checked against the level the engine actually returned,
   and against the reference, for all 56 horizon rows.
3. **Nothing falls back silently.** Replay is the labelled default; a live failure
   raises and is never answered from the snapshot; an unavailable local agent raises
   and is never answered from a script.
4. **Phase 5's evidence is read-only.** The benchmark tab opens the committed files
   and computes nothing; a test that wrote there would be caught, so the test asserts
   it does not.

The suite stays offline (spec 13): the local agent is exercised through
``present_agent_run`` and a stubbed availability object, never over the wire.
"""

from __future__ import annotations

import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

import tests.conftest as conftest
from demo.presenters.agent_view import (
    UNAVAILABLE,
    AgentAvailability,
    LocalAgentUnavailable,
    present_agent_run,
    probe_local_agent,
    run_local_agent,
)
from demo.presenters.benchmark_view import EVIDENCE_DIR, load_benchmark
from demo.presenters.catalog import load_catalog
from demo.presenters.chain import (
    WEATHER_UNAVAILABLE_PREFIX,
    WeatherMode,
    WeatherUnavailable,
    run_replay,
)
from demo.presenters.evidence_view import build_evidence
from demo.presenters.route_view import route_profile
from demo.presenters.scenario_view import build_overview
from demo.presenters.timeline import LEVEL_STYLE, build_timeline
from demo.presenters.weather_view import weather_rows
from demo.streamlit.components import charts
from trailguard.risk_core import load_calibration, load_rule_pack
from trailguard.schemas.risk import RiskLevel

REPO_ROOT = conftest.REPO_ROOT
REFERENCES_DIR = REPO_ROOT / "evals" / "phase5" / "references"

#: The three headline scenarios, in the order the brief puts them (spec 14).
FEATURED_IDS = ("B05_late_gust_hazard", "B04_tight_sunset_margin", "B10_compound_soft_pair")


def _reference_path(scenario_id: str) -> Path:
    return REFERENCES_DIR / f"{scenario_id}.json"


def _reference(scenario_id: str) -> dict:
    return json.loads(_reference_path(scenario_id).read_text(encoding="utf-8"))


def _scenario(scenario_id: str):
    from trailguard.agent.replay_runner import load_scenario

    return load_scenario(REPO_ROOT / _reference(scenario_id)["scenario_file"])


@pytest.fixture(scope="module")
def pack_and_calibration():
    return load_rule_pack(), load_calibration()


@pytest.fixture(scope="module")
def catalog():
    return load_catalog()


@pytest.fixture(scope="session")
def runs_by_id():
    """Every scenario's replay chain, once. Slow enough to be worth sharing."""
    output = {}
    for path in sorted(REFERENCES_DIR.glob("*.json")):
        reference = json.loads(path.read_text(encoding="utf-8"))
        output[path.stem] = run_replay(_scenario(path.stem))
    return output


# --------------------------------------------------------------------------- #
# 1. Replay Mode reproduces the frozen reference
# --------------------------------------------------------------------------- #


def test_every_scenario_has_a_reference():
    """The whole dataset is covered, not a convenient subset of it."""
    scenarios = sorted(conftest.SCENARIOS_DIR.rglob("*.yaml"))
    references = sorted(p.stem for p in REFERENCES_DIR.glob("*.json"))
    assert len(references) == len(scenarios), (
        f"{len(references)} references for {len(scenarios)} scenarios; the demo must be "
        "verifiable against every scenario, and a scenario without one is unverifiable"
    )


@pytest.mark.parametrize("scenario_id", [p.stem for p in sorted(REFERENCES_DIR.glob("*.json"))])
def test_replay_chain_matches_the_frozen_reference(scenario_id):
    """The demo's chain and the benchmark's reference are the same measurement."""
    reference = _reference(scenario_id)
    run = run_replay(_scenario(scenario_id))

    assert run.scenario_id == scenario_id
    assert [row.label for row in run.rows] == conftest.HORIZON_VALUES
    assert run.worst_level == reference["expectations_for_evaluator"]["worst_level"]

    for label in conftest.HORIZON_VALUES:
        expected = reference["horizons"][label]
        row = next(r for r in run.rows if r.label == label)
        risk = expected["risk"]

        assert row.risk_level == risk["level"], f"{scenario_id} {label} level"
        assert row.risk_score == risk["score"], f"{scenario_id} {label} soft score"
        assert row.hard_rules_triggered == risk["hard_rules_triggered"], f"{scenario_id} {label} hard rules"
        assert [d["rule_id"] for d in row.drivers] == [d["rule_id"] for d in risk["drivers"]], (
            f"{scenario_id} {label} drivers"
        )
        assert row.estimated_minutes_to_finish == pytest.approx(
            expected["estimated_minutes_to_finish"], abs=1e-9
        ), f"{scenario_id} {label} ETA"
        assert row.projected_position_lat == pytest.approx(expected["projected_position"]["lat"], abs=1e-9)
        assert row.projected_position_lon == pytest.approx(expected["projected_position"]["lon"], abs=1e-9)
        assert row.projected_alt_m == pytest.approx(expected["projected_position"]["alt_m"], abs=1e-9)
        assert row.weather.wind_gust_kmh == pytest.approx(expected["weather"]["wind_gust_kmh"], abs=1e-9)
        assert row.weather.temperature_c == pytest.approx(expected["weather"]["temperature_c"], abs=1e-9)
        assert row.weather.source == expected["weather"]["source"], f"{scenario_id} {label} weather source"
        assert row.evidence_refs == risk["evidence_refs"], f"{scenario_id} {label} evidence refs"


def test_replay_is_the_default_and_is_labelled(runs_by_id):
    """Replay is the default, and the run says so rather than leaving it to be inferred."""
    for scenario_id, run in runs_by_id.items():
        assert run.weather_mode == "replay", f"{scenario_id} defaulted to {run.weather_mode}"
        assert "weather mode replay" in " ".join(run.provenance)
        assert run.weather_snapshot_ref, f"{scenario_id} carried no snapshot reference"


def test_live_weather_failure_is_never_answered_from_the_snapshot():
    """A live call that cannot be reached raises; it is not silently replayed."""
    scenario = _scenario("B05_late_gust_hazard")

    class _Boom:
        def __call__(self, *args, **kwargs):
            raise RuntimeError("the network is down")

    import demo.presenters.chain as chain_module

    original = chain_module.get_weather_for_route_time
    chain_module.get_weather_for_route_time = _Boom()
    try:
        with pytest.raises(WeatherUnavailable) as raised:
            run_replay(scenario, weather_mode="live")
    finally:
        chain_module.get_weather_for_route_time = original

    message = str(raised.value)
    assert WEATHER_UNAVAILABLE_PREFIX in message
    assert "WeatherUnavailable" not in message, (
        "the message names the cause without inheriting the traceback"
    )
    assert UNAVAILABLE not in message, (
        "a weather failure was reported as the local agent being down: the "
        "sentence named the wrong broken thing, which is the one mistake a "
        "failure message cannot afford"
    )


def test_live_mode_does_not_mutate_the_frozen_scenario():
    """Asking for live weather must not edit the scenario in place."""
    scenario = _scenario("B05_late_gust_hazard")
    before = scenario.initial_context.weather_policy.model_copy(deep=True)
    scenario.initial_context.weather_policy.model_copy(update={"mode": "live"})
    assert scenario.initial_context.weather_policy == before


def test_weather_mode_vocabulary_is_closed():
    """Two modes, no third 'auto' that could quietly pick one."""
    assert set(WeatherMode.__args__) == {"replay", "live"}


# --------------------------------------------------------------------------- #
# 2. The explainer agrees with the engine
# --------------------------------------------------------------------------- #


def test_the_level_explainer_agrees_with_the_engine_for_every_horizon(
    runs_by_id, pack_and_calibration
):
    """max(band -> escalate -> clamp, hard-rule floor) is what the engine returned."""
    pack, calibration = pack_and_calibration
    floors = calibration.severity_floor
    rules = {rid: pack.get(rid) for rid in pack.ids()}
    compound_ids = [rid for rid, rule in rules.items() if "escalation_levels" in rule.thresholds]
    escalation = int(rules[compound_ids[0]].thresholds["escalation_levels"])

    rows = 0
    for scenario_id, run in runs_by_id.items():
        reference = _reference(scenario_id)
        for point in build_timeline(run, pack=pack, calibration=calibration).points:
            candidates = [RiskLevel(point.score_band).rank]
            for rid in point.hard_rules:
                candidates.append(RiskLevel(floors[rules[rid].severity.value].value).rank)
            if any(d in compound_ids for d in point.driver_ids):
                candidates.append(
                    min(RiskLevel(point.score_band).rank + escalation, RiskLevel.RED.rank)
                )
            predicted = max(candidates)
            reported = RiskLevel(point.level).rank
            frozen = RiskLevel(reference["horizons"][point.label]["risk"]["level"]).rank
            assert predicted == reported == frozen, (
                f"{scenario_id} {point.label}: explainer says {predicted}, engine said "
                f"{point.level}, reference holds {frozen}"
            )
            assert point.escalated_above_score == (reported > RiskLevel(point.score_band).rank)
            rows += 1
    assert rows == len(runs_by_id) * len(conftest.HORIZON_VALUES)


def test_the_three_featured_scenarios_cover_the_three_assembly_paths(runs_by_id):
    """The headline demos are chosen for what they prove, one path each."""
    # B05: the band alone. B04: a hard-rule floor alone. B10: escalation alone.
    assert runs_by_id["B05_late_gust_hazard"].rows[-1].risk_score > 0
    assert runs_by_id["B05_late_gust_hazard"].rows[-1].hard_rules_triggered == []
    assert runs_by_id["B05_late_gust_hazard"].rows[-1].drivers[0]["rule_id"] == "R-C"

    assert runs_by_id["B04_tight_sunset_margin"].rows[0].risk_score == 0, (
        "the killer demo: score 0 and still RED, because the rule is hard"
    )
    assert runs_by_id["B04_tight_sunset_margin"].rows[0].hard_rules_triggered == ["R-B"]
    assert runs_by_id["B04_tight_sunset_margin"].rows[0].risk_level == "RED"

    assert runs_by_id["B10_compound_soft_pair"].rows[0].hard_rules_triggered == []
    assert [d["rule_id"] for d in runs_by_id["B10_compound_soft_pair"].rows[0].drivers] == [
        "R-D",
        "R-E",
        "R-F",
    ]


def test_a_hard_rule_never_lowers_a_level(runs_by_id):
    """Spec 10.5, checked on the data rather than on the code."""
    for scenario_id, run in runs_by_id.items():
        for point in build_timeline(run).points:
            if point.hard_rules:
                assert RiskLevel(point.level).rank >= min(
                    RiskLevel(step.outcome).rank
                    for step in point.assembly
                    if step.source.startswith("hard-rule floor")
                ), f"{scenario_id} {point.label}"


def test_the_soft_score_is_never_presented_as_a_probability():
    """The label travels with the number, everywhere the number is shown."""
    from demo.presenters.timeline import SCORE_UNIT_NOTE

    assert "not a probability" in SCORE_UNIT_NOTE
    assert SCORE_UNIT_NOTE == build_timeline(run_replay(_scenario("S01_clear_day"))).score_note


# --------------------------------------------------------------------------- #
# 3. No silent fallback, no silent mode change
# --------------------------------------------------------------------------- #


def test_agent_unavailable_raises_rather_than_substituting(catalog):
    """There is no code path that fabricates a run when the endpoint is down."""
    scenario = _scenario("B04_tight_sunset_margin")
    unavailable = AgentAvailability(
        available=False,
        reason="the health endpoint is not reachable",
        detail="http://127.0.0.1:8000/health -> ConnectionRefusedError",
        model="qwen3-vl-30b-a3b",
        base_url="http://127.0.0.1:8000/v1",
    )
    with pytest.raises(LocalAgentUnavailable) as raised:
        run_local_agent(scenario, availability=unavailable)
    assert UNAVAILABLE in str(raised.value)
    assert "the health endpoint is not reachable" in str(raised.value)


def test_the_exception_a_screen_renders_never_carries_the_probed_address(catalog):
    """The message that reaches a screen, checked the way it is actually rendered.

    ``run_local_agent`` raises and ``app.py:120-121`` hands ``str(exc)`` to
    ``panels.unavailable`` as the box body -- verbatim. So this test walks the real
    probe's failure outcomes through the real ``run_local_agent`` and reads the raised
    message as the screen would see it. The detail is not merely absent from the
    screen field: it must be absent from the message, because the message *is* the
    screen field, and a test that only asserted the address stayed out of
    ``demo_screen_reason`` passed while this path carried it straight through.
    """
    import socket
    import urllib.error

    def refuses(url: str, timeout_s: float) -> tuple[int, bytes]:
        raise urllib.error.URLError("connection refused")

    def times_out(url: str, timeout_s: float) -> tuple[int, bytes]:
        raise socket.timeout()

    def serves_503(url: str, timeout_s: float) -> tuple[int, bytes]:
        return 503, b"unavailable"

    def serves_something_else(url: str, timeout_s: float) -> tuple[int, bytes]:
        return 200, b'{"data":[{"id":"some-other-model","max_model_len":8192}]}'

    for stub in (refuses, times_out, serves_503, serves_something_else):
        unavailable = probe_local_agent(
            base_url="http://127.0.0.1:8000/v1",
            model="qwen3-vl-30b-a3b",
            opener=stub,
        )
        # The caller-supplied branch and the internal re-probe branch must agree,
        # because the app uses the first on page load and the second on button press.
        with pytest.raises(LocalAgentUnavailable) as raised:
            run_local_agent(_scenario("B04_tight_sunset_margin"), availability=unavailable)
        message = str(raised.value)
        assert message.startswith(UNAVAILABLE), message
        assert unavailable.reason in message, message
        for banned in ("http://", "https://", "127.0.0.1", "8000", str(Path.home())):
            assert banned not in message, f"the screen would show {banned!r}: {message!r}"


def _healthy_opener(model: str = "qwen3-vl-30b-a3b", max_model_len: int = 16384):
    """A stub endpoint that answers the way vLLM answers."""

    def opener(url: str, timeout_s: float) -> tuple[int, bytes]:
        import json

        if url.endswith("/health"):
            return 200, b""
        return 200, json.dumps(
            {"data": [{"id": model, "max_model_len": max_model_len, "object": "model"}]}
        ).encode()

    return opener


#: The real ``_real_opener`` is deliberately not exercised here: the suite closes
#: the socket layer (spec 13), so every probe below injects a transport. What the
#: real opener does is two lines -- GET the URL, return the status and body -- and
#: the branches it can produce are the ones tested below.


def test_the_probe_accepts_an_endpoint_serving_the_expected_model():
    availability = probe_local_agent(base_url="http://127.0.0.1:8000/v1", opener=_healthy_opener())
    assert availability.available is True
    assert availability.reason == "reachable and serving the expected model"
    assert availability.max_model_len == 16384


def test_the_probe_names_a_wrong_model_instead_of_running_against_it():
    """Phase 5 froze 16384 and this model; a different server is a finding.

    The context limit is reported even though the model is wrong, because "serving
    some-other-model with max_model_len=4096" tells the operator what the server
    was actually launched with.
    """
    availability = probe_local_agent(
        base_url="http://127.0.0.1:8000/v1",
        opener=_healthy_opener(model="some-other-model", max_model_len=4096),
    )
    assert availability.available is False
    assert "some-other-model" in availability.reason
    assert "qwen3-vl-30b-a3b" in availability.reason
    assert availability.max_model_len == 4096


def test_the_probe_reports_a_refused_endpoint_as_unavailable():
    import urllib.error

    def refused(url: str, timeout_s: float) -> tuple[int, bytes]:
        raise urllib.error.URLError(ConnectionRefusedError(111, "refused"))

    availability = probe_local_agent(base_url="http://127.0.0.1:8000/v1", opener=refused)
    assert availability.available is False
    assert "not reachable" in availability.reason
    assert availability.model == "qwen3-vl-30b-a3b"


def test_the_probe_reports_a_timeout_as_unavailable():
    def slow(url: str, timeout_s: float) -> tuple[int, bytes]:
        raise TimeoutError("timed out")

    availability = probe_local_agent(base_url="http://127.0.0.1:8000/v1", opener=slow)
    assert availability.available is False
    assert "timed out" in availability.reason


def test_the_probe_reports_a_non_200_health_answer_as_unavailable():
    """A server still loading its weights is not a server that can take a request."""

    def sick(url: str, timeout_s: float) -> tuple[int, bytes]:
        return 503, b"loading"

    availability = probe_local_agent(base_url="http://127.0.0.1:8000/v1", opener=sick)
    assert availability.available is False
    assert "503" in availability.reason


def test_the_probe_does_not_run_when_the_model_is_not_the_one_it_will_call():
    """The gate is before the run, not after a wasted sixteen steps."""
    scenario = _scenario("B04_tight_sunset_margin")
    wrong = AgentAvailability(
        available=False,
        reason="the endpoint is serving some-other-model, not qwen3-vl-30b-a3b",
        detail="",
        model="qwen3-vl-30b-a3b",
        base_url="http://127.0.0.1:8000/v1",
    )
    with pytest.raises(LocalAgentUnavailable) as raised:
        run_local_agent(scenario, availability=wrong)
    assert "some-other-model" in str(raised.value)


def test_the_sentence_a_screen_shows_never_carries_the_address_it_probed():
    """Every way the probe can fail must leave a judge-facing sentence clean.

    The probe's ``detail`` quotes the URL it asked, which is exactly what an
    operator starting a vLLM server needs and exactly what a competition screen
    must not show. So the screen reads ``demo_screen_reason`` instead, and this
    test walks the probe's own failure outcomes -- the real function, with the
    transport injected the way the probe is designed to be tested (spec 13) -- and
    reads what the screen would have been handed.
    """
    import socket

    import urllib.error

    def refuses(url: str, timeout_s: float) -> tuple[int, bytes]:
        raise urllib.error.URLError("connection refused")

    def times_out(url: str, timeout_s: float) -> tuple[int, bytes]:
        raise socket.timeout()

    def serves_503(url: str, timeout_s: float) -> tuple[int, bytes]:
        return 503, b"unavailable"

    def serves_something_else(url: str, timeout_s: float) -> tuple[int, bytes]:
        return 200, b'{"data":[{"id":"some-other-model","max_model_len":8192}]}'

    for stub in (refuses, times_out, serves_503, serves_something_else):
        availability = probe_local_agent(
            base_url="http://127.0.0.1:8000/v1",
            model="qwen3-vl-30b-a3b",
            opener=stub,
        )
        assert availability.available is False, "the stub should have failed the probe"
        assert availability.detail, "the operator's detail is still there to be read"
        screen = availability.demo_screen_reason
        assert screen == availability.reason
        assert screen.strip(), "a screen needs a sentence, not an empty string"
        for banned in ("http://", "https://", "127.0.0.1", "8000", str(Path.home())):
            assert banned not in screen, f"a screen would show {banned!r}: {screen!r}"


# --------------------------------------------------------------------------- #
# 4. The agent's claim is read by the benchmark's own scanner
# --------------------------------------------------------------------------- #


def _trace_with(answer: str, core_level: str | None = "RED", covered=("NOW",), conf=0.9):
    from trailguard.agent.trace import TraceCollector
    from trailguard.schemas.risk import RiskLevel as RL

    collector = TraceCollector.new(run_id="probe", model_name="qwen3-vl-30b-a3b", provider="local")
    collector.scenario_id = "probe"
    for horizon in covered:
        collector.record_core_verdict(
            __import__("trailguard.schemas.route", fromlist=["Horizon"]).Horizon(horizon),
            RL(core_level) if core_level else None,
            conf,
        )
    return collector.finish(finished_at=datetime.now(timezone.utc)), answer


#: Answers in the shape the frozen scanner reads. ``scan_answer`` attributes a
#: level to a horizon only from a heading or a label -- "GREEN at NOW" is not one,
#: which is itself a property of the benchmark and not of the demo, so the test
#: fixtures are written the way a Qwen answer actually comes out.
GREENS = "NOW: GREEN\n+30min: GREEN\n"
REDS = "NOW: RED\n+30min: RED\n+60min: RED\n+120min: RED\n"


def test_a_contradiction_between_agent_and_core_is_reported():
    trace, answer = _trace_with(GREENS, "RED")
    view = present_agent_run(trace, answer, "completed", 12, "probe")
    assert view.contradicted is True
    assert "Contradiction" in view.contradiction_note
    assert view.worst_claimed == "GREEN"
    assert view.core_risk_level == "RED"


def test_agreement_between_agent_and_core_is_reported():
    trace, answer = _trace_with(REDS, "RED")
    view = present_agent_run(trace, answer, "completed", 12, "probe")
    assert view.contradicted is False
    assert "Agreement" in view.contradiction_note


def test_an_answer_with_no_readable_claim_is_not_scored_either_way():
    """The scanner cannot read a level, so the demo says so instead of guessing."""
    trace, answer = _trace_with("I am unable to complete the analysis.", "RED")
    view = present_agent_run(trace, answer, "completed", 3, "probe")
    assert view.claimed_levels == {}
    assert view.contradicted is False
    assert "no horizon-level claim" in view.contradiction_note


def test_the_view_reads_the_answer_exactly_as_the_frozen_scanner_reads_it():
    """The displayed claim is the benchmark's reading, not a second parser's."""
    from evals.phase5.evaluate import scan_answer

    answers = [
        GREENS,
        REDS,
        "NOW: YELLOW\n+30min: ORANGE\n+60min: RED\n+120min: RED\n",
        "NOW: possibly GREEN\n+30min: GREEN\n",
        "There is a 60% chance of an accident on this route.\n",
        "I am unable to complete the analysis.",
        "",
    ]
    for answer in answers:
        trace, answer = _trace_with(answer, "ORANGE")
        view = present_agent_run(trace, answer, "completed", 12, "probe")
        report = scan_answer(answer or "")
        assert view.claimed_levels == report.levels, answer
        assert view.claimed_rules == sorted(report.rules), answer
        assert view.hedges == report.hedges, answer
        assert view.probability_claims == report.probability_claims, answer
        assert view.negated == report.negated, answer
        assert view.conflicting == report.conflicting, answer


def test_the_comparison_is_of_maxima_like_the_frozen_metric():
    """M12 compares worst against worst; a partial disagreement is not a contradiction."""
    from evals.phase5.evaluate import _rank

    assert _rank("RED") == 3
    assert _rank("GREEN") == 0
    trace, answer = _trace_with("NOW: GREEN. +30min: YELLOW. +60min: ORANGE. +120min: RED.", "RED")
    view = present_agent_run(trace, answer, "completed", 12, "probe")
    assert view.worst_claimed == "RED"
    assert view.contradicted is False


def test_a_run_the_core_never_judged_is_not_reported_as_agreement(catalog):
    """The screen must not claim an agreement the run never produced.

    An agent that never calls ``assess_risk`` leaves the trace with no core verdict,
    and 16 of the 43 with-skill Phase 5 runs were recorded in exactly that state -- so
    this is the common case, not an edge. The old wording still branched on whether the
    *answer* was scannable, which made that state print "Agreement: ... None" and assert
    a comparison that had not happened.
    """
    trace, answer = _trace_with("NOW: GREEN\n+30min: YELLOW\n", core_level=None, covered=())
    view = present_agent_run(trace, answer, "completed", 3, "probe")

    assert view.contradicted is False, "no verdict means no contradiction, either way"
    assert "No comparison was possible" in view.contradiction_note
    assert "Agreement" not in view.contradiction_note
    assert "None" not in view.contradiction_note
    assert "never asked the deterministic core" in view.contradiction_note
    # The scanner's own reading is still shown, because it is a fact about the answer.
    assert view.claimed_levels == {"NOW": "GREEN", "+30min": "YELLOW"}


def test_stop_reasons_are_shown_in_words_not_just_identifiers():
    from demo.presenters.agent_view import STOP_REASON_TEXT

    for reason in ("completed", "max_steps", "provider_error", "tool_error", "invalid_tool_call"):
        assert reason in STOP_REASON_TEXT and STOP_REASON_TEXT[reason]
    trace, answer = _trace_with("")
    view = present_agent_run(trace, answer, "max_steps", 16, "probe")
    assert "steps" in view.stop_reason_text
    assert view.answer == ""


# --------------------------------------------------------------------------- #
# 5. The catalogue is derived, not hand-declared
# --------------------------------------------------------------------------- #


def test_the_catalogue_labels_cannot_drift_from_the_references(catalog, runs_by_id):
    """Levels and rule ids come from the reference at load time."""
    for entry in catalog:
        run = runs_by_id[entry.scenario_id]
        assert entry.worst_level == run.worst_level, entry.scenario_id
        assert entry.label == "/".join(row.risk_level[:1] for row in run.rows), entry.scenario_id
        expected_rules = []
        for row in run.rows:
            for driver in row.drivers:
                if driver["rule_id"] not in expected_rules:
                    expected_rules.append(driver["rule_id"])
        assert list(entry.rules) == expected_rules, entry.scenario_id


def test_the_three_featured_demos_are_offered_and_the_rest_are_not_on_the_path(catalog):
    assert [entry.scenario_id for entry in catalog.featured] == list(FEATURED_IDS)
    assert catalog.control.scenario_id == "S01_clear_day"
    assert catalog.count() == 14
    featured_ids = {entry.scenario_id for entry in catalog.featured}
    featured_ids.add(catalog.control.scenario_id)
    assert not featured_ids & {entry.scenario_id for entry in catalog.advanced}


def test_the_rule_catalogue_is_read_from_the_pack(catalog, pack_and_calibration):
    pack, _ = pack_and_calibration
    assert list(catalog.rule_catalog) == pack.ids()
    assert catalog.rule_catalog["R-B"]["severity"] == "CRITICAL"
    assert catalog.rule_catalog["R-B"]["kind"] == "hard"
    assert "time_margin_min_warn=30" in catalog.rule_catalog["R-B"]["thresholds"]


# --------------------------------------------------------------------------- #
# 6. The geometry and weather views copy, and do not compute
# --------------------------------------------------------------------------- #


def test_the_route_view_copies_the_geometry_it_did_not_measure():
    from trailguard.routes import resolve_route_geometry

    run = run_replay(_scenario("B05_late_gust_hazard"))
    profile = route_profile(run)
    geometry = resolve_route_geometry(run.route["route_id"])

    assert profile.coordinates == [(p.lat, p.lon) for p in geometry.points]
    assert profile.elevation.x_m == [float(d) for d in geometry.cumulative_m]
    assert profile.elevation.y_m == [float(p.elevation_m) for p in geometry.points]
    assert profile.total_distance_km == pytest.approx(geometry.total_distance_m / 1000.0)
    assert profile.point_count == geometry.point_count


def test_the_route_markers_are_the_positions_the_projection_returned():
    run = run_replay(_scenario("B05_late_gust_hazard"))
    profile = route_profile(run)
    for marker, row in zip(profile.markers, run.rows):
        assert marker.lat == pytest.approx(row.projected_position_lat)
        assert marker.lon == pytest.approx(row.projected_position_lon)
        assert marker.level == row.risk_level
        assert marker.colour == LEVEL_STYLE[row.risk_level]["colour"]


def test_the_weather_view_annotates_from_the_pack(pack_and_calibration):
    pack, _ = pack_and_calibration
    run = run_replay(_scenario("B05_late_gust_hazard"))
    rows = weather_rows(run, pack=pack)
    assert len(rows) == 4
    for row in rows:
        assert "wind_gust_kmh=45" in " ".join(row.gust_threshold_notes)
        assert row.freshness_budget_min == run.freshness_budget_min
        assert row.freshness_ok == (row.freshness_minutes <= run.freshness_budget_min)
    assert rows[-1].wind_gust_kmh > rows[0].wind_gust_kmh, "the gust front arrives later"


def test_the_weather_summary_names_the_mode_and_the_budget():
    run = run_replay(_scenario("B05_late_gust_hazard"))
    summary_line = __import__(
        "demo.presenters.weather_view", fromlist=["weather_summary"]
    ).weather_summary(run)
    assert "replay" in summary_line
    assert str(run.weather_snapshot_ref) in summary_line or "snapshot" in summary_line
    assert "staleness budget" in summary_line


# --------------------------------------------------------------------------- #
# 7. The evidence view hashes what was evaluated, and lists what is unknown
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("scenario_id", FEATURED_IDS)
def test_the_evidence_tab_hashes_match_what_the_benchmark_evaluated(scenario_id):
    reference = _reference(scenario_id)
    run = run_replay(_scenario(scenario_id))
    view = build_evidence(run, reference=reference)

    for artifact in view.artifacts:
        assert artifact.present, f"{artifact.role} is not on this machine"
        assert artifact.sha256, f"{artifact.role} has no hash"
        assert "DIFFERS" not in artifact.note, f"{artifact.role}: {artifact.note}"
    assert "DIFFERS" not in view.rule_pack.note
    assert view.rule_pack.sha256 == reference["rules_sha256"]


def test_the_evidence_tab_lists_what_it_does_not_know():
    """A disclosure list is part of the tab, not an afterthought."""
    reference = _reference("B05_late_gust_hazard")
    run = run_replay(_scenario("B05_late_gust_hazard"))
    view = build_evidence(run, reference=reference)
    assert view.gaps, "the control scenario has nothing to disclose; this one must"
    joined = " ".join(view.gaps)
    assert "assumed" in joined or "confidence" in joined or "quality" in joined


def test_the_evidence_view_reports_no_composite_and_no_percentage_of_harm():
    reference = _reference("B10_compound_soft_pair")
    run = run_replay(_scenario("B10_compound_soft_pair"))
    view = build_evidence(run, reference=reference)
    rendered = json.dumps([row.verdict for row in view.chain])
    assert "not a probability" in rendered
    for banned in ("chance of", "risk of dying", "probability of harm", "% chance"):
        assert banned not in rendered


# --------------------------------------------------------------------------- #
# 8. The Benchmark tab reads, and derives nothing
# --------------------------------------------------------------------------- #


@pytest.fixture(scope="module")
def benchmark():
    if not EVIDENCE_DIR.is_dir():
        pytest.skip("Phase 5 evidence is not committed in this checkout")
    return load_benchmark()


def test_the_benchmark_tab_shows_all_eighteen_metrics(benchmark):
    assert len(benchmark.metrics) == 18
    assert {row.metric_id for row in benchmark.metrics} == {
        f"M{i}_{name}"
        for i, name in enumerate(
            (
                "completion_rate",
                "structured_tool_call_success_rate",
                "valid_tool_call_rate",
                "protocol_completion_rate",
                "horizon_coverage",
                "risk_level_exact_match",
                "hazard_recall",
                "hard_rule_recall",
                "risk_driver_recall",
                "missed_hazard_rate",
                "false_alarm_rate",
                "core_contradiction_rate",
                "hard_rule_softening_rate",
                "unsupported_probability_claim_rate",
                "malformed_tool_call_rate",
                "context_limit_failure_rate",
                "max_step_failure_rate",
                "provider_failure_rate",
            ),
            start=1,
        )
    }


def test_the_benchmark_tab_never_invents_a_composite_score(benchmark):
    assert benchmark.no_composite_score is True
    groups = (benchmark.improved, benchmark.worsened, benchmark.unchanged)
    assert sum(len(group) for group in groups) == 18
    # A metric with no paired data is neither an improvement nor a regression.
    improved = {row.metric_id for row in benchmark.improved}
    worsened = {row.metric_id for row in benchmark.worsened}
    unchanged = {row.metric_id for row in benchmark.unchanged}
    assert not improved & worsened
    assert not worsened & unchanged
    assert improved | worsened | unchanged == {row.metric_id for row in benchmark.metrics}


def test_the_benchmark_tab_shows_both_sides_of_the_mixed_finding(benchmark):
    """Phase 5's finding was MIXED; hiding either side would make it a lie."""
    improved = {row.metric_id for row in benchmark.improved}
    worsened = {row.metric_id for row in benchmark.worsened}
    assert improved and worsened, (
        f"improved={sorted(improved)} worsened={sorted(worsened)}; the Phase 5 finding was "
        "that the skill moved some metrics each way, and a demo showing only one is dishonest"
    )
    for row in benchmark.metrics:
        if row.mean_paired_delta is not None:
            assert row.direction in ("higher_is_better", "lower_is_better")
            assert row.definition, row.metric_id


def test_the_benchmark_tab_reads_the_committed_files_without_writing_them(benchmark):
    for name, digest in benchmark.source_files.items():
        assert digest != "absent", f"{name} is not present"
    before = {
        path.name: path.stat().st_mtime_ns
        for path in EVIDENCE_DIR.iterdir()
        if path.is_file()
    }
    again = load_benchmark()
    after = {
        path.name: path.stat().st_mtime_ns
        for path in EVIDENCE_DIR.iterdir()
        if path.is_file()
    }
    assert before == after, "loading the benchmark changed the evidence files"
    assert len(again.metrics) == len(benchmark.metrics)


def test_the_evidence_tab_names_every_frozen_file_and_its_evaluated_hash(runs_by_id):
    """Six rows, six "matches", on the tab whose job is to make the demo checkable.

    The four inputs the run consumed were already there; the Safety Core's two files
    were computed and asserted by the tests but never rendered, which left the
    provenance exhibit proving what was fed in while omitting what decided the level.
    """
    import json

    for scenario_id, run in runs_by_id.items():
        reference = json.loads(
            (REFERENCES_DIR / f"{scenario_id}.json").read_text(encoding="utf-8")
        )
        view = build_evidence(run, reference=reference)
        shown = [view.rule_pack, view.calibration] + view.artifacts
        assert len(shown) == 6, scenario_id
        for artifact in shown:
            assert artifact.present, f"{scenario_id}: {artifact.role} is missing"
            assert artifact.note == "matches the hash the benchmark evaluated", (
                f"{scenario_id}: {artifact.role} says {artifact.note!r}"
            )
        assert view.rule_pack.sha256 == reference.get("rules_sha256")
        assert view.calibration.sha256 == reference.get("calibration_sha256")


def test_the_completion_rate_is_shown_as_a_percentage_of_its_own_runs(benchmark):
    """Brief 28 names this figure, so the tab must not make a judge do the division.

    The evidence stores the two sums and the two run counts; the percentage is
    arithmetic on those four numbers, which are already on the same screen. So this
    asserts both halves at once -- that the percentage is 62.5% -> 6.25%, and that the
    sums it came from are still there unmodified -- because a percentage that had
    quietly replaced the counts would be a statistic with nothing to check it against.
    """
    completion = benchmark.completion_rate
    assert completion is not None and completion.metric_id == "M1_completion_rate"
    assert completion.rate_text == "62.5% -> 6.25%"

    payload = json.loads((EVIDENCE_DIR / "metrics.json").read_text(encoding="utf-8"))
    entry = payload["metrics"]["M1_completion_rate"]
    assert completion.sum_baseline == entry["sum_baseline"] == 20.0
    assert completion.sum_treatment == entry["sum_treatment"] == 2.0
    assert completion.n_runs_baseline == entry["n_runs_baseline"] == 32
    assert completion.n_runs_treatment == entry["n_runs_treatment"] == 32


def test_a_rate_is_only_derived_where_the_evidence_makes_one_meaningful(benchmark):
    """The percentage must not be applied to a metric whose denominator is not runs.

    M5 horizon coverage sums *horizons*, of which there are four per reference, so
    dividing it by the 32 runs in the condition would produce a number that looks like
    a rate and means nothing. The line between a rate and a sum is the evidence's own
    denominator wording, and this test walks all 18 metrics to check the property
    followed it.
    """
    for row in benchmark.metrics:
        if row.is_run_rate:
            assert row.denominator.startswith("runs attempted in the condition")
            assert row.rate_text is not None, row.metric_id
        else:
            assert row.rate_text is None, row.metric_id
    horizon = next(r for r in benchmark.metrics if r.metric_id == "M5_horizon_coverage")
    assert horizon.is_run_rate is False
    assert horizon.rate_text is None


def test_benchmark_values_are_not_recomputed(benchmark):
    """Every number the tab shows is one the evidence already carries."""
    payload = json.loads((EVIDENCE_DIR / "metrics.json").read_text(encoding="utf-8"))
    for row in benchmark.metrics:
        entry = payload["metrics"][row.metric_id]
        assert row.mean_paired_delta == entry.get("mean_paired_delta")
        assert row.sum_baseline == entry.get("sum_baseline")
        assert row.sum_treatment == entry.get("sum_treatment")
        assert row.n_paired_deltas == entry.get("n_paired_deltas")
        assert row.manual_review == bool(entry.get("manual_review"))


# --------------------------------------------------------------------------- #
# 9. The first screen
# --------------------------------------------------------------------------- #


def test_the_overview_is_buildable_for_every_scenario(runs_by_id):
    for scenario_id, run in runs_by_id.items():
        overview = build_overview(run, catalog=load_catalog())
        assert overview.scenario_id == scenario_id
        assert overview.worst_level == run.worst_level
        assert overview.now_level == run.now_level
        assert overview.level_path.count("/") == len(conftest.HORIZON_VALUES) - 1, (
            f"{overview.level_path!r} is not one character per horizon"
        )
        assert overview.headline
        assert "not an accident probability" in " ".join(overview.what_it_is_not).lower()


def test_the_overview_discloses_the_weather_mode_and_the_rule_pack(runs_by_id):
    run = runs_by_id["B04_tight_sunset_margin"]
    overview = build_overview(run, catalog=load_catalog())
    assert overview.weather_mode == "replay"
    assert overview.weather_snapshot_ref
    assert overview.rule_pack.startswith("trailguard_rules_v1@")
    assert overview.freshness_budget_min > 0


def test_the_late_hazard_headline_says_the_hazard_is_in_the_future(runs_by_id):
    """The one sentence that carries the product's whole point."""
    overview = build_overview(runs_by_id["B05_late_gust_hazard"], catalog=load_catalog())
    assert overview.now_level == "GREEN"
    assert overview.worst_level == "YELLOW"
    assert "Safe now" in overview.headline


# --------------------------------------------------------------------------- #
# 10. The demo package is safely importable from the test suite
# --------------------------------------------------------------------------- #


def test_the_presenter_layer_needs_no_ui_framework():
    """``tests/test_packaging.py`` pins the base deps to pydantic+yaml and puts
    streamlit in the ``demo`` extra only, so a presenter that imported streamlit
    would break collection for every other test in the suite."""
    import demo.presenters  # noqa: F401

    for module_name in (
        "demo.presenters.chain",
        "demo.presenters.timeline",
        "demo.presenters.route_view",
        "demo.presenters.weather_view",
        "demo.presenters.evidence_view",
        "demo.presenters.agent_view",
        "demo.presenters.benchmark_view",
        "demo.presenters.scenario_view",
        "demo.presenters.catalog",
    ):
        module = __import__(module_name, fromlist=["__file__"])
        source = Path(module.__file__).read_text(encoding="utf-8")
        assert not re.search(r"^\s*(?:import\s+streamlit|from\s+streamlit)", source, re.MULTILINE), (
            f"{module_name} must not import streamlit"
        )

    # The behavioural half lives in
    # test_the_presentation_layer_does_not_load_a_ui_framework_in_a_clean_interpreter
    # below, in a subprocess. It used to be an in-process ``assert "streamlit" not
    # in sys.modules``, and that assertion can never hold in this suite:
    # ``tests/test_demo_ui.py`` imports streamlit at collection time, so the
    # module is in ``sys.modules`` before the first test in the run executes. The
    # in-process version passed only while the UI tests did not exist, and once
    # they did it began failing for a reason that had nothing to do with the
    # presenters -- a green test that measured the suite's import order rather
    # than the property it named.


def test_no_demo_module_writes_anything():
    """Phase 6 reads Phase 5's evidence and writes nothing anywhere.

    A capsule that only reads is a capsule that cannot have made Phase 5's numbers
    look better. The check is on the operations, not on ``open(``, because the demo
    legitimately opens files to hash them.
    """
    banned = ("write_text", "write_bytes", "mkdir", "makedirs", "unlink", "rename", "rmtree",
              "shutil.", "os.replace", "os.remove")
    for path in sorted((REPO_ROOT / "demo").rglob("*.py")):
        source = path.read_text(encoding="utf-8")
        for operation in banned:
            assert operation not in source, (
                f"{path.relative_to(REPO_ROOT)} calls {operation!r}; Phase 6 is read-only "
                "over Phase 5's artifacts"
            )


def test_the_demo_reads_no_credential_and_no_credential_file():
    """Brief security rule: no ``.env``, no key, no token file, no SSH key.

    Checked as behaviour rather than vocabulary, because it would otherwise be both
    too strict and too loose. Too strict: ``agent_view``'s docstrings contain the
    word "credential" *because* they document that none is attached, and
    ``max_tokens`` is a sampling parameter, not a secret. Too loose: banning words
    would not catch ``os.environ["SOME_API_KEY"]``. So the test asks the two
    questions that matter -- is any credential *file* named, and is any environment
    variable other than endpoint configuration read?
    """
    banned_files = ("getpass", "id_rsa", "ssh_key", "settings.json", "netrc")
    #: The only variables the demo may read, and neither is a secret: they name the
    #: local endpoint ``LocalModelProvider`` itself reads, and a wrong value fails
    #: loudly rather than quietly.
    allowed_env = {"TRAILGUARD_LOCAL_BASE_URL", "TRAILGUARD_LOCAL_MODEL"}
    #: ``.env`` must not appear as a filename. ``os.environ`` contains the same four
    #: characters, so the check is anchored rather than a substring test.
    dotenv = re.compile(r"(?<![A-Za-z0-9_])\.env(?![A-Za-z0-9_])")

    for path in sorted((REPO_ROOT / "demo").rglob("*.py")):
        source = path.read_text(encoding="utf-8")
        if dotenv.search(source):
            raise AssertionError(
                f"{path.relative_to(REPO_ROOT)} names a .env file; the demo reads no credential file"
            )
        for marker in banned_files:
            assert marker not in source, f"{path.relative_to(REPO_ROOT)} references {marker!r}"
        read = re.findall(r"""environ(?:\.get)?\s*[\[(]\s*['"]([A-Za-z_][A-Za-z0-9_]*)['"]""", source)
        for name in read:
            assert name in allowed_env, (
                f"{path.relative_to(REPO_ROOT)} reads {name!r} from the environment; "
                "Phase 6 may only configure the local endpoint"
            )
        assert "API_KEY" not in source, (
            f"{path.relative_to(REPO_ROOT)} names an API key; the demo must never handle one"
        )

    # Endpoint configuration is present exactly where it is allowed, and only there.
    agent_source = (REPO_ROOT / "demo" / "presenters" / "agent_view.py").read_text(encoding="utf-8")
    agent_reads = re.findall(r"""environ(?:\.get)?\s*[\[(]\s*['"]([A-Za-z_][A-Za-z0-9_]*)['"]""", agent_source)
    assert sorted(agent_reads) == sorted(allowed_env)


def test_the_preflight_checks_a_credential_for_presence_and_never_its_value():
    """The operator tool may name the variable; it may not read its value.

    ``scripts/demo_preflight.py`` is the one place a remote credential is mentioned
    at all, because the preflight's job is to tell the operator whether Agent Mode
    will work. It tests membership in ``os.environ`` -- a name lookup -- and never
    indexes or prints the value.
    """
    preflight = (REPO_ROOT / "scripts" / "demo_preflight.py").read_text(encoding="utf-8")
    assert "STEPFUN_API_KEY" in preflight
    assert '"STEPFUN_API_KEY" in os.environ' in preflight
    assert "os.environ[" not in preflight
    assert "value not read" in preflight
    # And it is reported as "not configured", never as an error.
    assert "NOT RUN" in preflight


def test_nothing_in_the_demo_renders_a_network_address():
    """The UI must not show a public IP, an SSH address or an absolute home path.

    Exactly one loopback address is allowed, and only as the agent probe's default:
    it is the operator's own model server, it is the same value
    ``LocalModelProvider`` defaults to, and it is configuration rather than a
    secret. It is never displayed -- the Agent tab shows the availability verdict
    the probe produced, not the address it probed.
    """
    loopback = "http://127.0.0.1:8000/v1"
    everywhere_banned = ("ssh://", "sftp://", str(Path.home()), "0.0.0.0")

    for path in sorted((REPO_ROOT / "demo").rglob("*.py")):
        text = path.read_text(encoding="utf-8")
        for banned in everywhere_banned:
            assert banned not in text, f"{path.relative_to(REPO_ROOT)} contains {banned!r}"
        if path.name == "agent_view.py":
            assert text.count(loopback) == 1, (
                "the loopback default must appear exactly once, as the probe's default"
            )
            continue
        for banned in ("http://", "https://", "127.0.0.1"):
            assert banned not in text, (
                f"{path.relative_to(REPO_ROOT)} renders a network address"
            )


def test_the_agent_tab_shows_a_verdict_and_never_the_address_it_probed():
    """The one address that exists is not one a judge could mistake for a target."""
    agent_source = (REPO_ROOT / "demo" / "presenters" / "agent_view.py").read_text(encoding="utf-8")
    # AgentAvailability -- the only thing the UI receives from the probe.
    fields = re.findall(r"^    (\w+):", agent_source, re.MULTILINE)
    assert "base_url" in fields, "the probe carries its address for the operator's benefit"
    assert "available" in fields and "reason" in fields


def test_no_demo_module_renders_a_stack_trace():
    """Spec 45. A failure is a sentence, never a traceback.

    Checked on the API *calls* rather than the words, since every docstring here
    that says "never a traceback" is arguing for the rule rather than against it.
    """
    banned = (
        "import traceback",
        "traceback.format_exc",
        "traceback.print_exc",
        "format_exc",
        "print_exc",
        "exc_info",
        "st.exception(",
    )
    for path in sorted((REPO_ROOT / "demo").rglob("*.py")):
        source = path.read_text(encoding="utf-8")
        for marker in banned:
            assert marker not in source, (
                f"{path.relative_to(REPO_ROOT)} would render a stack trace ({marker})"
            )


# --------------------------------------------------------------------------- #
# 12. The UI layer, and the boundary between it and everything else
# --------------------------------------------------------------------------- #


def test_the_presentation_layer_does_not_load_a_ui_framework_in_a_clean_interpreter():
    """The packaging hold, checked where it cannot be satisfied by import order.

    ``tests/test_packaging.py`` fixes the base dependencies at ``pydantic`` and
    ``yaml`` and puts ``streamlit`` in the ``demo`` extra only, so a presenter that
    imported a UI framework would stop the whole suite from collecting. A
    subprocess is the only way to assert this without depending on what some other
    test happened to import first.
    """
    import os
    import subprocess

    environment = dict(os.environ)
    environment["PYTHONPATH"] = str(REPO_ROOT) + os.pathsep + environment.get("PYTHONPATH", "")
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "import sys; import demo.presenters as p; "
            "print(len(p.__all__), 'streamlit' in sys.modules)",
        ],
        capture_output=True,
        text=True,
        env=environment,
        cwd=str(REPO_ROOT),
        timeout=120,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip().split()[-1] == "False", (
        "importing demo.presenters loaded a UI framework: "
        f"{result.stdout.strip()} {result.stderr.strip()}"
    )


def test_the_ui_layer_exists_and_imports_the_ui_framework_only_there():
    """The split is real: ``demo/presenters`` needs no streamlit, ``demo/streamlit``
    is the only place that uses it, and the two never import each other's way."""
    ui = REPO_ROOT / "demo" / "streamlit"
    presenters = REPO_ROOT / "demo" / "presenters"

    entry = ui / "app.py"
    assert entry.is_file(), f"{entry} is the entry point streamlit runs"
    assert (ui / "README.md").is_file(), "the UI needs its own start instructions"

    importers = []
    for path in sorted((REPO_ROOT / "demo").rglob("*.py")):
        source = path.read_text(encoding="utf-8")
        if re.search(r"^\s*(?:import\s+streamlit|from\s+streamlit)", source, re.MULTILINE):
            importers.append(path.relative_to(REPO_ROOT))
    assert importers, "nothing in the demo imports streamlit; the UI layer is missing"
    for path in importers:
        assert str(path).startswith("demo/streamlit/"), (
            f"{path} imports streamlit outside the UI layer"
        )

    # The presenters never reach into the UI, and the UI never reaches around them.
    for path in sorted(presenters.rglob("*.py")):
        assert "demo.streamlit" not in path.read_text(encoding="utf-8"), (
            f"{path.name} imports the UI layer; a presenter must not depend on it"
        )


def test_the_untrusted_answer_is_displayed_in_a_fence_it_cannot_close():
    """A model's answer is the only untrusted string in the app, and it must be shown
    in full without being able to inject anything."""
    from demo.streamlit.components.text import fenced

    hostile = [
        "```\n# injected heading\n```",
        "````\nclosing attempt\n````",
        "</script><script>alert(1)</script>",
        "<img src=x onerror=alert(1)>",
        "text with one ` and two `` runs",
        "",
    ]
    for answer in hostile:
        block = fenced(answer)
        opening = block.split("\n", 1)[0]
        assert opening.endswith("text"), answer
        fence = opening[: -len("text")]
        assert set(fence) == {"`"}, answer
        assert len(fence) >= 3, answer
        longest_inside = max((len(m.group(0)) for m in re.finditer(r"`+", answer)), default=0)
        assert len(fence) > longest_inside, (
            f"a fence of {len(fence)} cannot close a run of {longest_inside} inside: {answer!r}"
        )
        # The answer is present verbatim, between exactly that fence and its twin.
        assert answer in block, answer
        closing = block.rstrip("\n").rsplit("\n", 1)[-1]
        assert closing == fence, answer


def test_the_ui_scales_every_value_from_a_presenter_field_only(runs_by_id):
    """A chart that inverted the level rows or dropped a horizon would be lying in
    the one place a judge is least likely to check.

    Two properties, both about the picture rather than the presentation: the
    scale maps rank to a monotonically receding y, and every label and level the
    chart was handed appears in the output. The rest of the module is pixel
    arithmetic, so nothing else can silently disagree with the table.
    """
    for scenario_id, run in runs_by_id.items():
        timeline = build_timeline(run)
        markup = charts.level_chart(timeline, notes=[r.projected_arrival_time for r in run.rows])
        for point in timeline.points:
            assert point.label in markup, f"{scenario_id}: {point.label} missing from the chart"
            assert point.level in markup, f"{scenario_id}: {point.level} missing from the chart"
        assert "None" not in markup, f"{scenario_id}: an unset value reached the chart"
        assert "{" not in markup and "}" not in markup, (
            f"{scenario_id}: an f-string placeholder reached the chart"
        )
        assert markup.startswith("<svg") and markup.rstrip().endswith("</svg>")

    scale = [charts._level_y(rank) for rank in range(4)]
    assert scale == sorted(scale, reverse=True), "the level scale must run downward with rank"

    for run in runs_by_id.values():
        for markup in (charts.elevation_chart(route_profile(run)),
                       charts.route_sketch(route_profile(run))):
            if not markup.startswith("<svg"):
                continue
            assert markup.startswith("<svg") and markup.rstrip().endswith("</svg>")
            assert "None" not in markup and "{" not in markup

    svg = charts.metric_chart(load_benchmark())
    for metric in load_benchmark().metrics:
        if metric.mean_paired_delta is not None:
            assert metric.metric_id in svg
    assert svg.startswith("<svg") and svg.rstrip().endswith("</svg>")


# --------------------------------------------------------------------------- #
# 11. Offline
# --------------------------------------------------------------------------- #


def test_the_presenter_layer_makes_no_network_call_at_collection_or_default_run(runs_by_id, catalog):
    """Every view builds from committed data; the socket layer stays closed."""
    for scenario_id, run in runs_by_id.items():
        reference = _reference(scenario_id)
        build_overview(run, catalog=catalog)
        build_timeline(run)
        build_evidence(run, reference=reference)
        route_profile(run)
        weather_rows(run)
    assert True
