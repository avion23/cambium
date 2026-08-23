"""Revision-boundary decision port and conversation persistence scenarios
(implementation-plan step 2, items 23-24).

A caller-provided ``ArchitectusCore`` (or an ``aggregate``/``step`` adapter)
is the ONLY provider-side way a parent's response becomes a child proposal:
each admitted parent's terminal envelope feeds ``core.aggregate``/``core.step``
and the resulting typed proposals are routed through the existing
``_admit_child`` revision validation (never the live tree directly). Every
admitted/rejected revision is additionally persisted through
``ConversationStore`` at ``<session_dir>/.cambium/conversations.db`` (one
``kind="system"`` row per revision, node_id = child task id, parent task in
``meta``). Both backends are optional; with neither configured, ``run_plan``
is byte-for-byte the historical behavior.

Scenarios:
  RP1 an injected ``ArchitectusCore`` (``ScriptedLLM``) drives one valid child
      admission through the existing ``child_admitted`` path with a
      conversation row persisted.
  RP2 a malformed proposal from the port (a spawn for an unknown task id) is
      durably rejected with ``child_rejected`` and spawns nothing.
  RP3 a port proposal whose spec is not a valid task spec is durably rejected
      with ``child_rejected`` and spawns nothing.
  RP4 the port and conversation store are optional: a default fanout run
      behaves exactly as before (no conversation db, no child events).
  RP5 a conversation store open failure raises (no silent success).
  RP6 a conversation store append failure surfaces visibly (no silent success).
  RP7 the public ``Orchestrator`` caller forwards the port and conversation
      flag to ``run_plan`` (production caller wiring).
"""

from __future__ import annotations

import asyncio
import json
import subprocess
from pathlib import Path
from typing import Any

import pytest

from cambium.architectus import ArchitectusCore, ScriptedLLM
from cambium.conversations import ConversationStore, ConversationStoreInitError
from cambium.supervisor import read_events, run_plan
from cambium.tasktree import build_tree

TEST_RESOURCE_THRESHOLDS = {
    "mem_available_frac": 0.0,
    "load1_per_cpu": 1_000_000.0,
    "disk_free": 0,
}


def _make_repo(repo: Path, files: dict[str, str]) -> str:
    subprocess.run(["git", "init", "-b", "main", str(repo)], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.name", "rp-test"], check=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.email", "rp@test"], check=True)
    subprocess.run(["git", "-C", str(repo), "config", "gc.auto", "0"], check=True)
    for name, content in files.items():
        (repo / name).write_text(content)
    subprocess.run(["git", "-C", str(repo), "add", "-A"], check=True, capture_output=True)
    subprocess.run(
        ["git", "-C", str(repo), "commit", "-m", "initial"],
        check=True,
        capture_output=True,
    )
    return subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "HEAD"], check=True, capture_output=True, text=True
    ).stdout.strip()


def _task(
    session_dir: Path,
    repo: Path,
    base: str,
    task_id: str,
    *,
    worktree: str,
    branch: str,
    target_file: str,
    marker: str,
    **extra,
) -> dict:
    spec = {
        "task_id": task_id,
        "task": f"edit {target_file}",
        "repo": str(repo),
        "worktree_path": str(session_dir / worktree),
        "branch": branch,
        "worker": "cambium.worker",
        "target_file": target_file,
        "marker": marker,
        "write_marker": True,
        "base_commit": base,
        "provider_env_keys": ["FAKE_MODE"],
        "resource_thresholds": TEST_RESOURCE_THRESHOLDS,
    }
    spec.update(extra)
    return spec


def _kinds(events: list[dict], kind: str) -> list[dict]:
    return [e for e in events if e["kind"] == kind]


def _show(repo: Path, ref: str, path: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), "show", f"{ref}:{path}"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout


def _core_tree(root_spec: dict, *child_specs: dict) -> Any:
    """Build the ArchitectusCore frozen tree: root + candidate children."""
    nodes = [
        {
            "task_id": root_spec["task_id"],
            "kind": "FEATURE",
            "depends_on": [],
            "spec": dict(root_spec, goal="deliver the feature"),
        }
    ]
    for child in child_specs:
        nodes.append(
            {
                "task_id": child["task_id"],
                "kind": "FEATURE",
                "depends_on": [root_spec["task_id"]],
                "spec": child,
            }
        )
    return build_tree({"tasks": nodes})


