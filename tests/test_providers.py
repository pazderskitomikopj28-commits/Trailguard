"""Provider contract tests (spec sections 16, 14.2, 14.3).

Responsibility: prove that the two concrete providers of the frozen evaluation
matrix are *interchangeable* behind :class:`ModelProvider` -- identical public
surface, identical ``generate`` signature, identical argument semantics -- and
that neither of them exposes a credential: the StepFun credential stays private,
is masked in ``__repr__`` and is not probed by any public member, so no caller can
branch on which backend is loaded.

Explicit non-responsibility: these tests never construct a client, never open a
socket and never read ``.env`` -- configuration comes from ``os.environ`` at
construction time, which is exactly what spec 16 mandates, and the read is done
through pytest's ``monkeypatch`` rather than by touching a tracked file.
"""

from __future__ import annotations

import importlib
from typing import Any

import pytest

from trailguard.providers.base import ChatMessage, ModelProvider, ModelResponse, ToolCallRequest
from trailguard.providers.local import LocalModelProvider
from trailguard.providers.stepfun import StepFunProvider

#: The credential these tests pretend to find in the environment. It is a fixed,
#: obviously-fake string held in the environment variable only: the repository-wide
#: secret scan reads every committable file, so a credential-shaped keyword never
#: appears with a substantial value anywhere in this source.
FAKE_CREDENTIAL = "not-a-real-credential"

#: The two concrete providers of the frozen evaluation matrix (spec 14.2).
PROVIDER_MODULES: tuple[str, ...] = (
    "trailguard.providers.stepfun",
    "trailguard.providers.local",
)

TRANSCRIPT: list[ChatMessage] = [
    ChatMessage(role="system", content="You are a general-purpose assistant."),
    ChatMessage(role="user", content="Assess this hiker."),
]


# --------------------------------------------------------------------------- #
# Both providers implement the same frozen surface (spec 16)
# --------------------------------------------------------------------------- #


def _public_members(provider_class: type[ModelProvider]) -> set[str]:
    """Names a caller can reach on an instance, inherited members included."""
    return {name for name in dir(provider_class) if not name.startswith("_")}


def test_both_providers_expose_the_same_public_surface() -> None:
    """No provider-only member exists that a caller could branch on (spec 16).

    This is the regression guard for the credential probe that used to live on
    :class:`StepFunProvider` alone: a member present on one backend and absent on
    the other makes the two providers non-interchangeable and invites exactly the
    backend-specific branch the frozen matrix forbids.
    """
    local_only = _public_members(LocalModelProvider)
    stepfun_only = _public_members(StepFunProvider)

    assert local_only == stepfun_only, (
        f"provider surfaces diverge -- local-only: {sorted(local_only - stepfun_only)}; "
        f"stepfun-only: {sorted(stepfun_only - local_only)}"
    )


def test_no_provider_exposes_a_credential_probe() -> None:
    """Whether a credential exists is nobody's business but the transport's."""
    for provider_class in (LocalModelProvider, StepFunProvider):
        public = _public_members(provider_class)

        assert not [name for name in public if "key" in name.lower()], (
            f"{provider_class.__name__} exposes a credential-shaped member: "
            f"{sorted(n for n in public if 'key' in n.lower())}"
        )


@pytest.mark.parametrize("provider_class", [StepFunProvider, LocalModelProvider])
def test_the_credential_is_not_a_public_attribute(provider_class: type[ModelProvider]) -> None:
    """An instance dictionary holds no plainly-named credential to log."""
    provider = provider_class(model="stub-model", base_url="https://stub.invalid")
    instance_names = {name for name in vars(provider) if "key" in name.lower()}

    assert instance_names <= {"_api_key"}, (
        f"{provider_class.__name__} stores the credential as a public attribute: {instance_names}"
    )


def test_the_stepfun_credential_is_masked_in_repr(monkeypatch: pytest.MonkeyPatch) -> None:
    """A traceback or a debug log must not carry the credential."""
    monkeypatch.setenv("STEPFUN_API_KEY", FAKE_CREDENTIAL)
    provider = StepFunProvider(model="step-5-preview", base_url="https://stepfun.invalid")

    text = repr(provider)

    assert FAKE_CREDENTIAL not in text
    assert "<set>" in text
    assert "step-5-preview" in text, "the non-secret configuration stays readable"


def test_the_provider_name_survives_without_a_credential() -> None:
    """A missing credential is not an error at construction time (Phase 3 concern)."""
    provider = StepFunProvider(model="step-5-preview", base_url="https://stepfun.invalid")

    assert provider.name == "stepfun"
    assert "<set>" not in repr(provider), "no credential was configured, so none is reported as set"


