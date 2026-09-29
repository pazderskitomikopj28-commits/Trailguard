#!/usr/bin/env python
"""Pre-flight the demo: say, before a judge arrives, what will and will not work.

Deliberately separate from ``scripts/demo_smoke.py``. The smoke gate proves the
demo is *correct*; this proves it is *runnable*. A correct demo that will not start
is still a failed presentation, so every dependency and every asset is checked and
reported with the one command that fixes it.

Nothing here is fatal on its own. The only outcome is a printed inventory in three
groups -- **ready**, **degraded** (works, but a feature is unavailable and the UI
will say so) and **missing** (the demo cannot start) -- because the truthful answer
for a half-configured laptop is usually "most of it works, and here is exactly which
part does not".

No credential is read. ``STEPFUN_API_KEY`` is checked for *presence* only, by name,
and never printed.
"""

from __future__ import annotations

import os
import shutil
import sys
from dataclasses import dataclass, field
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

#: (label, module, extra, what breaks without it). Extras match pyproject.toml.
DEPENDENCIES = [
    ("pydantic", "pydantic", None, "nothing runs"),
    ("PyYAML", "yaml", None, "nothing runs"),
    ("gpxpy", "gpxpy", "route", "the replay chain cannot read the GPX"),
    ("shapely", "shapely", "route", "the replay chain cannot measure the polyline"),
    ("pyproj", "pyproj", "route", "distances are not geodesic"),
    ("httpx", "httpx", "weather", "live weather mode (replay mode is unaffected)"),
    ("openai", "openai", "providers", "the local agent cannot be called at all"),
    ("pytest", "pytest", "dev", "the test suite cannot be run"),
]

#: Assets the demo reads, never writes.
ASSETS = [
    ("SKILL.md", REPO_ROOT / "SKILL.md", "Agent Mode has no skill protocol to load"),
    ("rule pack", REPO_ROOT / "trailguard" / "rules" / "rules_v1.yaml", "no deterministic core"),
    ("calibration", REPO_ROOT / "trailguard" / "rules" / "calibration_v1.yaml", "no deterministic core"),
    ("scenarios", REPO_ROOT / "scenarios", "no scenario to replay"),
    ("route GPX", REPO_ROOT / "replay" / "routes", "no route to draw"),
    ("device feeds", REPO_ROOT / "replay" / "devices", "no device state to show"),
    ("weather snapshots", REPO_ROOT / "replay" / "weather", "Replay Mode has no weather"),
    ("Phase 5 references", REPO_ROOT / "evals" / "phase5" / "references", "the demo cannot be verified against the benchmark"),
    ("Phase 5 evidence", REPO_ROOT / "docs" / "phase5_evidence", "the Benchmark tab has nothing to show"),
]

FROZEN_SKILL_SHA256 = "98fd007eb23aee18f98bf67c295d7bad3c8e48f7a722022c852218fb0c649769"


@dataclass
class Row:
    kind: str  # ready | degraded | missing
    label: str
    detail: str
    fix: str = ""


@dataclass
class Preflight:
    rows: list[Row] = field(default_factory=list)

    def add(self, kind: str, label: str, detail: str, fix: str = "") -> None:
        self.rows.append(Row(kind, label, detail, fix))

    @property
    def ready(self) -> list[Row]:
        return [row for row in self.rows if row.kind == "ready"]

    @property
    def degraded(self) -> list[Row]:
        return [row for row in self.rows if row.kind == "degraded"]

    @property
    def missing(self) -> list[Row]:
        return [row for row in self.rows if row.kind == "missing"]

    @property
    def runnable(self) -> bool:
        return not self.missing


def _import_version(module_name: str) -> str | None:
    try:
        module = __import__(module_name)
    except ImportError:
        return None
    return str(getattr(module, "__version__", "present"))


def check_dependencies(report: Preflight) -> None:
    for label, module_name, extra, consequence in DEPENDENCIES:
        version = _import_version(module_name)
        if version is not None:
            report.add("ready", f"{label}", f"version {version}")
            continue
        install = ".venv/bin/pip install -e '.[{0}]'".format(extra) if extra else "unknown"
        report.add(
            "missing" if extra in (None, "route", "providers") else "degraded",
            f"{label}",
            f"not installed -- {consequence}",
            install,
        )


