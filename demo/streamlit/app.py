"""The TrailGuard competition demo.

    .venv/bin/streamlit run demo/streamlit/app.py

or, equivalently, ``scripts/start_demo.sh``.

The shell does four things and delegates everything else:

* it resolves the repository from its own location, so the demo runs from any
  working directory;
* it puts the weather mode and the scenario in the sidebar, because a judge should
  be able to see *and challenge* which scenario is on screen and whether the
  weather is real. Replay is the default and is labelled as replay everywhere it
  is used;
* it keeps the agent switch one click below the demo's core value, because the
  core value must not depend on a model being up;
* it never lets an exception reach the screen. Every failure is rendered by the
  module that raised it as a sentence, and this file's job in a failure is only to
  decide which box to put it in.

There is no bare ``except`` here. Each one names its own reason, and anything this
file does not expect propagates to the streamlit console -- which the operator
sees and a judge never does. ``st.exception`` is never called.
"""

from __future__ import annotations

import hashlib
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import streamlit as st  # noqa: E402 - the UI layer, and the only file that imports one

from demo.presenters.agent_view import (  # noqa: E402
    MAX_STEPS,
    MAX_TOKENS,
    TEMPERATURE,
    AgentAvailability,
    LocalAgentUnavailable,
    probe_local_agent,
    run_local_agent,
)
from demo.presenters.benchmark_view import available as benchmark_available  # noqa: E402
from demo.presenters.benchmark_view import load_benchmark  # noqa: E402
from demo.presenters.catalog import DemoCatalog, DemoEntry, load_catalog, reference_for  # noqa: E402
from demo.presenters.chain import (  # noqa: E402
    WEATHER_UNAVAILABLE_PREFIX,
    WeatherUnavailable,
    run_replay,
)
from demo.presenters.evidence_view import build_evidence  # noqa: E402
from demo.presenters.route_view import route_profile  # noqa: E402
from demo.presenters.scenario_view import build_overview  # noqa: E402
from demo.presenters.timeline import build_timeline  # noqa: E402
from demo.streamlit.components import panels, tabs  # noqa: E402
from trailguard.agent.replay_runner import load_scenario  # noqa: E402

#: The five top-level tabs and the one question each answers. Five is the ceiling
#: the brief sets, and the ceiling is the point: a demo with twelve tabs is a demo
#: where the judge has to go looking for the value.
TABS = (
    ("Overview", "What is this, and what is the answer?"),
    ("Risk Timeline", "Where does that answer come from, and what happens next?"),
    ("Evidence", "Why should I believe it?"),
    ("Benchmark", "What was measured, including what went the wrong way?"),
    ("Architecture", "How is it built, and what was frozen to build it?"),
)

REPLAY_LABEL = "Replay — labelled default, frozen snapshot"
LIVE_LABEL = "Live weather — needs the network"

SHOW_ALL = "show all 14 scenarios"

FONT = (
    "html,body,[class*='css']{font-family:system-ui,-apple-system,'Segoe UI',Roboto,"
    "'Helvetica Neue',sans-serif}"
    ".block-container{padding-top:2.2rem;padding-bottom:3rem;max-width:1180px}"
    "#MainMenu{visibility:hidden}footer{visibility:hidden}"
    "h1,h2,h3{letter-spacing:-.01em}"
)

SAMPLING = {"temperature": TEMPERATURE, "max_tokens": MAX_TOKENS, "max_steps": MAX_STEPS}


# --------------------------------------------------------------------------- #
# Building the run
# --------------------------------------------------------------------------- #


@st.cache_data(show_spinner="Loading the scenario and running the five tools...")
def _replay(scenario_id: str, scenario_path: str) -> object:
    """A replay run, cached across tab switches and reruns.

    Only ever called with ``weather_mode="replay"``. The live path is deliberately
    *not* cached: a live failure should be reported as the failure it is right now,
    not served from a cache entry made when the network was down, and a live success
    should not be served from one made when the forecast was older.
    """
    return run_replay(load_scenario(Path(scenario_path)), weather_mode="replay")


def _live(scenario_path: str) -> tuple[object | None, str | None]:
    """A live run, uncached, with the failure named and returned rather than raised."""
    try:
        return run_replay(load_scenario(Path(scenario_path)), weather_mode="live"), None
    except WeatherUnavailable as exc:
        return None, str(exc)
    except Exception as exc:  # noqa: BLE001 - named, then re-worded for the screen
        return None, f"{type(exc).__name__}: {exc}"


def _run_agent(scenario_path: str) -> tuple[object | None, str | None]:
    """Drive the real harness once. Returns a view or a sentence, never raises."""
    try:
        return run_local_agent(load_scenario(Path(scenario_path))), None
    except LocalAgentUnavailable as exc:
        return None, str(exc)
    except Exception as exc:  # noqa: BLE001 - a model failure is a sentence
        return None, f"{type(exc).__name__}: {exc}"


