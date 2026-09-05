from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

from cambium.supervisor import (
    DEFAULT_WALL_BUDGET_S,
    _child_spec,
    _prepare_child_budget,
    _Runtime,
)


class _Clock:
    def __init__(self, now: float) -> None:
        self.now = now

    def time(self) -> float:
        return self.now


class _TaskGroup:
    def __init__(self) -> None:
        self.tasks: list[asyncio.Task[Any]] = []

    def create_task(self, coroutine: Any) -> None:
        self.tasks.append(asyncio.create_task(coroutine))


def _parent(session_dir: Any, repo: Any) -> dict[str, Any]:
    return {
        "task_id": "parent",
        "task": "parent task",
        "repo": str(repo),
        "worktree_path": str(session_dir / "parent-wt"),
        "branch": "parent-branch",
        "base_commit": "base",
        "max_turns": 5,
        "max_wall_s": 120,
    }


def _proposal(session_dir: Any, repo: Any, *, budget: bool = True) -> dict[str, Any]:
    spec: dict[str, Any] = {
        "task": "child task",
        "repo": str(repo),
        "worktree_path": str(session_dir / "child-wt"),
        "branch": "child-branch",
        "base_commit": "base",
    }
    if budget:
        spec.update(max_turns=20, max_wall_s=240)
    return {
        "type": "propose_child",
        "request_id": "wire-request",
        "parent_task_id": "parent",
        "child_task_id": "child",
        "kind": "test",
        "spec": spec,
    }


def test_child_budget_is_clamped_at_proposal_and_forwarded_to_spawn_config(tmp_path) -> None:
    session_dir = tmp_path / "session"
    repo = tmp_path / "repo"
    parent = _parent(session_dir, repo)
    runtime = _Runtime(session_dir, None)
    runtime.set_session_tasks([parent])
    group = _TaskGroup()
    runtime._task_group = group  # type: ignore[assignment]
    events: list[dict[str, Any]] = []
    created_specs: list[dict[str, Any]] = []

    async def emit(kind: str, **payload: Any) -> None:
        events.append({"kind": kind, "payload": payload})

    async def no_pin(*_args: Any, **_kwargs: Any) -> None:
        return None

    async def no_conversation(*_args: Any, **_kwargs: Any) -> None:
        return None

    async def supervise(child_spec: dict[str, Any]) -> None:
        created_specs.append(child_spec)

    runtime.emit = emit  # type: ignore[method-assign]
    runtime._pin_fork_child = no_pin  # type: ignore[method-assign]
    runtime._record_revision_conversation = no_conversation  # type: ignore[method-assign]
    runtime.supervise_task = supervise  # type: ignore[method-assign]

    state = SimpleNamespace(
        task_id="parent",
        generation=1,
        spec=parent,
        turn=3,
        envelope=None,
        wall_deadline=125.0,
        loop=_Clock(25.0),
    )

    async def scenario() -> None:
        await runtime._handle_propose_child_message(state, _proposal(session_dir, repo))
        _, proposal = runtime._pending_children["parent"][0]
        assert proposal["_parent_budget"] == {"max_turns": 2, "max_wall_s": 100}
        assert await runtime._admit_child(parent, proposal, {}) == ["child"]
        await asyncio.gather(*group.tasks)

    asyncio.run(scenario())

    assert len(created_specs) == 1
    child_spec = created_specs[0]
    assert child_spec["max_turns"] == 2
    assert child_spec["max_wall_s"] == 100

    admitted = next(event for event in events if event["kind"] == "child_admitted")
    assert admitted["payload"]["budget"] == {
        "requested": {"max_turns": 20, "max_wall_s": 240},
        "admitted": {"max_turns": 2, "max_wall_s": 100},
        "parent_remaining": {"max_turns": 2, "max_wall_s": 100},
        "clamped": ["max_turns", "max_wall_s"],
    }

    runtime = _Runtime(session_dir, None)
    _request_id, init = runtime._build_generation_init_message(
        child_spec,
        session_dir / "child-wt",
        "child",
        1,
        15.0,
        90.0,
        100.0,
    )
    run_payload = runtime._run_payload(child_spec, 100.0, 1)
    assert init["max_turns"] == 2
    assert init["budget"]["max_wall_s"] == 100.0
    assert run_payload["max_turns"] == 2
    assert run_payload["max_wall_s"] == 100.0


def test_absent_child_budget_inherits_parent_limits(tmp_path, monkeypatch) -> None:
    monkeypatch.delenv("CAMBIUM_WALL_BUDGET_S", raising=False)
    session_dir = tmp_path / "session"
    parent = _parent(session_dir, tmp_path / "repo")
    proposal = _proposal(session_dir, tmp_path / "repo", budget=False)

    prepared, decision = _prepare_child_budget(parent, proposal)
    child_spec = _child_spec(session_dir, parent, prepared, {})
    runtime = _Runtime(session_dir, None)
    _request_id, init = runtime._build_generation_init_message(
        child_spec,
        session_dir / "child-wt",
        "child",
        1,
        15.0,
        90.0,
        DEFAULT_WALL_BUDGET_S,
    )
    run_payload = runtime._run_payload(child_spec, DEFAULT_WALL_BUDGET_S, 1)

    assert decision is None
    assert child_spec["max_turns"] == parent["max_turns"]
    assert child_spec["max_wall_s"] == parent["max_wall_s"]
    assert init["max_turns"] == parent["max_turns"]
    assert init["budget"]["max_wall_s"] == DEFAULT_WALL_BUDGET_S
    assert run_payload["max_turns"] == parent["max_turns"]
    assert run_payload["max_wall_s"] == DEFAULT_WALL_BUDGET_S
