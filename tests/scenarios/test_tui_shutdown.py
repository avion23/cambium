"""Focused shutdown regressions for the interactive input reader."""

from __future__ import annotations

import asyncio
import io
import signal
import threading
import time
from pathlib import Path

import pytest

from cambium import tui
from cambium.oneshot import OneShotConfig
from cambium.tui_screen import LinearTimeline


class _Tty(io.StringIO):
    def isatty(self) -> bool:
        return True


def test_idle_cancel_does_not_wait_for_a_blocked_input_reader(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("TERM", "xterm-256color")
    started = threading.Event()
    release = threading.Event()

    def blocked_reader(*_args, **_kwargs):
        started.set()
        release.wait()
        return None

    monkeypatch.setattr(tui, "_read_timeline_prompt", blocked_reader)

    async def scenario() -> None:
        task = asyncio.create_task(
            tui.run_tui(
                OneShotConfig(repo=tmp_path, session_root=tmp_path / "interactive"),
                input_stream=_Tty(),
                output_stream=_Tty(),
                error_stream=io.StringIO(),
            )
        )
        deadline = asyncio.get_running_loop().time() + 1.0
        while not started.is_set() and asyncio.get_running_loop().time() < deadline:
            await asyncio.sleep(0.01)
        assert started.is_set()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, timeout=1.0)

    started_at = time.monotonic()
    try:
        asyncio.run(scenario())
    finally:
        release.set()

    assert time.monotonic() - started_at < 2.0


def test_active_frontend_cancel_waits_for_turn_cleanup(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("TERM", "xterm-256color")

    async def scenario() -> None:
        started = asyncio.Event()
        stopped = asyncio.Event()

        async def run_turn(self, turn, **kwargs):
            started.set()
            try:
                await asyncio.Event().wait()
            finally:
                await asyncio.sleep(0)
                stopped.set()

        monkeypatch.setattr(tui.InteractiveSession, "run_turn", run_turn)
        task = asyncio.create_task(
            tui.run_tui(
                OneShotConfig(repo=tmp_path, session_root=tmp_path / "interactive"),
                input_stream=_Tty("work\n"),
                output_stream=_Tty(),
                error_stream=io.StringIO(),
            )
        )
        await asyncio.wait_for(started.wait(), timeout=2)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, timeout=2)
        assert stopped.is_set(), "frontend returned while its turn was still running"

    asyncio.run(scenario())


def test_timeline_restores_sigterm_after_broken_pipe(monkeypatch) -> None:
    monkeypatch.setenv("TERM", "xterm-256color")

    class BrokenOutput(_Tty):
        broken = False

        def write(self, text):
            if self.broken:
                raise BrokenPipeError
            return super().write(text)

    previous = signal.getsignal(signal.SIGTERM)
    output = BrokenOutput()
    timeline = LinearTimeline(output)
    try:
        with pytest.raises(BrokenPipeError), timeline:
            timeline.draw(
                tui.ObservabilityState().snapshot(),
                tui.Transcript(),
                session_description="session",
                branch_line="",
                cumulative_line="",
                force=True,
            )
            output.broken = True
        assert signal.getsignal(signal.SIGTERM) == previous
    finally:
        signal.signal(signal.SIGTERM, previous)
