"""Pure presentation tests for the persistent terminal cockpit."""

import io
import os
from types import SimpleNamespace
from typing import Any, cast

import pytest
from _helpers_g2 import _FlushCountingTty, _Tty  # type: ignore[reportMissingImports]

import cambium.tui_screen as tui_screen
from cambium.observability import ObservabilityState, RecentEvent, snapshot_from_events
from cambium.terminal import terminal_display_width
from cambium.tui import _command_output, _queued_prompt_notice, _safe_live_draw
from cambium.tui_screen import (
    ActivityState,
    Cockpit,
    Transcript,
    _bounded_markdown_lines,
    _compact_rail_rows,
    _display_width,
    _live_window_lines,
    _rail_rows,
    _side_sections,
    _status_rows,
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
        recent_events=(SimpleNamespace(kind="usage_event", detail="tokens=12345"),),
    )


class _Utf8Tty(_Tty):
    def write(self, value: str) -> int:
        value.encode("utf-8")
        return super().write(value)


def _traffic_snapshot():
    snapshot = _snapshot()
    snapshot.active_agents = 2
    snapshot.queued_agents = 1
    snapshot.succeeded_agents = 4
    snapshot.failed_agents = 1
    snapshot.calls = 9
    snapshot.summary_calls = 2
    snapshot.input_tokens = 100_000
    snapshot.output_tokens = 20_000
    snapshot.cached_tokens = 75_000
    snapshot.total_tokens = 120_000
    snapshot.output_tokens_per_s = 12.5
    snapshot.estimated_cost_usd = 0.123456
    return snapshot


def test_context_fork_lineage_is_explicit() -> None:
    events = (
        {
            "seq": 1,
            "kind": "context_fork",
            "task_id": "root",
            "payload": {
                "parent_task_id": "root",
                "child_task_id": "exact-child",
                "compatible": True,
            },
        },
        {
            "seq": 2,
            "kind": "context_fork",
            "task_id": "root",
            "payload": {
                "parent_task_id": "root",
                "child_task_id": "semantic-child",
                "compatible": False,
                "semantic_reuse": True,
            },
        },
        {
            "seq": 3,
            "kind": "context_fork_skipped",
            "task_id": "root",
            "payload": {"parent_task_id": "root", "child_task_id": "fresh-child"},
        },
        {
            "seq": 4,
            "kind": "context_fork",
            "task_id": "root",
            "payload": {"parent_task_id": "root", "child_task_id": "unknown-child"},
        },
    )

    snapshot = snapshot_from_events(events)
    lineages = {agent.task_id: agent.lineage for agent in snapshot.agents}

    assert lineages == {
        "root": "",
        "exact-child": "exact",
        "semantic-child": "semantic",
        "fresh-child": "fresh",
        "unknown-child": "",
    }


def test_full_operator_rail_rows_have_stable_golden_strings() -> None:
    snapshot = _snapshot()
    snapshot.agents = (
        SimpleNamespace(
            task_id="root",
            parent_task_id=None,
            state="active",
            lineage="exact",
            epoch=3,
        ),
        SimpleNamespace(
            task_id="child",
            parent_task_id="root",
            state="failed",
            lineage="fresh",
            epoch=3,
        ),
    )
    snapshot.context.summary_segments = 2
    snapshot.context.estimated_trunk_tokens = 2_000
    snapshot.context.summary_trunk_bytes = 8_192
    snapshot.context.estimated_raw_tail_tokens = 1_000
    snapshot.context.raw_tail_bytes = 4_096
    snapshot.context.checkpoint_ref = "root/epoch-0003.json"
    snapshot.recent_events = (
        RecentEvent(seq=8, kind="context_epoch_advanced", task_id="root", detail=""),
        RecentEvent(seq=9, kind="compaction_failed", task_id="root", detail="provider"),
    )

    rows = _rail_rows(snapshot, 32, 32)

    text = "\n".join(value for _, value in rows)
    for expected in ("root E3", "child E3", "trunk ≈2k tok", "raw ≈1k tok",
                     "context_epoch_advanced e4", "compaction_failed · provider"):
        assert expected in text
    assert all(value.strip() and _display_width(value) <= 32 for _, value in rows)


def test_compact_operator_rail_rows_keep_glyphs_and_epoch() -> None:
    snapshot = _snapshot()
    snapshot.context.epoch = 3
    snapshot.agents = (
        SimpleNamespace(
            task_id="root",
            parent_task_id=None,
            state="active",
            lineage="",
            epoch=3,
        ),
        SimpleNamespace(
            task_id="child",
            parent_task_id="root",
            state="active",
            lineage="semantic",
            epoch=3,
        ),
    )

    assert [text for _, text in _compact_rail_rows(snapshot)] == [
        "└●=?E3",
        "├●=~E3",
    ]


def test_operator_rail_change_uses_a_fresh_frame(monkeypatch) -> None:
    monkeypatch.setattr(
        tui_screen.shutil,
        "get_terminal_size",
        lambda _fallback: os.terminal_size((110, 24)),
    )
    stream = _Tty()
    snapshot = _snapshot()
    cockpit = Cockpit(stream)
    with cockpit:
        cockpit.draw(
            snapshot,
            Transcript(),
            session_description="session",
            branch_line="branch",
            cumulative_line="usage: calls=0",
        )
        first = stream.getvalue()
        snapshot.agents[0].lineage = "exact"
        cockpit.draw(
            snapshot,
            Transcript(),
            session_description="session",
            branch_line="branch",
            cumulative_line="usage: calls=0",
        )

    delta = stream.getvalue()[len(first) :]
    assert "┌ Cambium · conversation" in delta
    assert "\x1b[s" not in delta


