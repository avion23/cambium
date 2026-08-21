"""Line-oriented Cambium REPL."""

from __future__ import annotations

import os
import sys
from dataclasses import replace
from pathlib import Path
from typing import Any, TextIO

from . import oneshot, render, stats
from .auth import AuthError
from .cli import ExitCode
from .oneshot import OneShotConfig
from .supervisor import SessionAlreadyRunningError

try:
    import readline
except ImportError:  # non-interactive fallback: no history
    readline = None


def _config_for_prompt(config: OneShotConfig, prompt: str) -> OneShotConfig:
    return replace(config, prompt=prompt)


def _history_path(config: OneShotConfig) -> Path:
    """Return the repository-local REPL history file path."""
    root = (
        Path(config.session_root).expanduser().resolve()
        if config.session_root is not None
        else oneshot.default_session_root(config.repo)
    )
    return root / ".cambium" / "repl_history"


def _load_history(path: Path) -> None:
    """Load readline history from ``path``, tolerating a missing or stale file."""
    if not path.is_file():
        return
    try:
        readline.read_history_file(path)
    except (OSError, ValueError):
        pass


def _save_history(path: Path) -> None:
    """Persist readline history under a private file, creating the directory."""
    try:
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        readline.write_history_file(path)
        os.chmod(path, 0o600)
    except OSError:
        pass


async def run_repl(
    config: OneShotConfig,
    *,
    input_stream: TextIO | None = None,
    output_stream: TextIO | None = None,
    error_stream: TextIO | None = None,
) -> int:
    """Run prompts with one fresh immutable config per prompt."""
    input_stream = sys.stdin if input_stream is None else input_stream
    output_stream = sys.stdout if output_stream is None else output_stream
    error_stream = sys.stderr if error_stream is None else error_stream

    history_path = None
    if readline is not None and getattr(input_stream, "isatty", lambda: False)():
        history_path = _history_path(config)
        _load_history(history_path)

    try:
        failed = False
        usage_events: list[dict[str, Any]] = []
        for line in input_stream:
            prompt = line.rstrip("\r\n")
            if prompt == "/exit":
                break
            if not prompt.strip():
                continue
            if history_path is not None:
                readline.add_history(prompt)
            try:
                prompt_config = _config_for_prompt(config, prompt)
                events: list[dict[str, Any]] = []

                def _live_sink(
                    record: dict[str, Any],
                    _events: list[dict[str, Any]] = events,
                ) -> None:
                    _events.append(record)
                    if record.get("kind") == "usage_event":
                        usage_events.append(record)
                    output_stream.write(render.render_event_line(record) + "\n")
                    status = render.render_live_status_line(_events)
                    if status:
                        output_stream.write(status + "\n")
                    output_stream.flush()

                result = await oneshot.run_oneshot(prompt_config, on_event=_live_sink)
                rendered = render.render_text_result(result)
                usage = stats.usage_stats_from_events(usage_events)
                if usage is not None:
                    # Each one-shot prompt restarts its provider turn counter,
                    # so a cross-prompt fold cannot name one meaningful last turn.
                    usage = replace(usage, turns=None)
                usage_line = render.render_usage_stats_line(usage)
                if usage_line and usage is not None and usage.provider:
                    usage_line += f" · provider={usage.provider}"
                if result.exit_code != 0:
                    failed = True
            except BrokenPipeError:
                return ExitCode.SUCCESS
            except (AuthError, OSError, SessionAlreadyRunningError, ValueError) as exc:
                failed = True
                error_stream.write(f"cambium repl: {exc}\n")
                error_stream.flush()
                continue
            try:
                output_stream.write(rendered)
                if not rendered.endswith("\n"):
                    output_stream.write("\n")
                if usage_line:
                    output_stream.write(usage_line + "\n")
                output_stream.flush()
            except BrokenPipeError:
                return ExitCode.SUCCESS
    except KeyboardInterrupt:
        return ExitCode.INTERRUPTED
    finally:
        if history_path is not None:
            _save_history(history_path)
    return ExitCode.FAILURE if failed else ExitCode.SUCCESS


__all__ = ["run_repl"]
