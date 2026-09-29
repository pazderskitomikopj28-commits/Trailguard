"""The UI itself, exercised through Streamlit's own AppTest harness.

Separate from ``test_demo_presenters.py`` and guarded by ``importorskip`` for a
reason: the rest of the suite must run -- and pass -- on a machine with nothing but
``pydantic`` and ``yaml`` installed, because that is the state the brief requires
the presentation layer to survive. ``tests/test_packaging.py`` fixes those base
dependencies and puts ``streamlit`` in the ``demo`` extra only, so a test file that
imported streamlit unconditionally would break collection for everything else. When
streamlit *is* installed, this file turns the demo from "it starts" into "it says
the right things".

What it checks that nothing else can: that the screens a judge sees are the
screens the presenters built, that the failure paths render as sentences, and that
nothing on any screen carries a stack trace, an address or an absolute path.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

import pytest

pytest.importorskip("streamlit", reason="the UI layer needs the demo extra")

from streamlit.testing.v1 import AppTest  # noqa: E402

import streamlit as st  # noqa: E402

import tests.conftest as conftest  # noqa: E402
from demo.presenters import agent_view  # noqa: E402
from demo.presenters.agent_view import AgentAvailability, AgentRunView  # noqa: E402

REPO_ROOT = conftest.REPO_ROOT
APP = REPO_ROOT / "demo" / "streamlit" / "app.py"

TAB_LABELS = ["Overview", "Risk Timeline", "Evidence", "Benchmark", "Architecture"]

#: Words that must never appear on a screen, whatever went wrong.
NEVER_ON_SCREEN = (
    "Traceback",
    "File \"",
    "127.0.0.1",
    "0.0.0.0",
    "ssh://",
    str(Path.home()),
    "STEPFUN_API_KEY",
)

AGENT_QUESTION = "Who reads the five tools?"

#: The panel the Agent switch draws when the endpoint is down, exactly as the badge
#: renders it. Searching for the phrase alone would also match the Architecture
#: tab's explanation of the policy, which mentions the words without showing the
#: state.
UNREACHABLE_PANEL = "Unavailable &#183; Local Agent unavailable"


@pytest.fixture
def app() -> AppTest:
    """The demo, started fresh for each test.

    Function-scoped on purpose, and the reason is worth writing down. As a
    module-scoped fixture it ran during the *first* test's setup, which is before
    the autouse ``no_external_network`` fixture in ``conftest.py`` patches the
    socket layer -- so the script executed with a live wire. Its ``@st.cache_data``
    probe then recorded the real local vLLM endpoint as reachable, and that cached
    answer was still in place when the agent tests ran with the wire closed. The
    consequence was worse than a slow suite: the test that exists to prove an
    unreachable endpoint is reported never saw an unreachable endpoint, and the
    suite made a real HTTP call during ``pytest``, which spec 13 forbids.

    A fresh ``AppTest`` per test costs about a second and removes both problems:
    the socket layer is closed before the first line of the script runs, so what
    the demo builds is what a judge on a laptop with the model down would see.
    """
    at = AppTest.from_file(str(APP), default_timeout=240)
    at.run()
    return at


@pytest.fixture(autouse=True)
def _no_cached_probe():
    """Every test starts with an empty streamlit cache, and leaves one behind.

    ``app._probe`` is ``@st.cache_data`` and that cache is process-wide, so
    whichever test runs first sets the availability answer for every test after
    it. Without this, a test that stubs a down endpoint would leave the demo
    believing the model is up for the rest of the suite -- or the reverse, which is
    how the "unreachable endpoint" test spent its first version asserting against
    a server that was up.
    """
    st.cache_data.clear()
    yield
    st.cache_data.clear()


def _agent_screen(monkeypatch, availability: AgentAvailability) -> AppTest:
    """The demo with the agent switch turned on and a chosen probe answer.

    The stub goes into ``agent_view`` rather than into the app module, because
    AppTest executes the script from source: the app is not in ``sys.modules``, so
    patching it would do nothing. Everything the app imported from the presenters
    still resolves through the module this patches.
    """
    monkeypatch.setattr(agent_view, "probe_local_agent", lambda **kwargs: availability)
    at = AppTest.from_file(str(APP), default_timeout=240)
    at.run()
    chooser = next(radio for radio in at.radio if radio.label == AGENT_QUESTION)
    return chooser.set_value(chooser.options[1]).run()


def _text(app: AppTest) -> str:
    """Everything rendered as text, gathered so one helper can search all of it."""
    parts = [element.value or "" for element in app.markdown]
    parts += [element.value or "" for element in app.caption]
    parts += [element.value or "" for element in app.error]
    parts += [element.value or "" for element in app.warning]
    parts += [element.value or "" for element in app.info]
    # A metric is two strings, and both matter: the label says what it is and the
    # value is the number or sentence the viewer actually reads.
    for element in app.metric:
        parts.append(element.label or "")
        parts.append(element.value or "")
    return "\n".join(parts)


# --------------------------------------------------------------------------- #
# The screens exist and say what they are supposed to say
# --------------------------------------------------------------------------- #


def test_the_demo_renders_without_raising(app):
    assert not app.exception, _text(app)


def test_there_are_exactly_five_top_level_tabs(app):
    assert [tab.label for tab in app.tabs] == TAB_LABELS


def test_the_overview_leads_with_the_scenarios_own_worst_level(app):
    """The headline is the core's verdict for the default scenario, not a slogan."""
    from demo.presenters.catalog import load_catalog

    entry = load_catalog().featured[0]
    text = _text(app)
    assert "worst level across the next two hours" in text
    assert entry.worst_level in text
    assert entry.scenario_id in text


