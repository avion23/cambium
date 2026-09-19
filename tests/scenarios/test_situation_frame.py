"""Focused deterministic and bounded SituationFrame scenarios."""

from __future__ import annotations

from cambium.branch_state import BranchState, inspect_state
from cambium.situation import SECTION_ORDER, render_situation_frame


def _events() -> list[dict]:
    return [
        {
            "seq": 1,
            "kind": "task_assigned",
            "task_id": "root",
            "payload": {
                "session_id": "session-1",
                "task": "repair the parser",
                "repo": "/repo",
                "worktree": "/session/wt",
                "branch": "cambium/root",
                "constraints": ["keep the public API stable"],
                "done_when": ["focused test passes"],
                "verification": ["python -m pytest tests/test_parser.py -q"],
                "writable_scope": ["src/parser.py", "tests/test_parser.py"],
                "tools": ["read_batch", "edit_file", "run_shell"],
            },
        },
        {
            "seq": 2,
            "kind": "child_admitted",
            "task_id": "root",
            "payload": {
                "child_task_id": "review-1",
                "parent_task_id": "root",
                "child_kind": "review",
                "context_mode": "fresh",
                "placement": "spread",
                "critical": True,
            },
        },
        {
            "seq": 3,
            "kind": "context_checkpoint",
            "task_id": "root",
            "payload": {
                "epoch": 2,
                "checkpoint_ref": "root/epoch-002.json",
                "cache_key": {"provider": "provider-a", "model": "model-a"},
            },
        },
        {
            "seq": 4,
            "kind": "tool_event",
            "task_id": "root",
            "payload": {
                "tool": "read_batch",
                "turn": 1,
                "batch_index": 0,
                "ok": True,
            },
        },
    ]


def test_same_replayed_state_renders_byte_identically() -> None:
    state = inspect_state(_events())
    replayed = BranchState.from_json(state.to_json())

    first = render_situation_frame(state)
    second = render_situation_frame(replayed)

    assert first == second
    assert [line for line in first.splitlines() if line in SECTION_ORDER] == list(SECTION_ORDER)
    assert 'frame_sha256="' in first.splitlines()[0]
