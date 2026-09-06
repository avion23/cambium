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

from .terminal import sanitize_terminal_text, terminal_color_depth


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
    """Readable no-background palette; Rich degrades it to the terminal's color depth."""
    return Theme(
        {
            "markdown.h1": "bold #5fd7ff",
            "markdown.h2": "bold #87afff",
            "markdown.h3": "bold #af87ff",
            "markdown.h4": "bold #5fd7af",
            "markdown.h5": "bold #d7af5f",
            "markdown.h6": "bold #d0d0d0",
            "markdown.code": "bold #ffd75f",
            "markdown.code_block": "#5f87af",
            "markdown.item.bullet": "#d787ff",
            "markdown.item.number": "#d787ff",
            "markdown.block_quote": "italic #87afd7",
            "markdown.table.border": "#5f87af",
            "markdown.table.header": "bold #5fd7ff",
            "markdown.link": "underline #5fafff",
            "markdown.link_url": "dim #00d7d7",
            "markdown.strong": "bold #eeeeee",
            "markdown.em": "italic #d787ff",
        }
    )


def markdown_document(text: str) -> CambiumMarkdown:
    """Return a sanitized Rich document; model/provider escape codes never reach Rich."""
    return CambiumMarkdown(sanitize_terminal_text(text), hyperlinks=False, code_theme="ansi_dark")


def _rich_color_system(depth: int) -> tuple[str | None, ColorSystem | None]:
    if depth == 24:
        return "truecolor", ColorSystem.TRUECOLOR
    if depth >= 256:
        return "256", ColorSystem.EIGHT_BIT
    if depth >= 16:
        return "standard", ColorSystem.STANDARD
    return None, None


def render_markdown_lines(text: str, *, width: int, color_depth: int = 16) -> list[str]:
    """Render sanitized Markdown to terminal lines without a pager or subprocess."""
    color_name, color_system = _rich_color_system(color_depth)
    console = Console(
        color_system=color_name,
        file=StringIO(),
        force_terminal=color_system is not None,
        height=None,
        highlight=False,
        markup=False,
        no_color=color_system is None,
        theme=markdown_theme(),
        width=max(20, width),
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
            if color_system is not None and segment.style:
                parts.append(segment.style.render(segment.text, color_system=color_system))
            else:
                parts.append(segment.text)
        lines.append("".join(parts))
    while lines and not lines[-1].strip():
        lines.pop()
    return lines


def render_markdown(text: str, *, width: int | None = None, color_depth: int = 256) -> str:
    """Render Markdown to ANSI using Cambium's in-process Rich renderer."""
    lines = render_markdown_lines(
        text,
        width=max(20, width or shutil.get_terminal_size((100, 40)).columns),
        color_depth=color_depth,
    )
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
    return render_markdown(
        clean,
        width=shutil.get_terminal_size((100, 40)).columns,
        color_depth=terminal_color_depth(stream),
    )


__all__ = [
    "CambiumMarkdown",
    "markdown_document",
    "markdown_theme",
    "render_markdown",
    "render_markdown_if_tty",
    "render_markdown_lines",
]
