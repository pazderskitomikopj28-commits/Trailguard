"""Evidence-trace contract tests (spec sections 18, 10.6).

Responsibility: prove that a realistic run can be recorded as a structured
:class:`EvidenceTrace`, that secret-shaped argument keys are redacted to
``"<redacted>"`` at the contract boundary while ordinary keys survive untouched,
and that the trace survives a JSON round trip so a demo UI or a benchmark report
can attach it without a second format.

Explicit non-responsibility: these tests do not build the trace by running an
agent (that is ``test_harness.py`` through ``TraceCollector``) and they do not
score it -- scoring is the eval layer's job (Phase 5).
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest
from pydantic import ValidationError

from conftest import (
    SECRET_KEY_SAMPLES,
    make_risk_result,
    make_weather_dict,
)

from trailguard.agent.trace import TraceCollector
from trailguard.schemas import (
    REDACTED,
    SECRET_KEY_PATTERNS,
    EvidenceTrace,
    Horizon,
    HorizonCheckRecord,
    RiskLevel,
    RuleDriver,
    Severity,
    ToolCallRecord,
    ToolName,
)

STARTED_AT = datetime(2026, 5, 18, 10, 30, 5, tzinfo=timezone.utc)
FINISHED_AT = datetime(2026, 5, 18, 10, 31, 12, tzinfo=timezone.utc)


def _tool_call_payload() -> dict:
    """One realistic recorded tool call, as the collector would hand it over."""
    return {
        "seq": 1,
        "tool": ToolName.GET_WEATHER_FOR_ROUTE_TIME,
        "arguments": {
            "lat": 46.08102,
            "lon": 7.77210,
            "target_time": "2026-05-18T11:30:00Z",
        },
        "result_ref": "replay/weather/scn_weather_snapshot.json",
        "result_summary": "get_weather_for_route_time -> freshness_minutes=30.0",
        "called_at": STARTED_AT.isoformat(),
        "status": "ok",
        "error": None,
    }


def _realistic_trace() -> EvidenceTrace:
    """A trace that looks like a real with-skill run over the +60min horizon."""
    collector = TraceCollector.new(
        run_id="run-0001",
        scenario_id="S01_clear_day",
        model_name="stub-model",
        provider="StubProvider",
        skill_loaded=True,
        sampling_params={"temperature": 0.0, "max_tokens": 2048, "max_steps": 12},
        started_at=STARTED_AT,
    )
    collector.record_tool_call(
        tool=ToolName.GET_ROUTE_CONTEXT,
        arguments={
            "route_id": "scn_route_sample",
            "current_position": {"lat": 46.08102, "lon": 7.77210},
            "timestamp": "2026-05-18T10:30:00Z",
        },
        result_ref="route_context:S01_clear_day",
        result_summary="get_route_context -> progress 0.35",
        status="not_implemented",
        error="Phase 1: deterministic route context",
    )
    collector.record_horizon(
        HorizonCheckRecord.model_validate(
            {
                "horizon": "+60min",
                "weather_freshness_min": 30.0,
                "freshness_ok": True,
                "missing_data": [],
            }
        )
    )
    collector.record_triggered_rules(["R-B", "R-C"])
    collector.record_risk_drivers(
        [
            RuleDriver.model_validate(
                {
                    "rule_id": "R-B",
                    "type": "sunset_margin",
                    "severity": "CRITICAL",
                    "detail": "time_margin_min = -12.5",
                }
            )
        ]
    )
    collector.set_final(
        risk_level=RiskLevel.ORANGE,
        confidence=0.71,
        uncertainty_disclosed=True,
    )
    return collector.finish(finished_at=FINISHED_AT)


# --------------------------------------------------------------------------- #
# Building a realistic run
# --------------------------------------------------------------------------- #


def test_evidence_trace_builds_from_a_realistic_run() -> None:
    """A full run -- tool calls, horizons, rules, drivers, verdict -- is recordable."""
    trace = _realistic_trace()

    assert trace.run_id == "run-0001"
    assert trace.scenario_id == "S01_clear_day"
    assert trace.model_name == "stub-model"
    assert trace.provider == "StubProvider"
    assert trace.skill_loaded is True
    assert trace.started_at == STARTED_AT
    assert trace.finished_at == FINISHED_AT

    assert len(trace.tool_calls) == 1
    assert trace.tool_calls[0].tool is ToolName.GET_ROUTE_CONTEXT
    assert trace.tool_calls[0].status == "not_implemented"

    assert trace.horizons_checked == [Horizon.PLUS_60]
    assert trace.horizon_checks[0].freshness_ok is True
    assert trace.weather_freshness_min == 30.0
    assert trace.freshness_checks == ["+60min: weather age 30.0 min (within freshness budget)"]

    assert trace.triggered_rules == ["R-B", "R-C"]
    assert trace.risk_drivers[0].severity is Severity.CRITICAL
    assert trace.final_risk_level is RiskLevel.ORANGE
    assert trace.confidence == pytest.approx(0.71)
    assert trace.uncertainty_disclosed is True
    assert trace.sampling_params == {"temperature": 0.0, "max_tokens": 2048, "max_steps": 12}


def test_trace_requires_the_frozen_identity_fields() -> None:
    """A trace without provider/model/start time cannot be attributed."""
    payload = _realistic_trace().model_dump(mode="json")
    del payload["provider"]

    with pytest.raises(ValidationError):
        EvidenceTrace.model_validate(payload)


def test_trace_rejects_unknown_field() -> None:
    """``extra="forbid"`` keeps the spec 18 vocabulary closed."""
    payload = _realistic_trace().model_dump(mode="json")
    payload["proprietary_score"] = 0.5

    with pytest.raises(ValidationError):
        EvidenceTrace.model_validate(payload)


def test_trace_rejects_illegal_horizon_and_tool() -> None:
    """Recorded horizons and tools stay inside the frozen vocabularies."""
    with pytest.raises(ValueError):
        EvidenceTrace.model_validate(
            {
                "run_id": "run-0002",
                "model_name": "stub-model",
                "provider": "StubProvider",
                "skill_loaded": False,
                "started_at": STARTED_AT.isoformat(),
                "horizons_checked": ["+15min"],
            }
        )

    with pytest.raises(ValueError):
        ToolCallRecord.model_validate(_tool_call_payload() | {"tool": "forecast_risk"})


# --------------------------------------------------------------------------- #
# Redaction
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("secret_key", SECRET_KEY_SAMPLES)
def test_secret_shaped_argument_keys_are_redacted(secret_key: str) -> None:
    """Any credential-shaped key becomes ``<redacted>`` in the stored record."""
    arguments = {
        secret_key: "fake-value",
        "lat": 46.08102,
        "lon": 7.77210,
        "target_time": "2026-05-18T11:30:00Z",
    }

    record = ToolCallRecord.model_validate(_tool_call_payload() | {"arguments": arguments})

    assert record.arguments[secret_key] == REDACTED
    assert record.arguments["lat"] == pytest.approx(46.08102)
    assert record.arguments["target_time"] == "2026-05-18T11:30:00Z"


def test_non_secret_keys_are_preserved_verbatim() -> None:
    """Redaction replaces values, never keys, and never non-secret values."""
    arguments = {
        "route_id": "scn_route_sample",
        "device_id": "device_001",
        "horizon_min": 60,
        "nested": {"labelled": {"deep": 1}, "count": 2},
        "flags": [True, False],
        "note": None,
    }

    record = ToolCallRecord.model_validate(_tool_call_payload() | {"arguments": arguments})

    assert record.arguments == arguments


def test_key_merely_containing_a_secret_word_is_also_redacted() -> None:
    """The policy is a substring test, so ``monkey_count`` is redacted on purpose.

    Over-redaction is the documented trade-off of ``SECRET_KEY_PATTERNS``: a
    legitimate ``monkey_count`` argument loses its value rather than risk leaking
    a credential that a stricter key-name pattern would have missed. The value is
    replaced wholesale -- the policy never walks into a redacted value.
    """
    arguments = {"monkey_count": 12, "keyed": {"deep": 1}, "route_id": "scn_route_sample"}

    record = ToolCallRecord.model_validate(_tool_call_payload() | {"arguments": arguments})

    assert record.arguments["monkey_count"] == REDACTED
    assert record.arguments["keyed"] == REDACTED
    assert record.arguments["route_id"] == "scn_route_sample"


def test_redaction_is_case_insensitive_and_nested() -> None:
    """``X-Api-Key``, ``AUTH_TOKEN`` and nested mappings are all covered."""
    arguments = {
        "X-Api-Key": "fake-value",
        "headers": {"AUTH_TOKEN": "fake-value", "Accept": "application/json"},
        "options": {"api_key": "fake-value", "retries": 3},
    }

    record = ToolCallRecord.model_validate(_tool_call_payload() | {"arguments": arguments})

    assert record.arguments["X-Api-Key"] == REDACTED
    assert record.arguments["headers"] == {"AUTH_TOKEN": REDACTED, "Accept": "application/json"}
    assert record.arguments["options"] == {"api_key": REDACTED, "retries": 3}


def test_redact_mapping_matches_the_record_validator() -> None:
    """``EvidenceTrace.redact_mapping`` is the same policy the records apply."""
    mapping = {"api_key": "fake-value", "password": "fake-value", "route_id": "scn_route_sample"}

    assert EvidenceTrace.redact_mapping(mapping) == {
        "api_key": REDACTED,
        "password": REDACTED,
        "route_id": "scn_route_sample",
    }
    assert EvidenceTrace.redact_mapping({}) == {}


def test_secret_key_pattern_list_is_the_documented_policy() -> None:
    """The pattern list covers the credential families the spec names."""
    for pattern in ("key", "token", "secret", "password", "authorization", "api_key", "credential"):
        assert pattern in SECRET_KEY_PATTERNS


def test_no_unredacted_argument_survives_in_a_realistic_trace() -> None:
    """Even a provider-shaped payload with a credential is scrubbed by the collector."""
    collector = TraceCollector.new(
        run_id="run-0003",
        scenario_id="S01_clear_day",
        model_name="stub-model",
        provider="StubProvider",
        skill_loaded=True,
        sampling_params={"temperature": 0.0},
        started_at=STARTED_AT,
    )
    collector.record_tool_call(
        tool=ToolName.GET_ROUTE_CONTEXT,
        arguments={"route_id": "scn_route_sample", "api_key": "fake-value"},
        result_ref=None,
        result_summary="get_route_context: not_implemented",
        status="not_implemented",
        error="Phase 1",
    )
    trace = collector.finish(finished_at=FINISHED_AT)

    assert trace.tool_calls[0].arguments["api_key"] == REDACTED
    assert "fake-value" not in trace.model_dump_json()


# --------------------------------------------------------------------------- #
# Serialization
# --------------------------------------------------------------------------- #


def test_trace_serializes_to_json_and_back() -> None:
    """The trace round-trips through JSON, so a report can carry it verbatim."""
    trace = _realistic_trace()

    payload = trace.model_dump_json()
    restored = EvidenceTrace.model_validate_json(payload)

    assert restored == trace
    assert restored.tool_calls[0].tool is ToolName.GET_ROUTE_CONTEXT
    assert restored.horizons_checked == [Horizon.PLUS_60]


def test_trace_records_no_dimensionless_black_box() -> None:
    """Spec 18's trace vocabulary is explainable evidence, not one opaque score."""
    trace = _realistic_trace()
    payload = trace.model_dump(mode="json")

    assert "safety_margin" not in payload  # a single scalar "safety margin = 0.37" is forbidden (spec 8)
    assert set(payload) >= {
        "tool_calls",
        "horizon_checks",
        "freshness_checks",
        "triggered_rules",
        "risk_drivers",
        "final_risk_level",
        "confidence",
        "missing_data",
        "uncertainty_disclosed",
        "sampling_params",
    }


