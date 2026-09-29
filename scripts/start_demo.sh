#!/usr/bin/env bash
#
# Start the TrailGuard competition demo.
#
#   scripts/start_demo.sh                 # preflight, then start on a free port
#   scripts/start_demo.sh --no-preflight  # just start
#   DEMO_PORT=8610 scripts/start_demo.sh
#
# What this is careful about, and why:
#
#   * It binds to localhost, never to 0.0.0.0, so the demo is not reachable from
#     outside the laptop by accident. A competition demo is a presentation, not a
#     service.
#   * It prefers the repository's own virtualenv, because a demo started with a
#     different interpreter is a demo that may be missing the route and weather
#     extras without saying so.
#   * It runs the preflight first, so the one command that starts the demo is also
#     the one that says which part of it will not work.
#   * It prints the preflight's verdict before the UI's own, so an operator reads
#     the honest answer before a judge reads the screen.

set -euo pipefail

HERE="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd -- "$HERE/.." && pwd)"
cd -- "$REPO_ROOT"

PREFLIGHT=1
for argument in "$@"; do
    case "$argument" in
        --no-preflight) PREFLIGHT=0 ;;
        -h|--help)
            sed -n '3,17p' "$0" | sed 's/^# \{0,1\}//'
            exit 0
            ;;
        *)
            echo "unknown option: $argument (try --help)" >&2
            exit 2
            ;;
    esac
done

# --- the interpreter -------------------------------------------------------- #

if [[ -x "$REPO_ROOT/.venv/bin/python" ]]; then
    PYTHON="$REPO_ROOT/.venv/bin/python"
elif command -v python >/dev/null 2>&1; then
    PYTHON="$(command -v python)"
else
    echo "no python interpreter found; the demo cannot start." >&2
    exit 1
fi

if "$PYTHON" -c 'import streamlit' >/dev/null 2>&1; then
    if [[ -x "$REPO_ROOT/.venv/bin/streamlit" ]]; then
        STREAMLIT="$REPO_ROOT/.venv/bin/streamlit"
    else
        STREAMLIT="$PYTHON -m streamlit"
    fi
else
    echo
    echo "streamlit is not installed in this interpreter."
    echo "The presentation layer does not need it -- replay, the timeline, the"
    echo "evidence view and the benchmark numbers all work without it -- but the"
    echo "screen does. Install it with:"
    echo
    echo "    $PYTHON -m pip install 'streamlit>=1.40'"
    echo
    exit 1
fi

# --- the preflight ---------------------------------------------------------- #

if [[ "$PREFLIGHT" == "1" ]]; then
    echo
    "$PYTHON" scripts/demo_preflight.py || true
    echo
fi

# --- a port ----------------------------------------------------------------- #

if [[ -z "${DEMO_PORT:-}" ]]; then
    DEMO_PORT="$("$PYTHON" - <<'PY'
import socket

with socket.socket() as probe:
    probe.bind(("127.0.0.1", 0))
    print(probe.getsockname()[1])
PY
)"
fi

URL="http://localhost:${DEMO_PORT}"

echo "=============================================================================="
echo "Starting the TrailGuard demo on ${URL}"
echo
echo "  Stop it with Ctrl-C. A browser window should open by itself; if it does"
echo "  not, open ${URL} on this laptop."
echo
echo "  The five tabs are Overview, Risk Timeline, Evidence, Benchmark and"
echo "  Architecture. Agent Mode is a switch inside Risk Timeline, because it is"
echo "  the only part of the demo that depends on the local model being up."
echo "=============================================================================="
echo

# --server.address localhost is the default; it is stated here so that anyone
# reading this script knows the demo is not reachable from the network.
# --server.headless is the one flag that depends on who is running the script.
# Headless false lets streamlit open the browser, which is what a presenter
# wants; it also means streamlit will ask its one-time onboarding question the
# first time it runs, and that question blocks a launch nobody is watching --
# a demo started from a launcher, a menu entry or a terminal that already
# closed. So: a terminal attached to a person gets the browser, and anything
# else gets the headless server whose URL is printed just above.
if [[ -t 0 ]]; then
    HEADLESS="false"
else
    HEADLESS="true"
fi

exec $STREAMLIT run demo/streamlit/app.py \
    --server.port "$DEMO_PORT" \
    --server.address localhost \
    --server.headless "$HEADLESS" \
    --browser.gatherUsageStats false \
    --theme.base light
