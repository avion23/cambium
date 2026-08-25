"""Pure presentation tests for the persistent terminal cockpit."""

import io
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

import cambium.tui_screen as tui_screen
from cambium.tui import _queued_prompt_notice
from cambium.tui_screen import (
    ActivityState,
    Cockpit,
    Transcript,
    _bounded_markdown_lines,
    _display_width,
    _side_sections,
    _transcript_lines,
    _visible,
    _wrap_markdown,
    render_cockpit,
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
    assert "↳ Inspect the provider router" in text
    assert "provider=codex · model=gpt-5.6" in text
    assert "agents=1 active" in text
    assert "cost=" in text
    assert "checkpoint=" in text
    assert "│ input › " in text
    assert text.count("├") == 1
    assert lines[-1].startswith("└")


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
    table_header = next((line for line in visible if line.strip().startswith("a")), None)
    table_row = next((line for line in visible if line.strip().startswith("one")), None)
    if table_header is not None and table_row is not None:
        assert table_header.index("a") == table_row.index("one")
        assert table_header.index("b") == table_row.index("two")
    else:
        assert all(cell in "\n".join(visible) for cell in ("a", "b", "one", "two"))
    assert "\x1b[1m" in rendered
    assert "\x1b[33m" in rendered
    assert "\x1b[2;36m" in rendered

    hostile = render_markdown_lines("safe\x1b[31m\x1b]2;secret\x07 text", 36)
    assert "secret" not in "\n".join(hostile)
    assert "\x1b[31m" not in "\n".join(hostile)


def test_rich_markdown_sanitizes_before_the_parser_sees_text(monkeypatch) -> None:
    seen: list[str] = []
    rich_renderer = tui_screen._render_markdown_lines_rich

    def capture(text: str, width: int, color: bool) -> list[str]:
        seen.append(text)
        return rich_renderer(text, width, color)

    monkeypatch.setattr(tui_screen, "_render_markdown_lines_rich", capture)
    render_markdown_lines("safe\x1b[31m injected\x00\x1b]2;secret\x07 text", 36)

    assert seen == ["safe injected text"]


def test_markdown_falls_back_when_rich_import_is_unavailable(monkeypatch) -> None:
    text = "# Heading\n\n**bold** `code`\n\nvalue_with_underscores"
    expected = tui_screen._render_markdown_lines_fallback(text, 36, color=False)

    def unavailable(*args, **kwargs):
        raise ImportError("rich unavailable")

    monkeypatch.setattr(tui_screen, "_render_markdown_lines_rich", unavailable)
    assert render_markdown_lines(text, 36, color=False) == expected


def test_tui_screen_import_does_not_import_rich() -> None:
    source_root = Path(__file__).resolve().parents[2] / "src"
    probe = (
        "import sys; import cambium.tui_screen; "
        "assert not any(name == 'rich' or name.startswith('rich.') for name in sys.modules)"
    )
    subprocess.run(
        [sys.executable, "-c", probe],
        check=True,
        env={**os.environ, "PYTHONPATH": str(source_root)},
    )


def test_rich_path_keeps_literal_markup_text() -> None:
    pytest.importorskip("rich")
    rendered = "\n".join(render_markdown_lines("[bold]x[/bold]", 36, color=False))
    assert "[bold]x[/bold]" in rendered


@pytest.mark.parametrize(
    ("text", "cells", "width"),
    [
        (
            "| one | two |\n| --- | --- |\n| three | four |",
            ("one", "two", "three", "four"),
            8,
        ),
        (
            "| 界 | 文字 |\n| --- | --- |\n| 一 | 二三 |",
            ("界", "文字", "一", "二三"),
            10,
        ),
    ],
)
def test_narrow_tables_fall_back_without_losing_cells(
    text: str, cells: tuple[str, ...], width: int
) -> None:
    rendered = render_markdown_lines(text, width, color=False)
    visible = "\n".join(_visible(line) for line in rendered)
    assert all(cell in visible for cell in cells)
    assert all(_display_width(line) <= width for line in rendered)


def test_bounded_markdown_lines_limits_wrapped_rows() -> None:
    text = "\n".join("x" * 400 for _ in range(100))
    rendered = _bounded_markdown_lines(text, 20, 40, color=False)
    assert len(rendered) <= 40
    assert rendered[0].startswith("… ") and rendered[0].endswith(" lines hidden")


def test_resume_summary_identifiers_survive_deferred_startup_draw(monkeypatch) -> None:
    class _Tty(io.StringIO):
        def isatty(self) -> bool:
            return True

    monkeypatch.setenv("COLUMNS", "80")
    monkeypatch.setenv("LINES", "24")
    summary = (
        "Detected prior interactive session; resuming durable state: "
        "turns=1 last_epoch=7 last_checkpoint=interactive-main/epoch-7-"
        + "c" * 64
        + ".json. session=/tmp/interactive_session turn=1 branch=1 "
        "provider=provider-a model=model-a epoch=7"
    )
    rendered_summary = "\n".join(render_markdown_lines(summary, 73, color=False))
    assert "last_epoch=7" in rendered_summary
    assert "last_checkpoint=interactive-main/epoch-7-" in rendered_summary
    stream = _Tty()
    transcript = Transcript()
    transcript.user("durable prompt")
    transcript.assistant("durable answer")
    transcript.system(summary)
    cockpit = Cockpit(stream)

    with cockpit:
        cockpit.move_to_input()
        cockpit.draw(
            _snapshot(),
            transcript,
            session_description="session=/tmp/interactive_session",
            branch_line="branch: turn=1 provider=provider-a model=model-a epoch=7",
            cumulative_line="usage: calls=1 tokens=125",
        )
        assert "last_epoch=7" not in stream.getvalue()
        cockpit.hide_cursor(commit=True)
        cockpit.flush()

    rendered = stream.getvalue()
    assert "Detected prior interactive session" in rendered
    assert "last_epoch=7" in rendered
    assert "last_checkpoint=interactive-main/epoch-7-" in rendered


def test_transcript_blocks_are_dense_with_one_separator_and_no_trailing_blank() -> None:
    transcript = Transcript()
    transcript.user("prompt")
    transcript.assistant("first\n\n\nsecond\n\n")

    rows = _transcript_lines(transcript, 60, 100)
    values = [value for _, value in rows]
    assert values[-1].strip()
    assert max(
        sum(not value.strip() for value in values[index : index + 3])
        for index in range(max(1, len(values) - 2))
    ) <= 1
    assert values.count("") <= 1


def test_activity_state_reports_waiting_streaming_done_error_and_cooldown() -> None:
    activity = ActivityState()
    activity.start(now=10.0)
    assert activity.state == "WAITING"
    assert "WAITING" in activity.render(now=10.0)

    activity.observe_event(
        {
            "kind": "assistant_delta",
            "payload": {"delta": "first token", "output_tokens_per_s": 12.5},
        },
        now=11.0,
    )
    assert activity.state == "STREAMING"
    assert "STREAMING" in activity.render(now=11.0)
    assert "out/s=12.5" in activity.render(now=11.0)

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
    assert "conversation · running" in "\n".join(lines)
    assert "agents=1 active" in "\n".join(lines)
    assert lines[-1].startswith("└")


def test_long_words_wrap_without_clipping_content_or_frame_borders() -> None:
    long_word = "neveragainsttherunningliveservi" * 3
    transcript = Transcript()
    transcript.assistant(f"before {long_word} after")

    wrapped = _wrap_markdown(long_word, 17)
    assert "".join(wrapped) == long_word
    assert all(_display_width(line) <= 17 for line in wrapped)

    lines = render_cockpit(
        _snapshot(),
        transcript,
        session_description="session",
        branch_line="branch",
        cumulative_line="usage: calls=0",
        width=48,
        height=28,
        activity_line="running " + "界" * 80 + "\ud800",
    )
    conversation = "".join(
        value[3:]
        for role, value in _transcript_lines(transcript, 46, 100)
        if role == "assistant" and value.startswith("   ")
    )
    assert long_word in conversation
    assert all(_display_width(line) <= 48 for line in lines)
    assert lines[-1] == "└" + "─" * 46 + "┘"


def test_status_line_deduplicates_fields_and_shortens_checkpoint_hash() -> None:
    snapshot = _snapshot()
    checkpoint = "task/epoch-001-aaaaaaaaaaaaaaaa-bbbbbbbbbbbbbbbb.json"
    lines = render_primary(
        snapshot,
        Transcript(),
        session_description=(
            "session=/tmp/run turn=2 branch=1 provider=codex model=gpt-5.6 "
            f"epoch=4 checkpoint={checkpoint}"
        ),
        branch_line="branch: generation=3 turn=2 provider=codex model=gpt-5.6 epoch=4",
        cumulative_line="usage: calls=3 tokens=12345 out/s=47.5",
        width=120,
    )
    identity, usage, agents, context = lines[-4:]

    assert all(len(line) <= 120 for line in lines[-4:])
    assert identity.count("provider=codex · model=gpt-5.6") == 1
    assert "checkpoint=aaaaaaaa" in context
    assert "aaaaaaaaaaaaaaaa" not in context
    assert "tokens=" in usage
    assert "agents=" in agents


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
    assert lines[-4].startswith(" provider=codex · model=gpt-5.6")
    assert "tokens=" in lines[-3]


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


def test_cockpit_flushes_overflow_history_once() -> None:
    class _Tty(io.StringIO):
        def isatty(self) -> bool:
            return True

    stream = _Tty()
    transcript = Transcript()
    for index in range(20):
        transcript.system(f"restored-{index}")
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
        assert all(f"restored-{index}" in first for index in range(20))

        cockpit.draw(
            _snapshot(),
            transcript,
            session_description="session",
            branch_line="branch",
            cumulative_line="usage: calls=0",
        )
        assert stream.getvalue() == first


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


def test_cockpit_paints_mid_turn_tool_tick_while_input_is_pending() -> None:
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
        transcript.observe_event(
            {
                "kind": "tool_event",
                "payload": {"tool": "run_shell", "ok": True, "duration_ms": 118215},
            }
        )
        cockpit.draw(
            _snapshot(),
            transcript,
            session_description="session",
            branch_line="branch",
            cumulative_line="usage: calls=1",
            activity_line="⠋ WAITING",
            turn_active=True,
        )

        live_output = stream.getvalue()
        assert "✓ run_shell 118215ms" in live_output
        assert live_output.endswith("› ")
        assert cockpit._input_active


def test_cockpit_throttles_active_turn_frames(monkeypatch) -> None:
    class _Tty(io.StringIO):
        def isatty(self) -> bool:
            return True

    now = [10.0]
    monkeypatch.setattr("cambium.tui_screen.time.monotonic", lambda: now[0])
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
        for tool in ("run_shell", "read_batch"):
            transcript.observe_event(
                {"kind": "tool_event", "payload": {"tool": tool, "ok": True}}
            )
            cockpit.draw(
                _snapshot(),
                transcript,
                session_description="session",
                branch_line="branch",
                cumulative_line="usage: calls=2",
                activity_line="⠋ WAITING",
                turn_active=True,
            )
            if tool == "run_shell":
                now[0] = 10.05

        assert "✓ read_batch" not in stream.getvalue()
        now[0] = 10.11
        cockpit.draw(
            _snapshot(),
            transcript,
            session_description="session",
            branch_line="branch",
            cumulative_line="usage: calls=2",
            activity_line="⠋ WAITING",
            turn_active=True,
        )

    assert "✓ read_batch" in stream.getvalue()


def test_cockpit_replaces_failed_to_idle_status_in_place() -> None:
    class _Tty(io.StringIO):
        def isatty(self) -> bool:
            return True

    stream = _Tty()
    transcript = Transcript()
    snapshot = _snapshot()
    snapshot.agents[0].state = "failed"
    snapshot.active_agents = 0
    cockpit = Cockpit(stream)
    with cockpit:
        cockpit.draw(
            snapshot,
            transcript,
            session_description="session",
            branch_line="branch",
            cumulative_line="usage: calls=0",
        )
        cockpit.move_to_input()
        transcript.error("turn failed")
        cockpit.draw(
            snapshot,
            transcript,
            session_description="session",
            branch_line="branch",
            cumulative_line="usage: calls=0",
            activity_line="✗ ERROR",
            turn_active=True,
        )
        cockpit.draw(
            snapshot,
            transcript,
            session_description="session",
            branch_line="branch",
            cumulative_line="usage: calls=0",
            activity_line="✗ ERROR",
            force=True,
        )
        cockpit.draw(
            snapshot,
            transcript,
            session_description="session",
            branch_line="branch",
            cumulative_line="usage: calls=0",
        )
        cockpit.hide_cursor()
        cockpit.flush()

    agent_rows = [row for row in cockpit._last_status_rows if row.startswith(" agents=")]
    assert len(agent_rows) == 1
    assert agent_rows[0].endswith("state=IDLE")
    assert stream.getvalue().count("┌ Cambium · conversation") == 2


def test_cockpit_forces_completed_frame_while_input_read_is_pending() -> None:
    class _Tty(io.StringIO):
        flush_count = 0

        def isatty(self) -> bool:
            return True

        def flush(self) -> None:
            self.flush_count += 1
            super().flush()

    stream = _Tty()
    transcript = Transcript()
    cockpit = Cockpit(stream)
    final_snapshot = _snapshot()
    final_snapshot.session_status = "done"
    with cockpit:
        cockpit.draw(
            _snapshot(),
            transcript,
            session_description="session",
            branch_line="branch",
            cumulative_line="usage: calls=0",
        )
        cockpit.move_to_input()
        transcript.observe_event(
            {"kind": "assistant_delta", "payload": {"delta": "partial"}}
        )
        cockpit.draw(
            _snapshot(),
            transcript,
            session_description="session",
            branch_line="branch",
            cumulative_line="usage: calls=0",
        )
        before = stream.getvalue()
        flushes_before_completion = stream.flush_count

        transcript.finish_stream("completed response")
        cockpit.draw(
            final_snapshot,
            transcript,
            session_description="session",
            branch_line="branch",
            cumulative_line="usage: calls=1 tokens=20000",
            force=True,
        )

        after = stream.getvalue()
        assert "completed response" in after
        assert after.count("┌ Cambium · conversation") == 2
        assert "conversation · done" in after
        assert "tokens=20k" in after
        assert after != before
        assert stream.flush_count > flushes_before_completion


def test_cockpit_updates_fixed_status_pane_in_place() -> None:
    class _Tty(io.StringIO):
        def isatty(self) -> bool:
            return True

    stream = _Tty()
    cockpit = Cockpit(stream)
    transcript = Transcript()
    with cockpit:
        cockpit.draw(
            _snapshot(),
            transcript,
            session_description="session",
            branch_line="branch",
            cumulative_line="usage: calls=0",
        )
        first = stream.getvalue()
        cockpit.draw_activity("⠋ running run_shell")

    delta = stream.getvalue()[len(first) :]
    assert "\x1b[s" in delta
    assert "\x1b[4A" in delta
    assert "running run_shell" in delta
    assert "┌ Cambium · conversation" not in delta


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
    assert not any("─" in line for line in lines)
    assert any("provider=codex" in line for line in lines)


def test_live_cockpit_keeps_short_terminal_fallback(monkeypatch) -> None:
    class _Tty(io.StringIO):
        def isatty(self) -> bool:
            return True

    monkeypatch.setattr(
        tui_screen.shutil,
        "get_terminal_size",
        lambda _fallback: os.terminal_size((80, 11)),
    )
    stream = _Tty()
    transcript = Transcript()
    transcript.system("restored history")
    cockpit = Cockpit(stream)

    with cockpit:
        cockpit.draw(
            _snapshot(),
            transcript,
            session_description="session",
            branch_line="branch",
            cumulative_line="usage: calls=0",
        )

    assert "restored history" in stream.getvalue()
    assert "conversation ·" not in stream.getvalue()


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
    assert text == " tool errors: 1 (last: run_shell …)"
    assert "permission denied" not in text
    assert "cat protected.txt" not in text


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

    assert "✓ run_shell 141ms" in text
    assert "tool errors: 1 (last: run_shell …)" in text
    assert "✓ run_shell 2395ms" in text
    assert "✗ run_shell 9273ms" not in text
    assert len(transcript.entries) == 3


def test_consecutive_failed_tool_events_collapse_to_one_notice() -> None:
    transcript = Transcript()
    for _ in range(5):
        transcript.observe_event(
            {
                "kind": "tool_event",
                "payload": {"tool": "run_shell", "ok": False},
            }
        )

    rows = _transcript_lines(transcript, 80, 20)
    text = "\n".join(value for _, value in rows)
    assert text == " tool errors: 5 (last: run_shell …)"
    assert len(transcript.entries) == 1
    assert transcript.tool_error_count == 5


def test_tool_error_notice_updates_once_across_success_ticks_and_turns() -> None:
    transcript = Transcript()
    transcript.user("first turn")
    events = (
        ("run_shell", False),
        ("run_shell", True),
        ("read_batch", False),
        ("read_batch", True),
        ("git_op", False),
    )
    for tool, ok in events:
        transcript.observe_event(
            {
                "kind": "tool_event",
                "task_id": f"{tool}-task",
                "payload": {"tool": tool, "ok": ok},
            }
        )

    notices = [entry for entry in transcript.entries if entry.text.startswith("tool errors:")]
    assert [entry.text for entry in notices] == ["tool errors: 3 (last: git_op …)"]

    transcript.user("second turn")
    transcript.observe_event(
        {
            "kind": "tool_event",
            "task_id": "read-task",
            "payload": {"tool": "read_batch", "ok": False},
        }
    )
    notices = [entry for entry in transcript.entries if entry.text.startswith("tool errors:")]
    assert [entry.text for entry in notices] == [
        "tool errors: 3 (last: git_op …)",
        "tool errors: 1 (last: read_batch …)",
    ]


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


def test_adjacent_tool_ticks_have_no_blank_separator() -> None:
    transcript = Transcript()
    for tool in ("run_shell", "read_batch", "git_op"):
        transcript.observe_event(
            {
                "kind": "tool_event",
                "payload": {"tool": tool, "ok": True},
            }
        )

    rows = _transcript_lines(transcript, 80, 20)
    tick_rows = [
        index for index, (_, value) in enumerate(rows) if value.lstrip().startswith("✓ ")
    ]

    assert len(tick_rows) == 3
    assert tick_rows[1] == tick_rows[0] + 1
    assert tick_rows[2] == tick_rows[1] + 1


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
    assert [entry.text for entry in transcript.entries if entry.role == "assistant"] == []
    assert "plan=failed" not in "\n".join(entry.text for entry in transcript.entries)
    rendered = "\n".join(value for _, value in _transcript_lines(transcript, 80, 20))
    assert "ERROR" in rendered
    assert "CAMBIUM" not in rendered
    assert "plan=failed" not in rendered


def test_empty_queued_prompt_has_no_dangling_system_label() -> None:
    transcript = Transcript()

    notice = _queued_prompt_notice(" \n")
    if notice is not None:
        transcript.system(notice)
    assert not any("queued:" in entry.text for entry in transcript.entries)
    assert "queued:" not in "\n".join(
        value for _, value in _transcript_lines(transcript, 80, 20)
    )

    notice = _queued_prompt_notice("follow-up")
    assert notice == "queued: follow-up"
    transcript.system(notice)
    assert "queued: follow-up" in transcript.entries[-1].text
    assert "queued: follow-up" in "\n".join(
        value for _, value in _transcript_lines(transcript, 80, 20)
    )


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
