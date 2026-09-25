"""Packaging and dependency-footprint tests (spec sections 23, 16, 3.1).

Responsibility: keep the Phase 0 install honest. Two properties are asserted:

1. the *base* dependency list stays minimal (pydantic + PyYAML), and every other
   piece of the spec-23 technical stack is declared as an optional extra named
   for the phase that first needs it, so nothing is installed "just in case";
2. ``requirements.txt`` keeps its original pins unchanged -- the orchestrator owns
   that file, and Phase 0 only appends a comment -- and no Phase 0 module imports
   a later-phase library.

Explicit non-responsibility: this module does not install anything, does not
test any of those libraries' behaviour and does not pin versions (the extras use
lower bounds, the frozen environment file keeps the exact pins).
"""

from __future__ import annotations

import subprocess
import sys
import tomllib
from pathlib import Path

from conftest import REPO_ROOT

#: The base install of Phase 0: the contract layer, the rule pack and the tests.
#: Names are distributions, matching how they are declared in ``pyproject.toml``.
BASE_DEPENDENCIES: frozenset[str] = frozenset({"pydantic", "pyyaml"})

#: The spec-23 technical stack that must live in extras, never in the base list.
#: Keyed by the *import* name, which is what the interpreter and ``sys.modules``
#: use, so the footprint check below matches what actually gets imported.
LATER_PHASE_LIBRARIES: dict[str, str] = {
    "gpxpy": "route",
    "shapely": "route",
    "pyproj": "route",
    "openai": "providers",
    "dotenv": "providers",
    "streamlit": "demo",
}

#: Distributions whose import name differs from the project name, as they appear in
#: requirement lists (`python-dotenv` imports as `dotenv`, `PyYAML` as `yaml`).
DISTRIBUTION_TO_IMPORT: dict[str, str] = {
    "python-dotenv": "dotenv",
    "pyyaml": "yaml",
    "typing-extensions": "typing_extensions",
}

#: The pins the environment already carried before Phase 0. Append-only: this set
#: must survive verbatim, because removing or relaxing one would silently change
#: the environment the benchmark numbers were produced on.
ORIGINAL_PINS: tuple[str, ...] = (
    "annotated-types==0.8.0",
    "anyio==4.15.1",
    "h11==0.16.0",
    "httpcore2==2.13.0",
    "httpx2==2.13.0",
    "idna==3.20",
    "jiter==0.17.0",
    "openai==3.17.0",
    "pydantic==2.13.5",
    "pydantic_core==2.46.5",
    "python-dotenv==1.2.3",
    "sniffio==1.3.1",
    "truststore==0.10.4",
    "typing-inspection==0.4.4",
    "typing_extensions==4.16.0",
    "PyYAML==6.0.3",
    "pytest==9.1.1",
)


def _pyproject() -> dict:
    """Parsed ``pyproject.toml`` (stdlib only -- no TOML library in Phase 0)."""
    return tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))


def _extras() -> dict[str, list[str]]:
    """Optional dependency groups, normalised to lowercase project names."""
    table = _pyproject()["project"]["optional-dependencies"]
    return {name: [requirement.lower() for requirement in requirements] for name, requirements in table.items()}


def _canonical_name(requirement: str) -> str:
    """One requirement reduced to the name the interpreter would import."""
    name = requirement.split("[")[0]
    for separator in ("===", ">=", "<=", "==", "!=", "~=", ">", "<", ";"):
        name = name.split(separator)[0]
    name = name.strip().lower()
    return DISTRIBUTION_TO_IMPORT.get(name, name.replace("-", "_"))


def _requirement_names(requirements: list[str]) -> set[str]:
    """Import names of a requirement list, versions stripped."""
    return {_canonical_name(requirement) for requirement in requirements}


# --------------------------------------------------------------------------- #
# The base install stays minimal (spec 23)
# --------------------------------------------------------------------------- #


def test_base_dependencies_are_the_phase_zero_contract_layer_only() -> None:
    """Nothing is installed "just in case" for a later phase (spec 23)."""
    declared = _requirement_names([d.lower() for d in _pyproject()["project"]["dependencies"]])

    assert declared == {"pydantic", "yaml"}, (
        "the base install must stay at the Phase 0 contract layer; later-phase "
        "libraries belong in an extra"
    )
    assert declared == {_canonical_name(name) for name in BASE_DEPENDENCIES}