def test_activity_rail_ticks_redraw_in_place_at_duration_width_change(monkeypatch) -> None:
    monkeypatch.setattr(
        tui_screen.shutil,
        "get_terminal_size",
        lambda _fallback: os.terminal_size((110, 24)),
    )
    stream = _Tty()
    cockpit = Cockpit(stream)
    snapshot = _snapshot()
    with cockpit:
        cockpit.draw(
            snapshot,
            Transcript(),
            session_description="session",
            branch_line="branch",
            cumulative_line="usage: calls=0",
            activity_line="◌ thinking 9s · read config",
            turn_active=True,
        )
        first = stream.getvalue()
        cockpit.draw_activity("◌ thinking 10s · read config")
        cockpit.draw(
            snapshot,
            Transcript(),
            session_description="session",
            branch_line="branch",
            cumulative_line="usage: calls=0",
            activity_line="◌ thinking 10s · read config",
            turn_active=True,
        )
        delta = stream.getvalue()[len(first) :]

    assert "┌ Cambium · conversation" not in delta
    assert "read_batch · 10s" in delta
    assert "starting 10s" in delta


def test_replaying_events_after_snapshot_is_idempotent() -> None:
    events = (
        {
            "seq": 1,
            "kind": "spawned",
            "task_id": "root",
            "payload": {"epoch": 2},
        },
        {
            "seq": 2,
            "kind": "context_fork",
            "task_id": "root",
            "payload": {
                "parent_task_id": "root",
                "child_task_id": "child",
                "compatible": False,
                "semantic_reuse": True,
                "epoch": 2,
            },
        },
    )
    state = ObservabilityState()
    state.extend(events)
    before = state.snapshot()
    state.extend(events)

    assert state.snapshot() == before


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
    assert "YOU ▸ Inspect the provider router" in text
    assert "codex/gpt-5.6" in text
    assert "agents=1 active" not in text
    assert "cost=" not in text
    assert "checkpoint=" not in text
    assert "│ input › " in text
    assert text.count("├") == 1
    assert lines[-1].startswith("└")


@pytest.mark.parametrize("width", [79, 80, 99, 100])
def test_cockpit_frame_is_cell_exact_at_rail_breakpoints_with_wide_fields(width: int) -> None:
    snapshot = _snapshot()
    snapshot.session_status = "running\nwide"
    snapshot.agents[0].task_id = "任务🙂-" + "界" * 40
    snapshot.agents[0].provider = "提供者🙂"
    snapshot.agents[0].model = "模型界"
    transcript = Transcript()
    transcript.user("检查界面🙂")
    transcript.assistant("完成 wide output")

    lines = render_cockpit(
        snapshot,
        transcript,
        session_description="session=会话🙂",
        branch_line="branch=分支界",
        cumulative_line="usage: calls=3 tokens=12345",
        width=width,
        height=12,
        input_label="输入🙂",
    )

    assert len(lines) == 12
    assert all(terminal_display_width(line) == width for line in lines)
    assert all("\n" not in line for line in lines)
    assert all(line.endswith(("┐", "│", "┤", "┘")) for line in lines)
    assert ("┬" in lines[0]) is (width >= 80)
    assert ("OPERATOR RAIL" in lines[0]) is (width >= 100)
    assert "提供者" in "\n".join(lines)
    assert "模型界" in "\n".join(lines)
    if width >= 100:
        assert "任务" in "\n".join(lines)


