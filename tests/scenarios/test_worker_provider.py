"""Worker-side provider-backed agent loop drives the edit and publish path.

The fake OpenAI server returns a scripted sequence of strict JSON actions
(``tool_call`` read_batch -> tool_call edit_file -> finish). The worker runs
its bounded agent loop, emits ``tool_event``/``checkpoint`` IPC, makes exactly
one fenced worker-owned commit, and the supervisor gates and merges it.
"""

from __future__ import annotations

import asyncio
import copy
import json
import os
import shlex
import signal
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any, cast

import pytest

from cambium import worker
from cambium.fencing import write_generation
from cambium.ipc import MAX_LINE_BYTES, read_message
from cambium.supervisor import read_events, run_plan, run_session

ROOT = Path(__file__).resolve().parents[2]
WORKER = "cambium.worker"
PROVIDER_KEY = "CAMBIUM_PROVIDER_LOOPBACK_PROVIDER_API_KEY"
PROVIDER_SECRET = "loopback-provider-secret"
TASK_TEXT = "Append a single marker line starting with '// provider-' to target.txt."

REQUEST_LOCK = threading.Lock()
REQUESTS: list[dict[str, Any]] = []
REQUEST_AUTHORIZATION: list[str] = []
RESPONSES: list[dict[str, Any]] = []
SUMMARY_REQUESTS: list[dict[str, Any]] = []
RESPONSE_DELAY_S = 0.0

DEFAULT_USAGE = {"prompt_tokens": 17, "completion_tokens": 9, "total_tokens": 26}


_SUMMARY_CONTROL_OPEN = "<cambium-summary-control>\n"
_SUMMARY_CONTROL_CLOSE = "\n</cambium-summary-control>"


def _summary_completion(
    body: dict[str, Any], *, default_model: str
) -> dict[str, Any] | None:
    """Return a strict synthetic summary response without consuming actions."""
    messages = body.get("messages")
    if not isinstance(messages, list) or not messages:
        return None
    last = messages[-1]
    content = last.get("content") if isinstance(last, dict) else None
    if not isinstance(content, str) or not content.startswith(_SUMMARY_CONTROL_OPEN):
        return None
    try:
        control = json.loads(
            content.removeprefix(_SUMMARY_CONTROL_OPEN).removesuffix(
                _SUMMARY_CONTROL_CLOSE
            )
        )
    except (json.JSONDecodeError, TypeError, ValueError):
        return None
    required = {
        "sequence",
        "source_sha256",
        "source_message_count",
        "through_turn",
    }
    if not required <= control.keys():
        return None
    summary = {
        "type": "summary_entry",
        "sequence": control["sequence"],
        "source_sha256": control["source_sha256"],
        "source_message_count": control["source_message_count"],
        "through_turn": control["through_turn"],
        "objective": "preserve the current coding objective",
        "outcome": "captured the completed work segment",
        "decisions_added": [],
        "decisions_superseded": [],
        "facts_added": [],
        "facts_invalidated": [],
        "files_and_symbols_changed": [],
        "verification_results": [],
        "relevant_failed_approaches": [],
        "open_items": [],
    }
    model = body.get("model")
    if not isinstance(model, str) or not model:
        model = default_model
    return {
        "id": "chatcmpl-summary-fixture",
        "object": "chat.completion",
        "model": model,
        "choices": [
            {
                "index": 0,
                "message": {
                    "role": "assistant",
                    "content": json.dumps(
                        summary, sort_keys=True, separators=(",", ":")
                    ),
                },
                "finish_reason": "stop",
            }
        ],
        # Keep pre-existing action-usage assertions stable. Dedicated summary
        # tests cover accounting with non-zero usage.
        "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
    }


def _reset_server() -> None:
    global RESPONSE_DELAY_S
    with REQUEST_LOCK:
        REQUESTS.clear()
        REQUEST_AUTHORIZATION.clear()
        RESPONSES.clear()
        SUMMARY_REQUESTS.clear()
        RESPONSE_DELAY_S = 0.0


def _enqueue(
    content: str,
    *,
    model: str | None = None,
    usage: dict[str, Any] | None = None,
) -> None:
    completion = {
        "id": "chatcmpl-worker-agent-test",
        "object": "chat.completion",
        "model": model or "loopback-model",
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": content},
                "finish_reason": "stop",
            }
        ],
        "usage": usage or copy.deepcopy(DEFAULT_USAGE),
    }
    with REQUEST_LOCK:
        RESPONSES.append(completion)


def _error_payload() -> dict[str, Any]:
    return {"error": {"message": "no scripted response", "type": "server_error", "code": 500}}


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
        summary_response = _summary_completion(
            body, default_model="loopback-model"
        )
        with REQUEST_LOCK:
            if summary_response is None:
                REQUESTS.append(body)
                REQUEST_AUTHORIZATION.append(self.headers.get("Authorization", ""))
                response = RESPONSES.pop(0) if RESPONSES else _error_payload()
            else:
                SUMMARY_REQUESTS.append(body)
                response = summary_response
            delay = RESPONSE_DELAY_S
        if delay > 0:
            time.sleep(delay)
        encoded = json.dumps(response).encode("utf-8")
        status = 500 if "error" in response else 200
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(encoded)))
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(encoded)

    def log_message(self, format: str, *args: object) -> None:
        pass


class _FakeOpenAIServer:
    def __init__(self) -> None:
        self._httpd = HTTPServer(("127.0.0.1", 0), _FakeOpenAIHandler)
        self._thread = threading.Thread(
            target=self._httpd.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True
        )
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
    (repo / "notes.txt").write_text("output-sentinel-7x9q\n", encoding="utf-8")
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
    path.parent.mkdir(parents=True, exist_ok=True)
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


def _weighted_provider_config(path: Path) -> Path:
    path.write_text(
        json.dumps(
            {
                "providers": [
                    {
                        "name": name,
                        "tier": "fast",
                        "base_url": "http://127.0.0.1:1",
                        "api_key_env": f"CAMBIUM_PROVIDER_{name.upper()}_API_KEY",
                        "timeout_s": 1.0,
                        "max_retries": 0,
                        "rpm": 120,
                        "enabled": True,
                        "model": "loopback-model",
                        "priority": 0,
                        "cooldown_s": 1.0,
                        "price": 0.0,
                    }
                    for name in ("bad", "good")
                ]
            }
        ),
        encoding="utf-8",
    )
    return path


def _task(session_dir: Path, repo: Path, base: str, config_path: Path) -> dict[str, Any]:
    task = {
        "task_id": "worker-provider",
        "task": TASK_TEXT,
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
        "provider_env_keys": [PROVIDER_KEY, "NO_PROXY", "no_proxy"],
        "ready_timeout_s": 5.0,
        "gate_timeout_s": 5.0,
        "max_wall_s": 20.0,
        "heartbeat_interval_s": 0.05,
    }
    assert config_path.is_absolute()
    assert "target_file" not in task
    assert "marker" not in task
    return task


