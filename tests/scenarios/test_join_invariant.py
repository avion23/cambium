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
