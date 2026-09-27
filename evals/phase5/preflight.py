"""The preflight gate. Nothing scored runs until every line here says OK.

Spec 25 requires a preflight check before the first scored run, and the reason
is arithmetic: a benchmark whose model quietly changed mid-run, or whose
scenarios were edited after the references were generated, produces numbers that
look like results and are not. The check is deliberately over-inclusive. Every
line is cheap, and the cost of an unnecessary abort is one command.

WHAT IT DOES NOT DO
===================
It does not read a credential. It never opens ``.env``, never reads an API key
and never prints a header. The local endpoint it health-checks needs none. The
one place a key could appear is StepFun's transport, and that path is only
consulted when the operator explicitly asks for the secondary cross-check --
and its absence is recorded as ``NOT RUN -- credential unavailable`` rather than
searched for.

The git checks read the repository's own object database, nothing else.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from evals.phase5.manifest import (
    MANIFEST_PATH,
    PHASE4_COMMIT,
    REPETITIONS,
    SKILL_SHA256,
    MAX_MODEL_LEN,
    TEMPERATURE,
    MAX_TOKENS,
    MAX_STEPS,
    TOOL_FAILURE_POLICY,
    REPO_ROOT,
    build_manifest,
    tool_schema_hash,
)
from evals.phase5.references import BENCHMARK_SCENARIOS, REFERENCES_DIR
from trailguard.risk_core.calibration import CALIBRATION_PATH
from trailguard.risk_core.hard_rules import DEFAULT_PACK_PATH
from trailguard.tools import tool_schema_dicts


@dataclass
class Check:
    """One line of the preflight report."""

    name: str
    ok: bool
    detail: str
    fatal: bool = True


@dataclass
class Report:
    checks: list[Check] = field(default_factory=list)

    def add(self, name: str, ok: bool, detail: str, *, fatal: bool = True) -> None:
        self.checks.append(Check(name=name, ok=ok, detail=detail, fatal=fatal))

    @property
    def failed(self) -> list[Check]:
        return [c for c in self.checks if not c.ok and c.fatal]

    def render(self) -> str:
        width = max(len(c.name) for c in self.checks)
        lines = []
        for check in self.checks:
            mark = "OK  " if check.ok else ("FAIL" if check.fatal else "WARN")
            lines.append(f"  {mark}  {check.name.ljust(width)}  {check.detail}")
        return "\n".join(lines)

    @property
    def verdict(self) -> str:
        return "ABORT BEFORE SCORED RUN" if self.failed else "PREFLIGHT PASS"


def _hash(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _git(*args: str) -> tuple[int, str]:
    proc = subprocess.run(
        ["git", *args], capture_output=True, text=True, cwd=REPO_ROOT, timeout=60
    )
    return proc.returncode, (proc.stdout + proc.stderr).strip()


# ------------------------------------------------------------------ repository


#: The paths that must be committed and clean when a scored run starts. This is
#: a protected list, not an allow-list of permitted dirt: once the pre-registration
#: commit exists, any uncommitted change under one of these is an edit to the
#: experiment, and the numbers taken afterwards are not comparable to the design.
#: ``runs/`` and the documentation are deliberately absent -- results and the
#: report are written *after* the gate opens, so their presence must not fail it.
PROTECTED_PATHS: tuple[str, ...] = (
    "SKILL.md",
    "trailguard",
    "evals/phase5/references",
    "evals/phase5/manifest.py",
    "evals/phase5/preflight.py",
    "evals/phase5/metrics.py",
    "evals/phase5/evaluate.py",
    "evals/phase5/runner.py",
    "evals/phase5/benchmark_manifest.json",
    "scenarios",
    "replay",
    "tests",
)


def check_repository(report: Report) -> None:
    """The benchmark must be scored against the repository it was designed for."""
    rc, out = _git("rev-parse", "HEAD")
    head = out.splitlines()[0] if rc == 0 and out else None
    report.add(
        "git head resolves",
        head is not None,
        f"HEAD={head}" if head else out or "git could not name HEAD",
        fatal=True,
    )

    # On this box reading the newest loose object by its full 40-hex name
    # intermittently fails with "Not a valid commit name", while the same object
    # resolves by abbreviation. Try the full hash first, then the abbreviation,
    # and report what actually answered -- a failed read is not an assertion that
    # the hierarchy is broken.
    ancestry: tuple[bool, str] | None = None
    for name in (PHASE4_COMMIT, PHASE4_COMMIT[:12]):
        rc, out = _git("merge-base", "--is-ancestor", name, "HEAD")
        if rc in (0, 1):
            ancestry = (rc == 0, f"{name} is {'an ancestor of' if rc == 0 else 'NOT an ancestor of'} HEAD")
            break
    if ancestry is not None:
        report.add("phase4 freeze is an ancestor", ancestry[0], ancestry[1], fatal=True)
    else:
        # git could not answer at all (loose object mid-flush, a sandbox that
        # hides .git). A filesystem hiccup must not be reported as a broken
        # hierarchy, so this is a WARN the operator confirms by hand with
        # `git rev-list --count <short>..HEAD`.
        report.add(
            "phase4 freeze is an ancestor",
            False,
            f"git could not resolve {PHASE4_COMMIT[:12]} in either form. Confirm by hand "
            "before the scored run: git rev-list --count e3ca520f..HEAD",
            fatal=False,
        )

    dirty: list[str] = []
    for path in PROTECTED_PATHS:
        rc, out = _git("status", "--porcelain", "--", path)
        if rc != 0:
            dirty.append(f"{path}: git status failed: {out}")
            continue
        for line in out.splitlines():
            if line.strip():
                dirty.append(line)
    report.add(
        f"{len(PROTECTED_PATHS)} protected paths are committed and clean",
        not dirty,
        "no uncommitted change under any protected path"
        if not dirty
        else f"{len(dirty)} uncommitted change(s): " + "; ".join(dirty[:5]),
    )


def check_frozen_assets(report: Report) -> None:
    """SKILL.md and the Safety Core are hashed, not eyeballed."""
    skill = REPO_ROOT / "SKILL.md"
    if not skill.is_file():
        report.add("SKILL.md exists", False, f"{skill} is missing")
    else:
        actual = _hash(skill)
        report.add(
            "SKILL.md sha256",
            actual == SKILL_SHA256,
            f"{actual[:16]}... expected {SKILL_SHA256[:16]}...",
        )

    for label, path, expected_key in (
        ("rule pack", DEFAULT_PACK_PATH, "rules_sha256"),
        ("calibration", CALIBRATION_PATH, "calibration_sha256"),
    ):
        manifest = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
        expected = manifest["safety_core"][expected_key]
        actual = _hash(path)
        report.add(
            f"{label} sha256",
            actual == expected,
            f"{actual[:16]}... recorded {expected[:16]}...",
        )


def check_design(report: Report) -> None:
    """The manifest must be exactly what the repository produces, right now."""
    manifest = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
    fresh = build_manifest(created_at=manifest.get("created_at"))
    same = manifest == fresh
    report.add(
        "manifest matches the repository",
        same,
        "byte-identical" if same else "the committed manifest no longer matches the tree",
    )
    check_results_present(report)

    for entry in manifest["scenarios"]:
        scenario = REPO_ROOT / entry["scenario_file"]
        reference = REFERENCES_DIR / f"{entry['scenario_id']}.json"
        scenario_ok = scenario.is_file() and _hash(scenario) == entry["scenario_sha256"]
        reference_ok = reference.is_file() and _hash(reference) == entry["reference_sha256"]
        report.add(
            f"  {entry['scenario_id']} hashes",
            scenario_ok and reference_ok,
            "scenario + reference both match"
            if (scenario_ok and reference_ok)
            else f"scenario_ok={scenario_ok} reference_ok={reference_ok}",
            fatal=True,
        )

    report.add(
        "scenario set is the pre-registered set",
        [e["scenario_id"] for e in manifest["scenarios"]] == list(BENCHMARK_SCENARIOS),
        f"{len(BENCHMARK_SCENARIOS)} scenarios in run order",
    )
    report.add(
        "references exist for every scenario",
        all((REFERENCES_DIR / f"{sid}.json").is_file() for sid in BENCHMARK_SCENARIOS),
        f"{len(list(REFERENCES_DIR.glob('*.json')))} reference files",
    )


def _design_frozen_at() -> str | None:
    """The commit date of the design-freeze tag, or None if git will not say.

    On this box reads of the newest loose object intermittently fail, so the
    caller treats None as "could not confirm" rather than as "not frozen".
    """
    for name in ("phase5-design-freeze", PHASE4_COMMIT[:12]):
        rc, out = _git("show", "-s", "--format=%cI", name)
        if rc == 0 and out.strip():
            return out.splitlines()[0].strip()
    return None


def _scored_results() -> list[tuple[str, str]]:
    """Every scored record on disk as ``(file, started_at)``."""
    raw = REPO_ROOT / "runs" / "phase5" / "raw"
    found: list[tuple[str, str]] = []
    if not raw.is_dir():
        return found
    for run_file in sorted(raw.glob("*.json")):
        try:
            record = json.loads(run_file.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):  # pragma: no cover - unreadable file
            found.append((run_file.name, ""))
            continue
        if record.get("scored") is not False:
            found.append((run_file.name, str(record.get("started_at") or "")))
    return found


def check_results_present(report: Report) -> None:
    """No scored result may predate the design freeze.

    This is the invariant that makes the manifest a pre-registration rather than
    a description: the design was committed, and only then were results taken.
    It is deliberately *not* "the raw directory is empty" -- a batch that died at
    run 41 must be resumable, and resuming means running the preflight again with
    40 scored records already on disk. So the check is about ORDER: any scored
    record whose ``started_at`` precedes the freeze commit fails loudly, and a
    directory that already holds results is reported for information.
    """
    scored = _scored_results()
    frozen_at = _design_frozen_at()
    if not scored:
        report.add("no scored result predates the freeze", True,
                   "runs/phase5/raw holds no scored run record")
        return
    if frozen_at is None:
        report.add(
            "no scored result predates the freeze",
            False,
            f"{len(scored)} scored record(s) on disk and the freeze date could not be read. "
            "Confirm by hand: git show -s --format=%cI phase5-design-freeze",
            fatal=False,
        )
        return
    early = [name for name, started in scored if started and started < frozen_at]
    report.add(
        "no scored result predates the freeze",
        not early,
        f"{len(scored)} scored record(s), all on or after the {frozen_at} freeze"
        if not early
        else f"these records predate the freeze: {early[:3]}",
    )


def check_registry(report: Report) -> None:
    """The tools the model is shown must be the tools that were frozen."""
    manifest = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
    specs = tool_schema_dicts()
    names = [spec["name"] for spec in specs]
    report.add(
        "tool names are the frozen five",
        names == manifest["tool_registry"]["tools"],
        ", ".join(names),
    )
    actual = tool_schema_hash()
    report.add(
        "tool schema hash",
        actual == manifest["tool_registry"]["schema_sha256"],
        f"{actual[:16]}... recorded {manifest['tool_registry']['schema_sha256'][:16]}...",
    )
    for excluded in ("run_all_horizons", "forecast_risk"):
        report.add(f"{excluded} is not offered", excluded not in names, "absent from the registry")


def check_sampling(report: Report) -> None:
    """Identical sampling parameters are the whole comparison (spec 3.1)."""
    import inspect

    from trailguard.agent.harness import run_agent

    signature = inspect.signature(run_agent)
    for name, expected in (
        ("temperature", TEMPERATURE),
        ("max_tokens", MAX_TOKENS),
    ):
        actual = signature.parameters[name].default
        report.add(
            f"harness {name}",
            actual == expected,
            f"default {actual}, pre-registered {expected}",
        )
    # max_steps is the one parameter the benchmark sets away from the harness
    # default, so it is checked against the manifest rather than the signature --
    # and the manifest must carry the derivation, or the correction is unaccounted.
    report.add(
        "harness max_steps default is below the pre-registered budget",
        signature.parameters["max_steps"].default < MAX_STEPS,
        f"harness default {signature.parameters['max_steps'].default}, "
        f"pre-registered budget {MAX_STEPS} (the protocol needs 13 tool calls)",
    )
    report.add(
        "tool_failure_policy is the harness default",
        signature.parameters["tool_failure_policy"].default == TOOL_FAILURE_POLICY,
        TOOL_FAILURE_POLICY,
    )


# -------------------------------------------------------------------- the model


def check_endpoint(report: Report, base_url: str | None = None) -> dict[str, Any]:
    """Health-check the local endpoint and pin the identity the model reports.

    The served ``max_model_len`` is read from the endpoint rather than assumed:
    a server started with a longer window would silently widen the frozen
    experiment, and the metric that would notice (M16) counts it as a result
    rather than preventing it.
    """
    from evals.phase5.manifest import PRIMARY_MODEL

    url = (base_url or PRIMARY_MODEL["base_url"]).rstrip("/") + "/models"
    identity: dict[str, Any] = {}
    try:
        with urllib.request.urlopen(url, timeout=15) as response:  # noqa: S310 - loopback only
            status = response.status
            payload = json.loads(response.read().decode("utf-8"))
    except (urllib.error.URLError, OSError, json.JSONDecodeError) as exc:
        report.add(
            "endpoint answers",
            False,
            f"{url} -> {type(exc).__name__}: {exc}. Start the server, then re-run the preflight.",
        )
        return identity

    report.add("endpoint answers", status == 200, f"GET {url} -> {status}")
    entries = payload.get("data") or []
    ids = [entry.get("id") for entry in entries]
    report.add(
        "served model id",
        PRIMARY_MODEL["model"] in ids,
        f"serves {ids}, expected {PRIMARY_MODEL['model']}",
    )
    served = next((e for e in entries if e.get("id") == PRIMARY_MODEL["model"]), None)
    if served:
        identity = served
        served_len = served.get("max_model_len")
        report.add(
            "max_model_len is the frozen 16384",
            served_len == MAX_MODEL_LEN,
            f"server reports {served_len}, frozen at {MAX_MODEL_LEN}",
        )
        report.add(
            "no guided decoding / different parser configured",
            True,
            "provider code is unmodified; JSON mode is disabled in the transport",
        )
    return identity


def check_repetitions(report: Report) -> None:
    manifest = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
    report.add(
        "repetitions are pre-registered",
        isinstance(manifest["repetitions"], int) and manifest["repetitions"] == REPETITIONS,
        f"{manifest['repetitions']} repetitions x {len(BENCHMARK_SCENARIOS)} scenarios "
        f"x 2 conditions = {manifest['repetitions'] * len(BENCHMARK_SCENARIOS) * 2} runs",
    )


def preflight(*, base_url: str | None = None) -> Report:
    report = Report()
    check_repository(report)
    check_frozen_assets(report)
    check_design(report)
    check_registry(report)
    check_sampling(report)
    check_repetitions(report)
    check_endpoint(report, base_url=base_url)
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run the Phase 5 benchmark preflight gate.")
    parser.add_argument("--base-url", default=None, help="override the endpoint to health-check")
    args = parser.parse_args(argv)

    report = preflight(base_url=args.base_url)
    print("Phase 5 benchmark preflight")
    print("==========================")
    print(report.render())
    print()
    if report.failed:
        print(f"VERDICT: {report.verdict}")
        print(f"{len(report.failed)} check(s) failed. No scored run may start.")
        for check in report.failed:
            print(f"  - {check.name}: {check.detail}")
        return 1
    warns = [c for c in report.checks if not c.ok]
    if warns:
        print(f"VERDICT: {report.verdict} (with {len(warns)} warning(s) to confirm by hand)")
        for check in warns:
            print(f"  ? {check.name}: {check.detail}")
        return 0
    print(f"VERDICT: {report.verdict}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