def _set_provider_env(monkeypatch: pytest.MonkeyPatch, config_path: Path) -> None:
    monkeypatch.setenv("CAMBIUM_PROVIDERS", str(config_path.resolve()))
    monkeypatch.setenv(PROVIDER_KEY, PROVIDER_SECRET)
    monkeypatch.setenv("NO_PROXY", "127.0.0.1,localhost")
    monkeypatch.setenv("no_proxy", "127.0.0.1,localhost")
    monkeypatch.setenv(
        "PYTHONPATH",
        os.pathsep.join(filter(None, [str(ROOT / "src"), os.environ.get("PYTHONPATH")])),
    )


def _worker_env(config_path: Path, session_dir: Path) -> dict[str, str]:
    env = dict(os.environ)
    env["CAMBIUM_PROVIDERS"] = str(config_path.resolve())
    env[PROVIDER_KEY] = PROVIDER_SECRET
    env["NO_PROXY"] = "127.0.0.1,localhost"
    env["no_proxy"] = "127.0.0.1,localhost"
    env["PYTHONPATH"] = os.pathsep.join(
        filter(None, [str(ROOT / "src"), os.environ.get("PYTHONPATH")])
    )
    env["CAMBIUM_SESSION_ID"] = str(session_dir.resolve())
    return env


