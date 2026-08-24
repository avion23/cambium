"""Small helpers for rendering untrusted text to a terminal."""

from __future__ import annotations

import re
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

# Keep tabs/newlines available to the multiline cockpit renderer, but remove
# every other C0/C1 control and DEL.  Single-line renderers collapse the two
# remaining layout controls below.
_CONTROLS = re.compile(r"[\x00-\x08\x0b-\x1f\x7f-\x9f]")
_LINE_WHITESPACE = re.compile(r"[\t\n\r]+")


def sanitize_terminal_text(value: Any, *, single_line: bool = False) -> str:
    """Remove terminal controls from text before interpolating it in output.

    CSI and OSC sequences are removed as complete sequences, rather than only
    deleting their introducer.  This preserves ordinary readable text (for
    example a literal ``"[31m"``) while ensuring provider/model/error text
    cannot execute terminal commands.  ``single_line`` additionally converts
    tabs and newlines to spaces for fixed-width dashboard rows.
    """

    text = str(value)
    text = _OSC.sub("", text)
    text = _CSI.sub("", text)
    text = _CONTROLS.sub("", text).replace("\r", "")
    if single_line:
        text = _LINE_WHITESPACE.sub(" ", text)
    return text


__all__ = ["sanitize_terminal_text"]
