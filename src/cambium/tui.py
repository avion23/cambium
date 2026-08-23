"""Interactive terminal frontend with a persistent semantic branch and cockpit."""

from __future__ import annotations

import asyncio
import builtins
import os
import signal
import sqlite3
import sys
from collections import deque
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, TextIO

from cambium.render_markdown import render_markdown_if_tty

from .interactive import InteractiveSession, InteractiveSessionError
from .monitor import AnsiDashboard, render_agent_lines
from .observability import ObservabilityState, SessionSnapshot
from .store import StoreError, read_events_file
from .tui_screen import Cockpit, Transcript

try:
    import readline as _readline
except ImportError:  # pragma: no cover - platform dependent
    _readline = None

_PROMPT = "cambium> "
_CONTINUATION_PROMPT = "... "
_BRACKETED_PASTE_ENABLE = "\x1b[?2004h"
_BRACKETED_PASTE_DISABLE = "\x1b[?2004l"
_BRACKETED_PASTE_START = "\x1b[200~"
_BRACKETED_PASTE_END = "\x1b[201~"


@dataclass(slots=True)
class _Cumulative:
    calls: int = 0
    summary_calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cached_tokens: int = 0
    total_tokens: int = 0
    estimated_cost_usd: float = 0.0
    latest_output_tokens_per_s: float = 0.0

    def add(self, snapshot: SessionSnapshot) -> None:
        self.calls += snapshot.calls
        self.summary_calls += snapshot.summary_calls
        self.input_tokens += snapshot.input_tokens
        self.output_tokens += snapshot.output_tokens
        self.cached_tokens += snapshot.cached_tokens
        self.total_tokens += snapshot.total_tokens
        self.estimated_cost_usd += snapshot.estimated_cost_usd
        rates = [
            agent.output_tokens_per_s
            for agent in snapshot.agents
            if agent.output_tokens_per_s is not None
        ]
        self.latest_output_tokens_per_s = sum(rates)

    def line(self) -> str:
        return (
            "usage: "
            f"calls={self.calls} summaries={self.summary_calls} "
            f"tokens={self.total_tokens} "
            f"(in={self.input_tokens} out={self.output_tokens} "
            f"cached={self.cached_tokens}) "
            f"out/s={self.latest_output_tokens_per_s:.1f} "
            f"cost=${self.estimated_cost_usd:.6f}"
        )


_HELP = """Commands:
  /help       show this help
  /status     branch, context, agents, and usage in one view
  /usage      cumulative tokens, throughput, calls, and cost
  /agents     main/sub-agent lifecycle and provider/model rows
  /context    current trunk, raw tail, checkpoint, and epoch
  /session    persistent interactive-session identity and provider lease
  /model      current provider/model lease and branch generation
  /dashboard  explain the visible live cockpit
  /events     recent durable event summaries
  /new        start a fresh semantic branch; old turn artifacts remain
  /clear      clear the visible cockpit transcript
  /exit       leave Cambium

Transcript view:
  v           toggle full command/output details for every tool entry.

During a running turn, !cancel or Ctrl-C cancels that turn and returns to this cockpit.
The last successfully published context checkpoint remains the branch head.

Multiline input:
  bracketed paste keeps pasted newlines in one prompt.
  end a line with \\ to continue; a blank line submits the accumulated text.
  enter <<< on its own line, write the prompt, then enter >>> on its own line.
"""


def _is_tty(stream: Any) -> bool:
    try:
        return bool(getattr(stream, "isatty", lambda: False)())
    except (AttributeError, OSError, ValueError):
        return False


def _write_line(out: TextIO, line: str) -> None:
    if line:
        out.write(line)
        if not line.endswith("\n"):
            out.write("\n")