class _WorkerRunner:
    """Direct worker spawn for bounded agent-loop scenarios."""

    def __init__(self, env: dict[str, str]) -> None:
        self.proc: asyncio.subprocess.Process | None = None
        self.stderr_lines: list[str] = []
        self._stderr_task: asyncio.Task | None = None
        self._env = env

    async def start(self) -> None:
        self.proc = await asyncio.create_subprocess_exec(
            sys.executable, "-S", "-u", "-m", "cambium.worker",
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env={**os.environ, "PYTHONUNBUFFERED": "1", **self._env},
            start_new_session=True,
        )
        self._stderr_task = asyncio.create_task(self._drain_stderr())

    async def _drain_stderr(self) -> None:
        proc = self.proc
        assert proc is not None
        stderr = cast(asyncio.StreamReader, proc.stderr)
        while True:
            raw = await stderr.readline()
            if not raw:
                break
            self.stderr_lines.append(raw.decode("utf-8", "replace").rstrip())

    async def send(self, msg: dict[str, Any]) -> None:
        proc = self.proc
        assert proc is not None
        stdin = cast(asyncio.StreamWriter, proc.stdin)
        stdin.write((json.dumps(msg) + "\n").encode("utf-8"))
        await stdin.drain()

    async def recv(self, timeout: float = 30.0) -> dict[str, Any] | None:
        proc = self.proc
        assert proc is not None
        stdout = cast(asyncio.StreamReader, proc.stdout)
        return await asyncio.wait_for(
            read_message(stdout, limit=MAX_LINE_BYTES), timeout
        )

    async def stop(self) -> None:
        if self.proc is not None and self.proc.returncode is None:
            try:
                os.killpg(self.proc.pid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                pass
            await self.proc.wait()
        if self._stderr_task is not None:
            try:
                await asyncio.wait_for(self._stderr_task, 5.0)
            except (TimeoutError, asyncio.CancelledError):
                pass


async def _drive_worker(
    session_dir: Path,
    repo: Path,
    env: dict[str, str],
    *,
    init: dict[str, Any],
    run: dict[str, Any],
    branch: str,
) -> tuple[dict[str, Any], list[dict[str, Any]], int, list[str]]:
    """Spawn one worker, drive init -> run_task, collect messages to exit_message."""
    worktree = session_dir / "wt"
    generation = int(init.get("generation", 1))
    if not worktree.exists():
        subprocess.run(
            ["git", "-C", str(repo), "worktree", "add", "-b", branch, str(worktree), "main"],
            check=True,
            capture_output=True,
        )
    write_generation(worktree, generation)
    runner = _WorkerRunner(env)
    await runner.start()
    try:
        await runner.send({
            "type": "init", "request_id": "init-1", "task_id": "agent-001",
            "generation": generation, **init,
        })
        ready = await runner.recv()
        assert ready is not None and ready["type"] == "ready", f"stderr={runner.stderr_lines!r}"
        await runner.send({
            "type": "run_task", "request_id": "run-1", "task_id": "agent-001",
            "scratch_repo": str(repo), "worktree_path": str(worktree),
            "branch": branch, "generation": generation, **run,
        })
        messages: list[dict[str, Any]] = []
        while True:
            msg = await runner.recv()
            if msg is None:
                raise AssertionError(f"EOF before exit_message; stderr={runner.stderr_lines!r}")
            messages.append(msg)
            if msg["type"] == "exit_message":
                break
        proc = cast(asyncio.subprocess.Process, runner.proc)
        rc = await proc.wait()
        result = next(m for m in messages if m["type"] == "result_envelope")
        return result, messages, rc, runner.stderr_lines
    finally:
        await runner.stop()


def _agent_init(config_path: Path, **extra: Any) -> dict[str, Any]:
    return {
        "fanout_config": {"tier": "fast", "model": "loopback-model"},
        "heartbeat": {"interval_s": 0.05},
        **extra,
    }


def test_worker_init_debt_snapshot_orders_router_and_missing_debt_is_neutral(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config_path = _weighted_provider_config(tmp_path / "providers.json")
    monkeypatch.setenv("CAMBIUM_PROVIDERS", str(config_path))
    init = {
        "task_id": "weighted-worker",
        "fanout_config": {"tier": "fast", "model": "loopback-model"},
    }

    neutral_config = worker.AgentConfig.from_init(init)
    assert neutral_config.context_reuse is False
    neutral_router, tier, model, _identity = worker._provider_router(
        cast(dict[str, Any], neutral_config.fanout_config),
        debt=neutral_config.debt,
    )
    assert [provider.name for provider in neutral_router._candidates(tier, model)] == [
        "bad",
        "good",
    ]

    last_seen = time.time()
    weighted_config = worker.AgentConfig.from_init(
        {
            **init,
            "debt": {
                "bad": {
                    "requests": 10,
                    "cache_hit_count": 0,
                    "latency_total_s": 300.0,
                    "latency_count": 10,
                    "last_seen": last_seen,
                },
                "good": {
                    "requests": 10,
                    "cache_hit_count": 10,
                    "latency_total_s": 10.0,
                    "latency_count": 10,
                    "last_seen": last_seen,
                },
            },
        }
    )
    weighted_router, tier, model, _identity = worker._provider_router(
        cast(dict[str, Any], weighted_config.fanout_config),
        debt=weighted_config.debt,
    )
    assert [provider.name for provider in weighted_router._candidates(tier, model)] == [
        "good",
        "bad",
    ]


# ---------------------------------------------------------------------------
# Agent-loop happy path through the full supervisor (run_plan)
# ---------------------------------------------------------------------------


@pytest.mark.slow
def test_worker_agent_loop_read_edit_finish_one_fenced_commit(
    tmp_path, monkeypatch
) -> None:
    """read_batch -> edit_file -> run_shell -> finish: 4 model calls, one merge, no leaks.

    The provider config is supplied through ``CAMBIUM_PROVIDERS`` so this
    scenario stays isolated from the user's standard config path.
    """
    _reset_server()
    server = _FakeOpenAIServer()
    try:
        project = tmp_path / "project"
        project.mkdir()
        config_path = _provider_config(
            project / "providers.json", server.base_url
        )
        monkeypatch.chdir(project)
        monkeypatch.setenv("CAMBIUM_PROVIDERS", str(config_path))
        monkeypatch.setenv(PROVIDER_KEY, PROVIDER_SECRET)
        monkeypatch.setenv("NO_PROXY", "127.0.0.1,localhost")
        monkeypatch.setenv("no_proxy", "127.0.0.1,localhost")
        monkeypatch.setenv(
            "PYTHONPATH",
            os.pathsep.join(filter(None, [str(ROOT / "src"), os.environ.get("PYTHONPATH")])),
        )
        _enqueue(
            '{"type":"tool_call","name":"read_batch","arguments":'
            '{"paths":["notes.txt"]}}',
            usage={"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
        )
        _enqueue(
            '{"type":"tool_call","name":"edit_file","arguments":'
            '{"path":"target.txt","old_string":"fixture\\n",'
            '"new_string":"fixture\\n// provider-alpha\\n"}}',
            usage={"prompt_tokens": 12, "completion_tokens": 6, "total_tokens": 18},
        )
        _enqueue(
            '{"type":"tool_call","name":"run_shell","arguments":'
            '{"cmd":["true"]}}'
        )
        _enqueue(
            '{"type":"finish","summary":"read target.txt and appended a provider marker"}',
            usage={"prompt_tokens": 14, "completion_tokens": 7, "total_tokens": 21},
        )

        session_dir = tmp_path / "session"
        repo = session_dir / "repo"
        base = _make_repo(repo)
        task = _task(session_dir, repo, base, config_path)
        result = asyncio.run(
            run_plan(
                session_dir, {"tasks": [task]},
                routing_state_path=str(tmp_path / "routing-state.json"),
            )
        )
        events = read_events(session_dir)
        merged = subprocess.run(
            ["git", "-C", str(repo), "show", "refs/heads/main:target.txt"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout

        assert result.exit_code == 0
        assert result.results[0].status == "succeeded"
        assert merged.endswith("// provider-alpha\n")

        with REQUEST_LOCK:
            assert len(REQUESTS) == 4
            assert all(value == f"Bearer {PROVIDER_SECRET}" for value in REQUEST_AUTHORIZATION)
            assert all(request["model"] == "loopback-model" for request in REQUESTS)

        tool_events = [e for e in events if e["kind"] == "tool_event"]
        assert [e["payload"]["tool"] for e in tool_events] == [
            "read_batch", "edit_file", "run_shell"
        ]
        assert all(e["payload"]["ok"] is True for e in tool_events)
        assert [e["payload"]["turn"] for e in tool_events] == [1, 2, 3]
        assert all(isinstance(e["payload"]["duration_ms"], int) for e in tool_events)

        checkpoints = [e for e in events if e["kind"] == "checkpoint"]
        assert [e["payload"]["turn"] for e in checkpoints] == [1, 2, 3, 4]
        assert checkpoints[0]["payload"]["commits_so_far"] == []
        assert checkpoints[1]["payload"]["commits_so_far"] == []
        assert checkpoints[2]["payload"]["commits_so_far"] == []
        final_commits = checkpoints[3]["payload"]["commits_so_far"]
        assert len(final_commits) == 1
        final_sha = final_commits[0]
        assert (
            subprocess.run(
                ["git", "-C", str(repo), "cat-file", "-e", f"{final_sha}^{{commit}}"],
                check=False,
            ).returncode
            == 0
        )
        for checkpoint in checkpoints:
            state_ref = Path(checkpoint["payload"]["state_ref"])
            assert state_ref.exists()
            payload = json.loads(state_ref.read_text(encoding="utf-8"))
            assert payload["schema"] == 1
            assert payload["turn"] == checkpoint["payload"]["turn"]
            assert isinstance(payload["transcript"], list)

        result_events = [e for e in events if e["kind"] == "result"]
        assert len(result_events) == 1
        metadata = result_events[0]["payload"]["provider_metadata"]
        assert metadata == {
            "provider": "loopback-provider",
            "model": "loopback-model",
            "usage": {
                "prompt_tokens": 53,
                "completion_tokens": 27,
                "total_tokens": 80,
            },
            "latency_s": metadata["latency_s"],
        }
        assert isinstance(metadata["latency_s"], float)
        assert len([e for e in events if e["kind"] == "merge_committed"]) == 1

        event_text = json.dumps(events)
        assert PROVIDER_SECRET not in event_text
        assert "You are Cambium's autonomous coding agent." not in event_text
        # read_batch output must never reach the durable event log
        assert "output-sentinel-7x9q" not in event_text
    finally:
        server.close()


@pytest.mark.slow
def test_provider_no_change_succeeds_without_merge_and_preserves_session_result(
    tmp_path, monkeypatch
) -> None:
    _reset_server()
    server = _FakeOpenAIServer()
    try:
        config_path = _provider_config(tmp_path / "providers.json", server.base_url)
        _set_provider_env(monkeypatch, config_path)
        summary = "reviewed the repository and confirmed it already satisfies the request"
        _enqueue(json.dumps({"type": "finish", "summary": summary}))

        session_dir = tmp_path / "session"
        repo = session_dir / "repo"
        base = _make_repo(repo)
        task = _task(session_dir, repo, base, config_path)
        result = asyncio.run(
            run_plan(
                session_dir, {"tasks": [task]},
                routing_state_path=str(tmp_path / "routing-state.json"),
            )
        )
        events = read_events(session_dir)
        root_result = json.loads(
            (session_dir / ".cambium" / "result.json").read_text(encoding="utf-8")
        )

        assert result.exit_code == 0
        assert result.results[0].status == "succeeded"
        assert result.results[0].merge_sha is None
        assert result.results[0].summary == summary
        assert subprocess.run(
            ["git", "-C", str(repo), "rev-parse", "refs/heads/main"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip() == base
        assert not [event for event in events if event["kind"] == "merge_started"]
        assert not [event for event in events if event["kind"] == "merge_committed"]
        assert events[-1]["kind"] == "session_ended"
        assert root_result["status"] == "done"
        assert root_result["exit_code"] == 0
        assert root_result["commits"] == []
        assert root_result["files_changed"] == []
        assert root_result["unified_diff"] == ""
        assert root_result["summary"] == summary
        # Default context reuse persists one redacted immutable terminal epoch.
        # The summary stays in the envelope and no empty commit is created.
        assert not [event for event in events if event["kind"] == "checkpoint"]
        context_events = [event for event in events if event["kind"] == "context_checkpoint"]
        assert len(context_events) == 1
        checkpoint_ref = context_events[0]["payload"]["checkpoint_ref"]
        assert (session_dir / ".cambium" / "checkpoints" / checkpoint_ref).is_file()
    finally:
        server.close()



@pytest.mark.slow
def test_worker_advanced_head_no_change_fails_and_main_unchanged(
    tmp_path, monkeypatch
) -> None:
    """A provider that commits directly (permitted shell) must never succeed
    as a no-op: the worktree looks clean but HEAD has advanced beyond the
    base commit, so the worker fails and the supervisor never merges."""
    _reset_server()
    server = _FakeOpenAIServer()
    try:
        config_path = _provider_config(tmp_path / "providers.json", server.base_url)
        _set_provider_env(monkeypatch, config_path)
        _enqueue(
            '{"type":"tool_call","name":"run_shell","arguments":{"cmd":'
            '["git","commit","--allow-empty","-m","unfenced-provider-commit"]}}'
        )
        _enqueue('{"type":"finish","summary":"committed directly"}')

        session_dir = tmp_path / "session"
        repo = session_dir / "repo"
        base = _make_repo(repo)
        task = _task(session_dir, repo, base, config_path)
        task["max_restarts"] = 0
        result = asyncio.run(
            run_plan(
                session_dir, {"tasks": [task]},
                routing_state_path=str(tmp_path / "routing-state.json"),
            )
        )
        events = read_events(session_dir)

        assert result.exit_code != 0
        assert result.results[0].status == "failed"
        reason = cast(str, result.results[0].reason)
        assert "advanced beyond base_commit" in reason
        assert result.results[0].merge_sha is None
        assert subprocess.run(
            ["git", "-C", str(repo), "rev-parse", "refs/heads/main"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip() == base
        assert not [event for event in events if event["kind"] == "merge_started"]
        assert not [event for event in events if event["kind"] == "merge_committed"]
    finally:
        server.close()


@pytest.mark.slow
def test_worker_dirty_after_unfenced_provider_commit_fails_and_main_unchanged(
    tmp_path, monkeypatch
) -> None:
    """A provider that commits directly (permitted shell) and then edits
    another file must never reach the fenced-commit path: the worktree is
    dirty, but HEAD has advanced beyond the base commit, so the worker fails
    with the invariant reason and the supervisor never merges either commit."""
    _reset_server()
    server = _FakeOpenAIServer()
    try:
        config_path = _provider_config(tmp_path / "providers.json", server.base_url)
        _set_provider_env(monkeypatch, config_path)
        _enqueue(
            '{"type":"tool_call","name":"edit_file","arguments":'
            '{"path":"target.txt","old_string":"fixture\\n",'
            '"new_string":"fixture\\n// provider-1\\n"}}'
        )
        _enqueue(
            '{"type":"tool_call","name":"run_shell","arguments":{"cmd":'
            '["git","add","target.txt"]}}'
        )
        _enqueue(
            '{"type":"tool_call","name":"run_shell","arguments":{"cmd":'
            '["git","commit","-m","unfenced-provider-commit"]}}'
        )
        _enqueue(
            '{"type":"tool_call","name":"edit_file","arguments":'
            '{"path":"notes.txt","old_string":"output-sentinel-7x9q\\n",'
            '"new_string":"output-sentinel-7x9q\\n// dirty-after-commit\\n"}}'
        )
        _enqueue(
            '{"type":"tool_call","name":"run_shell","arguments":'
            '{"cmd":["true"]}}'
        )
        _enqueue('{"type":"finish","summary":"committed directly then edited"}')

        session_dir = tmp_path / "session"
        repo = session_dir / "repo"
        base = _make_repo(repo)
        task = _task(session_dir, repo, base, config_path)
        task["max_restarts"] = 0
        result = asyncio.run(
            run_plan(
                session_dir, {"tasks": [task]},
                routing_state_path=str(tmp_path / "routing-state.json"),
            )
        )
        events = read_events(session_dir)

        assert result.exit_code != 0
        assert result.results[0].status == "failed"
        reason = cast(str, result.results[0].reason)
        assert "advanced beyond base_commit" in reason
        assert "refusing to publish unverified changes" in reason
        assert result.results[0].merge_sha is None
        assert subprocess.run(
            ["git", "-C", str(repo), "rev-parse", "refs/heads/main"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip() == base
        assert not [event for event in events if event["kind"] == "merge_started"]
        assert not [event for event in events if event["kind"] == "merge_committed"]
    finally:
        server.close()


@pytest.mark.slow
def test_worker_delegate_tool_proposes_and_admits_child(tmp_path, monkeypatch) -> None:
    """A model ``delegate`` tool call admits and runs a child through the real worker loop.

    The root's provider-backed agent loop calls the ``delegate`` tool with a
    valid child task spec; the real worker emits the ``propose_child`` wire
    message from the agent loop (not from a plan-declared proposal), the
    supervisor buffers it and validates the revision at the root's terminal
    envelope, durably admits the child (``child_admitted``), and the child
    runs on the deterministic marker path (``spawned`` for the child id).
    """
    _reset_server()
    server = _FakeOpenAIServer()
    try:
        project = tmp_path / "project"
        project.mkdir()
        config_path = _provider_config(
            project / "providers.json", server.base_url
        )
        monkeypatch.chdir(project)
        monkeypatch.setenv("CAMBIUM_PROVIDERS", str(config_path))
        monkeypatch.setenv(PROVIDER_KEY, PROVIDER_SECRET)
        monkeypatch.setenv("NO_PROXY", "127.0.0.1,localhost")
        monkeypatch.setenv("no_proxy", "127.0.0.1,localhost")
        monkeypatch.setenv(
            "PYTHONPATH",
            os.pathsep.join(filter(None, [str(ROOT / "src"), os.environ.get("PYTHONPATH")])),
        )

        session_dir = tmp_path / "session"
        repo = session_dir / "repo"
        base = _make_repo(repo)
        delegate_args = {
            "child_task_id": "worker-provider-child",
            "kind": "test",
            "spec": {
                "task": "append a child marker to notes.txt",
                "repo": str(repo),
                "worktree_path": str(session_dir / "child-wt"),
                "branch": "worker-provider-child",
                "target_file": "notes.txt",
                "marker": "// provider-child",
                "write_marker": True,
                "base_commit": base,
            },
        }
        _enqueue(
            '{"type":"tool_call","name":"edit_file","arguments":'
            '{"path":"target.txt","old_string":"fixture\\n",'
            '"new_string":"fixture\\n// provider-root\\n"}}'
        )
        _enqueue(
            '{"type":"tool_call","name":"delegate","arguments":'
            + json.dumps(delegate_args)
            + "}"
        )
        _enqueue(
            '{"type":"tool_call","name":"run_shell","arguments":'
            '{"cmd":["true"]}}'
        )
        _enqueue('{"type":"finish","summary":"edited target.txt and delegated a child"}')

        task = _task(session_dir, repo, base, config_path)
        result = asyncio.run(
            run_plan(
                session_dir, {"tasks": [task]},
                routing_state_path=str(tmp_path / "routing-state.json"),
            )
        )
        events = read_events(session_dir)

        assert result.exit_code == 0
        assert {r.task_id for r in result.results} == {
            "worker-provider", "worker-provider-child"
        }
        assert all(r.status == "succeeded" for r in result.results)

        admitted = [e for e in events if e["kind"] == "child_admitted"]
        assert len(admitted) == 1
        assert admitted[0]["payload"]["parent_task_id"] == "worker-provider"
        assert admitted[0]["payload"]["child_task_id"] == "worker-provider-child"
        assert admitted[0]["payload"]["child_kind"] == "test"
        assert not [e for e in events if e["kind"] == "child_rejected"]

        spawned = [e for e in events if e["kind"] == "spawned"]
        spawned_ids = {e["task_id"] for e in spawned}
        assert "worker-provider-child" in spawned_ids

        merged = subprocess.run(
            ["git", "-C", str(repo), "show", "refs/heads/main:notes.txt"],
            check=True, capture_output=True, text=True,
        ).stdout
        assert merged.endswith("// provider-child\n")
        # the child's marker is the agent's own: no provider secret leaked
        assert PROVIDER_SECRET not in json.dumps(events)
    finally:
        server.close()


@pytest.mark.slow
def test_worker_context_reuse_fork_resume_is_byte_exact(tmp_path, monkeypatch) -> None:
    _reset_server()
    server = _FakeOpenAIServer()
    try:
        project = tmp_path / "project"
        project.mkdir()
        config_path = _provider_config(project / "providers.json", server.base_url)
        monkeypatch.chdir(project)
        _set_provider_env(monkeypatch, config_path)

        session_dir = tmp_path / "session"
        repo = session_dir / "repo"
        base = _make_repo(repo)
        child_task_id = "worker-provider-cache-child"
        child_spec = {
            "task": "complete the provider-backed child task",
            "repo": str(repo),
            "worktree_path": str(session_dir / "child-wt"),
            "branch": child_task_id,
            "worker": WORKER,
            "target_file": "notes.txt",
            "marker": "// provider-cache-child",
            "write_marker": True,
            "base_commit": base,
            "provider_env_keys": [PROVIDER_KEY, "NO_PROXY", "no_proxy"],
            "authorized_providers": ["loopback-provider"],
            "fanout_config": {
                "tier": "fast",
                "model": "loopback-model",
                "call_budget_s": 5.0,
                "pause_timeout_s": 0.1,
            },
        }
        delegate_args = {
            "child_task_id": child_task_id,
            "kind": "test",
            "spec": child_spec,
        }
        cache_hit_usage = {
            "prompt_tokens": 17,
            "completion_tokens": 9,
            "total_tokens": 26,
            "prompt_tokens_details": {"cached_tokens": 11},
        }
        _enqueue(
            '{"type":"tool_call","name":"edit_file","arguments":'
            '{"path":"target.txt","old_string":"not-present",'
            '"new_string":"fixture\\n// provider-cache-parent\\n"}}'
        )
        _enqueue(
            '{"type":"tool_call","name":"delegate","arguments":'
            + json.dumps(delegate_args)
            + "}"
        )
        _enqueue(
            '{"type":"finish","summary":"child provider call completed"}',
            usage=cache_hit_usage,
        )
        _enqueue(
            '{"type":"finish","summary":"resumed after child completion"}',
            usage=cache_hit_usage,
        )

        task = _task(session_dir, repo, base, config_path)
        task["authorized_providers"] = ["loopback-provider"]
        checkpoint_snapshots: dict[str, bytes] = {}
        checkpoint_path: Path | None = None

        def observe(event: dict[str, Any]) -> None:
            nonlocal checkpoint_path
            kind = event["kind"]
            # Keep the epoch-1 fork target as the byte-comparison fixture;
            # terminal epoch 2 is a separate immutable checkpoint.
            if kind == "context_checkpoint" and event["payload"]["epoch"] == 1:
                checkpoint_ref = cast(str, event["payload"]["checkpoint_ref"])
                checkpoint_file: Path = (
                    session_dir / ".cambium" / "checkpoints" / task["task_id"]
                    / checkpoint_ref.split("/", 1)[1]
                )
                checkpoint_path = checkpoint_file
                checkpoint_snapshots["at_checkpoint"] = checkpoint_file.read_bytes()
            elif kind == "child_result" and event["task_id"] == child_task_id:
                assert checkpoint_path is not None
                checkpoint_snapshots["after_child"] = checkpoint_path.read_bytes()
            elif kind == "context_resume":
                assert checkpoint_path is not None
                checkpoint_snapshots["after_resume"] = checkpoint_path.read_bytes()

        result = asyncio.run(
            run_plan(
                session_dir,
                {"tasks": [task]},
                on_event=observe,
                routing_state_path=str(tmp_path / "routing-state.json"),
                context_reuse=True,
            )
        )
        events = read_events(session_dir)
        with REQUEST_LOCK:
            requests = copy.deepcopy(REQUESTS)
            summary_requests = copy.deepcopy(SUMMARY_REQUESTS)
            authorizations = list(REQUEST_AUTHORIZATION)

        checkpoints = [event for event in events if event["kind"] == "context_checkpoint"]
        assert len(checkpoints) == 2
        checkpoint_event = checkpoints[0]["payload"]
        assert checkpoint_event["epoch"] == 1
        checkpoint_ref = checkpoint_event["checkpoint_ref"]
        assert isinstance(checkpoint_ref, str) and checkpoint_ref
        assert checkpoint_ref.startswith(f'{task["task_id"]}/')
        assert checkpoint_event["cache_key"]["redacted"] is False

        assert checkpoint_path is not None
        assert checkpoint_path.is_file()
        checkpoint = json.loads(checkpoint_path.read_text(encoding="utf-8"))
        assert checkpoint["epoch"] == 1
        assert checkpoint["task_id"] == task["task_id"]
        assert checkpoint["checkpoint_ref"] == checkpoint_ref
        checkpoint_prefix = checkpoint["provider_messages"] + checkpoint["continuation_suffix"]
        prefix_length = len(checkpoint_prefix)

        assert len(requests) == 4
        assert len(summary_requests) == 2
        assert all(request["model"] == "loopback-model" for request in requests)
        assert all(
            request["model"] == "loopback-model" for request in summary_requests
        )
        assert all(
            str(request["messages"][-1].get("content", "")).startswith(
                "<cambium-summary-control>\n"
            )
            for request in summary_requests
        )
        assert authorizations == [f"Bearer {PROVIDER_SECRET}"] * 4
        child_messages = requests[2]["messages"]
        resumed_messages = requests[3]["messages"]
        assert child_messages[:prefix_length] == checkpoint_prefix
        assert resumed_messages[:prefix_length] == checkpoint_prefix
        assert summary_requests[1]["messages"][:prefix_length] == checkpoint_prefix
        assert len(child_messages) == prefix_length + 1
        assert len(resumed_messages) == prefix_length + 1
        assert set(child_messages[prefix_length]) == {"role", "content"}
        assert child_messages[prefix_length]["role"] == "user"
        assert child_messages[prefix_length]["content"].startswith("Child task: ")
        assert set(resumed_messages[prefix_length]) == {"role", "content"}
        assert resumed_messages[prefix_length]["role"] == "user"
        assert resumed_messages[prefix_length]["content"].startswith("Child task result:\n")

        child_prefix_bytes = json.dumps(
            {"messages": child_messages[:prefix_length], "model": requests[2]["model"]}
        ).encode("utf-8")
        resumed_prefix_bytes = json.dumps(
            {"messages": resumed_messages[:prefix_length], "model": requests[3]["model"]}
        ).encode("utf-8")
        assert child_prefix_bytes == resumed_prefix_bytes

        assert set(checkpoint_snapshots) == {"at_checkpoint", "after_child", "after_resume"}
        checkpoint_bytes = checkpoint_path.read_bytes()
        assert checkpoint_snapshots["after_child"] == checkpoint_snapshots["at_checkpoint"]
        assert checkpoint_snapshots["after_resume"] == checkpoint_bytes
        assert checkpoint_snapshots["after_child"] == checkpoint_snapshots["after_resume"]

        forks = [event for event in events if event["kind"] == "context_fork"]
        assert len(forks) == 1
        fork_payload = forks[0]["payload"]
        assert fork_payload["epoch"] == 1
        assert fork_payload["compatible"] is True
        assert fork_payload["parent_task_id"] == task["task_id"]
        assert fork_payload["child_task_id"] == child_task_id

        resumes = [event for event in events if event["kind"] == "context_resume"]
        assert len(resumes) == 1
        resume_payload = resumes[0]["payload"]
        assert resume_payload["checkpoint_ref"] == checkpoint_ref
        assert resume_payload["epoch"] == 1
        assert resume_payload["child_count"] == 1

        usage_events = [event for event in events if event["kind"] == "usage_event"]
        child_usage = [
            event["payload"] for event in usage_events if event["task_id"] == child_task_id
        ]
        parent_usage = [
            event["payload"] for event in usage_events if event["task_id"] == task["task_id"]
        ]
        assert len(child_usage) == 1
        assert len(parent_usage) == 5
        assert child_usage[0]["epoch"] == 1
        assert child_usage[0]["fork_of"] == checkpoint_ref
        assert child_usage[0]["provider_cache_hit"] is True
        assert child_usage[0]["prompt_prefix_bytes"] == checkpoint["cache_key"]["prefix_bytes"]
        # The resumed action call is followed by the terminal summary call.
        assert parent_usage[-2]["epoch"] == 1
        assert "fork_of" not in parent_usage[-2]
        assert parent_usage[-2]["provider_cache_hit"] is True
        assert parent_usage[-2]["prompt_prefix_bytes"] == checkpoint["cache_key"]["prefix_bytes"]
        assert parent_usage[-1]["epoch"] == 1
        assert "fork_of" not in parent_usage[-1]
        assert all("epoch" not in payload for payload in parent_usage[:3])

        evidence_env = dict(os.environ)
        evidence_env["PYTHONPATH"] = str(ROOT / "src")
        evidence = subprocess.run(
            [
                sys.executable,
                str(ROOT / "scripts" / "context_cache_evidence.py"),
                "--json",
                str(session_dir),
            ],
            check=True,
            capture_output=True,
            text=True,
            env=evidence_env,
        )
        report = json.loads(evidence.stdout)
        buckets = report["providers"]["loopback-provider"]["buckets"]
        assert buckets["baseline"]["calls"] == 3
        assert buckets["fork_first"]["calls"] == 1
        assert buckets["resume_first"]["calls"] == 1
        assert buckets["fork_later"]["calls"] == 0
        assert buckets["resume_later"]["calls"] == 1

        assert result.exit_code == 0
        assert {item.task_id for item in result.results} == {
            task["task_id"], child_task_id
        }
        assert all(item.status == "succeeded" for item in result.results)
        assert PROVIDER_SECRET not in json.dumps(events)
    finally:
        server.close()


@pytest.mark.slow
def test_worker_rejects_untrusted_provider_response_model(tmp_path, monkeypatch) -> None:
    _reset_server()
    server = _FakeOpenAIServer()
    sentinel = "provider-key prompt reasoning sentinel"
    try:
        config_path = _provider_config(tmp_path / "providers.json", server.base_url)
        _set_provider_env(monkeypatch, config_path)
        _enqueue('{"type":"finish","summary":"done"}', model=sentinel)

        session_dir = tmp_path / "untrusted-model"
        repo = session_dir / "repo"
        base = _make_repo(repo)
        task = _task(session_dir, repo, base, config_path)
        task["max_restarts"] = 0
        result = asyncio.run(
            run_plan(
                session_dir, {"tasks": [task]},
                routing_state_path=str(tmp_path / "routing-state.json"),
            )
        )
        events = read_events(session_dir)

        assert result.exit_code != 0
        assert result.results[0].status == "failed"
        result_events = [e for e in events if e["kind"] == "result"]
        assert len(result_events) == 1
        assert "provider_metadata" not in result_events[0]["payload"]
        assert sentinel not in json.dumps(events)
        with REQUEST_LOCK:
            assert len(REQUESTS) == 1
    finally:
        server.close()


@pytest.mark.slow
def test_run_session_provider_mode_sends_task_to_worker(tmp_path, monkeypatch) -> None:
    _reset_server()
    server = _FakeOpenAIServer()
    try:
        config_path = _provider_config(tmp_path / "providers.json", server.base_url)
        _set_provider_env(monkeypatch, config_path)
        _enqueue(
            '{"type":"tool_call","name":"edit_file","arguments":'
            '{"path":"target.txt","old_string":"fixture\\n",'
            '"new_string":"fixture\\n// provider-alpha\\n"}}'
        )
        _enqueue(
            '{"type":"tool_call","name":"run_shell","arguments":'
            '{"cmd":["true"]}}'
        )
        _enqueue('{"type":"finish","summary":"edited target.txt"}')

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
            assert len(REQUESTS) == 3
            assert REQUESTS[0]["model"] == "loopback-model"
            system = REQUESTS[0]["messages"][0]["content"]
            assert system.startswith("You are Cambium's autonomous coding agent.")
            assert "Available tools:" in system
            # §9.1.6: the task is delimited user-role data, never system text.
            assert TASK_TEXT not in system
            task_message = REQUESTS[0]["messages"][1]
            assert task_message["role"] == "user"
            assert task_message["content"] == (
                f"<cambium-task>\nTask: {TASK_TEXT}\n</cambium-task>"
            )
    finally:
        server.close()


@pytest.mark.slow
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
    assert not record.exists(), "worker git command executed a repository hook"


def test_worker_git_argv_disables_hooks(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[list[str]] = []

    def fake_run(argv: list[str], **_kwargs: Any) -> subprocess.CompletedProcess[str]:
        calls.append(argv)
        return subprocess.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr(worker.subprocess, "run", fake_run)

    worker.git("status")

    assert calls == [["git", "-c", "core.hooksPath=/dev/null", "status"]]


def test_worker_fenced_git_argv_disables_hooks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[list[str]] = []

    class FakePopen:
        returncode = 0

        def __init__(self, argv: list[str], **_kwargs: Any) -> None:
            calls.append(argv)

        def poll(self) -> int:
            return 0

        def communicate(self) -> tuple[str, str]:
            return "", ""

    monkeypatch.setattr(worker, "validate_worker_generation", lambda *_args: True)
    monkeypatch.setattr(worker.subprocess, "Popen", FakePopen)

    worker._fenced_git(tmp_path, 1, "commit", "-m", "message")

    assert calls == [
        ["git", "-c", "core.hooksPath=/dev/null", "commit", "-m", "message"]
    ]


# ---------------------------------------------------------------------------
# Worker limits: turns, tokens, usage, wall budget
# ---------------------------------------------------------------------------


@pytest.mark.slow
def test_worker_endless_tool_calls_stop_at_max_turns(tmp_path) -> None:
    _reset_server()
    server = _FakeOpenAIServer()
    try:
        config_path = _provider_config(tmp_path / "providers.json", server.base_url)
        for _ in range(3):
            _enqueue(
                '{"type":"tool_call","name":"read_batch","arguments":'
                '{"paths":["target.txt"]}}'
            )

        session_dir = tmp_path / "session"
        repo = session_dir / "repo"
        _make_repo(repo)
        env = _worker_env(config_path, session_dir)
        init = _agent_init(config_path, max_turns=3, spec=TASK_TEXT)
        result, _messages, rc, _stderr = asyncio.run(
            _drive_worker(session_dir, repo, env, init=init, run={"task": TASK_TEXT},
                          branch="limits")
        )

        assert result["status"] == "failed"
        assert "max turns exceeded" in result["failure_reason"]
        assert rc == 0  # verdict delivered; the failure lives in the envelope
        with REQUEST_LOCK:
            assert len(REQUESTS) == 3
    finally:
        server.close()


@pytest.mark.slow
def test_worker_token_budget_fails_before_executing(tmp_path) -> None:
    _reset_server()
    server = _FakeOpenAIServer()
    try:
        config_path = _provider_config(tmp_path / "providers.json", server.base_url)
        # New-token accounting: only the prompt delta between turns plus the
        # completion count against the budget. The transcript grows between
        # the two calls (1000 -> 1600 prompt tokens), so the second turn's
        # 600 new input tokens plus 0 completion push the 1500 budget over
        # before the edit_file action is executed.
        _enqueue(
            '{"type":"tool_call","name":"read_batch","arguments":'
            '{"paths":["target.txt"]}}',
            usage={"prompt_tokens": 1000, "completion_tokens": 0, "total_tokens": 1000},
        )
        _enqueue(
            '{"type":"tool_call","name":"edit_file","arguments":'
            '{"path":"target.txt","old_string":"fixture\\n",'
            '"new_string":"fixture\\n// token-limit\\n"}}',
            usage={"prompt_tokens": 1600, "completion_tokens": 0, "total_tokens": 1600},
        )

        session_dir = tmp_path / "session"
        repo = session_dir / "repo"
        _make_repo(repo)
        env = _worker_env(config_path, session_dir)
        init = _agent_init(config_path, max_tokens=1500, spec=TASK_TEXT)
        result, _messages, rc, _stderr = asyncio.run(
            _drive_worker(session_dir, repo, env, init=init, run={"task": TASK_TEXT},
                          branch="tokens")
        )

        assert result["status"] == "failed"
        assert "token budget exceeded" in result["failure_reason"]
        assert rc == 0  # verdict delivered; the failure lives in the envelope
        # the second action (edit_file) was never executed
        assert (session_dir / "wt" / "target.txt").read_text(encoding="utf-8") == "fixture\n"
        with REQUEST_LOCK:
            assert len(REQUESTS) == 2
    finally:
        server.close()


@pytest.mark.slow
def test_worker_token_budget_binds_total_only_usage(tmp_path) -> None:
    """A provider that reports only total_tokens (no prompt/completion split)
    still binds the budget: each turn's whole total counts as new work, so the
    budget can never be bypassed."""
    _reset_server()
    server = _FakeOpenAIServer()
    try:
        config_path = _provider_config(tmp_path / "providers.json", server.base_url)
        _enqueue(
            '{"type":"tool_call","name":"read_batch","arguments":'
            '{"paths":["target.txt"]}}',
            usage={"total_tokens": 1000},
        )
        _enqueue(
            '{"type":"tool_call","name":"edit_file","arguments":'
            '{"path":"target.txt","old_string":"fixture\\n",'
            '"new_string":"fixture\\n// token-limit\\n"}}',
            usage={"total_tokens": 1000},
        )

        session_dir = tmp_path / "session"
        repo = session_dir / "repo"
        _make_repo(repo)
        env = _worker_env(config_path, session_dir)
        init = _agent_init(config_path, max_tokens=1500, spec=TASK_TEXT)
        result, _messages, rc, _stderr = asyncio.run(
            _drive_worker(session_dir, repo, env, init=init, run={"task": TASK_TEXT},
                          branch="tokens-total")
        )

        assert result["status"] == "failed"
        assert "token budget exceeded" in result["failure_reason"]
        assert rc == 0
        assert (session_dir / "wt" / "target.txt").read_text(encoding="utf-8") == "fixture\n"
        with REQUEST_LOCK:
            assert len(REQUESTS) == 2
    finally:
        server.close()


@pytest.mark.slow
def test_worker_missing_usable_token_counts_fail_closed(tmp_path) -> None:
    _reset_server()
    server = _FakeOpenAIServer()
    try:
        config_path = _provider_config(tmp_path / "providers.json", server.base_url)
        _enqueue('{"type":"finish","summary":"done"}', usage={"weird": 1})

        session_dir = tmp_path / "session"
        repo = session_dir / "repo"
        _make_repo(repo)
        env = _worker_env(config_path, session_dir)
        init = _agent_init(config_path, spec=TASK_TEXT)
        result, _messages, rc, _stderr = asyncio.run(
            _drive_worker(session_dir, repo, env, init=init, run={"task": TASK_TEXT},
                          branch="usage")
        )

        assert result["status"] == "failed"
        assert "missing usable token counts" in result["failure_reason"]
        assert rc == 0  # verdict delivered; the failure lives in the envelope
        with REQUEST_LOCK:
            assert len(REQUESTS) == 1
    finally:
        server.close()


@pytest.mark.slow
def test_worker_expired_wall_budget_bounded_failure(tmp_path) -> None:
    _reset_server()
    server = _FakeOpenAIServer()
    try:
        config_path = _provider_config(tmp_path / "providers.json", server.base_url)
        _enqueue('{"type":"finish","summary":"done"}')
        with REQUEST_LOCK:
            global RESPONSE_DELAY_S
            RESPONSE_DELAY_S = 0.15

        session_dir = tmp_path / "session"
        repo = session_dir / "repo"
        _make_repo(repo)
        env = _worker_env(config_path, session_dir)
        init = _agent_init(config_path, budget={"max_wall_s": 0.1}, spec=TASK_TEXT)
        result, _messages, rc, _stderr = asyncio.run(
            _drive_worker(session_dir, repo, env, init=init, run={"task": TASK_TEXT},
                          branch="wall")
        )

        assert result["status"] == "failed"
        assert "wall budget exceeded" in result["failure_reason"]
        assert rc == 0  # verdict delivered; the failure lives in the envelope
        with REQUEST_LOCK:
            assert len(REQUESTS) == 1
    finally:
        server.close()


# ---------------------------------------------------------------------------
# Permissions and strict action parsing
# ---------------------------------------------------------------------------


@pytest.mark.slow
def test_worker_run_shell_denied_never_executes(tmp_path) -> None:
    _reset_server()
    server = _FakeOpenAIServer()
    try:
        config_path = _provider_config(tmp_path / "providers.json", server.base_url)
        # Shell is denied here, so keep this transcript free of code edits:
        # finish must remain valid without verification while the no-existence
        # assertion below proves the denied command never executed.
        _enqueue(
            '{"type":"tool_call","name":"run_shell",'
            '"arguments":{"cmd":["touch","should-not-exist"]}}'
        )
        _enqueue('{"type":"finish","summary":"shell command denied"}')

        session_dir = tmp_path / "session"
        repo = session_dir / "repo"
        _make_repo(repo)
        env = _worker_env(config_path, session_dir)
        init = _agent_init(
            config_path, permissions={"shell": False, "network": False}, spec=TASK_TEXT
        )
        result, messages, rc, _stderr = asyncio.run(
            _drive_worker(session_dir, repo, env, init=init, run={"task": TASK_TEXT},
                          branch="shell-deny")
        )

        assert result["status"] == "succeeded"
        assert rc == 0
        assert not (session_dir / "wt" / "should-not-exist").exists()
        assert (session_dir / "wt" / "target.txt").read_text(encoding="utf-8") == "fixture\n"
        tool_events = [m for m in messages if m["type"] == "tool_event"]
        assert len(tool_events) == 1
        assert tool_events[0]["tool"] == "run_shell"
        assert tool_events[0]["ok"] is False
        with REQUEST_LOCK:
            assert len(REQUESTS) == 2
            assert any(
                "tool run_shell ok=False" in message.get("content", "")
                and "permission_denied:shell" in message.get("content", "")
                for message in REQUESTS[1]["messages"]
            )
    finally:
        server.close()


@pytest.mark.slow
def test_worker_malformed_and_unknown_actions_never_dispatch(tmp_path) -> None:
    _reset_server()
    server = _FakeOpenAIServer()
    try:
        config_path = _provider_config(tmp_path / "providers.json", server.base_url)
        _enqueue('{"type":"tool_call","name":"does_not_exist","arguments":{}}')
        _enqueue("this is not json")
        _enqueue(
            '{"type":"tool_call","name":"edit_file","arguments":'
            '{"path":"target.txt","old_string":"fixture\\n",'
            '"new_string":"fixture\\n// provider-alpha\\n"}}'
        )
        _enqueue(
            '{"type":"tool_call","name":"run_shell","arguments":'
            '{"cmd":["true"]}}'
        )
        _enqueue('{"type":"finish","summary":"edited target.txt"}')

        session_dir = tmp_path / "session"
        repo = session_dir / "repo"
        _make_repo(repo)
        env = _worker_env(config_path, session_dir)
        init = _agent_init(
            config_path, permissions={"shell": True, "network": False}, spec=TASK_TEXT
        )
        result, messages, rc, _stderr = asyncio.run(
            _drive_worker(session_dir, repo, env, init=init, run={"task": TASK_TEXT},
                          branch="strict")
        )

        assert result["status"] == "succeeded"
        assert rc == 0
        tool_events = [m for m in messages if m["type"] == "tool_event"]
        assert [m["tool"] for m in tool_events] == ["edit_file", "run_shell"]
        assert (session_dir / "wt" / "target.txt").read_text(encoding="utf-8") == (
            "fixture\n// provider-alpha\n"
        )
        with REQUEST_LOCK:
            assert len(REQUESTS) == 5
    finally:
        server.close()


# ---------------------------------------------------------------------------
# IPC observability: tool_event / checkpoint / heartbeat
# ---------------------------------------------------------------------------


@pytest.mark.slow
def test_worker_ipc_observability_tool_event_checkpoint_heartbeat(tmp_path) -> None:
    _reset_server()
    server = _FakeOpenAIServer()
    try:
        config_path = _provider_config(tmp_path / "providers.json", server.base_url)
        _enqueue(
            '{"type":"tool_call","name":"read_batch","arguments":'
            '{"paths":["notes.txt"]}}'
        )
        _enqueue(
            '{"type":"tool_call","name":"edit_file","arguments":'
            '{"path":"target.txt","old_string":"fixture\\n",'
            '"new_string":"fixture\\n// provider-alpha\\n"}}'
        )
        _enqueue(
            '{"type":"tool_call","name":"run_shell","arguments":'
            '{"cmd":["true"]}}'
        )
        _enqueue('{"type":"finish","summary":"read and edited target.txt"}')
        with REQUEST_LOCK:
            global RESPONSE_DELAY_S
            # 0.5s tool window vs the 0.05s heartbeat cadence: under load the
            # heartbeat loop must still tick at least once inside read_batch/
            # edit_file for the tool-carrying heartbeat assertions to hold.
            RESPONSE_DELAY_S = 0.5

        session_dir = tmp_path / "session"
        repo = session_dir / "repo"
        _make_repo(repo)
        env = _worker_env(config_path, session_dir)
        init = _agent_init(
            config_path,
            heartbeat={"interval_s": 0.05},
            permissions={"shell": True, "network": False},
            spec=TASK_TEXT,
        )
        result, messages, rc, stderr = asyncio.run(
            _drive_worker(session_dir, repo, env, init=init, run={"task": TASK_TEXT},
                          branch="observe")
        )

        assert result["status"] == "succeeded"
        assert rc == 0, f"stderr={stderr!r}"
        assert len(result["commits"]) == 1

        tool_events = [m for m in messages if m["type"] == "tool_event"]
        assert [m["tool"] for m in tool_events] == ["read_batch", "edit_file", "run_shell"]
        assert [m["turn"] for m in tool_events] == [1, 2, 3]
        assert all(m["ok"] is True for m in tool_events)
        assert all(isinstance(m["duration_ms"], int) for m in tool_events)
        # tool_event carries name/safe-cmd only; never tool output/content
        assert all("output-sentinel-7x9q" not in m["cmd"] for m in tool_events)

        checkpoints = [m for m in messages if m["type"] == "checkpoint"]
        assert [m["turn"] for m in checkpoints] == [1, 2, 3, 4]
        assert checkpoints[0]["commits_so_far"] == []
        assert checkpoints[1]["commits_so_far"] == []
        assert checkpoints[2]["commits_so_far"] == []
        final = checkpoints[3]
        assert final["commits_so_far"] == result["commits"]
        for cp in checkpoints:
            path = Path(cp["state_ref"])
            assert path.exists()
            payload = json.loads(path.read_text(encoding="utf-8"))
            assert payload["schema"] == 1
            assert payload["turn"] == cp["turn"]
            assert payload["generation"] == 1
            assert isinstance(payload["transcript"], list)
            assert isinstance(payload["usage"], dict)

        heartbeats = [m for m in messages if m["type"] == "heartbeat"]
        assert any(hb.get("turn", 0) >= 1 for hb in heartbeats)
        assert any(hb.get("tool") == "read_batch" for hb in heartbeats)
    finally:
        server.close()