def test_the_local_provider_needs_no_credential() -> None:
    """The DGX Spark backend is local by design (spec 14.1)."""
    provider = LocalModelProvider(model="local-4b", base_url="http://localhost:8000/v1")

    assert provider.name == "local"
    assert "key" not in repr(provider).lower()


# --------------------------------------------------------------------------- #
# Configuration comes from the process environment, once (spec 16)
# --------------------------------------------------------------------------- #


def test_stepfun_configuration_is_read_from_the_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    """Arguments win; otherwise the environment supplies the configuration."""
    monkeypatch.setenv("STEPFUN_MODEL", "env-model")
    monkeypatch.setenv("STEPFUN_BASE_URL", "https://env.invalid")
    monkeypatch.setenv("STEPFUN_API_KEY", FAKE_CREDENTIAL)

    from_environment = StepFunProvider()
    explicit = StepFunProvider(model="argument-model")

    assert from_environment.model == "env-model"
    assert from_environment.base_url == "https://env.invalid"
    assert explicit.model == "argument-model", "an explicit argument overrides the environment"


def test_local_configuration_is_read_from_the_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    """The local provider reads its own variables, not StepFun's."""
    monkeypatch.setenv("TG_LOCAL_MODEL", "env-local-model")
    monkeypatch.setenv("TG_LOCAL_BASE_URL", "http://127.0.0.1:8080/v1")

    provider = LocalModelProvider()

    assert provider.model == "env-local-model"
    assert provider.base_url == "http://127.0.0.1:8080/v1"


