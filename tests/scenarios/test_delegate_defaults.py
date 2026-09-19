"""The model supplies work and context intent; the supervisor supplies mechanics."""

from pathlib import Path

import pytest

from cambium.supervisor import _child_spec


def test_child_path_cannot_escape_session(tmp_path: Path) -> None:
    parent = {
        "task_id": "root",
        "repo": str(tmp_path / "repo"),
        "worktree_path": str(tmp_path / "root"),
        "branch": "root",
    }
    proposal = {
        "child_task_id": "child",
        "kind": "investigation",
        "spec": {"task": "read", "worktree_path": str(tmp_path.parent / "outside")},
    }
    with pytest.raises(ValueError, match="outside the session"):
        _child_spec(tmp_path, parent, proposal, {})
