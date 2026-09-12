from __future__ import annotations

import asyncio
from dataclasses import asdict, replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from cambium import worker
from cambium.child_policy import ChildPolicyError, parse_child_policy
from cambium.redact import Redactor
from cambium.routing import LaneCapacityExhausted, LaneState
from cambium.summary_trunk import SummaryEntry, append_summary_entry
from cambium.supervisor import _Runtime
from cambium.worker import _provider_task_tools_hash


def _checkpoint_epoch(
    session_dir: Path,
    *,
    redacted: bool = False,
    raw_tail: bool = False,
) -> dict[str, Any]:
    """Persist one strict parent checkpoint and expose supervisor epoch metadata."""
    config = replace(
        worker._PROVIDER_TOOLS_CONFIG,
        task_id="parent",
        generation=1,
        checkpoint_root=session_dir / ".cambium" / "checkpoints",
        redactor=Redactor(secret_values={"SECRETXYZ"}) if redacted else None,
    )
    messages: list[dict[str, str]] = [
        {"role": "system", "content": "You are the agent."},
        {"role": "user", "content": "<cambium-task>parent task</cambium-task>"},
    ]
    if raw_tail:
        messages.extend(
            [
                {"role": "assistant", "content": "raw assistant output"},
                {"role": "user", "content": "raw tool output"},
            ]
        )
    else:
        messages = append_summary_entry(
            messages,
            SummaryEntry(
                type="summary_entry",
                sequence=1,
                source_sha256="c" * 64,
                source_message_count=1,
                through_turn=1,
                objective="preserve parent context",
                outcome="captured SECRETXYZ" if redacted else "captured parent evidence",
                decisions_added=(),
                decisions_superseded=(),
                facts_added=(),
                facts_invalidated=(),
                files_and_symbols_changed=(),
                verification_results=(),
                relevant_failed_approaches=(),
                open_items=(),
            ),
        )
    checkpoint = worker._write_epoch_checkpoint(
        config,
        turn=1,
        epoch=2,
        messages=messages,
        provider="provider-a",
        model="model-a",
        tools_sha256=_provider_task_tools_hash(),
        provider_compat={"provider-a": ("http", "high")},
        created_at=1.0,
        wall_deadline=10.0,
    )
    assert checkpoint is not None
    return {
        "epoch": checkpoint.epoch,
        "checkpoint_ref": checkpoint.checkpoint_ref,
        "cache_key": asdict(checkpoint.cache_key),
    }


def _runtime(tmp_path: Path) -> tuple[_Runtime, list[dict[str, Any]]]:
    runtime = _Runtime(tmp_path, None)
    events: list[dict[str, Any]] = []

    async def emit(kind: str, **payload: Any) -> None:
        events.append({"kind": kind, **payload})

    runtime.emit = emit  # type: ignore[method-assign]
    runtime._task_epochs["parent"] = _checkpoint_epoch(tmp_path)
    return runtime, events


def test_semantic_child_pins_summary_trunk_and_drops_provider(tmp_path: Path) -> None:
    runtime, events = _runtime(tmp_path)

    child_spec: dict[str, Any] = {
        "context_mode": "semantic",
        "placement": "spread",
        "fanout_config": {},
        "authorized_providers": ["provider-a", "provider-b"],
    }

    asyncio.run(runtime._pin_fork_child(child_spec, "parent", "child", "investigation"))

    # Semantic (incompatible by construction) sets summary_trunk_ref
    # and drops assigned_provider so the child picks a fresh provider.
    assert child_spec.get("summary_trunk_ref") == runtime._task_epochs["parent"]["checkpoint_ref"]
    assert "assigned_provider" not in child_spec
    assert "context_fork" not in child_spec
    assert child_spec["_required_context_mode"] == "semantic"

    # The context_fork event carries the semantic_reuse flag.
    fork_events = [e for e in events if e["kind"] == "context_fork"]
    assert len(fork_events) == 1
    assert fork_events[0]["semantic_reuse"] is True
    assert fork_events[0]["compatible"] is False


