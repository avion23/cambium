"""Minimal check: primary worktree resync after publish (ponytail: one file)."""

from __future__ import annotations

import asyncio
import json
import subprocess
from pathlib import Path

import pytest

from cambium.state_view import load_state
from cambium.supervisor import run_plan

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
