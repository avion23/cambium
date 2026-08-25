"""Pure presentation tests for the persistent terminal cockpit."""

import io
from types import SimpleNamespace

import pytest

from cambium.tui_screen import (
    ActivityState,
    Cockpit,
    Transcript,
    _side_sections,
    _transcript_lines,
    render_cockpit,
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
        recent_events=(
            SimpleNamespace(kind="usage_event", detail="tokens=12345"),
        ),
    )


def test_wide_cockpit_has_transcript_agents_context_and_input() -> None:
    transcript = Transcript()
    transcript.user("Inspect the provider router")
    transcript.assistant("# Result\n- Found one issue\n```python\nprint('ok')\n```")
    lines = render_cockpit(
        _snapshot(),
        transcript,
        session_description="session=/tmp/run",
        branch_line="branch: turn=2 provider=codex model=gpt-5.6 epoch=4",
        cumulative_line="usage: calls=3 tokens=12345 out/s=47.5",
        width=120,
        height=32,
    )
    text = "\n".join(lines)
    assert len(lines) == 32
    assert "YOU" in text
    assert "CAMBIUM" in text
    assert "AGENTS" in text
    assert "CONTEXT" in text
    assert "SESSION USAGE" in text
    assert "codex/gpt-5.6" in text
    assert "segments 3" in text
    assert "│ › " in text
    assert lines[-1].startswith("└")


def test_compact_cockpit_stays_bounded() -> None:
    transcript = Transcript()
    transcript.system("hello")
    lines = render_cockpit(
        _snapshot(),
        transcript,
        session_description="session=/tmp/run",
        branch_line="branch: turn=1",
        cumulative_line="usage: calls=1 tokens=10",
        width=72,
        height=22,
    )
    assert len(lines) == 22
    assert all(len(line) == 72 for line in lines)
    assert "status=running" in "\n".join(lines)
    assert "agents=1 active" not in "\n".join(lines)
    assert lines[-1].startswith("└")


def test_status_line_deduplicates_fields_and_shortens_checkpoint_hash() -> None:
    snapshot = _snapshot()
    checkpoint = "task/epoch-001-aaaaaaaaaaaaaaaa-bbbbbbbbbbbbbbbb.json"
    line = render_primary(
        snapshot,
        Transcript(),
        session_description=(
            "session=/tmp/run turn=2 branch=1 provider=codex model=gpt-5.6 "
            f"epoch=4 checkpoint={checkpoint}"
        ),
        branch_line="branch: generation=3 turn=2 provider=codex model=gpt-5.6 epoch=4",
        cumulative_line="usage: calls=3 tokens=12345 out/s=47.5",
        width=120,
    )[-1]

    assert len(line) <= 120
    assert line.count("provider=codex model=gpt-5.6") == 1
    assert "ckpt=aaaaaaaa" in line
    assert "aaaaaaaaaaaaaaaa" not in line


def test_primary_renderer_ends_with_compact_status_row() -> None:
    transcript = Transcript()
    transcript.user("Inspect the provider router")
    transcript.assistant("The append-only view keeps terminal scrollback.")

    lines = render_primary(
        _snapshot(),
        transcript,
        session_description="session=/tmp/run",
        branch_line="branch: turn=2 provider=codex model=gpt-5.6 epoch=4",
        cumulative_line="usage: calls=3 tokens=12345",
        width=96,
    )

    assert "YOU" in "\n".join(lines)
    assert "CAMBIUM" in "\n".join(lines)
    assert lines[-1].startswith("┌ Cambium · status=running")
    assert "provider=codex model=gpt-5.6" in lines[-1]


def test_cockpit_appends_to_primary_buffer_without_repainting() -> None:
    class _Tty(io.StringIO):
        def isatty(self) -> bool:
            return True

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
            cumulative_line="usage: calls=0",
        )
        first = stream.getvalue()
        cockpit.draw(
            _snapshot(),
            transcript,
            session_description="session",
            branch_line="branch",
            cumulative_line="usage: calls=0",
        )
        assert stream.getvalue() == first

        transcript.assistant("new output")
        cockpit.draw(
            _snapshot(),
            transcript,
            session_description="session",
            branch_line="branch",
            cumulative_line="usage: calls=1",
        )

    text = stream.getvalue()
    assert "new output" in text
    assert text.count("┌ Cambium") == 2
    assert "\x1b[?1049h" not in text
    assert "\x1b[H" not in text
    assert "\x1b[2J" not in text


