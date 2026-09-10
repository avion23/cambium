"""Behavioral presentation tests for the persistent terminal cockpit.

Exact paint sequences, palette choices, and internal row choreography belong to
manual/PTY coverage. Keep this file focused on user-visible state, bounded
streaming, and terminal-safety invariants.
"""

import io
from types import SimpleNamespace

import pytest
from _helpers_g2 import _Tty  # type: ignore[reportMissingImports]

from cambium import tui_screen
from cambium.tui import _safe_live_draw
from cambium.tui_screen import (
    ActivityState,
    Cockpit,
    Transcript,
    _visible,
    render_markdown_lines,
    render_primary,
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


def test_usage_event_updates_status_metadata_without_duplicate_transcript_text() -> None:
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
    assert transcript.status_metadata["provider"] == "zai"
    assert transcript.status_metadata["model"] == "glm-5.3"
    assert transcript.entries == ()


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
    assert len(clipped) <= 320

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


def test_child_lifecycle_events_form_bounded_linear_timeline() -> None:
    transcript = Transcript()
    task = (
        "inspect README and report the provider routing path "
        + ("detail " * 80)
        + "private-tail"
    )
    events = [
        {
            "kind": "child_admitted",
            "task_id": "parent",
            "payload": {
                "parent_task_id": "parent",
                "child_task_id": "child-7",
                "child_kind": "analysis",
            },
        },
        {
            "kind": "task_assigned",
            "task_id": "child-7",
            "payload": {
                "parent_task_id": "parent",
                "task": task,
                "assigned_provider": "zai",
                "provider": "zai",
                "model": "glm-5.3",
            },
        },
        {
            "kind": "tool_output_delta",
            "task_id": "child-7",
            "payload": {
                "tool": "read_batch",
                "tool_call_id": "read-7",
                "stream": "stdout",
                "delta": "child output\n",
            },
        },
        {
            "kind": "tool_event",
            "task_id": "child-7",
            "payload": {
                "tool": "read_batch",
                "tool_call_id": "read-7",
                "cmd": "read README.md",
                "ok": True,
                "duration_ms": 12,
                "output": "child output",
            },
        },
        {
            "kind": "child_result",
            "task_id": "child-7",
            "payload": {
                "parent_task_id": "parent",
                "status": "succeeded",
                "summary": "analysis complete",
            },
        },
        {
            "kind": "result",
            "task_id": "parent",
            "payload": {"status": "suspended"},
        },
        {
            "kind": "context_resume",
            "task_id": "parent",
            "payload": {
                "epoch": 2,
                "checkpoint_ref": "parent/epoch-0002.json",
                "child_count": 1,
                "workspace_changed": True,
            },
        },
        {
            "kind": "child_failed",
            "task_id": "child-8",
            "payload": {
                "parent_task_id": "parent",
                "reason": "provider timeout",
            },
        },
        {
            "kind": "child_result",
            "task_id": "child-8",
            "payload": {
                "parent_task_id": "parent",
                "status": "failed",
                "summary": "provider timeout",
            },
        },
    ]
    for event in events:
        transcript.observe_event(event)

    text = "\n".join(entry.text for entry in transcript.entries)
    assert "child-7" in text and "child-8" in text
    assert "zai" in text and "glm-5.3" in text
    assert "read_batch" in text and "child-7" in text
    assert "child output" in text
    assert "succeeded" in text and "failed" in text
    assert "waiting" in text and "resume" in text
    assert "inspect README" in text
    assert "private-tail" not in text
    assert len(text) < 4_000


def test_heartbeats_and_private_provider_action_content_stay_out_of_timeline() -> None:
    transcript = Transcript()
    transcript.observe_event(
        {
            "kind": "heartbeat",
            "task_id": "parent",
            "payload": {
                "phase": "thinking",
                "tail": "SECRET_REASONING should never enter scrollback",
            },
        }
    )
    transcript.observe_event(
        {
            "kind": "response",
            "task_id": "parent",
            "payload": {
                "text": (
                    '{"type":"tool_call","calls":[{"name":"run_shell",'
                    '"arguments":{"cmd":"cat SECRET_ACTION"}}]}'
                )
            },
        }
    )
    transcript.observe_event(
        {
            "kind": "result",
            "task_id": "parent",
            "payload": {
                "status": "succeeded",
                "terminal_action": {
                    "type": "tool_call",
                    "calls": [{"name": "run_shell", "arguments": {"cmd": "cat SECRET_ACTION"}}],
                },
            },
        }
    )
    transcript.observe_event(
        {
            "kind": "checkpoint",
            "payload": {
                "paths": ['{"type":"tool_call","arguments":{"cmd":"cat SECRET_PATH"}}']
            },
        }
    )
    transcript.finish_stream()

    rendered = "\n".join(
        render_primary(
            _snapshot(),
            transcript,
            session_description="session",
            branch_line="branch",
            cumulative_line="usage: calls=0",
            width=100,
            color=False,
        )
    )
    assert "SECRET_REASONING" not in rendered
    assert "SECRET_ACTION" not in rendered
    assert "SECRET_PATH" not in rendered
    assert '"tool_call"' not in rendered


def test_nested_reasoning_content_stays_out_of_timeline() -> None:
    transcript = Transcript()
    transcript.observe_event(
        {
            "kind": "response",
            "task_id": "parent",
            "payload": {
                "content": [
                    {"type": "reasoning", "text": "SECRET_REASONING"},
                    {"type": "text", "text": "visible answer"},
                ]
            },
        }
    )
    transcript.finish_stream()

    rendered = "\n".join(
        render_primary(
            _snapshot(),
            transcript,
            session_description="",
            branch_line="",
            cumulative_line="",
            width=80,
            color=False,
        )
    )
    assert "SECRET_REASONING" not in rendered
    assert "visible answer" not in rendered


def test_private_heartbeat_tail_is_not_status_text() -> None:
    activity = ActivityState()
    activity.start(now=1.0)
    activity.observe_event(
        {
            "kind": "heartbeat",
            "payload": {
                "phase": "streaming",
                "tail": '{"type":"tool_call","arguments":{"cmd":"SECRET"}}',
            },
        },
        now=2.0,
    )
    assert "tool_call" not in activity.render(now=3.0)
    assert "SECRET" not in activity.render(now=3.0)


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


def test_live_cockpit_keeps_timeline_and_one_transient_status_input_pair(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        tui_screen.shutil,
        "get_terminal_size",
        lambda _default: tui_screen.os.terminal_size((80, 24)),
    )
    stream = _Tty()
    transcript = Transcript()
    transcript.system("ready")
    cockpit = Cockpit(stream)
    with cockpit:
        cockpit.draw(
            _snapshot(),
            transcript,
            session_description="session",
            branch_line="branch",
            cumulative_line="usage: calls=1 tokens=12",
            activity_line="◌ THINKING · 1s",
            turn_active=True,
        )
        first = stream.getvalue()
        assert "SYSTEM ▸ ready" in first
        assert "OPERATOR RAIL" not in first
        assert "┌ Cambium · conversation" not in first
        assert all(marker not in first for marker in ("\x1b[?1049h", "\x1b[2J", "\x1b[H"))

        cockpit.move_to_input()
        cockpit.set_input("draft", 5)
        transcript.assistant("completed output")
        cockpit.draw(
            _snapshot(),
            transcript,
            session_description="session",
            branch_line="branch",
            cumulative_line="usage: calls=2 tokens=20",
            activity_line="▸ STREAMING · 2s",
            turn_active=True,
        )
        updated = stream.getvalue()
        assert "CAMBIUM ▸ completed output" in updated
        assert "\x1b[1A" in updated[len(first) :]
        assert updated.count("OPERATOR RAIL") == 0

        unchanged = stream.getvalue()
        cockpit.draw(
            _snapshot(),
            transcript,
            session_description="session",
            branch_line="branch",
            cumulative_line="usage: calls=2 tokens=20",
            activity_line="▸ STREAMING · 2s",
            turn_active=True,
        )
        assert stream.getvalue() == unchanged


def test_live_status_update_does_not_replay_retained_timeline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        tui_screen.shutil,
        "get_terminal_size",
        lambda _default: tui_screen.os.terminal_size((80, 24)),
    )
    stream = _Tty()
    transcript = Transcript()
    transcript.assistant("retained history")
    cockpit = Cockpit(stream)
    with cockpit:
        cockpit.draw(
            _snapshot(),
            transcript,
            session_description="session",
            branch_line="branch",
            cumulative_line="usage: calls=1",
            activity_line="◌ THINKING · 1s",
        )
        first = stream.getvalue()
        transcript.observe_event(
            {
                "kind": "heartbeat",
                "task_id": "interactive-main",
                "payload": {"phase": "thinking", "phase_revision": 2},
            }
        )
        cockpit.draw(
            _snapshot(),
            transcript,
            session_description="session",
            branch_line="branch",
            cumulative_line="usage: calls=1",
            activity_line="◌ THINKING · 2s",
        )

    delta = stream.getvalue()[len(first) :]
    assert "retained history" not in delta
    assert "THINKING" in delta


