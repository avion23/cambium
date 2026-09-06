"""Real PTY paste framing keeps Unicode and large multiline payloads in one turn."""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from test_tui_live_pty import (
    _PROMPT_REPAINT,
    _CannedOpenAIServer,
    _init_repo,
    _kill_child,
    _provider_file,
    _read_into,
    _read_until,
    _spawn_tui,
    _wait_exit,
)

pytestmark = pytest.mark.slow


def _submitted_prompts(server: _CannedOpenAIServer) -> set[str]:
    """Distinct user prompts the TUI turned into provider requests."""
    prompts = set()
    for request in server.requests:
        for message in request.get("messages", []):
            content = message.get("content")
            if message.get("role") == "user" and isinstance(content, str) and "Task: " in content:
                prompts.add(content)
    return prompts


@pytest.mark.parametrize(
    "text", ['[tool]\nname = "界é"', 'line = "value"\n' * 2048], ids=["unicode", "large"]
)
def test_bracketed_paste_submits_one_prompt_with_newlines(tmp_path: Path, text: str) -> None:
    server = _CannedOpenAIServer()
    process = None
    master_fd = -1
    output = bytearray()
    try:
        repo = tmp_path / "repo"
        _init_repo(repo)
        providers = _provider_file(tmp_path / "providers.json", server.base_url)
        process, master_fd = _spawn_tui(repo, providers)
        _read_until(master_fd, output, _PROMPT_REPAINT, 5.0)

        wire = b"\x1b[200~" + text.replace("\n", "\r\n").encode() + b"\x1b[201~\r"
        while wire:
            sent = os.write(master_fd, wire[:4096])
            wire = wire[sent:]
        assert server.request_started.wait(5.0)
        server.release.set()
        _read_until(master_fd, output, b"canned response", 8.0)
        # A swallowed framing queues the second paste line as another prompt;
        # give any queued turn time to reach the provider before asserting.
        _read_into(master_fd, output, 2.0)

        assert _submitted_prompts(server) == {f"<cambium-task>\nTask: {text}\n</cambium-task>"}

        os.write(master_fd, b"/exit\n")
        assert _wait_exit(process, master_fd, output, 3.0) == 0
    finally:
        server.close()
        if process is not None:
            _kill_child(process)
        if master_fd >= 0:
            os.close(master_fd)
