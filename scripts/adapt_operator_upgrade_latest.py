#!/usr/bin/env python3
"""Adapt the operator-upgrade generator to the latest upstream UI tree."""

from __future__ import annotations

import argparse
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BOOTSTRAP = ROOT / "scripts" / "apply_operator_upgrade.py"


def replace_once(text: str, old: str, new: str, label: str) -> str:
    count = text.count(old)
    if count != 1:
        raise RuntimeError(f"{label}: expected one match, found {count}")
    return text.replace(old, new, 1)


def patch(path: str, old: str, new: str, label: str) -> None:
    target = ROOT / path
    text = target.read_text(encoding="utf-8")
    target.write_text(replace_once(text, old, new, label), encoding="utf-8")


def prepare() -> None:
    text = BOOTSTRAP.read_text(encoding="utf-8")
    old = '''replace_once(
    "src/cambium/oauth.py",
    "        self._client_id = client_id\\n"
    "        self._issuer = validate_issuer(issuer)\\n",
    "        self._client_id = resolve_codex_client_id(client_id)\\n"
    "        self._issuer = validate_issuer(issuer)\\n",
    "device flow pinned client id",
)
'''
    new = '''replace_once(
    "src/cambium/oauth.py",
    "        self._provider = _validate_provider_id(provider)\\n"
    "        self._client_id = client_id\\n"
    "        self._issuer = validate_issuer(issuer)\\n",
    "        self._provider = _validate_provider_id(provider)\\n"
    "        self._client_id = resolve_codex_client_id(client_id)\\n"
    "        self._issuer = validate_issuer(issuer)\\n",
    "device flow pinned client id",
)
'''
    text = replace_once(text, old, new, "scope DeviceFlow client id")
    text = replace_once(
        text,
        'remove("tests/scenarios/test_render_stream.py")\n',
        "",
        "preserve upstream stream-render tests",
    )
    BOOTSTRAP.write_text(text, encoding="utf-8")


def finalize_tui() -> None:
    patch(
        "src/cambium/tui.py",
        "from typing import Any\n\nfrom .monitor import AnsiDashboard\n",
        "from typing import Any\n\nfrom cambium.render_markdown import render_markdown_if_tty\n\n"
        "from .monitor import AnsiDashboard\n",
        "preserve upstream markdown renderer",
    )
    patch(
        "src/cambium/tui.py",
        '''            out.write(text)
            if not text.endswith("\\n"):
                out.write("\\n")
            try:
''',
        '''            out.write(text)
            if not text.endswith("\\n"):
                out.write("\\n")
            summaries = [
                entry.summary
                for entry in getattr(response, "results", ())
                if getattr(entry, "summary", None)
            ]
            if summaries:
                rendered_summaries = render_markdown_if_tty(
                    "\\n\\n".join(summaries), out
                )
                out.write(rendered_summaries)
                if not rendered_summaries.endswith("\\n"):
                    out.write("\\n")
            try:
''',
        "preserve upstream TUI markdown output",
    )


def finalize_lint() -> None:
    patch(
        "src/cambium/monitor.py",
        '    lines.append(_inside("Ctrl-C/q: close monitor  •  runtime continues unless its owner cancels it", width))\n',
        '    lines.append(\n'
        '        _inside(\n'
        '            "Ctrl-C/q: close monitor  •  "\n'
        '            "runtime continues unless its owner cancels it",\n'
        '            width,\n'
        '        )\n'
        '    )\n',
        "wrap monitor footer",
    )
    patch(
        "src/cambium/repl.py",
        '''                def _live_sink(
                    record: dict[str, Any],
                    _events: list[dict[str, Any]] = events,
                ) -> None:
''',
        '''                def _live_sink(
                    record: dict[str, Any],
                    _events: list[dict[str, Any]] = events,
                    _stream_tty: bool = stream_tty,
                    _session_label: str = session_label,
                ) -> None:
''',
        "bind REPL closure state",
    )
    patch(
        "src/cambium/repl.py",
        '''                    if not stream_tty:
''',
        '''                    if not _stream_tty:
''',
        "use bound REPL tty state",
    )
    patch(
        "src/cambium/repl.py",
        '''                            _events, session_label=session_label
''',
        '''                            _events, session_label=_session_label
''',
        "use bound REPL session label",
    )
    patch(
        "src/cambium/worker.py",
        '''    try:
        trunk, raw_tail = partition_summary_trunk(messages)
    except SummaryTrunkError:
        trunk = list(messages[:2])
        raw_tail = list(messages[2:])
''',
        '''    try:
        trunk, _ = partition_summary_trunk(messages)
    except SummaryTrunkError:
        trunk = list(messages[:2])
''',
        "remove unused prompt raw tail",
    )
    patch(
        "tests/scenarios/test_optimize_exception_hygiene.py",
        '''    candidate_path.write_text('{"id": "approved", "candidate": true, "review_status": "approved", "redacted": true}\\n', encoding="utf-8")
''',
        '''    candidate_path.write_text(
        '{"id": "approved", "candidate": true, '
        '"review_status": "approved", "redacted": true}\\n',
        encoding="utf-8",
    )
''',
        "wrap approved candidate fixture",
    )


