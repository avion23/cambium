"""Real CLI/PTY tasks using normal provider selection and accepted Git artifacts."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from cambium.oneshot import OneShotConfig, _resolve_provider
from cambium.store import read_events_file
from tests.acceptance.test_live_coding_gate import _main_head, _scratch_repo
from tests.scenarios.test_tui_live_pty import (
    _PROMPT_REPAINT,
    _kill_child,
    _read_into,
    _read_until,
    _set_size,
    _spawn_tui,
    _wait_exit,
)

pytestmark = [pytest.mark.slow, pytest.mark.acceptance]


@pytest.fixture
def live_frontend(tmp_path: Path):
    repo, _ = _scratch_repo(tmp_path)
    try:
        resolved, _ = _resolve_provider(OneShotConfig(repo=repo), repo)
    except (FileNotFoundError, ValueError) as exc:
        pytest.skip(f"live provider configuration unavailable: {exc}")
    # Exercise the real credential/fallback path, not a second test-only store
    # pinned to one provider whose quota may already be exhausted.
    config = resolved.provider_config_path
    env = dict(os.environ, CAMBIUM_PROVIDERS=str(config))
    env["PYTHONPATH"] = str(Path(__file__).resolve().parents[2] / "src")
    session = tmp_path / "session"
    arguments = [
        "--repo", str(repo), "--session-dir", str(session),
        "--max-turns", "10", "--max-wall-s", "150", "--max-restarts", "0",
    ]
    return repo, session, env, config, arguments


_CODING_TASK = (
    "Use repo_query to locate add in calc.py. Add the docstring "
    "'Return the sum of two numbers.' without changing behavior or any other file. "
    "Verify with a Python assertion that add(2, 3) == 5. Finish when verified."
)


def _check_code(repo: Path) -> None:
    code = subprocess.check_output(["git", "show", "main:calc.py"], cwd=repo, text=True)
    subprocess.run(
        [sys.executable, "-c", code + "\nassert add(2, 3) == 5\n"
         "assert add.__doc__ == 'Return the sum of two numbers.'\n"],
        check=True,
    )


def _events(session: Path) -> list[dict]:
    return [event for store in session.rglob("events.db") for event in read_events_file(store)]


def _wait_turn(root: Path, number: int, process, fd: int, output: bytearray) -> None:
    manifest = root / ".cambium" / "interactive.json"
    deadline = time.monotonic() + 180
    while process.poll() is None and time.monotonic() < deadline:
        _read_into(fd, output, 0.1)
        if manifest.is_file() and json.loads(manifest.read_text())["turn"] >= number:
            result = root / f"turn-{number:04d}" / ".cambium" / "result.json"
            assert json.loads(result.read_text())["exit_code"] == 0, result.read_text()
            return
    pytest.fail(output[-4000:].decode("utf-8", "replace"))


def test_live_cli_uses_navigation_and_publishes_code(live_frontend) -> None:
    repo, session, env, _config, arguments = live_frontend
    result = subprocess.run(
        [sys.executable, "-m", "cambium", "run", *arguments, "--json", _CODING_TASK],
        env=env, capture_output=True, text=True, timeout=180,
    )
    assert result.returncode == 0, (result.stderr or result.stdout)[-2000:]
    _check_code(repo)
    assert any(
        row.get("payload", {}).get("tool") == "repo_query" and row["payload"].get("ok") is True
        for row in _events(session)
    )


def test_live_tui_codes_then_reopens_its_tool_evidence(live_frontend, tmp_path: Path) -> None:
    repo, session, _env, config, arguments = live_frontend
    process, fd = _spawn_tui(repo, config, *arguments[2:])
    output = bytearray()
    try:
        _read_until(fd, output, _PROMPT_REPAINT, 5)
        os.write(fd, (_CODING_TASK + "\n").encode())
        _set_size(fd, 90, 24)
        _wait_turn(session, 1, process, fd, output)
        _check_code(repo)
        head = _main_head(repo)
        _set_size(fd, 110, 30)
        os.write(
            fd,
            b"Use branch_history to find the earlier tool call that changed calc.py "
            b"and reopen it by its returned ref. Report the exact edit and verification "
            b"evidence. Do not edit any files.\n",
        )
        _wait_turn(session, 2, process, fd, output)
        assert _main_head(repo) == head, "read-only continuation must not create an empty commit"
        os.write(fd, b"/usage\n/context\n/agents\n/exit\n")
        assert _wait_exit(process, fd, output, 10) == 0
        events = _events(session)
        assert any(
            row.get("payload", {}).get("tool") == "branch_history"
            and row["payload"].get("ok") is True for row in events
        )
        assert any(
            "assistant_action:" in path.read_text()
            for path in session.glob("turn-*/.cambium/checkpoints/*/turn-*.json")
        )
        assert b"live rendering disabled" not in output
    finally:
        _kill_child(process)
        os.close(fd)
        (tmp_path / "tui-output.ansi").write_bytes(output)
