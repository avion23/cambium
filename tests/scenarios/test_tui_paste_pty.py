"""PTY regression: a bracketed-paste payload stays one prompt with its newlines.

The fake-stream tests in test_tui_usability.py cover the app-side framing
parser (the non-native ``source.readline()`` path).  These drive the real
native readline path over a real PTY with ``\\x1b[200~`` / ``\\x1b[201~``
framing.  The ``simulate_missing_paste_key`` case reproduces runtimes whose
readline does not bind the paste-begin key (libedit, GNU readline < 8.0) by
remapping it in INPUTRC; without the fix every newline inside the paste
submits its own prompt.
"""

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

# Two-line TOML snippet, framed as a real terminal paste, terminated by Enter.
_PASTE = b'\x1b[200~[tool]\nname = "x"\x1b[201~\r'
_ONE_PROMPT = '<cambium-task>\nTask: [tool]\nname = "x"\n</cambium-task>'
_SWALLOW_INPUTRC = '"\\e[200~": abort\n"\\e[201~": abort\n'


def _submitted_prompts(server: _CannedOpenAIServer) -> set[str]:
    """Distinct user prompts the TUI turned into provider requests."""
    prompts = set()
    for messages in server.requests:
        for message in messages:
            content = message.get("content")
            if message.get("role") == "user" and isinstance(content, str) and "Task: " in content:
                prompts.add(content)
    return prompts


@pytest.mark.parametrize("simulate_missing_paste_key", [True, False])
def test_bracketed_paste_submits_one_prompt_with_newlines(
    tmp_path: Path, monkeypatch, simulate_missing_paste_key: bool
) -> None:
    if simulate_missing_paste_key:
        inputrc = tmp_path / "swallow-paste.inputrc"
        inputrc.write_text(_SWALLOW_INPUTRC, encoding="utf-8")
        monkeypatch.setenv("INPUTRC", str(inputrc))

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

        os.write(master_fd, _PASTE)
        assert server.request_started.wait(5.0)
        server.release.set()
        _read_until(master_fd, output, b"canned response", 8.0)
        # A swallowed framing queues the second paste line as another prompt;
        # give any queued turn time to reach the provider before asserting.
        _read_into(master_fd, output, 2.0)

        assert _submitted_prompts(server) == {_ONE_PROMPT}

        os.write(master_fd, b"/exit\n")
        assert _wait_exit(process, master_fd, output, 3.0) == 0
    finally:
        server.close()
        if process is not None:
            _kill_child(process)
        if master_fd >= 0:
            os.close(master_fd)
