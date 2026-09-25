"""Tool contract tests for the frozen five-tool registry (spec sections 4, 5).

Responsibility: prove that exactly the five atomic tools of spec 4.1 exist, that
their names and JSON-schema shapes are what the harness advertises, that every
implementation body honestly refuses to run in Phase 0, and that no one-shot
"answer all 30/60/120 horizons at once" tool (the banned ``forecast_risk``
pattern of spec 4.2) exists anywhere in the package.

Explicit non-responsibility: these tests do not execute tool logic, do not build
a risk verdict and do not check the horizon loop -- that loop is the Skill's job
(spec 10.2) and no tool may own it.
"""

from __future__ import annotations

import ast
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator

import pytest

from conftest import (
    EXPECTED_TOOL_NAMES,
    REPO_ROOT,
    make_geo_point,
    make_risk_result,
    make_route_context,
    make_route_projection,
    make_time_state,
    make_weather,
)

from trailguard.schemas import ToolName
from trailguard.tools import (
    TOOL_FUNCTIONS,
    TOOL_SPECS,
    ProjectRouteStateInput,
    all_tool_names,
    get_tool_spec,
    tool_schema_dicts,
)

#: Tool names spec 4.2 forbids: a single call that answers the whole horizon set.
#: The first three spellings are the ones the spec itself writes down
#: (``forecast_risk()`` returning ``{"30m": ..., "60m": ..., "120m": ...}``), the
#: rest are the obvious aliases a later contributor might reach for.
BANNED_TOOL_NAMES: tuple[str, ...] = (
    "forecast_risk",
    "predict_all_horizons",
    "calculate_future_risk",
    "future_risk",
    "predict_risk",
    "forecast_all_horizons",
    "assess_all_horizons",
    "assess_horizons",
    "get_risk_forecast",
    "risk_forecast",
    "one_shot_risk",
    "answer_all_horizons",
)

#: Identifier that must never appear in this package: the domain concept is a
#: *Projected Risk State* (spec 6.2), not an accident probability.
BANNED_IDENTIFIER = "accident_probability"


def _package_sources() -> Iterator[Path]:
    """Yield every ``.py`` file of the ``trailguard`` package."""
    package_root = REPO_ROOT / "trailguard"
    for path in sorted(package_root.rglob("*.py")):
        if "__pycache__" in path.parts:
            continue
        yield path


def _source_identifiers() -> set[str]:
    """Collect every identifier used in code (defs, names, arguments, fields).

    Docstrings and comments are excluded on purpose: they legitimately *mention*
    banned names such as ``forecast_risk`` while explaining why they do not
    exist, and only a code-level identifier would create the banned tool.
    """
    identifiers: set[str] = set()
    for path in _package_sources():
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                identifiers.add(node.name)
            elif isinstance(node, ast.Name):
                identifiers.add(node.id)
            elif isinstance(node, ast.arg):
                identifiers.add(node.arg)
            elif isinstance(node, ast.Attribute):
                identifiers.add(node.attr)
            elif isinstance(node, ast.keyword):
                if node.arg is not None:
                    identifiers.add(node.arg)
    return identifiers


# --------------------------------------------------------------------------- #
# Registry shape
# --------------------------------------------------------------------------- #


def test_registry_holds_exactly_the_five_frozen_tools() -> None:
    """``TOOL_SPECS`` is the frozen five-entry registry of spec 4.1."""
    assert len(TOOL_SPECS) == 5
    assert all_tool_names() == [
        ToolName.GET_ROUTE_CONTEXT,
        ToolName.PROJECT_ROUTE_STATE,
        ToolName.GET_WEATHER_FOR_ROUTE_TIME,
        ToolName.GET_USER_DEVICE_CONTEXT,
        ToolName.ASSESS_RISK,
    ]
    assert [spec.name.value for spec in TOOL_SPECS.values()] == EXPECTED_TOOL_NAMES


def test_tool_spec_names_match_the_enum_registry() -> None:
    """Every spec key equals its own ``name`` field -- no mixed-up entries."""
    for tool_name, spec in TOOL_SPECS.items():
        assert spec.name is tool_name
        assert spec.name.value in EXPECTED_TOOL_NAMES


