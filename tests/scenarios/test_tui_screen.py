"""Pure presentation tests for the persistent terminal cockpit."""

from types import SimpleNamespace

import pytest

from cambium.tui_screen import Transcript, _side_sections, render_cockpit


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
