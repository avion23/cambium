"""Wave-3 admission identity and deduplication scenarios."""

from __future__ import annotations

import asyncio
import copy
import json
from pathlib import Path
from typing import Any

from _helpers_g11 import init_repo  # type: ignore[reportMissingImports]

from cambium.routing import DebtStore
from cambium.supervisor import (
    _release_lane,
    _Runtime,
    read_events,
    run_plan,
)


class _RunningTaskGroup:
    """Small task-group seam that keeps admitted child coroutines live."""

    def __init__(self) -> None:
        self.tasks: list[asyncio.Task[Any]] = []

    def create_task(self, coroutine: Any) -> None:
        self.tasks.append(asyncio.create_task(coroutine))


def _provider_config(path: Path, providers: list[dict[str, Any]]) -> Path:
    path.write_text(json.dumps({"providers": providers}), encoding="utf-8")
    return path


def _provider(name: str) -> dict[str, Any]:
    return {
        "name": name,
        "tier": "fast",
        "base_url": "http://127.0.0.1:1",
        "api_key_env": f"CAMBIUM_PROVIDER_{name.upper().replace('-', '_')}_API_KEY",
        "api_key": f"sk-admission-{name}",
        "model": "m1",
    }


def _parent(session_dir: Path, repo: Path, base: str, **extra: Any) -> dict[str, Any]:
    spec: dict[str, Any] = {
        "task_id": "parent",
        "task": "parent task",
        "repo": str(repo),
        "worktree_path": str(session_dir / "parent-wt"),
        "branch": "parent-branch",
        "base_commit": base,
    }
    spec.update(extra)
    return spec


def _proposal(
    session_dir: Path,
    repo: Path,
    base: str,
    child_id: str,
    **extra: Any,
) -> dict[str, Any]:
    child_spec: dict[str, Any] = {
        "task_id": child_id,
        "task": f"child task {child_id}",
        "repo": str(repo),
        "worktree_path": str(session_dir / f"{child_id}-wt"),
        "branch": f"{child_id}-branch",
        "base_commit": base,
    }
    child_spec.update(extra)
    return {
        "request_id": "wire-run-1",
        "parent_task_id": "parent",
        "child_task_id": child_id,
        "kind": "test",
        "spec": child_spec,
    }


def _parent_envelope() -> dict[str, Any]:
    return {
        "status": "succeeded",
        "summary": "parent complete",
    }


def test_same_task_generation_deduplicates_while_child_is_running(tmp_path: Path) -> None:
    session_dir = tmp_path / "session"
    repo, base = init_repo(tmp_path, "admission-test", "admission@test")
    provider_name = "ready-provider"
    provider_key = "CAMBIUM_PROVIDER_READY_PROVIDER_API_KEY"
    config = _provider_config(tmp_path / "providers.json", [_provider(provider_name)])
    parent = _parent(
        session_dir,
        repo,
        base,
        provider_env_keys=[provider_key],
        provider_config_path=str(config),
        authorized_providers=[provider_name],
        authorized_providers_explicit=True,
    )
    proposal = _proposal(
        session_dir,
        repo,
        base,
        "child",
        fanout_config={},
        model_candidates=["m1"],
        provider_config_path=str(config),
    )
    runtime = _Runtime(
        session_dir,
        None,
        provider_environment={provider_key: "usable-key"},
        debt_store=DebtStore(tmp_path / "routing.json"),
    )
    runtime.set_session_tasks([parent])
    group = _RunningTaskGroup()
    runtime._task_group = group  # type: ignore[assignment]
    events: list[dict[str, Any]] = []
    pin_started = asyncio.Event()
    pin_release = asyncio.Event()
    child_started = asyncio.Event()
    child_release = asyncio.Event()
    pin_calls = 0
    created_specs: list[dict[str, Any]] = []

    async def emit(kind: str, **payload: Any) -> None:
        events.append({"kind": kind, **payload})

    async def hold_pin(*args: Any, **kwargs: Any) -> None:
        nonlocal pin_calls
        pin_calls += 1
        pin_started.set()
        await pin_release.wait()

    async def supervise(child_spec: dict[str, Any]) -> None:
        runtime._resolve_assignment(child_spec)
        await runtime._ensure_worktree(child_spec)
        created_specs.append(child_spec)
        child_started.set()
        try:
            await child_release.wait()
        finally:
            _release_lane(runtime._lanes, child_spec)
            await runtime._prune_worktree(child_spec, force=True)

    runtime.emit = emit  # type: ignore[method-assign]
    runtime._pin_fork_child = hold_pin  # type: ignore[method-assign]
    runtime.supervise_task = supervise  # type: ignore[method-assign]

    async def scenario() -> None:
        first = asyncio.create_task(runtime._admit_child(parent, proposal, _parent_envelope()))
        await asyncio.wait_for(pin_started.wait(), timeout=5)
        duplicate = await runtime._admit_child(
            parent,
            copy.deepcopy(proposal),
            _parent_envelope(),
        )
        assert duplicate == []
        assert len(runtime._session_tasks) == 2
        pin_release.set()
        assert await asyncio.wait_for(first, timeout=5) == ["child"]
        await asyncio.wait_for(child_started.wait(), timeout=5)
        assert len(created_specs) == 1
        assert runtime._lanes[provider_name].in_flight == 1
        child_release.set()
        await asyncio.gather(*group.tasks)

    asyncio.run(scenario())

    admitted = [event for event in events if event["kind"] == "child_admitted"]
    rejected = [event for event in events if event["kind"] == "child_rejected"]
    assert pin_calls == 1
    assert len(admitted) == 1
    assert isinstance(admitted[0]["request_id"], str)
    assert admitted[0]["request_id"] != proposal["request_id"]
    assert len(rejected) == 1
    assert rejected[0]["request_id"] != admitted[0]["request_id"]
    assert rejected[0]["reason"] == "DuplicateTaskError"
    assert len(created_specs) == 1
    assert created_specs[0]["worktree_path"] == str(session_dir / "child-wt")
    assert runtime._lanes[provider_name].in_flight == 0


