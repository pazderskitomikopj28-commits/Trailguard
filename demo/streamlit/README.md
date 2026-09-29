# The demo UI

```
.venv/bin/streamlit run demo/streamlit/app.py
```

or, equivalently, `scripts/start_demo.sh`, which starts it on the port the machine
has free and prints the local address. The demo runs from any working directory:
`app.py` resolves the repository from its own location and puts it on `sys.path`
before anything else is imported.

## What is on screen, and in what order

Five top-level tabs, in the order a judge asks the questions. The ceiling is five
on purpose — a demo with twelve tabs is a demo where the judge has to go looking
for the value.

| Tab | The question it answers |
|---|---|
| **Overview** | What is this, and what is the answer for this scenario? |
| **Risk Timeline** | Where does that answer come from, and what happens next? |
| **Evidence** | Why should I believe it? |
| **Benchmark** | What was measured, including what went the wrong way? |
| **Architecture** | How is it built, and what was frozen to build it? |

Agent Mode is deliberately not a sixth tab. It is a switch inside Risk Timeline
between *who reads the five tools* — the deterministic core, or a real local Qwen
driving them over the harness protocol — because that contrast is the answer to
the question that tab is about. It is also the only part of the demo that can
fail, and it sits one click below the core value for exactly that reason.

## Before a judge arrives

```
.venv/bin/python scripts/demo_preflight.py
```

It reports three groups: **ready**, **degraded** (works, but a feature is
unavailable and the UI says so) and **missing** (the demo cannot start), and names
the one command that fixes each row that is not ready. On a competition laptop the
expected answer is "most of it works, and here is exactly which part does not".

```
.venv/bin/python scripts/demo_smoke.py
```

The acceptance gate. It replays all 14 scenarios against the committed Phase 5
references field for field, checks the level explainer against the engine for all
56 horizon rows, verifies every evidence hash, and reads the benchmark tab's
sources. Run it after any change to the presentation layer; it says PASS or it
says what drifted.

## Three things the UI will not do

**It will not fall back silently.** Replay Mode is the default weather mode and is
labelled as replay on every screen that uses it. Switching to live weather is an
explicit action in the sidebar; if live weather fails, the demo reports the failure
and waits. It never answers a live request from the replay snapshot, because a
quietly relabelled number is the most dishonest thing this screen could show.

**It will not improvise an answer for the agent.** When the local model endpoint
does not answer, the Agent panel says "Local Agent unavailable", names the reason,
and stops. There is no scripted provider behind it: the value of Agent Mode is that
a real model is really calling the tools, and a stand-in would turn the strongest
part of the presentation into the weakest.

**It will not render a traceback, an address or a path.** Every failure is a
sentence the viewer can act on, produced by the module that raised it. No screen
in this demo shows an exception object, a host address, an absolute path or a
credential. The agent's own answer is the only untrusted string on screen and is
shown inside a code fence it cannot close.

## Layout of this directory

```
app.py               the shell: page, sidebar, tabs, and the failure routing
components/
  charts.py          offline SVG. No chart library, no tile server, no network.
  panels.py          badges, verdicts, tables, and the honesty box
  tabs.py            the five tabs
  text.py            escaping and the fence around untrusted text
```

`demo/presenters/` is a separate package on purpose and imports nothing from here.
It needs only `pydantic` and `yaml`, so the replay chain, the timeline, the
evidence view and the benchmark numbers are all runnable — and testable — on a
machine where `streamlit` is not installed at all.
