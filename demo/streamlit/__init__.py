"""The Streamlit layer.

Deliberately the only place in the project that imports a UI framework, and
deliberately kept out of ``demo/presenters``. This directory is what a judge
looks at; ``demo/presenters`` is what a judge is shown. Keeping the two apart
means the presentation models stay importable, testable and runnable on a laptop
with nothing but ``pydantic`` and ``yaml`` installed -- which is the state the
brief requires the demo to survive.
"""
