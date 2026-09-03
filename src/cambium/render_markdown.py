"""Terminal markdown rendering for model summaries (stdlib only).

Supported constructs: ATX headings ``#``..``####`` (bold, colored by level),
fenced ``` blocks (content verbatim with a dim-cyan tint), inline ``code``
(dim yellow), ``**bold**`` (bright white), ``*italic*`` (magenta),
unordered/ordered list markers (green), ``>`` blockquotes (dim blue indicator),
and inline links (underlined blue). Paragraphs and blank lines pass through
verbatim.

Untrusted text crosses the shared terminal sanitization boundary before any
styling is added. Complete terminal control sequences are removed and explicit
bidirectional-format controls remain visible as ``\\uXXXX`` escapes.
"""

from __future__ import annotations

import os
import re
from typing import TextIO

from .terminal import sanitize_terminal_text

_RESET = "\x1b[0m"
_HEADING_STYLES = {
    1: "\x1b[1;96m",
    2: "\x1b[1;94m",
    3: "\x1b[1;95m",
    4: "\x1b[1;93m",
}
_FENCE_STYLE = "\x1b[2;36m"
_CODE_STYLE = "\x1b[2;33m"
_BOLD_STYLE = "\x1b[1;97m"
_ITALIC_STYLE = "\x1b[3;35m"
_LIST_MARKER_STYLE = "\x1b[32m"
_QUOTE_PREFIX_STYLE = "\x1b[2;34m"
_LINK_STYLE = "\x1b[4;34m"

_HEADING = re.compile(r"^(#{1,4}) (.*)$")
_FENCE_MARKER = re.compile(r"^```")
_LIST_ITEM = re.compile(r"^([ \t]*)([-+*]|\d+[.)])([ \t]+)(.*)$")
_INLINE = re.compile(
    r"`([^`\n]+)`"  # code span
    r"|\[([^\]\n]+)\]\(([^)\n]*)\)"  # inline link
    r"|\*\*(\S(?:[^*\n]*\S)?)\*\*"  # bold
    r"|\*(\S(?:[^*\n]*\S)?)\*"  # italic
)


def _render_inline(text: str) -> str:
    def replace(match: re.Match[str]) -> str:
        code, link_text, link_target, bold, italic = match.groups()
        if code is not None:
            style = _CODE_STYLE
            value = code
        elif link_text is not None:
            style = _LINK_STYLE
            value = match.group(0)
        elif bold is not None:
            style = _BOLD_STYLE
            value = bold
        else:
            style = _ITALIC_STYLE
            value = italic
        return f"{style}{value}{_RESET}"

    return _INLINE.sub(replace, text)


def render_markdown(text: str) -> str:
    """Render one sanitized markdown document to an ANSI-styled string."""
    clean = sanitize_terminal_text(text)
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
        list_item = _LIST_ITEM.match(line)
        if list_item:
            indent, marker, gap, body = list_item.groups()
            out.append(f"{indent}{_LIST_MARKER_STYLE}{marker}{_RESET}{gap}{_render_inline(body)}")
            continue
        out.append(_render_inline(line))
    return "\n".join(out)


def render_markdown_if_tty(text: str, stream: TextIO) -> str:
    """Style ``text`` only for color-capable terminals; always sanitize it."""
    clean = sanitize_terminal_text(text)
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