def test_risk_result_keeps_its_physical_margins() -> None:
    """A verdict reports bounded, unit-carrying margins instead of one number."""
    risk = make_risk_result(risk_level="ORANGE", safety_margins={"time_margin_min": -12.5})

    assert risk.safety_margins.time_margin_min == pytest.approx(-12.5)
    assert "time_margin_min" in make_risk_result().model_dump()["safety_margins"]


def test_weather_provenance_is_data_not_a_credential() -> None:
    """Provider and age survive the trace: they are provenance, not secrets."""
    trace = _realistic_trace()

    assert trace.model_dump()["weather_freshness_min"] == 30.0
    assert make_weather_dict()["source"] == "open-meteo"
    assert make_weather_dict()["forecast_generated_at"] == "2026-05-18T10:00:00Z"


def test_horizon_check_record_distinguishes_unknown_freshness() -> None:
    """``freshness_ok=None`` means "not checkable", which is not the same as fresh."""
    check = HorizonCheckRecord.model_validate(
        {"horizon": "NOW", "weather_freshness_min": None, "freshness_ok": None, "missing_data": ["weather"]}
    )

    assert check.freshness_ok is None
    assert check.missing_data == ["weather"]


# --------------------------------------------------------------------------- #
# Free-text fields are scrubbed by shape, not by key (spec 18, no credential may
# reach a field nobody thought of as an input channel)
# --------------------------------------------------------------------------- #


