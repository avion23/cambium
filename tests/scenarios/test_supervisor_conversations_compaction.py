"""Supervisor contract scenarios for raw checkpoints and deterministic folds."""

from __future__ import annotations

import asyncio
import json
import subprocess
from pathlib import Path
from typing import Any

import pytest

from cambium.conversations import ConversationStore
from cambium.redact import build_session_redactor
from cambium.store import CRITICAL_KINDS
from cambium.supervisor import WorkerHandle, _Runtime

pytestmark = pytest.mark.slow


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


def _context_checkpoint_message(task_id: str = "task") -> dict[str, Any]:
    digest = "a" * 64
    return {
        "type": "context_checkpoint",
        "task_id": task_id,
        "generation": 1,
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


def _make_repo(tmp_path: Path) -> tuple[Path, Path, str]:
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(
        ["git", "init", "-b", "main", str(repo)], check=True, capture_output=True
    )
    subprocess.run(["git", "-C", str(repo), "config", "user.name", "contract"], check=True)
    subprocess.run(
        ["git", "-C", str(repo), "config", "user.email", "contract@test"], check=True
    )
    (repo / "file.txt").write_text("content\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(repo), "add", "-A"], check=True, capture_output=True)
    subprocess.run(
        ["git", "-C", str(repo), "commit", "-m", "initial"],
        check=True,
        capture_output=True,
    )
    worktree = tmp_path / "worktree"
    base = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "main"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    subprocess.run(
        ["git", "-C", str(repo), "worktree", "add", "-b", "task", str(worktree), base],
        check=True,
        capture_output=True,
    )
    return repo, worktree, base


def _spec(
    repo: Path, worktree: Path, base: str, worker: Path, *, task_id: str = "task"
) -> dict[str, Any]:
    return {
        "task_id": task_id,
        "task": "finish the task",
        "repo": str(repo),
        "worktree_path": str(worktree),
        "branch": task_id,
        "base_commit": base,
        "worker": str(worker),
        "provider_env_keys": [],
        "max_turns": 2,
        "max_tokens": 10_000,
        "max_wall_s": 10.0,
        "max_restarts": 0,
    }


def _write_worker(path: Path, messages: list[dict[str, Any]]) -> None:
    path.write_text(
        "import json\n"
        "import os\n"
        "import sys\n"
        f"MESSAGES = {messages!r}\n"
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
        "for message in MESSAGES:\n"
        "    send(message)\n"
        "send({'type': 'result_envelope', 'request_id': run['request_id'],\n"
        "      'task_id': task_id, 'generation': generation, 'status': 'succeeded',\n"
        "      'commits': [], 'files_changed': [], 'diff': '', 'summary': 'done',\n"
        "      'failure_reason': None})\n"
        "send({'type': 'exit_message', 'task_id': task_id,\n"
        "      'generation': generation, 'reason': 'done'})\n",
        encoding="utf-8",
    )


def _write_checkpoint_file(session_dir: Path, event: dict[str, Any]) -> None:
    checkpoint_ref = event["checkpoint_ref"]
    path = session_dir / ".cambium" / "checkpoints" / checkpoint_ref
    path.parent.mkdir(parents=True)
    path.write_text(
        json.dumps({
            "provider_messages": [
                {"role": "system", "content": "system TOP-SECRET"},
                {"role": "user", "content": "question"},
            ],
            "continuation_suffix": [
                {"role": "assistant", "content": "answer TOP-SECRET"},
            ],
        }),
        encoding="utf-8",
    )


def test_context_checkpoint_appends_redacted_raw_rows_and_replays_files(
    tmp_path: Path,
) -> None:
    repo, worktree, base = _make_repo(tmp_path)
    session_dir = tmp_path / "session"
    event = _context_checkpoint_message()
    _write_checkpoint_file(session_dir, event)
    worker = tmp_path / "worker.py"
    _write_worker(worker, [event])
    event_store = _MemoryStore()
    conversations = ConversationStore(session_dir / ".cambium" / "conversations.db")
    runtime = _Runtime(
        session_dir,
        event_store,
        redactor=build_session_redactor(["TOP-SECRET"]),
        conversations=conversations,
        context_reuse=True,
    )
    try:
        outcome = asyncio.run(
            runtime._drive_generation(
                _spec(repo, worktree, base, worker),
                WorkerHandle(task_id="task", generation=1),
                ready_timeout=2.0,
                heartbeat_interval=0.1,
                heartbeat_timeout=2.0,
                wall_budget=10.0,
            )
        )
        assert outcome.clean is True
        rows = conversations.history("task")
        assert len(rows) == 3
        assert all(row["node_id"] == "task" for row in rows)
        assert all(row["kind"] == "turn" for row in rows)
        assert all(
            row["meta"] == {
                "checkpoint_ref": event["checkpoint_ref"],
                "epoch": 1,
            }
            for row in rows
        )
        assert [row["content"] for row in rows] == [
            "system ***",
            "question",
            "answer ***",
        ]
        replay = runtime.replay_raw_record("task")
        assert len(replay["rows"]) == 3
        assert replay["checkpoint_files"][0]["checkpoint_ref"] == event["checkpoint_ref"]
        assert replay["checkpoint_files"][0]["continuation_suffix"] == [
            {"role": "assistant", "content": "answer ***"},
        ]
    finally:
        conversations.close()


def test_context_checkpoint_does_not_create_rows_when_conversations_are_disabled(
    tmp_path: Path,
) -> None:
    repo, worktree, base = _make_repo(tmp_path)
    session_dir = tmp_path / "session"
    event = _context_checkpoint_message()
    _write_checkpoint_file(session_dir, event)
    worker = tmp_path / "worker.py"
    _write_worker(worker, [event])
    runtime = _Runtime(session_dir, _MemoryStore(), context_reuse=True)

    outcome = asyncio.run(
        runtime._drive_generation(
            _spec(repo, worktree, base, worker),
            WorkerHandle(task_id="task", generation=1),
            ready_timeout=2.0,
            heartbeat_interval=0.1,
            heartbeat_timeout=2.0,
            wall_budget=10.0,
        )
    )

    assert outcome.clean is True
    assert not (session_dir / ".cambium" / "conversations.db").exists()
    assert runtime.replay_raw_record("task") == {
        "node_id": "task",
        "rows": [],
        "checkpoint_files": [],
    }


def _write_capture_worker(path: Path) -> None:
    path.write_text(
        "import json\n"
        "import os\n"
        "import sys\n"
        "from pathlib import Path\n"
        "def send(message):\n"
        "    sys.stdout.write(json.dumps(message) + '\\n')\n"
        "    sys.stdout.flush()\n"
        "init = json.loads(sys.stdin.readline())\n"
        "send({'type': 'ready', 'request_id': init['request_id'],\n"
        "      'task_id': init['task_id'], 'generation': init['generation'],\n"
        "      'pid': os.getpid(), 'proto': 1})\n"
        "run = json.loads(sys.stdin.readline())\n"
        "with Path(os.environ['PAYLOAD_PATH']).open('a', encoding='utf-8') as handle:\n"
        "    handle.write(json.dumps(run) + '\\n')\n"
        "send({'type': 'result_envelope', 'request_id': run['request_id'],\n"
        "      'task_id': init['task_id'], 'generation': init['generation'],\n"
        "      'status': 'succeeded', 'commits': [], 'files_changed': [],\n"
        "      'diff': '', 'summary': 'done', 'failure_reason': None})\n"
        "send({'type': 'exit_message', 'task_id': init['task_id'],\n"
        "      'generation': init['generation'], 'reason': 'done'})\n",
        encoding="utf-8",
    )


def test_published_run_field_tracks_prior_clean_success(tmp_path: Path, monkeypatch) -> None:
    repo, worktree, base = _make_repo(tmp_path)
    payload_path = tmp_path / "run-payloads.jsonl"
    worker = tmp_path / "capture_worker.py"
    _write_capture_worker(worker)
    monkeypatch.setenv("PAYLOAD_PATH", str(payload_path))
    spec = _spec(repo, worktree, base, worker)
    spec["provider_env_keys"] = ["PAYLOAD_PATH"]
    runtime = _Runtime(tmp_path / "session", _MemoryStore())

    asyncio.run(runtime.supervise_task(spec))
    assert runtime._results["task"].status == "succeeded"
    runtime._results.pop("task")
    asyncio.run(runtime.supervise_task(spec))

    payloads = [json.loads(line) for line in payload_path.read_text().splitlines()]
    assert [payload["published"] for payload in payloads] == [False, True]
    assert all(type(payload["published"]) is bool for payload in payloads)


def test_compaction_events_are_strictly_validated_and_durable(tmp_path: Path) -> None:
    repo, worktree, base = _make_repo(tmp_path)
    valid_advanced = {
        "type": "context_epoch_advanced",
        "request_id": "epoch-request",
        "task_id": "task",
        "generation": 1,
        "epoch": 2,
        "checkpoint_ref": "task/epoch-002-aaaaaaaaaaaaaaaa-bbbbbbbbbbbbbbbb.json",
        "folded_from_epoch": 1,
        "reason": None,
    }
    valid_failed = {
        "type": "compaction_failed",
        "request_id": "failure-request",
        "task_id": "task",
        "generation": 1,
        "epoch": 2,
        "reason": "canary failed",
    }
    malformed_advanced = {**valid_advanced, "folded_from_epoch": 0}
    malformed_failed = {**valid_failed, "reason": ""}
    worker = tmp_path / "events_worker.py"
    _write_worker(worker, [valid_advanced, valid_failed, malformed_advanced, malformed_failed])
    event_store = _MemoryStore()
    runtime = _Runtime(tmp_path / "session", event_store)

    outcome = asyncio.run(
        runtime._drive_generation(
            _spec(repo, worktree, base, worker),
            WorkerHandle(task_id="task", generation=1),
            ready_timeout=2.0,
            heartbeat_interval=0.1,
            heartbeat_timeout=2.0,
            wall_budget=10.0,
        )
    )

    assert outcome.clean is True
    advanced = [
        record for record in event_store.records if record["kind"] == "context_epoch_advanced"
    ]
    failed = [record for record in event_store.records if record["kind"] == "compaction_failed"]
    assert len(advanced) == 1
    assert advanced[0]["request_id"] == "epoch-request"
    assert advanced[0]["payload"] == {
        "epoch": 2,
        "checkpoint_ref": valid_advanced["checkpoint_ref"],
        "folded_from_epoch": 1,
        "reason": None,
    }
    assert len(failed) == 1
    assert failed[0]["request_id"] == "failure-request"
    assert failed[0]["payload"] == {"epoch": 2, "reason": "canary failed"}
    rejected = [record for record in event_store.records if record["kind"] == "protocol"]
    assert {
        record["payload"]["note"]
        for record in rejected
        if "rejected: invalid field(s)" in record["payload"].get("note", "")
    } == {
        "context_epoch_advanced rejected: invalid field(s)",
        "compaction_failed rejected: invalid field(s)",
    }
    assert "context_epoch_advanced" in CRITICAL_KINDS
    assert "compaction_failed" in CRITICAL_KINDS
