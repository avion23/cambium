"""Minimal check: primary worktree resync after publish (ponytail: one file)."""

from __future__ import annotations

import asyncio
import json
import subprocess
from pathlib import Path

import pytest

from cambium import supervisor
from cambium.state_view import load_state
from cambium.supervisor import read_events, run_plan

ROOT = Path(__file__).resolve().parents[2]
WORKER = str(ROOT / "scripts" / "fake_worker.py")
THRESHOLDS = {"mem_available_frac": 0.0, "load1_per_cpu": 1_000_000.0, "disk_free": 0}


def _make_repo(repo: Path) -> str:
    subprocess.run(["git", "init", "-b", "main", str(repo)], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.name", "t"], check=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.email", "t@t"], check=True)
    (repo / "target.txt").write_text("base\n")
    (repo / "keep.txt").write_text("keep\n")
    subprocess.run(["git", "-C", str(repo), "add", "-A"], check=True, capture_output=True)
    subprocess.run(
        ["git", "-C", str(repo), "commit", "-m", "init"],
        check=True,
        capture_output=True,
    )
    return subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def _task(session_dir: Path, repo: Path, base: str) -> dict:
    return {
        "task_id": "task",
        "task": "edit target.txt",
        "repo": str(repo),
        "worktree_path": str(session_dir / "wt"),
        "branch": "work",
        "worker": WORKER,
        "target_file": "target.txt",
        "marker": "merged-marker",
        "write_marker": True,
        "base_commit": base,
        "provider_env_keys": ["FAKE_MODE"],
        "resource_thresholds": THRESHOLDS,
    }


def _status(repo: Path) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), "status", "--porcelain=v1", "--untracked-files=all"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def test_clean_primary_worktree_is_refreshed(tmp_path: Path) -> None:
    session_dir = tmp_path / "session"
    repo = session_dir / "repo"
    base = _make_repo(repo)
    events: list[dict] = []
    plan = {"tasks": [_task(session_dir, repo, base)]}
    asyncio.run(run_plan(session_dir, plan, on_event=events.append))
    assert "merged-marker" in (repo / "target.txt").read_text()
    assert _status(repo) == ""
    assert [e for e in events if e["kind"] == "main_worktree_stale"] == []


def test_ignored_cambium_cache_does_not_block_clean_resync(tmp_path: Path) -> None:
    session_dir = tmp_path / "session-cache"
    repo = session_dir / "repo"
    base = _make_repo(repo)
    exclude = repo / ".git" / "info" / "exclude"
    exclude.write_text(exclude.read_text() + "\n.cambium/\n", encoding="utf-8")
    cache = repo / ".cambium" / "cache"
    cache.mkdir(parents=True)
    (cache / "state.json").write_text("{}\n", encoding="utf-8")
    events: list[dict] = []

    asyncio.run(
        run_plan(
            session_dir,
            {"tasks": [_task(session_dir, repo, base)]},
            on_event=events.append,
        )
    )

    assert "merged-marker" in (repo / "target.txt").read_text()
    assert (cache / "state.json").read_text() == "{}\n"
    assert [e for e in events if e["kind"] == "main_worktree_stale"] == []


