"""Focused contracts for persisted REPL readline history."""

from __future__ import annotations

import asyncio
import io
import re
import stat
from pathlib import Path

import pytest

from cambium import oneshot, repl
from cambium.supervisor import PlanResult, TaskResult

pytest.importorskip("readline")

import readline  # noqa: E402


def _plan_result() -> PlanResult:
    return PlanResult((TaskResult(task_id="oneshot", status="succeeded", exit_code=0),))


def _history_entries(content: str) -> list[str]:
    def unescape(match: re.Match[str]) -> str:
        return chr(int(match.group(1), 8))

    return [
        re.sub(r"\\(\d{3})", unescape, line)
        for line in content.splitlines()
        if line != "_HiStOrY_V2_"
    ]


@pytest.fixture(autouse=True)
def _fake_oneshot(monkeypatch) -> None:
    async def fake_run(config: oneshot.OneShotConfig, on_event=None) -> PlanResult:
        return _plan_result()

    monkeypatch.setattr(oneshot, "run_oneshot", fake_run)


@pytest.fixture(autouse=True)
def _clear_readline_history() -> None:
    readline.clear_history()


def _history_file(repo: Path) -> Path:
    return oneshot.default_session_root(repo) / ".cambium" / "repl_history"


def _tty_stream(text: str, monkeypatch) -> io.StringIO:
    stream = io.StringIO(text)
    monkeypatch.setattr(stream, "isatty", lambda: True)
    return stream


def test_non_tty_input_writes_no_history(monkeypatch, tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    config = oneshot.OneShotConfig(repo=repo)

    assert asyncio.run(repl.run_repl(
        config,
        input_stream=io.StringIO("hello\n/exit\n"),
        output_stream=io.StringIO(),
        error_stream=io.StringIO(),
    )) == 0

    assert not _history_file(repo).exists()


def test_readline_unavailable_writes_no_history(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(repl, "readline", None)
    repo = tmp_path / "repo"
    config = oneshot.OneShotConfig(repo=repo)

    assert asyncio.run(repl.run_repl(
        config,
        input_stream=_tty_stream("hello\n/exit\n", monkeypatch),
        output_stream=io.StringIO(),
        error_stream=io.StringIO(),
    )) == 0

    assert not _history_file(repo).exists()


def test_interactive_repl_saves_private_history_on_exit(
    monkeypatch, tmp_path: Path
) -> None:
    repo = tmp_path / "repo"
    config = oneshot.OneShotConfig(repo=repo)

    assert asyncio.run(repl.run_repl(
        config,
        input_stream=_tty_stream("first prompt\n/exit\n", monkeypatch),
        output_stream=io.StringIO(),
        error_stream=io.StringIO(),
    )) == 0

    history = _history_file(repo)
    assert history.is_file()
    assert stat.S_IMODE(history.stat().st_mode) == 0o600
    assert "first prompt" in _history_entries(history.read_text(encoding="utf-8"))


def test_interactive_repl_loads_history_and_saves_on_eof(
    monkeypatch, tmp_path: Path
) -> None:
    repo = tmp_path / "repo"
    history = _history_file(repo)
    history.parent.mkdir(parents=True, exist_ok=True)
    readline.add_history("prior prompt")
    readline.write_history_file(history)
    readline.clear_history()

    assert asyncio.run(repl.run_repl(
        oneshot.OneShotConfig(repo=repo),
        input_stream=_tty_stream("later prompt\n", monkeypatch),
        output_stream=io.StringIO(),
        error_stream=io.StringIO(),
    )) == 0

    entries = _history_entries(history.read_text(encoding="utf-8"))
    assert "prior prompt" in entries
    assert "later prompt" in entries
