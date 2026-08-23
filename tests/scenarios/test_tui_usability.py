"""Operator-facing TUI usability scenarios."""

from __future__ import annotations

import asyncio
import io
import os
from pathlib import Path

from cambium import tui
from cambium.oneshot import OneShotConfig


class _Tty(io.StringIO):
    def isatty(self) -> bool:
        return True


class _History:
    def __init__(self) -> None:
        self.read: Path | None = None
        self.written: Path | None = None
        self.length: int | None = None

    def read_history_file(self, path) -> None:
        self.read = Path(path)

    def write_history_file(self, path) -> None:
        self.written = Path(path)
        Path(path).write_text("history\n", encoding="utf-8")

    def set_history_length(self, length: int) -> None:
        self.length = length


def test_tui_operator_commands_render_without_provider_calls(tmp_path: Path) -> None:
    source = _Tty("/dashboard\n/events\n/model\n/cancel\n/exit\n")
    output = _Tty()
    error = io.StringIO()

    code = asyncio.run(
        tui.run_tui(
            OneShotConfig(repo=tmp_path, session_root=tmp_path / "interactive"),
            input_stream=source,
            output_stream=output,
            error_stream=error,
        )
    )

    text = output.getvalue()
    assert code == 0
    assert error.getvalue() == ""
    assert "Cambium interactive session" in text
    assert "events: none" in text
    assert "provider=auto model=auto" in text
    assert "press Ctrl-C while a turn is running" in text
    assert "┌ Cambium" in text


def test_tui_history_is_private_and_bounded(monkeypatch, tmp_path: Path) -> None:
    history = _History()
    monkeypatch.setattr(tui, "_readline", history)
    path = tmp_path / ".cambium" / "tui_history"
    path.parent.mkdir(parents=True)
    path.write_text("old\n", encoding="utf-8")

    tui._load_history(path)
    tui._save_history(path)

    assert history.read == path
    assert history.written == path
    assert history.length == 1000
    assert os.stat(path).st_mode & 0o777 == 0o600


def test_help_documents_turn_cancellation() -> None:
    assert "Ctrl-C cancels" in tui._HELP
    assert "/dashboard" in tui._HELP
    assert "/events" in tui._HELP
    assert "blank line submits" in tui._HELP


def test_tty_bracketed_paste_preserves_embedded_newlines() -> None:
    source = _Tty("\x1b[200~line one\nline two\x1b[201~\n")
    output = _Tty()

    assert tui._read_prompt(source, output) == "line one\nline two"
    assert tui._BRACKETED_PASTE_ENABLE in output.getvalue()
    assert tui._BRACKETED_PASTE_DISABLE in output.getvalue()


def test_tty_trailing_backslash_continues_until_next_line() -> None:
    source = _Tty("line one\\\nline two\n")
    output = _Tty()

    assert tui._read_prompt(source, output) == "line one\nline two"


def test_ctrl_c_cancels_active_turn_and_returns_to_prompt(monkeypatch, tmp_path: Path) -> None:
    from cambium.interactive import InteractiveSession

    class _Turn:
        def __init__(self, number: int, session_dir: Path) -> None:
            self.number = number
            self.session_dir = session_dir

    def prepare_turn(self, _prompt):
        self._turn += 1
        session_dir = self.root / f"turn-{self._turn:04d}"
        session_dir.mkdir(parents=True)
        return _Turn(self._turn, session_dir)

    async def fake_run(self, _turn, *, on_event=None):
        del on_event
        await asyncio.Event().wait()

    def complete_turn(self, _turn, *, succeeded):
        assert succeeded is False

    monkeypatch.setattr(InteractiveSession, "prepare_turn", prepare_turn, raising=False)
    monkeypatch.setattr(InteractiveSession, "run_turn", fake_run, raising=False)
    monkeypatch.setattr(InteractiveSession, "complete_turn", complete_turn, raising=False)

    source = _Tty("work\n/exit\n")
    output = _Tty()

    async def scenario() -> int:
        loop = asyncio.get_running_loop()
        monkeypatch.setattr(
            loop,
            "add_signal_handler",
            lambda _signal, callback: loop.call_soon(callback),
        )
        monkeypatch.setattr(loop, "remove_signal_handler", lambda _signal: True)
        return await tui.run_tui(
            OneShotConfig(repo=tmp_path, session_root=tmp_path / "interactive"),
            input_stream=source,
            output_stream=output,
            error_stream=io.StringIO(),
        )

    assert asyncio.run(scenario()) == 0
    assert "turn cancelled" in output.getvalue()
