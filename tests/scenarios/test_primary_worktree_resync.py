"""Minimal check: primary worktree resync after publish (ponytail: one file)."""

from __future__ import annotations

import asyncio
import json
import subprocess
from pathlib import Path

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


def test_dirty_caller_edit_is_preserved_and_reported(tmp_path: Path) -> None:
    session_dir = tmp_path / "session"
    repo = session_dir / "repo"
    base = _make_repo(repo)
    (repo / "keep.txt").write_text("caller edit\n")
    events: list[dict] = []
    plan = {"tasks": [_task(session_dir, repo, base)]}
    asyncio.run(run_plan(session_dir, plan, on_event=events.append))
    assert (repo / "keep.txt").read_text() == "caller edit\n"
    stale = [e for e in events if e["kind"] == "main_worktree_stale"]
    assert len(stale) == 1
    record = json.loads((session_dir / ".cambium" / "result.json").read_text())
    assert "primary worktree stale" in record["summary"]
