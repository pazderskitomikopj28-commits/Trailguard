"""The Evidence tab: every claim a judge can check, with its audit trail.

Spec 6, 30, 45. Trust is the deliverable here, not decoration. Three things make
it auditable rather than merely pretty:

1. **The artifacts are named and hashed.** The frozen GPX, the device feed, the
   weather snapshot and the rule pack are all identified by their sha256, taken
   from the committed Phase 5 reference so the strings a judge reads are the same
   ones the benchmark was evaluated against.
2. **The chain is shown, not summarised.** Route context, device state, then per
   horizon the projection, the forecast at the projected place and time, and the
   verdict -- with each field a tool actually returned.
3. **What is NOT known is listed.** Missing fields, stale readings and assumed
   values are on the same screen as the numbers, because a system that only shows
   its confident rows is asking to be trusted rather than showing it deserves to
   be.

Nothing in this module reads a credential, a key or an environment file, and
``EvidenceTrace.redact_mapping`` is applied to anything that arrives as a free
form mapping before it is stored here.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from pathlib import Path

from trailguard.risk_core import load_rule_pack

from .chain import HorizonRow, ReplayRun

REPO_ROOT = Path(__file__).resolve().parents[2]


def sha256_of(path: Path) -> str | None:
    """Hex digest of a file, or ``None`` when it is not on this machine.

    A missing artifact is disclosed rather than shown as empty: the reference
    records what the benchmark hashed, and if the file has moved the demo says
    so instead of silently dropping the row.
    """
    try:
        digest = hashlib.sha256()
        with path.open("rb") as handle:
            for block in iter(lambda: handle.read(65536), b""):
                digest.update(block)
        return digest.hexdigest()
    except OSError:
        return None


def _pack_artifact(role: str, relative: str, recorded: str | None) -> Artifact:
    """A frozen rule file, hashed against what the reference says was evaluated.

    The Safety Core's two files are what turn "a level appeared on screen" into "the
    level the benchmark measured", so they belong on the Evidence tab beside the four
    inputs the run consumed. The hashes were already computed here and asserted by
    the tests; a provenance exhibit that omits the rules is a provenance exhibit with
    the interesting half missing.
    """
    path = REPO_ROOT / relative
    digest = sha256_of(path)
    return Artifact(
        role=role,
        path=relative,
        sha256=digest,
        present=path.is_file(),
        note=(
            "matches the hash the benchmark evaluated"
            if digest == recorded
            else "DIFFERS from the hash the benchmark evaluated"
            if digest
            else "file not present on this machine"
        ),
    )


@dataclass(slots=True)
class Artifact:
    """One input artifact, as the reference identifies it."""

    role: str
    path: str
    sha256: str | None
    present: bool
    note: str = ""


@dataclass(slots=True)
class EvidenceRow:
    """One horizon's chain, flattened for display."""

    horizon: str
    arrival_time: str
    projection: dict[str, object]
    weather: dict[str, object]
    verdict: dict[str, object]
    margins: dict[str, object]
    evidence_refs: list[str]


@dataclass(slots=True)
class EvidenceView:
    """Everything the Evidence tab renders for one scenario."""

    scenario_id: str
    artifacts: list[Artifact]
    rule_pack: Artifact
    calibration: Artifact
    chain: list[EvidenceRow]
    rule_catalog: list[dict[str, str]]
    gaps: list[str]
    data_quality: dict[str, object]
    weather_mode: str
    weather_snapshot_ref: str | None
    provenance: list[str] = field(default_factory=list)
    #: The frozen Phase 5 reference for this scenario, as it is committed, behind a
    #: disclosure rather than on the screen. Brief 26 asks for both halves at once:
    #: the default must not be a JSON dump, and the raw bytes must still be one
    #: click away. The source is the file the benchmark hashed, not a
    #: reconstruction, so what a judge reads here is the evidence itself.
    raw_reference: str | None = None