@st.cache_data(show_spinner="Asking the local model endpoint what it is serving...")
def _probe() -> AgentAvailability:
    try:
        return probe_local_agent()
    except Exception as exc:  # noqa: BLE001 - a probe must never break a screen
        return AgentAvailability(
            available=False,
            reason="the probe itself failed",
            detail=f"{type(exc).__name__}: {exc}",
            model="unknown",
            base_url="unavailable",
        )


@st.cache_data(show_spinner=False)
def _benchmark() -> object | None:
    return load_benchmark() if benchmark_available() else None


def _skill_sha256() -> str:
    try:
        return hashlib.sha256((REPO_ROOT / "SKILL.md").read_bytes()).hexdigest()
    except OSError:
        return "not readable"


# --------------------------------------------------------------------------- #
# Sidebar
# --------------------------------------------------------------------------- #


def _all_entries(catalog: DemoCatalog) -> list[DemoEntry]:
    return list(catalog.featured) + [catalog.control] + list(catalog.advanced)


def _sidebar(catalog: DemoCatalog) -> tuple[DemoEntry, str]:
    st.sidebar.markdown("## TrailGuard")
    st.sidebar.caption(
        "Predictive outdoor safety. A deterministic safety core, five atomic tools, "
        "and an optional local model that drives them."
    )

    st.sidebar.markdown("### Weather mode")
    mode = st.sidebar.radio(
        "weather mode",
        (REPLAY_LABEL, LIVE_LABEL),
        index=0,
        help=(
            "Replay is the default and is labelled as replay on every screen that uses "
            "it: the weather came from a frozen snapshot committed with Phase 5, and "
            "its age is disclosed. Live weather asks a real forecast service and needs "
            "the network. The demo never switches modes on its own, and a live failure "
            "is never answered from the snapshot."
        ),
    )

    st.sidebar.markdown("### Scenario")
    featured = list(catalog.featured)
    control = catalog.control
    options = [f"{entry.title} ({entry.worst_level})" for entry in featured]
    options += [f"control: {control.title} ({control.worst_level})", SHOW_ALL]
    labels = {
        f"{entry.title} ({entry.worst_level})": entry for entry in featured
    }
    labels[f"control: {control.title} ({control.worst_level})"] = control

    picked = st.sidebar.radio("scenario", options, index=0)
    if picked == SHOW_ALL:
        everything = _all_entries(catalog)
        chosen = st.sidebar.selectbox(
            "all 14 scenarios",
            [f"{e.scenario_id} ({e.worst_level})" for e in everything],
            index=0,
        )
        entry = next(e for e in everything if chosen.startswith(e.scenario_id))
    else:
        entry = labels[picked]

    with st.sidebar.expander("What each headline demo teaches"):
        for candidate in featured + [control]:
            st.markdown(
                f"**{candidate.title}** ({candidate.worst_level}) -- {candidate.teaches}"
            )

    st.sidebar.markdown("### Before a judge arrives")
    st.sidebar.code("python scripts/demo_preflight.py", language="bash")
    st.sidebar.caption(
        "Says which features will work on this laptop, and names the one command "
        "that fixes each one that will not."
    )
    return entry, ("live" if mode == LIVE_LABEL else "replay")


# --------------------------------------------------------------------------- #
# The page
# --------------------------------------------------------------------------- #


