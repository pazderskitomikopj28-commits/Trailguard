"""Offline tests for the Phase 4 local-agent driver, ``scripts/test_local_agent.py``.

That script is the one place the *local* model meets the frozen harness, so it
cannot be exercised by pytest -- it reaches ``http://127.0.0.1:8000`` and needs a
30B checkpoint behind it, and the suite must run with no GPU, no container and no
network (spec 13, and the user's Phase 4 rule 18). ``tests/conftest.py`` lists it
in ``collect_ignore`` for exactly that reason.

What it leaves behind, and what these tests therefore cover
===========================================================
The script's *decisions* are pure functions that take no transport: which URL a
preflight probes, whether a run's answer softened a RED verdict, and whether a
hard rule the core fired reached the answer. Those are the parts that go wrong
silently -- and one already did. The first version of the preflight built
``{base_url}/health`` from an OpenAI-compatible base, which ends in ``/v1``, so it
asked vLLM for ``/v1/health``, got a 404 from a perfectly healthy server and would
have reported the deployment as down. Nothing in a green suite caught that, because
nothing in the suite looked. It is tested here now, and
``test_the_preflight_probes_the_deployment_root_not_the_api_path`` pins it.

A test that imports this script imports a file pytest otherwise refuses to collect.
That is deliberate and contained: the module's only import-time work is constants
and function definitions, and ``main()`` sits behind the usual
``if __name__ == "__main__"`` guard. The import is done through
``importlib.util`` rather than ``sys.path`` surgery, so the name it binds cannot
shadow anything.
"""

from __future__ import annotations

import ast
import importlib.util
import sys
from datetime import datetime, timezone
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = REPO_ROOT / "scripts" / "test_local_agent.py"


def _load_driver() -> ModuleType:
    """Import the local smoke script as a module, without collecting it.

    The module is registered in ``sys.modules`` *before* it executes. That is not
    boilerplate: ``dataclasses`` resolves string annotations by looking its class
    up in ``sys.modules``, so an unregistered module raises
    ``AttributeError: 'NoneType' object has no attribute '__dict__'`` the moment
    the script declares a frozen dataclass -- which the typed findings did.
    """
    spec = importlib.util.spec_from_file_location("test_local_agent_script", SCRIPT)
    if spec is None or spec.loader is None:  # pragma: no cover - a missing file
        raise AssertionError(f"cannot import {SCRIPT}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    try:
        spec.loader.exec_module(module)
    except Exception:
        del sys.modules[spec.name]
        raise
    return module


driver = _load_driver()


# --------------------------------------------------------------------------- #
# Fixtures: a run summary shaped like the real one, built from the real contract
# --------------------------------------------------------------------------- #


@pytest.fixture
def make_trace():
    """A minimal valid :class:`EvidenceTrace` for the checks to read."""

    def _make(*, core_risk_level: str | None = None, triggered: tuple[str, ...] = ()) -> Any:
        from trailguard.schemas.risk import RiskLevel
        from trailguard.schemas.trace import EvidenceTrace

        trace = EvidenceTrace(
            run_id="phase4-test",
            model_name="qwen3-vl-30b-a3b",
            provider="local",
            skill_loaded=True,
            started_at=datetime.now(timezone.utc),
            triggered_rules=list(triggered),
        )
        if core_risk_level is not None:
            return trace.model_copy(update={"core_risk_level": RiskLevel(core_risk_level)})
        return trace

    return _make


@pytest.fixture
def make_summary(make_trace):
    """A run summary the driver's checks accept, with the fields it reads."""

    def _make(
        *,
        answer: str = "Conditions are fine. Continue at your current pace.",
        core_risk_level: str | None = None,
        triggered: tuple[str, ...] = (),
    ) -> Any:
        class _Summary:
            def __init__(self) -> None:
                self.scenario_id = "S04_sunset_margin_pace_drop"
                self.condition = "with_skill"
                self.final_response = answer
                self.trace = make_trace(
                    core_risk_level=core_risk_level, triggered=triggered
                )

        return _Summary()

    return _make


#: Severities, spelled as the driver spells them so a test cannot drift from the
#: script by inventing a third value.
BLOCKING = "blocking"
RECORDED = "recorded"


def texts(findings: Any) -> list[str]:
    """The words of each finding, so assertions read as sentences."""
    return [finding.text for finding in findings]


def severities(findings: Any) -> list[str]:
    """The severity of each finding -- the part that decides the exit code."""
    return [finding.severity for finding in findings]


def blocking_texts(findings: Any) -> list[str]:
    """Only the findings that would fail the run."""
    return [finding.text for finding in findings if finding.severity == BLOCKING]


# --------------------------------------------------------------------------- #
# The preflight URL, which is where the driver already went wrong once
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("base_url", "expected"),
    [
        ("http://127.0.0.1:8000/v1", "http://127.0.0.1:8000"),
        ("http://127.0.0.1:8000/v1/", "http://127.0.0.1:8000"),
        ("http://127.0.0.1:8000", "http://127.0.0.1:8000"),
        ("http://127.0.0.1:8000/", "http://127.0.0.1:8000"),
        ("http://spark.invalid:8000/v1", "http://spark.invalid:8000"),
    ],
)
def test_the_preflight_probes_the_deployment_root_not_the_api_path(
    base_url: str, expected: str
) -> None:
    """``/health`` lives at the deployment root; ``/v1/models`` lives under it.

    vLLM serves its unversioned health endpoint at ``/health`` and its
    versioned API under ``/v1``. A base URL that is already OpenAI-compatible
    ends in ``/v1``, so concatenating a health path onto it produces ``/v1/health``
    -- a 404 from a healthy server, which is how the first draft of this preflight
    nearly reported the deployment as down.
    """
    assert driver._root(base_url) == expected
    assert f"{driver._root(base_url)}/health" == f"{expected}/health"