def artifacts_for(run: ReplayRun, *, reference: dict | None = None) -> list[Artifact]:
    """The inputs the run actually consumed, hashed where the file is present.

    Paths come from the scenario, resolved against the repo root -- never from
    ``$HOME`` or an absolute path a judge's machine would not share.
    """
    spec = reference or {}
    candidates = [
        ("scenario", spec.get("scenario_file"), None),
        ("route (GPX)", spec.get("route_ref"), run.route["polyline_ref"]),
        ("device feed", spec.get("device_feed_ref"), None),
        ("weather snapshot", spec.get("weather_snapshot_ref"), run.weather_snapshot_ref),
    ]
    artifacts: list[Artifact] = []
    for role, recorded, fallback in candidates:
        relative = recorded or fallback
        if relative is None:
            artifacts.append(
                Artifact(role=role, path="(not recorded)", sha256=None, present=False,
                         note="the reference does not identify this input")
            )
            continue
        path = (REPO_ROOT / relative).resolve()
        artifact = Artifact(
            role=role,
            path=relative,
            sha256=sha256_of(path),
            present=path.is_file(),
        )
        recorded_sha = spec.get(
            {
                "scenario": "scenario_sha256",
                "route (GPX)": "route_sha256",
                "device feed": "device_feed_sha256",
                "weather snapshot": "weather_snapshot_sha256",
            }[role]
        )
        if recorded_sha is not None:
            if artifact.sha256 is None:
                artifact.note = "file not present on this machine"
            elif artifact.sha256 != recorded_sha:
                artifact.note = "DIFFERS from the hash the benchmark evaluated"
            else:
                artifact.note = "matches the hash the benchmark evaluated"
        artifacts.append(artifact)
    return artifacts


def gaps_for(run: ReplayRun) -> list[str]:
    """Everything the run could not establish, in one place.

    Taken from the contracts the tools returned -- quality scores, staleness,
    assumed values -- so the list is the system reporting on itself.
    """
    gaps: list[str] = []
    for row in run.rows:
        if row.freshness_ok is False:
            gaps.append(
                f"{row.label}: weather reading is {row.weather.freshness_minutes:.0f} minutes "
                f"old, past the {run.freshness_budget_min:.0f}-minute budget."
            )
        if getattr(row.weather, "confidence_is_assumed", False):
            gaps.append(f"{row.label}: weather confidence is an assumed value, not reported.")
        if row.projection_confidence < 1.0:
            gaps.append(
                f"{row.label}: projection confidence is {row.projection_confidence:.2f} "
                "(inputs are not all perfect)."
            )
    quality = run.data_quality or {}
    if isinstance(quality, dict) and quality:
        gaps.append(f"Input data quality as reported: {quality}")
    if run.route["off_route_distance_m"] > 0:
        gaps.append(
            f"Hiker is {run.route['off_route_distance_m']:.1f} m off the planned polyline "
            "(measured, not a judgement)."
        )
    return gaps


