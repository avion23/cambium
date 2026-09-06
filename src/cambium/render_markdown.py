"""Shared Rich Markdown rendering for Cambium terminal surfaces."""

from __future__ import annotations

import os
import shutil
from functools import lru_cache
from io import StringIO
from typing import Any, TextIO

from rich.box import ROUNDED
from rich.color import ColorSystem
from rich.console import Console
from rich.markdown import BlockQuote, CodeBlock, Heading, Markdown
from rich.padding import Padding
from rich.panel import Panel
from rich.segment import Segment
from rich.syntax import Syntax
from rich.theme import Theme

from .terminal import sanitize_terminal_text


class _CambiumHeading(Heading):
    LEVEL_ALIGN = {level: "left" for level in ("h1", "h2", "h3", "h4", "h5", "h6")}


class _CambiumBlockQuote(BlockQuote):
    def __rich_console__(self, console: Any, options: Any):
        render_options = options.update(width=max(1, options.max_width - 2))
        lines = console.render_lines(self.elements, render_options, style=self.style, pad=False)
        for line in lines:
            yield Segment("│ ", self.style)
            yield from line
            yield Segment.line()


class _CambiumCodeBlock(CodeBlock):
    def __rich_console__(self, console: Any, options: Any):
        code = Syntax(
            str(self.text).rstrip(),
            self.lexer_name,
            theme=self.theme,
            word_wrap=True,
            padding=0,
        )
        yield Padding(
            Panel(
                code,
                box=ROUNDED,
                border_style="markdown.code_block",
                expand=True,
                padding=(0, 1),
            ),
            (0, 0, 0, 2),
        )


class CambiumMarkdown(Markdown):
    """Markdown with compact code/quote blocks suited to a terminal pane."""

    elements = {
        **Markdown.elements,
        "heading_open": _CambiumHeading,
        "blockquote_open": _CambiumBlockQuote,
        "fence": _CambiumCodeBlock,
        "code_block": _CambiumCodeBlock,
    }


@lru_cache(maxsize=1)
def markdown_theme() -> Theme:
    """High-contrast palette shared by one-shot output and the live cockpit."""
    return Theme(
        {
            "markdown.h1": "bold bright_cyan",
            "markdown.h2": "bold bright_blue",
            "markdown.h3": "bold bright_magenta",
            "markdown.h4": "bold bright_green",
            "markdown.h5": "bold bright_yellow",
            "markdown.h6": "bold white",
            "markdown.code": "bold yellow",
            "markdown.code_block": "cyan",
            "markdown.item.bullet": "bright_magenta",
            "markdown.item.number": "bright_magenta",
            "markdown.block_quote": "blue",
            "markdown.table.border": "cyan",
            "markdown.table.header": "bold bright_cyan",
            "markdown.link": "underline bright_blue",
            "markdown.link_url": "dim cyan",
            "markdown.strong": "bold bright_white",
            "markdown.em": "italic bright_magenta",
        }
    )


def markdown_document(text: str) -> CambiumMarkdown:
    """Return a sanitized Rich document; model/provider escape codes never reach Rich."""
    return CambiumMarkdown(
        sanitize_terminal_text(text), hyperlinks=False, code_theme="ansi_dark"
    )


def render_markdown(text: str, *, width: int | None = None) -> str:
    """Render Markdown to ANSI using the same parser and palette as the cockpit."""
    console = Console(
        color_system="standard",
        file=StringIO(),
        force_terminal=True,
        height=None,
        highlight=False,
        markup=False,
        theme=markdown_theme(),
        width=max(20, width or shutil.get_terminal_size((100, 40)).columns),
    )
    lines: list[str] = []
    for line in console.render_lines(markdown_document(text), pad=False):
        segments = [segment for segment in line if not segment.control]
        while segments:
            trimmed = segments[-1].text.rstrip()
            if trimmed:
                segments[-1] = Segment(trimmed, segments[-1].style)
                break
            segments.pop()
        parts: list[str] = []
        for segment in segments:
            if segment.style:
                parts.append(segment.style.render(segment.text, color_system=ColorSystem.STANDARD))
            else:
                parts.append(segment.text)
        lines.append("".join(parts))
    while lines and not lines[-1].strip():
        lines.pop()
    return "\n".join(lines) + ("\n" if lines else "")


def render_markdown_if_tty(text: str, stream: TextIO) -> str:
    """Render on an interactive color terminal; otherwise return sanitized plain text."""
    clean = sanitize_terminal_text(text)
    try:
        is_tty = bool(stream.isatty())
    except (AttributeError, OSError, ValueError):
        return clean
    if not is_tty or os.environ.get("NO_COLOR") or os.environ.get("TERM", "") == "dumb":
        return clean
    return render_markdown(clean, width=shutil.get_terminal_size((100, 40)).columns)


__all__ = [
    "CambiumMarkdown",
    "markdown_document",
    "markdown_theme",
    "render_markdown",
    "render_markdown_if_tty",
]
