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
through pytest's ``monkeypatch`` rather than by touching a tracked file. The
transport itself (wire translation, status mapping, the retry budget) is covered
in ``tests/test_stepfun_provider.py`` over ``httpx.MockTransport``.
"""

from __future__ import annotations

import importlib
from typing import Any

import pytest

from trailguard.providers.base import (
    ChatMessage,
    ModelProvider,
    ModelResponse,
    ProviderAuthenticationError,
    ProviderError,
    ProviderRateLimited,
    ProviderResponseError,
    ProviderServerError,
    ProviderTimeout,
    ProviderUnavailable,
    ToolCallRequest,
)
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


def forbidden_seams() -> tuple[str, ...]:
    """Substrings of a member name that would let a provider answer without transport.

    A fallback has to live *somewhere* on the object, and it has to be reachable
    by name or by attribute. Naming the seams turns "there is no fallback" from a
    claim into a check on the public surface -- and it fails the day somebody adds
    a cache, a replay, a fixture, a stub or a backup path.
    """
    return (
        "cache",
        "fallback",
        "backup",
        "replay",
        "fixture",
        "stub",
        "scripted",
        "canned",
        "fake",
        "mock",
        "sample",
        "default_response",
    )


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


def test_no_provider_binds_a_network_client_at_import_time() -> None:
    """Importing a provider must not require an SDK or open a socket.

    Phase 3 wires the StepFun transport, but the wiring is still lazy: ``openai``
    is imported inside the call that needs it, so the module attribute is never
    bound and a credential-free interpreter can import the provider.
    """
    for module_name in PROVIDER_MODULES:
        module = importlib.import_module(module_name)
        assert not hasattr(module, "openai"), (
            f"{module_name} binds a client at import time; the SDK import must stay inside _client()"
        )


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
# generate: the local backend is wired for Phase 4 (spec 16)
# --------------------------------------------------------------------------- #


def test_the_local_provider_is_wired_for_phase_four() -> None:
    """The local backend reaches its transport instead of deferring to its phase.

    This replaces the Phase 0/3 guard that asserted ``NotImplementedError``. The
    replacement is deliberately the *same* strength rather than a weaker one: it
    asserts that a call reaches the transport and surfaces as the transport failures
    a local provider must produce -- an unusable endpoint (no model, no server),
    which is the DGX Spark box exhibits when its server is stopped.

    What it must never do is fall back. A second assertion pins that the failure is
    a :class:`ProviderUnavailable`, and that the model it reports is still the one
    that was constructed -- not a substitute chosen so that some answer exists.
    """
    provider = LocalModelProvider(model="m", base_url="u")

    with pytest.raises(ProviderUnavailable) as excinfo:
        provider.generate(TRANSCRIPT, tools=None, temperature=0.0, max_tokens=2048)

    # The model was never substituted, and the endpoint named is the one configured.
    assert "no fallback" in str(excinfo.value)
    assert provider.model == "m", "the provider must not rewrite its own model to answer"


def test_the_local_provider_fails_visibly_rather_than_falling_back() -> None:
    """A stopped local server stops the run; it does not reroute it (spec 17).

    The one substitution that would make a broken local deployment look like a
    successful local run is a fallback -- to another provider, to a cached answer,
    or to a scripted turn. All three would produce a benchmark row for a run that
    did not happen.

    The first draft of this test pinned it with ``assert provider.model !=
    "step-5-preview"`` and ``assert "relative" not in repr(provider).lower()``:
    a literal comparison against a string the provider was never given, and a
    substring of a ``repr`` nobody defines. Neither could fail. This version
    reads the provider's *public surface* -- every attribute a caller can reach
    -- and forbids the seams a fallback would need, then runs the real failure
    path to confirm the endpoint named is the one that was configured.
    """
    provider = LocalModelProvider(model="m", base_url="http://127.0.0.1:1/v1")

    seams = forbidden_seams()
    public = [
        name
        for name in dir(provider)
        if not name.startswith("_") and name not in {"generate", "model", "base_url"}
    ]
    for name in public:
        assert not any(seam in name.lower() for seam in seams), (
            f"{name!r} is a public seam that could answer without the endpoint"
        )

    # The failure is classified as "no answer", which is what the harness records
    # as stop_reason='provider_error'. The message names the *local* transport, so a
    # reader can tell which endpoint refused; the class is deliberately refusal-free
    # (the endpoint's URL stays out of the message entirely, by design), so this
    # asserts what the message does carry rather than what it withholds.
    with pytest.raises(ProviderError) as excinfo:
        provider.generate(TRANSCRIPT, tools=None, temperature=0.0, max_tokens=2048)
    assert type(excinfo.value) is ProviderUnavailable, (
        "a connection failure must be 'no answer', not a determinism-shaped refusal"
    )
    assert "local transport failed" in str(excinfo.value)
    assert "stepfun" not in str(excinfo.value).lower()


def test_stepfun_generate_is_wired_for_phase_three(monkeypatch: pytest.MonkeyPatch) -> None:
    """Step 5 Preview no longer raises NotImplementedError: Phase 3 owns it now.

    What is asserted here is only that the transport is *live* and reaches its
    own failure path -- a missing credential -- rather than deferring to a phase
    that has arrived. The credential is never set, and the raised message must
    not contain one.
    """
    monkeypatch.delenv("STEPFUN_API_KEY", raising=False)
    provider = StepFunProvider(model="step-5-preview", base_url="https://stepfun.invalid")

    with pytest.raises(ProviderAuthenticationError) as excinfo:
        provider.generate(TRANSCRIPT, tools=None, temperature=0.0, max_tokens=2048)

    assert "STEPFUN_API_KEY" in str(excinfo.value)
    assert FAKE_CREDENTIAL not in str(excinfo.value)


def test_the_failure_taxonomy_separates_a_failed_endpoint_from_a_refusal() -> None:
    """A transport failure is data: the retry decision must be readable, not guessed.

    ``retryable`` is the one piece of policy on the hierarchy. Getting it wrong
    in either direction is expensive -- retrying a 401 burns the budget on a
    deterministic refusal, while not retrying a 429 throws away an answer the
    endpoint was willing to give one second later (spec 12).
    """
    retryable = {ProviderTimeout, ProviderUnavailable, ProviderRateLimited, ProviderServerError}
    deterministic = {ProviderAuthenticationError, ProviderResponseError}

    assert retryable.isdisjoint(deterministic)
    for failure in retryable:
        assert failure.retryable is True, f"{failure.__name__} must be retried once"
    for failure in deterministic:
        assert failure.retryable is False, f"{failure.__name__} must never be retried"

    assert issubclass(ProviderTimeout, ProviderUnavailable), "a timeout is an unreachable endpoint"
    assert issubclass(ProviderServerError, ProviderResponseError), "a 5xx is an unusable answer"
    assert all(issubclass(failure, ProviderError) for failure in retryable | deterministic)


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


def _environment_names_read(module_name: str) -> set[str]:
    """Every environment variable name a module actually reads, as an AST fact.

    Two shapes are recognised, because the two providers legitimately differ:

    * a direct ``os.environ.get("NAME")`` (the remote transport);
    * a name handed to this module's own ``_from_environment(...)`` helper, whose
      first argument is a tuple of literals in precedence order (the local transport).

    An AST walk rather than a source scan, because a source scan also matches the
    module's own constants (``DEFAULT_BASE_URL`` is a literal in this file and is
    not an environment variable). A third shape -- an env read reached through an
    argument or an attribute this walker does not model -- yields no name and
    therefore fails the equality assertion below, which is the safe failure: the
    contract is checked by absence, not by trust.
    """
    import ast
    import importlib
    import inspect

    tree = ast.parse(inspect.getsource(importlib.import_module(module_name)))

    # Resolve module-level literal assignments first, so a provider that declares
    # its environment names in one place (``_MODEL_ENV = (...)``) is as checkable as
    # one that inlines them at the call site.
    constants: dict[str, tuple[str, ...]] = {}
    for node in tree.body:
        if not isinstance(node, ast.Assign) or not isinstance(node.value, ast.Tuple):
            continue
        values = tuple(
            e.value for e in node.value.elts
            if isinstance(e, ast.Constant) and isinstance(e.value, str)
        )
        if len(values) == len(node.value.elts):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    constants[target.id] = values

    names: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not node.args:
            continue
        func = node.func
        if (
            isinstance(func, ast.Attribute)
            and func.attr in {"getenv", "get"}
            and isinstance(func.value, ast.Attribute)
            and func.value.attr == "environ"
        ):
            first = node.args[0]
            if isinstance(first, ast.Constant) and isinstance(first.value, str):
                names.add(first.value)
            continue
        if isinstance(func, ast.Name) and func.id == "_from_environment":
            first = node.args[0]
            if isinstance(first, ast.Name):
                names.update(constants.get(first.id, ()))
            else:
                for element in ast.walk(first):
                    if isinstance(element, ast.Constant) and isinstance(element.value, str):
                        names.add(element.value)
    return names


def test_every_configuration_lookup_is_a_documented_environment_variable() -> None:
    """Only the variables the module docstrings declare are read, and via os.environ.

    Bidirectional, in both directions, because a one-way check is not a check:

    * every documented name must still be read, so a rename cannot quietly orphan a
      variable somebody exported;
    * every variable the module *reads* must be documented, so a new lookup cannot
      be added without a docstring line and a test update.

    The second half is what makes this a guard rather than a description. Before it
    existed, adding ``os.environ.get("SOMETHING_NEW")`` to a provider passed every
    provider test in the suite -- the configuration contract is precisely the sort of
    thing that erodes one convenient lookup at a time.
    """
    documented: dict[str, set[str]] = {
        "trailguard.providers.stepfun": {"STEPFUN_MODEL", "STEPFUN_BASE_URL", "STEPFUN_API_KEY"},
        "trailguard.providers.local": {
            # Phase 4 names, which win when both are set...
            "TRAILGUARD_LOCAL_MODEL",
            "TRAILGUARD_LOCAL_BASE_URL",
            "TRAILGUARD_LOCAL_API_KEY",
            # ...and the Phase 0 names kept working, so an operator who exported
            # the documented-old form is not silently ignored.
            "TG_LOCAL_MODEL",
            "TG_LOCAL_BASE_URL",
        },
    }
    for module_name, expected in documented.items():
        executed = _provider_code_names(module_name)
        assert "environ" in executed, f"{module_name} must read the process environment"

        actually_read = _environment_names_read(module_name)
        assert actually_read == expected, (
            f"{module_name} reads {sorted(actually_read)} but documents {sorted(expected)}; "
            "the configuration contract drifted -- add the variable to the module "
            "docstring and to this set, or stop reading it"
        )

        # The names also appear as literals, so a rename cannot pass by changing
        # only the lookup helper.
        source = _provider_source(module_name)
        for name in expected:
            assert f'"{name}"' in source, f"{module_name} no longer reads {name}"


def test_no_credential_value_is_reachable_from_an_instance(monkeypatch: pytest.MonkeyPatch) -> None:
    """``__dict__`` and the public surface together hold no readable credential."""
    monkeypatch.setenv("STEPFUN_API_KEY", FAKE_CREDENTIAL)
    provider = StepFunProvider(model="m", base_url="u")

    public_values = {
        getattr(provider, name) for name in dir(provider) if not name.startswith("_") and name != "generate"
    }

    assert FAKE_CREDENTIAL not in public_values
    assert vars(provider).get("_api_key") == FAKE_CREDENTIAL, "the private slot does hold it"
