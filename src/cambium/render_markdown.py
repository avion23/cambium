"""Terminal markdown rendering for model summaries (stdlib only).

Supported constructs: ATX headings ``#``..``####`` (all bold, brightness falls
from h1), fenced ``` blocks (content verbatim with a dim-cyan tint), inline
``code`` (yellow), ``**bold**``, ``*italic*`` (dim), unordered/ordered lists,
and ``>`` blockquotes. Paragraphs and blank lines pass through verbatim.

Every C0/C1 control character except ``\\n`` and ``\\t`` is stripped before
any processing so raw escape sequences decoded from model output can never reach
the terminal.
"""

from __future__ import annotations

import os
import re
from typing import TextIO

_RESET = "\x1b[0m"
_HEADING_STYLES = {
    1: "\x1b[1;97m",
    2: "\x1b[1;37m",
    3: "\x1b[1;90m",
    4: "\x1b[1;30m",
}
_FENCE_STYLE = "\x1b[2;36m"
_CODE_STYLE = "\x1b[33m"
_BOLD_STYLE = "\x1b[1m"
_ITALIC_STYLE = "\x1b[2m"
_QUOTE_PREFIX_STYLE = "\x1b[2;3m"

_C0_CONTROLS = re.compile(r"[\x00-\x08\x0b-\x0d\x0e-\x1f\x80-\x9f]")
_HEADING = re.compile(r"^(#{1,4}) (.*)$")
_FENCE_MARKER = re.compile(r"^```")
_INLINE = re.compile(
    r"`([^`\n]+)`"                      # code span
    r"|\*\*(\S(?:[^*\n]*\S)?)\*\*"      # bold
    r"|\*(\S(?:[^*\n]*\S)?)\*"          # italic
)


def _strip_controls(text: str) -> str:
    return _C0_CONTROLS.sub("", text)


def _render_inline(text: str) -> str:
    def replace(match: re.Match[str]) -> str:
        code, bold, italic = match.groups()
        if code is not None:
            return f"{_CODE_STYLE}{code}{_RESET}"
        if bold is not None:
            return f"{_BOLD_STYLE}{bold}{_RESET}"
        return f"{_ITALIC_STYLE}{italic}{_RESET}"

    return _INLINE.sub(replace, text)


def render_markdown(text: str) -> str:
    """Render one markdown document to an ANSI-styled terminal string."""
    clean = _strip_controls(text)
    out: list[str] = []
    in_fence = False
    for line in clean.split("\n"):
        marker = _FENCE_MARKER.match(line.lstrip(" \t"))
        if in_fence:
            in_fence = marker is None
            out.append(f"{_FENCE_STYLE}{line}{_RESET}")
            continue
        if marker:
            in_fence = True
            out.append(f"{_FENCE_STYLE}{line}{_RESET}")
            continue
        heading = _HEADING.match(line)
        if heading:
            style = _HEADING_STYLES[len(heading.group(1))]
            out.append(f"{style}{heading.group(2).strip()}{_RESET}")
            continue
        if line.startswith(">"):
            body = line[1:]
            if body.startswith((" ", "\t")):
                body = body[1:]
            out.append(f"{_QUOTE_PREFIX_STYLE}>{_RESET} {_render_inline(body)}")
            continue
        out.append(_render_inline(line))
    return "\n".join(out)


def render_markdown_if_tty(text: str, stream: TextIO) -> str:
    """Render ``text`` only for color-capable terminals; otherwise pass through."""
    clean = _strip_controls(text)
    try:
        is_tty = bool(stream.isatty())
    except (AttributeError, OSError, ValueError):
        return clean
    if not is_tty:
        return clean
    if os.environ.get("NO_COLOR"):
        return clean
    if os.environ.get("TERM", "") == "dumb":
        return clean
    return render_markdown(clean)


__all__ = ["render_markdown", "render_markdown_if_tty"]
