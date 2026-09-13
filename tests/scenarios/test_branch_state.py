"""Focused scenarios for the immutable branch-state projection."""

from __future__ import annotations

from dataclasses import replace

import pytest

from cambium.branch_state import (
    BranchState,
    FailureSource,
    Identity,
    Lifecycle,
    ResultEnvelope,
    inspect_state,
    reduce,
)


def _event(seq: int, kind: str, task_id: str, **payload: object) -> dict[str, object]:
    return {"seq": seq, "kind": kind, "task_id": task_id, "payload": payload}


def _admitted_child() -> dict[str, object]:
    return _event(
        1,
        "child_admitted",
        "parent",
        parent_task_id="parent",
        child_task_id="child",
    )


@pytest.mark.parametrize(
    ("kind", "payload", "expected_lifecycle", "expected_status"),
    (
        ("child_result", {"status": "succeeded"}, Lifecycle.SUCCEEDED, "succeeded"),
        ("child_result", {"status": "timeout"}, Lifecycle.FAILED, "timeout"),
        ("child_failed", {"reason": "child timeout"}, Lifecycle.FAILED, "failed"),
    ),
)
def test_child_terminal_events_keep_lifecycle_and_result_aligned(
    kind: str,
    payload: dict[str, object],
    expected_lifecycle: Lifecycle,
    expected_status: str,
) -> None:
    state = inspect_state(
        [
            _admitted_child(),
            _event(2, kind, "child", parent_task_id="parent", **payload),
        ]
    )

    child = next(child for child in state.children if child.branch_id == "child")
    assert child.lifecycle is expected_lifecycle
    assert child.result is not None
    assert child.result.status == expected_status


def test_late_child_failure_overrides_a_successful_child_result() -> None:
    state = inspect_state(
        [
            _admitted_child(),
            _event(2, "child_result", "child", parent_task_id="parent", status="succeeded"),
            _event(3, "child_failed", "child", parent_task_id="parent", reason="late failure"),
        ]
    )

    child = next(child for child in state.children if child.branch_id == "child")
    assert child.lifecycle is Lifecycle.FAILED
    assert child.result is not None
    assert child.result.status == "failed"
    assert child.result.failure_reason == "late failure"
    assert child.result.failure_source is FailureSource.CHILD_FAILED


def test_failed_child_envelope_keeps_the_prior_child_failure_reason() -> None:
    state = inspect_state(
        [
            _admitted_child(),
            _event(2, "child_failed", "child", parent_task_id="parent", reason="worker crashed"),
            _event(
                3,
                "child_result",
                "child",
                parent_task_id="parent",
                status="failed",
                summary="worker crashed",
            ),
        ]
    )

    child = next(child for child in state.children if child.branch_id == "child")
    assert child.result is not None
    assert child.result.status == "failed"
    assert child.result.failure_reason == "worker crashed"
    assert child.result.failure_source is FailureSource.CHILD_FAILED


def test_focused_child_failure_updates_the_projection_root() -> None:
    state = inspect_state([_event(1, "result", "child", status="succeeded")])
    state = reduce(
        replace(state, identity=Identity(branch_id="child")),
        _event(
            2,
            "child_failed",
            "child",
            parent_task_id="parent",
            child_task_id="child",
            reason="late failure",
        ),
    )

    assert state.children == ()
    assert state.lifecycle is Lifecycle.FAILED
    assert state.result is not None
    assert state.result.status == "failed"


def test_child_result_payload_identity_updates_only_the_named_child() -> None:
    state = inspect_state(
        [
            _admitted_child(),
            _event(
                2,
                "child_result",
                "parent",
                parent_task_id="parent",
                child_task_id="child",
                status="succeeded",
            ),
        ]
    )

    child = next(child for child in state.children if child.branch_id == "child")
    assert child.lifecycle is Lifecycle.SUCCEEDED
    assert child.result is not None
    assert child.result.status == "succeeded"
    assert state.result is None


def test_unattributed_child_rejection_does_not_reject_the_parent() -> None:
    state = inspect_state(
        [
            _event(1, "task_queued", "parent"),
            _event(
                2,
                "child_rejected",
                "parent",
                parent_task_id="parent",
                child_task_id=None,
                reason="MalformedProposal",
            ),
        ]
    )

    assert state.lifecycle is Lifecycle.QUEUED
    assert state.result is None
    assert state.children == ()


