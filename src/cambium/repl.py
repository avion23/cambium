"""Line-oriented Cambium REPL."""

from __future__ import annotations

import asyncio
import codecs
import os
import signal
import sys
from collections.abc import Iterator
from dataclasses import replace
from pathlib import Path
from typing import Any, Protocol, TextIO, cast

from . import oneshot, render, stats
from .auth import AuthError
from .cli import ExitCode
from .oneshot import OneShotConfig
from .render_markdown import render_markdown_if_tty
from .supervisor import SessionAlreadyRunningError


class _Readline(Protocol):
    def read_history_file(self, filename: str | os.PathLike[str]) -> None: ...

    def write_history_file(self, filename: str | os.PathLike[str]) -> None: ...

    def add_history(self, line: str) -> None: ...


readline: _Readline | None
try:
    import readline as _readline
except ImportError:  # non-interactive fallback: no history
    readline = None
else:
    readline = cast(_Readline, _readline)


_BAR_TERMINAL_KINDS = frozenset(
    {"result", "session_ended", "exit", "worker_failed", "reuse_ready"}
)

_PROMPT = "cambium> "


def _read_stdin_byte() -> bytes:
    """Read one raw byte from stdin (patch point for tests)."""
    return os.read(0, 1)


_ORIGINAL_READ_BYTE = _read_stdin_byte


class _TtyLineReader:
    """Byte-at-a-time tty line reader with prompt echo.

    Enter submits the accumulated partial line; DEL/NUL-backspace edits it and
    repaints the prompt; unhandled CSI/arrow escape sequences are swallowed
    without submitting; EOF returns ``None``.  Typed characters are not echoed
    here (the tty driver echoes in canonical mode); only edits and event-driven
    repaints redraw ``clear-line + prompt + partial``.
    """

    def __init__(self, output_stream: TextIO, *, echo: bool, read_fn=None) -> None:
        self._out = output_stream
        self._echo = echo
        self._read_fn = read_fn or _read_stdin_byte
        self._decoder = codecs.getincrementaldecoder("utf-8")("replace")
        self.partial = ""

    def write_prompt(self) -> None:
        if not self._echo:
            return
        self._out.write("\r\033[K" + _PROMPT + self.partial)
        self._out.flush()

    def _skip_csi(self) -> None:
        data = self._read_fn()
        if not data or data != b"[":
            return
        while True:
            data = self._read_fn()
            if not data or 0x40 <= data[0] <= 0x7E:
                return

    def read_line(self) -> str | None:
        self.partial = ""
        self._decoder.reset()
        self.write_prompt()
        while True:
            data = self._read_fn()
            if not data:
                return None
            for ch in self._decoder.decode(data):
                if ch in ("\r", "\n"):
                    return self.partial
                if ch in ("\x7f", "\b"):
                    if self.partial:
                        self.partial = self.partial[:-1]
                        self.write_prompt()
                elif ch == "\x1b":
                    self._skip_csi()
                else:
                    self.partial += ch

    def __iter__(self) -> Iterator[str]:
        return self

    def __next__(self) -> str:
        line = self.read_line()
        if line is None:
            raise StopIteration
        return line


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
        cast(_Readline, readline).read_history_file(path)
    except (OSError, ValueError):
        pass


