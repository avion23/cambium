"""P0 fork/join publication and conflict-envelope scenarios."""

from __future__ import annotations

import asyncio
import subprocess
from pathlib import Path
from typing import Any

import pytest

from cambium.supervisor import WorkerHandle, _Runtime

pytestmark = pytest.mark.slow


class _Store:
    def __init__(self) -> None:
        self.records: list[dict[str, Any]] = []

    def append(self, record: dict[str, Any]) -> int:
        self.records.append(record)
        return len(self.records)

    def events_after(self, _seq: int) -> list[dict[str, Any]]:
        return list(self.records)


def _git(cwd: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args], cwd=str(cwd), capture_output=True, text=True, check=check
    )


def _rev(cwd: Path, ref: str = "HEAD") -> str:
    return _git(cwd, "rev-parse", "--verify", f"{ref}^{{commit}}").stdout.strip()


def _init_repo(repo: Path) -> str:
    subprocess.run(["git", "init", "-b", "main", str(repo)], check=True, capture_output=True)
    _git(repo, "config", "user.name", "join-test")
    _git(repo, "config", "user.email", "join@test")
    _git(repo, "config", "gc.auto", "0")
    (repo / "base.txt").write_text("base\n", encoding="utf-8")
    _git(repo, "add", "base.txt")
    _git(repo, "commit", "-m", "initial")
    return _rev(repo)


def _branch_commit(
    repo: Path, base: str, branch: str, worktree: Path, path: str, content: str
) -> str:
    _git(repo, "worktree", "add", "-b", branch, str(worktree), base)
    (worktree / path).write_text(content, encoding="utf-8")
    _git(worktree, "add", path)
    _git(worktree, "commit", "-m", f"{branch} change")
    return _rev(worktree)


def _runtime(session_dir: Path, store: _Store) -> _Runtime:
    return _Runtime(session_dir=session_dir, store=store)


def _write_resolver_worker(path: Path, *, verdict: str = "succeeded") -> None:
    """Write a tiny resolver worker that consumes the resolver wire payload."""
    if verdict == "succeeded":
        body = """
files = run["resolver"]["conflicted_files"]
worktree = Path(run["worktree_path"])
target = worktree / files[0]
lines = target.read_text().splitlines()
resolved = []
inside = True
for line in lines:
    if line.startswith("<<<<<<<"):
        inside = False
    elif line.startswith("======="):
        inside = True
        resolved.append("// resolved by child")
    elif line.startswith(">>>>>>>"):
        continue
    elif inside:
        resolved.append(line)
target.write_text("\\n".join(resolved) + "\\n")
subprocess.run(["git", "add", files[0]], cwd=worktree, check=True)
subprocess.run(["git", "commit", "-m", "resolver result"], cwd=worktree, check=True)
sha = subprocess.run(
    ["git", "rev-parse", "HEAD"], cwd=worktree, check=True, capture_output=True, text=True
).stdout.strip()
status = "succeeded"
failure_reason = None
summary = "resolver merged both intents"
commits = [sha]
files_changed = files
diff = subprocess.run(
    ["git", "diff", "HEAD^..HEAD"], cwd=worktree, check=True, capture_output=True, text=True
).stdout
"""
    else:
        body = """
status = "unresolvable"
failure_reason = "unresolvable"
summary = "resolver cannot safely choose an intent"
commits = []
files_changed = []
diff = ""
"""
    path.write_text(
        "import json, os, subprocess, sys\n"
        "from pathlib import Path\n\n"
        "def send(message):\n"
        "    print(json.dumps(message), flush=True)\n\n"
        "def read():\n"
        "    for line in sys.stdin:\n"
        "        if line.strip():\n"
        "            return json.loads(line)\n"
        "    return None\n\n"
        "init = read()\n"
        "if init is None:\n"
        "    raise SystemExit(1)\n"
        "task_id = init['task_id']\n"
        "generation = init.get('generation', 1)\n"
        "send({'type': 'ready', 'request_id': init['request_id'], 'task_id': task_id,\n"
        "      'generation': generation, 'proto': 1, 'pid': os.getpid()})\n"
        "run = read()\n"
        "if run is None or 'resolver' not in run:\n"
        "    raise SystemExit(1)\n"
        f"{body}\n"
        "send({'type': 'result_envelope', 'request_id': run['request_id'],\n"
        "      'task_id': task_id, 'generation': generation, 'status': status,\n"
        "      'summary': summary, 'failure_reason': failure_reason,\n"
        "      'commits': commits, 'files_changed': files_changed, 'diff': diff})\n"
        "send({'type': 'exit_message', 'task_id': task_id, 'generation': generation,\n"
        "      'reason': 'done'})\n",
        encoding="utf-8",
    )


