"""Line-oriented terminal front end for Cambium one-shot sessions."""

from __future__ import annotations

import sqlite3
import sys
from dataclasses import replace
from pathlib import Path
from typing import Any

from cambium.render_markdown import render_markdown_if_tty

_PROMPT = "cambium> "
_ANSI_CLEAR = "\033[2J\033[H"


def _is_tty(stream: Any) -> bool:
    try:
        return bool(getattr(stream, "isatty", lambda: False)())
    except (AttributeError, OSError):
        return False


def _write_line(out: Any, line: str) -> None:
    if line:
        out.write(line)
        if not line.endswith("\n"):
            out.write("\n")


def _dashboard_lines(session_dir: Path, events: list[dict[str, Any]], render, stats) -> list[str]:
    lines = [f"session: {session_dir}"]
    elapsed = render.render_elapsed(events)
    if elapsed:
        lines[0] += f" · {elapsed}"
    status = render.render_subagent_status(events)
    if status:
        lines.extend(status.splitlines())
    live = render.render_live_status_line(events)
    if live:
        lines.append(live)
    usage = render.render_usage_stats_line(stats.usage_stats_from_events(events))
    if usage:
        lines.append(usage)
    if events:
        lines.append(render.render_event_line(events[-1]))
    return lines


async def run_tui(
    config, *, input_stream=None, output_stream=None, error_stream=None, quiet=False
) -> int:
    """Run the line-oriented terminal loop and return an exit code."""
    source = sys.stdin if input_stream is None else input_stream
    out = sys.stdout if output_stream is None else output_stream
    err = sys.stderr if error_stream is None else error_stream
    dashboard = _is_tty(out) and not quiet

    try:
        from cambium import oneshot, render, stats
    except ImportError as exc:
        err.write(f"cambium tui: {exc}\n")
        err.flush()
        from cambium.cli import ExitCode

        return ExitCode.FAILURE

    from cambium.auth import AuthError
    from cambium.cli import ExitCode
    from cambium.supervisor import SessionAlreadyRunningError

    failed = False
    try:
        while True:
            out.write(_PROMPT)
            out.flush()
            line = source.readline()
            if line == "":
                out.write("\n")
                out.flush()
                return ExitCode.FAILURE if failed else ExitCode.SUCCESS
            prompt = line.rstrip("\r\n")
            if not prompt.strip():
                continue
            try:
                session_dir = (
                    Path(config.session_root).expanduser().resolve()
                    if config.session_root is not None
                    else oneshot.allocate_session_dir(oneshot.resolve_repo(config.repo))
                )
                prompt_config = replace(config, prompt=prompt, session_root=session_dir)
                events: list[dict[str, Any]] = []

                def _live_sink(
                    record: dict[str, Any],
                    _events: list[dict[str, Any]] = events,
                    _session_dir: Path = session_dir,
                ) -> None:
                    _events.append(record)
                    if dashboard:
                        out.write(_ANSI_CLEAR)
                        for line in _dashboard_lines(_session_dir, _events, render, stats):
                            _write_line(out, line)
                    elif not quiet:
                        _write_line(out, render.render_event_line(record))
                        status = render.render_live_status_line(_events)
                        _write_line(out, status)
                    out.flush()

                response = await oneshot.run_oneshot(prompt_config, on_event=_live_sink)
                text = render.render_text_result(response)
                if response.exit_code != 0:
                    failed = True
            except BrokenPipeError:
                return ExitCode.SUCCESS
            except (AuthError, OSError, SessionAlreadyRunningError, ValueError) as exc:
                failed = True
                err.write(f"cambium tui: {exc}\n")
                err.flush()
                continue
            out.write(text)
            if not text.endswith("\n"):
                out.write("\n")
            summaries = [
                entry.summary
                for entry in getattr(response, "results", ())
                if getattr(entry, "summary", None)
            ]
            if summaries:
                rendered_summaries = render_markdown_if_tty(
                    "\n\n".join(summaries), out
                )
                out.write(rendered_summaries)
                if not rendered_summaries.endswith("\n"):
                    out.write("\n")
            try:
                worktree = (
                    str(prompt_config.worktree_path)
                    if prompt_config.worktree_path is not None
                    else str(session_dir / "wt")
                )
                stats_line = render.render_usage_stats_line(
                    stats.session_usage_stats(session_dir), worktree=worktree
                )
            except (OSError, ValueError, sqlite3.Error) as exc:
                err.write(f"cambium tui: usage stats unavailable: {exc}\n")
                err.flush()
                stats_line = ""
            if stats_line:
                out.write(stats_line)
                if not stats_line.endswith("\n"):
                    out.write("\n")
            out.flush()
    except KeyboardInterrupt:
        out.write("\n")
        out.flush()
        return ExitCode.INTERRUPTED
    except BrokenPipeError:
        return ExitCode.SUCCESS


__all__ = ["run_tui"]