def test_the_soft_score_is_never_presented_as_a_probability(app):
    """The spec's one numerical prohibition, checked where the number appears."""
    assert "not a probability of harm" in _text(app)


def test_the_overview_says_what_the_demo_is_not(app):
    text = _text(app)
    assert "What it is not" in text
    assert "What it does" in text


# --------------------------------------------------------------------------- #
# The benchmark tells both sides
# --------------------------------------------------------------------------- #


def test_the_benchmark_tab_shows_the_mixed_finding_rather_than_the_wins(app):
    text = _text(app)
    assert "The finding was MIXED, and the mixed result is the point" in text

    improved = {metric.label: metric.value for metric in app.metric}
    assert improved.get("moved the right way") == "4"
    assert improved.get("moved the wrong way") == "4"
    assert improved.get("did not move") == "10"


def test_the_benchmark_tab_carries_the_cost_beside_the_improvement(app):
    """The narrative boundary, on the screen rather than only in the docs.

    The sanctioned sentence is only ever legitimate with its cost half attached to
    it, so the two are asserted as one paragraph: a screen that claimed the
    improvement and left the reliability and context-budget cost out would be
    passing the counts test above while telling a judge the wrong story.
    """
    text = _text(app)
    assert (
        "TrailGuard changes the agent's orchestration behavior and increases proactive "
        "future-risk investigation in several benchmark dimensions."
    ) in text
    assert (
        "On the current local 30B model and 16K context window, the longer orchestration "
        "chain also introduces substantial reliability and context-budget costs."
    ) in text
    assert "TrailGuard makes Qwen safer" not in text


def test_the_benchmark_tab_shows_all_eighteen_metrics(app):
    """One dataframe is the 18-row metric table; both conditions are beside each."""
    wide = [frame for frame in app.dataframe if len(frame.value) == 18]
    assert wide, "the 18 frozen metrics are not on screen"
    columns = {column for frame in wide for column in frame.value.columns}
    assert {"baseline", "treatment", "mean paired delta", "direction"} <= columns


def test_the_benchmark_tab_quotes_the_completion_rate_as_a_percentage(app):
    """Brief 28 names the completion rate, so the screen must not make a judge divide.

    The percentage and the two sums it came from are asserted together, on the same
    screen: a percentage with the counts hidden beside it would be a number a reader
    has to take on trust, which is the one thing this tab exists to avoid.
    """
    text = _text(app)
    assert "Completion rate." in text
    assert "20 of 32 runs (62.5%)" in text
    assert "2 of 32 (6.25%)" in text

    # And the same figure in the table beside the sums it came from, on the row that
    # carries them -- so a judge who misses the sentence can still find it without
    # being handed a number they cannot check.
    wide = [frame for frame in app.dataframe if len(frame.value) == 18]
    assert wide, "the 18 frozen metrics are not on screen"
    frame = wide[0].value
    assert "rate of its own runs" in frame.columns
    assert frame.loc[frame["metric"] == "M1_completion_rate", "rate of its own runs"].iloc[0] == (
        "62.5% -> 6.25%"
    )
    # A metric whose denominator is not runs gets no rate, rather than a meaningless one.
    horizon = frame[frame["metric"] == "M5_horizon_coverage"]
    assert horizon["rate of its own runs"].iloc[0] == "--"