def test_cockpit_coalesces_draws_while_input_line_is_active() -> None:
    class _Tty(io.StringIO):
        def isatty(self) -> bool:
            return True

    stream = _Tty()
    transcript = Transcript()
    cockpit = Cockpit(stream)
    with cockpit:
        cockpit.draw(
            _snapshot(),
            transcript,
            session_description="session",
            branch_line="branch",
            cumulative_line="usage: calls=0",
        )
        cockpit.move_to_input()
        transcript.assistant("deferred output")
        cockpit.draw(
            _snapshot(),
            transcript,
            session_description="session",
            branch_line="branch",
            cumulative_line="usage: calls=1",
        )
        assert "deferred output" not in stream.getvalue()
        cockpit.hide_cursor(commit=True)
        cockpit.flush()

    assert "deferred output" in stream.getvalue()


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
    transcript.observe_event(
        {"kind": "assistant_delta", "payload": {"delta": "# Findings\n"}}
    )
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

    assert "CAMBIUM · generating" in "\n".join(first)
    assert "Findings" in "\n".join(first)
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
        transcript.observe_event(
            {"kind": "assistant_delta", "payload": {"delta": "x"}}
        )

    assert any(entry.role == "tool" and "old" in entry.text for entry in transcript.entries)
    assert transcript.streaming_role == "assistant"
    assert len(transcript.streaming_text) <= 16_384
    assert transcript.streaming_text.endswith("x")
    lines = render_cockpit(
        _snapshot(),
        transcript,
        session_description="session",
        branch_line="branch",
        cumulative_line="usage: calls=0",
        width=80,
        height=22,
    )
    assert "CAMBIUM · generating" in "\n".join(lines)


def test_repeated_successful_tool_events_render_as_one_counter_line() -> None:
    transcript = Transcript()
    for duration in (141, 512, 900, 2395):
        transcript.observe_event(
            {
                "kind": "tool_event",
                "payload": {
                    "tool": "run_shell",
                    "ok": True,
                    "duration_ms": duration,
                },
            }
        )

    lines = render_cockpit(
        _snapshot(),
        transcript,
        session_description="session",
        branch_line="branch",
        cumulative_line="usage: calls=4",
        width=80,
        height=24,
    )
    text = "\n".join(lines)

    assert len(transcript.entries) == 4
    assert "✓ run_shell ×4 · last 2395ms" in text
    assert text.count("run_shell") == 1


def test_tool_detail_toggle_reveals_command_and_output_without_mutating_state() -> None:
    transcript = Transcript()
    transcript.observe_event(
        {
            "kind": "tool_event",
            "payload": {
                "tool": "run_shell",
                "ok": True,
                "cmd": "printf 'hello'",
                "output": "hello\nfull output line",
            },
        }
    )

    compact = "\n".join(text for _, text in _transcript_lines(transcript, 80, 20))
    assert "printf 'hello'" not in compact
    assert "full output line" not in compact

    assert transcript.toggle_tool_details() is True
    expanded = "\n".join(text for _, text in _transcript_lines(transcript, 80, 20))
    assert "cmd: printf 'hello'" in expanded
    assert "full output line" in expanded
    assert "full output line" in transcript.entries[0].text


def test_expanded_tool_output_is_bounded_without_truncating_entry_state() -> None:
    transcript = Transcript()
    output = "\n".join(f"output-{index}" for index in range(100))
    transcript.observe_event(
        {
            "kind": "tool_event",
            "payload": {
                "tool": "run_shell",
                "ok": True,
                "cmd": "long-command",
                "output": output,
            },
        }
    )
    transcript.toggle_tool_details()

    rows = _transcript_lines(transcript, 80, 100)
    rendered = "\n".join(text for _, text in rows)
    output_rows = [text for _, text in rows if "output-" in text]
    assert "…" in rendered
    assert "lines hidden" in rendered
    assert "output-0" not in rendered
    assert "output-99" in rendered
    assert len(output_rows) <= 40
    assert "output-0" in transcript.entries[0].text
    assert "output-99" in transcript.entries[0].text


