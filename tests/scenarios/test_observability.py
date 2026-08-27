"""Event-sourced operator dashboard regressions."""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

from cambium.monitor import render_agent_lines, render_dashboard
from cambium.observability import (
    ObservabilityState,
    RecentEvent,
    _checkpoint_path,
    snapshot_from_events,
)
from cambium.summary_trunk import SUMMARY_ENTRY_CLOSE, SUMMARY_ENTRY_OPEN


def _event(
    seq: int,
    kind: str,
    *,
    task_id: str | None = None,
    generation: int = 1,
    **payload,
) -> dict:
    record = {
        "seq": seq,
        "kind": kind,
        "payload": payload,
        "generation": generation,
        "monotonic_ms": seq * 100,
    }
    if task_id is not None:
        record["task_id"] = task_id
    return record


def test_reducer_exposes_main_and_subagent_usage_and_models() -> None:
    events = [
        _event(1, "session_started"),
        _event(2, "task_assigned", task_id="root", assigned_provider="codex", model="gpt"),
        _event(3, "spawned", task_id="root"),
        _event(
            4,
            "usage_event",
            task_id="root",
            provider="codex",
            model="gpt",
            turn=1,
            latency_s=2.0,
            call_kind="agent",
            active_context_bytes=4_000,
            active_context_messages=8,
            summary_trunk_bytes=3_000,
            summary_segments=2,
            raw_tail_bytes=1_000,
            usage={
                "input_tokens": 1_000,
                "output_tokens": 100,
                "cached_tokens": 600,
                "total_tokens": 1_100,
            },
            estimated_cost_usd=0.01,
        ),
        _event(
            5,
            "child_admitted",
            task_id="root",
            parent_task_id="root",
            child_task_id="child",
        ),
        _event(6, "spawned", task_id="child"),
        _event(
            7,
            "usage_event",
            task_id="child",
            provider="zai",
            model="glm",
            turn=1,
            latency_s=1.0,
            call_kind="summary",
            usage={
                "prompt_tokens": 200,
                "completion_tokens": 50,
                "total_tokens": 250,
            },
            estimated_cost_usd=0.002,
        ),
        _event(8, "result", task_id="child", status="succeeded"),
    ]

    snapshot = snapshot_from_events(events)
    assert [agent.role for agent in snapshot.agents] == ["main", "sub"]
    root, child = snapshot.agents
    assert root.state == "active"
    assert root.provider == "codex"
    assert root.model == "gpt"
    assert root.input_tokens == 1_000
    assert root.output_tokens_per_s == 50.0
    assert child.parent_task_id == "root"
    assert child.state == "succeeded"
    assert child.summary_calls == 1
    assert child.output_tokens_per_s == 50.0
    assert snapshot.total_tokens == 1_350
    assert snapshot.cached_tokens == 600
    assert snapshot.output_tokens_per_s == 50.0
    assert snapshot.context.summary_segments == 2
    assert snapshot.context.exact_prompt_tokens == 1_000


def test_terminal_state_is_not_overwritten_by_late_heartbeat() -> None:
    state = ObservabilityState()
    state.extend(
        [
            _event(1, "spawned", task_id="root"),
            _event(2, "result", task_id="root", status="failed"),
            _event(3, "heartbeat", task_id="root", turn=9),
        ]
    )
    snapshot = state.snapshot()
    assert snapshot.agents[0].state == "failed"
    assert snapshot.agents[0].turn == 9


def test_first_terminal_state_wins_over_late_exit() -> None:
    snapshot = snapshot_from_events(
        [
            _event(1, "result", task_id="root", status="succeeded"),
            _event(2, "exit", task_id="root"),
        ]
    )
    assert snapshot.agents[0].state == "succeeded"
    assert snapshot.succeeded_agents == 1
    assert snapshot.failed_agents == 0


def test_checkpoint_inspection_rejects_symlink_outside_session(tmp_path: Path) -> None:
    session = tmp_path / "session"
    outside = tmp_path / "outside.json"
    outside.write_text("{}", encoding="utf-8")
    link = session / ".cambium" / "checkpoints" / "root" / "checkpoint.json"
    link.parent.mkdir(parents=True)
    link.symlink_to(outside)

    assert _checkpoint_path(session, "root/checkpoint.json") is None