def build_evidence(run: ReplayRun, *, reference: dict, pack=None) -> EvidenceView:
    """Assemble the Evidence tab for a scenario.

    ``reference`` is the frozen Phase 5 reference for this scenario; it is the
    only source of the artifact hashes, so the demo quotes what was evaluated
    rather than re-deriving an identity for the same bytes.
    """
    resolved_pack = pack if pack is not None else load_rule_pack()
    pack_path = REPO_ROOT / "trailguard" / "rules" / "rules_v1.yaml"

    chain = [
        EvidenceRow(
            horizon=row.label,
            arrival_time=row.projected_arrival_time,
            projection={
                "projected position (lat, lon)": (
                    f"{row.projected_position_lat:.6f}, {row.projected_position_lon:.6f}"
                ),
                "projected altitude": (
                    f"{row.projected_alt_m:.1f} m"
                    if row.projected_alt_m is not None
                    else "not known"
                ),
                "projected arrival time": row.projected_arrival_time,
                "route progress": f"{row.projected_route_progress:.3f}",
                "distance still to walk": f"{row.projected_remaining_distance_km:.2f} km",
                "projection confidence": f"{row.projection_confidence:.2f}",
                "minutes to sunset at this horizon": f"{row.minutes_to_sunset:.1f}",
                "estimated minutes to finish": f"{row.estimated_minutes_to_finish:.1f}",
            },
            weather={
                "forecast for": row.weather.target_time.strftime("%Y-%m-%dT%H:%M:%SZ"),
                "temperature": f"{row.weather.temperature_c:.1f} C",
                "wind (mean)": f"{row.weather.wind_speed_kmh:.1f} km/h",
                "wind gust": f"{row.weather.wind_gust_kmh:.1f} km/h",
                "precipitation probability": f"{row.weather.precipitation_probability:.2f}",
                "provider code": f"{row.weather.weather_code}",
                "provider": row.weather.source,
                "reading age at use": f"{row.weather.freshness_minutes:.0f} minutes",
                "within staleness budget": (
                    "yes"
                    if row.freshness_ok
                    else ("no" if row.freshness_ok is False else "not checkable")
                ),
            },
            verdict={
                "level": row.risk_level,
                "soft score (not a probability)": str(row.risk_score),
                "hard rules triggered": ", ".join(row.hard_rules_triggered) or "none",
                "drivers": ", ".join(d["rule_id"] for d in row.drivers) or "none",
                "confidence": f"{row.confidence:.2f}",
            },
            margins={
                "time margin (min before sunset)": _fmt_minutes(row.safety_margins.get("time_margin_min")),
                "projected battery": _fmt_percent(row.safety_margins.get("projected_battery_percent")),
                "route deviation": _fmt_metres(row.safety_margins.get("route_deviation_m")),
                "weather freshness": _fmt_minutes(row.safety_margins.get("weather_freshness_min")),
                "projection confidence": _fmt_ratio(row.safety_margins.get("projection_confidence")),
            },
            evidence_refs=list(row.evidence_refs),
        )
        for row in run.rows
    ]

    rule_catalog = []
    for rid in resolved_pack.ids():
        rule = resolved_pack.get(rid)
        rule_catalog.append(
            {
                "rule": rid,
                "name": rule.name,
                "kind": rule.trigger_type,
                "severity": rule.severity.value,
                "condition": rule.condition,
                "thresholds": ", ".join(
                    f"{key}={value} {rule.units.get(key, '')}".strip()
                    for key, value in rule.thresholds.items()
                ),
                "provenance": rule.source_type,
                "demo calibration": "yes" if rule.calibration else "no",
                "version": rule.version,
            }
        )

    return EvidenceView(
        scenario_id=run.scenario_id,
        artifacts=artifacts_for(run, reference=reference),
        rule_pack=_pack_artifact(
            "rule pack",
            "trailguard/rules/rules_v1.yaml",
            reference.get("rules_sha256"),
        ),
        calibration=_pack_artifact(
            "calibration pack",
            "trailguard/rules/calibration_v1.yaml",
            reference.get("calibration_sha256"),
        ),
        chain=chain,
        rule_catalog=rule_catalog,
        gaps=gaps_for(run),
        data_quality=run.data_quality,
        weather_mode=run.weather_mode,
        weather_snapshot_ref=run.weather_snapshot_ref,
        provenance=list(run.provenance),
        raw_reference=(
            json.dumps(reference, indent=2, ensure_ascii=False) if reference else None
        ),
    )


def _fmt_minutes(value: object) -> str:
    return f"{float(value):.1f} min" if isinstance(value, (int, float)) else "not known"


def _fmt_percent(value: object) -> str:
    return f"{float(value):.1f} %" if isinstance(value, (int, float)) else "not known"


def _fmt_metres(value: object) -> str:
    return f"{float(value):.1f} m" if isinstance(value, (int, float)) else "not known"


def _fmt_ratio(value: object) -> str:
    return f"{float(value):.2f}" if isinstance(value, (int, float)) else "not known"


def row_summaries(rows: list[HorizonRow]) -> list[dict[str, str]]:
    """Compact per-horizon summaries, for a table a judge scans top to bottom."""
    return [
        {
            "horizon": row.label,
            "level": row.risk_level,
            "score": str(row.risk_score),
            "rules": ", ".join(d["rule_id"] for d in row.drivers) or "-",
            "hard": ", ".join(row.hard_rules_triggered) or "-",
            "confidence": f"{row.confidence:.2f}",
        }
        for row in rows
    ]
