"""The batch loop is tested against a fake provider, against a fake preflight,
and against a directory it has already written to.

Those are the three ways ``run_benchmark`` could quietly do the wrong thing:
start a batch it should have aborted, overwrite a result it already reported, or
keep running after the machine failed and file the outage under the skill. None
of them needs a model to detect -- which is the point, because finding them with
a model means finding them 84 runs too late.
"""

from __future__ import annotations

import json

import pytest

from evals.phase5 import runner
from evals.phase5.manifest import RUN_ORDER
from evals.phase5.preflight import Report
from evals.phase5.runner import (
    RAW_DIR,
    RecordingProvider,
    is_context_limit_error,
    run_benchmark,
    run_id_for,
    run_one,
)
from trailguard.providers.base import (
    ModelProvider,
    ModelResponse,
    ProviderResponseError,
    ProviderTimeout,
    ProviderUnavailable,
    ToolCallRequest,
)

ANSWER = "**NOW** Risk Level: GREEN."


class FakeProvider(ModelProvider):
    """Answers immediately, or raises, and counts every turn it was asked for."""

    def __init__(self, answer: str = ANSWER, raise_error: Exception | None = None):
        self.answer = answer
        self.raise_error = raise_error
        self.calls = 0

    @property
    def name(self) -> str:
        return "fake"

    def generate(self, messages, *, tools=None, temperature=0.0, max_tokens=2048):
        self.calls += 1
        if self.raise_error is not None:
            raise self.raise_error
        return ModelResponse(content=self.answer)


class OneToolThenAnswer(FakeProvider):
    """Asks for one real tool, then answers -- so a record has tool evidence."""

    def __init__(self, answer: str = ANSWER):
        super().__init__(answer)
        self._asked = False

    def generate(self, messages, *, tools=None, temperature=0.0, max_tokens=2048):
        self.calls += 1
        if not self._asked:
            self._asked = True
            return ModelResponse(
                content=None,
                tool_calls=[ToolCallRequest(id="call_1", name="get_route_context", arguments={})],
            )
        return ModelResponse(content=self.answer)


class InventingATool(FakeProvider):
    """Names a tool the frozen registry does not have."""

    def generate(self, messages, *, tools=None, temperature=0.0, max_tokens=2048):
        self.calls += 1
        return ModelResponse(
            content=None,
            tool_calls=[ToolCallRequest(id="call_1", name="forecast_risk", arguments={})],
        )


@pytest.fixture
def scoring_dir(tmp_path, monkeypatch):
    """Point the batch at a scratch directory, never at the real run store."""
    monkeypatch.setattr(runner, "RAW_DIR", tmp_path / "raw")
    return tmp_path / "raw"


def _one_plan(scenario_id: str = "S01_clear_day") -> list[tuple[str, str, int]]:
    return [(scenario_id, "without_skill", 1)]


def _passing_preflight(monkeypatch) -> None:
    report = Report()
    report.add("fake endpoint", True, "ok")
    monkeypatch.setattr(runner, "preflight", lambda *a, **k: report)


# ------------------------------------------------------------------- the gate


def test_the_batch_refuses_to_start_without_an_explicit_confirm(scoring_dir) -> None:
    provider = FakeProvider()
    log: list[str] = []
    assert run_benchmark(provider, confirm=False, plan=_one_plan(), log=log.append) == 1
    assert provider.calls == 0, "no model may be called before the operator confirms"
    assert not scoring_dir.exists()
    assert any("confirm=True" in line for line in log)


def test_the_batch_aborts_when_the_preflight_fails_and_makes_no_calls(scoring_dir, monkeypatch) -> None:
    failing = Report()
    failing.add("SKILL.md sha256", False, "changed on disk")
    monkeypatch.setattr(runner, "preflight", lambda *a, **k: failing)
    provider = FakeProvider()
    log: list[str] = []
    assert run_benchmark(provider, confirm=True, plan=_one_plan(), log=log.append) == 1
    assert provider.calls == 0
    assert any("will not start" in line for line in log)
    assert not scoring_dir.exists()


def test_the_batch_runs_when_the_preflight_passes(scoring_dir, monkeypatch) -> None:
    _passing_preflight(monkeypatch)
    provider = OneToolThenAnswer()
    log: list[str] = []
    assert run_benchmark(provider, confirm=True, plan=_one_plan(), log=log.append) == 0
    assert provider.calls >= 1
    assert sorted(p.name for p in scoring_dir.glob("*.json")) == [
        "S01_clear_day__without_skill__rep1.json"
    ]