def test_narrow_frame_row_does_not_use_more_cells_than_the_frame() -> None:
    for width in (8, 9, 79):
        line = tui_screen._split_frame_row("界🙂", width, tui_screen._rail_width(width))
        assert terminal_display_width(line) == max(8, width)


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
    monkeypatch.setenv("COLUMNS", "80")
    monkeypatch.setenv("LINES", "24")
    summary = (
        "Detected prior interactive session; resuming durable state: "
        "turns=1 last_epoch=7 last_checkpoint=interactive-main/epoch-7-"
        + "c"
        * 64
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
    transcript.system("operator note")

    rows = _transcript_lines(transcript, 60, 100)
    values = [value.rstrip() for _, value in rows]
    assert values[-1].strip()
    assert values == [
        "YOU ▸ prompt",
        "CAMBIUM ▸ first",
        "    second",
        "SYSTEM ▸ operator note",
    ]

    transcript.observe_event(
        {
            "kind": "tool_event",
            "payload": {"tool": "run_shell", "ok": True, "duration_ms": 83},
        }
    )
    values = [value.rstrip() for _, value in _transcript_lines(transcript, 60, 100)]
    assert values[-2] == ""
    assert values[-1].endswith("83ms ✓ run_shell")


def test_transcript_labels_are_inline_and_separators_only_split_speakers() -> None:
    transcript = Transcript()
    transcript.user("first prompt")
    transcript.user("second prompt")
    transcript.assistant("first answer")
    transcript.assistant("second answer")
    transcript.system("operator note")

    values = [_visible(value).rstrip() for _, value in _transcript_lines(transcript, 80, 100)]
    assert values[0].startswith("YOU ▸ first prompt")
    assert values[1].startswith("YOU ▸ second prompt")
    assert values[2].startswith("CAMBIUM ▸ first answer")
    assert values[3].startswith("CAMBIUM ▸ second answer")
    assert values[4].startswith("SYSTEM ▸ operator note")


def test_status_strip_has_one_detail_row_and_hides_context_internals_from_spinner() -> None:
    transcript = Transcript()
    snapshot = _snapshot()
    rows = _status_rows(
        snapshot,
        transcript,
        session_description="session=/private/root turn=2 branch=3 checkpoint=secret",
        branch_line="branch: generation=4 turn=2 epoch=9",
        cumulative_line="usage: tokens=1100000",
        width=80,
        activity_line="⠇ WAITING · thinking… 12s",
    )

    assert len(rows) == 3
    assert all("\n" not in row for row in rows)
    assert rows[1].strip() == "⠇ thinking 12s · codex/gpt-5.6 · t2 · 1.1m tok"
    assert all(
        value not in rows[1] for value in ("branch", "generation", "epoch", "session", "checkpoint")
    )


def test_detail_row_reports_traffic_and_context() -> None:
    rows = _status_rows(
        _traffic_snapshot(),
        Transcript(),
        session_description="session=/tmp/run",
        branch_line="branch: turn=2",
        cumulative_line=(
            "usage: calls=9 summaries=2 tokens=120000 (in=100000 out=20000 cached=75000) "
            "out/s=12.5 cost=$0.123456"
        ),
        width=220,
        activity_line="⠦ WAITING · thinking… 2s",
    )

    detail = rows[-1]
    assert "agents active=2 queued=1 ok=4 failed=1" in detail
    assert "cached=75k (75%)" in detail
    assert "summaries=2" in detail
    assert "context epoch=4 trunk≈9ktok segments=3" in detail


def test_status_palette_is_gated_without_changing_visible_text() -> None:
    snapshot = _traffic_snapshot()
    snapshot.active_agents = 0
    snapshot.queued_agents = 1
    snapshot.succeeded_agents = 0
    snapshot.failed_agents = 0
    snapshot.input_tokens = 749_000
    snapshot.output_tokens = 44_700
    snapshot.cached_tokens = 677_600
    snapshot.total_tokens = 793_600
    snapshot.calls = 9
    snapshot.summary_calls = 2
    snapshot.estimated_cost_usd = 0.123456
    arguments: dict[str, Any] = dict(
        session_description="session",
        branch_line="branch: turn=4",
        cumulative_line=(
            "usage: calls=9 summaries=2 tokens=793600 "
            "(in=749000 out=44700 cached=677600) out/s=12.5 cost=$0.123456"
        ),
        width=220,
        activity_line="⠋ idle",
    )

    colored = render_primary(snapshot, Transcript(), color=True, **arguments)
    plain = render_primary(snapshot, Transcript(), color=False, **arguments)

    assert "\x1b[" in colored[-2] + colored[-1]
    assert "\x1b[" not in plain[-2] + plain[-1]
    assert _visible(colored[-2:][0]) == plain[-2]
    assert _visible(colored[-2:][1]) == plain[-1]
    assert f"{tui_screen._DIM}idle{tui_screen._RESET}" in colored[-2]
    assert f"{tui_screen._CYAN}codex/gpt-5.6{tui_screen._RESET}" in colored[-2]
    assert f"{tui_screen._DIM}793.6k tok{tui_screen._RESET}" in colored[-2]
    assert f"{tui_screen._MD_BOLD}active=0{tui_screen._RESET}" in colored[-1]
    assert f"{tui_screen._YELLOW}queued=1{tui_screen._RESET}" in colored[-1]
    assert f"{tui_screen._GREEN}ok=0{tui_screen._RESET}" in colored[-1]
    assert f"{tui_screen._RED}failed=0{tui_screen._RESET}" in colored[-1]
    assert f"{tui_screen._GREEN}90%{tui_screen._RESET}" in colored[-1]
    context_style = f"{tui_screen._DIM}context epoch=4 trunk≈9ktok segments=3{tui_screen._RESET}"
    assert context_style in colored[-1]


@pytest.mark.parametrize(
    ("activity_line", "phase", "style"),
    [
        ("⠋ idle", "idle", "_DIM"),
        ("▸ streaming 1s", "streaming", "_GREEN"),
        ("⠋ queued", "queued", "_YELLOW"),
        ("✗ ERROR", "error", "_RED"),
    ],
)
def test_status_phase_palette_follows_activity_state(
    activity_line: str, phase: str, style: str
) -> None:
    rendered = _status_rows(
        _snapshot(),
        Transcript(),
        session_description="session",
        branch_line="branch: turn=2",
        cumulative_line="usage: calls=0 tokens=12345",
        width=120,
        color=True,
        activity_line=activity_line,
    )[1]

    assert f"{getattr(tui_screen, style)}{phase}{tui_screen._RESET}" in rendered


def test_detail_command_shows_optional_row_on_next_frame(monkeypatch) -> None:
    monkeypatch.setattr(
        tui_screen.shutil,
        "get_terminal_size",
        lambda _fallback: os.terminal_size((220, 24)),
    )
    stream = _Tty()
    cockpit = Cockpit(stream)
    snapshot = _traffic_snapshot()
    cumulative_line = (
        "usage: calls=9 summaries=2 tokens=120000 (in=100000 out=20000 cached=75000) "
        "out/s=12.5 cost=$0.123456"
    )

    with cockpit:
        cockpit.draw(
            snapshot,
            Transcript(),
            session_description="session",
            branch_line="branch",
            cumulative_line=cumulative_line,
        )
        first = stream.getvalue()
        assert "summaries=2" not in first
        assert (
            _command_output(
                "/detail",
                session=cast(Any, SimpleNamespace()),
                cumulative=cast(Any, SimpleNamespace()),
                snapshot=cast(Any, snapshot),
                cockpit=cockpit,
            )
            == "detail: shown"
        )
        cockpit.draw(
            snapshot,
            Transcript(),
            session_description="session",
            branch_line="branch",
            cumulative_line=cumulative_line,
        )

    assert "summaries=2" in stream.getvalue()[len(first) :]


def test_tool_row_counts_failures_and_uses_compact_last_duration() -> None:
    transcript = Transcript()
    for ok, duration in ((True, 118215), (False, 1000), (False, 2395)):
        transcript.observe_event(
            {
                "kind": "tool_event",
                "payload": {"tool": "run_shell", "ok": ok, "duration_ms": duration},
            }
        )

    rows = _status_rows(
        _snapshot(),
        transcript,
        session_description="session=/tmp/run",
        branch_line="branch: turn=2",
        cumulative_line="usage: tokens=1100000",
        width=80,
        activity_line="⠇ WAITING · thinking… 12s",
    )

    assert rows[0].strip() == "✓ 3 tools · last run_shell 2s"
    assert rows[1].strip().endswith("· err2")


@pytest.mark.parametrize(
    ("duration_ms", "count", "expected"),
    [
        (83, 2, "   83ms ✓ run_shell ×2"),
        (1000, 3, "     1s ✓ run_shell ×3"),
        (1500, 2, "     1s ✓ run_shell ×2"),
        (24411, 2, "    24s ✓ run_shell ×2"),
        (0, 2, "✓ run_shell ×2"),
        (None, 1, "✓ run_shell"),
    ],
)
def test_tool_rows_put_positive_duration_first(
    duration_ms: int | None, count: int, expected: str
) -> None:
    entry = tui_screen.TranscriptEntry(
        role="tool",
        text="run_shell: ok",
        tool_name="run_shell",
        tool_ok=True,
        duration_ms=duration_ms,
    )

    line = tui_screen._tool_line(entry, count=count, last_duration_ms=duration_ms)
    assert line == expected
    assert (
        tui_screen._tool_compact_lines(entry, 80, count=count, last_duration_ms=duration_ms)[0][1]
        == "  " + expected
    )


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
    assert "out/s= 12.5" in activity.render(now=11.0)  # fixed-width field

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
    assert activity.render(now=13.0) == "◌ thinking 3s · read config"

    activity.observe_event(
        {
            "kind": "heartbeat",
            "payload": {"phase": "streaming", "tail": "answer fragment"},
        },
        now=12.0,
    )
    assert activity.render(now=14.0) == "▸ streaming 4s · answer fragment"

    activity.observe_event(
        {"kind": "heartbeat", "payload": {"phase": "waiting", "tail": "stale tail"}},
        now=15.0,
    )
    assert activity.render(now=16.0) == "… waiting 6s"

    transcript = Transcript()
    transcript.observe_event(
        {"kind": "heartbeat", "payload": {"phase": "thinking", "tail": "private tail"}}
    )
    assert transcript.entries == ()


def test_suspended_activity_stays_live_and_has_distinct_status() -> None:
    activity = ActivityState()
    activity.start(now=0.0)
    activity.observe_event(
        {"kind": "result", "payload": {"status": "suspended"}},
        now=1.0,
    )

    assert activity.active
    assert activity.state == "SUSPENDED"
    assert "SUSPENDED" in activity.render(now=1.0)
    assert activity.status_line() != "✓ DONE"


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
    assert "⠋ thinking" in "\n".join(lines)
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
        "".join(value.split())
        for role, value in _transcript_lines(transcript, 46, 100)
        if role == "assistant"
    )
    assert long_word in conversation
    assert all(_display_width(line) <= 48 for line in lines)
    assert lines[-1] == "└" + "─" * 46 + "┘"