def _resolver_plan_task(
    session_dir: Path,
    repo: Path,
    base: str,
    task_id: str,
    resolver_worker: Path,
) -> dict[str, Any]:
    fake_worker = Path(__file__).resolve().parents[2] / "scripts" / "fake_worker.py"
    return {
        "task_id": task_id,
        "task": f"replace the shared intent for {task_id}",
        "repo": str(repo),
        "worktree_path": str(session_dir / f"wt-{task_id}"),
        "branch": f"wt-{task_id}",
        "worker": str(fake_worker),
        "resolver_worker": str(resolver_worker),
        "target_file": "base.txt",
        "marker": f"// {task_id}",
        "write_marker": True,
        "base_commit": base,
        "provider_env_keys": ["FAKE_MODE"],
    }


def test_clean_child_join_satisfies_head_invariant(tmp_path: Path) -> None:
    session = tmp_path / "session"
    repo = tmp_path / "repo"
    base = _init_repo(repo)
    parent_worktree = tmp_path / "parent"
    _git(repo, "worktree", "add", "-b", "parent", str(parent_worktree), base)
    child_worktree = tmp_path / "child"
    child_tip = _branch_commit(repo, base, "child", child_worktree, "child.txt", "child\n")

    store = _Store()
    runtime = _runtime(session, store)
    parent_spec = {
        "task_id": "parent",
        "kind": "test",
        "repo": str(repo),
        "worktree_path": str(parent_worktree),
        "branch": "parent",
        "base_commit": base,
    }
    runtime.set_session_tasks([parent_spec])
    child_spec = {
        "task_id": "child",
        "repo": str(repo),
        "branch": "child",
        "parent_task_id": "parent",
    }

    accepted = asyncio.run(runtime._merge_task(child_spec, WorkerHandle("child", 1)))

    assert accepted == child_tip
    assert _rev(parent_worktree) == accepted
    assert asyncio.run(runtime._assert_parent_join_invariant(parent_spec, ["child"], 1))
    assert not [record for record in store.records if record["kind"] == "join_invariant_failed"]


def test_conflict_emits_bounded_structured_envelope(tmp_path: Path) -> None:
    session = tmp_path / "session"
    repo = tmp_path / "repo"
    base = _init_repo(repo)
    worker_worktree = tmp_path / "worker"
    _branch_commit(repo, base, "worker", worker_worktree, "base.txt", "worker\n")

    (repo / "base.txt").write_text("main\n", encoding="utf-8")
    _git(repo, "add", "base.txt")
    _git(repo, "commit", "-m", "main change")
    integration_head = _rev(repo, "main")

    store = _Store()
    runtime = _runtime(session, store)
    result = asyncio.run(
        runtime._merge_task(
            {
                "task_id": "worker",
                "repo": str(repo),
                "branch": "worker",
            },
            WorkerHandle("worker", 1),
        )
    )

    assert result is None
    conflicts = [record for record in store.records if record["kind"] == "merge_failed"]
    assert len(conflicts) == 1
    payload = conflicts[0]["payload"]
    assert payload["status"] == "merge_conflict"
    assert payload["conflicted_files"] == ["base.txt"]
    assert payload["summary"]
    assert payload["integration_head"] == integration_head
    assert payload["diff_evidence"]
    assert len(payload["diff_evidence"].encode("utf-8")) <= 4 * 1024
    assert payload["merge_error"] == "MergeConflictError"
    assert payload["message"] == payload["summary"]