def test_the_raw_json_is_behind_a_disclosure_and_not_dumped_on_the_screen(app):
    """Brief 26 in both halves at once.

    The default must be a table rather than a JSON dump, and the raw bytes must
    still be one click away. Asserting only that the label exists would pass on a
    tab that offered nothing, which is how this requirement was first missed -- so
    the body is checked too, for the two properties that make it safe: it sits
    behind the disclosure's subtree, and it is fenced, because a path and a hash
    rendered as live markdown would be a screen a judge cannot trust.
    """
    labels = [expander.label for expander in app.expander]
    disclosure = [label for label in labels if "View raw JSON" in label]
    assert disclosure, f"no raw-JSON disclosure on the Evidence tab: {labels}"

    nested = [
        (element.value or "")
        for element in app.expander
        if "View raw JSON" in element.label
        for element in element.markdown
        if '"scenario_file"' in (element.value or "")
    ]
    assert nested, "the disclosure is offered, but the frozen reference is not behind it"
    body = nested[0]
    assert body.startswith("```json"), "the frozen reference JSON is not inside a code fence"
    assert body.rstrip().endswith("```"), "the fence is left open"


#: Phrases that would mean some screen invented one number for "how good was
#: TrailGuard". The benchmark tab denies all of these in a single sentence, so a
#: plain phrase ban is self-defeating -- it fails on the honest denial. Instead,
#: every occurrence has to sit behind a negator. The bare word "overall" is
#: deliberately absent: the device-quality payload on the Evidence tab carries a
#: field of that name, and a field name is not a verdict.
SCORE_PHRASES = (
    "composite score",
    "overall score",
    "net score",
    "trailguard score",
    "total score",
    "final score",
    "aggregate score",
    "weighted score",
    "summary score",
)

#: Words that turn one of those phrases into a refusal rather than an offer.
NEGATORS = ("no ", "not ", "never ", "neither ", "nor ", "without ", "none ")


def test_no_screen_offers_a_composite_score(app):
    """No screen computes a single number for the whole run.

    The Phase 5 evidence itself records that no composite score exists, and the
    benchmark tab says so out loud. What this test forbids is the other kind of
    sentence: one that presents a rolled-up figure as if the evidence had produced
    it. Both are checked by reading every occurrence and asking which it is.
    """
    text = _text(app).lower().replace("&#x27;", "'")
    mentions = 0
    for phrase in SCORE_PHRASES:
        index = text.find(phrase)
        while index != -1:
            window = text[max(0, index - 24):index]
            assert any(word in window for word in NEGATORS), (
                f"{phrase!r} is offered on a screen rather than denied: "
                f"...{text[max(0, index - 90):index + 90]}..."
            )
            mentions += 1
            index = text.find(phrase, index + len(phrase))
    # The denial has to still be there, or the loop above only proves absence.
    assert mentions >= 2, "the benchmark tab no longer says what it refuses to compute"


# --------------------------------------------------------------------------- #
# Nothing on screen is a stack trace, an address or a secret
# --------------------------------------------------------------------------- #


def test_no_screen_renders_a_traceback_an_address_or_a_path(app):
    text = _text(app)
    for banned in NEVER_ON_SCREEN:
        assert banned not in text, f"a screen renders {banned!r}"


def test_the_weather_mode_is_labelled_and_replay_is_the_default(app):
    modes = {radio.label: radio.options for radio in app.radio if radio.label == "weather mode"}
    assert modes, "the weather mode chooser is not in the sidebar"
    options = modes["weather mode"]
    assert options[0].lower().startswith("replay")
    assert "live" in options[1].lower()
    text = _text(app)
    assert "weather mode **replay**" in text or "weather mode <strong>replay</strong>" in text


# --------------------------------------------------------------------------- #
# The agent layer, and its honest failure
# --------------------------------------------------------------------------- #


def test_the_agent_switch_is_a_choice_not_a_dependency(app):
    labels = [radio.label for radio in app.radio]
    assert AGENT_QUESTION in labels


