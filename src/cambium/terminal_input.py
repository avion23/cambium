"""POSIX terminal input owned by the same event loop as the timeline.

No native editor buffer or input thread is shared with the renderer. Pasted
newlines remain one prompt; Enter submits, Alt-Enter inserts a newline.
"""

from __future__ import annotations

import asyncio
import codecs
import os
import termios
from collections.abc import Callable
from pathlib import Path
from typing import Any

from rich.cells import split_graphemes

from .terminal import supports_cursor_controls


def _grapheme_boundaries(text: str) -> list[int]:
    """Return code-point indexes delimiting grapheme clusters."""

    spans, _ = split_graphemes(text)
    return [0, *(end for _, end, _ in spans)]


def _previous_boundary(text: str, cursor: int) -> int:
    """Return the grapheme boundary immediately before ``cursor``."""

    previous = 0
    for boundary in _grapheme_boundaries(text):
        if boundary >= cursor:
            break
        previous = boundary
    return previous


def _next_boundary(text: str, cursor: int) -> int:
    """Return the grapheme boundary immediately after ``cursor``."""

    for boundary in _grapheme_boundaries(text):
        if boundary > cursor:
            return boundary
    return len(text)


def _line_cell_width(text: str) -> int:
    return split_graphemes(text)[1]


def _index_at_cell(text: str, cells: int) -> int:
    """Return a grapheme boundary at or before ``cells`` terminal cells."""

    if cells <= 0:
        return 0
    used = 0
    index = 0
    spans, _ = split_graphemes(text)
    for _boundary, next_boundary, width in spans:
        if used + width > cells:
            break
        used += width
        index = next_boundary
    return index


def _line_bounds(text: str, cursor: int) -> tuple[int, int]:
    """Return start/end indexes of the line containing ``cursor``."""

    start = text.rfind("\n", 0, cursor) + 1
    end = text.find("\n", cursor)
    return start, len(text) if end < 0 else end


def _is_text_character(char: str) -> bool:
    """Accept printable text and the joiner needed by emoji graphemes."""

    return char.isprintable() or char == "\u200d"