def test_configuration_is_captured_at_construction_not_read_per_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Spec 16: configuration is read once, so a later change cannot split a run."""
    monkeypatch.setenv("STEPFUN_MODEL", "first-model")
    provider = StepFunProvider()
    monkeypatch.setenv("STEPFUN_MODEL", "second-model")

    assert provider.model == "first-model"


def _provider_source(module_name: str) -> str:
    """Verbatim source of one provider module, for hygiene assertions."""
    import inspect

    module = importlib.import_module(module_name)
    return inspect.getsource(module)


def _provider_code_names(module_name: str) -> set[str]:
    """Every identifier a provider module *executes*, docstrings and comments excluded.

    Docstrings legitimately mention ``.env.example`` and ``os.environ`` -- that is
    where the configuration is documented. What must not happen is a statement that
    *reads* a file, so the check walks the syntax tree instead of matching text.
    """
    import ast
    import inspect

    module = importlib.import_module(module_name)
    tree = ast.parse(inspect.getsource(module))
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Name):
            names.add(node.id)
        elif isinstance(node, ast.Attribute):
            names.add(node.attr)
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names.add(node.name)
    return names


def test_no_provider_imports_a_network_client_in_phase_zero() -> None:
    """Importing a provider must not require an SDK or open a socket."""
    for module_name in PROVIDER_MODULES:
        module = importlib.import_module(module_name)
        assert not hasattr(module, "openai"), f"{module_name} must not bind a client in Phase 0"


def test_no_provider_reads_a_configuration_file() -> None:
    """Configuration comes from ``os.environ`` at construction time -- never a file.

    Spec 16 forbids spreading model configuration through business code; reading
    ``.env`` (or any other tracked file) at import time would do exactly that, and
    would put a credential-shaped path into a module that is imported by the
    harness. The absence is asserted on the identifiers the module *executes*
    (docstrings excluded), so the documentation may still name ``.env.example``.
    """
    for module_name in PROVIDER_MODULES:
        executed = _provider_code_names(module_name)

        assert "dotenv" not in executed, f"{module_name} executes a dotenv loader"
        assert "open" not in executed, f"{module_name} opens a file for configuration"
        assert "environ" in executed, f"{module_name} no longer reads the process environment"
        assert "read_text" not in executed, f"{module_name} reads a configuration file"
        assert "Path" not in executed, f"{module_name} builds a configuration file path"


# --------------------------------------------------------------------------- #
# generate: Phase 0 raises, naming the phase that owns the transport
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("provider", "phase"),
    [
        (StepFunProvider(model="m", base_url="u"), "Phase 3"),
        (LocalModelProvider(model="m", base_url="u"), "Phase 4"),
    ],
)
def test_generate_defers_to_its_own_phase(provider: ModelProvider, phase: str) -> None:
    """Each backend names the phase that wires it, so no call silently no-ops."""
    with pytest.raises(NotImplementedError) as excinfo:
        provider.generate(TRANSCRIPT, tools=None, temperature=0.0, max_tokens=2048)

    assert phase in str(excinfo.value)


def test_both_providers_accept_the_identical_call_signature() -> None:
    """The harness must be able to call either backend the same way (spec 3.1, 16)."""
    stepfun_call = StepFunProvider.generate
    local_call = LocalModelProvider.generate

    import inspect

    stepfun_signature = inspect.signature(stepfun_call)
    local_signature = inspect.signature(local_call)

    assert list(stepfun_signature.parameters) == list(local_signature.parameters)
    assert list(stepfun_signature.parameters) == ["self", "messages", "tools", "temperature", "max_tokens"]


# --------------------------------------------------------------------------- #
# The provider-agnostic transcript (spec 16)
# --------------------------------------------------------------------------- #


def test_an_assistant_turn_carries_the_calls_it_made() -> None:
    """An assistant turn without its calls orphan the tool results that follow."""
    request = ToolCallRequest(id="call-1", name="get_route_context", arguments={"route_id": "r1"})
    message = ChatMessage(role="assistant", content="", tool_calls=[request])

    restored = ChatMessage.model_validate_json(message.model_dump_json())

    assert restored.tool_calls == [request]
    assert restored.tool_calls[0].id == "call-1"


def test_a_tool_message_points_back_at_its_call() -> None:
    """``tool_call_id`` and ``name`` are what let a provider rebuild the request."""
    result = ChatMessage(role="tool", content="get_route_context -> {...}", tool_call_id="call-1", name="get_route_context")

    restored = ChatMessage.model_validate_json(result.model_dump_json())

    assert restored.tool_call_id == "call-1"
    assert restored.name == "get_route_context"
    assert restored.tool_calls == []


def test_a_transcript_round_trips_through_json() -> None:
    """Both backends see the same transcript, so it must have one wire format."""
    transcript = TRANSCRIPT + [ChatMessage(role="tool", content="ok", tool_call_id="call-1", name="get_route_context")]

    restored = [ChatMessage.model_validate(payload) for payload in [m.model_dump() for m in transcript]]

    assert [m.role for m in restored] == ["system", "user", "tool"]


def test_a_model_response_carries_either_text_or_calls() -> None:
    """One turn is text, calls, or both; the harness checks the calls first."""
    text_turn = ModelResponse(content="Answer.", tool_calls=[])
    call_turn = ModelResponse(content=None, tool_calls=[ToolCallRequest(id="c", name="assess_risk")])

    assert bool(text_turn.tool_calls) is False
    assert call_turn.content is None
    assert call_turn.tool_calls[0].name == "assess_risk"


def test_the_transcript_message_rejects_unknown_fields() -> None:
    """The provider contract stays closed, like every other schema in the package."""
    with pytest.raises(Exception) as excinfo:
        ChatMessage(role="system", content="hi", accident_probability=0.02)

    assert "accident_probability" in str(excinfo.value)


def test_every_configuration_lookup_is_a_documented_environment_variable() -> None:
    """Only the variables the module docstrings declare are read, and via os.environ."""
    documented: dict[str, set[str]] = {
        "trailguard.providers.stepfun": {"STEPFUN_MODEL", "STEPFUN_BASE_URL", "STEPFUN_API_KEY"},
        "trailguard.providers.local": {"TG_LOCAL_MODEL", "TG_LOCAL_BASE_URL"},
    }
    for module_name, expected in documented.items():
        executed = _provider_code_names(module_name)

        # The documented names appear in the code as literal strings, which the AST
        # walk above deliberately does not collect; read the raw source for those.
        source = _provider_source(module_name)
        for name in expected:
            assert f'"{name}"' in source, f"{module_name} no longer reads {name}"
        assert "environ" in executed, f"{module_name} must read the process environment"


def test_no_credential_value_is_reachable_from_an_instance(monkeypatch: pytest.MonkeyPatch) -> None:
    """``__dict__`` and the public surface together hold no readable credential."""
    monkeypatch.setenv("STEPFUN_API_KEY", FAKE_CREDENTIAL)
    provider = StepFunProvider(model="m", base_url="u")

    public_values = {
        getattr(provider, name) for name in dir(provider) if not name.startswith("_") and name != "generate"
    }

    assert FAKE_CREDENTIAL not in public_values
    assert vars(provider).get("_api_key") == FAKE_CREDENTIAL, "the private slot does hold it"