def test_a_scored_run_is_marked_scored_and_the_warmup_is_not(scoring_dir, monkeypatch) -> None:
    """The flag is what keeps the warm-up out of the pre-registration."""
    _passing_preflight(monkeypatch)
    run_benchmark(FakeProvider(), confirm=True, plan=_one_plan(), log=lambda *_: None)
    record = json.loads((scoring_dir / "S01_clear_day__without_skill__rep1.json").read_text())
    assert record["scored"] is True
    assert "warmup" not in record
    assert record["steps"] >= 1
    assert record["final_response"] == ANSWER


# ------------------------------------------------------------- append-only


def test_a_run_id_is_never_overwritten_by_a_resumed_batch(scoring_dir, monkeypatch) -> None:
    """The resumable batch must not quietly replace what it already reported."""
    _passing_preflight(monkeypatch)
    assert run_benchmark(OneToolThenAnswer(), confirm=True, plan=_one_plan(),
                         log=lambda *_: None) == 0
    original = (scoring_dir / "S01_clear_day__without_skill__rep1.json").read_text()

    second = OneToolThenAnswer(answer="**NOW** Risk Level: RED.")
    log: list[str] = []
    assert run_benchmark(second, confirm=True, plan=_one_plan(), log=log.append) == 0
    assert second.calls == 0, "a preserved run id is not re-run"
    assert (scoring_dir / "S01_clear_day__without_skill__rep1.json").read_text() == original
    assert any("already preserved" in line for line in log)


def test_replacing_a_run_is_visible_as_a_replacement(scoring_dir, monkeypatch) -> None:
    """A correction gets a new id and says what it replaced -- it never edits."""
    _passing_preflight(monkeypatch)
    from evals.phase5.runner import load_scenario_for_benchmark

    _, scenario = load_scenario_for_benchmark("S01_clear_day")
    first = run_one(
        scenario=scenario,
        model_provider=FakeProvider(answer="**NOW** Risk Level: YELLOW."),
        skill_enabled=False,
        repetition=1,
        output_dir=scoring_dir,
    )
    first.payload["scored"] = True
    first.save()

    with pytest.raises(FileExistsError):
        run_one(
            scenario=scenario,
            model_provider=FakeProvider(),
            skill_enabled=False,
            repetition=1,
            output_dir=scoring_dir,
        )

    replacement = run_one(
        scenario=scenario,
        model_provider=FakeProvider(),
        skill_enabled=False,
        repetition=1,
        output_dir=scoring_dir,
        replaces="S01_clear_day__without_skill__rep1",
        reason="the first record holds a stale answer; recorded, not overwritten",
    )
    assert replacement.payload["replaces"] == "S01_clear_day__without_skill__rep1"
    assert "stale answer" in replacement.payload["reason"]
    assert (scoring_dir / "S01_clear_day__without_skill__rep1.json").read_text().find(
        "YELLOW"
    ), "the record it replaced is still what it was"


# ----------------------------------------------------------- infra accidents


def test_a_transport_failure_pauses_the_batch_and_records_itself(scoring_dir, monkeypatch) -> None:
    """An outage is not evidence about the skill, so the batch stops here."""
    _passing_preflight(monkeypatch)
    log: list[str] = []
    provider = FakeProvider(raise_error=ProviderTimeout("read timeout after 300s"))
    assert run_benchmark(provider, confirm=True, plan=_one_plan(), log=log.append) == 1
    record = json.loads((scoring_dir / "S01_clear_day__without_skill__rep1.json").read_text())
    assert record["provider_error"]["class_name"] == "ProviderTimeout"
    assert record["scored"] is True, "the run is recorded; the evaluator excludes it from M1-M14"
    assert any("PAUSED" in line for line in log)


def test_a_pause_does_not_cost_the_runs_after_it(scoring_dir, monkeypatch) -> None:
    """The failing run's record is preserved; everything after it is not started.

    "Pause" is not "abort the batch and lose the evidence" -- it is stop, keep
    what you have, and pick up from the next unrun id when the endpoint is back.
    """
    _passing_preflight(monkeypatch)
    plan = [
        ("S01_clear_day", "without_skill", 1),
        ("S01_clear_day", "with_skill", 1),
        ("B01_mid_route_all_clear", "without_skill", 1),
    ]

    class FailsOnItsSecondRun(FakeProvider):
        """The first run answers; the second never reaches the endpoint."""

        def __init__(self):
            super().__init__()
            self.runs = 0

        def generate(self, messages, *, tools=None, temperature=0.0, max_tokens=2048):
            if self.runs >= 1:  # the second planned run never reaches the endpoint
                raise ProviderUnavailable("endpoint refused")
            self.runs += 1
            self.calls += 1
            return ModelResponse(content=self.answer)

    log: list[str] = []
    assert run_benchmark(FailsOnItsSecondRun(), confirm=True, plan=plan, log=log.append) == 1

    # The run that failed is on disk, with its error named, so a reader can see
    # the evidence is missing rather than negative.
    failed = json.loads((scoring_dir / "S01_clear_day__with_skill__rep1.json").read_text())
    assert failed["provider_error"]["class_name"] == "ProviderUnavailable"
    # And nothing after it was started at all.
    assert not (scoring_dir / "B01_mid_route_all_clear__without_skill__rep1.json").exists()
    assert any("PAUSED" in line for line in log)