def test_wide_text_wraps_by_cells_without_losing_continuation_spaces() -> None:
    for wrapper in (tui_screen._wrap_plain_markdown, _wrap_markdown):
        lines = wrapper("界界 a b", 5)
        assert lines == ["界界", "a b"]
        assert all(_display_width(line) <= 5 for line in lines)


def test_wide_side_columns_measure_cells_before_selecting_layout() -> None:
    agent = SimpleNamespace(
        task_id="task",
        role="main",
        state="活跃",
        provider="提供者",
        model="模型🙂",
        total_tokens=1,
        output_tokens_per_s=1.0,
        tool="工具界",
    )
    agent_rows = tui_screen._agent_rows((agent,), 24)
    assert "活跃" in agent_rows[0][1]
    assert all(terminal_display_width(text) <= 24 for _, text in agent_rows)

    recent_rows = tui_screen._recent_rows(SimpleNamespace(kind="事件", detail="界界界"), 10)
    assert [text for _, text in recent_rows] == [" 事件", "   界界界"]

    quota_snapshot = SimpleNamespace(
        quota_windows=(
            SimpleNamespace(
                provider="提供",
                name="模型🙂",
                allowance_tokens=1000,
                remaining_tokens=500,
            ),
        )
    )
    quota_rows = tui_screen._quota_rows(quota_snapshot, 24)
    assert [text for _, text in quota_rows] == [" 提供/模型🙂", "   500/1000 tokens"]


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
    tool_row, strip, detail = lines[-3:]

    assert all(len(line) <= 120 for line in lines[-3:])
    assert "· 0 tools" in tool_row
    assert strip.count("codex/gpt-5.6") == 1
    assert "checkpoint=" not in strip
    assert "generation=" not in strip
    assert "12.3k tok" in strip
    assert "cached=" in detail


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
    assert len(lines[-3:]) == 3
    assert lines[-3].startswith(" · 0 tools")
    assert lines[-2].startswith(" ⠋ thinking")
    assert "12.3k tok" in lines[-2]
    assert lines[-1].startswith(" agents active=")


