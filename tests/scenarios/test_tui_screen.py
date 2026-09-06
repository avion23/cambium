"""Behavioral presentation tests for the persistent terminal cockpit.

Exact paint sequences, palette choices, and internal row choreography belong to
manual/PTY coverage. Keep this file focused on user-visible state, bounded
streaming, and terminal-safety invariants.
"""

import io
from types import SimpleNamespace

from _helpers_g2 import _Tty  # type: ignore[reportMissingImports]

from cambium.tui import _safe_live_draw
from cambium.tui_screen import (
    ActivityState,
    Cockpit,
    Transcript,
    _transcript_lines,
    _visible,
    render_cockpit,
    render_markdown_lines,
)


def _snapshot():
    agent = SimpleNamespace(
        task_id="interactive-main",
        role="main",
        state="active",
        provider="codex",
        model="gpt-5.6",
        tool="read_batch",
        total_tokens=12345,
        output_tokens_per_s=47.5,
    )
    context = SimpleNamespace(
        epoch=4,
        summary_segments=3,
        approximate=True,
        estimated_trunk_tokens=9000,
        summary_trunk_bytes=32000,
        estimated_raw_tail_tokens=800,
        checkpoint_ref="interactive-main/epoch-0004.json",
    )
    return SimpleNamespace(
        session_status="running",
        agents=(agent,),
        active_agents=1,
        total_tokens=12345,
        output_tokens_per_s=47.5,
        context=context,
        recent_events=(SimpleNamespace(kind="usage_event", detail="tokens=12345"),),
    )


class _Utf8Tty(_Tty):
    def write(self, value: str) -> int:
        value.encode("utf-8")
        return super().write(value)


def test_conversation_markdown_is_structured_styled_and_sanitized() -> None:
    lines = render_markdown_lines(
        "# Heading\n\n**bold** *italic* `code`\n\n"
        "- a long list item that hangs on continuation\n\n"
        "> quote\n\n---\n\n```py\nprint('ok')\n```\n\n"
        "| a | b |\n|---|---|\n| one | two |",
        36,
    )
    visible = [_visible(line).rstrip() for line in lines]
    rendered = "\n".join(lines)

    assert visible[0].startswith("Heading")
    assert "bold" in rendered and "italic" in rendered and "code" in rendered
    assert "**" not in rendered and "`code`" not in rendered
    assert any(line.startswith("  ") for line in visible)
    assert any(line.startswith("  │") for line in visible)
    assert any("─" in line for line in visible)
    assert any(line.startswith("│ quote") for line in visible)
    assert "\x1b[" in rendered

    hostile = render_markdown_lines("safe\x1b[31m\x1b]2;secret\x07 text", 36)
    assert "secret" not in "\n".join(hostile)
    assert "\x1b[31m" not in "\n".join(hostile)


def test_activity_state_reports_waiting_streaming_done_error_and_cooldown() -> None:
    activity = ActivityState()
    activity.start(now=10.0)
    assert activity.state == "WAITING"
    assert "ORCHESTRATING" in activity.render(now=10.0)

    activity.observe_event(
        {
            "kind": "assistant_delta",
            "payload": {"delta": "first token", "output_tokens_per_s": 12.5},
        },
        now=11.0,
    )
    assert activity.state == "STREAMING"
    assert "STREAMING" in activity.render(now=11.0)
    assert "12.5 tok/s" in activity.render(now=11.0)

    activity.observe_event(
        {"kind": "usage_event", "payload": {"request_rate_status": "cooldown", "retry_after_s": 4}},
        now=12.0,
    )
    assert "COOLDOWN" in activity.render(now=12.0)

    activity.observe_event({"kind": "result", "payload": {"status": "succeeded"}}, now=13.0)
    assert activity.state == "DONE"
    assert activity.status_line() == "✓ DONE"

    activity.start(now=20.0)
    activity.observe_event({"kind": "turn_failed", "payload": {"reason": "provider"}}, now=21.0)
    assert activity.state == "ERROR"
    assert activity.status_line() == "✗ ERROR"


