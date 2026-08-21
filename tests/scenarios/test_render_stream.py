"""Unit tests for the pure live-stream render helpers in cambium.render.

These helpers are side-effect free and deterministic: they only read
already-redacted event records (mappings with ``kind`` and ``payload`` keys)
and return a string.  No subprocesses, no network, no I/O.
"""

from __future__ import annotations

import json
import os
import shutil

from cambium.render import (
    render_active_workers,
    render_event_line,
    render_status_bar,
    render_tokens_per_s,
)


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


def _line(
    kind: str,
    payload: dict[str, object],
    *,
    seq: int | None = None,
    task_id: str | None = None,
) -> str:
    event: dict[str, object] = {"kind": kind, "payload": payload}
    if seq is not None:
        event["seq"] = seq
    if task_id is not None:
        event["task_id"] = task_id
    return render_event_line(event)  # type: ignore[arg-type]


def test_unknown_kind_keeps_raw_compact_json_dump() -> None:
    payload = {"zeta": 1, "alpha": "x"}
    line = _line("brand_new_kind", payload)

    kind_field, body = line.rsplit("  ", 1)
    assert kind_field == "brand_new_kind".rjust(16)
    assert json.loads(body) == payload


def test_missing_kind_falls_back_to_raw_dump_with_event_label() -> None:
    line = render_event_line({"seq": 4, "payload": {"a": 1}})

    assert line.startswith(f"{4:>6} {'event':>16}  ")
    assert json.loads(line.rsplit("  ", 1)[1]) == {"a": 1}


def test_prefix_shape_is_seq_kind_task_then_body() -> None:
    line = _line("ready", {"pid": 7}, seq=12, task_id="t1")

    assert line == f"{12:>6} {'ready':>16} t1  pid=7"


def test_silent_kinds_print_nothing() -> None:
    for kind in ("heartbeat", "log", "ping", "pong"):
        assert _line(kind, {"turn": 2}, seq=9, task_id="t") == ""


def test_usage_event_success_is_silent_failure_names_provider_and_reason() -> None:
    success = {
        "turn": 1,
        "provider": "p",
        "usage": {"total_tokens": 100},
    }
    failure = {
        "turn": 2,
        "provider": "codex",
        "failure_reason": "rate_limited: slow down",
    }

    assert _line("usage_event", success, seq=1, task_id="t") == ""
    assert _line("usage_event", failure, seq=2, task_id="t").endswith(
        "  provider codex FAILED rate_limited: slow down"
    )
    no_provider = {"failure_reason": "boom"}
    assert _line("usage_event", no_provider, seq=3, task_id="t").endswith(
        "  FAILED boom"
    )


def test_tool_event_ok_line_and_cmd_truncation() -> None:
    ok = _line(
        "tool_event",
        {"tool": "run_shell", "cmd": "git status", "ok": True, "duration_ms": 42},
        seq=5,
        task_id="t",
    )

    assert ok.endswith("  run_shell git status OK 42ms")

    long_cmd = "x" * 100
    truncated = _line(
        "tool_event", {"tool": "edit", "cmd": long_cmd, "ok": False}, seq=6, task_id="t"
    )

    assert truncated.endswith(f"  edit {'x' * 60} FAIL ?")
    assert len(long_cmd[:60]) == 60


def test_context_checkpoint_golden() -> None:
    line = _line(
        "context_checkpoint",
        {"epoch": 2, "turn": 14, "checkpoint_ref": "ckpt://t/2/14"},
        seq=8,
        task_id="t",
    )

    assert line.endswith("  epoch=2 turn=14 ckpt://t/2/14")


def test_context_epoch_advanced_appends_reason_and_folded_from_when_present() -> None:
    full = _line(
        "context_epoch_advanced",
        {
            "epoch": 3,
            "turn": 20,
            "checkpoint_ref": "ckpt://t/3/20",
            "folded_from_epoch": 2,
            "reason": "rolling_transcript_compaction",
        },
        seq=9,
        task_id="t",
    )

    assert full.endswith(
        "  epoch=3 turn=20 ckpt://t/3/20 reason=rolling_transcript_compaction"
        " folded_from=2"
    )

    bare = _line(
        "context_epoch_advanced",
        {"epoch": 1, "turn": 3, "checkpoint_ref": "ckpt://t/1/3"},
        seq=10,
        task_id="t",
    )

    assert bare.endswith("  epoch=1 turn=3 ckpt://t/1/3")


def test_checkpoint_golden() -> None:
    assert _line("checkpoint", {"turn": 7}, seq=3, task_id="t").endswith("  ckpt turn=7")