def finalize_stream_tests() -> None:
    patch(
        "tests/scenarios/test_render_stream.py",
        '''import json
import os
import shutil

from cambium.render import (
''',
        '''import asyncio
import io
import json
import os
import shutil

from cambium import oneshot, repl, tui
from cambium.render import (
''',
        "move stream-test stdlib and cambium imports",
    )
    patch(
        "tests/scenarios/test_render_stream.py",
        '''from cambium.render import (
    render_active_workers,
    render_event_line,
    render_status_bar,
    render_tokens_per_s,
)
''',
        '''from cambium.render import (
    render_active_workers,
    render_event_line,
    render_status_bar,
    render_tokens_per_s,
)
from cambium.supervisor import PlanResult, TaskResult
''',
        "move stream-test supervisor imports",
    )
    patch(
        "tests/scenarios/test_render_stream.py",
        '''import asyncio
import io

from cambium import oneshot, repl, tui
from cambium.supervisor import PlanResult, TaskResult


''',
        "",
        "remove late stream-test imports",
    )
    patch(
        "tests/scenarios/test_render_stream.py",
        '''    on_event({"kind": "tool_event", "payload": {"tool": "run_shell", "cmd": "df -h", "ok": True, "duration_ms": 5, "turn": 1}})
    on_event({"kind": "heartbeat", "payload": {"status": "working", "tool": None, "turn": 1}})
''',
        '''    on_event(
        {
            "kind": "tool_event",
            "payload": {
                "tool": "run_shell",
                "cmd": "df -h",
                "ok": True,
                "duration_ms": 5,
                "turn": 1,
            },
        }
    )
    on_event(
        {
            "kind": "heartbeat",
            "payload": {"status": "working", "tool": None, "turn": 1},
        }
    )
''',
        "wrap scripted stream events",
    )
    start = '''def test_tui_tty_draws_bar_then_suppresses_after_terminal_events(monkeypatch, tmp_path):
    monkeypatch.setattr(oneshot, "run_oneshot", _scripted_run)
    out = _TtyStream()
    assert asyncio.run(tui.run_tui(
        oneshot.OneShotConfig(repo=tmp_path),
        input_stream=io.StringIO("hi\\n"),
        output_stream=out,
        error_stream=io.StringIO(),
    )) == 0
    text = out.getvalue()
    assert "run_shell df -h OK 5ms" in text
    # bar drawn after tool_event, heartbeat (keeps elapsed ticking), and
    # result (final totals); NOT refreshed again after session_ended.
    # 4 events -> exactly 3 draws proves session_ended refreshed nothing.
    assert _bar_draws(text) == 3
'''
    replacement = '''def test_tui_tty_draws_event_sourced_dashboard(monkeypatch, tmp_path):
    monkeypatch.setattr(oneshot, "run_oneshot", _scripted_run)
    out = _TtyStream()
    assert asyncio.run(
        tui.run_tui(
            oneshot.OneShotConfig(repo=tmp_path),
            input_stream=io.StringIO("hi\\n"),
            output_stream=out,
            error_stream=io.StringIO(),
        )
    ) == 0
    text = out.getvalue()
    assert "\\x1b[?1049h" in text
    assert "\\x1b[?1049l" in text
    assert "Cambium" in text
    assert "run_shell" in text
'''
    patch(
        "tests/scenarios/test_render_stream.py",
        start,
        replacement,
        "replace TUI status-bar test with dashboard contract",
    )


def finalize() -> None:
    finalize_tui()
    finalize_lint()
    finalize_stream_tests()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("phase", choices=("prepare", "finalize"))
    args = parser.parse_args()
    if args.phase == "prepare":
        prepare()
    else:
        finalize()


if __name__ == "__main__":
    main()