def test_every_spec_declares_phase_and_forbidden_uses() -> None:
    """Each tool says when it is implemented and what it must never be used for."""
    for spec in TOOL_SPECS.values():
        assert spec.implemented_in_phase.startswith("Phase ")
        assert spec.forbidden_uses, f"{spec.name.value} must list its spec boundaries"
        assert spec.input_model is not None
        assert spec.output_model is not None


def test_assess_risk_forbidden_uses_encode_the_atomic_boundary() -> None:
    """``assess_risk`` explicitly may not project, fetch or loop (spec 4.2, 5.5)."""
    forbidden = " ".join(get_tool_spec(ToolName.ASSESS_RISK).forbidden_uses).lower()

    assert "must not project future positions" in forbidden
    assert "must not fetch weather" in forbidden
    assert "must not run the 30/60/120 loop" in forbidden


def test_get_tool_spec_raises_for_unknown_tool() -> None:
    """An unknown tool name fails loudly instead of being invented."""
    with pytest.raises(KeyError):
        get_tool_spec("forecast_risk")


def test_tool_functions_map_onto_the_five_implementations() -> None:
    """``TOOL_FUNCTIONS`` maps each registry entry to a distinct callable."""
    assert set(TOOL_FUNCTIONS) == set(TOOL_SPECS)
    assert len(set(id(fn) for fn in TOOL_FUNCTIONS.values())) == 5


# --------------------------------------------------------------------------- #
# Phase 0 stub contract
# --------------------------------------------------------------------------- #


def _route_state_kwargs() -> dict:
    """Keyword arguments a caller would hand ``project_route_state``."""
    return {
        "route_context": make_route_context(),
        "movement": {
            "current_speed_kmh": 3.5,
            "avg_speed_10m": 3.6,
            "avg_speed_30m": 3.7,
        },
        "horizon_min": 60,
    }


@pytest.mark.parametrize("tool_name", list(ToolName), ids=lambda tool: tool.value)
def test_every_tool_raises_not_implemented(tool_name: ToolName) -> None:
    """Each body refuses to run and names the phase that owes the work."""
    arguments = {
        ToolName.GET_ROUTE_CONTEXT: {
            "route_id": "scn_route_sample",
            "current_position": make_geo_point(),
            "timestamp": datetime(2026, 5, 18, 10, 30, tzinfo=timezone.utc),
        },
        ToolName.PROJECT_ROUTE_STATE: _route_state_kwargs(),
        ToolName.GET_WEATHER_FOR_ROUTE_TIME: {
            "lat": 46.08102,
            "lon": 7.77210,
            "target_time": datetime(2026, 5, 18, 11, 30, tzinfo=timezone.utc),
        },
        ToolName.GET_USER_DEVICE_CONTEXT: {"device_id": "device_001"},
        ToolName.ASSESS_RISK: {
            "route_state": make_route_context(),
            "weather_state": make_weather(),
            "user_device_state": {
                "battery_percent": 78.0,
                "network_available": True,
                "fatigue_index": 0.12,
            },
            "time_state": make_time_state(),
        },
    }[tool_name]

    with pytest.raises(NotImplementedError) as excinfo:
        TOOL_FUNCTIONS[tool_name](**arguments)

    message = str(excinfo.value)
    assert "Phase" in message, f"{tool_name.value} must name its owning phase"
    assert message != "", f"{tool_name.value} must not raise a bare NotImplementedError"


def test_assess_risk_stub_does_not_return_a_verdict() -> None:
    """The stub raises instead of inventing a plausible-looking verdict."""
    verdict = make_risk_result(risk_level="RED")

    with pytest.raises(NotImplementedError):
        TOOL_FUNCTIONS[ToolName.ASSESS_RISK](
            route_state=make_route_context(),
            weather_state=make_weather(),
            user_device_state={
                "battery_percent": 78.0,
                "network_available": True,
                "fatigue_index": 0.12,
            },
            time_state=make_time_state(),
        )

    # The helper above proves the factory stays usable for other tests; the stub
    # itself never produced this or any other verdict.
    assert verdict.risk_level.value == "RED"