def test_exact_compatible_child_inherits_provider_and_model(tmp_path: Path) -> None:
    """A child with compatible provider/model/protocol gets an exact fork."""
    runtime, events = _runtime(tmp_path)

    child_spec: dict[str, Any] = {
        "context_mode": "trunk",
        "placement": "inherit",
        "fanout_config": {
            "model": "model-a",
            "protocol": "http",
            "reasoning_effort": "high",
        },
        "authorized_providers": ["provider-a", "provider-b"],
    }

    asyncio.run(runtime._pin_fork_child(child_spec, "parent", "child", "investigation"))

    # Compatible fork: provider and model are pinned.
    assert child_spec.get("assigned_provider") == "provider-a"
    assert child_spec.get("fanout_config", {}).get("model") == "model-a"
    assert "context_fork" in child_spec
    assert child_spec["_required_context_mode"] == "trunk"

    # No summary_trunk_ref for exact forks.
    assert "summary_trunk_ref" not in child_spec

    fork_events = [e for e in events if e["kind"] == "context_fork"]
    assert len(fork_events) == 1
    assert fork_events[0]["semantic_reuse"] is False
    assert fork_events[0]["compatible"] is True


@pytest.mark.parametrize("context_mode", ["trunk", "semantic", "fresh"])
def test_inherited_children_respect_provider_lane_capacity(
    tmp_path: Path, context_mode: str
) -> None:
    runtime, _events = _runtime(tmp_path)
    runtime._lanes["provider-a"] = LaneState(in_flight=1, max_concurrency=1)
    child_spec: dict[str, Any] = {
        "context_mode": context_mode,
        "placement": "inherit",
        "fanout_config": {
            "model": "model-a",
            "protocol": "http",
            "reasoning_effort": "high",
        },
        "authorized_providers": ["provider-a"],
    }

    asyncio.run(runtime._pin_fork_child(child_spec, "parent", "child", "investigation"))

    assert child_spec["assigned_provider"] == "provider-a"
    assert child_spec["_supervisor_pinned_lane"] is True
    # Context pinning itself does not bypass or consume capacity.
    assert runtime._lanes["provider-a"].in_flight == 1
    with pytest.raises(LaneCapacityExhausted):
        runtime._resolve_assignment(child_spec)

    runtime._lanes["provider-a"].release()
    runtime._resolve_assignment(child_spec)
    assert child_spec["_lane_reserved"] is True
    assert runtime._lanes["provider-a"].in_flight == 1


def test_inherited_lane_reservation_honors_retry_after_pressure(tmp_path: Path) -> None:
    runtime, _events = _runtime(tmp_path)
    debt = SimpleNamespace(retry_after_count=50)
    runtime._debt_store = SimpleNamespace(as_mapping=lambda: {"provider-a": debt})
    runtime._lanes["provider-a"] = LaneState(
        in_flight=1,
        rpm_allowance=2,
        max_concurrency=2,
    )
    spec: dict[str, Any] = {
        "task_id": "child",
        "assigned_provider": "provider-a",
        "_supervisor_pinned_lane": True,
    }

    # Retry-After pressure halves the legacy lane cap from two to one.
    with pytest.raises(LaneCapacityExhausted):
        runtime._resolve_assignment(spec)
    assert runtime._lanes["provider-a"].in_flight == 1

    debt.retry_after_count = 0
    runtime._resolve_assignment(spec)
    assert spec["_lane_reserved"] is True
    assert runtime._lanes["provider-a"].in_flight == 2


