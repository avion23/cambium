"""Focused supervisor regressions for delegation admission boundaries."""

from __future__ import annotations

import asyncio
import subprocess
from pathlib import Path
from typing import Any

from cambium.supervisor import _Runtime, run_plan


class _TaskGroup:
    def create_task(self, coroutine: Any) -> None:
        coroutine.close()


class _DecisionPort:
    def __init__(self) -> None:
        self.events: list[dict[str, Any]] = []

    def aggregate(self, _task_id: str, _envelope: dict[str, Any]) -> None:
        return None

    async def step(self, events: list[dict[str, Any]]) -> list[dict[str, Any]]:
        self.events.extend(events)
        return []


def _parent(session_dir: Path, repo: Path) -> dict[str, Any]:
    return {
        "task_id": "parent",
        "task": "parent task",
        "repo": str(repo),
        "worktree_path": str(session_dir / "parent-wt"),
        "branch": "parent-branch",
        "base_commit": "base",
    }


def _proposal(session_dir: Path, repo: Path, child_id: str) -> dict[str, Any]:
    return {
        "request_id": "run-1",
        "parent_task_id": "parent",
        "child_task_id": child_id,
        "kind": "test",
        "spec": {
            "task_id": child_id,
            "task": f"child task {child_id}",
            "repo": str(repo),
            "worktree_path": str(session_dir / f"{child_id}-wt"),
            "branch": f"{child_id}-branch",
            "base_commit": "base",
        },
    }


def test_empty_fanout_uses_provider_payload_boundary_and_keeps_marker_opt_in(
    tmp_path: Path,
) -> None:
    runtime = _Runtime(tmp_path, None)
    common = {
        "task_id": "task",
        "task": "do work",
        "repo": str(tmp_path / "repo"),
        "worktree_path": str(tmp_path / "wt"),
        "branch": "branch",
        "base_commit": "base",
        "target_file": "target.txt",
        "marker": "// marker",
    }

    provider_payload = runtime._run_payload({**common, "fanout_config": {}}, 1.0, 1)
    marker_payload = runtime._run_payload(common, 1.0, 1)

    assert "target_file" not in provider_payload
    assert "marker" not in provider_payload
    assert marker_payload["target_file"] == "target.txt"
    assert marker_payload["marker"] == "// marker"


def test_each_child_admission_gets_a_supervisor_request_id(tmp_path: Path) -> None:
    session_dir = tmp_path / "session"
    repo = tmp_path / "repo"
    parent = _parent(session_dir, repo)
    runtime = _Runtime(session_dir, None)
    runtime.set_session_tasks([parent])
    runtime._task_group = _TaskGroup()
    emitted: list[dict[str, Any]] = []

    async def emit(kind: str, **payload: Any) -> None:
        emitted.append({"kind": kind, **payload})

    async def no_pin(*_args: Any, **_kwargs: Any) -> None:
        return None

    async def no_conversation(*_args: Any, **_kwargs: Any) -> None:
        return None

    runtime.emit = emit  # type: ignore[method-assign]
    runtime._pin_fork_child = no_pin  # type: ignore[method-assign]
    runtime._record_revision_conversation = no_conversation  # type: ignore[method-assign]

    async def scenario() -> None:
        await runtime._admit_child(parent, _proposal(session_dir, repo, "child-a"), {})
        await runtime._admit_child(parent, _proposal(session_dir, repo, "child-b"), {})

    asyncio.run(scenario())
    request_ids = [
        event["request_id"] for event in emitted if event["kind"] == "child_admitted"
    ]

    assert len(request_ids) == 2
    assert len(set(request_ids)) == 2
    assert all(request_id != "run-1" for request_id in request_ids)


def test_failure_reason_reaches_decision_port_without_widening_child_envelope(
    tmp_path: Path,
) -> None:
    port = _DecisionPort()
    runtime = _Runtime(tmp_path, None, architectus=port)
    parent = _parent(tmp_path, tmp_path / "repo")
    parent_envelope = runtime._strict_envelope(
        parent,
        {
            "status": "failed",
            "failure_reason": "content_flagged: refusal",
        },
    )

    asyncio.run(
        runtime._admit_port_proposals(
            parent,
            parent_envelope,
            failure_reason="content_flagged: refusal",
            admit_proposals=False,
        )
    )

    assert "failure_reason" not in parent_envelope
    assert port.events[0]["payload"]["failure_reason"] == "content_flagged: refusal"


def test_failed_worker_reason_reaches_decision_port(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-b", "main", str(repo)], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.name", "supervisor-test"], check=True)
    subprocess.run(
        ["git", "-C", str(repo), "config", "user.email", "supervisor@test"], check=True
    )
    (repo / "a.txt").write_text("base\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(repo), "add", "a.txt"], check=True, capture_output=True)
    subprocess.run(
        ["git", "-C", str(repo), "commit", "-m", "initial"], check=True, capture_output=True
    )
    base = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    port = _DecisionPort()
    session_dir = tmp_path / "session"
    task = {
        "task_id": "root",
        "task": "fail with a structured reason",
        "repo": str(repo),
        "worktree_path": str(session_dir / "root-wt"),
        "branch": "root-branch",
        "worker": str(Path(__file__).parents[1] / "fixtures" / "hierarchy_worker.py"),
        "target_file": "a.txt",
        "marker": "// FAIL: reason",
        "write_marker": True,
        "base_commit": base,
        "max_restarts": 0,
    }

    result = asyncio.run(run_plan(session_dir, {"tasks": [task]}, architectus=port))

    assert result.results[0].status == "failed"
    assert port.events[0]["payload"]["failure_reason"] == "injected_hierarchy_failure"
