"""The model supplies work and context intent; the supervisor supplies mechanics."""

from pathlib import Path

import pytest

from cambium.schemas import TOOL_SCHEMAS, validate_tool_call
from cambium.supervisor import _child_spec


def test_minimal_delegate_inherits_execution_without_pinning_spread(tmp_path: Path) -> None:
    parent = {
        "task_id": "root", "task": "implement two independent changes",
        "repo": str(tmp_path / "repo"), "worktree_path": str(tmp_path / "root"),
        "branch": "cambium-root", "worker": "cambium.worker", "base_commit": "abc",
        "fanout_config": {"model": "model-a", "tier": "fast", "call_budget_s": 90},
        "model_candidates": ["model-a", "model-b"], "assigned_provider": "a",
        "authorized_providers": ["a", "b"], "authorized_providers_explicit": True,
        "provider_env_keys": [], "max_tokens": 12000,
    }
    proposal = {
        "child_task_id": "parser", "kind": "feature",
        "spec": {
            "task": "Fix parser.py and verify it",
            "context_mode": "semantic", "placement": "spread",
        },
    }
    schema = next(s for s in TOOL_SCHEMAS if s["name"] == "delegate")
    assert not validate_tool_call(schema, proposal)
    child = _child_spec(tmp_path, parent, proposal, {})
    assert child["repo"] == parent["repo"]
    assert child["worktree_path"] == str(tmp_path / "children" / "parser")
    assert child["branch"] == "cambium-root--parser"
    assert child["fanout_config"] == {"call_budget_s": 90, "provider_env_keys": []}
    assert child["model_candidates"] == ["model-a", "model-b"]
    assert child["max_tokens"] == 12000
    assert parent["fanout_config"]["model"] == "model-a"
    proposal["spec"].update(context_mode="trunk", placement="inherit")
    exact = _child_spec(tmp_path, parent, proposal, {})
    assert exact["fanout_config"]["model"] == "model-a"
    assert exact["assigned_provider"] == "a"


def test_child_path_cannot_escape_session(tmp_path: Path) -> None:
    parent = {
        "task_id": "root", "repo": str(tmp_path / "repo"),
        "worktree_path": str(tmp_path / "root"), "branch": "root",
    }
    proposal = {
        "child_task_id": "child", "kind": "investigation",
        "spec": {"task": "read", "worktree_path": str(tmp_path.parent / "outside")},
    }
    with pytest.raises(ValueError, match="outside the session"):
        _child_spec(tmp_path, parent, proposal, {})