def _conversation_records(session_dir: Path, node_id: str) -> list[dict]:
    store = ConversationStore(session_dir / ".cambium" / "conversations.db")
    try:
        return store.history(node_id)
    finally:
        store.close()


# ---------------------------------------------------------------------------
# RP1: an injected ArchitectusCore drives one valid child admission through
# the existing child_admitted path, with a conversation row persisted.
# ---------------------------------------------------------------------------


def test_rp1_port_drives_valid_child_admission_with_conversation_row(tmp_path) -> None:
    session_dir = tmp_path / "session"
    repo = session_dir / "repo"
    base = _make_repo(repo, {"a.txt": "file a\n", "b.txt": "file b\n"})
    root = _task(
        session_dir,
        repo,
        base,
        "t-root",
        worktree="wt-root",
        branch="wt-root",
        target_file="a.txt",
        marker="// root-marker",
    )
    child = _task(
        session_dir,
        repo,
        base,
        "c1",
        worktree="wt-c1",
        branch="wt-c1",
        target_file="b.txt",
        marker="// child-marker",
    )
    core = ArchitectusCore(
        ScriptedLLM([{"action": "spawn", "task_id": "c1"}]),
        tree=_core_tree(root, child),
    )

    result = asyncio.run(
        run_plan(session_dir, {"tasks": [root]}, architectus=core, conversations=True)
    )

    assert result.exit_code == 0
    assert {r.task_id for r in result.results} == {"t-root", "c1"}
    assert all(r.status == "succeeded" for r in result.results)
    assert "// root-marker" in _show(repo, "main", "a.txt")
    assert "// child-marker" in _show(repo, "main", "b.txt")

    events = read_events(session_dir)
    admitted = _kinds(events, "child_admitted")
    assert len(admitted) == 1
    assert admitted[0]["payload"]["parent_task_id"] == "t-root"
    assert admitted[0]["payload"]["child_task_id"] == "c1"
    assert not _kinds(events, "child_rejected")

    records = _conversation_records(session_dir, "c1")
    assert len(records) == 1
    assert records[0]["node_id"] == "c1"
    assert records[0]["kind"] == "system"
    assert records[0]["meta"] == {"parent_task_id": "t-root"}
    content = json.loads(records[0]["content"])
    assert content["outcome"] == "admitted"
    assert content["parent_task_id"] == "t-root"
    assert content["child_task_id"] == "c1"
    assert content["proposal"]["parent_task_id"] == "t-root"
    assert content["proposal"]["child_task_id"] == "c1"
    assert content["proposal"]["kind"] == "feature"
    assert content["proposal"]["spec"]["marker"] == "// child-marker"


# ---------------------------------------------------------------------------
# RP2: a malformed proposal from the port (spawn for an unknown task id) is
# durably rejected with child_rejected and spawns nothing.
# ---------------------------------------------------------------------------


def test_rp2_port_malformed_proposal_rejected_no_spawn(tmp_path) -> None:
    session_dir = tmp_path / "session"
    repo = session_dir / "repo"
    base = _make_repo(repo, {"a.txt": "file a\n"})
    root = _task(
        session_dir,
        repo,
        base,
        "t-root",
        worktree="wt-root",
        branch="wt-root",
        target_file="a.txt",
        marker="// root-marker",
    )
    core = ArchitectusCore(
        ScriptedLLM([{"action": "spawn", "task_id": "ghost-child"}]),
        tree=_core_tree(root),
    )

    result = asyncio.run(run_plan(session_dir, {"tasks": [root]}, architectus=core))

    assert result.exit_code == 0
    assert [r.task_id for r in result.results] == ["t-root"]
    assert result.results[0].status == "succeeded"
    assert "// root-marker" in _show(repo, "main", "a.txt")

    events = read_events(session_dir)
    rejected = _kinds(events, "child_rejected")
    assert len(rejected) == 1
    assert rejected[0]["task_id"] == "t-root"
    assert rejected[0]["payload"]["reason"] == "MalformedProposal"
    assert not _kinds(events, "child_admitted")
    spawned = _kinds(events, "spawned")
    assert {e["task_id"] for e in spawned} == {"t-root"}
    assert not (session_dir / "wt-ghost-child").exists()