def test_suspended_parent_reacquires_lane_without_overbooking(tmp_path: Path) -> None:
    runtime, _events = _runtime(tmp_path)
    runtime._lanes["provider-a"] = LaneState(in_flight=1, max_concurrency=1)
    spec: dict[str, Any] = {
        "task_id": "parent",
        "assigned_provider": "provider-a",
        "_lane_reserved": False,
    }

    async def scenario() -> None:
        task = asyncio.create_task(
            runtime._await_reacquire_lane(spec, asyncio.get_running_loop().time() + 1.0)
        )
        await asyncio.sleep(0.01)
        assert not task.done()
        runtime._lanes["provider-a"].release()
        runtime._lane_changed.set()
        await task

    asyncio.run(scenario())
    assert spec["_lane_reserved"] is True
    assert runtime._lanes["provider-a"].in_flight == 1


def test_missing_parent_epoch_rejects_declared_semantic(tmp_path: Path) -> None:
    """An explicit semantic child cannot become fresh when its checkpoint is missing."""
    runtime = _Runtime(tmp_path, None)
    events: list[dict[str, Any]] = []

    async def emit(kind: str, **payload: Any) -> None:
        events.append({"kind": kind, **payload})

    runtime.emit = emit  # type: ignore[method-assign]
    child_spec: dict[str, Any] = {
        "context_mode": "semantic",
        "placement": "spread",
        "summary_trunk_ref": "stale-ref",
        "context_fork": {"checkpoint_ref": "stale-ref"},
        "parent_envelope": {"summary": "parent"},
    }

    with pytest.raises(ChildPolicyError, match="semantic"):
        asyncio.run(runtime._pin_fork_child(child_spec, "missing", "child", "investigation"))

    assert child_spec["context_mode"] == "semantic"
    assert child_spec["placement"] == "spread"
    assert events == []


def test_raw_tail_parent_epoch_rejects_declared_semantic(tmp_path: Path) -> None:
    """A checkpoint with raw tail is not a valid explicit semantic context."""
    runtime = _Runtime(tmp_path, None)
    events: list[dict[str, Any]] = []

    async def emit(kind: str, **payload: Any) -> None:
        events.append({"kind": kind, **payload})

    runtime.emit = emit  # type: ignore[method-assign]
    runtime._task_epochs["parent"] = _checkpoint_epoch(tmp_path, raw_tail=True)
    child_spec: dict[str, Any] = {
        "context_mode": "semantic",
        "placement": "spread",
        "parent_envelope": {"summary": "parent"},
    }

    with pytest.raises(ChildPolicyError, match="semantic"):
        asyncio.run(runtime._pin_fork_child(child_spec, "parent", "child", "investigation"))

    assert child_spec["context_mode"] == "semantic"
    assert child_spec["placement"] == "spread"
    assert events == []


def test_parse_child_policy_rejects_trunk_spread_combination() -> None:
    """trunk+spread is contradictory and must be rejected."""
    with pytest.raises(ValueError, match="trunk requires placement=inherit"):
        parse_child_policy({"context_mode": "trunk", "placement": "spread"})


def _redacted_epoch(session_dir: Path) -> dict[str, Any]:
    return _checkpoint_epoch(session_dir, redacted=True)


def test_redacted_parent_epoch_allows_declared_semantic(tmp_path: Path) -> None:
    """Semantic reuse may import summaries from a redacted checkpoint."""
    runtime, events = _runtime(tmp_path)
    runtime._task_epochs["parent"] = _redacted_epoch(tmp_path)

    child_spec: dict[str, Any] = {"context_mode": "semantic", "placement": "spread"}
    asyncio.run(
        runtime._pin_fork_child(
            child_spec,
            "parent",
            "child",
            "investigation",
        )
    )

    assert child_spec["summary_trunk_ref"] == runtime._task_epochs["parent"]["checkpoint_ref"]
    assert "context_fork" not in child_spec
    fork_events = [event for event in events if event["kind"] == "context_fork"]
    assert len(fork_events) == 1
    assert fork_events[0]["semantic_reuse"] is True
    assert fork_events[0]["compatible"] is False
    assert fork_events[0]["context_mode"] == "semantic"
    assert fork_events[0]["resolved_context_mode"] == "semantic"