def test_cockpit_appends_to_primary_buffer_without_repainting() -> None:
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


def test_cockpit_commits_native_input_without_leaving_fixed_frame() -> None:
    stream = _Tty()
    cockpit = Cockpit(stream)
    with cockpit:
        cockpit.draw(
            _snapshot(),
            Transcript(),
            session_description="session",
            branch_line="branch",
            cumulative_line="usage: calls=0",
        )
        cockpit.move_to_input(native=True)
        before_hide = stream.getvalue()
        cockpit.hide_cursor(commit=True)
        after_hide = stream.getvalue()
        assert cockpit._fixed_frame

    assert "\n\n" not in after_hide[len(before_hide) :]


def test_cockpit_paints_mid_turn_tool_tick_while_input_is_pending() -> None:
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
        assert "118s ✓ run_shell" in live_output
        assert live_output.endswith("› ")
        assert cockpit._input_active


def test_cockpit_throttles_active_turn_frames(monkeypatch) -> None:
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
            transcript.observe_event({"kind": "tool_event", "payload": {"tool": tool, "ok": True}})
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

    assert any(_visible(row).startswith(" ⠋ idle") for row in cockpit._last_status_rows)
    assert stream.getvalue().count("┌ Cambium · conversation") == 1


def test_result_commits_into_conversation_rows_after_final_hold(monkeypatch) -> None:
    """A finished assistant response must appear in the conversation rows.

    Regression: the final-hold snapshot was captured while the assistant text
    was still un-committed stream state.  The force draw then stayed on the
    live-only path, so the committed response stayed invisible in the
    transcript area (only the bottom live rows showed it).
    """

    now = [10.0]
    monkeypatch.setattr(tui_screen.time, "monotonic", lambda: now[0])
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
            activity_line="⠋ WAITING · thinking… 0s",
            turn_active=True,
        )
        cockpit.move_to_input()
        transcript.observe_event(
            {
                "kind": "result",
                "task_id": "interactive-main",
                "payload": {"status": "succeeded", "summary": "found one issue"},
            }
        )
        now[0] = 10.2
        cockpit.draw(
            _snapshot(),
            transcript,
            session_description="session",
            branch_line="branch",
            cumulative_line="usage: calls=1",
            activity_line="✓ DONE",
            turn_active=True,
        )
        assert cockpit._final_hold_conversation_rows == cockpit._last_conversation_rows
        assert transcript.entries == ()

        transcript.finish_stream("found one issue")
        before = stream.getvalue()
        cockpit.draw(
            _snapshot(),
            transcript,
            session_description="session",
            branch_line="branch",
            cumulative_line="usage: calls=1",
            activity_line="✓ DONE",
            turn_active=False,
            force=True,
        )
        # The conversation frame is repainted on the force draw; the committed
        # assistant entry must be visible in the transcript rows.
        output = stream.getvalue()[len(before) :]
        assert "┌ Cambium · conversation" in output
        assert "CAMBIUM ▸ found one issue" in output
        assert any(
            role == "assistant" and "found one issue" in text
            for role, text in cockpit._last_conversation_rows
        )
        assert transcript.entries[-1].text == "found one issue"


def test_cockpit_forces_completed_frame_while_input_read_is_pending() -> None:
    stream = _FlushCountingTty()
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
        transcript.observe_event({"kind": "assistant_delta", "payload": {"delta": "partial"}})
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
        # While the input line is still active the completion is coalesced,
        # never painted.
        cockpit.draw(
            final_snapshot,
            transcript,
            session_description="session",
            branch_line="branch",
            cumulative_line="usage: calls=1 tokens=20000",
        )
        mid = stream.getvalue()
        assert "completed response" not in mid

        # Release the input FIRST, then flush: the pending completion frame
        # must be delivered as a fresh conversation frame.
        cockpit.hide_cursor()
        cockpit.flush()

        after = stream.getvalue()
        assert "completed response" in after
        assert after.count("┌ Cambium · conversation") == 2
        assert "conversation · done" in after
        assert any(
            role == "assistant" and "completed response" in text
            for role, text in cockpit._last_conversation_rows
        )
        assert "20k tok" in after
        assert after != before
        assert stream.flush_count > flushes_before_completion