def test_enabled_conflict_spawns_resolver_and_publishes_result(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    session = tmp_path / "session"
    repo = tmp_path / "repo"
    base = _init_repo(repo)
    resolver_worker = tmp_path / "resolver.py"
    _write_resolver_worker(resolver_worker)
    monkeypatch.setenv("FAKE_MODE", "overwrite")
    tasks = [
        _resolver_plan_task(session, repo, base, task_id, resolver_worker)
        for task_id in ("left", "right")
    ]

    from cambium.supervisor import read_events, run_plan

    result = asyncio.run(
        run_plan(
            session,
            {"tasks": tasks},
            resolver_child_enabled=True,
            max_concurrent_tasks=2,
        )
    )

    assert result.exit_code == 0
    assert all(item.status == "succeeded" for item in result.results)
    events = read_events(session)
    admitted = [event for event in events if event["kind"] == "resolver_child_admitted"]
    assert len(admitted) == 1
    payload = admitted[0]["payload"]
    assert payload["conflicted_files"] == ["base.txt"]
    assert payload["diff_evidence"]
    assert set(payload["parent_intent_summaries"]) == {"worker", "integration"}
    assert [event for event in events if event["kind"] == "resolver_succeeded"]
    merged = _git(repo, "show", "main:base.txt").stdout
    assert "// resolved by child" in merged
    assert "<<<<<<<" not in merged


def test_unresolvable_resolver_verdict_is_structured_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    session = tmp_path / "session"
    repo = tmp_path / "repo"
    base = _init_repo(repo)
    resolver_worker = tmp_path / "unresolver.py"
    _write_resolver_worker(resolver_worker, verdict="unresolvable")
    monkeypatch.setenv("FAKE_MODE", "overwrite")
    tasks = [
        _resolver_plan_task(session, repo, base, task_id, resolver_worker)
        for task_id in ("left", "right")
    ]

    from cambium.supervisor import read_events, run_plan

    result = asyncio.run(
        run_plan(
            session,
            {"tasks": tasks},
            resolver_child_enabled=True,
            max_concurrent_tasks=2,
        )
    )

    assert result.exit_code == 1
    failed_source = next(
        item
        for item in result.results
        if item.task_id in {"left", "right"} and item.status == "failed"
    )
    assert failed_source.status == "failed"
    assert failed_source.reason == "resolver_unresolvable"
    events = read_events(session)
    failures = [event for event in events if event["kind"] == "resolver_failed"]
    assert len(failures) == 1
    assert failures[0]["payload"]["status"] == "unresolvable"
    assert failures[0]["payload"]["reason"] == "unresolvable"
    assert not [event for event in events if event["kind"] == "resolver_succeeded"]


def test_resolver_flag_disabled_preserves_merge_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    session = tmp_path / "session"
    repo = tmp_path / "repo"
    base = _init_repo(repo)
    resolver_worker = tmp_path / "resolver.py"
    _write_resolver_worker(resolver_worker)
    monkeypatch.setenv("FAKE_MODE", "overwrite")
    tasks = [
        _resolver_plan_task(session, repo, base, task_id, resolver_worker)
        for task_id in ("left", "right")
    ]

    from cambium.supervisor import read_events, run_plan

    result = asyncio.run(run_plan(session, {"tasks": tasks}, max_concurrent_tasks=2))

    assert result.exit_code == 1
    source_results = [item for item in result.results if item.task_id in {"left", "right"}]
    assert {item.reason for item in source_results} == {None, "merge_failed"}
    events = read_events(session)
    assert not [event for event in events if event["kind"] == "resolver_child_admitted"]
    conflicts = [event for event in events if event["kind"] == "merge_failed"]
    assert len(conflicts) == 1
    assert conflicts[0]["payload"]["status"] == "merge_conflict"


def test_resolver_rechecks_parent_join_before_publication(tmp_path: Path) -> None:
    session = tmp_path / "session"
    repo = tmp_path / "repo"
    base = _init_repo(repo)
    parent_worktree = tmp_path / "parent"
    _branch_commit(repo, base, "parent", parent_worktree, "parent.txt", "parent\n")
    child_worktree = tmp_path / "child"
    _branch_commit(repo, base, "child", child_worktree, "base.txt", "child\n")
    (repo / "base.txt").write_text("main\n", encoding="utf-8")
    _git(repo, "add", "base.txt")
    _git(repo, "commit", "-m", "main conflict")
    integration_head = _rev(repo, "main")
    resolver_worker = tmp_path / "resolver.py"
    _write_resolver_worker(resolver_worker)
    parent_spec = {
        "task_id": "parent",
        "task": "parent intent",
        "repo": str(repo),
        "worktree_path": str(parent_worktree),
        "branch": "parent",
        "base_commit": base,
        "provider_env_keys": [],
    }
    child_spec = {
        "task_id": "child",
        "task": "child intent",
        "repo": str(repo),
        "worktree_path": str(child_worktree),
        "branch": "child",
        "worker": str(resolver_worker),
        "resolver_worker": str(resolver_worker),
        "base_commit": base,
        "parent_task_id": "parent",
        "provider_env_keys": [],
    }
    store = _Store()
    runtime = _runtime(session, store)
    runtime._resolver_child_enabled = True
    runtime.set_session_tasks([parent_spec, child_spec])
    conflict = {
        "status": "merge_conflict",
        "conflicted_files": ["base.txt"],
        "diff_evidence": "bounded conflict evidence",
        "diff_truncated": False,
        "integration_head": integration_head,
    }

    result = asyncio.run(
        runtime._resolve_merge_conflict(
            child_spec,
            WorkerHandle("child", 1),
            conflict,
            {"summary": "child intent", "status": "succeeded"},
            None,
        )
    )

    assert result is None
    assert _rev(repo, "main") == integration_head
    assert _rev(parent_worktree) != integration_head
    join_failures = [
        event for event in store.records if event["kind"] == "join_invariant_failed"
    ]
    assert join_failures
    assert not [event for event in store.records if event["kind"] == "resolver_succeeded"]
