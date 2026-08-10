"""Worker-side provider completion drives the edit and publish path."""

from __future__ import annotations

import asyncio
import copy
import json
import os
import shlex
import subprocess
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any

import pytest

from cambium import worker
from cambium.supervisor import read_events, run_plan, run_session

ROOT = Path(__file__).resolve().parents[2]
WORKER = "cambium.worker"
PROVIDER_KEY = "CAMBIUM_PROVIDER_LOOPBACK_PROVIDER_API_KEY"
PROVIDER_SECRET = "loopback-provider-secret"
STATIC_PREFIX = (
    "You are Cambium's deterministic coding worker.\n"
    "Return exactly one append-marker decision.\n"
    "Change only the requested file."
)

SCRIPTED_COMPLETION: dict[str, Any] = {
    "id": "chatcmpl-worker-provider-test",
    "object": "chat.completion",
    "model": "loopback-model",
    "choices": [
        {
            "index": 0,
            "message": {
                "role": "assistant",
                "content": "append marker line to file target.txt: // provider-alpha",
            },
            "finish_reason": "stop",
        }
    ],
    "usage": {"prompt_tokens": 17, "completion_tokens": 9, "total_tokens": 26},
}
REQUESTS: list[dict[str, Any]] = []
REQUEST_AUTHORIZATION: list[str] = []
REQUEST_LOCK = threading.Lock()


class _FakeOpenAIHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.0"

    def do_POST(self) -> None:  # noqa: N802 (http.server API)
        if self.path != "/chat/completions":
            self.send_error(404)
            return
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length)
        try:
            body = json.loads(raw.decode("utf-8"))
        except json.JSONDecodeError:
            body = {}
        with REQUEST_LOCK:
            REQUESTS.append(body)
            REQUEST_AUTHORIZATION.append(self.headers.get("Authorization", ""))
            response = copy.deepcopy(SCRIPTED_COMPLETION)
        encoded = json.dumps(response).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(encoded)))
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(encoded)

    def log_message(self, *args: object) -> None:
        pass


class _FakeOpenAIServer:
    def __init__(self) -> None:
        self._httpd = HTTPServer(("127.0.0.1", 0), _FakeOpenAIHandler)
        self._thread = threading.Thread(target=self._httpd.serve_forever, daemon=True)
        self._thread.start()

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self._httpd.server_port}"

    def close(self) -> None:
        self._httpd.shutdown()
        self._httpd.server_close()
        self._thread.join()