def test_a_context_limit_rejection_is_a_result_not_a_pause(scoring_dir, monkeypatch) -> None:
    """M16 exists precisely so this failure is counted instead of excused."""
    _passing_preflight(monkeypatch)
    log: list[str] = []
    provider = FakeProvider(
        raise_error=ProviderResponseError("maximum context length is 16384 tokens")
    )
    assert run_benchmark(provider, confirm=True, plan=_one_plan(), log=log.append) == 0
    assert not any("PAUSED" in line for line in log)
    record = json.loads((scoring_dir / "S01_clear_day__without_skill__rep1.json").read_text())
    assert record["provider_error"]["class_name"] == "ProviderResponseError"


def test_the_context_limit_rule_matches_the_frozen_vocabulary() -> None:
    """One vocabulary, imported: a message the runner excuses and the evaluator
    ignores would make M16 measure nothing at all."""
    from evals.phase5.metrics import CONTEXT_LIMIT_SUBSTRINGS

    for phrase in CONTEXT_LIMIT_SUBSTRINGS:
        if phrase.strip().isdigit():  # "413" only means a rejection inside a longer string
            continue
        assert is_context_limit_error(f"upstream said: {phrase}"), phrase
    assert not is_context_limit_error("read timeout after 300s")
    assert not is_context_limit_error("connection refused")
    assert not is_context_limit_error("")


# ------------------------------------------------------- the recording wrapper


def test_the_wrapper_names_the_error_it_caught_and_reraises_it_unchanged() -> None:
    recording = RecordingProvider(FakeProvider(raise_error=ProviderTimeout("boom")))
    with pytest.raises(ProviderTimeout):
        recording.generate([], tools=None)
    assert recording.provider_error is not None
    assert recording.provider_error.class_name == "ProviderTimeout"
    assert recording.turns == [], "a failed turn asked for nothing"


def test_the_wrapper_counts_a_call_the_trace_cannot_see() -> None:
    """M3's denominator: an unregistered name produces no ToolCallRecord."""
    recording = RecordingProvider(OneToolThenAnswer())
    recording.generate([], tools=None)
    recording.generate([], tools=None)
    assert recording.attempted_tool_calls == 1
    assert recording.attempted_names == ["get_route_context"]
    assert recording.unknown_tool_calls == 0

    recording = RecordingProvider(InventingATool())
    recording.generate([], tools=None)
    assert recording.unknown_tool_calls == 1
    assert recording.attempted_names == ["forecast_risk"]


# ---------------------------------------------------------------- the identity


def test_the_run_id_names_the_scenario_the_condition_and_the_repetition() -> None:
    assert run_id_for("S01_clear_day", "without_skill", 1) == "S01_clear_day__without_skill__rep1"
    assert run_id_for("S01_clear_day", "with_skill", 3) == "S01_clear_day__with_skill__rep3"


def test_the_plan_is_the_pre_registered_order_exactly() -> None:
    """The queue is the manifest's order, not a sort of the scenario names."""
    plan = runner.planned_runs()
    assert len(plan) == len(RUN_ORDER) * 14
    # planned_runs() is (scenario, condition, repetition); RUN_ORDER is
    # (repetition, condition). The first entry of every repetition block must be
    # the repetition's first scenario under that repetition's condition.
    for index, (repetition, condition) in enumerate(RUN_ORDER):
        start = index * 14
        assert set(plan[start:start + 14]) == {
            (scenario_id, condition, repetition) for scenario_id in runner.BENCHMARK_SCENARIOS
        }
    for repetition, condition in RUN_ORDER:
        for scenario_id in ("S01_clear_day", "B01_mid_route_all_clear", "S11_stale_gps_missing_data"):
            assert (scenario_id, condition, repetition) in plan
    assert len({run_id_for(*row) for row in plan}) == len(plan), "every run id is distinct"


def test_the_raw_directory_is_the_append_only_result_store() -> None:
    assert RAW_DIR.parent.name == "phase5"
    assert RAW_DIR.name == "raw"
