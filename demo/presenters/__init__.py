"""Presentation layer for the TrailGuard competition demo.

Everything a judge sees is built here, and nothing a judge sees is computed here.
Each module in this package takes the pydantic results of the same five atomic
tools the benchmark drives, and flattens them into display-ready view models:

* :mod:`~demo.presenters.chain` -- the frozen horizon chain itself, replay or live
* :mod:`~demo.presenters.timeline` -- per-horizon levels and how they were assembled
* :mod:`~demo.presenters.route_view` -- the GPX polyline and the projected markers
* :mod:`~demo.presenters.weather_view` -- the forecast for the projected place/time
* :mod:`~demo.presenters.evidence_view` -- artifact hashes, the chain, and the gaps
* :mod:`~demo.presenters.agent_view` -- a real harness run and its honest failures
* :mod:`~demo.presenters.benchmark_view` -- Phase 5's committed numbers, read-only
* :mod:`~demo.presenters.scenario_view` -- the first screen
* :mod:`~demo.presenters.catalog` -- which scenarios are offered, and why

The import rule that keeps this a presentation layer rather than a second safety
core is simple and testable: **no module here may evaluate a rule, decide a level,
project a position, compute an ETA or normalise a number.** Where a number must be
presented in a different form -- a timestamp, a threshold annotation -- it is
copied from a value a tool already returned, and the docstring says which one.

One deliberate exception is documented rather than hidden: ``RuleDriver`` carries
no structured threshold or observed-value fields, so the numbers live in the
prose of the core's own ``detail`` string. Presenters copy that string; they never
parse it into a new value.
"""

from .benchmark_view import BenchmarkView, load_benchmark
from .catalog import DemoCatalog, DemoEntry, load_catalog
from .chain import HorizonRow, ReplayRun, WeatherMode, WeatherUnavailable, run_replay
from .evidence_view import EvidenceView, build_evidence
from .route_view import RouteProfile, route_profile
from .scenario_view import OverviewView, build_overview
from .timeline import Timeline, TimelinePoint, build_timeline
from .weather_view import WeatherRow, weather_rows, weather_summary

__all__ = [
    "BenchmarkView",
    "DemoCatalog",
    "DemoEntry",
    "EvidenceView",
    "HorizonRow",
    "OverviewView",
    "ReplayRun",
    "RouteProfile",
    "Timeline",
    "TimelinePoint",
    "WeatherMode",
    "WeatherRow",
    "WeatherUnavailable",
    "build_evidence",
    "build_overview",
    "build_timeline",
    "load_benchmark",
    "load_catalog",
    "route_profile",
    "run_replay",
    "weather_rows",
    "weather_summary",
]
