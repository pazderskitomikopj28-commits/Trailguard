"""TrailGuard Phase 6 competition demo -- presentation layer.

Nothing in this package computes a safety verdict, a projection, an ETA or a
margin of its own. It drives the same five atomic tools a model is given, over
the same frozen replay assets, and turns the pydantic results into flat,
display-ready view models. The rule that separates this from business logic is
enforced by tests, not by good intentions: if a number appears in a view model
it must be a number a TrailGuard tool returned.

The one place this package *reads* rather than *returns* is the rule pack: the
core reports a triggered rule's observed value and threshold as prose inside
``RuleDriver.detail`` (there is no structured field for either), so a caller that
wants to show "70 km/h observed against a 65 km/h threshold" has to take the
numbers the core already formatted. ``drivers_view`` does that and says in its
docstring that it is parsing the core's own output, not deciding anything.
"""