def test_activity_heartbeat_phase_tail_is_latest_sanitized_and_not_transcript() -> None:
    activity = ActivityState()
    activity.start(now=10.0)

    activity.observe_event(
        {
            "kind": "heartbeat",
            "payload": {"phase": "thinking", "tail": "read\n\x1b[31mconfig"},
        },
        now=11.0,
    )
    assert activity.render(now=13.0) == "◌ THINKING · 3s"

    activity.observe_event(
        {
            "kind": "heartbeat",
            "payload": {"phase": "streaming", "tail": "answer fragment"},
        },
        now=12.0,
    )
    assert "▸ STREAMING · 4s" in activity.render(now=14.0)
    assert "answer fragment" in activity.render(now=14.0)

    activity.observe_event(
        {"kind": "heartbeat", "payload": {"phase": "waiting", "tail": "stale tail"}},
        now=15.0,
    )
    assert activity.render(now=16.0) == "◒ PROVIDER · waiting 6s"

    transcript = Transcript()
    transcript.observe_event(
        {"kind": "heartbeat", "payload": {"phase": "thinking", "tail": "private tail"}}
    )
    assert transcript.entries == ()


def test_usage_event_updates_live_cache_without_duplicate_transcript_text() -> None:
    transcript = Transcript()
    transcript.observe_event(
        {
            "kind": "usage_event",
            "task_id": "root",
            "payload": {
                "provider": "zai",
                "model": "glm-5.3",
                "provider_cache_hit": False,
                "usage": {"total_tokens": 123, "completion_tokens": 7},
            },
        }
    )
    assert transcript._live_cache_hit is False
    assert transcript._live_text == ""


def test_child_rejected_lane_row_shows_reason_and_human_message() -> None:
    transcript = Transcript()
    transcript.observe_event(
        {
            "kind": "child_rejected",
            "task_id": "parent",
            "payload": {
                "child_task_id": "child-7",
                "reason": "ChildPolicyError",
                "message": "spec.tool 'run_shell' is not allowed for children",
            },
        }
    )
    assert (
        "child rejected: child-7 · ChildPolicyError"
        " · spec.tool 'run_shell' is not allowed for children"
    ) in [entry.text for entry in transcript.entries]


def test_child_rejected_lane_row_truncates_message_and_falls_back_to_reason() -> None:
    transcript = Transcript()
    transcript.observe_event(
        {
            "kind": "child_rejected",
            "task_id": "parent",
            "payload": {
                "child_task_id": "child-9",
                "reason": "ValidationError",
                "message": "x" * 300,
            },
        }
    )
    clipped = transcript.entries[-1].text
    assert clipped.startswith("child rejected: child-9 · ValidationError · ")
    assert clipped.endswith("…")
    assert "x" * 121 not in clipped

    transcript.observe_event(
        {
            "kind": "child_rejected",
            "task_id": "parent",
            "payload": {"child_task_id": "child-8", "reason": "BudgetExceeded"},
        }
    )
    assert "child rejected: child-8 · BudgetExceeded" in [
        entry.text for entry in transcript.entries
    ]


def test_activity_names_provider_and_cache_state_before_turn_finishes() -> None:
    activity = ActivityState()
    activity.start(now=10.0)
    activity.observe_event(
        {
            "kind": "heartbeat",
            "payload": {
                "phase": "waiting",
                "phase_revision": 1,
                "provider": "zai",
                "model": "glm-5.3",
                "tail": "zai/glm-5.3",
            },
        },
        now=11.0,
    )
    assert "PROVIDER · zai/glm-5.3 · waiting" in activity.render(now=12.0)

    activity.observe_event(
        {
            "kind": "usage_event",
            "payload": {
                "provider": "zai",
                "model": "glm-5.3",
                "provider_cache_hit": False,
            },
        },
        now=12.0,
    )
    activity.observe_event(
        {
            "kind": "heartbeat",
            "payload": {
                "phase": "thinking",
                "phase_revision": 2,
                "provider": "zai",
                "model": "glm-5.3",
            },
        },
        now=13.0,
    )
    assert activity.render(now=14.0) == "◌ THINKING · zai/glm-5.3 · 4s · cache MISS"


