"""The demo's scenario catalogue: three headline cases, one control, the rest.

Spec 14. A judge gets a few minutes, so the headline cases are chosen for what
they *prove* rather than for how they look. The three featured scenarios are the
only three ways a TrailGuard level is produced, each isolated from the other two:

* ``B05`` -- the level comes from the soft-score band alone. Three of four
  horizons are GREEN and the gust front only reaches the hiker at +120 minutes.
* ``B04`` -- the level comes from a hard-rule floor alone. The soft score is
  **zero** and the verdict is RED, because R-B is a CRITICAL hard rule. This is
  the case that shows the score is not the verdict.
* ``B10`` -- the level comes from compound escalation alone. The score band is
  ORANGE, no hard rule fires, and R-F raises it to RED.

S01 is the control: a clean day where nothing fires, so a judge can see the
system does not cry wolf. The other ten are one case per rule family and per
edge, available but not on the main path.

Anything derived (level, rule ids, whether the hazard clears) is read from the
frozen Phase 5 reference at load time rather than restated here, so a catalogue
label cannot drift away from what the benchmark actually measured. The editorial
column -- what the case proves -- is the only thing written by hand.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from trailguard.risk_core import load_rule_pack

REPO_ROOT = Path(__file__).resolve().parents[2]
REFERENCES_DIR = REPO_ROOT / "evals" / "phase5" / "references"
SCENARIOS_ROOT = REPO_ROOT / "scenarios"

HORIZON_ORDER = ("NOW", "+30min", "+60min", "+120min")


@dataclass(frozen=True, slots=True)
class DemoEntry:
    """One selectable scenario, with everything the picker needs."""

    scenario_id: str
    title: str
    label: str  # G/G/G/Y style, from the reference
    worst_level: str
    rules: tuple[str, ...]
    hard_rules: tuple[str, ...]
    teaches: str  # the editorial line
    clears: bool
    scenario_path: Path

    @property
    def level_path(self) -> str:
        return self.label

    @property
    def headline_rules(self) -> str:
        return ", ".join(self.rules) if self.rules else "none"


@dataclass(frozen=True, slots=True)
class DemoCatalog:
    featured: tuple[DemoEntry, ...]
    control: DemoEntry
    advanced: tuple[DemoEntry, ...]
    rule_catalog: dict[str, dict[str, str]]

    def __iter__(self):
        yield from self.featured
        yield self.control
        yield from self.advanced

    def find(self, scenario_id: str) -> DemoEntry:
        for entry in self:
            if entry.scenario_id == scenario_id:
                return entry
        raise KeyError(scenario_id)

    def count(self) -> int:
        return len(self.featured) + 1 + len(self.advanced)


#: Why each case is on the list. Keyed by scenario id; a scenario absent from
#: here is not offered in the catalogue at all, which is how the list stays short
#: enough for a judge to finish.
_TEACHES = {
    "B05_late_gust_hazard": (
        "A calm morning is not a safe day. The gust front reaches the hiker only "
        "at +120 minutes, so NOW, +30 and +60 are all GREEN -- the future-weather "
        "rule is the whole point of the horizon loop."
    ),
    "B04_tight_sunset_margin": (
        "Soft score 0, verdict RED. R-B is a CRITICAL hard rule, and a hard rule "
        "sets a floor the score cannot argue with."
    ),
    "B10_compound_soft_pair": (
        "Two modest signals -- a 15% pace drop and 11.5% projected battery -- are "
        "each worth a warning on their own. Together R-F escalates them a level, "
        "and RED is reached by combination rather than by any one number."
    ),
    "S01_clear_day": (
        "The control. Nothing fires at any horizon: the same four tool calls, the "
        "same engine, no verdict. Also the benchmark's warm-up scenario."
    ),
    "B01_mid_route_all_clear": "A second quiet day, mid-route.",
    "B02_gps_drift_warn": "Route deviation: off-track by more than the pack allows.",
    "B03_route_deviation_under_threshold": (
        "A slight detour that stays under R-A's 50 m warn threshold, so nothing "
        "fires. The demo does not raise a flag for ordinary wandering."
    ),
    "B06_future_gust_front": "A gust front that arrives early and stays.",
    "B07_pace_drop_warn": "Pace degradation alone: the pace-drop soft rule.",
    "B08_gust_band_clearing": (
        "The hazard that clears: the gust band passes, so +60 and +120 come back "
        "to GREEN. A risk timeline is not monotonic."
    ),
    "B09_battery_shortfall": "Battery against the distance still to walk: R-E.",
    "B11_multi_driver_storm": (
        "Every soft rule at once, including a saturated score -- the compound "
        "escalation from an already-RED band, clamped at RED."
    ),
    "S04_sunset_margin_pace_drop": "R-B, R-D, R-E and R-F together: the full storm.",
    "S11_stale_gps_missing_data": (
        "Stale GPS and a missing fix. The input is degraded, and the core says so "
        "rather than pretending to a confident number."
    ),
}


def _label(reference: dict) -> str:
    return "/".join(
        reference["horizons"][h]["risk"]["level"][:1] for h in HORIZON_ORDER
    )


def reference_for(scenario_id: str) -> dict | None:
    """The committed Phase 5 reference for a scenario, or ``None``.

    The single door for a reference, so the app, the evidence builder and the
    tests cannot each open the file their own way. Read-only, and never cached as
    anything but the bytes on disk: the whole point of the Evidence tab is that the
    hash on screen is a hash of the file as it is now.
    """
    path = REFERENCES_DIR / f"{scenario_id}.json"
    if not path.is_file():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _entry(scenario_id: str) -> DemoEntry | None:
    reference_path = REFERENCES_DIR / f"{scenario_id}.json"
    if not reference_path.is_file():
        return None
    reference = json.loads(reference_path.read_text(encoding="utf-8"))

    rules: list[str] = []
    hard: list[str] = []
    for horizon in HORIZON_ORDER:
        for driver in reference["horizons"][horizon]["risk"]["drivers"]:
            if driver["rule_id"] not in rules:
                rules.append(driver["rule_id"])
        for rid in reference["horizons"][horizon]["risk"]["hard_rules_triggered"]:
            if rid not in hard:
                hard.append(rid)

    teaches = _TEACHES.get(scenario_id)
    if teaches is None:
        return None

    return DemoEntry(
        scenario_id=scenario_id,
        title=scenario_id.split("_", 1)[1].replace("_", " "),
        label=_label(reference),
        worst_level=reference["expectations_for_evaluator"]["worst_level"],
        rules=tuple(rules),
        hard_rules=tuple(hard),
        teaches=teaches,
        clears=bool(reference["coverage"]["hazard_clears_again"]),
        scenario_path=REPO_ROOT / reference["scenario_file"],
    )


@lru_cache(maxsize=1)
def load_catalog() -> DemoCatalog:
    """Build the catalogue from the frozen references.

    Cached because every call re-reads five reference files otherwise, and a
    cached catalogue is also a stable object a Streamlit session can hold.
    """
    featured_ids = ("B05_late_gust_hazard", "B04_tight_sunset_margin", "B10_compound_soft_pair")
    control_id = "S01_clear_day"

    featured = tuple(e for e in (_entry(sid) for sid in featured_ids) if e)
    control = _entry(control_id)
    if control is None:
        raise FileNotFoundError(f"control scenario {control_id!r} has no frozen reference")

    chosen = set(featured_ids) | {control_id}
    advanced = tuple(
        e for e in (_entry(sid) for sid in sorted(_TEACHES)) if e and e.scenario_id not in chosen
    )

    pack = load_rule_pack()
    rule_catalog = {
        rid: {
            "name": rule.name,
            "condition": rule.condition,
            "severity": rule.severity.value,
            "kind": rule.trigger_type,
            "thresholds": ", ".join(
                f"{key}={value} {rule.units.get(key, '')}".strip()
                for key, value in rule.thresholds.items()
            ),
            "source_type": rule.source_type,
        }
        for rid, rule in ((rid, pack.get(rid)) for rid in pack.ids())
    }

    return DemoCatalog(
        featured=featured,
        control=control,
        advanced=advanced,
        rule_catalog=rule_catalog,
    )