def test_redacted_parent_epoch_automatically_falls_back_to_semantic(tmp_path: Path) -> None:
    """Automatic incompatibility falls back to persisted semantic summaries."""
    runtime, events = _runtime(tmp_path)
    runtime._task_epochs["parent"] = _redacted_epoch(tmp_path)
    child_spec: dict[str, Any] = {"fanout_config": {"model": "other-model"}}

    asyncio.run(runtime._pin_fork_child(child_spec, "parent", "child", "investigation"))

    assert child_spec["summary_trunk_ref"] == runtime._task_epochs["parent"]["checkpoint_ref"]
    assert "context_fork" not in child_spec
    fork_events = [event for event in events if event["kind"] == "context_fork"]
    assert len(fork_events) == 1
    assert fork_events[0]["semantic_reuse"] is True
    assert fork_events[0]["compatible"] is False


def test_redacted_parent_epoch_rejects_declared_trunk(tmp_path: Path) -> None:
    """A redacted checkpoint is never an exact fork; trunk stays a rejection."""
    runtime = _Runtime(tmp_path, None)
    runtime._task_epochs["parent"] = _redacted_epoch(tmp_path)
    child_spec: dict[str, Any] = {
        "context_mode": "trunk",
        "placement": "inherit",
        "fanout_config": {
            "model": "model-a",
            "protocol": "http",
            "reasoning_effort": "high",
        },
        "authorized_providers": ["provider-a"],
    }

    with pytest.raises(ValueError, match="exact compatible parent checkpoint"):
        asyncio.run(runtime._pin_fork_child(child_spec, "parent", "child", "investigation"))


def test_missing_parent_epoch_rejects_declared_trunk(tmp_path: Path) -> None:
    """The first delegation of a checkpoint-less parent cannot fork exactly."""
    runtime = _Runtime(tmp_path, None)
    child_spec: dict[str, Any] = {"context_mode": "trunk", "placement": "inherit"}

    with pytest.raises(ValueError, match="exact compatible parent checkpoint"):
        asyncio.run(runtime._pin_fork_child(child_spec, "missing", "child", "investigation"))


def test_fresh_child_admits_with_missing_or_redacted_parent_epoch(tmp_path: Path) -> None:
    """fresh has no checkpoint precondition: it is the usable first-batch mode."""
    for epoch in (None, _redacted_epoch(tmp_path)):
        runtime, events = _runtime(tmp_path)
        if epoch is None:
            runtime._task_epochs.clear()
        child_spec: dict[str, Any] = {"context_mode": "fresh", "placement": "inherit"}

        asyncio.run(runtime._pin_fork_child(child_spec, "parent", "child", "investigation"))

        assert "summary_trunk_ref" not in child_spec
        assert "context_fork" not in child_spec
        fork_events = [e for e in events if e["kind"] == "context_fork"]
        assert len(fork_events) == 1
        assert fork_events[0]["resolved_context_mode"] == "fresh"
        assert fork_events[0]["semantic_reuse"] is False


def test_fresh_child_drops_parent_context_when_reuse_is_disabled(tmp_path: Path) -> None:
    """fresh stays task-only even when the session has no context reuse."""
    runtime = _Runtime(tmp_path, None, context_reuse=False)
    child_spec: dict[str, Any] = {
        "context_mode": "fresh",
        "placement": "inherit",
        "parent_envelope": {"summary": "must not cross the fresh boundary"},
        "context_fork": {"checkpoint_ref": "stale"},
        "summary_trunk_ref": "stale",
    }

    asyncio.run(runtime._pin_fork_child(child_spec, "parent", "child", "investigation"))

    assert child_spec["_required_context_mode"] == "fresh"
    assert "parent_envelope" not in child_spec
    assert "context_fork" not in child_spec
    assert "summary_trunk_ref" not in child_spec