def test_repeated_tool_failure_status_does_not_replay_retained_timeline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        tui_screen.shutil,
        "get_terminal_size",
        lambda _default: tui_screen.os.terminal_size((80, 24)),
    )
    stream = _Tty()
    transcript = Transcript()
    for index in range(159):
        transcript.system(f"history-{index}")
    cockpit = Cockpit(stream)
    failed_tool = {
        "kind": "tool_event",
        "task_id": "child-a",
        "payload": {"tool": "run_shell", "ok": False, "error": "blocked"},
    }
    with cockpit:
        cockpit.draw(
            _snapshot(),
            transcript,
            session_description="",
            branch_line="",
            cumulative_line="",
        )
        transcript.observe_event(failed_tool)
        cockpit.draw(
            _snapshot(),
            transcript,
            session_description="",
            branch_line="",
            cumulative_line="",
        )
        first = stream.getvalue()
        transcript.observe_event(failed_tool)
        cockpit.draw(
            _snapshot(),
            transcript,
            session_description="",
            branch_line="",
            cumulative_line="",
        )

    delta = stream.getvalue()[len(first) :]
    assert "history-0" not in delta
    assert "err2" in delta


def test_live_resize_replaces_transient_rows_without_replaying_history(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sizes = iter(
        (
            tui_screen.os.terminal_size((80, 24)),
            tui_screen.os.terminal_size((40, 24)),
        )
    )
    monkeypatch.setattr(tui_screen.shutil, "get_terminal_size", lambda _default: next(sizes))
    stream = _Tty()
    transcript = Transcript()
    transcript.assistant("resize history")
    cockpit = Cockpit(stream)
    with cockpit:
        cockpit.draw(
            _snapshot(),
            transcript,
            session_description="session",
            branch_line="branch",
            cumulative_line="usage: calls=0",
        )
        first = stream.getvalue()
        cockpit.draw(
            _snapshot(),
            transcript,
            session_description="session",
            branch_line="branch",
            cumulative_line="usage: calls=0",
            force=True,
        )

    delta = stream.getvalue()[len(first) :]
    assert "resize history" not in delta
    assert stream.getvalue().count("CAMBIUM ▸ resize history") == 1


def test_live_status_prioritizes_owner_phase_provider_tool_and_detail_stays_one_row() -> None:
    agent = SimpleNamespace(
        task_id="child-a",
        role="subagent",
        state="active",
        provider="zai",
        model="glm-5",
        tool="run_shell",
        total_tokens=1234,
    )
    snapshot = SimpleNamespace(
        session_status="running",
        agents=(agent,),
        active_agents=1,
        queued_agents=0,
        total_tokens=1234,
        calls=2,
        context=SimpleNamespace(epoch=3),
    )
    transcript = Transcript()
    transcript.observe_event(
        {
            "kind": "heartbeat",
            "task_id": "child-a",
            "payload": {
                "phase": "thinking",
                "provider": "zai",
                "model": "glm-5",
                "tool": "run_shell",
            },
        }
    )
    status = tui_screen._status_line(
        snapshot,
        transcript,
        session_description="",
        branch_line="",
        cumulative_line="usage: calls=2 tokens=1234",
        width=120,
        activity_line="◌ THINKING · 2s",
    )
    assert all(value in status for value in ("owner=child-a", "zai/glm-5", "tool=run_shell"))
    detailed = tui_screen._status_line(
        snapshot,
        transcript,
        session_description="",
        branch_line="",
        cumulative_line="usage: calls=2 tokens=1234",
        width=120,
        activity_line="◌ THINKING · 2s",
        show_detail=True,
    )
    assert len(detailed.splitlines()) == 1
    assert "agents=" in detailed and "ctx=e3" in detailed


def test_live_resize_does_not_replay_timeline_history(monkeypatch: pytest.MonkeyPatch) -> None:
    sizes = iter(
        (
            tui_screen.os.terminal_size((80, 24)),
            tui_screen.os.terminal_size((80, 24)),
            tui_screen.os.terminal_size((40, 24)),
            tui_screen.os.terminal_size((40, 24)),
        )
    )
    monkeypatch.setattr(tui_screen.shutil, "get_terminal_size", lambda _default: next(sizes))
    stream = _Tty()
    transcript = Transcript()
    transcript.assistant("history row")
    cockpit = Cockpit(stream)
    with cockpit:
        cockpit.draw(
            _snapshot(),
            transcript,
            session_description="session",
            branch_line="branch",
            cumulative_line="usage: calls=0",
        )
        cockpit.draw(
            _snapshot(),
            transcript,
            session_description="session",
            branch_line="branch",
            cumulative_line="usage: calls=0",
            force=True,
        )
        transcript.assistant("new row")
        cockpit.draw(
            _snapshot(),
            transcript,
            session_description="session",
            branch_line="branch",
            cumulative_line="usage: calls=0",
        )
    rendered = stream.getvalue()
    assert rendered.count("CAMBIUM ▸ history row") == 1
    assert rendered.count("CAMBIUM ▸ new row") == 1


def test_concurrent_child_tool_streams_keep_distinct_history() -> None:
    transcript = Transcript()
    for task, call_id, delta in (
        ("child-a", "a-1", "A-1"),
        ("child-b", "b-1", "B-1"),
        ("child-a", "a-1", "A-2"),
        ("child-b", "b-1", "B-2"),
    ):
        transcript.observe_event(
            {
                "kind": "tool_output_delta",
                "task_id": task,
                "payload": {
                    "tool": "run_shell",
                    "tool_call_id": call_id,
                    "delta": delta,
                },
            }
        )
    transcript.observe_event(
        {
            "kind": "tool_event",
            "task_id": "child-a",
            "payload": {"tool": "run_shell", "tool_call_id": "a-1", "ok": True},
        }
    )
    transcript.observe_event(
        {
            "kind": "tool_event",
            "task_id": "child-b",
            "payload": {"tool": "run_shell", "tool_call_id": "b-1", "ok": True},
        }
    )
    texts = [entry.text for entry in transcript.entries]
    assert all(not ("A-" in text and "B-" in text) for text in texts)
    assert all(any(fragment in text for text in texts) for fragment in ("A-1", "A-2", "B-1", "B-2"))


def test_live_stream_switch_does_not_repeat_committed_tool_tail(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        tui_screen.shutil,
        "get_terminal_size",
        lambda _default: tui_screen.os.terminal_size((80, 24)),
    )
    stream = _Tty()
    transcript = Transcript()
    cockpit = Cockpit(stream)
    with cockpit:
        transcript.observe_event(
            {
                "kind": "tool_output_delta",
                "task_id": "child-a",
                "payload": {"tool": "run_shell", "tool_call_id": "a", "delta": "A1\n"},
            }
        )
        cockpit.draw(
            _snapshot(),
            transcript,
            session_description="",
            branch_line="",
            cumulative_line="",
        )
        transcript.observe_event(
            {
                "kind": "tool_output_delta",
                "task_id": "child-b",
                "payload": {"tool": "run_shell", "tool_call_id": "b", "delta": "B1\n"},
            }
        )
        cockpit.draw(
            _snapshot(),
            transcript,
            session_description="",
            branch_line="",
            cumulative_line="",
        )

    rendered = stream.getvalue()
    assert rendered.count("TOOL[child-a] ▸ [run_shell] A1") == 1
    assert rendered.count("TOOL[child-b] ▸ [run_shell] B1") == 1


def test_managed_native_input_uses_draft_and_keeps_status_row_position(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        tui_screen.shutil,
        "get_terminal_size",
        lambda _default: tui_screen.os.terminal_size((80, 24)),
    )
    stream = _Tty()
    transcript = Transcript()
    transcript.system("ready")
    cockpit = Cockpit(stream)
    with cockpit:
        cockpit.draw(
            _snapshot(),
            transcript,
            session_description="",
            branch_line="",
            cumulative_line="",
        )
        cockpit.move_to_input(native=True)
        cockpit.set_input("draft", 5)
        assert cockpit._input_line_text() == "draft"
        cockpit.hide_cursor(commit=True)
        transcript.assistant("after input")
        cockpit.draw(
            _snapshot(),
            transcript,
            session_description="",
            branch_line="",
            cumulative_line="",
            force=True,
        )

    rendered = stream.getvalue()
    assert rendered.count("SYSTEM ▸ ready") == 1
    assert "CAMBIUM ▸ after input" in rendered


def test_live_resize_keeps_stream_suffix_arriving_with_new_width(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sizes = iter(
        (
            tui_screen.os.terminal_size((80, 24)),
            tui_screen.os.terminal_size((80, 24)),
            tui_screen.os.terminal_size((40, 24)),
            tui_screen.os.terminal_size((40, 24)),
        )
    )
    monkeypatch.setattr(tui_screen.shutil, "get_terminal_size", lambda _default: next(sizes))
    stream = _Tty()
    transcript = Transcript()
    cockpit = Cockpit(stream)
    with cockpit:
        cockpit.draw(
            _snapshot(),
            transcript,
            session_description="",
            branch_line="",
            cumulative_line="",
        )
        transcript.observe_event({"kind": "assistant_delta", "payload": {"delta": "first\n"}})
        cockpit.draw(
            _snapshot(),
            transcript,
            session_description="",
            branch_line="",
            cumulative_line="",
        )
        transcript.observe_event({"kind": "assistant_delta", "payload": {"delta": "second\n"}})
        cockpit.draw(
            _snapshot(),
            transcript,
            session_description="",
            branch_line="",
            cumulative_line="",
        )
        transcript.observe_event({"kind": "assistant_delta", "payload": {"delta": "third\n"}})
        cockpit.draw(
            _snapshot(),
            transcript,
            session_description="",
            branch_line="",
            cumulative_line="",
        )

    rendered = stream.getvalue()
    assert rendered.count("CAMBIUM ▸ first") == 1
    assert rendered.count("CAMBIUM ▸ second") == 1
    assert rendered.count("CAMBIUM ▸ third") == 1


def test_live_completion_keeps_unbroken_stream_content_together(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        tui_screen.shutil,
        "get_terminal_size",
        lambda _default: tui_screen.os.terminal_size((80, 24)),
    )
    stream = _Tty()
    transcript = Transcript()
    cockpit = Cockpit(stream)
    with cockpit:
        cockpit.draw(
            _snapshot(),
            transcript,
            session_description="",
            branch_line="",
            cumulative_line="",
        )
        transcript.observe_event({"kind": "assistant_delta", "payload": {"delta": "one"}})
        cockpit.draw(
            _snapshot(),
            transcript,
            session_description="",
            branch_line="",
            cumulative_line="",
        )
        transcript.observe_event({"kind": "assistant_delta", "payload": {"delta": " two"}})
        transcript.finish_stream("one two")
        cockpit.draw(
            _snapshot(),
            transcript,
            session_description="",
            branch_line="",
            cumulative_line="",
            force=True,
        )

    rendered = stream.getvalue()
    assert rendered.count("CAMBIUM ▸ one two") == 1


def test_live_bounded_stream_rollover_does_not_replay_retained_tail(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        tui_screen.shutil,
        "get_terminal_size",
        lambda _default: tui_screen.os.terminal_size((80, 24)),
    )
    stream = _Tty()
    transcript = Transcript()
    cockpit = Cockpit(stream)
    with cockpit:
        transcript.observe_event({"kind": "assistant_delta", "payload": {"delta": "x\n" * 5000}})
        cockpit.draw(
            _snapshot(),
            transcript,
            session_description="",
            branch_line="",
            cumulative_line="",
        )
        first = stream.getvalue()
        transcript.observe_event({"kind": "assistant_delta", "payload": {"delta": "y\n" * 4000}})
        cockpit.draw(
            _snapshot(),
            transcript,
            session_description="",
            branch_line="",
            cumulative_line="",
        )

    delta = stream.getvalue()[len(first) :]
    assert "CAMBIUM ▸ x" not in delta
    assert "    x" not in delta
    assert "CAMBIUM ▸ y" in delta


def test_primary_renderer_has_one_status_row() -> None:
    lines = render_primary(
        _snapshot(),
        Transcript(),
        session_description="session",
        branch_line="branch",
        cumulative_line="usage: calls=0",
        width=80,
    )

    assert lines
    assert not any(line.startswith("┌") for line in lines)
    assert any("codex/gpt-5.6" in line for line in lines)
    assert sum(line.startswith("Cambium · ") for line in lines) == 1


def test_control_sequences_are_removed_and_color_is_opt_in() -> None:
    transcript = Transcript()
    transcript.error("bad\x1b[31m injected\x00 value")
    plain = render_primary(
        _snapshot(),
        transcript,
        session_description="session",
        branch_line="branch",
        cumulative_line="usage: calls=0",
        width=100,
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
    first = render_primary(
        _snapshot(),
        transcript,
        session_description="session",
        branch_line="branch",
        cumulative_line="usage: calls=0",
        width=80,
    )
    transcript.observe_event(
        {"kind": "assistant_delta", "payload": {"delta": "The stream is live."}}
    )
    second = render_primary(
        _snapshot(),
        transcript,
        session_description="session",
        branch_line="branch",
        cumulative_line="usage: calls=0",
        width=80,
    )

    assert "CAMBIUM ▸ Findings" in "\n".join(first)
    assert "The stream is live." in "\n".join(second)
    assert transcript.entries == ()

    transcript.finish_stream("# Findings\nThe stream is live.")
    assert transcript.streaming_text == ""
    assert transcript.entries[-1].text == "# Findings\nThe stream is live."


def test_accepted_response_chunks_use_one_assistant_timeline_stream() -> None:
    transcript = Transcript()
    transcript.observe_event(
        {
            "kind": "response_chunk",
            "request_id": "run-1",
            "payload": {"chunk_index": 0, "text": "# Result\n\nFull ", "final": False},
        }
    )
    transcript.observe_event(
        {
            "kind": "response_chunk",
            "request_id": "run-1",
            "payload": {"chunk_index": 1, "text": "operator response.", "final": True},
        }
    )

    assert transcript.streaming_role == "assistant"
    assert transcript.streaming_text == "# Result\n\nFull operator response."
    transcript.finish_stream()
    assert transcript.entries[-1].text == "# Result\n\nFull operator response."


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
    lines = render_primary(
        _snapshot(),
        transcript,
        session_description="session",
        branch_line="branch",
        cumulative_line="usage: calls=0",
        width=80,
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

    text = "\n".join(
        render_primary(
            _snapshot(),
            transcript,
            session_description="",
            branch_line="",
            cumulative_line="",
            width=100,
        )
    )
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