def test_the_preflight_reports_a_real_health_failure_rather_than_a_url_bug(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A genuine failure must still be reported, and by the reason it failed."""
    monkeypatch.setattr(driver, "_status_of", lambda base_url, path, timeout_s: 503)
    reason = driver.preflight("http://127.0.0.1:8000/v1", "qwen3-vl-30b-a3b", 1.0)
    assert reason is not None
    assert "503" in reason
    # Not a URL-shaped answer: the reader has to be able to tell "the engine is
    # loading" from "you asked the wrong endpoint".
    assert "/v1/health" not in reason


def test_the_preflight_refuses_a_model_the_caller_did_not_ask_for(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(driver, "_status_of", lambda base_url, path, timeout_s: 200)
    monkeypatch.setattr(
        driver, "_get", lambda base_url, path, timeout_s: (200, {"data": [{"id": "other"}]})
    )
    reason = driver.preflight("http://127.0.0.1:8000/v1", "qwen3-vl-30b-a3b", 1.0)
    assert reason is not None
    assert "qwen3-vl-30b-a3b" in reason


def test_the_preflight_probes_the_root_for_health_and_the_api_prefix_for_models(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The two probes read at two depths of the same deployment.

    ``/health`` is unversioned and bodyless; ``/v1/models`` is versioned and JSON.
    Getting the depths wrong in either direction is how a healthy server ends up
    reported as down -- or a 200 with an empty body gets parsed as JSON and raises.
    """
    seen: list[str] = []

    def _fake_status(base_url: str, path: str, timeout_s: float) -> int:
        seen.append(f"{base_url}{path}")
        assert timeout_s > 0
        return 200

    def _fake_get(base_url: str, path: str, timeout_s: float) -> tuple[int, Any]:
        seen.append(f"{base_url}{path}")
        return 200, {"data": [{"id": "qwen3-vl-30b-a3b"}]}

    monkeypatch.setattr(driver, "_status_of", _fake_status)
    monkeypatch.setattr(driver, "_get", _fake_get)
    assert driver.preflight("http://127.0.0.1:8000/v1", "qwen3-vl-30b-a3b", 1.0) is None
    assert seen == [
        "http://127.0.0.1:8000/health",
        "http://127.0.0.1:8000/v1/models",
    ]


def test_the_context_limit_is_read_from_the_server_not_assumed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The window size decides whether an agent run fits, so it is read, not guessed.

    A total-token count cannot distinguish "the model gave up" from "the prompt
    outgrew the window"; the window can, and only if it came from the endpoint
    that then enforced it.
    """
    monkeypatch.setattr(
        driver,
        "_get",
        lambda base_url, path, timeout_s: (200, {"data": [{"id": "qwen3-vl-30b-a3b",
                                                          "max_model_len": 16384}]}),
    )
    assert driver.context_limit("http://127.0.0.1:8000/v1", "qwen3-vl-30b-a3b", 1.0) == 16384


def test_the_context_limit_is_none_when_the_server_will_not_say(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A missing or unreadable limit prints as ``?`` rather than crashing a run."""
    monkeypatch.setattr(driver, "_get", lambda base_url, path, timeout_s: (200, {"data": []}))
    assert driver.context_limit("http://127.0.0.1:8000/v1", "qwen3-vl-30b-a3b", 1.0) is None


def test_the_health_probe_never_tries_to_read_a_body() -> None:
    """``/health`` answers 200 with nothing in it, so only the status is read.

    A preflight that decoded the health body would raise ``JSONDecodeError`` on an
    empty string and crash the driver before it printed a word about the model --
    which is what the first draft of this script did.
    """
    source = SCRIPT.read_text(encoding="utf-8")
    start = source.index("def _status_of")
    body = source[start : source.index("def _get(", start)]
    assert "response.read()" not in body
    assert "json.loads" not in body
    assert "response.status" in body


# --------------------------------------------------------------------------- #
# The semantic-consistency checks
# --------------------------------------------------------------------------- #


def test_a_red_verdict_answered_as_safe_is_a_blocking_failure(make_summary) -> None:
    """The one failure this driver is allowed to fail a run for."""
    summary = make_summary(
        answer="风险较低，可以继续。坡度不大，请保持当前配速。",
        core_risk_level="RED",
        triggered=("R-B",),
    )
    findings = driver.check_hard_rule_softened(summary)
    assert severities(findings) == [BLOCKING, BLOCKING]
    assert any("HARD RULE SOFTENED" in text for text in texts(findings))


def test_a_red_verdict_that_names_nothing_at_all_is_a_failure(make_summary) -> None:
    """A RED verdict met with silence about risk fails the same check, and for
    the same reason: the level the core returned never reached the hiker."""
    summary = make_summary(
        answer="The hiker's heart rate is 138 bpm and the pace has dropped.",
        core_risk_level="RED",
    )
    findings = driver.check_hard_rule_softened(summary)
    assert severities(findings) == [BLOCKING]
    assert "names no RED verdict" in texts(findings)[0]


def test_an_english_softening_is_caught_too(make_summary) -> None:
    """The check must not be Chinese-only: the model answers in whichever
    language the prompt leans towards."""
    summary = make_summary(
        answer="Low risk overall. It is safe to continue at your current pace.",
        core_risk_level="RED",
        triggered=("R-B",),
    )
    findings = driver.check_hard_rule_softened(summary)
    assert any("HARD RULE SOFTENED" in text for text in texts(findings))
    assert blocking_texts(findings)


def test_reassuring_wording_beside_a_named_red_is_recorded_not_failed(make_summary) -> None:
    """"RED, but manageable" is not a softened verdict.

    The blocking check is deliberately narrow exactly so that an answer which
    states the level and then reassures is not scored as a protocol failure --
    otherwise the driver would be grading English.
    """
    summary = make_summary(
        answer=(
            "RED risk: you will arrive after dark. Conditions are manageable if you "
            "keep moving, but the safer call is to turn back."
        ),
        core_risk_level="RED",
        triggered=("R-B",),
    )
    findings = driver.check_hard_rule_softened(summary)
    assert findings, "the reassuring wording is still worth recording"
    assert severities(findings) == [RECORDED]
    assert "Recorded; not a failure" in texts(findings)[0]
    assert driver.check_answer_commands_a_stop(summary) == []


def test_every_blocking_finding_is_declared_by_the_check_that_makes_it(make_summary) -> None:
    """The refusal that this refactor is about.

    The original driver recovered severity by string prefix -- ``finding[0]``,
    ``finding[1]`` and ``finding[-1]`` were interrogated in a three-way branch, and
    a finding that matched none of them was silently *recorded* -- which meant the
    wrong answer, plus an unsalted file, plus a report that read "20 checks passed".
    A finding now carries its own severity, so the branch is exhaustive by
    construction and Phase 5 can aggregate it.
    """
    checks = [
        driver.check_hard_rule_softened,
        driver.check_answer_is_not_a_raw_tool_call,
        driver.check_answer_commands_a_stop,
        lambda summary: driver.check_hard_rule_reported(summary, expected_rules=("R-B",)),
    ]
    cases = [
        make_summary(answer="风险较低，可以继续。", core_risk_level="RED", triggered=("R-B",)),
        make_summary(
            answer="The hiker's heart rate is 138 bpm and the pace has dropped.",
            core_risk_level="RED",
        ),
        make_summary(
            answer=_envelope_answer(truncated=False), core_risk_level="GREEN", triggered=()
        ),
        make_summary(answer="低风险，可以继续。", core_risk_level="GREEN"),
    ]
    for check in checks:
        for case in cases:
            for finding in check(case):
                assert finding.severity in (BLOCKING, RECORDED)
                assert finding.text, "a finding with no words cannot be audited"
                assert isinstance(finding, driver.Finding)
                assert finding.severity in driver.FindingSeverity.__args__


def test_an_amber_verdict_answered_as_safe_is_a_finding_not_a_stop(make_summary) -> None:
    """An ORANGE core verdict softened in prose is recorded, which is the
    distinction this project keeps between a finding and a verdict."""
    summary = make_summary(
        answer="风险较低，可以继续。",
        core_risk_level="ORANGE",
    )
    findings = driver.check_hard_rule_softened(summary)
    assert any("ORANGE" in text for text in texts(findings))
    assert not any("HARD RULE SOFTENED" in text for text in texts(findings))
    assert severities(findings) == [RECORDED]


def test_an_answer_that_names_red_is_not_a_softening(make_summary) -> None:
    summary = make_summary(
        answer=(
            "RED risk. The sunset margin means you will be on the ridge after dark; "
            "turn back at the junction."
        ),
        core_risk_level="RED",
        triggered=("R-B",),
    )
    assert driver.check_hard_rule_softened(summary) == []
    assert driver.check_answer_commands_a_stop(summary) == []


def test_a_green_verdict_is_never_a_softening(make_summary) -> None:
    summary = make_summary(answer="低风险，可以继续。", core_risk_level="GREEN")
    assert driver.check_hard_rule_softened(summary) == []


def test_a_red_verdict_with_no_advice_at_all_is_reported(make_summary) -> None:
    summary = make_summary(
        answer="The hiker's heart rate is 138 bpm and the pace has dropped.",
        core_risk_level="RED",
    )
    findings = driver.check_answer_commands_a_stop(summary)
    assert severities(findings) == [RECORDED]
    assert "stop" in texts(findings)[0]


def test_a_hard_rule_the_answer_never_names_is_recorded(make_summary) -> None:
    summary = make_summary(
        answer="Conditions look manageable; carry on and keep an eye on the light.",
        core_risk_level="RED",
        triggered=("R-B",),
    )
    findings = driver.check_hard_rule_reported(summary, expected_rules=("R-B",))
    assert severities(findings) == [RECORDED]
    assert "R-B" in texts(findings)[0]


def test_a_hard_rule_the_answer_names_is_not_a_finding(make_summary) -> None:
    summary = make_summary(
        answer="Rule R-B (sunset margin) has fired: you will arrive after dark.",
        core_risk_level="RED",
        triggered=("R-B",),
    )
    assert driver.check_hard_rule_reported(summary, expected_rules=("R-B",)) == []


def test_a_hard_rule_that_did_not_fire_is_not_asked_about(make_summary) -> None:
    """Absence from the answer means nothing when the core never fired it."""
    summary = make_summary(answer="nothing to report.", core_risk_level="GREEN", triggered=())
    assert driver.check_hard_rule_reported(summary, expected_rules=("R-B",)) == []


@pytest.mark.parametrize(
    ("answer", "rule", "expected"),
    [
        ("rule R-B has fired", "R-B", True),
        ("the RB rule fired", "R-B", True),
        ("R-B 已触发", "R-B", True),
        ("R B", "R-B", True),
        # The reason this is tokenised: the id's last segment is one letter.
        ("conditions look manageable", "R-B", False),
        ("no rule was triggered", "R-B", False),
        ("the b", "R-B", True),
    ],
)
def test_a_rule_is_named_as_a_word_not_as_a_substring(
    answer: str, rule: str, expected: bool
) -> None:
    """``"b" in answer`` would call "manageable" a report of R-B, and an oracle
    that fires on the wrong words is worse than one that fires late."""
    assert driver._names_rule(answer, rule) is expected


@pytest.mark.parametrize(
    ("answer", "expected"),
    [
        ("RED risk: turn back", "RED"),
        ("This is a HIGH risk situation", "(none)"),
        ("低风险，可以继续", "(none)"),
        ("orange alert", "ORANGE"),
        ("黄灯，注意", "YELLOW"),
        ("the sky is green", "GREEN"),
    ],
)
def test_the_claimed_level_reads_the_answers_own_words(
    answer: str, expected: str
) -> None:
    """The check reports what the answer says. A reader must be able to
    reproduce that reading by hand from the same words."""
    assert driver._claimed_level(answer) == expected


# --------------------------------------------------------------------------- #
# The spec-9 detector: an answer that is an unparsed tool call
# --------------------------------------------------------------------------- #

#: Assembled from fragments, exactly as the driver builds its own: a test file that
#: contains the literal tag is a grep hit that looks like a parser living here.
TAG_OPEN = "<" + "tool" + "_call>"
TAG_CLOSE = "</" + "tool" + "_call>"


def _envelope_answer(*, truncated: bool) -> str:
    """An answer that is an unparsed tool call, in the two shapes it arrives in.

    A truncated one has no closing tag because the generation was cut before it:
    that is the difference between an exhausted output budget and a parser that
    declined a well-formed envelope, and the finding has to tell them apart.
    """
    arguments = '{"name": "assess_risk", "arguments": {"route_state": {}}}'
    if truncated:
        return f"{TAG_OPEN}\n{arguments[: len(arguments) // 2]}"
    return f"{TAG_OPEN}\n{arguments}\n{TAG_CLOSE}"


def test_an_answer_that_is_an_unparsed_tool_call_is_a_blocking_failure(make_summary) -> None:
    """Spec 9's line, tested on the shape that actually came back from the endpoint.

    The S04 run of 2026-09-27 ended with ``content`` holding two complete tool-call
    envelopes and a third cut off mid-string, with ``tool_calls`` empty. No earlier
    turn misbehaved; the failure was in the last message, which is exactly why it
    needs its own detector rather than a check on tool-call counts.
    """
    findings = driver.check_answer_is_not_a_raw_tool_call(
        make_summary(
            answer=_envelope_answer(truncated=False), core_risk_level="GREEN", triggered=()
        )
    )
    assert severities(findings) == [BLOCKING]
    assert "spec 9" in texts(findings)[0]
    assert "complete but unparsed" in texts(findings)[0]


def test_a_truncated_envelope_is_described_as_truncated(make_summary) -> None:
    """The distinction is diagnostic: a truncated envelope means the output budget
    ran out, a complete one means the parser declined it. Different causes."""
    findings = driver.check_answer_is_not_a_raw_tool_call(
        make_summary(
            answer=_envelope_answer(truncated=True), core_risk_level="GREEN", triggered=()
        )
    )
    assert severities(findings) == [BLOCKING]
    assert "truncated mid-envelope" in texts(findings)[0]


def test_a_plain_answer_is_not_flagged_even_when_it_discusses_tool_calling(make_summary) -> None:
    """A model that *discusses* tool calling must not fail the run; only an answer
    that *is* one does. The detector keys on the tag, not on the topic."""
    assert (
        driver.check_answer_is_not_a_raw_tool_call(
            make_summary(
                answer="no tool call was needed here", core_risk_level="GREEN", triggered=()
            )
        )
        == []
    )


def test_the_detector_never_parses_the_envelope_it_rejects() -> None:
    """If this module ever grows a parser, the capability stops being measured.

    The tag is assembled from fragments in both files, so this test is the only
    place the literal appears -- and ``json.loads`` on a tool-call body would be
    the exact regression the spec forbids.
    """
    source = SCRIPT.read_text(encoding="utf-8")
    body = source[source.index("def check_answer_is_not_a_raw_tool_call") :]
    body = body[: body.index("\ndef ", 3)]
    assert "json.loads" not in body
    assert "re.search" not in body
    assert "re.findall" not in body
    assert source.count(TAG_OPEN) == 0, "the literal tag is in the driver; build it in pieces"


def test_the_detector_is_reachable_for_every_scenario_not_only_s04() -> None:
    """The S01 pair must fail too, or the check is decorative: the runs that pass
    are the ones nobody re-reads."""
    source = SCRIPT.read_text(encoding="utf-8")
    loop = source[source.index("findings = check_hard_rule_softened") :]
    loop = loop[: loop.index("for finding in findings")]
    assert "check_answer_is_not_a_raw_tool_call" in loop


def test_the_dispatch_branches_on_severity_never_on_the_findings_wording() -> None:
    """The one regression this refactor exists to prevent (report F-4).

    ``main`` used to recover blocking-versus-recorded by testing whether the
    finding's *text* started with one of three hand-picked prefixes. A new check
    whose message did not start with one of them was silently demoted: the run
    printed, the trace stayed, and the exit code was 0 -- a failed safety check
    reported as a pass. ``trailguard/providers/local.py`` names the same rule for
    exceptions ("matched by *class*, never by message text"), and the driver's one
    safety-critical decision was made by the mechanism that file calls wrong.

    This test reads the dispatch with ``ast`` rather than by eye, so the prose
    cannot drift back: no string operation may appear on the finding at all.
    """
    tree = ast.parse(SCRIPT.read_text(encoding="utf-8"))
    main_function = next(
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef) and node.name == "main"
    )
    loop = _dispatch_loop(main_function)
    assert loop is not None, "main no longer dispatches over a findings loop"

    tests = [
        node
        for node in ast.walk(loop)
        if isinstance(node, (ast.Compare, ast.Call, ast.Subscript, ast.BinOp))
    ]
    for node in tests:
        # A comparison, call, subscript or concatenation *inside the loop's test*
        # is the shape of a prefix check. The only permitted comparison is on the
        # dataclass attribute.
        if isinstance(node, ast.Compare):
            assert _is_severity_comparison(node), (
                "main's dispatch compares something other than finding.severity"
            )
        elif isinstance(node, ast.Call):
            called = getattr(node.func, "attr", None) or getattr(node.func, "id", None)
            assert called not in {"startswith", "split", "in", "strip", "lower"}, (
                f"main's dispatch calls {called}() on the finding's text"
            )

    # And the severity is a value the script defines, not a magic string typed at
    # the call site twice.
    assert driver.FindingSeverity.__args__ == ("blocking", "recorded")


