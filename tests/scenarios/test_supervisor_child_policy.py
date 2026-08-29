from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest

from cambium.child_policy import ContextMode, Placement, parse_child_policy
from cambium.supervisor import _Runtime
from cambium.worker import _provider_task_tools_hash


def _epoch() -> dict[str, Any]:
    return {
        "epoch": 2,
        "checkpoint_ref": "parent/epoch-002-0000000000000000-0000000000000000.json",
        "cache_key": {
            "provider": "provider-a",
            "model": "model-a",
            "protocol": "http",
            "reasoning_effort": "high",
            "redacted": False,
            "system_sha256": "aaa",
            "tools_sha256": _provider_task_tools_hash(),
            "prefix_sha256": "ccc",
            "suffix_sha256": "ddd",
            "full_sha256": "eee",
            "prefix_bytes": 100,
            "provider_boundary": {"provider": "provider-a", "model": "model-a", "epoch": 2},
        },
    }


def _runtime(tmp_path: Path) -> tuple[_Runtime, list[dict[str, Any]]]:
    runtime = _Runtime(tmp_path, None)
    events: list[dict[str, Any]] = []

    async def emit(kind: str, **payload: Any) -> None:
        events.append({"kind": kind, **payload})

    runtime.emit = emit  # type: ignore[method-assign]
    runtime._task_epochs["parent"] = _epoch()
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
    assert child_spec.get("summary_trunk_ref") == _epoch()["checkpoint_ref"]
    assert "assigned_provider" not in child_spec
    assert "context_fork" not in child_spec

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

    # No summary_trunk_ref for exact forks.
    assert "summary_trunk_ref" not in child_spec

    fork_events = [e for e in events if e["kind"] == "context_fork"]
    assert len(fork_events) == 1
    assert fork_events[0]["semantic_reuse"] is False
    assert fork_events[0]["compatible"] is True


def test_missing_parent_epoch_skips_pin_without_error(tmp_path: Path) -> None:
    """_pin_fork_child returns silently when no parent epoch exists."""
    runtime = _Runtime(tmp_path, None)
    child_spec: dict[str, Any] = {
        "context_mode": "semantic",
        "placement": "spread",
    }

    # No epoch for task "missing" — method returns, no error.
    asyncio.run(runtime._pin_fork_child(child_spec, "missing", "child", "investigation"))

    assert child_spec == {"context_mode": "semantic", "placement": "spread"}


def test_parse_child_policy_rejects_trunk_spread_combination() -> None:
    """trunk+spread is contradictory and must be rejected."""
    with pytest.raises(ValueError, match="trunk requires placement=inherit"):
        parse_child_policy({"context_mode": "trunk", "placement": "spread"})
    parse_child_policy({"context_mode": "trunk", "placement": "inherit"})
    parse_child_policy({"context_mode": "semantic", "placement": "spread"})