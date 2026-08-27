"""Focused shutdown regressions for the interactive input reader."""

from __future__ import annotations

import asyncio
import io
import threading
import time
from pathlib import Path

import pytest

from cambium import tui
from cambium.oneshot import OneShotConfig


class _Tty(io.StringIO):
    def isatty(self) -> bool:
        return True


def test_idle_cancel_does_not_wait_for_a_blocked_input_reader(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    started = threading.Event()
    release = threading.Event()

    def blocked_reader(*_args, **_kwargs):
        started.set()
        release.wait()
        return None

    monkeypatch.setattr(tui, "_read_cockpit_prompt", blocked_reader)

    async def scenario() -> None:
        task = asyncio.create_task(
            tui.run_tui(
                OneShotConfig(repo=tmp_path, session_root=tmp_path / "interactive"),
                input_stream=_Tty(),
                output_stream=_Tty(),
                error_stream=io.StringIO(),
            )
        )
        while not started.is_set():
            await asyncio.sleep(0.01)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, timeout=1.0)

    started_at = time.monotonic()
    try:
        asyncio.run(scenario())
    finally:
        release.set()

    assert time.monotonic() - started_at < 2.0