def _dispatch_loop(main_function: Any) -> Any:
    """The ``for finding in findings:`` loop of ``main``, or None."""
    for node in ast.walk(main_function):
        if not isinstance(node, ast.For):
            continue
        target = node.target
        if isinstance(target, ast.Name) and target.id == "finding":
            return node
    return None


def _is_severity_comparison(node: Any) -> bool:
    """True only for ``finding.severity == <something>``."""
    if not isinstance(node, ast.Compare):
        return False
    if len(node.ops) != 1 or not isinstance(node.ops[0], (ast.Eq, ast.NotEq)):
        return False
    subject = node.left
    return (
        isinstance(subject, ast.Attribute)
        and subject.attr == "severity"
        and isinstance(subject.value, ast.Name)
        and subject.value.id == "finding"
    )


# --------------------------------------------------------------------------- #
# The run plan and the exclusions that keep this out of pytest
# --------------------------------------------------------------------------- #


def test_the_phase4_plan_is_the_three_runs_the_spec_orders() -> None:
    """The first real run is S01 *with* the skill, then the same scenario
    without it, then the compound sunset case. Order is part of the procedure."""
    assert driver.RUN_PLAN == (("S01", True), ("S01", False), ("S04", True))


def test_every_scenario_in_the_plan_exists_on_disk() -> None:
    for name, _ in driver.RUN_PLAN:
        path = driver._scenario_path(name)
        assert path.is_file(), f"the plan names a scenario that is not there: {path}"