def _free_text_summary(text: str) -> dict:
    """A tool-call payload whose *summary* is ``text`` and whose status is ok."""
    return _tool_call_payload() | {"result_summary": text, "status": "ok", "error": None}


@pytest.mark.parametrize("secret_key", SECRET_KEY_SAMPLES)
def test_a_credential_in_the_result_summary_is_masked(secret_key: str) -> None:
    """A tool that echoes a header, or a provider error pasted into a log line."""
    record = ToolCallRecord.model_validate(_free_text_summary(f"route ok; {secret_key}: fake-value; done"))

    assert REDACTED in record.result_summary
    assert "route ok" in record.result_summary
    assert "done" in record.result_summary
    assert "fake-value" not in record.result_summary


def test_a_credential_in_the_error_field_is_masked() -> None:
    """``error`` is the field most likely to carry a pasted transport failure."""
    payload = _tool_call_payload() | {
        "result_summary": "get_weather_for_route_time: error",
        "status": "error",
        "error": "401 from provider; Authorization: Bearer fake-value",
    }

    record = ToolCallRecord.model_validate(payload)

    assert "fake-value" not in (record.error or "")
    assert REDACTED in record.error
    assert "401 from provider" in record.error, "the non-secret context survives"


def test_a_quoted_credential_in_the_summary_keeps_its_quoting() -> None:
    """Redaction re-emits the opening quote only, so the text stays parseable."""
    record = ToolCallRecord.model_validate(
        _free_text_summary('weather snapshot: {"api_key": "fake-value", "source": "open-meteo"}')
    )

    assert '"api_key": "<redacted>"' in record.result_summary
    assert '"<redacted>""' not in record.result_summary
    assert '"source": "open-meteo"' in record.result_summary, "the non-secret sibling is untouched"


