"""Unit tests for the pure live-stream render helpers in cambium.render.

These helpers are side-effect free and deterministic: they only read
already-redacted event records (mappings with ``kind`` and ``payload`` keys)
and return a string.  No subprocesses, no network, no I/O.
"""

from __future__ import annotations

from cambium.render import (
    render_active_workers,
    render_live_status_line,
    render_tokens_per_s,
)


def _usage_event(
    turn: int, total_tokens: int | float, latency_s: int | float
) -> dict[str, object]:
    return {
        "kind": "usage_event",
        "payload": {
            "turn": turn,
            "latency_s": latency_s,
            "provider": "p",
            "model": "m",
            "usage": {
                "input_tokens": total_tokens // 2,
                "completion_tokens": total_tokens - total_tokens // 2,
                "total_tokens": total_tokens,
            },
        },
    }


def _lifecycle(kind: str) -> dict[str, object]:
    return {"kind": kind, "payload": {}}


def test_tokens_per_s_uses_latest_usable_pair() -> None:
    events = [_usage_event(1, 100, 10.0), _usage_event(2, 123, 10.0)]

    assert render_tokens_per_s(events) == "tokens/s=12.3"


def test_tokens_per_s_single_event_uses_its_own_latency() -> None:
    events = [_usage_event(1, 50, 4.0)]

    assert render_tokens_per_s(events) == "tokens/s=12.5"


def test_tokens_per_s_empty_without_usage_events() -> None:
    assert render_tokens_per_s([]) == ""
    assert render_tokens_per_s([_lifecycle("heartbeat")]) == ""


def test_tokens_per_s_skips_non_finite_or_non_positive_latency() -> None:
    events = [
        _usage_event(1, 50, 0.0),
        _usage_event(2, 60, float("nan")),
        _usage_event(3, 70, float("inf")),
        _usage_event(4, 80, -5.0),
        _usage_event(5, 200, 20.0),
    ]

    assert render_tokens_per_s(events) == "tokens/s=10.0"


def test_tokens_per_s_skips_non_numeric_totals() -> None:
    bad = {
        "kind": "usage_event",
        "payload": {
            "turn": 1,
            "latency_s": 5.0,
            "usage": {"total_tokens": "many"},
        },
    }

    assert render_tokens_per_s([bad]) == ""


def test_active_workers_spawn_ready_exit_ends_at_zero() -> None:
    events = [_lifecycle("spawned"), _lifecycle("ready"), _lifecycle("exit")]

    assert render_active_workers(events) == ""


def test_active_workers_counts_two_spawned_workers() -> None:
    events = [
        _lifecycle("spawned"),
        _lifecycle("ready"),
        _lifecycle("spawned"),
        _lifecycle("ready"),
    ]

    assert render_active_workers(events) == "subagents=2"


def test_active_workers_reuse_ready_never_negative() -> None:
    events = [_lifecycle("spawned"), _lifecycle("reuse_ready")]

    assert render_active_workers(events) == ""


def test_active_workers_worker_failed_decrements() -> None:
    events = [
        _lifecycle("spawned"),
        _lifecycle("ready"),
        _lifecycle("spawned"),
        _lifecycle("ready"),
        _lifecycle("worker_failed"),
    ]

    assert render_active_workers(events) == "subagents=1"


def test_active_workers_clamps_a_lone_decrement() -> None:
    assert render_active_workers([_lifecycle("exit")]) == ""
    assert render_active_workers([_lifecycle("reuse_ready")]) == ""


def test_live_status_line_combines_non_empty_parts() -> None:
    events = [
        _usage_event(1, 100, 10.0),
        _lifecycle("spawned"),
        _lifecycle("ready"),
        _lifecycle("spawned"),
        _lifecycle("ready"),
    ]

    assert render_live_status_line(events) == "live: tokens/s=10.0 · subagents=2"


def test_live_status_line_empty_when_both_parts_empty() -> None:
    assert render_live_status_line([]) == ""
    assert render_live_status_line([_lifecycle("heartbeat")]) == ""