@contextmanager
def _bracketed_paste_mode(source: TextIO, out: TextIO) -> Iterator[None]:
    """Enable terminal bracketed paste only for interactive input reads."""
    if not (_is_tty(source) and _is_tty(out)):
        yield
        return
    out.write(_BRACKETED_PASTE_ENABLE)
    out.flush()
    try:
        yield
    finally:
        out.write(_BRACKETED_PASTE_DISABLE)
        out.flush()


def _unframe_bracketed_paste(value: str, source: TextIO) -> str:
    """Remove terminal paste framing while retaining newlines in the payload."""
    if not _is_tty(source):
        return value.rstrip("\r\n")
    start = value.find(_BRACKETED_PASTE_START)
    if start < 0:
        return value.rstrip("\r\n")

    prefix = value[:start]
    payload = value[start + len(_BRACKETED_PASTE_START) :]
    while True:
        end = payload.find(_BRACKETED_PASTE_END)
        if end >= 0:
            return prefix + payload[:end]
        line = source.readline()
        if line == "":
            return prefix + payload.rstrip("\r\n")
        payload += line


def _input_line(
    source: TextIO, out: TextIO, prompt: str, *, native: bool
) -> str | None:
    with _bracketed_paste_mode(source, out):
        if native:
            try:
                line = builtins.input(prompt)
            except EOFError:
                return None
        else:
            out.write(prompt)
            out.flush()
            line = source.readline()
            if line == "":
                return None
    return _unframe_bracketed_paste(line, source)


def _read_multiline(
    value: str, read_next: Callable[[], str | None]
) -> str | None:
    """Apply explicit blocks and trailing-backslash line continuation."""
    if value.strip() == "<<<":
        lines: list[str] = []
        while True:
            next_value = read_next()
            if next_value is None or next_value.strip() == ">>>":
                return "\n".join(lines)
            lines.append(next_value)

    lines = []
    while True:
        if value == "":
            return "\n".join(lines)
        if not value.endswith("\\"):
            if lines:
                lines.append(value)
                return "\n".join(lines)
            return value
        lines.append(value[:-1])
        next_value = read_next()
        if next_value is None:
            return "\n".join(lines)
        value = next_value


def _read_prompt(
    source: TextIO, out: TextIO, *, native: bool = False
) -> str | None:
    """Read one prompt, preserving pasted newlines and continued lines."""
    value = _input_line(source, out, _PROMPT, native=native)
    if value is None:
        return None
    return _read_multiline(
        value,
        lambda: _input_line(source, out, _CONTINUATION_PROMPT, native=native),
    )


def _read_cockpit_prompt(
    source: TextIO, cockpit: Cockpit, *, native: bool
) -> str | None:
    """Read input on the cockpit footer while preserving native line editing."""

    def read_one(label: str) -> str | None:
        cockpit.move_to_input(label=label)
        try:
            return _input_line(source, cockpit.stream, "", native=native)
        finally:
            cockpit.hide_cursor()

    value = read_one("›")
    if value is None:
        return None
    return _read_multiline(value, lambda: read_one("…"))


def _history_path(session: InteractiveSession) -> Path:
    return session.root / ".cambium" / "tui_history"


def _load_history(path: Path) -> None:
    if _readline is None or not path.is_file():
        return
    try:
        _readline.read_history_file(path)
        _readline.set_history_length(1000)
    except (OSError, ValueError):
        pass


def _save_history(path: Path) -> None:
    if _readline is None:
        return
    try:
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        _readline.set_history_length(1000)
        _readline.write_history_file(path)
        os.chmod(path, 0o600)
    except (OSError, ValueError):
        pass


def _branch_line(session: InteractiveSession) -> str:
    seed = session.seed
    provider = session.provider or "auto"
    model = session.model or "auto"
    epoch = seed.epoch if seed is not None else 0
    return (
        f"branch: generation={session.branch_generation} turn={session.turn} "
        f"provider={provider} model={model} epoch={epoch}"
    )