def test_the_frozen_rule_pack_and_contracts_import_without_extras() -> None:
    """The Phase 0 import path needs nothing the extras provide."""
    for module_name in (
        "trailguard",
        "trailguard.schemas",
        "trailguard.risk_core",
        "trailguard.rules",
        "trailguard.tools",
        "trailguard.agent",
        "trailguard.providers",
        "evals",
    ):
        __import__(module_name)


def test_no_phase_zero_module_imports_a_later_phase_library() -> None:
    """Importing the package must not pull a single spec-23 library into memory."""
    code = (
        "import trailguard, trailguard.schemas, trailguard.risk_core, trailguard.tools, "
        "trailguard.agent, trailguard.providers, evals;"
        "import sys;"
        "print(sorted(name for name in sys.modules if name.split('.')[0] in "
        f"{sorted(set(LATER_PHASE_LIBRARIES))}))"
    )
    result = subprocess.run(
        [sys.executable, "-c", code],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, f"importing the Phase 0 package failed: {result.stderr}"
    assert result.stdout.strip() == "[]", (
        f"a later-phase library was imported: {result.stdout}"
    )


# --------------------------------------------------------------------------- #
# The spec-23 stack is declared as per-phase extras (spec 23)
# --------------------------------------------------------------------------- #


def test_every_later_phase_library_has_a_named_extra() -> None:
    """Each spec-23 library is opt-in, in the extra of the phase that needs it."""
    extras = _extras()

    for library, expected_extra in LATER_PHASE_LIBRARIES.items():
        homes = {
            extra
            for extra, requirements in extras.items()
            if library in _requirement_names(requirements)
        }
        assert homes == {expected_extra}, (
            f"{library} must be declared only in the {expected_extra!r} extra, found in {sorted(homes)}"
        )


def test_no_later_phase_library_reaches_the_base_dependencies() -> None:
    """An extra library in the base list would widen the Phase 0 footprint."""
    base = _requirement_names([d.lower() for d in _pyproject()["project"]["dependencies"]])

    assert base.isdisjoint(LATER_PHASE_LIBRARIES), (
        f"later-phase libraries in the base dependencies: {sorted(base & set(LATER_PHASE_LIBRARIES))}"
    )


def test_the_dev_extra_still_carries_the_test_runner() -> None:
    """``dev`` keeps pytest, so ``pip install -e .[dev]`` stays the test setup."""
    assert "pytest" in _requirement_names(_extras()["dev"])


def test_extras_declare_lower_bounds_not_pins() -> None:
    """An extra bound lets the later phase pick the runtime version; the frozen
    environment file keeps the exact pins."""
    for extra, requirements in _extras().items():
        for requirement in requirements:
            assert "==" not in requirement, f"{extra} pins {requirement}; extras must use lower bounds"


# --------------------------------------------------------------------------- #
# requirements.txt: append-only (the orchestrator owns that file)
# --------------------------------------------------------------------------- #


def _pin_lines() -> list[str]:
    """Non-comment, non-blank lines of ``requirements.txt``."""
    text = (REPO_ROOT / "requirements.txt").read_text(encoding="utf-8")
    return [line.strip() for line in text.splitlines() if line.strip() and not line.strip().startswith("#")]


def test_requirements_keeps_every_original_pin() -> None:
    """No pin was removed, relaxed or reordered by Phase 0."""
    pins = _pin_lines()

    assert pins == list(ORIGINAL_PINS), (
        "requirements.txt no longer holds the frozen environment; only a comment may be appended"
    )


def test_requirements_documents_itself_as_append_only() -> None:
    """The appended comment states the rule and names the extras it points at."""
    text = (REPO_ROOT / "requirements.txt").read_text(encoding="utf-8")

    assert "append-only" in text
    for extra in ("route", "providers", "demo"):
        assert f".[{extra}]" in text, f"the appended comment must name the {extra!r} extra"


def test_requirements_pins_are_the_installed_environment() -> None:
    """Every pinned package is importable in this interpreter."""
    missing = []
    for pin in ORIGINAL_PINS:
        name = _canonical_name(pin)
        try:
            __import__(name)
        except ImportError:
            missing.append(f"{pin} (imports as {name})")

    assert missing == [], f"pinned but not importable: {missing}"
