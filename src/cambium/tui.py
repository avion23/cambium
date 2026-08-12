"""Line-oriented terminal front end for Cambium one-shot sessions."""

from __future__ import annotations

import asyncio
import inspect
import sys
from dataclasses import replace
from pathlib import Path
from typing import Any

_PROMPT = "cambium> "
_EXIT_EOF = 0
_EXIT_INTERRUPT = 130
_EXIT_BROKEN_PIPE = 0
_EXIT_BACKEND_MISSING = 1
_EXIT_RUN_FAILED = 1


def _run(value):
    return asyncio.run(value) if inspect.isawaitable(value) else value


def run_tui(config, *, input_stream=None, output_stream=None, error_stream=None) -> int:
    """Run the line-oriented terminal loop and return an exit code."""
    source = sys.stdin if input_stream is None else input_stream
    out = sys.stdout if output_stream is None else output_stream
    err = sys.stderr if error_stream is None else error_stream

    try:
        from cambium import oneshot, render, stats
    except ImportError as exc:
        err.write(f"cambium tui: {exc}\n")
        err.flush()
        return _EXIT_BACKEND_MISSING

    failed = False
    try:
        while True:
            out.write(_PROMPT)
            out.flush()
            line = source.readline()
            if line == "":
                out.write("\n")
                out.flush()
                return _EXIT_RUN_FAILED if failed else _EXIT_EOF
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

                def _live_sink(record: dict[str, Any]) -> None:
                    events.append(record)
                    out.write(render.render_event_line(record) + "\n")
                    status = render.render_live_status_line(events)
                    if status:
                        out.write(status + "\n")
                    out.flush()

                response = _run(oneshot.run_oneshot(prompt_config, on_event=_live_sink))
                text = render.render_text_result(response)
                if response.exit_code != 0:
                    failed = True
            except Exception as exc:
                failed = True
                err.write(f"cambium: {exc}\n")
                err.flush()
                continue
            out.write(text)
            if not text.endswith("\n"):
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
            except Exception:
                stats_line = ""
            if stats_line:
                out.write(stats_line)
                if not stats_line.endswith("\n"):
                    out.write("\n")
            out.flush()
    except KeyboardInterrupt:
        out.write("\n")
        out.flush()
        return _EXIT_INTERRUPT
    except BrokenPipeError:
        return _EXIT_RUN_FAILED if failed else _EXIT_BROKEN_PIPE
    except Exception as exc:
        err.write(f"cambium: {exc}\n")
        err.flush()
        return _EXIT_RUN_FAILED if failed else _EXIT_BROKEN_PIPE


__all__ = ["run_tui"]