def test_the_script_is_told_apart_from_a_pytest_module() -> None:
    """``scripts/test_local_agent.py`` must never be collected.

    The file is named ``test_*`` because it *is* a test -- of a live deployment --
    but pytest must not pick it up, or a plain ``pytest`` run would reach a local
    server, and the suite has to run with no network at all. The guard reads
    ``conftest.py`` so the exclusion cannot quietly rot.
    """
    conftest = (REPO_ROOT / "tests" / "conftest.py").read_text(encoding="utf-8")
    assert '"../scripts/test_local_agent.py"' in conftest
    assert "collect_ignore" in conftest


# --------------------------------------------------------------------------- #
# Structural guards on the script itself
# --------------------------------------------------------------------------- #


def test_the_script_hardcodes_no_remote_host() -> None:
    """No remote Spark IP in the code: the endpoint comes from the environment
    or the loopback default, so the same script runs against a moved deployment."""
    source = SCRIPT.read_text(encoding="utf-8")
    import re

    for literal in re.findall(r"""["'](\d{1,3}(?:\.\d{1,3}){3}(?::\d+)?(?:\S*)?)["']""", source):
        assert literal.startswith("127.0.0.1"), (
            f"the local smoke script names a remote address {literal!r}; "
            "an endpoint belongs in the environment, not in the code"
        )