def test_caller_mutation_after_snapshot_is_preserved_and_reported(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    session_dir = tmp_path / "session-late-mutation"
    repo = session_dir / "repo"
    base = _make_repo(repo)
    original_snapshot = supervisor._Runtime._snapshot_primary_worktree

    async def snapshot_then_mutate(runtime: supervisor._Runtime, target: Path) -> dict | None:
        snapshot = await original_snapshot(runtime, target)
        (target / "keep.txt").write_text("late caller edit\n")
        return snapshot

    monkeypatch.setattr(
        supervisor._Runtime,
        "_snapshot_primary_worktree",
        snapshot_then_mutate,
    )
    events: list[dict] = []

    asyncio.run(
        run_plan(
            session_dir,
            {"tasks": [_task(session_dir, repo, base)]},
            on_event=events.append,
        )
    )

    assert (repo / "keep.txt").read_text() == "late caller edit\n"
    assert (repo / "target.txt").read_text() == "base\n"
    published = subprocess.run(
        ["git", "-C", str(repo), "show", "refs/heads/main:target.txt"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    assert "merged-marker" in published
    stale = [event for event in events if event["kind"] == "main_worktree_stale"]
    assert len(stale) == 1
    assert stale[0]["payload"]["reason"] == "caller_changes_after_snapshot"
    assert "keep.txt" in stale[0]["payload"]["files_changed"]


def test_branch_switch_after_snapshot_preserves_the_new_checkout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    session_dir = tmp_path / "session-branch-switch"
    repo = session_dir / "repo"
    base = _make_repo(repo)
    original_snapshot = supervisor._Runtime._snapshot_primary_worktree

    async def snapshot_then_switch(runtime: supervisor._Runtime, target: Path) -> dict | None:
        snapshot = await original_snapshot(runtime, target)
        subprocess.run(
            ["git", "-C", str(target), "switch", "-c", "caller-branch"],
            check=True,
            capture_output=True,
        )
        return snapshot

    monkeypatch.setattr(
        supervisor._Runtime,
        "_snapshot_primary_worktree",
        snapshot_then_switch,
    )
    events: list[dict] = []

    asyncio.run(
        run_plan(
            session_dir,
            {"tasks": [_task(session_dir, repo, base)]},
            on_event=events.append,
        )
    )

    branch = subprocess.run(
        ["git", "-C", str(repo), "branch", "--show-current"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    assert branch == "caller-branch"
    assert (repo / "target.txt").read_text() == "base\n"
    stale = [event for event in events if event["kind"] == "main_worktree_stale"]
    assert len(stale) == 1
    assert stale[0]["payload"]["reason"] == "branch_changed"


def test_read_tree_failure_keeps_published_ref_and_reports_durable_stale_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    session_dir = tmp_path / "session-read-tree-failure"
    repo = session_dir / "repo"
    base = _make_repo(repo)
    original_git = supervisor._Runtime._git

    async def fail_read_tree(
        runtime: supervisor._Runtime,
        target: Path,
        *args: str,
        check: bool = True,
    ) -> subprocess.CompletedProcess[str]:
        if args and args[0] == "read-tree":
            return subprocess.CompletedProcess(
                ["git", "-C", str(target), *args],
                1,
                "",
                "simulated read-tree failure",
            )
        return await original_git(runtime, target, *args, check=check)

    monkeypatch.setattr(supervisor._Runtime, "_git", fail_read_tree)

    asyncio.run(run_plan(session_dir, {"tasks": [_task(session_dir, repo, base)]}))

    assert (repo / "target.txt").read_text() == "base\n"
    published = subprocess.run(
        ["git", "-C", str(repo), "show", "refs/heads/main:target.txt"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    assert "merged-marker" in published
    durable = [
        event for event in read_events(session_dir) if event["kind"] == "main_worktree_stale"
    ]
    assert len(durable) == 1
    assert durable[0]["payload"]["reason"] == "read_tree_failed"
    record = json.loads((session_dir / ".cambium" / "result.json").read_text())
    assert "main worktree is stale" in record["summary"]
    state = load_state(session_dir, "task")
    assert state.result is not None
    assert state.result.summary == durable[0]["payload"]["summary"]


@pytest.mark.parametrize("dirty_kind", ["staged", "unstaged", "untracked", "ignored"])
def test_dirty_caller_state_is_preserved_and_reported(tmp_path: Path, dirty_kind: str) -> None:
    session_dir = tmp_path / f"session-{dirty_kind}"
    repo = session_dir / "repo"
    base = _make_repo(repo)
    caller_path = "caller.txt" if dirty_kind in {"untracked", "ignored"} else "keep.txt"
    caller_file = repo / caller_path
    if dirty_kind in {"untracked", "ignored"}:
        if dirty_kind == "ignored":
            exclude = repo / ".git" / "info" / "exclude"
            exclude.write_text(exclude.read_text() + "\ncaller.txt\n", encoding="utf-8")
        caller_file.write_text("caller edit\n")
    else:
        caller_file.write_text("caller edit\n")
        if dirty_kind == "staged":
            subprocess.run(["git", "-C", str(repo), "add", caller_path], check=True)
    events: list[dict] = []
    plan = {"tasks": [_task(session_dir, repo, base)]}

    asyncio.run(run_plan(session_dir, plan, on_event=events.append))

    assert caller_file.read_text() == "caller edit\n"
    stale = [e for e in events if e["kind"] == "main_worktree_stale"]
    assert len(stale) == 1
    payload = stale[0]["payload"]
    assert caller_path in payload["files_changed"]
    record = json.loads((session_dir / ".cambium" / "result.json").read_text())
    assert caller_path in record["files_changed"]
    assert "main worktree is stale" in record["summary"]
    assert "content matches HEAD exactly" not in record["summary"]
    state = load_state(session_dir, "task")
    assert state.result is not None
    assert caller_path in state.result.files_changed
    assert state.result.summary == payload["summary"]