# --------------------------------------------------------------------------- #
# Advertised schemas
# --------------------------------------------------------------------------- #


def test_tool_schema_dicts_returns_five_json_schema_shaped_entries() -> None:
    """The harness receives JSON-schema-shaped specs, one per frozen tool."""
    schemas = tool_schema_dicts()

    assert isinstance(schemas, list)
    assert len(schemas) == 5
    for spec, expected_name in zip(schemas, EXPECTED_TOOL_NAMES):
        assert set(spec) == {"name", "description", "input_schema"}
        assert spec["name"] == expected_name
        assert isinstance(spec["description"], str) and spec["description"]
        assert spec["input_schema"]["type"] == "object"
        assert isinstance(spec["input_schema"].get("properties"), dict)
        assert spec["input_schema"]["properties"], "a tool with no arguments is not a contract"
        assert spec["input_schema"].get("additionalProperties") is False


def test_tool_schema_dicts_match_the_input_models() -> None:
    """Each advertised schema is exactly its input model's JSON schema."""
    for spec in tool_schema_dicts():
        entry = TOOL_SPECS[ToolName(spec["name"])]
        assert spec["input_schema"] == entry.input_model.model_json_schema()


def test_no_tool_advertises_a_multi_horizon_payload() -> None:
    """No advertised schema keys its arguments by horizon (30/60/120)."""
    horizon_like = {"30min", "60min", "120min", "+30min", "+60min", "+120min", "horizons"}

    for spec in tool_schema_dicts():
        properties = set(spec["input_schema"]["properties"])
        assert not properties & horizon_like, f"{spec['name']} advertises a horizon-keyed payload"


# --------------------------------------------------------------------------- #
# Spec 4.2: the banned one-shot future-risk tool must not exist
# --------------------------------------------------------------------------- #


def test_no_banned_one_shot_tool_is_defined() -> None:
    """No ``def <banned tool>`` anywhere in the package source."""
    identifiers = _source_identifiers()

    for banned in BANNED_TOOL_NAMES:
        assert banned not in identifiers, (
            f"'{banned}' is defined in trailguard/; spec 4.2 forbids a tool that "
            "answers all 30/60/120 horizons in one call"
        )


def test_no_banned_one_shot_tool_is_declared_in_source() -> None:
    """The raw source contains no banned tool definition or registration."""
    raw = "\n".join(path.read_text(encoding="utf-8") for path in _package_sources())

    for banned in BANNED_TOOL_NAMES:
        assert f"def {banned}(" not in raw
        assert f'"{banned}"' not in raw
        assert f"'{banned}'" not in raw


def test_no_registered_or_advertised_tool_is_one_shot() -> None:
    """Neither the registry nor the advertised schemas expose a banned name."""
    names = {spec.name.value for spec in TOOL_SPECS.values()}
    names |= {spec["name"] for spec in tool_schema_dicts()}

    assert not names & set(BANNED_TOOL_NAMES)


def test_banned_accident_probability_identifier_is_absent() -> None:
    """``accident_probability`` is never a code identifier in this package."""
    identifiers = _source_identifiers()

    assert BANNED_IDENTIFIER not in identifiers, (
        f"'{BANNED_IDENTIFIER}' must not be used as an identifier; the domain concept "
        "is a Projected Risk State (spec 6.2)"
    )


def test_projection_contract_documents_conditional_state() -> None:
    """``project_route_state`` output is a state, with no probability of harm."""
    projection = make_route_projection()
    spec = get_tool_spec(ToolName.PROJECT_ROUTE_STATE)

    assert projection.horizon_min == projection.horizon.minutes
    assert 0.0 <= projection.projection_confidence <= 1.0
    joined = " ".join(spec.forbidden_uses).lower()
    assert "never a probability" in spec.description.lower()
    assert "risk level" in joined


# --------------------------------------------------------------------------- #
# Spec 4.2: the same proof, taken from grep instead of from the parse tree
# --------------------------------------------------------------------------- #


