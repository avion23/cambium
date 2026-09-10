"""Deterministic POSIX terminal-input and capability regressions."""

from __future__ import annotations

import asyncio
import io
import os
import pty
import termios
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import pytest

from cambium.terminal import (
    TerminalCapabilities,
    clip_terminal_text,
    sanitize_terminal_text,
    supports_cursor_controls,
    supports_synchronized_output,
    terminal_capabilities,
    terminal_color_depth,
    terminal_display_width,
)
from cambium.terminal_input import TerminalInput


class _Tty(io.StringIO):
    def isatty(self) -> bool:
        return True


class _Cockpit:
    def __init__(self, stream: _Tty) -> None:
        self.stream = stream
        self.inputs: list[tuple[str, int, bool]] = []
        self.moves: list[str] = []
        self.hidden = 0

    def set_input(self, text: str, cursor: int, *, paint: bool) -> None:
        self.inputs.append((text, cursor, paint))

    def move_to_input(self, *, label: str) -> None:
        self.moves.append(label)

    def hide_cursor(self, *, commit: bool) -> None:
        del commit
        self.hidden += 1


@contextmanager
def _editor(
    tmp_path: Path,
    *,
    term: str = "xterm-256color",
    interrupt: Any = None,
    focus: Any = None,
) -> Iterator[tuple[TerminalInput, int, _Cockpit, list[str]]]:
    """Construct the real POSIX editor over a PTY and restore its mode."""

    master, slave = pty.openpty()
    stream = _Tty()
    cockpit = _Cockpit(stream)
    interrupted = []
    focused = []
    if interrupt is None:

        def interrupt() -> None:
            interrupted.append("interrupt")

    if focus is None:

        def focus() -> None:
            focused.append("focus")

    old_term = os.environ.get("TERM")
    os.environ["TERM"] = term
    try:
        editor = TerminalInput(
            slave,
            cockpit,
            tmp_path / "history",
            interrupt=interrupt,
            focus=focus,
        )
        try:
            yield editor, master, cockpit, focused
        finally:
            editor.close()
    finally:
        if old_term is None:
            os.environ.pop("TERM", None)
        else:
            os.environ["TERM"] = old_term
        os.close(master)
        os.close(slave)


def _feed(editor: TerminalInput, master: int, *chunks: bytes) -> None:
    for chunk in chunks:
        os.write(master, chunk)
        editor._read_ready()


def _queued(editor: TerminalInput) -> list[str | None]:
    values: list[str | None] = []
    while not editor.queue.empty():
        values.append(editor.queue.get_nowait())
    return values


def _run(coro: Any) -> Any:
    return asyncio.run(coro)


def test_terminal_capability_matrix_handles_pipes_term_no_color_and_color_depth(
    monkeypatch,
) -> None:
    stream = _Tty()
    monkeypatch.setenv("TERM", "ansi")
    monkeypatch.delenv("NO_COLOR", raising=False)
    assert terminal_color_depth(stream) == 16
    assert supports_cursor_controls(stream)
    assert not supports_synchronized_output(stream)

    monkeypatch.setenv("TERM", "xterm-256color")
    assert terminal_color_depth(stream) == 256
    assert supports_synchronized_output(stream) is True

    monkeypatch.setenv("COLORTERM", "truecolor")
    assert terminal_color_depth(stream) == 24

    monkeypatch.setenv("NO_COLOR", "")
    assert terminal_color_depth(stream) == 0
    assert supports_cursor_controls(stream)
    assert supports_synchronized_output(stream)

    monkeypatch.setenv("TERM", "dumb")
    assert terminal_capabilities(stream) == TerminalCapabilities()
    assert not supports_cursor_controls(stream)
    assert not supports_synchronized_output(stream)
    assert terminal_color_depth(io.StringIO()) == 0

    monkeypatch.setenv("TERM", "xterm-256color")
    assert terminal_capabilities(io.StringIO()) == TerminalCapabilities()

    monkeypatch.delenv("TERM", raising=False)
    monkeypatch.delenv("COLORTERM", raising=False)
    monkeypatch.delenv("NO_COLOR", raising=False)
    monkeypatch.setenv("TERM_PROGRAM", "kitty")
    assert not supports_cursor_controls(stream)
    assert not supports_synchronized_output(stream)


def test_terminal_text_sanitizes_lone_surrogates_and_grapheme_cells() -> None:
    clean = sanitize_terminal_text("left\ud800right\udfff")
    assert clean == r"left\ud800right\udfff"
    clean.encode("utf-8")

    assert terminal_display_width("👍🏽👩‍💻e\u0301界") == 2 + 2 + 1 + 2
    assert clip_terminal_text("👍🏽👩‍💻", 3) == "👍🏽…"


def test_terminal_input_split_crlf_submits_once(tmp_path: Path) -> None:
    async def scenario() -> None:
        with _editor(tmp_path) as (editor, master, _cockpit, _focused):
            _feed(editor, master, b"hello\r", b"\n")
            assert _queued(editor) == ["hello"]

    _run(scenario())


def test_terminal_input_paste_keeps_multiline_crlf_split_across_reads(tmp_path: Path) -> None:
    async def scenario() -> None:
        with _editor(tmp_path) as (editor, master, cockpit, _focused):
            _feed(
                editor,
                master,
                b"\x1b[200~first\r",
                b"\nsecond\x1b[201~",
                b"\r",
            )
            assert _queued(editor) == ["first\nsecond"]
            assert "\x1b[?2004h" in cockpit.stream.getvalue()
            assert "\x1b[?2004l" not in cockpit.stream.getvalue()
        assert "\x1b[?2004l" in cockpit.stream.getvalue()

        with _editor(tmp_path) as (editor, master, _cockpit, _focused):
            _feed(editor, master, "\x1b[200~👩‍💻e\u0301\x1b[201~\r".encode())
            assert _queued(editor) == ["👩‍💻e\u0301"]

    _run(scenario())