def check_assets(report: Preflight) -> None:
    for label, path, consequence in ASSETS:
        if path.is_dir():
            count = sum(1 for _ in path.rglob("*") if _.is_file())
            report.add("ready", label, f"{path.relative_to(REPO_ROOT)}/ ({count} files)")
            continue
        if path.is_file():
            size = path.stat().st_size
            report.add("ready", label, f"{path.relative_to(REPO_ROOT)} ({size} bytes)")
            continue
        report.add("missing", label, f"{path.relative_to(REPO_ROOT)} is absent -- {consequence}")

    skill = REPO_ROOT / "SKILL.md"
    if skill.is_file():
        import hashlib

        digest = hashlib.sha256(skill.read_bytes()).hexdigest()
        if digest == FROZEN_SKILL_SHA256:
            report.add("ready", "SKILL.md identity", "sha256 matches the hash Phase 5 evaluated")
        else:
            report.add(
                "degraded",
                "SKILL.md identity",
                f"sha256 is {digest[:16]}..., not the frozen {FROZEN_SKILL_SHA256[:16]}...",
                "git diff SKILL.md -- the demo must never edit the skill to make an agent run better",
            )


def check_demo_runtime(report: Preflight) -> None:
    streamlit_version = _import_version("streamlit")
    if streamlit_version is None:
        report.add(
            "degraded",
            "Streamlit UI",
            "streamlit>=1.40 is not installed",
            ".venv/bin/pip install 'streamlit>=1.40'  (or: pip install -e '.[demo]')",
        )
    else:
        report.add("ready", "Streamlit UI", f"streamlit {streamlit_version}")

    from demo.presenters.benchmark_view import EVIDENCE_DIR

    if EVIDENCE_DIR.is_dir():
        files = sorted(p.name for p in EVIDENCE_DIR.iterdir())
        report.add("ready", "Phase 5 evidence files", ", ".join(files))
    else:
        report.add(
            "missing",
            "Phase 5 evidence files",
            f"{EVIDENCE_DIR} is absent",
            "the Benchmark tab cannot show numbers that were never committed",
        )

    try:
        from demo.presenters.agent_view import UNAVAILABLE, probe_local_agent

        availability = probe_local_agent()
    except Exception as exc:  # noqa: BLE001 - a probe must never crash a preflight
        report.add("degraded", "Local Agent", f"probe failed: {type(exc).__name__}: {exc}")
    else:
        if availability.available:
            limit = availability.max_model_len
            detail = f"{availability.model} at {availability.base_url}"
            if limit is not None and limit != 16384:
                detail += f", max_model_len={limit} (Phase 5 froze 16384)"
            report.add("ready", "Local Agent", detail)
        else:
            report.add(
                "degraded",
                "Local Agent",
                f"{UNAVAILABLE} -- {availability.reason} ({availability.detail})",
                "start the vLLM server; the Agent tab will show 'Local Agent unavailable' "
                "and Replay Mode is unaffected",
            )

    # Remote model: presence only, never the value (brief security rule).
    if "STEPFUN_API_KEY" in os.environ:
        report.add("ready", "Remote model credential", "present in the environment (value not read)")
    else:
        report.add("ready", "Remote model credential", "not configured -- Phase 5 recorded this arm NOT RUN")

    runs = REPO_ROOT / "runs"
    if runs.is_dir() and os.access(runs, os.W_OK):
        report.add("ready", "runs/ directory", "present and writable (gitignored)")
    else:
        report.add("degraded", "runs/ directory", "absent or read-only", "mkdir -p runs")

    python = sys.version_info
    report.add("ready", "Python", f"{python.major}.{python.minor}.{python.micro} on {sys.platform}")
    if shutil.which("streamlit"):
        report.add("ready", "streamlit on PATH", str(shutil.which("streamlit")))
    else:
        report.add("degraded", "streamlit on PATH", "not on PATH; launch with .venv/bin/streamlit")


def render(report: Preflight) -> int:
    print("=" * 78)
    print("TrailGuard demo pre-flight")
    print("=" * 78)
    for group, title, marker in (
        (report.missing, "MISSING -- the demo cannot run as intended", "x"),
        (report.degraded, "DEGRADED -- runs, but a feature is unavailable and the UI says so", "!"),
        (report.ready, "READY", "+"),
    ):
        print(f"\n{title} ({len(group)})")
        for row in group:
            print(f"  [{marker}] {row.label}")
            print(f"        {row.detail}")
            if row.fix:
                print(f"        fix: {row.fix}")

    print("\n" + "-" * 78)
    if report.runnable:
        print("Verdict: RUNNABLE. Start the demo with scripts/start_demo.sh")
        return 0
    print("Verdict: NOT RUNNABLE. Resolve the MISSING rows above first; the DEGRADED rows")
    print("are expected on a competition laptop and the demo discloses each one on screen.")
    return 1


def main() -> int:
    report = Preflight()
    check_dependencies(report)
    check_assets(report)
    check_demo_runtime(report)
    return render(report)


if __name__ == "__main__":
    raise SystemExit(main())