def test_activity_distinguishes_active_thinking_from_stalled_provider_or_tool() -> None:
    activity = ActivityState()
    activity.start(now=0.0)
    activity.observe_event(
        {"kind": "heartbeat", "payload": {"phase": "thinking", "phase_revision": 1}},
        now=1.0,
    )
    activity.observe_event(
        {"kind": "heartbeat", "payload": {"phase": "thinking", "phase_revision": 2}},
        now=10.0,
    )
    assert "stalled" not in activity.render(now=20.0)
    assert "stalled 13s" in activity.render(now=23.0)

    activity.observe_event(
        {"kind": "heartbeat", "payload": {"phase": "waiting", "phase_revision": 3}},
        now=24.0,
    )
    assert "PROVIDER · waiting" in activity.render(now=37.0)
    assert "silent 13s" in activity.render(now=37.0)

    activity.observe_event(
        {"kind": "tool_start", "payload": {"tool": "run_shell", "tool_call_id": "x"}},
        now=40.0,
    )
    assert "TOOL · run_shell" in activity.render(now=53.0)
    assert "no output 13s" in activity.render(now=53.0)


def test_suspended_activity_stays_live_and_has_distinct_status() -> None:
    activity = ActivityState()
    activity.start(now=0.0)
    activity.observe_event(
        {"kind": "result", "payload": {"status": "suspended"}},
        now=1.0,
    )

    assert activity.active
    assert activity.state == "SUSPENDED"
    assert "CHILDREN · waiting" in activity.render(now=1.0)
    assert activity.status_line() != "✓ DONE"


def test_tool_output_stream_rotates_per_tool_without_committing_a_mixture() -> None:
    transcript = Transcript()
    transcript.observe_event(
        {
            "kind": "tool_output_delta",
            "payload": {"tool": "run_shell", "stream": "stdout", "delta": "OLD-A"},
        }
    )
    transcript.observe_event(
        {
            "kind": "tool_output_delta",
            "payload": {"tool": "git_op", "stream": "stdout", "delta": "NEW-B"},
        }
    )

    assert transcript.streaming_role == "tool"
    assert transcript.streaming_text == "NEW-B"

    transcript.finish_stream("done")
    texts = [entry.text for entry in transcript.entries]
    assert any("OLD-A" in text for text in texts)
    assert any("NEW-B" in text for text in texts)
    assert all(not ("OLD-A" in text and "NEW-B" in text) for text in texts)


def test_short_terminal_falls_back_to_stream_rows() -> None:
    lines = render_cockpit(
        _snapshot(),
        Transcript(),
        session_description="session",
        branch_line="branch",
        cumulative_line="usage: calls=0",
        width=80,
        height=11,
    )

    assert lines
    assert not any(line.startswith("┌") for line in lines)
    assert any("codex/gpt-5.6" in line for line in lines)


def test_control_sequences_are_removed_and_color_is_opt_in() -> None:
    transcript = Transcript()
    transcript.error("bad\x1b[31m injected\x00 value")
    plain = render_cockpit(
        _snapshot(),
        transcript,
        session_description="session",
        branch_line="branch",
        cumulative_line="usage: calls=0",
        width=100,
        height=24,
        color=False,
    )
    assert "\x1b" not in "".join(plain)
    assert "injected" in "\n".join(plain)


def test_transcript_is_bounded() -> None:
    transcript = Transcript(max_entries=8)
    for index in range(20):
        transcript.system(f"entry {index}")
    assert len(transcript.entries) == 8
    assert transcript.entries[0].text == "entry 12"