def main() -> None:
    st.set_page_config(page_title="TrailGuard — predictive outdoor safety", layout="wide")
    st.markdown(f"<style>{FONT}</style>", unsafe_allow_html=True)

    try:
        catalog = load_catalog()
    except Exception as exc:  # noqa: BLE001 - without the catalogue there is no demo
        panels.unavailable(
            "The scenario catalogue could not be loaded",
            f"{type(exc).__name__}: {exc}",
            "The demo has nothing to show without the frozen scenarios and their "
            "Phase 5 references.",
            "python scripts/demo_preflight.py",
        )
        return

    entry, weather_mode = _sidebar(catalog)

    # An agent run belongs to the scenario it was made on, so changing the
    # scenario discards it rather than showing one model's answer for another
    # scenario's data.
    if st.session_state.get("agent_for") != entry.scenario_id:
        st.session_state["agent_for"] = entry.scenario_id
        st.session_state["agent_view"] = None
        st.session_state["agent_error"] = None

    # --- the run itself, before any tab can mean anything --------------------- #
    if weather_mode == "replay":
        try:
            run = _replay(entry.scenario_id, str(entry.scenario_path))
        except Exception as exc:  # noqa: BLE001
            panels.unavailable(
                "This scenario will not replay",
                f"{type(exc).__name__}: {exc}",
                "A replay run reads only committed files, so a failure here is a "
                "missing or unreadable input rather than a network problem.",
                "python scripts/demo_preflight.py",
            )
            return
    else:
        run, live_error = _live(str(entry.scenario_path))
        if run is None:
            # The body already opens with the phrase, so the title uses it and the
            # body carries on from it -- a box that says "Live weather
            # unavailable" and then repeats it in the first line reads like a bug.
            body = (live_error or "the forecast service did not answer").removeprefix(
                f"{WEATHER_UNAVAILABLE_PREFIX}: "
            )
            panels.unavailable(
                WEATHER_UNAVAILABLE_PREFIX,
                body,
                "The demo will not fall back to the replay snapshot silently, because a "
                "quietly relabelled 'live' number would be the single most dishonest "
                "thing this screen could show.",
                "start the demo again with Replay mode chosen",
            )
            if st.button("Switch back to Replay mode"):
                st.session_state["weather_override"] = REPLAY_LABEL
                st.rerun()
            return

    # --- the views ------------------------------------------------------------ #
    try:
        timeline = build_timeline(run)
        overview_view = build_overview(run, timeline, catalog=catalog)
        route = route_profile(run)
        reference = reference_for(entry.scenario_id)
        evidence_view = build_evidence(run, reference=reference)
    except Exception as exc:  # noqa: BLE001
        panels.unavailable(
            "The presentation layer could not build this run",
            f"{type(exc).__name__}: {exc}",
            "This would be a bug in Phase 6's display code and not in the safety core: "
            "the core produced the verdict, and the layer that turns it into a screen "
            "did not.",
            ".venv/bin/python scripts/demo_smoke.py",
        )
        return

    availability = _probe()
    agent_state = {
        "availability": availability,
        "view": st.session_state.get("agent_view"),
        "error": st.session_state.get("agent_error"),
        "runner": _agent_runner(entry, availability),
        "route": route,
        "sampling": SAMPLING,
    }

    st.markdown(
        f'<div style="font-size:13px;color:#6b7785;margin:-6px 0 10px">'
        f'scenario <code>{panels._esc(entry.scenario_id)}</code> &#183; '
        f'weather mode <strong>{panels._esc(weather_mode)}</strong> &#183; '
        f'{"replayed from the frozen snapshot, and labelled as replay" if weather_mode == "replay" else "fetched live, and labelled as live"}'
        f'</div>',
        unsafe_allow_html=True,
    )

    selected = st.tabs([name for name, _question in TABS])
    for (name, question), tab in zip(TABS, selected):
        with tab:
            if name != "Overview":
                st.caption(question)
            try:
                if name == "Overview":
                    tabs.overview(run, overview_view)
                elif name == "Risk Timeline":
                    tabs.risk_timeline(
                        run,
                        timeline,
                        rule_catalog=catalog.rule_catalog,
                        agent_state=agent_state,
                    )
                elif name == "Evidence":
                    tabs.evidence(evidence_view, timeline)
                elif name == "Benchmark":
                    tabs.benchmark(_benchmark(), available=benchmark_available())
                else:
                    tabs.architecture(
                        run,
                        rule_catalog=catalog.rule_catalog,
                        skill_sha256=_skill_sha256(),
                        streamlit_version=getattr(st, "__version__", None),
                        agent_available=availability.available,
                    )
            except Exception as exc:  # noqa: BLE001 - a tab fails, the demo does not
                panels.unavailable(
                    f"The {name} tab could not be drawn",
                    f"{type(exc).__name__}: {exc}",
                    "The other tabs are unaffected, and the verdict at the top of the "
                    "Overview tab still came from the safety core.",
                    ".venv/bin/python scripts/demo_smoke.py",
                )

    st.sidebar.markdown("---")
    st.sidebar.caption(
        "Replay Mode is the reliable demonstration base. Local Qwen is the Agent "
        "capability layer. No model failure affects the rest of the demo."
    )


def _agent_runner(entry: DemoEntry, availability: AgentAvailability):
    """The callable the Agent panel presses.

    Returns ``(view, error)`` and records both in the session, so a panel rerun
    after a failed attempt shows the failure rather than a stale success.
    """

    def run() -> tuple[object | None, str | None]:
        if not availability.available:
            # The screen reason, not the operator's detail: the detail quotes the
            # endpoint that was asked, and a judge's screen does not need an
            # address to understand that the local model is not up.
            error = f"Local Agent unavailable: {availability.demo_screen_reason}"
            st.session_state["agent_view"], st.session_state["agent_error"] = None, error
            return None, error
        view, error = _run_agent(str(entry.scenario_path))
        st.session_state["agent_view"], st.session_state["agent_error"] = view, error
        return view, error

    return run


if __name__ == "__main__":
    main()
