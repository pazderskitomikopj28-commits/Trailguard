"""Secret-hygiene and git-ignore tests (hard requirement: no credential in the repo).

Responsibility: prove the three things that keep this repository safe to publish:

1. ``.env`` is excluded by ``.gitignore`` *and* by git itself, so a live
   credential file cannot be committed, staged or pushed;
2. ``.env.example`` stays committable and names every required variable **with an
   empty value**, so a collaborator knows what to supply without ever being given
   the value;
3. no file git would commit -- tracked or untracked -- contains an assigned
   secret, so the history stays clean.

Explicit non-responsibility: this module never *reads* ``.env`` (or any
``*.key`` / ``*.pem`` file). Reading a live credential file in order to prove it
is absent would itself be the leak; existence is asserted, content is never
opened. Rotating or issuing credentials is outside the repository entirely.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest

from conftest import REPO_ROOT, SECRET_KEY_SAMPLES

#: Directories that never hold source and are skipped by the scan.
IGNORED_DIRECTORIES: frozenset[str] = frozenset(
    {".git", ".venv", "__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache", ".idea", ".vscode"}
)

#: Filenames that are never opened by this suite, in any test.
UNREADABLE_FILENAMES: frozenset[str] = frozenset({".env"})

#: Credential-bearing file suffixes, by convention.
CREDENTIAL_SUFFIXES: frozenset[str] = frozenset({".key", ".pem", ".p12", ".pfx"})

#: The credential key families the repo's own redaction policy names.
SECRET_KEY_NAMES: tuple[str, ...] = (
    "api_key",
    "apikey",
    "access_key",
    "secret",
    "secret_key",
    "token",
    "auth_token",
    "password",
    "passwd",
    "authorization",
    "credential",
)

def _key_name_alternation() -> str:
    """One regex alternation over the credential key families, ``_``/``-`` tolerant."""
    return "|".join(name.replace("_", "[_-]?") for name in SECRET_KEY_NAMES)


#: A quoted assignment: YAML, JSON, INI or a Python keyword argument. The value
#: must be substantial -- an empty ``KEY=`` placeholder is the documented and
#: correct state for a published example file.
_QUOTED_ASSIGNMENT = re.compile(
    r"""(?ix)                                   # case-insensitive, verbose
    \b(?:{names})\b                            # a credential key name
    \s*(?::=|=>|[:=])\s*                         # an assignment operator
    ["'][^"'\s]{{12,}}["']                       # a quoted value
    """.format(names=_key_name_alternation())
)

#: A bare assignment: shell variable, environment file or a plain config line.
_BARE_ASSIGNMENT = re.compile(
    r"""(?ix)
    \b(?:{names})\b
    \s*[:=]\s*
    [A-Za-z0-9_\-]{{16,}}
    """.format(names=_key_name_alternation())
)

SECRET_ASSIGNMENT_PATTERNS: tuple[re.Pattern[str], ...] = (_QUOTED_ASSIGNMENT, _BARE_ASSIGNMENT)


# --------------------------------------------------------------------------- #
# Git plumbing (read-only)
# --------------------------------------------------------------------------- #


def _git(*args: str) -> subprocess.CompletedProcess[str]:
    """Run a read-only git command in the repository root."""
    return subprocess.run(
        ["git", *args],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )


def _tracked_files() -> list[str]:
    """Paths git currently has in the index."""
    result = _git("ls-files")
    return [line for line in result.stdout.splitlines() if line]


def _committable_files() -> list[str]:
    """Every path git would offer for a commit: tracked and untracked alike.

    ``-uall`` expands untracked directories to individual files, so the scan sees
    a repo that has not been committed yet (which is exactly the current state).
    """
    result = _git("status", "--porcelain", "--untracked-files=all", "-z")
    entries = [chunk for chunk in result.stdout.split("\0") if chunk]
    paths: list[str] = []
    for entry in entries:
        # Porcelain v1 with -z: "XY<space><path>" (two status chars, one space).
        path = entry[3:] if len(entry) > 3 else entry
        if path:
            paths.append(path)
    return paths


def _scannable_files() -> list[Path]:
    """Committable files that are safe and worth scanning for secrets."""
    files: list[Path] = []
    for raw in _committable_files():
        path = REPO_ROOT / raw
        if any(part in IGNORED_DIRECTORIES for part in path.parts):
            continue
        if path.name in UNREADABLE_FILENAMES:
            continue
        if not path.is_file():
            continue
        files.append(path)
    return files


def _secret_findings(path: Path) -> list[str]:
    """Every assigned-secret-looking match in one file, as ``key operator value``."""
    try:
        text = path.read_text(encoding="utf-8")
    except (UnicodeDecodeError, OSError):
        return []  # binary or unreadable: not a credential text file
    return [match.group(0).strip() for pattern in SECRET_ASSIGNMENT_PATTERNS for match in pattern.finditer(text)]


# --------------------------------------------------------------------------- #
# .env is excluded
# --------------------------------------------------------------------------- #


def test_gitignore_exists_and_excludes_the_environment_file() -> None:
    """``.gitignore`` declares the secrets block, not just a single line."""
    gitignore = REPO_ROOT / ".gitignore"
    assert gitignore.is_file(), "the repository must ship a .gitignore"
    lines = {line.strip() for line in gitignore.read_text(encoding="utf-8").splitlines()}

    assert ".env" in lines, ".env must be ignored by name"
    assert ".env.*" in lines, "variant environment files must be ignored too"
    for credential_pattern in ("*.key", "*.pem", "secrets/", "credentials.json"):
        assert credential_pattern in lines, f".gitignore must exclude {credential_pattern}"
    assert "!.env.example" in lines, "the example file must be explicitly re-included"


def test_git_confirms_dot_env_is_excluded() -> None:
    """git itself, not just the text of ``.gitignore``, excludes ``.env``."""
    result = _git("check-ignore", ".env")

    assert result.returncode == 0, (
        "git does not consider .env ignored; a live credential file could be committed"
    )
    assert ".env" in result.stdout
    # The matched pattern must be a positive exclusion, not a negation.
    assert "!.env" not in result.stdout


def test_dot_env_is_absent_from_the_working_tree() -> None:
    """A submitted repository carries no live credential file to leak.

    Only the filename is inspected: opening ``.env`` to assert on its content
    would be the leak this test exists to prevent.
    """
    assert not (REPO_ROOT / ".env").exists(), ".env must not be committed or copied into the repository"
    variants = sorted(p.name for p in REPO_ROOT.glob(".env.*") if p.name != ".env.example")
    assert not variants, f"variant .env files must not be carried either: {variants}"


def test_no_credential_suffix_file_is_committable() -> None:
    """No ``*.key`` / ``*.pem`` / ``secrets/`` path reaches the committable set."""
    offenders = [
        raw
        for raw in _committable_files()
        if Path(raw).suffix in CREDENTIAL_SUFFIXES
        or any(part in {"secrets"} for part in Path(raw).parts)
        or Path(raw).name == "credentials.json"
    ]

    assert offenders == [], f"credential-bearing paths must not be committable: {offenders}"


# --------------------------------------------------------------------------- #
# .env.example stays usable and value-free
# --------------------------------------------------------------------------- #


def test_env_example_is_committable() -> None:
    """The example file is tracked-able: git does not exclude it."""
    assert (REPO_ROOT / ".env.example").is_file(), "the repo must ship .env.example"
    result = _git("check-ignore", "--quiet", "--no-index", ".env.example")

    assert result.returncode != 0, ".env.example is excluded by git and could never be committed"


def _example_assignments() -> list[tuple[str, str]]:
    """``(key, value)`` pairs declared in ``.env.example``, comments excluded."""
    entries: list[tuple[str, str]] = []
    for line in (REPO_ROOT / ".env.example").read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, _, value = stripped.partition("=")
        entries.append((key.strip(), value.strip().strip("'\"")))
    return entries


def test_env_example_declares_credential_slots_without_values() -> None:
    """Every credential-shaped slot is declared and empty -- no value to leak."""
    assignments = _example_assignments()
    assert assignments, ".env.example must declare at least one variable"

    secret_like = [
        (key, value)
        for key, value in assignments
        if any(pattern.search(key) for pattern in SECRET_ASSIGNMENT_PATTERNS) or "KEY" in key.upper()
    ]
    assert secret_like, "the key names should be annotated by the scanned patterns"

    populated = [key for key, value in secret_like if value]
    assert populated == [], f".env.example must not carry real values for {populated}; use placeholders"


def test_env_example_still_marks_the_model_configuration() -> None:
    """Non-secret settings (base URL, model id) may hold real, non-secret values."""
    values = dict(_example_assignments())

    assert values.get("STEPFUN_BASE_URL", "").startswith("https://")
    assert values.get("STEPFUN_MODEL"), "the example should name the default model"


def test_env_example_values_are_not_secret_assignments_themselves() -> None:
    """The example file passes the same secret scan as every other source file."""
    findings = _secret_findings(REPO_ROOT / ".env.example")

    assert findings == [], f".env.example itself looks like it holds a secret: {findings}"


# --------------------------------------------------------------------------- #
# No file git would commit holds an assigned secret
# --------------------------------------------------------------------------- #


def test_scanned_set_is_not_empty() -> None:
    """Guard: the scan below cannot silently turn into a no-op."""
    files = _scannable_files()

    assert files, "no committable files were found; the secret scan would be vacuous"
    assert len(files) > 20, f"expected the whole source tree, found only {len(files)} files"


def test_no_committable_file_contains_an_assigned_secret() -> None:
    """Every file git would commit passes the assigned-secret scan."""
    offenders = {str(path.relative_to(REPO_ROOT)): _secret_findings(path) for path in _scannable_files()}

    findings = {path: hits for path, hits in offenders.items() if hits}
    assert findings == {}, f"assigned secrets found in committable files: {findings}"


def test_no_tracked_file_contains_an_assigned_secret() -> None:
    """The same scan over the git index, ready for the first commit."""
    tracked = _tracked_files()

    offenders = {}
    for raw in tracked:
        path = REPO_ROOT / raw
        if any(part in IGNORED_DIRECTORIES for part in path.parts):
            continue
        if path.name in UNREADABLE_FILENAMES or not path.is_file():
            continue
        hits = _secret_findings(path)
        if hits:
            offenders[raw] = hits

    assert offenders == {}, f"assigned secrets found in tracked files: {offenders}"
    # Nothing tracked may live in an ignored directory or be a credential file.
    assert not [raw for raw in tracked if Path(raw).suffix in CREDENTIAL_SUFFIXES], (
        "a credential-bearing file is already tracked; rotate it and amend history"
    )


def test_dot_env_is_neither_tracked_nor_committable() -> None:
    """``.env`` appears in neither the index nor the untracked listing."""
    assert ".env" not in _tracked_files()
    assert ".env" not in _committable_files()


# --------------------------------------------------------------------------- #
# The patterns cover the policy's own key names
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("secret_key", SECRET_KEY_SAMPLES)
def test_patterns_catch_every_credential_key_name(secret_key: str) -> None:
    """The scan flags each key family the trace-redaction policy names."""
    quoted = f'{secret_key}: "0123456789abcdef"'
    bare = f"{secret_key}=0123456789abcdefghij"

    assert any(pattern.search(quoted) for pattern in SECRET_ASSIGNMENT_PATTERNS), (
        f"{secret_key} with a quoted value is not detected"
    )
    assert any(pattern.search(bare) for pattern in SECRET_ASSIGNMENT_PATTERNS), (
        f"{secret_key} with a bare value is not detected"
    )


def test_patterns_ignore_empty_placeholders() -> None:
    """``KEY=`` and ``KEY=""`` are the documented state of a published example."""
    for placeholder in ("api_key=", "api_key=\"\"", "token: ''", "password: null", "credential={}"):
        assert not any(pattern.search(placeholder) for pattern in SECRET_ASSIGNMENT_PATTERNS), (
            f"{placeholder} is an empty placeholder and must not be reported as a secret"
        )