def _save_history(path: Path) -> None:
    """Persist readline history under a private file, creating the directory."""
    try:
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        cast(_Readline, readline).write_history_file(path)
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
    input_stream = cast(TextIO, sys.stdin if input_stream is None else input_stream)
    output_stream = cast(TextIO, sys.stdout if output_stream is None else output_stream)
    error_stream = cast(TextIO, sys.stderr if error_stream is None else error_stream)

    history_path = None
    input_tty = bool(getattr(input_stream, "isatty", lambda: False)())
    if readline is not None and input_tty:
        history_path = _history_path(config)
        _load_history(history_path)

    loop = asyncio.get_running_loop()
    turn_task: asyncio.Task[Any] | None = None
    sigint_fired = False

    def _on_sigint() -> None:
        nonlocal sigint_fired
        sigint_fired = True
        if turn_task is not None and not turn_task.done():
            turn_task.cancel()

    sigint_installed = False
    if input_tty:
        try:
            loop.add_signal_handler(signal.SIGINT, _on_sigint)
            sigint_installed = True
        except (NotImplementedError, RuntimeError):
            pass

    reader: _TtyLineReader | None = None
    if input_tty:
        # Byte source precedence: a patched module hook (tests) wins over the
        # injected stream, which otherwise serves bytes for real tty sessions.
        if _read_stdin_byte is not _ORIGINAL_READ_BYTE:
            byte_source = _read_stdin_byte
        else:
            def byte_source() -> bytes:
                data = input_stream.read(1)
                if isinstance(data, str):
                    return data.encode("utf-8")
                return data or b""

        reader = _TtyLineReader(
            output_stream,
            echo=bool(getattr(output_stream, "isatty", lambda: False)()),
            read_fn=byte_source,
        )

    line_source: Any = reader if reader is not None else input_stream

    try:
        failed = False
        usage_events: list[dict[str, Any]] = []
        for line in line_source:
            prompt = line.rstrip("\r\n")
            if prompt == "/exit":
                break
            if not prompt.strip():
                continue
            sigint_fired = False
            if history_path is not None:
                cast(_Readline, readline).add_history(prompt)
            try:
                prompt_config = _config_for_prompt(config, prompt)
                events: list[dict[str, Any]] = []
                stream_tty = bool(getattr(output_stream, "isatty", lambda: False)())
                bar_live = stream_tty
                session_label = (
                    Path(config.session_root).expanduser().resolve().name
                    if config.session_root is not None
                    else oneshot.default_session_root(config.repo).name
                )

                def _live_sink(
                    record: dict[str, Any],
                    _events: list[dict[str, Any]] = events,
                    _stream_tty: bool = stream_tty,
                    _session_label: str = session_label,
                ) -> None:
                    nonlocal bar_live
                    _events.append(record)
                    if record.get("kind") == "usage_event":
                        usage_events.append(record)
                    if sigint_fired:  # noqa: B023
                        return
                    if not _stream_tty:
                        output_stream.write(
                            render.render_event_line(record, stream=output_stream)
                            + "\n"
                        )
                        status = render.render_live_status_line(_events)
                        if status:
                            output_stream.write(status + "\n")
                        output_stream.flush()
                        return
                    line = render.render_event_line(record, stream=output_stream)
                    if line:
                        output_stream.write(line + "\n")
                    if bar_live:
                        output_stream.write("\r\033[K")
                        bar = render.render_status_bar(
                            _events, session_label=_session_label
                        )
                        if bar:
                            output_stream.write(bar + "\n")
                        if record.get("kind") in _BAR_TERMINAL_KINDS:
                            bar_live = False
                    if reader is not None:
                        reader.write_prompt()
                    output_stream.flush()

                if input_tty:
                    turn_task = loop.create_task(
                        oneshot.run_oneshot(prompt_config, on_event=_live_sink)
                    )
                    try:
                        result = await turn_task
                    except asyncio.CancelledError:
                        if not sigint_fired:
                            raise
                        bar = render.render_status_bar(
                            events, session_label=session_label
                        )
                        if stream_tty:
                            output_stream.write("\r\033[K")
                            if bar:
                                output_stream.write(bar + "\n")
                            output_stream.write("interrupted\n")
                            output_stream.flush()
                        continue
                    finally:
                        turn_task = None
                else:
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
                summaries = [
                    entry.summary
                    for entry in getattr(result, "results", ())
                    if getattr(entry, "summary", None)
                ]
                if summaries:
                    rendered_summaries = render_markdown_if_tty(
                        "\n\n".join(summaries), output_stream
                    )
                    output_stream.write(rendered_summaries)
                    if not rendered_summaries.endswith("\n"):
                        output_stream.write("\n")
                if usage_line:
                    output_stream.write(usage_line + "\n")
                output_stream.flush()
            except BrokenPipeError:
                return ExitCode.SUCCESS
    except KeyboardInterrupt:
        return ExitCode.INTERRUPTED
    finally:
        if sigint_installed:
            loop.remove_signal_handler(signal.SIGINT)
        if history_path is not None:
            _save_history(history_path)
    return ExitCode.FAILURE if failed else ExitCode.SUCCESS


__all__ = ["run_repl"]
