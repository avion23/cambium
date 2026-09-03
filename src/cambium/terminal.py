"""Shared helpers for rendering untrusted plain text to a terminal."""

from __future__ import annotations

import os
import re
import unicodedata
from typing import Any

# CSI sequences cover common styling and cursor controls such as ``ESC[31m``.
# Include the 8-bit CSI introducer as well because it is another terminal
# escape form that can arrive in decoded provider text.
_CSI = re.compile(r"(?:\x1b\[|\x9b)[0-?]*[ -/]*[@-~]")

# OSC sequences can set terminal titles or access clipboard integrations.  The
# standard terminators are BEL, ST (ESC + backslash), and the 8-bit ST.  The
# end-of-input alternative fails closed for a truncated sequence: once an OSC
# introducer is seen, none of its remaining payload should be rendered as
# terminal input.
_OSC = re.compile(r"(?:\x1b\]|\x9d)(?s:.*?)(?:\x07|\x1b\\|\x9c|\Z)")

# Preserve the fact that an explicit bidi-format control was present without
# letting it reorder paths, source snippets, diagnostics, or neighbouring UI
# labels.  ASCII escapes are visible, searchable, and stable in logs.
_BIDI_CONTROL = re.compile("[\u061c\u200e\u200f\u202a-\u202e\u2066-\u2069]")

# Unicode NEL, LINE SEPARATOR, and PARAGRAPH SEPARATOR are line boundaries even
# though ``str.splitlines`` and terminal renderers do not all treat them alike.
# Normalize them before removing C1 controls so every output path agrees.
_UNICODE_LINE_SEPARATOR = re.compile("[\u0085\u2028\u2029]")

# Keep tabs/newlines available to multiline renderers, but remove every other
# C0/C1 control and DEL.  Single-line renderers collapse the two remaining
# layout controls below.
_CONTROLS = re.compile(r"[\x00-\x08\x0b-\x1f\x7f-\x9f]")
_LINE_WHITESPACE = re.compile(r"[\t\n\r]+")


def _escape_bidi_control(match: re.Match[str]) -> str:
    return f"\\u{ord(match.group(0)):04X}"


def sanitize_terminal_text(value: Any, *, single_line: bool = False) -> str:
    """Return readable text that cannot inject terminal or bidi controls.

    CSI and OSC sequences are removed as complete sequences, rather than only
    deleting their introducer.  Explicit bidirectional-format controls become
    visible ``\\uXXXX`` escapes so logical text order remains inspectable.
    Unicode line separators are normalized to newlines, then collapsed with
    tabs when ``single_line`` is requested.
    """

    text = str(value)
    text = _UNICODE_LINE_SEPARATOR.sub("\n", text)
    text = _OSC.sub("", text)
    text = _CSI.sub("", text)
    text = _BIDI_CONTROL.sub(_escape_bidi_control, text)
    text = _CONTROLS.sub("", text).replace("\r", "")
    if single_line:
        text = _LINE_WHITESPACE.sub(" ", text)
    return text


def is_tty(stream: Any) -> bool:
    """Return whether ``stream`` reports an interactive terminal."""

    try:
        return bool(getattr(stream, "isatty", lambda: False)())
    except (AttributeError, OSError, ValueError):
        return False


def supports_cursor_controls(stream: Any) -> bool:
    """Return whether Cambium may use cursor-addressing control sequences.

    ``NO_COLOR`` intentionally has no effect: it is a color preference, not a
    request to disable interactive cursor movement.  An explicit ``TERM=dumb``
    does disable screen-oriented rendering.
    """

    return is_tty(stream) and os.environ.get("TERM", "").strip().casefold() != "dumb"


def _cell_width(char: str) -> int:
    if unicodedata.combining(char) or unicodedata.category(char) == "Cf":
        return 0
    return 2 if unicodedata.east_asian_width(char) in {"W", "F"} else 1


def terminal_display_width(value: Any) -> int:
    """Return the terminal-cell width of sanitized single-line plain text."""

    text = sanitize_terminal_text(value, single_line=True)
    return sum(_cell_width(char) for char in text)


def clip_terminal_text(value: Any, width: int) -> str:
    """Clip plain text to ``width`` terminal cells without splitting code points."""

    if width <= 0:
        return ""
    text = sanitize_terminal_text(value, single_line=True)
    if terminal_display_width(text) <= width:
        return text

    ellipsis = "…"
    if width == 1:
        return ellipsis
    limit = width - terminal_display_width(ellipsis)
    used = 0
    clipped: list[str] = []
    for char in text:
        char_width = _cell_width(char)
        if used + char_width > limit:
            break
        clipped.append(char)
        used += char_width
    return "".join(clipped) + ellipsis


def pad_terminal_text(value: Any, width: int) -> str:
    """Clip and right-pad plain text to exactly ``width`` terminal cells."""

    if width <= 0:
        return ""
    text = clip_terminal_text(value, width)
    return text + " " * max(0, width - terminal_display_width(text))


__all__ = [
    "clip_terminal_text",
    "is_tty",
    "pad_terminal_text",
    "sanitize_terminal_text",
    "supports_cursor_controls",
    "terminal_display_width",
]
