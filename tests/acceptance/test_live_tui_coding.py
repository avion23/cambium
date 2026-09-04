"""Opt-in real-provider coding and continuation through a terminal, not a mock UI."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from cambium.terminal import sanitize_terminal_text
from tests.acceptance.test_live_coding_gate import (
    _main_head,
    _pick_provider,
    _scratch_repo,
    _session_events,
)
from tests.scenarios.test_tui_live_pty import (
    _PROMPT_REPAINT,
    _kill_child,
    _read_into,
    _read_until,
    _set_size,
    _spawn_tui,
    _wait_exit,
)

pytestmark = [pytest.mark.acceptance, pytest.mark.slow]


def _wait_turn(root: Path, number: int, process, fd: int, output: bytearray) -> None:
    manifest = root / ".cambium" / "interactive.json"
    deadline = time.monotonic() + 180
    while time.monotonic() < deadline and process.poll() is None:
        _read_into(fd, output, 0.1)
        if manifest.is_file() and json.loads(manifest.read_text())["turn"] >= number:
            return
    pytest.fail(sanitize_terminal_text(output[-4000:].decode("utf-8", "replace")))


def test_live_tui_codes_then_reuses_its_result(tmp_path: Path) -> None:
    resolved = _pick_provider()
    if resolved is None:
        pytest.skip("no configured live-provider credential")
    provider, key = resolved
    repo, base = _scratch_repo(tmp_path)
    root = tmp_path / "session"
    config = tmp_path / "providers.json"
    config.write_text(json.dumps({"providers": [{
        "name": provider.name,
        "tier": provider.tier.value,
        "base_url": provider.base_url,
        "model": provider.model,
        "auth": "api_key",
        "protocol": "chat_completions",
        "api_key": key,
    }]}))
    config.chmod(0o600)
    output = bytearray()
    process, fd = _spawn_tui(
        repo, config, "--session-dir", str(root), "--max-turns", "10", "--max-wall-s", "150"
    )
    try:
        _read_until(fd, output, _PROMPT_REPAINT, 5)
        os.write(
            fd,
            b"Modify calc.py only: add subtract(a, b) returning a - b. "
            b"Verify add(2, 3) == 5, subtract(7, 2) == 5, and subtract(2, 7) == -5 "
            b"with Python assertions. Do not commit.\n",
        )
        _set_size(fd, 90, 24)
        _wait_turn(root, 1, process, fd, output)
        head = _main_head(repo)
        assert head != base
        first = _session_events(root / "turn-0001")
        assert any(e["kind"] == "tool_event" for e in first)
        assert any(e["kind"] == "usage_event" for e in first)
        assert any(e["kind"] == "result" and e["payload"]["status"] == "succeeded" for e in first)

        _set_size(fd, 110, 30)
        os.write(
            fd,
            b"Read calc.py and run Python assertions that subtract(8, 3) == 5 "
            b"and add(8, 3) == 11. This is read-only: change no files.\n",
        )
        _wait_turn(root, 2, process, fd, output)
        second = _session_events(root / "turn-0002")
        assert any(e["kind"] == "result" and e["payload"]["status"] == "succeeded" for e in second)
        assert any(e["kind"] == "tool_event" for e in second)
        assert _main_head(repo) == head, "read-only follow-up must not create an empty commit"

        os.write(fd, b"/usage\n/context\n/agents\n/exit\n")
        assert _wait_exit(process, fd, output, 10) == 0
        assert key.encode() not in output
        assert b"Traceback (most recent call last)" not in output
        (tmp_path / "tui.ansi").write_bytes(output)
        (tmp_path / "tui.txt").write_text(
            sanitize_terminal_text(output.decode("utf-8", "replace")), encoding="utf-8"
        )
        published = subprocess.run(
            ["git", "show", f"{head}:calc.py"], cwd=repo, text=True, capture_output=True, check=True
        ).stdout
        subprocess.run(
            [sys.executable, "-c", published + "\nassert subtract(8, 3) == 5\n"], check=True
        )
    finally:
        _kill_child(process)
        os.close(fd)
