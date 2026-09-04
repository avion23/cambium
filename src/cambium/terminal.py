"""Shared helpers for rendering untrusted plain text to a terminal."""

from __future__ import annotations

import os
import re
import unicodedata
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from functools import lru_cache
from threading import RLock
from typing import Any

SYNCHRONIZED_UPDATE_BEGIN = "\x1b[?2026h"
SYNCHRONIZED_UPDATE_END = "\x1b[?2026l"
_FRAME_CURSOR_REPOSITION = "\x1b[1A\r"


@dataclass(frozen=True, slots=True)
class TerminalCapabilities:
    """Capabilities that are safe to use for one terminal stream."""

    color_depth: int = 0
    cursor_controls: bool = False
    synchronized_output: bool = False


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


_COLOR_TERMINALS = frozenset(
    {
        "alacritty",
        "ansi",
        "cygwin",
        "foot",
        "konsole",
        "linux",
        "mintty",
        "putty",
        "rxvt",
        "screen",
        "st",
        "tmux",
        "wezterm",
        "xterm",
    }
)
_SYNCHRONIZED_TERMINAL_PREFIXES = (
    "alacritty",
    "contour",
    "foot",
    "kitty",
    "konsole",
    "rxvt",
    "screen",
    "st-",
    "tmux",
    "wezterm",
    "xterm",
)
_SYNCHRONIZED_TERMINAL_PROGRAMS = frozenset(
    {
        "alacritty",
        "apple_terminal",
        "contour",
        "foot",
        "hyper",
        "iterm.app",
        "kitty",
        "konsole",
        "rio",
        "tabby",
        "vscode",
        "warpterminal",
        "wezterm",
        "windows terminal",
    }
)
_SYNC_OVERRIDE_TRUE = frozenset({"1", "on", "true", "yes"})
_SYNC_OVERRIDE_FALSE = frozenset({"0", "off", "false", "no"})


def _terminal_color_depth(
    tty: bool,
    term: str,
    colorterm: str,
    no_color: bool,
) -> int:
    if not tty or no_color or term == "dumb":
        return 0
    if colorterm in {"24bit", "truecolor"} or term.endswith(("-direct", "-truecolor")):
        return 24
    if "256color" in term:
        return 256
    if term in _COLOR_TERMINALS or "color" in term:
        return 16
    return 0


def _synchronized_output_supported(
    tty: bool,
    term: str,
    term_program: str,
    override: str,
) -> bool:
    if not tty or term == "dumb":
        return False
    if override in _SYNC_OVERRIDE_FALSE:
        return False
    if override in _SYNC_OVERRIDE_TRUE:
        return True
    if term_program in _SYNCHRONIZED_TERMINAL_PROGRAMS:
        return True
    return any(term.startswith(prefix) for prefix in _SYNCHRONIZED_TERMINAL_PREFIXES)


@lru_cache(maxsize=32)
def _probe_terminal_capabilities(
    tty: bool,
    term: str,
    colorterm: str,
    no_color: bool,
    term_program: str,
    sync_override: str,
) -> TerminalCapabilities:
    """Probe environment-derived terminal capabilities once per environment."""
    return TerminalCapabilities(
        color_depth=_terminal_color_depth(tty, term, colorterm, no_color),
        cursor_controls=tty and term != "dumb",
        synchronized_output=_synchronized_output_supported(
            tty,
            term,
            term_program,
            sync_override,
        ),
    )


def terminal_capabilities(stream: Any) -> TerminalCapabilities:
    """Return cached terminal capabilities for ``stream`` and its environment."""
    return _probe_terminal_capabilities(
        is_tty(stream),
        os.environ.get("TERM", "").strip().casefold(),
        os.environ.get("COLORTERM", "").strip().casefold(),
        bool(os.environ.get("NO_COLOR")),
        os.environ.get("TERM_PROGRAM", "").strip().casefold(),
        os.environ.get("CAMBIUM_SYNCHRONIZED_OUTPUT", "").strip().casefold(),
    )


def terminal_color_depth(stream: Any) -> int:
    """Return the supported color level: 0, 16, 256, or 24-bit truecolor."""
    return terminal_capabilities(stream).color_depth


def supports_synchronized_output(stream: Any) -> bool:
    """Return whether DECSET 2026 is safe for the terminal stream."""
    return terminal_capabilities(stream).synchronized_output


@contextmanager
def synchronized_update(stream: Any, *, enabled: bool | None = None) -> Iterator[None]:
    """Batch one terminal update with DECSET 2026 when the capability is known."""
    use_sync = supports_synchronized_output(stream) if enabled is None else enabled
    if not use_sync:
        yield
        return

    stream.write(SYNCHRONIZED_UPDATE_BEGIN)
    try:
        yield
    finally:
        stream.write(SYNCHRONIZED_UPDATE_END)
        stream.flush()


class SynchronizedOutput:
    """Text stream proxy that brackets explicitly marked frame updates."""

    def __init__(self, stream: Any, *, enabled: bool = False) -> None:
        self.stream = stream
        self.enabled = enabled
        self._active = False
        self._lock = RLock()

    def isatty(self) -> bool:
        return is_tty(self.stream)

    def write(self, value: str) -> int:
        with self._lock:
            if self._active and value == _FRAME_CURSOR_REPOSITION:
                try:
                    self.stream.write(SYNCHRONIZED_UPDATE_END)
                finally:
                    self._active = False
            return self.stream.write(value)

    def flush(self) -> None:
        with self._lock:
            self.stream.flush()

    @contextmanager
    def frame(self) -> Iterator[None]:
        """Bracket one renderer frame while leaving prompts and edits untouched."""
        if not self.enabled:
            yield
            return
        with self._lock:
            self.stream.write(SYNCHRONIZED_UPDATE_BEGIN)
            self._active = True
        try:
            yield
        finally:
            with self._lock:
                if self._active:
                    try:
                        self.stream.write(SYNCHRONIZED_UPDATE_END)
                    finally:
                        self._active = False
                self.stream.flush()

    def __getattr__(self, name: str) -> Any:
        return getattr(self.stream, name)


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
    "SynchronizedOutput",
    "SYNCHRONIZED_UPDATE_BEGIN",
    "SYNCHRONIZED_UPDATE_END",
    "TerminalCapabilities",
    "clip_terminal_text",
    "is_tty",
    "pad_terminal_text",
    "sanitize_terminal_text",
    "synchronized_update",
    "supports_cursor_controls",
    "supports_synchronized_output",
    "terminal_capabilities",
    "terminal_color_depth",
    "terminal_display_width",
]
