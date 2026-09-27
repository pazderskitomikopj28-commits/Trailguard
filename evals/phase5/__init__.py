"""Phase 5 benchmark package: the fair-skill benchmark (spec sections 11, 12, 3.1, 25).

The question this package exists to answer is a single-variable one: with the same
model, the same task, the same context, the same tool registry, the same replay
data, the same sampling parameters and the same agent harness, does loading
``SKILL.md`` change what an agent observably *does* -- which horizons it projects,
which tools it calls, whether it reports the deterministic core's verdict or its
own?

Three parts, in the order they must exist:

``references.py``
    The deterministic answer key. Calls the five atomic tools once per horizon for
    each pre-registered scenario and writes the result to ``references/``. No
    language model participates, at any point.

``metrics.py``
    The frozen metric definitions M1..M18. Scored from a run's evidence trace
    against a reference -- never by asking a model what it thinks of a run.

``evaluate.py`` / ``runner.py``
    The evaluator and the paired runner. Freeze this before the first scored run;
    nothing here may be edited once results exist, because a batch of results
    scored by a moving rulebook is not a measurement.
"""

from .references import BENCHMARK_SCENARIOS, BENCHMARK_VERSION, build_reference, load_scenarios

__all__ = ["BENCHMARK_SCENARIOS", "BENCHMARK_VERSION", "build_reference", "load_scenarios"]