def _restore_history(
    session: InteractiveSession,
) -> tuple[_Cumulative, SessionSnapshot]:
    """Rebuild current-branch usage and the latest view from durable turn logs."""
    cumulative = _Cumulative()
    latest = ObservabilityState(recent_limit=16).snapshot()
    for turn_dir in session.active_turn_dirs():
        event_db = turn_dir / ".cambium" / "events.db"
        if not event_db.is_file():
            continue
        state = ObservabilityState(recent_limit=16)
        try:
            for event in read_events_file(event_db):
                state.apply(event)
        except (OSError, ValueError, StoreError, sqlite3.Error):
            continue
        latest = state.snapshot(session_dir=turn_dir)
        cumulative.add(latest)
    return cumulative, latest


def _context_line(snapshot: SessionSnapshot) -> str:
    context = snapshot.context
    prompt = "?" if context.exact_prompt_tokens is None else str(context.exact_prompt_tokens)
    approximate = "≈" if context.approximate else ""
    root = next((agent for agent in snapshot.agents if agent.role == "main"), None)
    provider = root.provider if root is not None and root.provider is not None else "?"
    model = root.model if root is not None and root.model is not None else "?"
    return (
        "context: "
        f"task={context.task_id or '?'} provider={provider} model={model} "
        f"epoch={context.epoch} checkpoint={context.checkpoint_ref or 'none'} "
        f"prompt={prompt}tok trunk={approximate}{context.estimated_trunk_tokens}tok "
        f"segments={context.summary_segments} "
        f"raw={approximate}{context.estimated_raw_tail_tokens}tok "
        f"messages={context.active_context_messages}"
    )


def _response_markdown(render: Any, response: Any) -> str:
    summaries = [
        entry.summary
        for entry in getattr(response, "results", ())
        if getattr(entry, "summary", None)
    ]
    if summaries:
        return "\n\n".join(summaries)
    return render._sanitize_field(render.render_text_result(response))


def _write_result(out: TextIO, render: Any, response: Any) -> None:
    text = render._sanitize_field(render.render_text_result(response))
    _write_line(out, text)
    summaries = [
        entry.summary
        for entry in getattr(response, "results", ())
        if getattr(entry, "summary", None)
    ]
    if summaries:
        rendered = render_markdown_if_tty("\n\n".join(summaries), out)
        _write_line(out, rendered)


async def _run_legacy(
    config: Any,
    *,
    source: TextIO,
    out: TextIO,
    err: TextIO,
    quiet: bool,
) -> int:
    """Preserve deterministic line-oriented behavior for pipes and tests."""
    from cambium import oneshot, render, stats
    from cambium.auth import AuthError
    from cambium.cli import ExitCode
    from cambium.supervisor import SessionAlreadyRunningError

    dashboard_enabled = render.should_color(out) and not quiet
    failed = False
    try:
        while True:
            prompt = _read_prompt(source, out)
            if prompt is None:
                out.write("\n")
                out.flush()
                return ExitCode.FAILURE if failed else ExitCode.SUCCESS
            if prompt in {"/exit", "/quit"}:
                return ExitCode.FAILURE if failed else ExitCode.SUCCESS
            if not prompt.strip():
                continue
            try:
                session_dir = (
                    Path(config.session_root).expanduser().resolve()
                    if config.session_root is not None
                    else oneshot.allocate_session_dir(oneshot.resolve_repo(config.repo))
                )
                prompt_config = replace(config, prompt=prompt, session_root=session_dir)
                state = ObservabilityState()
                dashboard = AnsiDashboard(
                    session_dir,
                    stream=out,
                    enabled=dashboard_enabled,
                )

                def _live_sink(
                    record: dict[str, Any],
                    _state: ObservabilityState = state,
                    _dashboard: AnsiDashboard = dashboard,
                    _session_dir: Path = session_dir,
                ) -> None:
                    _state.apply(record)
                    if _dashboard.enabled:
                        _dashboard.draw(_state.snapshot(session_dir=_session_dir))
                    elif not quiet:
                        _write_line(out, render.render_event_line(record, stream=out))
                        snapshot = _state.snapshot()
                        _write_line(
                            out,
                            "live: "
                            f"out/s={snapshot.output_tokens_per_s:.1f} · "
                            f"active={snapshot.active_agents} · "
                            f"tokens={snapshot.total_tokens}",
                        )
                    out.flush()

                with dashboard:
                    response = await oneshot.run_oneshot(
                        prompt_config,
                        on_event=_live_sink,
                    )
                    if dashboard.enabled:
                        dashboard.draw(state.snapshot(session_dir=session_dir))
                if response.exit_code != 0:
                    failed = True
            except BrokenPipeError:
                return ExitCode.SUCCESS
            except (AuthError, OSError, SessionAlreadyRunningError, ValueError) as exc:
                failed = True
                err.write(f"cambium tui: {exc}\n")
                err.flush()
                continue
            _write_result(out, render, response)
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
            _write_line(out, stats_line)
            out.flush()
    except KeyboardInterrupt:
        out.write("\n")
        out.flush()
        return ExitCode.INTERRUPTED
    except BrokenPipeError:
        return ExitCode.SUCCESS


