from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest

from cambium.supervisor import _Runtime


def _epoch() -> dict[str, Any]:
    return {
        "epoch": 2,
        "checkpoint_ref": "parent/epoch-002-0000000000000000-0000000000000000.json",
        "cache_key": {
            "provider": "provider-a",
            "model": "model-a",
            "redacted": False,
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


def test_semantic_spread_reuses_summaries_and_removes_parent_pin(tmp_path: Path) -> None:
    runtime, events = _runtime(tmp_path)
    child = {
        "context_mode": "semantic",
        "placement": "spread",
        "assigned_provider": "provider-a",
        "fanout_config": {"model": "model-a"},
        "model_candidates": ["model-a", "model-b"],
        "authorized_providers": ["provider-a", "provider-b"],
    }

    runtime._validate_child_context_policy(child, "parent")
    asyncio.run(runtime._pin_fork_child(child, "parent", "child", "investigation"))

    assert child["summary_trunk_ref"] == _epoch()["checkpoint_ref"]
    assert "context_fork" not in child
    assert "assigned_provider" not in child
    assert "model" not in child["fanout_config"]
    assert child["spread_from_provider"] == "provider-a"
    assert events[-1]["context_mode"] == "semantic"
    assert events[-1]["placement"] == "spread"
    assert events[-1]["semantic_reuse"] is True


def test_fresh_inherit_keeps_provider_but_no_parent_context(tmp_path: Path) -> None:
    runtime, events = _runtime(tmp_path)
    child = {
        "context_mode": "fresh",
        "placement": "inherit",
        "fanout_config": {},
        "authorized_providers": ["provider-a", "provider-b"],
    }

    runtime._validate_child_context_policy(child, "parent")
    asyncio.run(runtime._pin_fork_child(child, "parent", "child", "investigation"))

    assert child["assigned_provider"] == "provider-a"
    assert child["fanout_config"]["model"] == "model-a"
    assert "context_fork" not in child
    assert "summary_trunk_ref" not in child
    assert events[-1]["context_mode"] == "fresh"
    assert events[-1]["semantic_reuse"] is False


def test_semantic_mode_requires_a_parent_checkpoint(tmp_path: Path) -> None:
    runtime = _Runtime(tmp_path, None)
    child = {"context_mode": "semantic", "placement": "spread"}

    with pytest.raises(ValueError, match="requires a parent checkpoint"):
        runtime._validate_child_context_policy(child, "missing")
