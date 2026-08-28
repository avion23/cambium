"""Process-boundary regressions for the live TUI cockpit."""

from __future__ import annotations

import errno
import fcntl
import http.server
import json
import os
import pty
import select
import signal
import struct
import subprocess
import sys
import termios
import threading
import time
from pathlib import Path

import pytest

pytestmark = pytest.mark.slow

REPO_ROOT = Path(__file__).resolve().parents[2]
SRC_DIR = REPO_ROOT / "src"
_PROMPT_REPAINT = b"\x1b[1A\r\r\x1b[2K\xe2\x80\xba "


class _CannedOpenAIServer:
    def __init__(self) -> None:
        self.request_started = threading.Event()
        self.release = threading.Event()
        response = {
            "id": "chatcmpl-pty",
            "object": "chat.completion",
            "model": "pty-model",
            "choices": [
                {
                    "index": 0,
                    "message": {
                        "role": "assistant",
                        "content": json.dumps(
                            {"type": "finish", "summary": "canned response", "objective_met": True}
                        ),
                    },
                    "finish_reason": "stop",
                }
            ],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
        }
        encoded_response = json.dumps(response).encode("utf-8")
        started = self.request_started
        release = self.release

        class Handler(http.server.BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.0"

            def do_POST(self) -> None:  # noqa: N802 (http.server API)
                if self.path != "/chat/completions":
                    self.send_error(404)
                    return
                length = int(self.headers.get("Content-Length") or 0)
                self.rfile.read(length)
                started.set()
                release.wait(5.0)
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(encoded_response)))
                self.send_header("Connection", "close")
                self.end_headers()
                self.wfile.write(encoded_response)

            def log_message(self, format: str, *_args: object) -> None:
                del format

        self._httpd = http.server.HTTPServer(("127.0.0.1", 0), Handler)
        self._thread = threading.Thread(
            target=self._httpd.serve_forever,
            kwargs={"poll_interval": 0.01},
            daemon=True,
        )
        self._thread.start()

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self._httpd.server_port}"

    def close(self) -> None:
        self.release.set()
        self._httpd.shutdown()
        self._httpd.server_close()
        self._thread.join(timeout=2.0)


def _set_size(fd: int, columns: int, rows: int = 30) -> None:
    fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack("HHHH", rows, columns, 0, 0))


def _init_repo(path: Path) -> None:
    path.mkdir()
    subprocess.run(["git", "init", "-b", "main", str(path)], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(path), "config", "user.name", "pty-test"], check=True)
    subprocess.run(
        ["git", "-C", str(path), "config", "user.email", "pty-test@example.test"], check=True
    )
    (path / "README.md").write_text("pty fixture\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(path), "add", "README.md"], check=True, capture_output=True)
    subprocess.run(
        ["git", "-C", str(path), "commit", "-m", "initial"], check=True, capture_output=True
    )


def _provider_file(path: Path, base_url: str) -> Path:
    path.write_text(
        json.dumps(
            {
                "providers": [
                    {
                        "name": "pty-provider",
                        "tier": "fast",
                        "base_url": base_url,
                        "api_key_env": "CAMBIUM_PROVIDER_PTY_PROVIDER_API_KEY",
                        "api_key": "pty-secret",
                        "timeout_s": 2.0,
                        "max_retries": 0,
                        "rpm": 120,
                        "enabled": True,
                        "model": "pty-model",
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    return path


def _spawn_tui(repo: Path, provider_file: Path) -> tuple[subprocess.Popen[bytes], int]:
    env = os.environ.copy()
    env["PYTHONPATH"] = os.pathsep.join(filter(None, [str(SRC_DIR), env.get("PYTHONPATH")]))
    env.update(
        {
            "CAMBIUM_PROVIDERS": str(provider_file),
            "NO_PROXY": "127.0.0.1,localhost",
            "no_proxy": "127.0.0.1,localhost",
        }
    )
    master_fd, slave_fd = pty.openpty()
    _set_size(slave_fd, 110)

    def _claim_tty() -> None:
        fcntl.ioctl(0, termios.TIOCSCTTY, 0)

    try:
        process = subprocess.Popen(
            [sys.executable, "-m", "cambium", "tui", "--repo", str(repo)],
            stdin=slave_fd,
            stdout=slave_fd,
            stderr=slave_fd,
            cwd=repo,
            env=env,
            start_new_session=True,
            preexec_fn=_claim_tty,
        )
    finally:
        os.close(slave_fd)
    _set_size(master_fd, 110)
    return process, master_fd


def _read_into(fd: int, output: bytearray, timeout: float) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        remaining = deadline - time.monotonic()
        ready, _, _ = select.select([fd], [], [], min(0.05, remaining))
        if not ready:
            continue
        try:
            output.extend(os.read(fd, 65536))
        except OSError as exc:
            if exc.errno != errno.EIO:
                raise
            return


def _read_until(fd: int, output: bytearray, marker: bytes, timeout: float) -> None:
    deadline = time.monotonic() + timeout
    while marker not in output and time.monotonic() < deadline:
        _read_into(fd, output, min(0.1, deadline - time.monotonic()))
    assert marker in output, output[-1000:].decode("utf-8", "replace")


def _wait_exit(process: subprocess.Popen[bytes], fd: int, output: bytearray, timeout: float) -> int:
    deadline = time.monotonic() + timeout
    while process.poll() is None and time.monotonic() < deadline:
        _read_into(fd, output, 0.05)
    if process.poll() is None:
        raise AssertionError("TUI did not exit before the deadline")
    _read_into(fd, output, 0.1)
    return process.returncode


def _kill_child(process: subprocess.Popen[bytes]) -> None:
    if process.poll() is not None:
        return
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        return
    process.wait()


def test_live_tui_resize_repaints_one_input_prompt(tmp_path: Path) -> None:
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

        os.write(master_fd, b"hello\n")
        assert server.request_started.wait(5.0)
        _read_into(master_fd, output, 0.2)

        _set_size(master_fd, 90)
        _read_into(master_fd, output, 0.6)
        _set_size(master_fd, 70)
        _read_into(master_fd, output, 0.6)
        server.release.set()

        _read_until(master_fd, output, b"canned response", 8.0)
        _read_into(master_fd, output, 0.3)
        assert output.count(_PROMPT_REPAINT) >= 4
        final_frame = output.rsplit(b"\x1b[1A\r", 1)[-1]
        assert final_frame.count(b"\r\x1b[2K\xe2\x80\xba ") == 1
    finally:
        server.close()
        if master_fd >= 0:
            if process is not None:
                _kill_child(process)
            os.close(master_fd)


def test_idle_ctrl_c_exits_cleanly_within_three_seconds(tmp_path: Path) -> None:
    process = None
    master_fd = -1
    output = bytearray()
    try:
        repo = tmp_path / "repo"
        _init_repo(repo)
        providers = tmp_path / "missing-providers.json"
        process, master_fd = _spawn_tui(repo, providers)
        _read_until(master_fd, output, _PROMPT_REPAINT, 5.0)

        started = time.monotonic()
        os.write(master_fd, b"\x03")
        assert process is not None
        assert _wait_exit(process, master_fd, output, 3.0) == 0
        assert time.monotonic() - started < 3.0
    finally:
        if process is not None:
            _kill_child(process)
        if master_fd >= 0:
            os.close(master_fd)
