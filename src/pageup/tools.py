"""Shared utility helpers for the pageup package.

This module provides:

* **``Pipeline``** — a lightweight function-composition utility that chains
  an ordered sequence of callables, passing the output of each as the input
  to the next.
* **``cleaner``** — a pre-built ``Pipeline`` that normalises raw text
  extracted from the SberChat DOM: strips leading time prefixes, removes
  emoji, and collapses redundant whitespace.
* **``finalize_quote_content``** — post-processes quoted message text after
  DOM extraction (sender prefix, time prefix, whitespace).
* **``strip_leading_sender_name``** — removes a duplicate quote author prefix
  (used inside ``finalize_quote_content``).
* **Small pure functions** used inside the pipeline and quote normalisation.
* **Constants** shared across modules: the Moscow timezone object and the
  compiled group-URL pattern.

Used by:
    models.py — cleaner on every get_text(); finalize_quote_content on quotes;
                group_url_pattern in ParsingTask
    cli.py    — moscow_timezone when parsing --min-date
"""

import re
from typing import Any
from zoneinfo import ZoneInfo

import emoji


# ── Constants ─────────────────────────────────────────────────────────────────

# Canonical origin for SberChat links.  models._get_sender_url() concatenates
# this with relative href fragments from the DOM.
SBERCHAT_BASE_URL: str = "https://sberchat.sberbank.ru/"

# Validates CLI --group-url and ParsingTask.group_url.
# Must match a *group* chat (…/group{digits}), not a private DM URL.
# group_url_pattern uses fullmatch — trailing slashes or query strings are rejected.
# \d+ requires at least one digit in the group id segment.
group_url_pattern: re.Pattern[str] = re.compile(
    rf"{re.escape(SBERCHAT_BASE_URL)}#/chat/group\d+"
)

# Quote wrappers sometimes include a visible "17:22" prefix in raw text.
# Anchored at start (^ implicit via match()); DOTALL allows content after newline.
# (.+) requires at least one character after the time; bare "17:22" is left unchanged.
_time_prefix_pattern: re.Pattern[str] = re.compile(r"\d\d:\d\d(.+)", re.DOTALL)

# All message timestamps are converted to this zone for output and comparison.
# tzdata package ensures this works on minimal Linux images without system tz DB.
moscow_timezone: ZoneInfo = ZoneInfo("Europe/Moscow")


# ── Text-normalisation helpers ────────────────────────────────────────────────

def remove_time_prefix(string: str) -> str:
    """Strip a leading ``HH:MM`` time from *string*, if present.

    SberChat embeds the message timestamp inside some container elements
    (particularly reply / quote wrappers), so raw ``get_text()`` output may
    start with a time token.  This function removes it defensively — it is a
    no-op when no time prefix is found.
    """
    # match() only succeeds when the time is at the very beginning of the string.
    match = _time_prefix_pattern.match(string)
    return match.group(1) if match else string


def remove_emoji(string: str) -> str:
    """Replace all Unicode emoji in *string* with an empty string."""
    # Empty replace keeps surrounding text; double spaces are fixed later.
    return emoji.replace_emoji(string, replace="")


def remove_redundant_whitespaces(string: str) -> str:
    """Collapse all runs of whitespace in *string* to a single space."""
    # str.split() with no args splits on any whitespace run.
    return " ".join(string.split())


def strip_leading_sender_name(content: str, sender_name: str | None) -> str:
    """Remove a duplicated quote sender name from the start of *content*.

    SberChat often embeds the quoted author's name inside the content element
    even though it is also shown in a separate title element.  This is a
    no-op when *sender_name* is missing or does not prefix *content*.
    """
    if not sender_name or not content.startswith(sender_name):
        return content
    # lstrip() removes the space SberChat inserts between name and message text.
    return content[len(sender_name):].lstrip()


def finalize_quote_content(content: str, sender_name: str | None) -> str:
    """Normalise quote text after DOM extraction.

    Strips a duplicate sender prefix, a leading ``HH:MM`` time (fallback
    wrappers), and collapses whitespace — including a leading space left after
    the time token.
    """
    # Sender before time: fallback wrapper text is often "Bob 17:22 …".
    # Stripping the name first leaves "17:22 …" where remove_time_prefix can match.
    content = strip_leading_sender_name(content, sender_name)
    content = remove_time_prefix(content)
    return remove_redundant_whitespaces(content)


# ── Pipeline ──────────────────────────────────────────────────────────────────

class Pipeline:
    """Compose an ordered sequence of single-argument callables.

    ``Pipeline(f, g, h)(v)`` is equivalent to ``h(g(f(v)))``.  Each callable
    receives the return value of the preceding one as its sole argument.

    Design: avoids a heavy functional library; used for post-processing in
    models.write_json() and for the shared ``cleaner`` instance.

    Example::

        pipe = Pipeline(str.strip, str.lower)
        pipe("  Hello  ")  # → "hello"
    """

    def __init__(self, *funcs: Any) -> None:
        # Callables are applied left-to-right (same order as written).
        self.funcs = funcs

    def __call__(self, arg: Any) -> Any:
        """Apply all functions in sequence and return the final result."""
        for func in self.funcs:
            arg = func(arg)
        return arg


# ── Pre-built cleaner pipeline ────────────────────────────────────────────────

# Applied in models.ParsingTask._get_text() for every DOM text extraction.
# Order matters: strip time before emoji removal, then normalise spaces.
cleaner: Pipeline = Pipeline(
    remove_time_prefix,
    remove_emoji,
    remove_redundant_whitespaces,
)
