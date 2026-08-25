"""Supervisor-side cache-first context event regression scenarios."""

from __future__ import annotations

import asyncio
import subprocess
from pathlib import Path
from typing import Any

import pytest

import cambium.supervisor as supervisor_module
from cambium.store import CRITICAL_KINDS
from cambium.supervisor import (
    TaskResult,
    WorkerHandle,
    _GenOutcome,
    _Runtime,
    read_events,
    run_plan,
)


class _MemoryStore:
    def __init__(self) -> None:
        self.records: list[dict[str, Any]] = []

    def append(self, record: dict[str, Any]) -> int:
        self.records.append(record)
        return len(self.records)


def _provider_boundary() -> dict[str, Any]:
    return {
        "provider": "fake-provider",
        "endpoint": "https://api.example",
        "authmode": "api_key",
        "api_key_env": "FAKE_KEY",
        "provider_env_keys": [],
        "authorized_providers": None,
        "authorized_providers_explicit": False,
        "protocol": "loopback",
        "model": "fake-model",
        "tier": "fast",
        "reasoning_effort": None,
        "provider_config_path": "/opt/cambium/providers.json",
    }


def _checkpoint_message(*, task_id: str = "task", generation: int = 1) -> dict[str, Any]:
    digest = "a" * 64
    return {
        "type": "context_checkpoint",
        "task_id": task_id,
        "generation": generation,
        "epoch": 1,
        "turn": 1,
        "checkpoint_ref": f"{task_id}/epoch-001-{'a' * 16}-{'b' * 16}.json",
        "cache_key": {
            "provider": "fake-provider",
            "model": "fake-model",
            "protocol": "loopback",
            "reasoning_effort": None,
            "system_sha256": digest,
            "tools_sha256": digest,
            "prefix_sha256": digest,
            "suffix_sha256": digest,
            "full_sha256": digest,
            "prefix_bytes": 0,
            "message_count": 1,
            "redacted": False,
            "provider_boundary": _provider_boundary(),
        },
    }


def _write_checkpoint_worker(path: Path, event: dict[str, Any]) -> None:
    event_literal = repr(event)
    path.write_text(
        "import json\n"
        "import os\n"
        "import sys\n"
        "def send(message):\n"
        "    sys.stdout.write(json.dumps(message) + '\\n')\n"
        "    sys.stdout.flush()\n"
        "init = json.loads(sys.stdin.readline())\n"
        "task_id = init['task_id']\n"
        "generation = init['generation']\n"
        "send({'type': 'ready', 'request_id': init['request_id'],\n"
        "      'task_id': task_id, 'generation': generation, 'pid': os.getpid(),\n"
        "      'proto': 1})\n"
        "run = json.loads(sys.stdin.readline())\n"
        f"send({event_literal})\n"
        "send({'type': 'result_envelope', 'request_id': run['request_id'],\n"
        "      'task_id': task_id, 'generation': generation, 'status': 'succeeded',\n"
        "      'commits': [], 'files_changed': [], 'diff': '', 'summary': 'done',\n"
        "      'failure_reason': None})\n"
        "send({'type': 'exit_message', 'task_id': task_id,\n"
        "      'generation': generation, 'reason': 'done'})\n",
        encoding="utf-8",
    )