def test_failed_tool_detail_is_expanded_automatically() -> None:
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
    assert "permission denied" in text
    assert "stderr: access blocked" in text
    assert "cat protected.txt" in text


def test_failed_tool_event_breaks_runs_and_feeds_failure_context() -> None:
    transcript = Transcript()
    for event in (
        {"tool": "run_shell", "ok": True, "duration_ms": 141},
        {
            "tool": "run_shell",
            "ok": False,
            "duration_ms": 9273,
            "error": "permission denied",
        },
        {"tool": "run_shell", "ok": True, "duration_ms": 2395},
    ):
        transcript.observe_event({"kind": "tool_event", "payload": event})

    lines = render_cockpit(
        _snapshot(),
        transcript,
        session_description="session",
        branch_line="branch",
        cumulative_line="usage: calls=3",
        width=80,
        height=24,
    )
    text = "\n".join(lines)

    # Failed tool events are owned by the consolidated failure block, not the
    # transcript: successes render as compact lines, the failure does not
    # duplicate as a transcript entry.
    assert "✓ run_shell ×2 · last 2395ms" in text
    assert "✗ run_shell 9273ms" not in text
    assert len(transcript.entries) == 2


def test_mixed_successful_tools_do_not_collapse_across_each_other() -> None:
    transcript = Transcript()
    for event in (
        {"tool": "git_op", "ok": True, "duration_ms": 141},
        {"tool": "run_shell", "ok": True, "duration_ms": 9273},
        {"tool": "git_op", "ok": True, "duration_ms": 2395},
    ):
        transcript.observe_event({"kind": "tool_event", "payload": event})

    lines = render_cockpit(
        _snapshot(),
        transcript,
        session_description="session",
        branch_line="branch",
        cumulative_line="usage: calls=3",
        width=80,
        height=24,
    )
    text = "\n".join(lines)

    assert "✓ git_op 141ms" in text
    assert "✓ run_shell 9273ms" in text
    assert "✓ git_op 2395ms" in text
    assert "×" not in text
def test_failed_turn_is_one_consolidated_block_with_preceding_context() -> None:
    transcript = Transcript()
    events = (
        {
            "kind": "tool_event",
            "task_id": "task-timeout",
            "generation": 1,
            "payload": {"tool": "run_shell", "ok": False, "turn": 1},
        },
        {
            "kind": "timeout",
            "task_id": "task-timeout",
            "generation": 1,
            "payload": {"phase": "wall"},
        },
        {
            "kind": "worker_failed",
            "task_id": "task-timeout",
            "generation": 1,
            "payload": {"reason": "wall", "max_restarts": 0},
        },
    )

    for event in events:
        transcript.observe_event(event)

    transcript.finish_stream(
        "plan=tasks:1 plan_status={failed} "
        "plan_failures={task-timeout:'max_restarts (0): wall'}"
    )

    failures = [entry for entry in transcript.entries if entry.role == "error"]
    assert len(failures) == 1
    assert "task_id=task-timeout" in failures[0].text
    assert "cause=max_restarts (0): wall" in failures[0].text
    assert "↳ run_shell: failed" in failures[0].text
    assert "↳ timeout: wall" in failures[0].text
    assert [entry.text for entry in transcript.entries if entry.role == "assistant"] == [
        "plan=failed"
    ]


def test_repeated_failure_events_do_not_duplicate_the_block_or_cause() -> None:
    transcript = Transcript()
    event = {
        "kind": "worker_failed",
        "task_id": "task-repeat",
        "generation": 1,
        "payload": {"reason": "wall", "max_restarts": 0},
    }
    for _ in range(3):
        transcript.observe_event(dict(event))
        transcript.observe_event(
            {
                "kind": "result",
                "task_id": "task-repeat",
                "generation": 1,
                "payload": {"status": "failed"},
            }
        )

    failures = [entry for entry in transcript.entries if entry.role == "error"]
    assert len(failures) == 1
    assert failures[0].text.count("task-repeat") == 1
    assert failures[0].text.count("max_restarts (0): wall") == 1


