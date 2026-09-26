"""Runtime loader for the TrailGuard SKILL.md instruction file (spec 15, 3.1).

The skill file is plain markdown prose authored for the model. It is *loaded*,
never parsed: this module deliberately exposes no API for extracting rules,
thresholds, horizons or algorithms from it. The reason is the fairness
requirement of spec 3.1 -- the with-skill condition must be "same harness, same
tools, same model, same context, plus the SKILL.md text in the system prompt",
and any interpretation of the skill text on the Python side would give the
baseline and the treatment different effective behaviour.

Loading is intentionally lazy: :func:`load_skill_md` is called at run time by
the harness, never at import time, so importing ``trailguard.agent`` works even
when the repository has no SKILL.md yet.

Identity versus interpretation
------------------------------
:func:`skill_identity` reads two things about the skill text -- the ``version:``
line of its YAML frontmatter and a SHA-256 of the exact bytes loaded -- and
nothing else. That is provenance, not interpretation: it answers "which text was
injected into this run's system prompt", which the evidence trace needs so a
benchmark row can be tied to the protocol version that produced it. It does not
answer "what does this text ask the model to do", and it must never grow into
that: the moment Python reads horizons, freshness budgets or explanation blocks
out of the skill, the treatment stops being a single variable.
"""

from __future__ import annotations

import hashlib
import re
from pathlib import Path

#: Repository root (``.../dgx-spark-hackathon``), resolved from
#: ``trailguard/agent/skill_loader.py`` -> parents[2].
REPO_ROOT: Path = Path(__file__).resolve().parents[2]

#: Default location of the skill file (spec 24 repository layout).
DEFAULT_SKILL_PATH: Path = REPO_ROOT / "SKILL.md"

#: The one frontmatter field identity reports. Deliberately a single named
#: line rather than a YAML document: parsing the whole frontmatter would be the
#: first step towards reading protocol content on the Python side, which spec
#: 3.1 forbids (see the module docstring).
_VERSION_LINE = re.compile(r"^[ \t]*version[ \t]*:[ \t]*(?P<value>.+?)[ \t]*$", re.MULTILINE)
_FRONTMATTER_FENCE = "---"


def _frontmatter(text: str) -> str:
    """Return the leading YAML frontmatter block, or ``""`` when there is none.

    A regex over the whole document would happily match a ``version:`` line in
    the prose body -- a code sample, a changelog paragraph -- and report a
    version the skill never declared. Fencing the search to the opening block
    keeps the reported version the one the file's frontmatter actually states,
    and a file with no frontmatter reports no version rather than a guessed one.
    """
    if not text.startswith(_FRONTMATTER_FENCE):
        return ""
    end = text.find(f"\n{_FRONTMATTER_FENCE}", len(_FRONTMATTER_FENCE))
    return text[:end] if end != -1 else ""


def load_skill_md(path: Path | None = None) -> str:
    """Return the raw markdown text of the TrailGuard skill file.

    Responsibility: read the skill file from disk and hand its exact text back
    to the caller (the agent harness), which injects it into the system prompt.
    Not responsible for: parsing, validating, summarising or interpreting the
    skill content -- no algorithm, threshold or protocol is extracted here, and
    no caching or rewriting happens, because any such transformation would break
    the single-variable fairness rule of spec 3.1.

    Args:
        path: Explicit skill file path. ``None`` means
            :data:`DEFAULT_SKILL_PATH` (``<repo root>/SKILL.md``).

    Returns:
        The file's markdown text, exactly as stored.

    Raises:
        FileNotFoundError: The path does not exist. Propagated by the harness so
            a "with skill" run never silently degrades into a baseline run.
        ValueError: The file exists but is empty (or whitespace only).
    """
    target = Path(path) if path is not None else DEFAULT_SKILL_PATH

    if not target.exists():
        raise FileNotFoundError(
            f"TrailGuard skill file not found: {target}. Pass skill_path=..., "
            "or run with skill_enabled=False for the baseline condition."
        )

    text = target.read_text(encoding="utf-8")
    if not text.strip():
        raise ValueError(f"TrailGuard skill file is empty: {target}")

    return text


class SkillIdentity:
    """Which skill text a run was given (spec 18 evidence, not interpretation).

    Responsibility: carry the version string and the SHA-256 of the exact bytes
    that were injected into the system prompt, so an evidence trace can record
    *which* protocol produced a run. Not responsible for: describing what the
    protocol asks for -- no horizon, threshold or rule is read out of the text.
    """

    __slots__ = ("sha256", "version")

    def __init__(self, version: str | None, sha256: str) -> None:
        self.version = version
        self.sha256 = sha256

    def __repr__(self) -> str:
        return f"SkillIdentity(version={self.version!r}, sha256={self.sha256!r})"

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, SkillIdentity):
            return NotImplemented
        return self.version == other.version and self.sha256 == other.sha256

    def __hash__(self) -> int:
        return hash((self.version, self.sha256))


def skill_identity(text: str) -> SkillIdentity:
    """Return the identity of an already-loaded skill text.

    The hash is taken over the string that was loaded -- which is the exact
    string the harness injects into the system prompt -- and not over a
    re-read of the file. Those are the same bytes only when nothing rewrites the
    file between the two reads, and the run's evidence is what was injected, so
    the hash must be of the injected text.

    Args:
        text: Skill text as returned by :func:`load_skill_md`.

    Returns:
        A :class:`SkillIdentity` whose ``version`` is ``None`` when the
        frontmatter declares none, and whose ``sha256`` is the hex digest of the
        text encoded as UTF-8.
    """
    match = _VERSION_LINE.search(_frontmatter(text))
    version = match.group("value").strip().strip("\"'") if match else None
    digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
    return SkillIdentity(version=version, sha256=digest)