def test_a_placeholder_survives_the_text_policy() -> None:
    """``token: null`` is the documented empty state, not a credential."""
    record = ToolCallRecord.model_validate(_free_text_summary("config: token: null, retries: 3"))

    assert "token: null" in record.result_summary
    assert "retries: 3" in record.result_summary


def test_the_weather_source_is_scrubbed_but_a_plain_provider_survives() -> None:
    """``open-meteo`` is provenance; a provider URL with an inline credential is not."""
    trace = _realistic_trace().model_copy(update={"weather_source": "open-meteo"})
    assert trace.weather_source == "open-meteo"

    inline = EvidenceTrace.model_validate(
        _realistic_trace().model_dump() | {"weather_source": "https://weather.example/v1?api_key=fake-value"}
    )

    assert REDACTED in inline.weather_source
    assert "fake-value" not in inline.weather_source
    assert "https://weather.example/v1" in inline.weather_source, "the non-secret part stays readable"


@pytest.mark.parametrize("secret_key", SECRET_KEY_SAMPLES)
def test_every_freshness_log_line_is_scrubbed(secret_key: str) -> None:
    """The freshness log is free text appended per horizon, so each line is scanned."""
    trace = EvidenceTrace.model_validate(
        _realistic_trace().model_dump()
        | {"freshness_checks": [f"+60min: age 30.0 min; {secret_key}: fake-value", "+120min: age 45.0 min"]}
    )

    assert REDACTED in trace.freshness_checks[0]
    assert "age 30.0 min" in trace.freshness_checks[0]
    assert trace.freshness_checks[1] == "+120min: age 45.0 min", "an innocent line is untouched"


def test_sampling_params_keep_the_decoding_values_spec_18_requires() -> None:
    """``max_tokens`` contains the ``token`` substring and must survive the scan."""
    trace = EvidenceTrace.model_validate(
        _realistic_trace().model_dump()
        | {"sampling_params": {"temperature": 0.0, "max_tokens": 2048, "max_steps": 12, "seed": 7}}
    )

    assert trace.sampling_params == {"temperature": 0.0, "max_tokens": 2048, "max_steps": 12, "seed": 7}


def test_sampling_params_still_redact_a_real_credential_key() -> None:
    """The decoding-parameter exemption is by name; every other key keeps the policy."""
    trace = EvidenceTrace.model_validate(
        _realistic_trace().model_dump() | {"sampling_params": {"max_tokens": 2048, "token": "fake-value"}}
    )

    assert trace.sampling_params["max_tokens"] == 2048
    assert trace.sampling_params["token"] == REDACTED


def test_the_decoding_parameter_exemption_is_by_name_not_by_shape() -> None:
    """``max_tokens_backup`` is not a declared decoding parameter and is redacted."""
    trace = EvidenceTrace.model_validate(
        _realistic_trace().model_dump()
        | {"sampling_params": {"max_tokens": 2048, "max_tokens_backup": "fake-value"}}
    )

    assert trace.sampling_params["max_tokens"] == 2048
    assert trace.sampling_params["max_tokens_backup"] == REDACTED


def test_no_free_text_field_leaks_a_credential_in_one_trace() -> None:
    """One trace carrying a secret in every free-text channel stays clean end to end."""
    trace = EvidenceTrace.model_validate(
        _realistic_trace().model_dump()
        | {
            "weather_source": "https://weather.example/v1?api_key=fake-value",
            "freshness_checks": ["+60min: age 30.0 min; token: fake-value"],
            "sampling_params": {"temperature": 0.0, "max_tokens": 2048, "token": "fake-value"},
            "tool_calls": [
                call.model_dump()
                | {
                    "result_summary": f"route ok; api_key: fake-value",
                    "error": "401; Authorization: Bearer fake-value",
                    "status": "error",
                }
                for call in _realistic_trace().tool_calls
            ],
        }
    )

    serialized = trace.model_dump_json()

    assert "fake-value" not in serialized
    assert serialized.count(REDACTED) >= 5