def test_an_unreachable_endpoint_is_reported_and_never_substituted(app):
    """The single most important behaviour in the UI.

    The suite closes the socket layer before the script's first line runs, so the
    endpoint is unreachable whenever this test runs. Choosing the local agent must
    produce the fixed sentence and nothing else -- no scripted answer, no replay
    verdict dressed up as a model's, and not even a button that offers to try.
    """
    chooser = next(radio for radio in app.radio if radio.label == AGENT_QUESTION)
    app = chooser.set_value(chooser.options[1]).run()

    assert not app.exception
    text = _text(app)
    assert UNREACHABLE_PANEL in text
    # The panel says what the rest of the screen still is, and it offers no
    # button, because a demo that retries a dead endpoint looks broken.
    assert "does not depend on the model" in text
    assert "Run the agent" not in text
    # And the deterministic verdict is still on screen above it.
    assert "worst level across the next two hours" in text


def test_the_deterministic_verdict_survives_the_agent_being_down(app):
    """Switching to a down agent must not remove or change the core's answer."""
    before = _text(app)
    chooser = next(radio for radio in app.radio if radio.label == AGENT_QUESTION)
    app = chooser.set_value(chooser.options[1]).run()
    after = _text(app)

    verdicts = re.findall(r"worst level across the next two hours(.{0,400})", after, re.S)
    assert verdicts
    assert verdicts[0][:200] in before


def test_switching_scenario_discards_the_agents_answer(app):
    """One model's answer must never be shown against another scenario's data.

    The agent panel keeps its answer in ``session_state`` and the demo resets it
    when the scenario changes. That reset is the mechanism the brief asks for, and
    it is testable without a model: plant an answer against the featured scenario,
    switch the scenario radio, and check the plant is gone from the session *and*
    that the screen is now the other scenario's.
    """
    from demo.presenters.catalog import load_catalog

    catalog = load_catalog()
    featured = catalog.featured[0]
    control = catalog.control
    other = f"control: {control.title} ({control.worst_level})"

    app.session_state["agent_for"] = featured.scenario_id
    app.session_state["agent_view"] = "PLANTED ANSWER FOR THE WRONG SCENARIO"
    app.session_state["agent_error"] = None
    app.run()
    # The plant survives a rerun, which is what makes the rest of this a test.
    assert app.session_state["agent_view"] == "PLANTED ANSWER FOR THE WRONG SCENARIO"

    chooser = next(radio for radio in app.radio if radio.label == "scenario")
    assert other in chooser.options
    app = chooser.set_value(other).run()

    assert not app.exception
    assert app.session_state["agent_view"] is None
    assert app.session_state["agent_for"] == control.scenario_id

    text = _text(app)
    assert control.scenario_id in text
    assert featured.scenario_id not in text
    assert "PLANTED ANSWER" not in text


def test_a_dead_endpoint_named_its_address_only_to_the_operator(monkeypatch):
    """The down-endpoint sentence is complete without the address that failed.

    ``probe_local_agent`` writes the URL it asked into ``detail``, which is right
    for ``scripts/demo_preflight.py`` and wrong for this screen. This test hands
    the demo a failure whose detail contains both the loopback URL and the port,
    then checks the judge reads a sentence about the health endpoint and never an
    address -- while the deterministic verdict above it is untouched.
    """
    availability = AgentAvailability(
        available=False,
        reason="the health endpoint is not reachable",
        detail="http://127.0.0.1:8000/health -> URLError: connection refused",
        model="qwen3-vl-30b-a3b",
        base_url="http://127.0.0.1:8000/v1",
    )
    app = _agent_screen(monkeypatch, availability)

    assert not app.exception
    text = _text(app)
    assert UNREACHABLE_PANEL in text
    assert availability.reason in text
    assert "does not depend on the model" in text
    for banned in ("http://", "127.0.0.1", ":8000", "URLError"):
        assert banned not in text, f"the screen shows {banned!r}"
    assert "worst level across the next two hours" in text


def test_a_live_endpoint_offers_the_run_and_says_which_model_will_call(monkeypatch):
    """The other half of the switch: when the model is up, the panel invites a run.

    Not a second path through the demo, just proof that the switch is a choice --
    the same tab offers the deterministic core first and only then a real run,
    with the model named and the frozen sampling disclosed.
    """
    availability = AgentAvailability(
        available=True,
        reason="reachable and serving the expected model",
        detail="",
        model="qwen3-vl-30b-a3b",
        base_url="http://127.0.0.1:8000/v1",
        max_model_len=16384,
    )
    app = _agent_screen(monkeypatch, availability)

    assert not app.exception
    text = _text(app)
    assert availability.model in text
    assert "temperature" in text and "max_steps" in text
    assert [button.label for button in app.button] == [
        "Run the agent on B05_late_gust_hazard"
    ]
    assert UNREACHABLE_PANEL not in text
    assert "worst level across the next two hours" in text