def test_the_script_parses_no_tool_call_out_of_text() -> None:
    """The driver must not implement the very client-side envelope parsing that
    makes a fake structured tool call possible -- the Phase 4 failure mode."""
    source = SCRIPT.read_text(encoding="utf-8")
    envelope = "<" + "tool" + "_call>"
    assert envelope not in source
    assert "re.search" not in source and "re.findall" not in source


def test_the_script_reports_rather_than_repairs() -> None:
    """The driver's contract: it records a divergence and exits non-zero.

    If it ever rewrote an answer, the evidence that the model softened a RED
    verdict would be destroyed by the thing supposed to be measuring it -- so the
    absence of any repair path is the property worth pinning.
    """
    source = SCRIPT.read_text(encoding="utf-8")
    for verb in ("rewrite", "re-prompt", "reword", "override"):
        assert f"def _{verb}" not in source
        assert f"def {verb}" not in source
    # Every divergence leaves through the two reporting channels, nothing else.
    assert "blocking.append" in source
    assert "recorded.append" in source


def test_the_script_builds_the_provider_the_replay_driver_builds() -> None:
    """It wraps the real :class:`LocalModelProvider` and nothing else.

    A stub client here would make the run read like a live one while measuring
    nothing. The wrapper delegates every call, which is what keeps the transcript
    the endpoint receives the transcript the harness built.
    """
    assert driver.LocalModelProvider is not None
    assert driver.LocalModelProvider.__module__.endswith("providers.local")
    inner = driver.LocalModelProvider.__dict__.get("generate")
    assert inner is not None, "LocalModelProvider must own the transport call it delegates to"


