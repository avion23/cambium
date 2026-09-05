"""Opt-in real-provider CLI and PTY exercises, using disposable repositories."""

from __future__ import annotations

import fcntl
import json
import os
import pty
import select
import struct
import subprocess
import sys
import termios
import time
from pathlib import Path

import pytest
from test_live_coding_gate import _pick_provider, _scratch_repo

from cambium.store import read_events_file

pytestmark = [pytest.mark.slow, pytest.mark.acceptance]


@pytest.fixture
def live_frontend(tmp_path: Path):
    chosen = _pick_provider()
    if chosen is None:
        pytest.skip("no configured API-key provider")
    provider, key = chosen
    repo, _ = _scratch_repo(tmp_path)
    config = tmp_path / "providers.json"
    config.write_text(
        json.dumps(
            {
                "providers": [
                    {
                        "name": provider.name,
                        "tier": provider.tier.value,
                        "base_url": provider.base_url,
                        "model": provider.model,
                        "auth": "api_key",
                        "protocol": "chat_completions",
                        "api_key": key,
                    }
                ]
            }
        )
    )
    config.chmod(0o600)
    env = dict(os.environ, CAMBIUM_PROVIDERS=str(config))
    env["PYTHONPATH"] = str(Path(__file__).resolve().parents[2] / "src")
    session = tmp_path / "session"
    arguments = [
        "--repo",
        str(repo),
        "--session-dir",
        str(session),
        "--provider",
        provider.name,
        "--max-turns",
        "8",
        "--max-wall-s",
        "90",
        "--max-restarts",
        "0",
    ]
    try:
        yield repo, session, env, arguments
    finally:
        config.unlink(missing_ok=True)


_CODING_TASK = (
    "Use repo_query to locate add in calc.py. Add the docstring "
    "'Return the sum of two numbers.' without changing behavior or any other file. "
    "Verify with a Python assertion that add(2, 3) == 5. Finish when verified."
)


def _published_code(repo: Path) -> str:
    return subprocess.check_output(["git", "show", "main:calc.py"], cwd=repo, text=True)


def _events(session: Path) -> list[dict]:
    return [event for store in session.rglob("events.db") for event in read_events_file(store)]


def test_live_cli_uses_navigation_and_publishes_code(live_frontend) -> None:
    repo, session, env, arguments = live_frontend
    result = subprocess.run(
        [sys.executable, "-m", "cambium", "run", *arguments, "--json", _CODING_TASK],
        env=env,
        capture_output=True,
        text=True,
        timeout=130,
    )
    assert result.returncode == 0, (result.stderr or result.stdout)[-2000:]
    assert "Return the sum of two numbers." in _published_code(repo)
    assert any(
        row.get("payload", {}).get("tool") == "repo_query" and row["payload"].get("ok") is True
        for row in _events(session)
    )


def test_live_tui_codes_then_reopens_its_tool_evidence(live_frontend, tmp_path: Path) -> None:
    repo, session, env, arguments = live_frontend
    master, slave = pty.openpty()
    fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", 32, 110, 0, 0))
    process = subprocess.Popen(
        [sys.executable, "-m", "cambium", "tui", *arguments],
        env=env,
        stdin=slave,
        stdout=slave,
        stderr=slave,
        start_new_session=True,
    )
    os.close(slave)
    output = bytearray()

    def drain() -> None:
        ready, _, _ = select.select([master], [], [], 0.1)
        if ready:
            try:
                output.extend(os.read(master, 65536))
            except OSError:
                pass  # A PTY returns EIO once the child has closed it.

    def wait_for_turn(count: int) -> None:
        deadline = time.monotonic() + 125
        while len(list(session.glob("turn-*/.cambium/result.json"))) < count:
            drain()
            assert process.poll() is None, output[-2000:].decode("utf-8", "replace")
            assert time.monotonic() < deadline, output[-2000:].decode("utf-8", "replace")
        drain()

    try:
        os.write(master, (_CODING_TASK + "\n").encode())
        wait_for_turn(1)
        assert "Return the sum of two numbers." in _published_code(repo)
        os.write(
            master,
            b"Use branch_history to find the earlier tool call that changed calc.py "
            b"and reopen it by its returned ref. Report the exact edit and verification "
            b"evidence. Do not edit any files.\n",
        )
        wait_for_turn(2)
        os.write(master, b"/usage\n/exit\n")
        deadline = time.monotonic() + 10
        while process.poll() is None and time.monotonic() < deadline:
            drain()
        assert process.wait(timeout=2) == 0, output[-2000:].decode("utf-8", "replace")
        events = _events(session)
        assert any(
            row.get("payload", {}).get("tool") == "branch_history"
            and row["payload"].get("ok") is True
            for row in events
        )
        checkpoint_text = []
        for row in events:
            ref = row.get("payload", {}).get("state_ref")
            if row.get("kind") == "checkpoint" and isinstance(ref, str):
                path = Path(ref)
                if path.is_relative_to(tmp_path) and path.is_file():
                    checkpoint_text.append(path.read_text())
        assert any("assistant_action:" in text for text in checkpoint_text)
    finally:
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
        os.close(master)
        (tmp_path / "tui-output.txt").write_bytes(output)