def test_cockpit_updates_fixed_status_pane_in_place() -> None:
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
        cockpit.draw_activity("⠋ running run_shell 1.0s")
        second = stream.getvalue()
        cockpit.draw_activity("⠙ running run_shell 2.0s")

    delta = stream.getvalue()[len(first) :]
    second_delta = stream.getvalue()[len(second) :]
    assert "\x1b[s" in delta
    assert "\x1b[3A" in delta
    assert "last run_shell 1s" in delta
    assert "last run_shell 2s" in second_delta
    assert "┌ Cambium · conversation" not in delta


def test_status_counter_updates_repaint_in_place_without_stale_width(monkeypatch) -> None:
    monkeypatch.setattr(
        tui_screen.shutil,
        "get_terminal_size",
        lambda _fallback: os.terminal_size((110, 24)),
    )
    stream = _Tty()
    snapshot = _traffic_snapshot()
    snapshot.queued_agents = 123456
    cockpit = Cockpit(stream)
    cockpit.toggle_detail()

    with cockpit:
        cockpit.draw(
            snapshot,
            Transcript(),
            session_description="session",
            branch_line="branch",
            cumulative_line="usage: calls=9",
        )
        first = stream.getvalue()
        snapshot.queued_agents = 1
        cockpit.draw(
            snapshot,
            Transcript(),
            session_description="session",
            branch_line="branch",
            cumulative_line="usage: calls=9",
        )

    delta = stream.getvalue()[len(first) :]
    assert "queued=1" in delta
    assert "queued=123456" not in delta
    assert tui_screen._CLEAR_LINE in delta
    assert "┌ Cambium · conversation" not in delta
    assert cockpit._last_rendered_width == 110


def test_live_window_updates_two_fixed_rows_without_reflow() -> None:
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
            activity_line="⠋ WAITING · thinking… 0s",
            turn_active=True,
        )
        first = stream.getvalue()
        cockpit.move_to_input()
        for second, tail in ((1, "first"), (2, "second")):
            transcript.observe_event(
                {
                    "kind": "heartbeat",
                    "task_id": "interactive-main",
                    "monotonic_ms": second * 1_000,
                    "payload": {"phase": "streaming", "tail": tail, "turn": 2},
                }
            )
            cockpit.draw(
                _snapshot(),
                transcript,
                session_description="session",
                branch_line="branch",
                cumulative_line="usage: calls=0",
                activity_line=f"▸ streaming {second}s · {tail}",
                turn_active=True,
            )

        delta = stream.getvalue()[len(first) :]

    assert len(_live_window_lines(transcript, 80, activity_line="▸ streaming 2s")) == 2
    assert "second" in delta
    assert "┌ Cambium · conversation" not in delta
    assert "\n" not in delta


def test_completed_response_moves_from_live_window_into_conversation() -> None:
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
            activity_line="⠋ WAITING · thinking… 0s",
            turn_active=True,
        )
        first = stream.getvalue()
        cockpit.move_to_input()
        transcript.observe_event(
            {
                "kind": "heartbeat",
                "task_id": "interactive-main",
                "monotonic_ms": 1_000,
                "payload": {"phase": "streaming", "tail": "partial", "turn": 2},
            }
        )
        cockpit.draw(
            _snapshot(),
            transcript,
            session_description="session",
            branch_line="branch",
            cumulative_line="usage: calls=0",
            activity_line="▸ streaming 1s · partial",
            turn_active=True,
        )
        middle = stream.getvalue()
        transcript.finish_stream("final\nline")
        cockpit.draw(
            _snapshot(),
            transcript,
            session_description="session",
            branch_line="branch",
            cumulative_line="usage: calls=1",
            activity_line="✓ DONE",
            turn_active=True,
            force=True,
        )
        delta = stream.getvalue()[len(middle) :]

    assert "final line" in delta
    assert "┌ Cambium · conversation" in delta
    assert "CAMBIUM ▸ final" in delta
    assert "\n" in delta
    assert len(cockpit._last_status_rows[1:3]) == 2
    assert all(_display_width(row) <= 118 for row in cockpit._last_status_rows[1:3])
    assert stream.getvalue().count("┌ Cambium · conversation") == 2
    assert first != middle


def test_live_rows_sanitize_multiline_ansi_and_wide_output() -> None:
    transcript = Transcript()
    transcript.observe_event(
        {
            "kind": "heartbeat",
            "payload": {"phase": "streaming", "tail": "line one\n\x1b[31m" + "界" * 200},
        }
    )

    rows = _live_window_lines(transcript, 32)
    assert len(rows) == 2
    assert all("\n" not in row and "\x1b" not in row for row in rows)
    assert all(_display_width(row) <= 32 for row in rows)
    assert "line one" in rows[1]