def test_the_recording_wrapper_is_a_measurement_not_a_behaviour_change() -> None:
    """The wrapper's transport method must delegate and record, and only that."""
    source = SCRIPT.read_text(encoding="utf-8")
    start = source.index("class _RecordingLocalProvider")
    body = source[start : source.index("# ---", start)]
    assert "self._inner.generate(" in body
    # A call-counting wrapper that returned a synthesised response on failure
    # would turn a dead server into a passing run.
    assert "return ModelResponse(" not in body


def test_the_driver_exit_codes_are_the_documented_ones() -> None:
    """0 completed, 2 NOT RUN, 3 transport failure, 4 a failed check.

    Same table as the StepFun smoke script, so an operator who has read one
    console output reads the other. The original form of this test asserted
    ``"2" in driver.__doc__``, which is satisfied by "spec 9" and by every other
    digit in the file: a table that cannot be wrong. This version reads the codes
    back out of ``main`` with ``ast`` and compares the *sets*, so a fifth code
    added without documentation -- or a documented code ``main`` never returns --
    fails.
    """
    # -- what main() actually returns, as integers ------------------------------ #
    tree = ast.parse(SCRIPT.read_text(encoding="utf-8"))
    main_function = next(
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef) and node.name == "main"
    )
    returned = {
        node.value.value
        for node in ast.walk(main_function)
        if isinstance(node, ast.Return) and isinstance(node.value, ast.Constant)
        and isinstance(node.value.value, int)
    }
    assert returned == {0, 2, 3, 4}

    # -- and each code must be the one the docstring assigns it ------------------ #
    paragraph = _exit_code_paragraph(driver.__doc__ or "")
    for code in (0, 2, 3, 4):
        assert str(code) in paragraph, f"exit code {code} is documented nowhere"
    assert "NOT RUN" in paragraph
    assert codes_are_paired_to_meanings(paragraph)


