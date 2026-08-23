"""Pure presentation tests for the persistent terminal cockpit."""

from types import SimpleNamespace

from cambium.tui_screen import Transcript, render_cockpit


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
    assert "agents=1 active" in "\n".join(lines)
    assert lines[-1].startswith("└")


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


def test_failed_tool_event_breaks_runs_and_keeps_expanded_error() -> None:
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

    assert "✓ run_shell 141ms" in text
    assert "✗ run_shell 9273ms" in text
    assert "TOOL" in text
    assert "error: permission denied" in text
    assert "✓ run_shell 2395ms" in text
    assert "×3" not in text


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
