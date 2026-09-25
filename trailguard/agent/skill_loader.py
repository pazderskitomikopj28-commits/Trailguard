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
"""

from __future__ import annotations

from pathlib import Path

#: Repository root (``.../dgx-spark-hackathon``), resolved from
#: ``trailguard/agent/skill_loader.py`` -> parents[2].
REPO_ROOT: Path = Path(__file__).resolve().parents[2]

#: Default location of the skill file (spec 24 repository layout).
DEFAULT_SKILL_PATH: Path = REPO_ROOT / "SKILL.md"


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