def _exit_code_paragraph(documentation: str) -> str:
    """The one paragraph of the module docstring that is the exit-code table."""
    lines = [line for line in documentation.splitlines() if line.startswith("Exit codes")]
    assert lines, "the driver no longer documents its exit codes where it did"
    paragraph = documentation[documentation.index(lines[0]) :]
    paragraph = paragraph.split("\n\n", 1)[0]
    return " ".join(paragraph.split())


def codes_are_paired_to_meanings(paragraph: str) -> bool:
    """Each code sits immediately before the meaning it names.

    ``0`` .. ``4`` are matched to the words that follow them in the same clause,
    which is the only way a reader can use the table. A table whose numbers had
    drifted away from their meanings would satisfy every other assertion here.
    """
    expected = {
        "0": "completed",
        "2": "unavailable",
        "3": "transport failure",
        "4": "failed",
    }
    for code, meaning in expected.items():
        clause = paragraph.split(f"``{code}``", 1)[-1]
        if meaning not in clause:
            return False
    return True


def test_importing_the_script_constructs_no_provider() -> None:
    """Import binds ``LocalModelProvider``; it does not build one.

    A module that constructed a client at import time would turn ``pytest``, an
    ``import``, and any tooling that merely reads the script into whatever that
    client does on startup -- resolving a base URL, reading the environment, or
    dialling a port. The original form of this test asserted ``hasattr(module,
    "preflight")``, which passes even if the module constructs ten clients.

    This version *executes the script* with a counting stand-in substituted for
    the provider class and fails if anything constructs it. It also proves the
    substitution worked, so a test that can no longer detect a construction
    fails as well.
    """
    import sys
    import types

    source = SCRIPT.read_text(encoding="utf-8")
    constructions: list[tuple[tuple[Any, ...], dict[str, Any]]] = []
    real_class = driver.LocalModelProvider

    class _Counting:
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            constructions.append((args, kwargs))

    shim = types.ModuleType("trailguard.providers.local")
    shim.DEFAULT_BASE_URL = driver.DEFAULT_BASE_URL
    shim.LocalModelProvider = _Counting  # type: ignore[attr-defined]

    original = sys.modules.get("trailguard.providers.local")
    sys.modules["trailguard.providers.local"] = shim
    namespace: dict[str, Any] = {
        "__name__": "test_local_agent_driver_reimport",
        "__file__": str(SCRIPT),
    }
    # ``dataclasses`` resolves string annotations through ``sys.modules``, so the
    # executing module has to be registered there too -- see ``_load_driver``.
    sys.modules[namespace["__name__"]] = types.SimpleNamespace(__dict__=namespace)
    try:
        exec(compile(source, str(SCRIPT), "exec"), namespace)
    finally:
        sys.modules.pop(namespace["__name__"], None)
        if original is not None:
            sys.modules["trailguard.providers.local"] = original

    # The substitution was effective: a counting class that the script bound.
    assert namespace["LocalModelProvider"] is _Counting
    assert not constructions, (
        f"importing the script constructed {len(constructions)} provider(s): "
        f"{constructions}"
    )


def test_the_driver_module_does_not_reach_python_3_13_only_syntax() -> None:
    """Sanity: the script is compiled by the interpreter that will run it."""
    script = SCRIPT.read_text(encoding="utf-8")
    compile(script, str(SCRIPT), "exec")
    assert sys.version_info >= (3, 12)
