"""Effect tests for the optional Architectus admission port."""

from __future__ import annotations

import asyncio
import json
import subprocess
from pathlib import Path

from cambium.architectus import ArchitectusCore, ScriptedLLM
from cambium.conversations import ConversationStore
from cambium.supervisor import read_events, run_plan
from cambium.tasktree import build_tree

TEST_RESOURCE_THRESHOLDS = {
    "mem_available_frac": 0.0,
    "load1_per_cpu": 1_000_000.0,
    "disk_free": 0,
}
FAKE_WORKER = str(Path(__file__).resolve().parents[2] / "scripts" / "fake_worker.py")


def _repo(path: Path) -> tuple[Path, str]:
    subprocess.run(["git", "init", "-b", "main", str(path)], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(path), "config", "user.name", "rp-test"], check=True)
    subprocess.run(["git", "-C", str(path), "config", "user.email", "rp@test"], check=True)
    subprocess.run(["git", "-C", str(path), "config", "gc.auto", "0"], check=True)
    for name in ("a.txt", "b.txt"):
        (path / name).write_text(f"file {name[0]}\n")
    subprocess.run(["git", "-C", str(path), "add", "-A"], check=True, capture_output=True)
    subprocess.run(
        ["git", "-C", str(path), "commit", "-m", "initial"],
        check=True,
        capture_output=True,
    )
    base = subprocess.run(
        ["git", "-C", str(path), "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    return path, base


def _task(session: Path, repo: Path, base: str, task_id: str, filename: str) -> dict:
    return {
        "task_id": task_id,
        "task": f"edit {filename}",
        "repo": str(repo),
        "worktree_path": str(session / f"wt-{task_id}"),
        "branch": f"wt-{task_id}",
        "worker": FAKE_WORKER,
        "target_file": filename,
        "marker": f"// {task_id}-marker",
        "write_marker": True,
        "base_commit": base,
        "provider_env_keys": ["FAKE_MODE"],
        "resource_thresholds": TEST_RESOURCE_THRESHOLDS,
        "max_restarts": 0,
    }


def _setup(tmp_path: Path) -> tuple[Path, Path, dict, dict, ArchitectusCore]:
    session = tmp_path / "session"
    repo, base = _repo(session / "repo")
    root = _task(session, repo, base, "root", "a.txt")
    child = _task(session, repo, base, "child", "b.txt")
    tree = build_tree(
        {
            "tasks": [
                {
                    "task_id": "root",
                    "kind": "FEATURE",
                    "depends_on": [],
                    "spec": {**root, "goal": "deliver the feature"},
                },
                {
                    "task_id": "child",
                    "kind": "FEATURE",
                    "depends_on": ["root"],
                    "spec": child,
                },
            ]
        }
    )
    core = ArchitectusCore(
        ScriptedLLM([{"action": "spawn", "task_id": "child"}]),
        tree=tree,
    )
    return session, repo, root, child, core


def _show(repo: Path, path: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), "show", f"main:{path}"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout


def test_port_admits_child_and_persists_decision(tmp_path: Path) -> None:
    session, repo, root, _child, core = _setup(tmp_path)

    result = asyncio.run(
        run_plan(session, {"tasks": [root]}, architectus=core, conversations=True)
    )

    assert result.exit_code == 0
    assert "// root-marker" in _show(repo, "a.txt")
    assert "// child-marker" in _show(repo, "b.txt")
    admitted = [event for event in read_events(session) if event["kind"] == "child_admitted"]
    assert len(admitted) == 1
    assert admitted[0]["payload"]["child_task_id"] == "child"

    store = ConversationStore(session / ".cambium" / "conversations.db")
    try:
        records = store.history("child")
    finally:
        store.close()
    assert len(records) == 1
    decision = json.loads(records[0]["content"])
    assert decision["outcome"] == "admitted"
    assert decision["child_task_id"] == "child"


def test_conversation_append_failure_is_visible(tmp_path: Path, monkeypatch) -> None:
    session, _repo_path, root, _child, core = _setup(tmp_path)
    monkeypatch.setenv("CAMBIUM_WARM_POOL_SIZE", "0")

    def fail_append(self, *args, **kwargs):
        raise RuntimeError("injected conversation append failure")

    monkeypatch.setattr(ConversationStore, "append", fail_append)

    result = asyncio.run(
        run_plan(session, {"tasks": [root]}, architectus=core, conversations=True)
    )

    assert result.exit_code != 0
    assert any(
        event["kind"] == "worker_failed" and event["task_id"] == "root"
        for event in read_events(session)
    )