def test_assistant_deltas_render_in_the_active_tail_before_turn_completion() -> None:
    transcript = Transcript()
    transcript.observe_event({"kind": "assistant_delta", "payload": {"delta": "# Findings\n"}})
    first = render_cockpit(
        _snapshot(),
        transcript,
        session_description="session",
        branch_line="branch",
        cumulative_line="usage: calls=0",
        width=80,
        height=22,
    )
    transcript.observe_event(
        {"kind": "assistant_delta", "payload": {"delta": "The stream is live."}}
    )
    second = render_cockpit(
        _snapshot(),
        transcript,
        session_description="session",
        branch_line="branch",
        cumulative_line="usage: calls=0",
        width=80,
        height=22,
    )

    assert "CAMBIUM ▸ Findings" in "\n".join(first)
    assert "The stream is live." in "\n".join(second)
    assert transcript.entries == ()

    transcript.finish_stream("# Findings\nThe stream is live.")
    assert transcript.streaming_text == ""
    assert transcript.entries[-1].text == "# Findings\nThe stream is live."


def test_message_events_switch_roles_and_keep_streaming_text_bounded() -> None:
    transcript = Transcript()
    transcript.observe_event(
        {
            "kind": "tool_event",
            "payload": {
                "tool": "read_batch",
                "message": "--- a.py ---\nold",
                "ok": True,
            },
        }
    )
    transcript.observe_event(
        {
            "kind": "message",
            "payload": {"role": "assistant", "content": "I found the issue."},
        }
    )
    for _ in range(20_000):
        transcript.observe_event({"kind": "assistant_delta", "payload": {"delta": "x"}})

    assert any(entry.role == "tool" and "old" in entry.text for entry in transcript.entries)
    assert transcript.streaming_role == "assistant"
    assert len(transcript.streaming_text) <= 16_384
    lines = render_cockpit(
        _snapshot(),
        transcript,
        session_description="session",
        branch_line="branch",
        cumulative_line="usage: calls=0",
        width=80,
        height=22,
    )
    assert "CAMBIUM ▸" in "\n".join(lines)


def test_failed_tool_event_is_one_compact_notice() -> None:
    transcript = Transcript()
    transcript.observe_event(
        {
            "kind": "tool_event",
            "payload": {
                "tool": "run_shell",
                "ok": False,
                "cmd": "cat protected.txt",
                "error": "permission denied",
                "output": "stderr: access blocked",
            },
        }
    )

    text = "\n".join(value for _, value in _transcript_lines(transcript, 80, 20))
    assert "tool errors:" not in text
    assert "permission denied" not in text
    assert "cat protected.txt" not in text
    assert transcript.tool_error_count == 1


def test_activity_keeps_tool_in_flight_until_matching_end() -> None:
    activity = ActivityState()
    activity.start(now=1.0)
    activity.observe_event(
        {
            "kind": "tool_started",
            "payload": {"tool": "run_shell", "tool_call_id": "call-1"},
        },
        now=2.0,
    )

    activity.observe_event(
        {
            "kind": "tool_completed",
            "payload": {"tool": "run_shell", "tool_call_id": "other-call"},
        },
        now=3.0,
    )
    assert "TOOL · run_shell" in activity.render(now=3.0)

    activity.observe_event(
        {
            "kind": "tool_completed",
            "payload": {"tool": "run_shell", "tool_call_id": "call-1"},
        },
        now=4.0,
    )
    assert "TOOL · run_shell" not in activity.render(now=4.0)


def test_restore_input_line_escapes_lone_surrogates_before_writing() -> None:
    stream = _Utf8Tty()
    cockpit = Cockpit(stream)
    with cockpit:
        cockpit.move_to_input()
        cockpit._restore_input_line("\udc80\udc81\udc82", force=True)

    rendered = stream.getvalue()
    assert r"\udc80\udc81\udc82" in rendered
    assert all(value not in rendered for value in ("\udc80", "\udc81", "\udc82"))


def test_live_draw_failure_is_contained_and_disables_rendering() -> None:
    error = io.StringIO()
    enabled = True

    def disable() -> None:
        nonlocal enabled
        enabled = False

    def fail() -> None:
        raise RuntimeError("render failed")

    enabled = _safe_live_draw(fail, error=error, disable=disable)

    assert enabled is False
    assert "live rendering disabled (RuntimeError)" in error.getvalue()
