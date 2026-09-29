"""Text that came from somewhere else, and has to be shown without becoming code.

Kept apart from :mod:`panels` because it is the one piece of the UI layer that
handles untrusted input and therefore the one piece that deserves its own tests.
It imports nothing from the project, so a test can exercise it without pulling a
UI framework into the process.
"""

from __future__ import annotations

import html
import re


def escape(text: object) -> str:
    """XML/HTML escaping for anything that reaches an SVG or a style attribute."""
    return html.escape(str(text), quote=True)


def fenced(text: str, language: str = "text") -> str:
    """Wrap ``text`` in a fence it cannot close.

    The agent's answer is the one string in this app that came from outside the
    repository. It has to be shown in full -- a judge is being asked to trust a
    model, so hiding what it said would defeat the purpose -- and it has to be
    inert. Choosing a backtick fence longer than the longest run inside the text
    means the answer can always be displayed in a code block, and can never be
    used to smuggle markdown, a link or HTML out of it.
    """
    longest = max((len(match.group(0)) for match in re.finditer(r"`+", text or "")), default=0)
    fence = "`" * max(3, longest + 1)
    return f"{fence}{language}\n{text or ''}\n{fence}"


def level_rows(levels: dict[str, str] | None) -> list[dict[str, str]]:
    """A horizon-to-level mapping as table rows, in horizon order.

    ``None`` and ``{}`` both become an empty list, which the caller renders as "no
    claim was readable" rather than as a table with nothing in it.
    """
    order = ("NOW", "+30min", "+60min", "+120min")
    levels = levels or {}
    return [{"horizon": horizon, "level": levels[horizon]} for horizon in order if horizon in levels]
