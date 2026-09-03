"""Focused scenarios for the immutable branch-state projection."""

from __future__ import annotations

from cambium.branch_state import BranchState, Lifecycle, inspect_state, reduce


def test_lifecycle_fold_tracks_tool_usage_and_terminal_result() -> None:
    state = inspect_state(
        [
            {
                "seq": 1,
                "kind": "ready",
                "task_id": "root",
                "generation": 1,
                "turn": 1,
            },
            {
                "seq": 2,
                "kind": "tool_event",
                "task_id": "root",
                "generation": 1,
                "payload": {
                    "tool": "run_shell",
                    "turn": 1,
                    "batch_index": 0,
                    "ok": True,
                },
            },
            {
                "seq": 3,
                "kind": "usage_event",
                "task_id": "root",
                "generation": 1,
                "payload": {
                    "usage": {
                        "input_tokens": 120,
                        "output_tokens": 30,
                        "total_tokens": 150,
                    },
                    "estimated_cost_usd": 0.012,
                    "latency_s": 2.0,
                },
            },
            {
                "seq": 4,
                "kind": "result_envelope",
                "task_id": "root",
                "generation": 1,
                "payload": {
                    "status": "succeeded",
                    "summary": "completed",
                },
            },
        ]
    )

    assert state.lifecycle == Lifecycle.SUCCEEDED
    assert state.calls == 1
    assert state.input_tokens == 120
    assert state.output_tokens == 30
    assert state.total_tokens == 150
    assert state.tool_event_count == 1
    assert state.result is not None
    assert state.result.status == "succeeded"


def test_json_round_trip_preserves_state_equality() -> None:
    state = inspect_state(
        [
            {
                "seq": 1,
                "kind": "task_assigned",
                "task_id": "root",
                "payload": {
                    "session_id": "session-1",
                    "task": "repair the parser",
                    "repo": "/repo",
                    "worktree": "/worktree",
                    "branch": "cambium/root",
                    "constraints": ["keep the API stable"],
                    "done_when": ["focused test passes"],
                },
            },
            {
                "seq": 2,
                "kind": "context_checkpoint",
                "task_id": "root",
                "payload": {
                    "epoch": 2,
                    "checkpoint_ref": "root/epoch-002.json",
                    "cache_key": {"provider": "provider-a", "model": "model-a"},
                },
            },
        ]
    )

    restored = BranchState.from_json(state.to_json())

    assert restored == state


def test_unknown_events_increment_explicit_counter() -> None:
    state = reduce(BranchState(), {"seq": 1, "kind": "future_event", "payload": {}})
    state = reduce(state, {"seq": 2, "type": "another_future_event", "payload": {}})

    assert state.unknown_events == 2
    assert state.unknown_event_kinds == ("future_event", "another_future_event")
    assert state.source_watermark == 2
    assert state.lifecycle == Lifecycle.UNKNOWN