def _command_output(
    command: str,
    *,
    session: InteractiveSession,
    cumulative: _Cumulative,
    snapshot: SessionSnapshot,
) -> str | None:
    if command == "/help":
        return _HELP
    if command == "/usage":
        return cumulative.line()
    if command == "/agents":
        return "\n".join(render_agent_lines(snapshot))
    if command == "/context":
        return _context_line(snapshot)
    if command in {"/session", "/model"}:
        return session.describe()
    if command == "/dashboard":
        return "The persistent cockpit is already the live dashboard."
    if command in {"/events", "/tail"}:
        if not snapshot.recent_events:
            return "events: none"
        lines = ["events:"]
        for event in snapshot.recent_events:
            task = event.task_id or "-"
            detail = f"  {event.detail}" if event.detail else ""
            lines.append(f"  #{event.seq:<6} {event.kind:<28} {task}{detail}")
        return "\n".join(lines)
    if command == "/cancel":
        return "No turn is active; press Ctrl-C while a turn is running."
    if command == "/status":
        return "\n".join(
            [
                session.describe(),
                _branch_line(session),
                _context_line(snapshot),
                cumulative.line(),
                *render_agent_lines(snapshot),
            ]
        )
    return None


async def _run_interactive(
    config: Any,
    *,
    source: TextIO,
    out: TextIO,
    err: TextIO,
    quiet: bool,
) -> int:
    """Run one persistent cache-aligned branch in one persistent cockpit."""
    from cambium import render
    from cambium.auth import AuthError
    from cambium.cli import ExitCode
    from cambium.supervisor import SessionAlreadyRunningError

    session = InteractiveSession(config)
    cumulative, last_snapshot = _restore_history(session)
    state = ObservabilityState(recent_limit=16)
    transcript = Transcript()
    if session.turn:
        transcript.system(
            "Cambium interactive session\n"
            f"Reopened persistent branch at turn {session.turn}. "
            "Durable turn artifacts and the latest context checkpoint were restored."
        )
    else:
        transcript.system(
            "Cambium interactive session\n"
            "Persistent CAST branch ready. Type /help for commands."
        )
    sequence = 0
    failed = False
    cockpit = Cockpit(out, enabled=not quiet)
    native_input = source is sys.stdin and out is sys.stdout
    history_path = _history_path(session)
    if native_input:
        _load_history(history_path)

    loop = asyncio.get_running_loop()
    pending_prompts: deque[str] = deque()
    input_task: asyncio.Task[str | None] | None = None
    input_eof = False
    input_closing = False

    async def _read_line_source() -> str | None:
        """Read a prompt without stopping live turn events or input steering."""
        return await asyncio.to_thread(
            _read_cockpit_prompt,
            source,
            cockpit,
            native=native_input,
        )

    def _start_input_read() -> None:
        nonlocal input_task
        if input_task is None and not input_eof and not input_closing:
            input_task = loop.create_task(_read_line_source())

    async def _next_prompt() -> str | None:
        """Return queued input first, then wait for the next line source value."""
        nonlocal input_task, input_eof
        if pending_prompts:
            return pending_prompts.popleft()
        _start_input_read()
        if input_task is None:
            return None
        task = input_task
        input_task = None
        prompt = await task
        if prompt is None:
            input_eof = True
        return prompt

    async def _close_input_reader() -> None:
        """Cancel a pending input read when the cockpit is shutting down."""
        nonlocal input_task
        task = input_task
        input_task = None
        if task is None:
            return
        if not task.done():
            task.cancel()
        try:
            await task
        except BaseException:
            pass

    try:
        with cockpit:
            while True:
                cockpit.draw(
                    last_snapshot,
                    transcript,
                    session_description=session.describe(),
                    branch_line=_branch_line(session),
                    cumulative_line=cumulative.line(),
                )
                prompt = await _next_prompt()
                if prompt is None or prompt in {"/exit", "/quit"}:
                    return ExitCode.FAILURE if failed else ExitCode.SUCCESS
                command = prompt.strip()
                if not command:
                    continue
                if command == "v":
                    transcript.toggle_tool_details()
                    continue
                if command == "!cancel":
                    transcript.system(
                        "No turn is active; press !cancel or Ctrl-C while a turn is running."
                    )
                    continue
                if command == "/clear":
                    transcript.clear()
                    transcript.system(
                        "Cockpit transcript cleared; durable session history is unchanged."
                    )
                    continue
                if command == "/new":
                    session.reset()
                    state = ObservabilityState(recent_limit=16)
                    last_snapshot = state.snapshot()
                    cumulative = _Cumulative()
                    sequence = 0
                    transcript.clear()
                    transcript.system(
                        "Started a fresh semantic branch; old artifacts remain durable."
                    )
                    continue
                if command.startswith("/"):
                    output = _command_output(
                        command,
                        session=session,
                        cumulative=cumulative,
                        snapshot=last_snapshot,
                    )
                    if output is None:
                        transcript.error(f"Unknown command: {command}. Type /help.")
                    else:
                        transcript.system(output)
                    continue

                transcript.user(prompt)
                turn = session.prepare_turn(prompt)
                state = ObservabilityState(recent_limit=16)
                sequence = 1
                state.apply(
                    {
                        "seq": sequence,
                        "kind": "interactive_turn_started",
                        "task_id": "interactive-main",
                        "payload": {"turn": turn.number},
                    }
                )
                completed = False
                cancel_requested = False

                def _live_sink(record: dict[str, Any]) -> None:
                    nonlocal sequence
                    session.observe_event(turn, record)  # noqa: B023
                    transcript.observe_event(record)
                    sequence += 1
                    normalized = dict(record)
                    normalized["seq"] = sequence
                    state.apply(normalized)  # noqa: B023
                    cockpit.draw(  # noqa: B023  # noqa: B023
                        state.snapshot(session_dir=turn.session_dir),  # noqa: B023
                        transcript,
                        session_description=session.describe(),
                        branch_line=_branch_line(session),
                        cumulative_line=cumulative.line(),  # noqa: B023
                    )

                _start_input_read()
                turn_task = loop.create_task(
                    session.run_turn(turn, on_event=_live_sink)
                )

                def _request_cancel() -> None:  # noqa: B023
                    nonlocal cancel_requested
                    if not turn_task.done():  # noqa: B023
                        cancel_requested = True
                        turn_task.cancel()  # noqa: B023

                signal_installed = False
                try:
                    try:
                        loop.add_signal_handler(signal.SIGINT, _request_cancel)
                        signal_installed = True
                    except (NotImplementedError, RuntimeError, ValueError):
                        pass
                    try:
                        while True:
                            wait_tasks: set[asyncio.Task[Any]] = {turn_task}
                            if input_task is not None:
                                wait_tasks.add(input_task)
                            done, _ = await asyncio.wait(
                                wait_tasks,
                                return_when=asyncio.FIRST_COMPLETED,
                            )

                            if input_task is not None and input_task in done:
                                task = input_task
                                input_task = None
                                queued_prompt = task.result()
                                if queued_prompt is None:
                                    input_eof = True
                                elif (
                                    queued_prompt.strip() == "!cancel"
                                    and not turn_task.done()
                                ):
                                    _request_cancel()
                                else:
                                    pending_prompts.append(queued_prompt)
                                    if queued_prompt in {"/exit", "/quit"}:
                                        input_closing = True
                                    transcript.system(f"queued: {queued_prompt}")
                                    cockpit.draw(
                                        state.snapshot(session_dir=turn.session_dir),
                                        transcript,
                                        session_description=session.describe(),
                                        branch_line=_branch_line(session),
                                        cumulative_line=cumulative.line(),
                                    )
                                _start_input_read()

                            if turn_task in done:
                                response = await turn_task
                                break
                    except asyncio.CancelledError:
                        if not cancel_requested:
                            raise
                        session.complete_turn(turn, succeeded=False)
                        completed = True
                        snapshot = state.snapshot(session_dir=turn.session_dir)
                        last_snapshot = snapshot
                        cumulative.add(snapshot)
                        transcript.finish_stream()
                        transcript.system(
                            "turn cancelled; the previous successful checkpoint "
                            "remains the branch head."
                        )
                        continue

                    succeeded = response.exit_code == 0
                    session.complete_turn(turn, succeeded=succeeded)
                    completed = True
                    if not succeeded:
                        failed = True
                except BrokenPipeError:
                    if not completed:
                        session.complete_turn(turn, succeeded=False)
                    return ExitCode.SUCCESS
                except (
                    AuthError,
                    InteractiveSessionError,
                    OSError,
                    SessionAlreadyRunningError,
                    ValueError,
                ) as exc:
                    if not completed:
                        session.complete_turn(turn, succeeded=False)
                    failed = True
                    transcript.error(str(exc))
                    err.write(f"cambium tui: {exc}\n")
                    err.flush()
                    continue
                except BaseException:
                    if not completed:
                        session.complete_turn(turn, succeeded=False)
                    raise
                finally:
                    if signal_installed:
                        loop.remove_signal_handler(signal.SIGINT)

                snapshot = state.snapshot(session_dir=turn.session_dir)
                last_snapshot = snapshot
                cumulative.add(snapshot)
                transcript.finish_stream(_response_markdown(render, response))
                cockpit.draw(
                    snapshot,
                    transcript,
                    session_description=session.describe(),
                    branch_line=_branch_line(session),
                    cumulative_line=cumulative.line(),
                )
    except KeyboardInterrupt:
        return ExitCode.INTERRUPTED
    except BrokenPipeError:
        return ExitCode.SUCCESS
    finally:
        await _close_input_reader()
        if native_input:
            _save_history(history_path)


async def run_tui(
    config, *, input_stream=None, output_stream=None, error_stream=None, quiet=False
) -> int:
    """Run Cambium's terminal frontend.

    Real terminals receive the persistent semantic-branch cockpit. Pipes and
    injected streams retain the deterministic line-oriented adapter used by
    scripts and tests.
    """
    source = sys.stdin if input_stream is None else input_stream
    out = sys.stdout if output_stream is None else output_stream
    err = sys.stderr if error_stream is None else error_stream
    if _is_tty(source) and _is_tty(out) and not quiet:
        return await _run_interactive(
            config,
            source=source,
            out=out,
            err=err,
            quiet=quiet,
        )
    return await _run_legacy(
        config,
        source=source,
        out=out,
        err=err,
        quiet=quiet,
    )


__all__ = ["run_tui"]