def test_different_child_tasks_are_admitted_with_distinct_request_ids(
    tmp_path: Path,
) -> None:
    session_dir = tmp_path / "session"
    repo, base = init_repo(tmp_path, "admission-test", "admission@test")
    parent = _parent(session_dir, repo, base)
    runtime = _Runtime(session_dir, None)
    runtime.set_session_tasks([parent])
    group = _RunningTaskGroup()
    runtime._task_group = group  # type: ignore[assignment]
    events: list[dict[str, Any]] = []
    release = asyncio.Event()
    created_specs: list[dict[str, Any]] = []

    async def emit(kind: str, **payload: Any) -> None:
        events.append({"kind": kind, **payload})

    async def supervise(child_spec: dict[str, Any]) -> None:
        created_specs.append(child_spec)
        await release.wait()

    runtime.emit = emit  # type: ignore[method-assign]
    runtime.supervise_task = supervise  # type: ignore[method-assign]

    async def scenario() -> list[list[str]]:
        results = await asyncio.gather(
            runtime._admit_child(
                parent,
                _proposal(session_dir, repo, base, "child-a"),
                _parent_envelope(),
            ),
            runtime._admit_child(
                parent,
                _proposal(session_dir, repo, base, "child-b"),
                _parent_envelope(),
            ),
        )
        release.set()
        await asyncio.gather(*group.tasks)
        return list(results)

    assert asyncio.run(scenario()) == [["child-a"], ["child-b"]]
    admitted = [event for event in events if event["kind"] == "child_admitted"]
    assert [event["child_task_id"] for event in admitted] == ["child-a", "child-b"]
    assert len({event["request_id"] for event in admitted}) == 2
    assert {spec["task_id"] for spec in created_specs} == {"child-a", "child-b"}
    assert {spec["worktree_path"] for spec in created_specs} == {
        str(session_dir / "child-a-wt"),
        str(session_dir / "child-b-wt"),
    }


def test_new_generation_request_is_not_lost_to_stale_generation_filter(
    tmp_path: Path,
) -> None:
    session_dir = tmp_path / "session"
    repo, base = init_repo(tmp_path, "admission-test", "admission@test")
    parent = _parent(session_dir, repo, base)
    runtime = _Runtime(session_dir, None)
    runtime.set_session_tasks([parent])
    runtime._task_group = _RunningTaskGroup()  # type: ignore[assignment]
    events: list[dict[str, Any]] = []

    async def emit(kind: str, **payload: Any) -> None:
        events.append({"kind": kind, **payload})

    async def supervise(_child_spec: dict[str, Any]) -> None:
        return None

    runtime.emit = emit  # type: ignore[method-assign]
    runtime.supervise_task = supervise  # type: ignore[method-assign]
    first_generation = _proposal(session_dir, repo, base, "child")
    second_generation = copy.deepcopy(first_generation)
    runtime._pending_children["parent"] = [
        (1, first_generation),
        (2, second_generation),
    ]

    async def scenario() -> list[str]:
        stale = runtime._take_generation_proposals("parent", 1)
        current = runtime._take_generation_proposals("parent", 2)
        assert stale == (first_generation,)
        assert current == (second_generation,)
        return await runtime._admit_generation_children(
            parent,
            _parent_envelope(),
            current,
            include_port=False,
        )

    assert asyncio.run(scenario()) == ["child"]
    assert len([event for event in events if event["kind"] == "child_admitted"]) == 1


def test_credential_feasible_admission_books_only_ready_provider(
    tmp_path: Path,
) -> None:
    missing = "missing-provider"
    ready = "ready-provider"
    config = _provider_config(
        tmp_path / "providers.json",
        [_provider(missing) | {"api_key": ""}, _provider(ready)],
    )
    spec = {
        "task_id": "candidate",
        "fanout_config": {},
        "model_candidates": ["m1"],
        "provider_config_path": str(config),
        "authorized_providers": [missing, ready],
        "authorized_providers_explicit": True,
    }
    runtime = _Runtime(
        tmp_path / "session",
        None,
        debt_store=DebtStore(tmp_path / "routing.json"),
    )

    runtime._resolve_assignment(spec)

    assert spec["assigned_provider"] == ready
    assert spec["authorized_providers"] == [ready]
    assert spec["_lane_reserved"] is True
    assert runtime._lanes[ready].in_flight == 1
    _release_lane(runtime._lanes, spec)
    assert runtime._lanes[ready].in_flight == 0


def test_empty_credential_feasible_set_fails_before_worker_spawn(
    tmp_path: Path,
) -> None:
    provider_name = "missing-provider"
    repo, base = init_repo(tmp_path, "admission-test", "admission@test")
    config = _provider_config(
        tmp_path / "providers.json", [_provider(provider_name) | {"api_key": ""}]
    )
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
        "authorized_providers": [provider_name],
        "authorized_providers_explicit": True,
        "max_restarts": 0,
    }

    result = asyncio.run(
        run_plan(
            session_dir,
            {"tasks": [task]},
        )
    )
    events = read_events(session_dir)

    assert result.results[0].reason == "no credential-feasible providers"
    assert not [event for event in events if event["kind"] == "spawned"]
    assert not [event for event in events if event["kind"] == "task_assigned"]
    infeasible = [event for event in events if event["kind"] == "provider_infeasible"]
    assert len(infeasible) == 1
    assert infeasible[0]["payload"] == {
        "provider": provider_name,
        "reason": "credential unavailable",
    }