# ---------------------------------------------------------------------------
# RP3: a port proposal whose spec is not a valid task spec is durably rejected
# with child_rejected and spawns nothing; the rejection row is persisted.
# ---------------------------------------------------------------------------


def test_rp3_port_invalid_child_spec_rejected_no_spawn(tmp_path) -> None:
    session_dir = tmp_path / "session"
    repo = session_dir / "repo"
    base = _make_repo(repo, {"a.txt": "file a\n"})
    root = _task(
        session_dir,
        repo,
        base,
        "t-root",
        worktree="wt-root",
        branch="wt-root",
        target_file="a.txt",
        marker="// root-marker",
    )
    malformed_child = {"task_id": "c1"}  # missing repo/worktree_path/branch/task
    core = ArchitectusCore(
        ScriptedLLM([{"action": "spawn", "task_id": "c1"}]),
        tree=_core_tree(root, malformed_child),
    )

    result = asyncio.run(
        run_plan(session_dir, {"tasks": [root]}, architectus=core, conversations=True)
    )

    assert result.exit_code == 0
    assert [r.task_id for r in result.results] == ["t-root"]
    assert result.results[0].status == "succeeded"

    events = read_events(session_dir)
    rejected = _kinds(events, "child_rejected")
    assert len(rejected) == 1
    assert rejected[0]["payload"]["parent_task_id"] == "t-root"
    assert rejected[0]["payload"]["child_task_id"] == "c1"
    assert rejected[0]["payload"]["reason"] == "ValueError"
    assert not _kinds(events, "child_admitted")
    spawned = _kinds(events, "spawned")
    assert {e["task_id"] for e in spawned} == {"t-root"}
    assert not (session_dir / "wt-c1").exists()

    records = _conversation_records(session_dir, "c1")
    assert len(records) == 1
    assert records[0]["meta"] == {"parent_task_id": "t-root"}
    content = json.loads(records[0]["content"])
    assert content["outcome"] == "rejected"
    assert content["reason"] == "ValueError"
    assert content["proposal"]["child_task_id"] == "c1"


# ---------------------------------------------------------------------------
# RP4: the port and conversation store are optional — a default fanout run
# behaves exactly as before (no conversation db, no child events).
# ---------------------------------------------------------------------------


def test_rp4_port_and_conversations_optional_by_default(tmp_path) -> None:
    session_dir = tmp_path / "session"
    repo = session_dir / "repo"
    base = _make_repo(repo, {"a.txt": "file a\n", "b.txt": "file b\n"})
    plan = {
        "tasks": [
            _task(
                session_dir,
                repo,
                base,
                "t-a",
                worktree="wt-a",
                branch="wt-a",
                target_file="a.txt",
                marker="// cambium-a",
            ),
            _task(
                session_dir,
                repo,
                base,
                "t-b",
                worktree="wt-b",
                branch="wt-b",
                target_file="b.txt",
                marker="// cambium-b",
            ),
        ]
    }

    result = asyncio.run(run_plan(session_dir, plan))

    assert result.exit_code == 0
    assert {r.task_id for r in result.results} == {"t-a", "t-b"}
    assert all(r.status == "succeeded" for r in result.results)
    assert "// cambium-a" in _show(repo, "main", "a.txt")
    assert "// cambium-b" in _show(repo, "main", "b.txt")
    events = read_events(session_dir)
    assert not _kinds(events, "child_admitted")
    assert not _kinds(events, "child_rejected")
    assert not (session_dir / ".cambium" / "conversations.db").exists()


# ---------------------------------------------------------------------------
# RP5: a conversation store open failure raises — no silent success.
# ---------------------------------------------------------------------------