#: One finished run, in the shape ``present_agent_run`` returns. The agent checked
#: only the NOW horizon and its answer carried no horizon-level claim, which is
#: what the local model actually did on this laptop when this phase was written --
#: so the panel under test is the panel a judge would have seen.
A_RUN_THAT_CHECKED_ONE_HORIZON = AgentRunView(
    scenario_id="B05_late_gust_hazard",
    run_id="demo-walkthrough",
    model_name="qwen3-vl-30b-a3b",
    provider="local",
    skill_loaded=True,
    skill_version="1.0.0",
    stop_reason="completed",
    stop_reason_text="finished with an answer",
    steps=12,
    calls=[],
    horizons_checked=["NOW"],
    triggered_rules=[],
    core_risk_level="GREEN",
    core_confidence=0.54375,
    weather_source="synthetic weather fixture, not live data",
    freshness_checks=["NOW: weather age 60.0 min (within freshness budget)"],
    missing_data=[],
    sampling_params={"temperature": 0.0, "max_tokens": 2048, "max_steps": 16},
    answer="Now for the +30min horizon.",
    claimed_levels={},
    claimed_rules=[],
    worst_claimed=None,
    contradicted=False,
    contradiction_note=(
        "The answer gave no horizon-level claim the deterministic scanner could "
        "read, so no comparison is possible. Nothing is implied either way."
    ),
    hedges=[],
    probability_claims=[],
    negated=[],
    conflicting=[],
    timing_note=["153.4 s wall clock"],
)


def test_a_finished_run_shows_what_the_agent_did_and_whose_verdict_is_whose(monkeypatch):
    """The comparison panel, and the one thing it must not leave ambiguous.

    Both numbers on the right are "the core's", but they answer different
    questions: one is the worst level among the horizons the agent happened to
    assess, the other -- the headline above the tab -- is the worst over all four.
    A run that checks one horizon makes them disagree, and the screen has to say
    so rather than let a judge read it as the demo contradicting itself.
    """
    app = _agent_screen(
        monkeypatch,
        AgentAvailability(
            available=True,
            reason="reachable and serving the expected model",
            detail="",
            model="qwen3-vl-30b-a3b",
            base_url="http://127.0.0.1:8000/v1",
            max_model_len=16384,
        ),
    )
    app.session_state["agent_for"] = "B05_late_gust_hazard"
    app.session_state["agent_view"] = A_RUN_THAT_CHECKED_ONE_HORIZON
    app.session_state["agent_error"] = None
    app.run()

    assert not app.exception
    text = _text(app)
    # The run's own facts: eleven tool calls' worth of metadata, and the real cost.
    assert "finished with an answer" in text
    assert "153.4 s wall clock" in text
    assert "qwen3-vl-30b-a3b" in text
    # The answer is shown verbatim, inside a fence it cannot close.
    assert "Now for the +30min horizon." in text
    # No claim was readable, so nothing is claimed about agreement.
    assert "no horizon-level claim" in text
    assert "contradicted the core" not in text
    # And the ambiguity is named, not left to the judge to resolve.
    assert "'Core' above is the worst level the core returned to the agent" in text
    assert "the verdict the safety core actually owns" in text


@pytest.mark.parametrize(
    "entry",
    ["B05_late_gust_hazard", "B04_tight_sunset_margin", "B10_compound_soft_pair"],
)
def test_no_tab_fails_to_draw_for_any_featured_scenario(app, entry):
    """The per-tab router keeps a failure local -- and it must never be needed.

    ``app.py`` wraps each tab in its own ``try`` so that one broken tab cannot take
    the demo down. That safety net is also a way for a tab to be quietly broken for
    weeks: the screen renders a red box, and every assertion about content still
    passes because nothing asserted that content. This test walks the three
    featured scenarios -- future weather, hard rule, compound -- and requires that
    no tab used the net at all.
    """
    from demo.presenters.catalog import load_catalog

    chosen = next(
        candidate
        for candidate in load_catalog().featured
        if candidate.scenario_id == entry
    )
    # The radio labels by the scenario's title and worst level, not its id.
    label = f"{chosen.title} ({chosen.worst_level})"
    chooser = next(radio for radio in app.radio if radio.label == "scenario")
    assert label in chooser.options
    app = chooser.set_value(label).run()

    text = _text(app)
    assert "could not be drawn" not in text
    assert "Unavailable" not in text
    # And the screen is really showing this scenario's own numbers.
    assert entry in text
    assert chosen.title in text
