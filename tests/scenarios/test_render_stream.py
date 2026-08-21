"""Unit tests for the pure live-stream render helpers in cambium.render.

These helpers are side-effect free and deterministic: they only read
already-redacted event records (mappings with ``kind`` and ``payload`` keys)
and return a string.  No subprocesses, no network, no I/O.
"""

from __future__ import annotations

from cambium.render import render_active_workers, render_tokens_per_s


def _usage_event(
    turn: int, usage: dict[str, object], latency_s: int | float
) -> dict[str, object]:
    return {
        "kind": "usage_event",
        "payload": {
            "turn": turn,
            "latency_s": latency_s,
            "provider": "p",
            "model": "m",
            "usage": usage,
        },
    }


def _lifecycle(kind: str) -> dict[str, object]:
    return {"kind": kind, "payload": {}}


def test_tokens_per_s_prefers_completion_tokens() -> None:
    events = [
        _usage_event(
            1,
            {"input_tokens": 900, "completion_tokens": 50, "total_tokens": 950},
            5.0,
        )
    ]

    assert render_tokens_per_s(events) == "tokens/s=10.0"


def test_tokens_per_s_falls_back_to_total_tokens_when_completion_missing() -> None:
    events = [_usage_event(1, {"total_tokens": 300}, 10.0)]

    assert render_tokens_per_s(events) == "tokens/s=30.0"


def test_tokens_per_s_falls_back_when_completion_is_not_numeric() -> None:
    events = [
        _usage_event(1, {"completion_tokens": "many", "total_tokens": 300}, 10.0)
    ]

    assert render_tokens_per_s(events) == "tokens/s=30.0"


def test_tokens_per_s_treats_unusable_completion_as_missing() -> None:
    bool_case = _usage_event(1, {"completion_tokens": True, "total_tokens": 300}, 10.0)
    nan_case = _usage_event(
        2, {"completion_tokens": float("nan"), "total_tokens": 300}, 10.0
    )
    inf_case = _usage_event(
        3, {"completion_tokens": float("inf"), "total_tokens": 300}, 10.0
    )

    assert render_tokens_per_s([bool_case]) == "tokens/s=30.0"
    assert render_tokens_per_s([nan_case]) == "tokens/s=30.0"
    assert render_tokens_per_s([inf_case]) == "tokens/s=30.0"


def test_tokens_per_s_skips_non_finite_or_non_positive_latency() -> None:
    events = [
        _usage_event(1, {"completion_tokens": 50, "total_tokens": 950}, 0.0),
        _usage_event(2, {"completion_tokens": 50, "total_tokens": 960}, float("nan")),
        _usage_event(3, {"completion_tokens": 50, "total_tokens": 970}, float("inf")),
        _usage_event(4, {"completion_tokens": 50, "total_tokens": 980}, -5.0),
        _usage_event(5, {"completion_tokens": 200, "total_tokens": 1200}, 20.0),
    ]

    assert render_tokens_per_s(events) == "tokens/s=10.0"


def test_tokens_per_s_skips_events_without_a_usable_token_count() -> None:
    non_numeric = _usage_event(1, {"completion_tokens": None, "total_tokens": "many"}, 5.0)
    bool_total = _usage_event(2, {"total_tokens": True}, 5.0)
    inf_total = _usage_event(3, {"total_tokens": float("inf")}, 5.0)

    assert render_tokens_per_s([non_numeric]) == ""
    assert render_tokens_per_s([bool_total]) == ""
    assert render_tokens_per_s([inf_total]) == ""


def test_active_workers_reuse_ready_never_negative() -> None:
    events = [_lifecycle("spawned"), _lifecycle("reuse_ready")]

    assert render_active_workers(events) == ""


def test_active_workers_clamps_a_lone_decrement() -> None:
    assert render_active_workers([_lifecycle("exit")]) == ""
    assert render_active_workers([_lifecycle("reuse_ready")]) == ""
