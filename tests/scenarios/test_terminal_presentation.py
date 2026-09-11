"""Shared terminal presentation and capability scenarios."""

from __future__ import annotations

import io

from cambium.monitor import AnsiDashboard, render_dashboard
from cambium.observability import snapshot_from_events
from cambium.terminal import (
    clip_terminal_text,
    pad_terminal_text,
    sanitize_terminal_text,
    supports_cursor_controls,
    terminal_display_width,
)


class _Tty(io.StringIO):
    def isatty(self) -> bool:
        return True


def test_cursor_controls_require_a_capable_terminal_but_ignore_no_color(
    monkeypatch,
) -> None:
    stream = _Tty()
    monkeypatch.setenv("TERM", "xterm-256color")
    monkeypatch.setenv("NO_COLOR", "1")

    assert supports_cursor_controls(stream)

    monkeypatch.setenv("TERM", "dumb")
    assert not supports_cursor_controls(stream)
    assert not supports_cursor_controls(io.StringIO())


def test_dashboard_appends_frames_without_terminal_controls(tmp_path) -> None:
    stream = io.StringIO()
    dashboard = AnsiDashboard(tmp_path, stream=stream)
    snapshot = snapshot_from_events([])

    with dashboard:
        dashboard.draw(snapshot)
    first = stream.getvalue()
    dashboard.draw(snapshot)

    assert dashboard.enabled
    assert first.endswith("\n")
    assert "\x1b" not in first
    assert stream.getvalue() == first * 2


def test_plain_text_clipping_and_padding_use_terminal_cells() -> None:
    text = "界e\u0301界"

    assert terminal_display_width(text) == 5
    assert clip_terminal_text(text, 4) == "界e\u0301…"
    padded = pad_terminal_text(text, 8)
    assert terminal_display_width(padded) == 8
    assert padded.startswith(text)


def test_sanitizer_exposes_bidi_controls_and_normalizes_line_boundaries() -> None:
    text = "left\u202eright\u202c\u2028next\u0085last"

    assert sanitize_terminal_text(text) == "left\\u202Eright\\u202C\nnext\nlast"
    assert sanitize_terminal_text(text, single_line=True) == ("left\\u202Eright\\u202C next last")


def test_monitor_frame_keeps_cell_width_with_wide_and_combining_text(tmp_path) -> None:
    task = "根e\u0301"
    snapshot = snapshot_from_events(
        (
            {
                "seq": 1,
                "kind": "spawned",
                "task_id": task,
                "generation": 1,
                "monotonic_ms": 100,
                "payload": {},
            },
            {
                "seq": 2,
                "kind": "usage_event",
                "task_id": task,
                "generation": 1,
                "monotonic_ms": 200,
                "payload": {
                    "provider": "提供者",
                    "model": "模型e\u0301",
                    "turn": 1,
                    "usage": {
                        "input_tokens": 10,
                        "output_tokens": 2,
                        "total_tokens": 12,
                    },
                },
            },
            {
                "seq": 3,
                "kind": "状态e\u0301",
                "task_id": task,
                "generation": 1,
                "monotonic_ms": 300,
                "payload": {"detail": "完成"},
            },
        )
    )

    lines = render_dashboard(snapshot, session_dir=tmp_path, width=80, height=24)

    assert len(lines) == 24
    assert all(terminal_display_width(line) == 80 for line in lines)
    assert "提供者/模型e\u0301" in "\n".join(lines)