@pytest.mark.parametrize("kind", ("worker_failed", "merge_failed", "join_invariant_failed"))
def test_late_durable_failure_overrides_successful_result(kind: str) -> None:
    state = inspect_state(
        [
            _event(1, "result", "root", status="succeeded", summary="done"),
            _event(2, kind, "root", reason=f"{kind} after success"),
        ]
    )

    assert state.lifecycle is Lifecycle.FAILED
    assert state.result is not None
    assert state.result.status == "failed"
    assert state.result.failure_reason == f"{kind} after success"


def test_late_failure_provenance_follows_the_latest_non_merge_verdict() -> None:
    state = inspect_state(
        [
            _event(1, "result", "root", generation=1, status="succeeded"),
            _event(2, "worker_failed", "root", generation=1, reason="worker integrity"),
            _event(
                3,
                "join_invariant_failed",
                "root",
                generation=1,
                reason="parent head changed",
            ),
        ]
    )

    assert state.result is not None
    assert state.result.failure_source is FailureSource.JOIN_INVARIANT_FAILED
    assert state.result.failure_reason == "parent head changed"


def test_recoverable_merge_diagnostic_clears_without_touching_other_blockers() -> None:
    state = inspect_state(
        [
            _event(
                1,
                "result",
                "root",
                generation=1,
                status="succeeded",
                blockers=["keep this blocker"],
            ),
            _event(
                2,
                "merge_failed",
                "root",
                generation=1,
                internal=True,
                recoverable=True,
                reason="observer failed after integration",
            ),
            _event(3, "merge_committed", "root", generation=1, new="abc"),
        ]
    )

    assert state.lifecycle is Lifecycle.SUCCEEDED
    assert state.result is not None
    assert state.result.status == "succeeded"
    assert state.control.blockers == ("keep this blocker",)


def test_stale_merge_recovery_cannot_claim_success_for_a_pending_diagnostic() -> None:
    state = inspect_state(
        [
            _event(1, "spawned", "root", generation=1),
            _event(
                2,
                "merge_failed",
                "root",
                generation=1,
                internal=True,
                recoverable=True,
                reason="observer failed after integration",
            ),
            _event(3, "merge_committed", "root", generation=0, new="stale"),
        ]
    )

    assert state.lifecycle is Lifecycle.STARTING
    assert state.result is None
    assert any(blocker.startswith("merge_failed[root]:") for blocker in state.control.blockers)


def test_resolver_success_recovers_a_merge_failure_but_not_a_worker_failure() -> None:
    recovered = inspect_state(
        [
            _event(1, "result", "root", generation=1, status="succeeded"),
            _event(
                2,
                "merge_failed",
                "root",
                generation=1,
                status="merge_conflict",
                reason="conflict",
            ),
            _event(3, "resolver_succeeded", "root", generation=1, status="succeeded"),
        ]
    )
    assert recovered.lifecycle is Lifecycle.SUCCEEDED
    assert recovered.result is not None
    assert recovered.result.status == "succeeded"

    not_recovered = inspect_state(
        [
            _event(1, "result", "root", generation=1, status="succeeded"),
            _event(
                2,
                "merge_failed",
                "root",
                generation=1,
                status="merge_conflict",
                reason="conflict",
            ),
            _event(3, "worker_failed", "root", generation=1, reason="integrity"),
            _event(4, "merge_committed", "root", generation=1, new="abc"),
        ]
    )
    assert not_recovered.lifecycle is Lifecycle.FAILED
    assert not_recovered.result is not None
    assert not_recovered.result.status == "failed"
    assert not_recovered.result.failure_source is FailureSource.WORKER_FAILED
    assert not_recovered.result.failure_reason == "integrity"


@pytest.mark.parametrize("generation", (None, 0, 2))
def test_stale_or_unqualified_merge_recovery_does_not_clear_failure(
    generation: int | None,
) -> None:
    recovery = _event(3, "merge_committed", "root", generation=generation, new="abc")
    state = inspect_state(
        [
            _event(1, "result", "root", generation=1, status="succeeded"),
            _event(2, "merge_failed", "root", generation=1, reason="conflict"),
            recovery,
        ]
    )

    assert state.lifecycle is Lifecycle.FAILED
    assert state.result is not None
    assert state.result.status == "failed"
    assert any(blocker.startswith("merge_failed[root]:") for blocker in state.control.blockers)


