from __future__ import annotations

import ast
import subprocess
from pathlib import Path

import pytest

from cambium.merge import GitError, MergeSequencer

MERGE_PATH = Path(__file__).resolve().parents[2] / "src" / "cambium" / "merge.py"


def test_merge_has_no_unbounded_exception_handlers() -> None:
    tree = ast.parse(MERGE_PATH.read_text(encoding="utf-8"))
    broad_handlers = [
        handler.lineno
        for handler in ast.walk(tree)
        if isinstance(handler, ast.ExceptHandler)
        and (
            handler.type is None
            or isinstance(handler.type, ast.Name)
            and handler.type.id in {"BaseException", "Exception"}
        )
    ]
    assert broad_handlers == []


def test_prepare_staging_rolls_back_after_git_worktree_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = tmp_path / "repo"
    staging = tmp_path / "staging"
    seq = MergeSequencer(task_id="git-failure")
    calls: list[tuple[str, ...]] = []

    monkeypatch.setattr(seq, "_rev_parse", lambda cwd, rev: "a" * 40)
    monkeypatch.setattr(seq, "_ensure_worker_tip", lambda repo_path, branch: "b" * 40)
    monkeypatch.setattr(seq, "_is_registered_worktree", lambda repo_path, path: False)

    def run_repo(
        repo_path: Path, *args: str, check: bool = True
    ) -> subprocess.CompletedProcess[str]:
        calls.append(args)
        if args[:2] == ("worktree", "add"):
            result = subprocess.CompletedProcess(["git", *args], 1, "", "failed")
            raise GitError(repo_path, list(args), result)
        return subprocess.CompletedProcess(["git", *args], 0, "", "")

    monkeypatch.setattr(seq, "_run_repo", run_repo)

    with pytest.raises(GitError):
        seq.prepare_staging(repo, staging, "worker", "main")

    assert calls[0][0] == "update-ref"
    assert calls[0][1].startswith("refs/cambium/staging/" + seq._task_key + "-")
    assert calls[1][:2] == ("worktree", "add")
    assert calls[2][:2] == ("update-ref", "-d")


def test_prepare_staging_does_not_rollback_unexpected_worktree_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = tmp_path / "repo"
    staging = tmp_path / "staging"
    seq = MergeSequencer(task_id="unexpected-failure")
    calls: list[tuple[str, ...]] = []

    monkeypatch.setattr(seq, "_rev_parse", lambda cwd, rev: "a" * 40)
    monkeypatch.setattr(seq, "_ensure_worker_tip", lambda repo_path, branch: "b" * 40)
    monkeypatch.setattr(seq, "_is_registered_worktree", lambda repo_path, path: False)

    def run_repo(
        repo_path: Path, *args: str, check: bool = True
    ) -> subprocess.CompletedProcess[str]:
        calls.append(args)
        if args[:2] == ("worktree", "add"):
            raise RuntimeError("unexpected failure")
        return subprocess.CompletedProcess(["git", *args], 0, "", "")

    monkeypatch.setattr(seq, "_run_repo", run_repo)

    with pytest.raises(RuntimeError, match="unexpected failure"):
        seq.prepare_staging(repo, staging, "worker", "main")

    assert not any(call[:2] == ("update-ref", "-d") for call in calls)


def test_cleanup_records_os_error_before_reraising(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seq = MergeSequencer(task_id="cleanup-error")
    seq._worktree_path = tmp_path / "staging"

    def fail_registration(repo: Path, worktree_path: Path) -> bool:
        raise OSError("worktree list unavailable")

    monkeypatch.setattr(seq, "_is_registered_worktree", fail_registration)

    with pytest.raises(OSError, match="worktree list unavailable"):
        seq.cleanup_staging(tmp_path / "repo")

    assert seq.drain_events() == [
        (
            "merge_staging_cleanup_failed",
            {"task": "cleanup-error", "staging_sha": "unknown", "reason": "OSError"},
        )
    ]
