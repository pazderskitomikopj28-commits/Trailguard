"""Timeline view models: how the reported level was assembled, per horizon.

Spec 18. The judge's question is not only "what is the risk" but "how did you
get that number". The core answers it in three steps -- the band the soft score
lands in, the floor any triggered hard rule sets, and the compound escalation of
R-F -- and takes the maximum, because a hard rule can only ever raise a level
(spec 10.5). This module states those three steps in the words the frozen rule
pack uses, and never as its own arithmetic.

The distinction matters enough to be tested rather than asserted here. Everything
shown is read from :func:`~trailguard.risk_core.scoring.score_band` and the
calibration's own ``hard_rule_severity_floor`` table, and a test checks the
identity ``max(band, floor, escalated) == the level the engine reported`` for
every horizon of every scenario in the dataset. If the engine ever changed how
it assembles a level, the demo would say something the engine did not -- and the
test would fail before a judge could see it.

The soft score is shown as a count-and-weight bookkeeping figure, not a
probability. Nothing in this package ever renders it as a percentage of harm,
and the labels below are written to make that hard to do by accident.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from trailguard.risk_core import load_calibration, load_rule_pack
from trailguard.risk_core.scoring import score_band
from trailguard.schemas.risk import RuleDriver, RiskLevel

from .chain import HorizonRow, ReplayRun

#: Display decoration. Colour and label only -- never a threshold or a meaning.
LEVEL_STYLE: dict[str, dict[str, str]] = {
    "GREEN": {"badge": "SAFE", "colour": "#1b7f3b"},
    "YELLOW": {"badge": "WATCH", "colour": "#b8860b"},
    "ORANGE": {"badge": "HAZARD", "colour": "#c25e00"},
    "RED": {"badge": "CRITICAL", "colour": "#b00020"},
}

#: Documented in ``scoring.py``: a soft score is a count-and-weight sum, not a
#: probability of anything. Surfaced so the UI label can be checked against it.
SCORE_UNIT_NOTE = (
    "soft score = a count-and-weight bookkeeping sum (severity weights, capped at 100). "
    "It is not a probability of harm."
)


@dataclass(slots=True)
class AssemblyStep:
    """One of the three inputs to the reported level."""

    source: str  # e.g. "soft-score band", "hard-rule floor (R-B)", "compound escalation (R-F)"
    outcome: str  # the RiskLevel this step produces, or "no effect"
    detail: str


@dataclass(slots=True)
class TimelinePoint:
    """One horizon as a judge reads it."""

    label: str
    horizon_min: int
    level: str
    badge: str
    colour: str
    rank: int
    score: int
    score_band: str
    hazard: bool
    hard_rules: list[str]
    driver_ids: list[str]
    headline: str
    #: The three steps, most decisive first, then the maximum the engine took.
    assembly: list[AssemblyStep]
    escalated_above_score: bool
    estimated_minutes_to_finish: float
    minutes_to_sunset: float
    time_margin_min: float | None
    evidence_refs: list[str]


@dataclass(slots=True)
class Timeline:
    """The whole run as a sequence a judge can read left to right."""

    points: list[TimelinePoint]
    worst: str
    now: str
    worst_label: str
    story: str
    score_note: str = SCORE_UNIT_NOTE
    transitions: list[str] = field(default_factory=list)


def _headline(row: HorizonRow) -> str:
    """The one-line story for a horizon, in the core's own vocabulary.

    The driver's ``detail`` is quoted, not rewritten: it already contains the
    observed value, the operator, the threshold, the units and the band. The only
    thing taken away is the tail the core itself appends after ``" | "`` -- the
    context features and any not-evaluable disclosure -- because a headline that
    ends in a projected coordinate is not a headline. The full string is shown
    verbatim in the drivers table, so nothing is lost.
    """
    if not row.drivers and not row.hard_rules_triggered:
        return "No rule triggered at this horizon."
    lead = row.drivers[0] if row.drivers else None
    if lead is not None and lead["detail"]:
        return lead["detail"].split(" | ", 1)[0]
    return f"Hard rule {row.hard_rules_triggered[0]} triggered."


def _assembly(row: HorizonRow, pack, calibration) -> list[AssemblyStep]:
    """The three steps, then the maximum, read from the pack rather than restated.

    ``calibration`` and ``pack`` are passed in so every horizon of a run explains
    itself against the very engine that produced it, and a test can supply the
    same pair the engine was bound to.
    """
    floors = calibration.severity_floor
    rules = {rid: pack.get(rid) for rid in pack.ids()}
    band = score_band(row.risk_score)
    steps = [
        AssemblyStep(
            source=f"soft-score band (score {row.risk_score})",
            outcome=band,
            detail=(
                f"{row.risk_score} soft-score points lands in the {band} band "
                f"{calibration.soft_score_bands[band]}, out of a saturation of "
                f"{calibration.saturation_score}."
            ),
        )
    ]

    triggered = row.hard_rules_triggered
    if triggered:
        for rid in triggered:
            # ``severity`` is a Severity enum; ``severity_floor`` is keyed by the
            # plain vocabulary string the calibration file uses.
            severity = rules[rid].severity.value
            floor = floors[severity].value
            steps.append(
                AssemblyStep(
                    source=f"hard-rule floor ({rid}, {severity})",
                    outcome=floor,
                    detail=(
                        f"{rid} is a hard rule with {severity} severity, so its floor is "
                        f"{floor} regardless of band."
                    ),
                )
            )
    else:
        steps.append(
            AssemblyStep(
                source="hard-rule floor",
                outcome="no effect",
                detail="No hard rule triggered at this horizon.",
            )
        )

    # Which rule is the compound one is structural, not a name: it is the rule
    # whose thresholds carry ``escalation_levels``. Naming "R-F" here would make
    # the explainer silently wrong the day the pack is renumbered.
    compound_ids = [
        rid for rid, rule in rules.items() if "escalation_levels" in rule.thresholds
    ]
    compound = [d for d in row.drivers if d["rule_id"] in compound_ids]
    if compound:
        rid = compound[0]["rule_id"]
        operand = rules[rid].thresholds["escalation_levels"]
        min_rules = rules[rid].thresholds["min_independent_soft_rules"]
        steps.append(
            AssemblyStep(
                source=f"compound escalation ({rid})",
                outcome=row.risk_level,
                detail=(
                    f"{rid} fired on {min_rules} or more independent soft rules and "
                    f"moves the level up by {operand}; it contributes no score points "
                    "of its own."
                ),
            )
        )
    else:
        steps.append(
            AssemblyStep(
                source=f"compound escalation ({compound_ids[0] if compound_ids else 'R-F'})",
                outcome="no effect",
                detail="No compound rule fired at this horizon.",
            )
        )
    return steps


def build_timeline(run: ReplayRun, *, pack=None, calibration=None) -> Timeline:
    """Turn a :class:`ReplayRun` into the sequence the Risk Timeline tab renders.

    ``pack`` and ``calibration`` default to the frozen files the engine was bound
    to; a test supplies the same pair to prove the explainer agrees with them.
    """
    resolved_pack = pack if pack is not None else load_rule_pack()
    resolved_calibration = calibration if calibration is not None else load_calibration()

    points: list[TimelinePoint] = []
    transitions: list[str] = []
    previous: str | None = None
    for row in run.rows:
        level = row.risk_level
        style = LEVEL_STYLE[level]
        band = score_band(row.risk_score)
        assembled = _assembly(row, resolved_pack, resolved_calibration)
        hazard = bool(row.hard_rules_triggered or row.drivers)

        if previous is not None and previous != level:
            direction = "worsened" if RiskLevel(level).rank > RiskLevel(previous).rank else "eased"
            transitions.append(f"{previous} to {level} at {row.label} ({direction}).")
        previous = level

        points.append(
            TimelinePoint(
                label=row.label,
                horizon_min=row.horizon_min,
                level=level,
                badge=style["badge"],
                colour=style["colour"],
                rank=RiskLevel(level).rank,
                score=row.risk_score,
                score_band=band,
                hazard=hazard,
                hard_rules=list(row.hard_rules_triggered),
                driver_ids=[d["rule_id"] for d in row.drivers],
                headline=_headline(row),
                assembly=assembled,
                escalated_above_score=RiskLevel(level).rank > RiskLevel(band).rank,
                estimated_minutes_to_finish=row.estimated_minutes_to_finish,
                minutes_to_sunset=row.minutes_to_sunset,
                time_margin_min=row.safety_margins.get("time_margin_min"),
                evidence_refs=list(row.evidence_refs),
            )
        )

    return Timeline(
        points=points,
        worst=run.worst_level,
        now=run.now_level,
        worst_label=max(points, key=lambda p: p.rank).label,
        story=build_story(run, points),
        transitions=transitions,
    )


def build_story(run: ReplayRun, points: list[TimelinePoint]) -> str:
    """The one sentence that explains this run without hiding anything.

    Both halves of an honest finding are always included: what happened, and
    what it cost -- the baseline and the skill condition where a pair was run.
    This function does not know about conditions, so it states only the replay
    fact; the Agent tab supplies the cost side and the Overview tab refuses to
    summarise Phase 5 without it.
    """
    worst = max(points, key=lambda p: p.rank)
    hazards = [p for p in points if p.hazard]
    if not hazards:
        return f"No rule triggers at any of the four horizons; the worst level is {worst.level}."
    first = hazards[0]
    if len(hazards) == 1:
        return (
            f"{first.label} is the only horizon where a rule triggers, reaching {first.level}; "
            f"the other three are {points[0].level if first is not points[0] else 'safe'}."
        )
    if len(hazards) == len(points):
        return (
            f"Every one of the four horizons triggers a rule, worst at {worst.label} ({worst.level}); "
            f"the run starts at {points[0].level} and ends at {points[-1].level}."
        )
    return (
        f"{len(hazards)} of the four horizons trigger a rule, worst at {worst.label} ({worst.level}); "
        f"the run starts at {points[0].level} and ends at {points[-1].level}."
    )


def driver_rows(drivers: list[dict]) -> list[dict[str, str]]:
    """Flatten a horizon's drivers for a table, quoting the core's own detail.

    ``RuleDriver`` carries no structured threshold or observed-value fields, so
    the numbers live in the ``detail`` prose the core wrote. This copies that
    string across unchanged -- parsing it would be the presentation layer
    inventing a structured API the core does not have.
    """
    rows = []
    for driver in drivers:
        rows.append(
            {
                "rule": driver["rule_id"],
                "kind": driver["type"],
                "severity": driver["severity"],
                "detail": driver["detail"] or "",
            }
        )
    return rows


def driver_objects(drivers: list[RuleDriver]) -> list[RuleDriver]:
    """Identity helper for callers holding pydantic drivers rather than dicts."""
    return list(drivers)