def _make_repo(repo: Path) -> str:
    repo.mkdir(parents=True)
    subprocess.run(["git", "init", "-b", "main", str(repo)], check=True, capture_output=True)
    subprocess.run(
        ["git", "-C", str(repo), "config", "user.name", "worker-provider-test"], check=True
    )
    subprocess.run(
        ["git", "-C", str(repo), "config", "user.email", "worker-provider@test"], check=True
    )
    subprocess.run(["git", "-C", str(repo), "config", "gc.auto", "0"], check=True)
    (repo / "target.txt").write_text("fixture\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(repo), "add", "-A"], check=True, capture_output=True)
    subprocess.run(
        ["git", "-C", str(repo), "commit", "-m", "initial"], check=True, capture_output=True
    )
    return subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "refs/heads/main"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def _provider_config(path: Path, base_url: str) -> Path:
    path.write_text(
        json.dumps(
            {
                "providers": [
                    {
                        "name": "loopback-provider",
                        "tier": "fast",
                        "base_url": base_url,
                        "api_key_env": PROVIDER_KEY,
                        "timeout_s": 2.0,
                        "max_retries": 0,
                        "rpm": 120,
                        "enabled": True,
                        "model": "loopback-model",
                        "priority": 0,
                        "cooldown_s": 1.0,
                        "price": 0.0,
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    return path


def _task(session_dir: Path, repo: Path, base: str, config_path: Path) -> dict[str, Any]:
    task = {
        "task_id": "worker-provider",
        "task": "Choose the provider completion and append its marker to target.txt.",
        "repo": str(repo),
        "worktree_path": str(session_dir / "worker-wt"),
        "branch": "worker-provider",
        "worker": WORKER,
        "gate": "test \"$(grep -Ec '^// provider-' target.txt)\" -eq 1",
        "base_commit": base,
        "fanout_config": {
            "tier": "fast",
            "model": "loopback-model",
            "call_budget_s": 5.0,
            "pause_timeout_s": 0.1,
        },
        "provider_env_keys": [PROVIDER_KEY],
        "ready_timeout_s": 5.0,
        "gate_timeout_s": 5.0,
        "max_wall_s": 20.0,
    }
    assert config_path.is_absolute()
    assert "target_file" not in task
    assert "marker" not in task
    return task


def _run_case(
    root: Path,
    server: _FakeOpenAIServer,
    config_path: Path,
    completion: str,
    *,
    response_model: str | None = None,
    max_restarts: int | None = None,
) -> tuple[Any, list[dict[str, Any]], str]:
    original_model = SCRIPTED_COMPLETION["model"]
    SCRIPTED_COMPLETION["choices"][0]["message"]["content"] = completion
    if response_model is not None:
        SCRIPTED_COMPLETION["model"] = response_model
    try:
        session_dir = root / "session"
        repo = session_dir / "repo"
        base = _make_repo(repo)
        task = _task(session_dir, repo, base, config_path)
        if max_restarts is not None:
            task["max_restarts"] = max_restarts
        result = asyncio.run(run_plan(session_dir, {"tasks": [task]}))
        events = read_events(session_dir)
        merged = subprocess.run(
            ["git", "-C", str(repo), "show", "refs/heads/main:target.txt"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout
        return result, events, merged
    finally:
        SCRIPTED_COMPLETION["model"] = original_model


def test_worker_provider_completion_drives_one_gated_merge_and_canary(
    tmp_path, monkeypatch
) -> None:
    """The worker, not the parent, parses the completion that selects the edit."""
    with REQUEST_LOCK:
        REQUESTS.clear()
        REQUEST_AUTHORIZATION.clear()
    server = _FakeOpenAIServer()
    try:
        config_path = _provider_config(tmp_path / "providers.json", server.base_url)
        monkeypatch.setenv("CAMBIUM_PROVIDERS", str(config_path.resolve()))
        monkeypatch.setenv(PROVIDER_KEY, PROVIDER_SECRET)
        monkeypatch.setenv("NO_PROXY", "127.0.0.1,localhost")
        monkeypatch.setenv("no_proxy", "127.0.0.1,localhost")
        monkeypatch.setenv(
            "PYTHONPATH",
            os.pathsep.join(filter(None, [str(ROOT / "src"), os.environ.get("PYTHONPATH")])),
        )

        first, first_events, first_text = _run_case(
            tmp_path / "first",
            server,
            config_path,
            "append marker line to file target.txt: // provider-alpha",
        )
        second, second_events, second_text = _run_case(
            tmp_path / "second",
            server,
            config_path,
            "append marker line to file target.txt: // provider-beta",
        )

        assert first.exit_code == second.exit_code == 0
        assert first.results[0].status == second.results[0].status == "succeeded"
        assert first.results[0].gate_exit_code == second.results[0].gate_exit_code == 0
        assert first_text.endswith("// provider-alpha\n")
        assert second_text.endswith("// provider-beta\n")
        assert first_text != second_text

        for events in (first_events, second_events):
            assert len([event for event in events if event["kind"] == "merge_committed"]) == 1
            result_events = [event for event in events if event["kind"] == "result"]
            assert len(result_events) == 1
            metadata = result_events[0]["payload"]["provider_metadata"]
            assert metadata == {
                "provider": "loopback-provider",
                "model": "loopback-model",
                "usage": {"prompt_tokens": 17, "completion_tokens": 9, "total_tokens": 26},
                "latency_s": metadata["latency_s"],
            }
            assert isinstance(metadata["latency_s"], float)

        with REQUEST_LOCK:
            assert len(REQUESTS) == 2
            assert len(REQUEST_AUTHORIZATION) == 2
            assert REQUESTS[0] == REQUESTS[1]
            assert all(value == f"Bearer {PROVIDER_SECRET}" for value in REQUEST_AUTHORIZATION)
            assert all(request["model"] == "loopback-model" for request in REQUESTS)
            assert all(request["messages"][0]["content"] == STATIC_PREFIX for request in REQUESTS)
        event_text = json.dumps(first_events + second_events)
        assert PROVIDER_SECRET not in event_text
        assert STATIC_PREFIX not in event_text
        assert "reasoning" not in event_text.lower()
    finally:
        server.close()


def test_worker_rejects_untrusted_provider_response_model(tmp_path, monkeypatch) -> None:
    with REQUEST_LOCK:
        REQUESTS.clear()
        REQUEST_AUTHORIZATION.clear()
    server = _FakeOpenAIServer()
    sentinel = "provider-key prompt reasoning sentinel"
    try:
        config_path = _provider_config(tmp_path / "providers.json", server.base_url)
        monkeypatch.setenv("CAMBIUM_PROVIDERS", str(config_path.resolve()))
        monkeypatch.setenv(PROVIDER_KEY, PROVIDER_SECRET)
        monkeypatch.setenv("NO_PROXY", "127.0.0.1,localhost")
        monkeypatch.setenv("no_proxy", "127.0.0.1,localhost")
        monkeypatch.setenv(
            "PYTHONPATH",
            os.pathsep.join(filter(None, [str(ROOT / "src"), os.environ.get("PYTHONPATH")])),
        )

        result, events, _merged = _run_case(
            tmp_path / "untrusted-model",
            server,
            config_path,
            "append marker line to file target.txt: // provider-alpha",
            response_model=sentinel,
            max_restarts=0,
        )

        assert result.exit_code != 0
        assert result.results[0].status == "failed"
        result_events = [event for event in events if event["kind"] == "result"]
        assert len(result_events) == 1
        assert "provider_metadata" not in result_events[0]["payload"]
        assert sentinel not in json.dumps(events)
        with REQUEST_LOCK:
            assert len(REQUESTS) == 1
    finally:
        server.close()


def test_run_session_provider_mode_sends_task_to_worker(tmp_path, monkeypatch) -> None:
    with REQUEST_LOCK:
        REQUESTS.clear()
        REQUEST_AUTHORIZATION.clear()
    server = _FakeOpenAIServer()
    try:
        config_path = _provider_config(tmp_path / "providers.json", server.base_url)
        monkeypatch.setenv("CAMBIUM_PROVIDERS", str(config_path.resolve()))
        monkeypatch.setenv(PROVIDER_KEY, PROVIDER_SECRET)
        monkeypatch.setenv("NO_PROXY", "127.0.0.1,localhost")
        monkeypatch.setenv("no_proxy", "127.0.0.1,localhost")
        monkeypatch.setenv(
            "PYTHONPATH",
            os.pathsep.join(filter(None, [str(ROOT / "src"), os.environ.get("PYTHONPATH")])),
        )

        session_dir = tmp_path / "slice-provider"
        repo = session_dir / "repo"
        base = _make_repo(repo)
        spec = _task(session_dir, repo, base, config_path)
        spec["scratch_repo"] = str(repo)
        spec["worker"] = str(ROOT / "src" / "cambium" / "worker.py")
        result = asyncio.run(run_session(session_dir, spec))

        assert result.status == "succeeded"
        assert result.exit_code == 0
        with REQUEST_LOCK:
            assert len(REQUESTS) == 1
            assert REQUESTS[0]["model"] == "loopback-model"
            assert REQUESTS[0]["messages"][1]["content"] == (
                f"task={spec['task']}\nReturn one decision now."
            )
    finally:
        server.close()


def test_run_session_provider_mode_missing_task_fails_before_spawn(tmp_path) -> None:
    session_dir = tmp_path / "missing-task"
    with pytest.raises(ValueError, match="provider mode requires a non-empty task"):
        asyncio.run(
            run_session(
                session_dir,
                {
                    "task_id": "missing-task",
                    "fanout_config": {"tier": "fast", "model": "loopback-model"},
                },
            )
        )
    assert not (session_dir / ".cambium" / "events.jsonl").exists()


def test_worker_git_worktree_hook_does_not_receive_provider_key(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = tmp_path / "repo"
    subprocess.run(["git", "init", "-b", "main", str(repo)], check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=repo, check=True)
    (repo / "tracked.txt").write_text("base\n", encoding="utf-8")
    subprocess.run(["git", "add", "tracked.txt"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-m", "base"], cwd=repo, check=True, capture_output=True)

    record = tmp_path / "hook-environment"
    hook = repo / ".git" / "hooks" / "post-checkout"
    hook.write_text(f"#!/bin/sh\nenv > {shlex.quote(str(record))}\n", encoding="utf-8")
    hook.chmod(0o700)
    provider_name = "CAMBIUM_PROVIDER_OPENAI_API_KEY"
    monkeypatch.setenv(provider_name, "provider-secret")

    worktree = tmp_path / "worktree"
    returncode, _stdout, stderr = worker.git(
        "worktree", "add", "-b", "worker-test", str(worktree), "main", cwd=repo
    )

    assert returncode == 0, stderr
    assert record.exists(), "the post-checkout hook never ran"
    hook_environment = record.read_text(encoding="utf-8").splitlines()
    assert not any(line.startswith(f"{provider_name}=") for line in hook_environment)