def test_lifecycle_kinds_one_concise_key_value_line_each() -> None:
    cases: list[tuple[str, dict[str, object], str]] = [
        ("spawned", {"worker": "/usr/bin/python3 -m cambium.worker"}, None),
        ("init", {"request_id": "rid-1"}, "request_id=rid-1"),
        ("run_task", {"request_id": "rid-2"}, "request_id=rid-2"),
        ("ready", {"pid": 4242, "proto": "cambium/1"}, "pid=4242"),
        ("reuse_ready", {"pid": 4242}, "pid=4242"),
        ("exit", {"reason": "clean_exit"}, "reason=clean_exit"),
        ("worker_failed", {"reason": "worker_detached_head"}, None),
        ("task_failed", {"reason": "marker missing"}, None),
        ("result", {"status": "succeeded"}, "status=succeeded"),
        ("result", {"status": "failed", "failure_reason": "timeout"}, None),
        ("session_ended", {"session_status": "ended", "results": {}}, "status=ended"),
        ("task_assigned", {"branch": "cambium/t1", "assigned_provider": "codex"}, None),
    ]
    for kind, payload, expected_body in cases:
        line = _line(kind, payload, seq=1, task_id="t")
        if expected_body is None:
            assert line and not line.endswith("  "), kind
            continue
        assert line.endswith(f"  {expected_body}"), kind


def test_spawned_worker_cmd_is_truncated_to_60_chars() -> None:
    line = _line("spawned", {"worker": "y" * 90}, seq=2, task_id="t")

    assert line.endswith(f"  worker={'y' * 60}")


def test_merge_worktree_and_child_kinds_golden() -> None:
    sha_old = "a" * 40
    sha_new = "b" * 40
    cases: list[tuple[str, dict[str, object], str]] = [
        ("merge_started", {"branch": "cambium/t1"}, "branch=cambium/t1"),
        (
            "merge_committed",
            {"branch": "cambium/t1", "old": sha_old, "new": sha_new},
            f"branch=cambium/t1 old={sha_old[:12]} new={sha_new[:12]}",
        ),
        ("worktree_created", {"branch": "cambium/t2"}, "branch=cambium/t2"),
        ("worktree_pruned", {"branch": "cambium/t2"}, "branch=cambium/t2"),
        (
            "context_fork",
            {"child_task_id": "child-1", "epoch": 4},
            "child=child-1 epoch=4",
        ),
        ("context_resume", {"epoch": 4, "child_count": 2}, "epoch=4 children=2"),
        (
            "child_admitted",
            {"child_task_id": "child-1", "branch": "cambium/child-1"},
            "child=child-1 branch=cambium/child-1",
        ),
    ]
    for kind, payload, expected_body in cases:
        assert _line(kind, payload, seq=1, task_id="t").endswith(f"  {expected_body}"), kind


def test_diagnostic_kinds_include_message_or_reason() -> None:
    cases: list[tuple[str, dict[str, object], str]] = [
        ("protocol", {"note": "run_task write failed"}, "note=run_task write failed"),
        (
            "protocol",
            {"error_type": "PROTO_UNKNOWN_REQUEST_ID", "message": "bad rid"},
            None,
        ),
        ("parse_error", {"message": "Expecting value: line 1 column 1"}, None),
        ("compaction_failed", {"epoch": 2, "reason": "provider_error"}, None),
        ("context_resume_failed", {"reason": "wall budget exhausted"}, None),
        (
            "child_rejected",
            {
                "child_task_id": "child-9",
                "reason": "ParentTerminatedWithoutResult",
                "message": "parent ended without a result envelope; proposal dropped",
            },
            None,
        ),
    ]
    for kind, payload, exact_body in cases:
        line = _line(kind, payload, seq=1, task_id="t")
        if exact_body is not None:
            assert line.endswith(f"  {exact_body}"), kind
            continue
        assert line and "msg=" in line or "reason=" in line or "note=" in line, kind


def test_non_mapping_payload_derives_body_from_extra_envelope_keys() -> None:
    event: dict[str, object] = {"kind": "exit", "task_id": "t", "reason": "done"}
    line = render_event_line(event)

    assert line.endswith("  reason=done")


def test_tool_event_cmd_control_characters_are_neutralized() -> None:
    hostile = "echo \x1b[31mINJECTED\x1b[0m\nrm -rf /"
    line = _line(
        "tool_event",
        {"tool": "run_shell", "cmd": hostile, "ok": True, "duration_ms": 5},
        seq=1,
        task_id="t",
    )

    body = line.rsplit("  ", 1)[1]
    assert "\x1b" not in line
    assert "\n" not in line
    assert "\x9b" not in line
    assert body == "run_shell echo [31mINJECTED[0m rm -rf / OK 5ms"