@pytest.mark.parametrize("width", [24, 32, 48])
def test_side_sections_are_width_safe(width: int) -> None:
    snapshot = _snapshot()
    snapshot.agents = (
        SimpleNamespace(
            task_id="a-very-long-interactive-task-id",
            role="main",
            state="active",
            provider="codex",
            model="gpt-5.6",
            tool="read_batch",
            total_tokens=12345,
            output_tokens_per_s=12.4,
        ),
    )
    snapshot.recent_events = (
        SimpleNamespace(kind="worktree_cleanup", detail="internal"),
        SimpleNamespace(kind="dirty", detail="internal"),
        SimpleNamespace(kind="result", detail="published"),
    )

    rows = _side_sections(
        snapshot,
        "usage: calls=19 summaries=2 tokens=104000 (in=101000 out=2700 cached=93000) "
        "out/s=12.4 cost=$0.000000",
        width,
        100,
    )

    assert rows
    assert all("\n" not in line and len(line) <= width for _, line in rows)
    text = "\n".join(line for _, line in rows)
    assert "worktree_cleanup" not in text
    assert "dirty" not in text
    assert any(
        line.strip().startswith("cost") and line.rstrip().endswith("free")
        for line in text.splitlines()
    )

    usage_columns = []
    for label, value in (("calls", "19"), ("tokens", "104k"), ("out/s", "12.4"), ("cost", "free")):
        line = next(line for line in text.splitlines() if line.strip().startswith(label))
        usage_columns.append(line.index(value))
    assert len(set(usage_columns)) == 1

    task_line = next(line for line in text.splitlines() if line.startswith(" M "))
    model_line = next(line for line in text.splitlines() if line.startswith("   codex/"))
    stats_line = next(line for line in text.splitlines() if line.startswith("   12.3k"))
    assert task_line.index("M") == 1
    assert task_line.index("a-") == model_line.index("codex") == stats_line.index("12.3k")



def test_activity_state_transitions_thinking_responding_tool_and_done() -> None:
    activity = ActivityState()
    activity.start(now=10.0)

    thinking = activity.render(now=10.0)
    assert thinking.startswith("⠋ ")
    assert "thinking… 0.0s" in thinking
    assert activity.tick(now=10.1).startswith("⠙ ")

    activity.observe_event(
        {"kind": "assistant_delta", "payload": {"delta": "I will inspect this."}},
        now=11.0,
    )
    assert "responding… 2.0s" in activity.render(now=12.0)

    activity.observe_event(
        {
            "kind": "tool_start",
            "payload": {"tool": "run_shell", "tool_call_id": "call-1"},
        },
        now=13.0,
    )
    running = activity.render(now=14.5)
    assert "running run_shell 1.5s" in running
    assert "turn 4.5s" in running
    frame = render_cockpit(
        _snapshot(),
        Transcript(),
        session_description="session",
        branch_line="branch",
        cumulative_line="usage: calls=0",
        width=80,
        height=22,
        activity_line=running,
    )
    assert any("running run_shell 1.5s" in line for line in frame)

    activity.observe_event(
        {
            "kind": "tool_event",
            "payload": {
                "tool": "run_shell",
                "tool_call_id": "call-1",
                "ok": True,
                "duration_ms": 1500,
            },
        },
        now=15.0,
    )
    assert "thinking… 5.0s" in activity.render(now=15.0)

    activity.stop()
    assert activity.render(now=16.0) == ""


def test_activity_redraw_is_silent_for_non_tty() -> None:
    stream = io.StringIO()
    cockpit = Cockpit(stream)

    cockpit.draw_activity("⠋ thinking… 1.0s")

    assert stream.getvalue() == ""


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
    assert "running run_shell" in activity.render(now=3.0)

    activity.observe_event(
        {
            "kind": "tool_completed",
            "payload": {"tool": "run_shell", "tool_call_id": "call-1"},
        },
        now=4.0,
    )
    assert "running run_shell" not in activity.render(now=4.0)