def _grep(pattern: str) -> list[str]:
    """Every matching ``path:line`` of a ``grep -rn`` over the package."""
    completed = subprocess.run(
        ["grep", "-rn", "-E", pattern, str(REPO_ROOT / "trailguard")],
        capture_output=True,
        text=True,
        check=False,
    )
    return [line for line in completed.stdout.splitlines() if line]


@pytest.mark.parametrize("banned", BANNED_TOOL_NAMES)
def test_grep_finds_no_one_shot_future_risk_tool(banned: str) -> None:
    """``grep`` over ``trailguard/`` finds no definition or registration of it.

    Two shapes are searched, because those are the only two ways a banned tool
    can come into existence: a function definition (``def forecast_risk(``) and a
    registration/advertisement by name (a quoted string in the registry or in a
    tool-schema dict). Prose mentions in docstrings are not matches and are
    deliberately allowed -- they explain why the tool must not exist.
    """
    definitions = _grep(rf"def\s+{banned}\s*\(")
    registrations = _grep(rf"[\"']{banned}[\"']")

    assert definitions == [], f"'def {banned}(' exists in trailguard/; spec 4.2 forbids it"
    assert registrations == [], f"{banned!r} is registered or advertised in trailguard/; spec 4.2 forbids it"


def test_no_dict_literal_in_code_is_keyed_by_horizon() -> None:
    """No code builds a ``{"30m": ..., "60m": ..., "120m": ...}`` result payload.

    This is the other half of the banned pattern of spec 4.2: a single call that
    answers the whole horizon set. The advertised argument schemas are checked
    above; here the *package source* is walked for a dict literal keyed by a
    horizon. Docstrings are excluded because a docstring legitimately quotes the
    banned shape in order to explain why it must not exist -- only a real dict
    literal in code would create the one-shot tool.
    """
    horizon_keys = {"30m", "60m", "120m", "+30min", "+60min", "+120min"}
    offenders: list[str] = []

    for path in _package_sources():
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Dict):
                continue
            for key in node.keys:
                if isinstance(key, ast.Constant) and key.value in horizon_keys:
                    offenders.append(f"{path.name}:{node.lineno}")

    assert offenders == [], f"a horizon-keyed payload is built in code: {offenders}"


# --------------------------------------------------------------------------- #
# Frozen horizon domain (spec 6.1)
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("bad_horizon", [-30, 15, 45, 90, 121, 999999])
def test_project_route_state_input_rejects_offsets_outside_the_frozen_set(bad_horizon: int) -> None:
    """``horizon_min`` accepts only 0/30/60/120 (spec 6.1).

    Every horizon-keyed consumer -- the skill's loop, the scenario's
    ``expected_required_horizons``, the trace's ``horizons_checked`` -- is keyed
    on exactly those four values, so a projection at ``horizon_min=15`` could not
    be recorded, compared or scored. The contract refuses it instead of letting a
    model ask a question the benchmark cannot answer.
    """
    kwargs = _route_state_kwargs() | {"horizon_min": bad_horizon}

    with pytest.raises(ValueError, match="frozen horizons"):
        ProjectRouteStateInput.model_validate(kwargs)


def test_project_route_state_input_accepts_every_frozen_horizon() -> None:
    """Each frozen offset is a legitimate input, including NOW (0)."""
    for offset in (0, 30, 60, 120):
        payload = ProjectRouteStateInput.model_validate(_route_state_kwargs() | {"horizon_min": offset})
        assert payload.horizon_min == offset


def test_projection_tool_description_is_a_state_not_a_verdict() -> None:
    """``project_route_state`` must not describe its output as a risk verdict.

    ``RouteProjection`` is a kinematic state; the Projected Risk *State* verdict
    is ``assess_risk``'s output. A tool description that blurred the two would
    invite the model to read a risk level off a position forecast.
    """
    description = get_tool_spec(ToolName.PROJECT_ROUTE_STATE).description.lower()

    assert "projected route state" in description
    assert "never a probability" in description
    assert "returns a projected risk state" not in description