def test_tool_event_c1_csi_introducer_is_removed() -> None:
    line = _line(
        "tool_event",
        {"tool": "edit", "cmd": "a\x9b31mb", "ok": False},
        seq=2,
        task_id="t",
    )

    assert line.endswith("  edit a31mb FAIL ?")
    assert "\x9b" not in line


def test_truncation_applies_after_sanitization() -> None:
    cmd = "\x1b" + "x" * 70
    line = _line("tool_event", {"tool": "t", "cmd": cmd, "ok": True}, seq=3, task_id="t")

    body = line.rsplit("  ", 1)[1]
    assert "\x1b" not in line
    assert body == f"t {'x' * 60} OK ?"


def test_failure_and_diagnostic_message_fields_are_neutralized() -> None:
    failure = _line(
        "usage_event",
        {"provider": "p", "failure_reason": "boom\x1b[2Jboom\nsecond"},
        seq=4,
        task_id="t",
    )

    assert "\x1b" not in failure and "\n" not in failure
    assert failure.endswith("  provider p FAILED boom[2Jboom second")

    rejected = _line(
        "child_rejected",
        {"child_task_id": "c", "reason": "r\nx", "message": "m\x9bm"},
        seq=5,
        task_id="t",
    )

    assert "\x1b" not in rejected and "\x9b" not in rejected and "\n" not in rejected
    assert rejected.endswith("  child=c reason=r x msg=mm")


def test_unknown_kind_json_fallback_stays_escaped_single_line() -> None:
    payload = {"note": "raw\nnewline \x1b esc \x9b csi"}
    line = _line("brand_new_kind", payload)

    assert "\x1b" not in line and "\x9b" not in line and "\n" not in line
    assert json.loads(line.rsplit("  ", 1)[1])["note"] == payload["note"]


def _fixed_columns(monkeypatch: object, columns: int) -> None:
    monkeypatch.setattr(  # type: ignore[attr-defined]
        shutil, "get_terminal_size", lambda: os.terminal_size((columns, 24))
    )


def _bar_events() -> list[dict[str, object]]:
    return [
        {"kind": "spawned", "payload": {}, "task_id": "t1", "monotonic_ms": 0},
        {"kind": "ready", "payload": {"pid": 7}, "task_id": "t1", "monotonic_ms": 100},
        {
            "kind": "usage_event",
            "payload": {
                "turn": 1,
                "latency_s": 10.0,
                "provider": "p",
                "model": "m",
                "estimated_cost_usd": 0.0125,
                "usage": {
                    "input_tokens": 100,
                    "output_tokens": 50,
                    "total_tokens": 150,
                },
            },
            "task_id": "t1",
            "monotonic_ms": 200,
        },
    ]


def test_status_bar_full_golden_at_fixed_columns(monkeypatch: object) -> None:
    _fixed_columns(monkeypatch, 120)
    left = "session=sess · elapsed=0s · task=t1"
    right = "tokens/s=15.0 · in=100 out=50 cached=0 · cost=$0.012500 · subagents=1"
    expected = f"{left}{' ' * (120 - len(left) - len(right))}{right}"

    line = render_status_bar(_bar_events(), session_label="sess")

    assert line == expected
    assert len(line) == 120


def test_status_bar_narrow_terminal_drops_right_segments_first(
    monkeypatch: object,
) -> None:
    _fixed_columns(monkeypatch, 60)

    line = render_status_bar(_bar_events(), session_label="sess")

    assert len(line) == 60
    assert line.endswith("tokens/s=15.0")
    assert "cost=" not in line and "subagents=" not in line


def test_status_bar_no_events_is_empty() -> None:
    assert render_status_bar([], session_label="s") == ""
    assert render_status_bar(None, session_label="s") == ""


def test_status_bar_sanitizes_label_and_task_id(monkeypatch: object) -> None:
    _fixed_columns(monkeypatch, 100)

    line = render_status_bar(
        [{"kind": "spawned", "payload": {}, "task_id": "t\x1b[31mi\nx"}],
        session_label="se\x9b[2ms\ns",
    )

    assert "\x1b" not in line and "\x9b" not in line and "\n" not in line
    assert line.startswith("session=se[2ms s · ")
    assert " task=t[31mi x" in line


def test_status_bar_drops_absent_segments() -> None:
    line = render_status_bar([{"kind": "heartbeat", "payload": {}}], session_label="lab")

    assert line == "session=lab"