def _make_repo(tmp_path: Path) -> tuple[Path, Path, str]:
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-b", "main", str(repo)], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.name", "context-events"], check=True)
    subprocess.run(
        ["git", "-C", str(repo), "config", "user.email", "context-events@test"], check=True
    )
    (repo / "file.txt").write_text("content\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(repo), "add", "-A"], check=True, capture_output=True)
    subprocess.run(
        ["git", "-C", str(repo), "commit", "-m", "initial"],
        check=True,
        capture_output=True,
    )
    worktree = tmp_path / "worktree"
    subprocess.run(
        ["git", "-C", str(repo), "worktree", "add", "-b", "task", str(worktree), "main"],
        check=True,
        capture_output=True,
    )
    base = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "main"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    return repo, worktree, base


def _generation_spec(
    repo: Path, worktree: Path, base: str, worker: Path, *, task_id: str = "task"
) -> dict[str, Any]:
    return {
        "task_id": task_id,
        "task": "finish the task",
        "repo": str(repo),
        "worktree_path": str(worktree),
        "branch": "task",
        "base_commit": base,
        "worker": str(worker),
        "provider_env_keys": [],
        "max_turns": 2,
        "max_tokens": 10_000,
        "max_wall_s": 10.0,
    }


def _drive_checkpoint_generation(
    tmp_path: Path, event: dict[str, Any]
) -> tuple[_Runtime, _MemoryStore, _GenOutcome]:
    repo, worktree, base = _make_repo(tmp_path)
    worker = tmp_path / "checkpoint_worker.py"
    _write_checkpoint_worker(worker, event)
    store = _MemoryStore()
    runtime = _Runtime(tmp_path / "session", store, context_reuse=True)
    outcome = asyncio.run(
        runtime._drive_generation(
            _generation_spec(repo, worktree, base, worker),
            WorkerHandle(task_id="task", generation=1),
            ready_timeout=2.0,
            heartbeat_interval=0.1,
            heartbeat_timeout=2.0,
            wall_budget=10.0,
        )
    )
    return runtime, store, outcome


def test_incomplete_context_checkpoint_is_rejected_without_epoch_update(
    tmp_path: Path,
) -> None:
    for missing in ("system_sha256", "tools_sha256", "provider_boundary"):
        case_dir = tmp_path / missing
        case_dir.mkdir()
        event = _checkpoint_message()
        del event["cache_key"][missing]
        runtime, store, outcome = _drive_checkpoint_generation(case_dir, event)

        assert outcome.clean is True
        assert "task" not in runtime._task_epochs
        rejected = [
            record
            for record in store.records
            if record["kind"] == "protocol"
            and record["payload"].get("note") == "context_checkpoint rejected: invalid field(s)"
        ]
        assert rejected


def test_context_checkpoint_generation_mismatch_is_rejected(tmp_path: Path) -> None:
    event = _checkpoint_message(generation=2)
    runtime, store, outcome = _drive_checkpoint_generation(tmp_path, event)

    assert outcome.clean is True
    assert "task" not in runtime._task_epochs
    assert any(
        record["kind"] == "protocol"
        and record["payload"].get("note") == "context_checkpoint rejected: identity mismatch"
        for record in store.records
    )


class _Clock:
    def __init__(self, current: float, real_time: Any) -> None:
        self.current = current
        self._real_time = real_time

    def monotonic(self) -> float:
        return self.current

    def monotonic_ns(self) -> int:
        return int(self.current * 1_000_000_000)

    def time(self) -> float:
        return self._real_time.time()

    def time_ns(self) -> int:
        return self._real_time.time_ns()


def test_wall_budget_uses_one_deadline_across_two_suspensions(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    clock = _Clock(100.0, supervisor_module.time)
    monkeypatch.setattr(supervisor_module, "time", clock)
    store = _MemoryStore()
    runtime = _Runtime(tmp_path / "session", store, context_reuse=True)
    spec = {
        "task_id": "root",
        "task": "finish",
        "repo": str(tmp_path / "repo"),
        "worktree_path": str(tmp_path / "worktree"),
        "branch": "root",
        "base_commit": "base",
        "max_wall_s": 20.0,
    }
    outcomes = [
        _GenOutcome(
            clean=True,
            correlated=True,
            envelope={"status": "suspended", "checkpoint_ref": "ref-1", "epoch": 1},
        ),
        _GenOutcome(
            clean=True,
            correlated=True,
            envelope={"status": "suspended", "checkpoint_ref": "ref-2", "epoch": 2},
        ),
        _GenOutcome(
            clean=True,
            correlated=True,
            envelope={"status": "succeeded", "summary": "done"},
        ),
    ]
    drive_elapsed = iter((2.0, 4.0, 0.0))

    async def fake_drive(
        spec_arg: dict[str, Any], handle: WorkerHandle, **kwargs: Any
    ) -> _GenOutcome:
        del spec_arg, handle, kwargs
        clock.current += next(drive_elapsed)
        return outcomes.pop(0)

    remaining_samples: list[tuple[float, float]] = []

    async def fake_await(parent_task_id: str, remaining: float) -> None:
        del parent_task_id
        remaining_samples.append((remaining, clock.current))
        clock.current += 3.0

    async def fake_ensure_worktree(spec_arg: dict[str, Any]) -> int:
        del spec_arg
        return 1

    async def fake_git_stdout(*args: Any, **kwargs: Any) -> str:
        del args, kwargs
        return "base"

    async def fake_integrity(spec_arg: dict[str, Any], worktree: Path) -> None:
        del spec_arg, worktree

    monkeypatch.setattr(runtime, "_drive_generation", fake_drive)
    monkeypatch.setattr(runtime, "_await_suspend_children", fake_await)
    monkeypatch.setattr(runtime, "_ensure_worktree", fake_ensure_worktree)
    monkeypatch.setattr(runtime, "_git_stdout", fake_git_stdout)
    monkeypatch.setattr(runtime, "_worker_success_integrity", fake_integrity)

    asyncio.run(runtime._supervise(spec))

    assert len(remaining_samples) == 2
    second_remaining, second_now = remaining_samples[1]
    assert second_remaining == pytest.approx(120.0 - second_now)
    assert second_remaining == pytest.approx(11.0)
    assert runtime._results["root"].status == "succeeded"


def _write_resume_failure_worker(path: Path) -> None:
    path.write_text(
        "import json\n"
        "import os\n"
        "import sys\n"
        "def send(message):\n"
        "    sys.stdout.write(json.dumps(message) + '\\n')\n"
        "    sys.stdout.flush()\n"
        "init = json.loads(sys.stdin.readline())\n"
        "task_id = init['task_id']\n"
        "generation = init['generation']\n"
        "send({'type': 'ready', 'request_id': init['request_id'],\n"
        "      'task_id': task_id, 'generation': generation, 'pid': os.getpid(),\n"
        "      'proto': 1})\n"
        "run = json.loads(sys.stdin.readline())\n"
        "if init.get('resume') is None:\n"
        "    boundary = {\n"
        "        'provider': 'fake-provider', 'endpoint': 'https://api.example',\n"
        "        'authmode': 'api_key', 'api_key_env': 'FAKE_KEY',\n"
        "        'provider_env_keys': [], 'authorized_providers': None,\n"
        "        'authorized_providers_explicit': False, 'protocol': 'loopback',\n"
        "        'model': 'fake-model', 'tier': 'fast',\n"
        "        'reasoning_effort': None,\n"
        "        'provider_config_path': '/opt/cambium/providers.json'}\n"
        "    cache_key = {\n"
        "        'provider': 'fake-provider', 'model': 'fake-model',\n"
        "        'protocol': 'loopback', 'reasoning_effort': None,\n"
        "        'system_sha256': 'a' * 64, 'tools_sha256': 'a' * 64,\n"
        "        'prefix_sha256': 'a' * 64, 'suffix_sha256': 'a' * 64,\n"
        "        'full_sha256': 'a' * 64, 'prefix_bytes': 0, 'message_count': 1,\n"
        "        'redacted': False,\n"
        "        'provider_boundary': boundary}\n"
        "    checkpoint_ref = task_id + '/epoch-001-' + 'a' * 16 + '-' + 'b' * 16 + '.json'\n"
        "    send({'type': 'context_checkpoint', 'task_id': task_id,\n"
        "          'generation': generation, 'epoch': 1, 'turn': 1,\n"
        "          'checkpoint_ref': checkpoint_ref, 'cache_key': cache_key})\n"
        "    send({'type': 'result_envelope', 'request_id': run['request_id'],\n"
        "          'task_id': task_id, 'generation': generation, 'status': 'suspended',\n"
        "          'checkpoint_ref': checkpoint_ref, 'epoch': 1, 'commits': [],\n"
        "          'files_changed': [], 'diff': '', 'summary': 'suspended',\n"
        "          'failure_reason': None})\n"
        "else:\n"
        "    send({'type': 'result_envelope', 'request_id': run['request_id'],\n"
        "          'task_id': task_id, 'generation': generation, 'status': 'failed',\n"
        "          'commits': [], 'files_changed': [], 'diff': '',\n"
        "          'summary': 'resume failed',\n"
        "          'failure_reason': 'context_resume_failed: checkpoint unreadable'})\n"
        "send({'type': 'exit_message', 'task_id': task_id,\n"
        "      'generation': generation, 'reason': 'done'})\n",
        encoding="utf-8",
    )


def test_failed_resume_emits_durable_context_resume_failed(tmp_path: Path) -> None:
    session_dir = tmp_path / "session"
    repo = session_dir / "repo"
    repo.parent.mkdir()
    subprocess.run(["git", "init", "-b", "main", str(repo)], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.name", "resume-events"], check=True)
    subprocess.run(
        ["git", "-C", str(repo), "config", "user.email", "resume-events@test"], check=True
    )
    (repo / "file.txt").write_text("content\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(repo), "add", "-A"], check=True, capture_output=True)
    subprocess.run(
        ["git", "-C", str(repo), "commit", "-m", "initial"],
        check=True,
        capture_output=True,
    )
    base = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "main"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    worker = tmp_path / "resume_failure_worker.py"
    _write_resume_failure_worker(worker)
    task = {
        "task_id": "resume-task",
        "task": "finish",
        "repo": str(repo),
        "worktree_path": str(session_dir / "worktree"),
        "branch": "resume-task",
        "base_commit": base,
        "worker": str(worker),
        "provider_env_keys": [],
        "max_wall_s": 20.0,
        "max_restarts": 0,
    }

    result = asyncio.run(run_plan(session_dir, {"tasks": [task]}, context_reuse=True))

    assert result.exit_code == 1
    task_result = result.results[0]
    assert task_result == TaskResult(
        task_id="resume-task",
        status="failed",
        exit_code=1,
        reason="context_resume_failed: checkpoint unreadable",
        restarts=0,
        summary="resume failed",
    )
    assert "context_resume_failed" in CRITICAL_KINDS
    events = read_events(session_dir)
    failures = [event for event in events if event["kind"] == "context_resume_failed"]
    assert len(failures) == 1
    assert failures[0]["payload"]["reason"] == "context_resume_failed: checkpoint unreadable"