def test_rp5_conversation_store_open_failure_raises(tmp_path) -> None:
    session_dir = tmp_path / "session"
    repo = session_dir / "repo"
    base = _make_repo(repo, {"a.txt": "file a\n"})
    root = _task(
        session_dir,
        repo,
        base,
        "t-root",
        worktree="wt-root",
        branch="wt-root",
        target_file="a.txt",
        marker="// root-marker",
    )
    (session_dir / ".cambium").mkdir(parents=True)
    (session_dir / ".cambium" / "conversations.db").mkdir()

    with pytest.raises(ConversationStoreInitError):
        asyncio.run(run_plan(session_dir, {"tasks": [root]}, conversations=True))
    assert not (session_dir / "wt-root").exists()


# ---------------------------------------------------------------------------
# RP6: a conversation store append failure surfaces visibly — no silent
# success; the session fails loudly instead of swallowing the store error.
# ---------------------------------------------------------------------------


def test_rp6_conversation_append_failure_is_visible(tmp_path, monkeypatch) -> None:
    session_dir = tmp_path / "session"
    repo = session_dir / "repo"
    base = _make_repo(repo, {"a.txt": "file a\n", "b.txt": "file b\n"})
    root = _task(
        session_dir,
        repo,
        base,
        "t-root",
        worktree="wt-root",
        branch="wt-root",
        target_file="a.txt",
        marker="// root-marker",
        max_restarts=0,
    )
    child = _task(
        session_dir,
        repo,
        base,
        "c1",
        worktree="wt-c1",
        branch="wt-c1",
        target_file="b.txt",
        marker="// child-marker",
        max_restarts=0,
    )
    # The append failure interrupts the parent's result handling before its
    # reuse_ready is read, so the supervise finally would otherwise wait the
    # full WORKER_EXIT_WAIT_S for a pooled worker that never exits. The pool's
    # own behavior is covered in test_worker_pool.py.
    monkeypatch.setenv("CAMBIUM_WARM_POOL_SIZE", "0")
    core = ArchitectusCore(
        ScriptedLLM([{"action": "spawn", "task_id": "c1"}]),
        tree=_core_tree(root, child),
    )

    def fail_append(self, *args, **kwargs):
        raise RuntimeError("injected conversation append failure")

    monkeypatch.setattr(ConversationStore, "append", fail_append)

    result = asyncio.run(
        run_plan(session_dir, {"tasks": [root]}, architectus=core, conversations=True)
    )

    assert result.exit_code != 0
    events = read_events(session_dir)
    worker_failed = _kinds(events, "worker_failed")
    assert any(e["task_id"] == "t-root" for e in worker_failed)


# ---------------------------------------------------------------------------
# RP7: the public Orchestrator caller forwards the decision port and the
# conversation flag to run_plan (production caller for the revision path).
# ---------------------------------------------------------------------------


def test_rp7_orchestrator_forwards_port_and_conversations(tmp_path) -> None:
    session_dir = tmp_path / "session"
    repo = session_dir / "repo"
    base = _make_repo(repo, {"a.txt": "file a\n", "b.txt": "file b\n"})
    root = _task(
        session_dir,
        repo,
        base,
        "t-root",
        worktree="wt-root",
        branch="wt-root",
        target_file="a.txt",
        marker="// root-marker",
    )
    child = _task(
        session_dir,
        repo,
        base,
        "c1",
        worktree="wt-c1",
        branch="wt-c1",
        target_file="b.txt",
        marker="// child-marker",
    )
    core = ArchitectusCore(
        ScriptedLLM([{"action": "spawn", "task_id": "c1"}]),
        tree=_core_tree(root, child),
    )
    from cambium.orchestrator import Orchestrator

    result = asyncio.run(
        Orchestrator(architectus=core, conversations=True).run(str(session_dir), {"tasks": [root]})
    )

    assert result.exit_code == 0
    assert {r.task_id for r in result.results} == {"t-root", "c1"}
    assert all(r.status == "succeeded" for r in result.results)
    events = read_events(session_dir)
    assert len(_kinds(events, "child_admitted")) == 1
    assert not _kinds(events, "child_rejected")
    assert _conversation_records(session_dir, "c1")
    assert (session_dir / ".cambium" / "conversations.db").exists()