def test_terminal_input_split_paste_markers_and_alt_enter_are_incremental(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        with _editor(tmp_path) as (editor, master, _cockpit, _focused):
            _feed(editor, master, b"\x1b[2", b"00~hello\x1b[20", b"1~", b"\r")
            assert _queued(editor) == ["hello"]

        with _editor(tmp_path) as (editor, master, _cockpit, _focused):
            _feed(editor, master, b"abc\x1b", b"\r", b"\n")
            assert editor.text == "abc\n"
            assert _queued(editor) == []

    _run(scenario())


def test_terminal_input_controls_preserve_multiline_draft_and_focus(tmp_path: Path) -> None:
    async def scenario() -> None:
        focused: list[str] = []
        with _editor(tmp_path, focus=lambda: focused.append("focus")) as (
            editor,
            master,
            _cockpit,
            _focused,
        ):
            editor.text, editor.cursor = "first\nsecond\nthird", len("first\nsec")
            editor._key("\x15")
            assert (editor.text, editor.cursor) == ("first\nond\nthird", len("first\n"))

            editor.text, editor.cursor = "first\nsecond\nthird", len("first\nsec")
            editor._key("\x0b")
            assert (editor.text, editor.cursor) == ("first\nsec\nthird", len("first\nsec"))

            editor.text, editor.cursor = "first\nsecond\nthird", len("first\nsecond")
            editor._key("\x17")
            assert (editor.text, editor.cursor) == ("first\n\nthird", len("first\n"))

            editor.text, editor.cursor = "first\nsecond", len("first\nse")
            editor._key("\x1b[H")
            assert editor.cursor == len("first\n")
            editor.cursor = len("first\nse")
            editor._key("\x1b[F")
            assert editor.cursor == len("first\nsecond")
            editor.cursor = len("first\nse")
            editor._key("\x01")
            assert editor.cursor == len("first\n")
            editor._key("\x05")
            assert editor.cursor == len("first\nsecond")

            _feed(editor, master, b"\x1b[17~")
            assert focused == ["focus"]
            assert editor.text == "first\nsecond"

    _run(scenario())


def test_terminal_input_grapheme_editing_and_cell_vertical_navigation(tmp_path: Path) -> None:
    async def scenario() -> None:
        with _editor(tmp_path) as (editor, _master, _cockpit, _focused):
            editor.text, editor.cursor = "e\u0301", 2
            editor._key("\x1b[D")
            assert editor.cursor == 0
            editor._key("\x1b[C")
            assert editor.cursor == 2
            editor._key("\x7f")
            assert (editor.text, editor.cursor) == ("", 0)

            editor.text, editor.cursor = "👍🏽", 0
            editor._key("\x04")
            assert editor.text == ""

            editor.text, editor.cursor = "界a\nx", len("界a\nx")
            editor._key("\x1b[A")
            assert editor.cursor == 0

    _run(scenario())


def test_terminal_input_ctrl_c_idle_and_active_signals(tmp_path: Path) -> None:
    async def scenario() -> None:
        with _editor(tmp_path, interrupt=lambda: None) as (editor, master, _cockpit, _focused):
            _feed(editor, master, b"\x03")
            assert _queued(editor) == [None]

        with _editor(tmp_path, interrupt=lambda: "/cancel") as (editor, master, _cockpit, _focused):
            _feed(editor, master, b"\x03")
            assert _queued(editor) == ["/cancel"]

    _run(scenario())


def test_terminal_input_restores_pty_mode_and_disables_bracketed_paste_for_dumb(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        master, slave = pty.openpty()
        before = termios.tcgetattr(slave)
        stream = _Tty()
        cockpit = _Cockpit(stream)
        old_term = os.environ.get("TERM")
        os.environ["TERM"] = "dumb"
        try:
            editor = TerminalInput(
                slave,
                cockpit,
                tmp_path / "history",
                interrupt=lambda: None,
                focus=lambda: None,
            )
            try:
                mode = termios.tcgetattr(slave)
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
                    assert mode[0] & getattr(termios, flag_name, 0) == 0
                for flag_name in (
                    "ECHO",
                    "ECHONL",
                    "ICANON",
                    "ISIG",
                    "IEXTEN",
                ):
                    assert mode[3] & getattr(termios, flag_name, 0) == 0
                assert "\x1b[?2004h" not in stream.getvalue()
            finally:
                editor.close()
            assert termios.tcgetattr(slave) == before
            assert "\x1b[?2004l" not in stream.getvalue()
        finally:
            if old_term is None:
                os.environ.pop("TERM", None)
            else:
                os.environ["TERM"] = old_term
            os.close(master)
            os.close(slave)

    _run(scenario())


@pytest.mark.parametrize("term", ["screen-256color", "tmux-256color", "xterm-256color"])
def test_tmux_and_ssh_like_terms_keep_256_color(monkeypatch, term: str) -> None:
    stream = _Tty()
    monkeypatch.setenv("TERM", term)
    monkeypatch.delenv("NO_COLOR", raising=False)
    assert terminal_color_depth(stream) == 256
