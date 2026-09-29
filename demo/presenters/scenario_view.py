"""The first screen: what TrailGuard is, in the four horizons it just computed.

Spec 12. The Overview tab has one job -- a judge who has never seen this system
should know within thirty seconds what it does, and a judge who has should be able
to check the claim. So the view model carries the run's own numbers and the
sentence that names where they came from, and it carries the headline scenario
cases as evidence rather than as prose.

Two things this module refuses to do. It does not summarise Phase 5 -- the
Benchmark tab does that, with both sides, and an Overview that reduced the MIXED
finding to a slogan would be the exact dishonesty the brief forbids. And it does
not rank, net or combine anything: the worst level shown here is the maximum over
horizons by the core's own ``RiskLevel`` rank, which is the same operation the
hazard-tracking metrics use, not a score.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .catalog import DemoCatalog, DemoEntry, load_catalog
from .chain import ReplayRun
from .timeline import SCORE_UNIT_NOTE, LEVEL_STYLE, Timeline, build_timeline


@dataclass(slots=True)
class ScenarioCard:
    """A headline scenario as a card on the first screen."""

    scenario_id: str
    title: str
    label: str
    worst_level: str
    badge: str
    colour: str
    teaches: str
    rules: tuple[str, ...]


@dataclass(slots=True)
class OverviewView:
    """The first screen, ready to render."""

    headline: str
    subhead: str
    what_it_does: list[str]
    what_it_is_not: list[str]
    scenario_id: str
    description: str
    simulation_note: str
    now_level: str
    now_badge: str
    worst_level: str
    worst_badge: str
    worst_label: str
    level_path: str
    story: str
    transitions: list[str]
    score_note: str
    weather_mode: str
    weather_snapshot_ref: str | None
    freshness_budget_min: float
    rule_pack: str
    provenance: list[str]
    featured: list[ScenarioCard]
    control: ScenarioCard
    count_of_scenarios: int
    journey: list[dict[str, str]] = field(default_factory=list)

    @property
    def worst_colour(self) -> str:
        return LEVEL_STYLE[self.worst_level]["colour"]

    @property
    def now_colour(self) -> str:
        return LEVEL_STYLE[self.now_level]["colour"]


WHAT_IT_DOES = [
    "Reads the route, the phone and the weather, and asks what the weather will be "
    "where the hiker will be at four horizons: now, +30, +60 and +120 minutes.",
    "Evaluates six frozen rules -- two hard, four soft -- and reports a Projected "
    "Risk State with the drivers, the safety margins and the confidence behind it.",
    "Shows its work: every number on screen is a value one of five atomic tools "
    "returned, over inputs identified by their sha256 hash.",
]

WHAT_IT_IS_NOT = [
    "Not an accident probability. The soft score is a count-and-weight bookkeeping "
    "sum; the framework gives it no probability of anything.",
    "Not a routing engine and not a personalised pace model: the ETA is a frozen "
    "calibrated formula, and it says so.",
    "Not a black box: a hard rule can only raise the verdict, never lower it, and "
    "the level is the maximum of the score band, the compound escalation and any "
    "triggered hard rule's floor.",
]

JOURNEY = [
    {
        "step": "1. Route context",
        "tool": "get_route_context",
        "what": "Where the hiker is on the planned polyline, and how much of the "
                "route is still ahead.",
    },
    {
        "step": "2. Device context",
        "tool": "get_user_device_context",
        "what": "Battery, network, fatigue and the movement window from the phone.",
    },
    {
        "step": "3. Projection, per horizon",
        "tool": "project_route_state",
        "what": "Where the hiker will be, and at what time, for each of the four "
                "horizons.",
    },
    {
        "step": "4. Weather, per horizon",
        "tool": "get_weather_for_route_time",
        "what": "The forecast for the projected position at the projected arrival "
                "time -- not the current-location weather.",
    },
    {
        "step": "5. Verdict, per horizon",
        "tool": "assess_risk",
        "what": "The level, the drivers that fired, the safety margins and the "
                "confidence, from the frozen rule pack.",
    },
]


def _card(entry: DemoEntry) -> ScenarioCard:
    style = LEVEL_STYLE[entry.worst_level]
    return ScenarioCard(
        scenario_id=entry.scenario_id,
        title=entry.title,
        label=entry.label,
        worst_level=entry.worst_level,
        badge=style["badge"],
        colour=style["colour"],
        teaches=entry.teaches,
        rules=entry.rules,
    )


def build_overview(
    run: ReplayRun, timeline: Timeline | None = None, *, catalog: DemoCatalog | None = None
) -> OverviewView:
    """Assemble the first screen for ``run``.

    ``timeline`` and ``catalog`` are accepted so a caller that already built them
    does not pay for a second pass; both default to the frozen sources.
    """
    resolved_timeline = timeline if timeline is not None else build_timeline(run)
    resolved_catalog = catalog if catalog is not None else load_catalog()
    worst_style = LEVEL_STYLE[resolved_timeline.worst]
    now_style = LEVEL_STYLE[resolved_timeline.now]

    if resolved_timeline.worst == resolved_timeline.now:
        headline = (
            f"{resolved_timeline.worst} across all four horizons -- nothing escalates "
            "and nothing clears."
        )
    elif resolved_timeline.worst not in ("GREEN",) and resolved_timeline.now == "GREEN":
        headline = (
            f"Safe now, {resolved_timeline.worst} by {resolved_timeline.worst_label}. "
            "This is the case a current-location forecast would miss."
        )
    else:
        headline = (
            f"{resolved_timeline.now} now, {resolved_timeline.worst} at "
            f"{resolved_timeline.worst_label}."
        )

    return OverviewView(
        headline=headline,
        subhead=(
            "A predictive safety skill for hikers: it does not report the weather "
            "outside, it reports where the hiker will be when the risk arrives."
        ),
        what_it_does=list(WHAT_IT_DOES),
        what_it_is_not=list(WHAT_IT_IS_NOT),
        scenario_id=run.scenario_id,
        description=run.description,
        simulation_note=run.simulation_note,
        now_level=resolved_timeline.now,
        now_badge=now_style["badge"],
        worst_level=resolved_timeline.worst,
        worst_badge=worst_style["badge"],
        worst_label=resolved_timeline.worst_label,
        level_path="/".join(point.level[:1] for point in resolved_timeline.points),
        story=resolved_timeline.story,
        transitions=list(resolved_timeline.transitions),
        score_note=SCORE_UNIT_NOTE,
        weather_mode=run.weather_mode,
        weather_snapshot_ref=run.weather_snapshot_ref,
        freshness_budget_min=run.freshness_budget_min,
        rule_pack=f"{run.rule_pack_id}@{run.rule_pack_version}",
        provenance=list(run.provenance),
        featured=[_card(entry) for entry in resolved_catalog.featured],
        control=_card(resolved_catalog.control),
        count_of_scenarios=resolved_catalog.count(),
        journey=list(JOURNEY),
    )
