"""Focused supervisor regressions for delegation admission boundaries."""

from __future__ import annotations

import asyncio
import json
import subprocess
import textwrap
from pathlib import Path
from typing import Any

import pytest

from cambium.supervisor import (
    _resolve_model_candidates,
    _Runtime,
    _success_invariant_violation,
    read_events,
    run_plan,
)


class _MemoryStore:
    def __init__(self) -> None:
        self.records: list[dict[str, Any]] = []

    def append(self, record: dict[str, Any]) -> int:
        self.records.append(record)
        return len(self.records)


def _repo(tmp_path: Path) -> tuple[Path, str]:
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-b", "main", str(repo)], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.name", "supervisor-test"], check=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.email", "supervisor@test"], check=True)
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
    return repo, base


def _provider_config(path: Path, providers: list[dict[str, Any]]) -> Path:
    path.write_text(json.dumps({"providers": providers}), encoding="utf-8")
    return path


def _provider(name: str) -> dict[str, Any]:
    return {
        "name": name,
        "tier": "fast",
        "base_url": "http://127.0.0.1:1",
        "api_key_env": f"CAMBIUM_PROVIDER_{name.upper().replace('-', '_')}_API_KEY",
        "model": "m1",
    }


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


def test_unset_key_provider_is_skipped_and_persisted_as_infeasible(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    missing = "missing-provider"
    ready = "ready-provider"
    missing_env = "CAMBIUM_PROVIDER_MISSING_PROVIDER_API_KEY"
    ready_env = "CAMBIUM_PROVIDER_READY_PROVIDER_API_KEY"
    monkeypatch.delenv(missing_env, raising=False)
    config = _provider_config(tmp_path / "providers.json", [_provider(missing), _provider(ready)])
    spec = {
        "task_id": "candidate",
        "fanout_config": {},
        "model_candidates": ["m1"],
        "provider_config_path": str(config),
        "authorized_providers": [missing, ready],
        "authorized_providers_explicit": True,
    }

    assert _resolve_model_candidates(
        spec,
        {},
        {},
        provider_environment={ready_env: "usable-key"},
    )
    assert spec["assigned_provider"] == ready
    assert spec["authorized_providers"] == [ready]
    assert spec["_provider_infeasible"] == [(missing, "credential unavailable")]

    store = _MemoryStore()
    runtime = _Runtime(tmp_path / "session", store)
    asyncio.run(runtime._emit_provider_infeasible(spec))

    events = [record for record in store.records if record["kind"] == "provider_infeasible"]
    assert len(events) == 1
    assert events[0]["payload"] == {
        "provider": missing,
        "reason": "credential unavailable",
    }


@pytest.mark.parametrize(
    ("authorized_providers", "explicit"),
    [(["missing-provider"], True), ([], True)],
)
def test_no_credential_feasible_providers_fail_before_spawn(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    authorized_providers: list[str],
    explicit: bool,
) -> None:
    provider_name = "missing-provider"
    monkeypatch.delenv("CAMBIUM_PROVIDER_MISSING_PROVIDER_API_KEY", raising=False)
    repo, base = _repo(tmp_path)
    config = _provider_config(tmp_path / "providers.json", [_provider(provider_name)])
    session_dir = tmp_path / "session"
    task = {
        "task_id": "no-credentials",
        "task": "must not spawn",
        "repo": str(repo),
        "worktree_path": str(session_dir / "wt"),
        "branch": "no-credentials",
        "base_commit": base,
        "fanout_config": {},
        "model_candidates": ["m1"],
        "provider_config_path": str(config),
        "authorized_providers": authorized_providers,
        "authorized_providers_explicit": explicit,
        "max_restarts": 0,
    }

    result = asyncio.run(run_plan(session_dir, {"tasks": [task]}))
    events = read_events(session_dir)

    assert result.results[0].reason == "no credential-feasible providers"
    assert not [event for event in events if event["kind"] == "spawned"]
    if authorized_providers:
        infeasible = [event for event in events if event["kind"] == "provider_infeasible"]
        assert len(infeasible) == 1
        assert infeasible[0]["payload"]["provider"] == provider_name


def test_success_invariant_rejects_base_claim_when_commit_is_required() -> None:
    spec = {"base_commit": "base-head"}
    envelope = {
        "status": "succeeded",
        "commits": [],
        "files_changed": [],
        "diff": "",
        "requires_commit": True,
    }

    assert _success_invariant_violation(spec, envelope, "base-head")


def test_advanced_head_commit_mismatch_is_failed_and_retained(tmp_path: Path) -> None:
    repo, base = _repo(tmp_path)
    session_dir = tmp_path / "session"
    worker = tmp_path / "dishonest-worker.py"
    worker.write_text(
        textwrap.dedent(
            """
            import json
            import subprocess
            import sys
            from pathlib import Path

            def send(message):
                print(json.dumps(message), flush=True)

            init = json.loads(sys.stdin.readline())
            send({
                "type": "ready",
                "request_id": init["request_id"],
                "task_id": init["task_id"],
                "generation": init["generation"],
                "proto": 1,
            })
            run = json.loads(sys.stdin.readline())
            worktree = Path(run["worktree_path"])
            target = worktree / run["target_file"]
            target.write_text(target.read_text() + run["marker"] + "\\n", encoding="utf-8")
            subprocess.run(["git", "add", run["target_file"]], cwd=worktree, check=True)
            subprocess.run(
                ["git", "commit", "-m", "advanced"],
                cwd=worktree,
                check=True,
                capture_output=True,
            )
            send({
                "type": "result_envelope",
                "request_id": run["request_id"],
                "task_id": init["task_id"],
                "generation": init["generation"],
                "status": "succeeded",
                "commits": ["not-the-worktree-head"],
                "files_changed": [run["target_file"]],
                "diff": "reported diff",
                "requires_commit": True,
            })
            send({
                "type": "exit_message",
                "task_id": init["task_id"],
                "generation": init["generation"],
                "reason": "done",
            })
            """
        ),
        encoding="utf-8",
    )
    task = {
        "task_id": "invariant",
        "task": "report a truthful commit",
        "repo": str(repo),
        "worktree_path": str(session_dir / "wt"),
        "branch": "invariant",
        "base_commit": base,
        "worker": str(worker),
        "target_file": "a.txt",
        "marker": "// changed",
        "max_restarts": 0,
    }

    result = asyncio.run(run_plan(session_dir, {"tasks": [task]}))
    events = read_events(session_dir)

    assert result.results[0].status == "failed"
    assert result.results[0].reason == "success invariant violated"
    assert (session_dir / "wt").exists()
    assert not [event for event in events if event["kind"] == "merge_committed"]
    deferred = [event for event in events if event["kind"] == "worktree_cleanup_deferred"]
    assert len(deferred) == 1
    assert deferred[0]["payload"]["reason"] == "dirty"