def test_current_tool_counters_reset_at_the_next_turn() -> None:
    transcript = Transcript()
    transcript.observe_event({"kind": "tool_event", "payload": {"tool": "run_shell", "ok": False}})
    transcript.observe_event({"kind": "tool_event", "payload": {"tool": "read_batch", "ok": True}})

    assert transcript.tool_count == 2
    assert transcript.current_tool_count == 2
    assert transcript.current_tool_error_count == 1

    transcript.user("next turn")
    assert transcript.tool_count == 2
    assert transcript.current_tool_count == 0
    assert transcript.current_tool_error_count == 0


def test_local_waiting_activity_renders_honest_starting_state() -> None:
    rows = _live_window_lines(
        Transcript(),
        80,
        activity_line="⠇ WAITING · thinking… 12s",
    )

    # No runtime event has arrived: the window may only claim the local
    # clock. It must not fabricate a provider call, turn, or call counter.
    joined = " ".join(rows)
    assert all("waiting" not in row.casefold() for row in rows)
    assert "starting" in rows[0]
    assert "12s" in rows[0]
    assert "provider call" not in joined
    assert "turn=" not in joined
    assert "call=" not in joined
    assert "no runtime events yet" in rows[1]


def test_small_terminal_live_events_rewrite_two_rows_without_newlines(monkeypatch) -> None:
    monkeypatch.setattr(
        tui_screen.shutil,
        "get_terminal_size",
        lambda _fallback: os.terminal_size((70, 11)),
    )
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
            activity_line="⠋ WAITING · thinking… 0s",
            turn_active=True,
        )
        first = stream.getvalue()
        cockpit.move_to_input()
        # At least three repeated live updates: each rewrites the same rows
        # in place with stable cursor coordinates and never a newline.
        for index in range(1, 5):
            tail = f"chunk-{index}"
            transcript.observe_event(
                {
                    "kind": "tool_output_delta",
                    "task_id": "interactive-main",
                    "payload": {
                        "tool": "run_shell",
                        "stream": "stdout",
                        "delta": tail,
                    },
                }
            )
            cockpit.draw(
                _snapshot(),
                transcript,
                session_description="session",
                branch_line="branch",
                cumulative_line="usage: calls=0",
                activity_line="▸ streaming",
                turn_active=True,
            )
            delta = stream.getvalue()[len(first) :]
            first = stream.getvalue()
            assert tail in delta
            assert "┌ Cambium" not in delta
            assert "\n" not in delta
            assert "\x1b[s\x1b[2A" in delta
            assert "\x1b[1A" in delta


def test_live_tool_tail_and_duration_clear_at_provider_boundary() -> None:
    transcript = Transcript()
    transcript.observe_event(
        {
            "kind": "heartbeat",
            "payload": {
                "phase": "streaming",
                "tool": "run_shell",
                "tail": "stale command output",
            },
        }
    )
    transcript.observe_event(
        {
            "kind": "tool_event",
            "payload": {"tool": "run_shell", "ok": True, "duration_ms": 1250},
        }
    )
    transcript.observe_event(
        {"kind": "heartbeat", "payload": {"phase": "waiting", "status": "working"}}
    )

    rows = _live_window_lines(transcript, 100)
    assert all(
        value not in " ".join(rows) for value in ("stale command output", "run_shell", "1250ms")
    )
    assert "provider call" in " ".join(rows)


def test_live_provider_tail_clears_when_heartbeat_phase_changes() -> None:
    transcript = Transcript()
    transcript.observe_event(
        {"kind": "heartbeat", "payload": {"phase": "streaming", "tail": "old provider tail"}}
    )
    transcript.observe_event(
        {"kind": "heartbeat", "payload": {"phase": "waiting", "status": "working"}}
    )

    rows = _live_window_lines(transcript, 100)
    assert "old provider tail" not in " ".join(rows)
    assert "provider call" in " ".join(rows)


def test_heartbeat_tool_none_clears_the_tool_in_every_view() -> None:
    # The worker clears progress.tool before the completion event, so a
    # heartbeat carrying tool=None must clear the rail tool AND the live
    # transcript view instead of keeping the previous tool stuck.
    events = [
        {
            "seq": 1,
            "kind": "heartbeat",
            "task_id": "root",
            "payload": {"tool": "run_shell", "phase": "streaming", "tail": "cmd out"},
        },
        {
            "seq": 2,
            "kind": "heartbeat",
            "task_id": "root",
            "payload": {"tool": None, "phase": "waiting", "status": "working"},
        },
    ]
    snapshot = snapshot_from_events(events)
    assert snapshot.agents[0].tool is None

    transcript = Transcript()
    transcript.observe_event(events[0])
    assert "run_shell" in " ".join(_live_window_lines(transcript, 100))
    transcript.observe_event(events[1])
    rows = _live_window_lines(transcript, 100)
    assert all("run_shell" not in row for row in rows)

    # The rail detail rows read the same snapshot tool field.
    detail = [value for _, value in _rail_rows(snapshot, 32, 32)]
    assert all("run_shell" not in value for value in detail)


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

    # The active stream belongs to the newest tool only.
    assert transcript.streaming_role == "tool"
    assert transcript.streaming_text == "NEW-B"

    transcript.finish_stream("done")
    texts = [entry.text for entry in transcript.entries]
    assert any("OLD-A" in text for text in texts)
    assert any("NEW-B" in text for text in texts)
    assert all(not ("OLD-A" in text and "NEW-B" in text) for text in texts)
    assert any(text == "done" for text in texts)


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
    assert any("codex/gpt-5.6" in line for line in lines)