class TerminalInput:
    def __init__(
        self,
        fd: int,
        timeline: Any,
        history_path: Path,
        interrupt: Callable[[], str | None],
        focus: Callable[[], None],
    ) -> None:
        self.fd, self.timeline = fd, timeline
        self.history_path, self.interrupt, self.focus = history_path, interrupt, focus
        self.loop = asyncio.get_running_loop()
        self.queue: asyncio.Queue[str | None] = asyncio.Queue()
        self.decoder = codecs.getincrementaldecoder("utf-8")("replace")
        self.pending = ""
        self.text, self.cursor = "", 0
        self.pasting = False
        self._suppress_lf = False
        self._bracketed_paste = supports_cursor_controls(timeline.stream)
        self.reading = False
        self.block: list[str] | None = None
        self.continued: list[str] = []
        self.saved = termios.tcgetattr(fd)
        try:
            self.history = history_path.read_text().splitlines()[-1000:]
        except (OSError, UnicodeError):
            self.history = []
        self.history_index = len(self.history)
        self.draft = ""
        mode = termios.tcgetattr(fd)
        for flag_name in (
            "IXON",
            "IXOFF",
            "IXANY",
            "IGNBRK",
            "IGNPAR",
            "ICRNL",
            "INLCR",
            "IGNCR",
            "BRKINT",
            "PARMRK",
            "INPCK",
            "ISTRIP",
        ):
            mode[0] &= ~getattr(termios, flag_name, 0)
        for flag_name in (
            "ECHO",
            "ECHONL",
            "ECHOE",
            "ECHOK",
            "ECHOKE",
            "ECHOCTL",
            "ECHOPRT",
            "ICANON",
            "ISIG",
            "IEXTEN",
        ):
            mode[3] &= ~getattr(termios, flag_name, 0)
        mode[6][termios.VMIN], mode[6][termios.VTIME] = 1, 0
        termios.tcsetattr(fd, termios.TCSANOW, mode)
        try:
            self.loop.add_reader(fd, self._read_ready)
            if self._bracketed_paste:
                self.timeline.stream.write("\x1b[?2004h")
                self.timeline.stream.flush()
        except BaseException:
            self.loop.remove_reader(fd)
            termios.tcsetattr(fd, termios.TCSANOW, self.saved)
            raise

    def close(self) -> None:
        self.loop.remove_reader(self.fd)
        termios.tcsetattr(self.fd, termios.TCSANOW, self.saved)
        if self._bracketed_paste:
            self.timeline.stream.write("\x1b[?2004l")
            self.timeline.stream.flush()
        try:
            self.history_path.parent.mkdir(parents=True, exist_ok=True)
            self.history_path.write_text("\n".join(self.history[-1000:]) + "\n")
            self.history_path.chmod(0o600)
        except OSError:
            pass  # Optional editor history must not prevent terminal restoration or exit.

    async def read(self) -> str | None:
        if not self.queue.empty():
            return self.queue.get_nowait()
        self.reading = True
        self.timeline.move_to_input(label="…" if self.block is not None else "›")
        self._paint()
        try:
            return await self.queue.get()
        finally:
            self.reading = False

    def _paint(self) -> None:
        self.timeline.set_input(self.text, self.cursor, paint=self.reading)

    def _insert(self, text: str) -> None:
        self.text = self.text[: self.cursor] + text + self.text[self.cursor :]
        self.cursor += len(text)

    def _submit(self, value: str | None) -> None:
        if value:
            # Native history is line-oriented. Keep multiline prompts in durable
            # session history instead of splitting them into executable lines.
            if "\n" not in value and (not self.history or value != self.history[-1]):
                self.history.append(value)
        self.text, self.cursor, self.draft = "", 0, ""
        self.history_index = len(self.history)
        self._paint()
        self.timeline.hide_cursor(commit=True)
        self.reading = False
        self.queue.put_nowait(value)

    def _enter(self) -> None:
        value = self.text
        self.text, self.cursor = "", 0
        if self.block is not None:
            if value.strip() == ">>>":
                value, self.block = "\n".join(self.block), None
            else:
                self.block.append(value)
                return
        elif value.strip() == "<<<":
            self.block = []
            return
        elif value.endswith("\\"):
            self.continued.append(value[:-1])
            return
        elif self.continued:
            value = "\n".join([*self.continued, value])
            self.continued.clear()
        self._submit(value)

    def _key(self, key: str) -> None:  # noqa: C901 - flat terminal-key dispatch
        # POSIX terminals commonly report Enter as CRLF.  We process CR as
        # the submit event and discard only its immediately following LF,
        # including when the pair is split across separate reads.
        suppress_lf = getattr(self, "_suppress_lf", False)
        if key == "\n" and suppress_lf:
            self._suppress_lf = False
            return
        self._suppress_lf = key in {"\r", "\x1b\r"}
        if key == "\x1b[200~":
            self.pasting = True
        elif key == "\x1b[201~":
            self.pasting = False
        elif key == "\x1b[17~":  # F6 selects another lane without submitting the draft.
            self.focus()
        elif key in {"\x1b[D", "\x02"}:
            self.cursor = _previous_boundary(self.text, self.cursor)
        elif key in {"\x1b[C", "\x06"}:
            self.cursor = _next_boundary(self.text, self.cursor)
        elif key in {
            "\x1b[H",
            "\x1bOH",
            "\x1b[1~",
            "\x01",
        }:
            self.cursor = self.text.rfind("\n", 0, self.cursor) + 1
        elif key in {
            "\x1b[F",
            "\x1bOF",
            "\x1b[4~",
            "\x05",
        }:
            end = self.text.find("\n", self.cursor)
            self.cursor = len(self.text) if end < 0 else end
        elif key in {"\x7f", "\x08"} and self.cursor:
            start = _previous_boundary(self.text, self.cursor)
            self.text = self.text[:start] + self.text[self.cursor :]
            self.cursor = start
        elif key == "\x1b[3~":
            end = _next_boundary(self.text, self.cursor)
            self.text = self.text[: self.cursor] + self.text[end:]
        elif key == "\x15":
            start, _ = _line_bounds(self.text, self.cursor)
            self.text = self.text[:start] + self.text[self.cursor :]
            self.cursor = start
        elif key == "\x0b":
            _, end = _line_bounds(self.text, self.cursor)
            self.text = self.text[: self.cursor] + self.text[end:]
        elif key == "\x17":
            start, _ = _line_bounds(self.text, self.cursor)
            end = self.cursor
            while end > start:
                previous = _previous_boundary(self.text, end)
                if not self.text[previous:end].isspace():
                    break
                end = previous
            while end > start:
                previous = _previous_boundary(self.text, end)
                if self.text[previous:end].isspace():
                    break
                end = previous
            self.text = self.text[:end] + self.text[self.cursor :]
            self.cursor = end
        elif key in {"\x1b[A", "\x1b[B"} and "\n" in self.text:
            # Arrow keys edit a multiline draft instead of replacing it with history.
            start, current_end = _line_bounds(self.text, self.cursor)
            column = _line_cell_width(self.text[start : self.cursor])
            if key == "\x1b[A" and start:
                previous_end = start - 1
                previous = self.text.rfind("\n", 0, previous_end) + 1
                self.cursor = previous + _index_at_cell(self.text[previous:previous_end], column)
            elif key == "\x1b[B":
                if current_end < len(self.text):
                    following = self.text.find("\n", current_end + 1)
                    following_end = len(self.text) if following < 0 else following
                    following_start = current_end + 1
                    self.cursor = following_start + _index_at_cell(
                        self.text[following_start:following_end], column
                    )
        elif key in {"\x1b[A", "\x1b[B"}:
            if self.history_index == len(self.history):
                self.draft = self.text
            change = -1 if key == "\x1b[A" else 1
            self.history_index = min(len(self.history), max(0, self.history_index + change))
            self.text = (
                self.history[self.history_index]
                if self.history_index < len(self.history)
                else self.draft
            )
            self.cursor = len(self.text)
        elif key == "\x03":
            self.block, self.continued = None, []
            self._submit(self.interrupt())
        elif key == "\x04":
            if not self.text:
                self._submit(None)
            else:
                end = _next_boundary(self.text, self.cursor)
                self.text = self.text[: self.cursor] + self.text[end:]
        elif key in {"\r", "\n"}:
            self._enter()
        elif key in {"\x1b\r", "\x1b\n"}:
            self._insert("\n")
        elif key == "\t" or (len(key) == 1 and _is_text_character(key)):
            self._insert("    " if key == "\t" else key)

    def _read_ready(self) -> None:
        try:
            data = os.read(self.fd, 65536)
            if not data:
                self.loop.remove_reader(self.fd)
                self._submit(None)
                return
            self.pending += self.decoder.decode(data)
            while self.pending:
                if self.pasting and not self.pending.startswith("\x1b"):
                    # Insert a paste chunk once, not by repeatedly copying the
                    # whole draft for each character. Leave split CRLF intact.
                    end = self.pending.find("\x1b")
                    if end < 0:
                        end = len(self.pending) - int(self.pending.endswith("\r"))
                    if not end:
                        break
                    text, self.pending = self.pending[:end], self.pending[end:]
                    text = text.replace("\r\n", "\n").replace("\r", "\n")
                    self._insert(
                        "".join(char for char in text if _is_text_character(char) or char in "\n\t")
                    )
                    continue
                if self.pending.startswith("\x1b"):
                    # CSI sequences can arrive across reads; never execute a
                    # newline inside a pasted payload as an operator command.
                    if len(self.pending) == 1:
                        break
                    if self.pending[1] in "[O":
                        end = next(
                            (
                                i
                                for i in range(2, len(self.pending))
                                if "@" <= self.pending[i] <= "~"
                            ),
                            None,
                        )
                        if end is None:
                            break
                        key, self.pending = self.pending[: end + 1], self.pending[end + 1 :]
                    else:
                        key, self.pending = self.pending[:2], self.pending[2:]
                else:
                    key, self.pending = self.pending[0], self.pending[1:]
                if self.pasting and key != "\x1b[201~":
                    if len(key) == 1 and (_is_text_character(key) or key in "\r\n\t"):
                        self._insert("\n" if key == "\r" else key)
                else:
                    self._key(key)
            self._paint()
        except OSError:
            self.loop.remove_reader(self.fd)
            self._submit(None)
