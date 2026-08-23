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