def test_merge_reconciled_does_not_clear_a_merge_failure() -> None:
    state = inspect_state(
        [
            _event(1, "result", "root", generation=1, status="succeeded"),
            _event(2, "merge_failed", "root", generation=1, reason="conflict"),
            _event(3, "merge_reconciled", "root", generation=1, new="abc"),
        ]
    )

    assert state.lifecycle is Lifecycle.FAILED
    assert state.result is not None
    assert state.result.status == "failed"

    diagnostic = inspect_state(
        [
            _event(1, "spawned", "root", generation=1),
            _event(
                2,
                "merge_failed",
                "root",
                generation=1,
                internal=True,
                recoverable=True,
                reason="observer diagnostic",
            ),
            _event(3, "merge_reconciled", "root", generation=1, new="abc"),
        ]
    )
    assert diagnostic.lifecycle is Lifecycle.STARTING


def test_late_failed_result_cannot_be_cleared_by_merge_recovery() -> None:
    state = inspect_state(
        [
            _event(1, "result", "root", generation=1, status="succeeded"),
            _event(2, "merge_failed", "root", generation=1, reason="conflict"),
            _event(3, "result", "root", generation=1, status="failed", reason="worker failed"),
            _event(4, "merge_committed", "root", generation=1, new="abc"),
        ]
    )

    assert state.lifecycle is Lifecycle.FAILED
    assert state.result is not None
    assert state.result.status == "failed"
    assert state.result.failure_source is FailureSource.RESULT


def test_result_payload_cannot_forge_merge_recovery_provenance() -> None:
    state = inspect_state(
        [
            _event(1, "result", "root", generation=1, status="succeeded"),
            _event(2, "merge_failed", "root", generation=1, reason="conflict"),
            _event(
                3,
                "result",
                "root",
                generation=1,
                status="failed",
                reason="worker failed",
                failure_source="merge_failed",
            ),
            _event(4, "merge_committed", "root", generation=1, new="abc"),
        ]
    )

    assert state.lifecycle is Lifecycle.FAILED
    assert state.result is not None
    assert state.result.status == "failed"
    assert state.result.failure_source is FailureSource.RESULT


def test_merge_recovery_does_not_revive_a_schema_less_prior_failure() -> None:
    state = BranchState(
        identity=Identity(branch_id="root", generation=1, lifecycle=Lifecycle.FAILED),
        result=replace(ResultEnvelope(status="failed"), failure_reason="prior failure"),
    )
    state = reduce(
        state,
        _event(1, "merge_failed", "root", generation=1, reason="merge conflict"),
    )
    state = reduce(
        state,
        _event(2, "merge_committed", "root", generation=1, new="abc"),
    )

    assert state.lifecycle is Lifecycle.FAILED
    assert state.result is not None
    assert state.result.failure_reason == "prior failure"
    assert state.result.failure_source is FailureSource.UNKNOWN


def test_restart_generation_can_recover_a_timed_out_attempt() -> None:
    state = inspect_state(
        [
            _event(1, "timeout", "root", generation=1),
            _event(2, "restart_scheduled", "root", generation=1),
            _event(3, "spawned", "root", generation=2),
            _event(4, "result", "root", generation=2, status="succeeded"),
        ]
    )

    assert state.lifecycle is Lifecycle.SUCCEEDED
    assert state.result is not None
    assert state.result.status == "succeeded"


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


def test_failure_provenance_round_trip_preserves_late_failure_state() -> None:
    state = inspect_state(
        [
            _event(1, "result", "root", status="succeeded"),
            _event(2, "worker_failed", "root", reason="integrity"),
        ]
    )

    restored = BranchState.from_json(state.to_json())

    assert restored == state
    assert restored.result is not None
    assert restored.result.failure_source is FailureSource.WORKER_FAILED


def test_unknown_events_increment_explicit_counter() -> None:
    state = reduce(BranchState(), {"seq": 1, "kind": "future_event", "payload": {}})
    state = reduce(state, {"seq": 2, "type": "another_future_event", "payload": {}})

    assert state.unknown_events == 2
    assert state.unknown_event_kinds == ("future_event", "another_future_event")
    assert state.source_watermark == 2
    assert state.lifecycle == Lifecycle.UNKNOWN


def test_context_fork_projects_resolved_fresh_lineage() -> None:
    state = reduce(
        BranchState(),
        {
            "seq": 1,
            "kind": "context_fork",
            "task_id": "parent",
            "payload": {
                "parent_task_id": "parent",
                "child_task_id": "child",
                "context_mode": "semantic",
                "resolved_context_mode": "fresh",
                "semantic_reuse": False,
                "compatible": False,
            },
        },
    )

    child = next(child for child in state.children if child.branch_id == "child")
    assert child.context_mode == "fresh"
    assert child.lineage == "fresh"