def test_live_cockpit_keeps_short_terminal_fallback(monkeypatch) -> None:
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
        transcript.observe_event({"kind": "assistant_delta", "payload": {"delta": "x"}})

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
    assert "CAMBIUM ▸" in "\n".join(lines)


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
    assert "2s ✓ run_shell ×4" in text
    assert "✓ 4 tools · last run_shell 2s" in text


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
    assert "tool errors:" not in text
    assert "permission denied" not in text
    assert "cat protected.txt" not in text
    assert transcript.tool_error_count == 1


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

    assert "141ms ✓ run_shell" in text
    assert "err1" in text
    assert "2s ✓ run_shell" in text
    assert "✗ run_shell 9s" not in text
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
    assert "Waiting for a prompt" in text
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

    assert "141ms ✓ git_op" in text
    assert "9s ✓ run_shell" in text
    assert "2s ✓ git_op" in text
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
    tick_rows = [index for index, (_, value) in enumerate(rows) if value.lstrip().startswith("✓ ")]

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
        "plan=tasks:1 plan_status={failed} plan_failures={task-timeout:'max_restarts (0): wall'}"
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
    assert "queued:" not in "\n".join(value for _, value in _transcript_lines(transcript, 80, 20))

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
        line.strip().startswith("cost") and line.rstrip().endswith("$0")
        for line in text.splitlines()
    )

    usage_columns = []
    for label, value in (("calls", "19"), ("tokens", "104k"), ("out/s", "12.4"), ("cost", "$0")):
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
    assert "thinking… 0s" in thinking
    assert activity.tick(now=10.1).startswith("⠙ ")

    activity.observe_event(
        {"kind": "assistant_delta", "payload": {"delta": "I will inspect this."}},
        now=11.0,
    )
    assert "responding… 2s" in activity.render(now=12.0)

    activity.observe_event(
        {
            "kind": "tool_start",
            "payload": {"tool": "run_shell", "tool_call_id": "call-1"},
        },
        now=13.0,
    )
    running = activity.render(now=14.5)
    assert "running run_shell 1s" in running
    assert "turn 4s" in running
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
    assert any("last run_shell 1s" in line for line in frame)

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
    assert "thinking… 5s" in activity.render(now=15.0)

    activity.stop()
    assert activity.render(now=16.0) == ""


def test_activity_redraw_is_silent_for_non_tty() -> None:
    stream = io.StringIO()
    cockpit = Cockpit(stream)

    cockpit.draw_activity("⠋ thinking… 1.0s")

    assert stream.getvalue() == ""


def test_activity_resize_repaints_frame_and_restores_input_prompt(monkeypatch) -> None:
    sizes = iter(
        (
            os.terminal_size((110, 24)),
            os.terminal_size((90, 24)),
            os.terminal_size((70, 24)),
        )
    )
    monkeypatch.setattr(tui_screen.shutil, "get_terminal_size", lambda _fallback: next(sizes))
    stream = _Tty()
    cockpit = Cockpit(stream)
    with cockpit:
        cockpit.draw(
            _snapshot(),
            Transcript(),
            session_description="session",
            branch_line="branch",
            cumulative_line="usage: calls=0",
        )
        cockpit.move_to_input()
        for activity_line in ("⠋ thinking… 1.0s", "⠙ thinking… 2.0s"):
            before = stream.getvalue()
            cockpit.draw_activity(activity_line)
            assert stream.getvalue()[len(before) :].endswith("› ")

    assert cockpit.size.columns == 70


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

    enabled = _safe_live_draw(
        fail,
        error=error,
        disable=disable,
    )

    assert enabled is False
    assert "live rendering disabled (RuntimeError)" in error.getvalue()


def test_consecutive_identical_queued_system_notices_are_collapsed() -> None:
    transcript = Transcript()
    transcript.system("queued: follow-up")
    transcript.system("queued: follow-up")
    transcript.system("queued: another")
    transcript.system("queued: follow-up")

    assert [entry.text for entry in transcript.entries] == [
        "queued: follow-up",
        "queued: another",
        "queued: follow-up",
    ]


def test_live_resize_repaints_before_rewriting_live_rows(monkeypatch) -> None:
    sizes = iter((os.terminal_size((110, 24)), os.terminal_size((90, 24))))
    monkeypatch.setattr(tui_screen.shutil, "get_terminal_size", lambda _fallback: next(sizes))
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
            activity_line="⠋ WAITING",
            turn_active=True,
        )
        cockpit.move_to_input()
        transcript.observe_event(
            {
                "kind": "heartbeat",
                "payload": {"phase": "streaming", "tail": "resized"},
            }
        )
        before = stream.getvalue()
        cockpit.draw(
            _snapshot(),
            transcript,
            session_description="session",
            branch_line="branch",
            cumulative_line="usage: calls=0",
            activity_line="▸ streaming",
            turn_active=True,
        )

    delta = stream.getvalue()[len(before) :]
    assert cockpit._last_rendered_width == 90
    assert "┌ Cambium · conversation" in delta
    assert f"{tui_screen._CLEAR_LINE}{' ' * 110}" in delta