def test_checkpoint_inspection_counts_immutable_segments(tmp_path: Path) -> None:
    session = tmp_path / "session"
    reference = "root/epoch-001-aaaaaaaaaaaaaaaa-bbbbbbbbbbbbbbbb.json"
    checkpoint = session / ".cambium" / "checkpoints" / reference
    checkpoint.parent.mkdir(parents=True)
    summary = SUMMARY_ENTRY_OPEN + '{"type":"summary_entry"}' + SUMMARY_ENTRY_CLOSE
    document = {
        "provider_messages": [
            {"role": "system", "content": "system"},
            {"role": "user", "content": "task"},
            {"role": "user", "content": summary},
            {"role": "user", "content": summary},
        ],
        "continuation_suffix": [
            {"role": "assistant", "content": "working"},
            {"role": "user", "content": "tool result"},
        ],
    }
    checkpoint.write_text(json.dumps(document), encoding="utf-8")
    events = [
        _event(
            1,
            "context_checkpoint",
            task_id="root",
            epoch=1,
            checkpoint_ref=reference,
            cache_key={"prefix_bytes": 900, "message_count": 4},
        )
    ]

    snapshot = snapshot_from_events(events, session_dir=session)

    assert snapshot.context.checkpoint_ref == reference
    assert snapshot.context.summary_segments == 2
    assert snapshot.context.summary_trunk_bytes > snapshot.context.stable_head_bytes
    assert snapshot.context.raw_tail_bytes > 0
    assert snapshot.context.estimated_trunk_tokens > 0


def test_dashboard_and_status_render_core_introspection(tmp_path: Path) -> None:
    snapshot = snapshot_from_events(
        [
            _event(1, "spawned", task_id="root"),
            _event(
                2,
                "usage_event",
                task_id="root",
                provider="codex",
                model="gpt",
                latency_s=2.0,
                usage={"input_tokens": 10, "output_tokens": 4, "total_tokens": 14},
            ),
        ]
    )
    dashboard = "\n".join(render_dashboard(snapshot, session_dir=tmp_path, width=120, height=24))
    status = "\n".join(render_agent_lines(snapshot))
    assert "codex/gpt" in dashboard
    assert "out/s=2.0" in dashboard
    assert "main" in status
    assert "tokens=14" in status


def test_dashboard_sanitizes_provider_derived_text_at_render_boundary(tmp_path: Path) -> None:
    hostile_provider = "codex\x1b[31m-live\x1b]52;c;clipboard\x07\x00"
    hostile_model = "gpt\x1b[?25l-5\x9b31m\x9c"
    hostile_error = "HTTP 503 \x1b[31mbody\x1b]52;c;clipboard\x07 next\x01"
    hostile_fallback = "primary\x1b]0;title\x1b\\-fallback\x02"
    snapshot = snapshot_from_events(
        [
            _event(1, "spawned", task_id="root"),
            _event(
                2,
                "usage_event",
                task_id="root",
                provider=hostile_provider,
                model=hostile_model,
                failure_reason=hostile_error,
                usage={"total_tokens": 1},
            ),
        ]
    )
    # A fallback origin is a provider-derived value carried by result/event
    # projections.  Include it as a recent rendered detail to exercise the
    # same dashboard boundary as the provider error text.
    snapshot = replace(
        snapshot,
        recent_events=(
            *snapshot.recent_events,
            RecentEvent(seq=3, kind="fallback", task_id="root", detail=hostile_fallback),
        ),
    )

    dashboard = "\n".join(render_dashboard(snapshot, session_dir=tmp_path, width=120, height=24))
    status = "\n".join(render_agent_lines(snapshot))
    rendered = dashboard + "\n" + status

    for control in ("\x00", "\x01", "\x02", "\x07", "\x1b", "\x9b", "\x9c"):
        assert control not in rendered
    for sequence_payload in ("[31m", "[?25l", "52;c;clipboard", "0;title"):
        assert sequence_payload not in rendered
    assert "codex-live/gpt-5" in rendered
    assert "HTTP 503 body next" in rendered
    assert "primary-fallback" in rendered


def test_unsequenced_and_invalid_sequence_replays_are_deduplicated() -> None:
    state = ObservabilityState(recent_limit=8)
    unsequenced = {
        "kind": "usage_event",
        "task_id": "root",
        "payload": {
            "usage": {"input_tokens": 10, "output_tokens": 2, "total_tokens": 12}
        },
    }
    invalid_sequence = {**unsequenced, "seq": 0}

    state.apply(unsequenced)
    state.apply(dict(unsequenced))
    state.apply(invalid_sequence)
    state.apply(dict(invalid_sequence))

    snapshot = state.snapshot()
    assert snapshot.calls == 1
    assert len(snapshot.recent_events) == 1


def test_unsequenced_event_hash_ring_is_bounded() -> None:
    state = ObservabilityState(recent_limit=128)
    for index in range(64):
        state.apply({"kind": f"event-{index}"})

    assert len(state._unsequenced_hashes) == 64
    state.apply({"kind": "event-64"})
    state.apply({"kind": "event-0"})
    assert len(state.snapshot().recent_events) == 66
