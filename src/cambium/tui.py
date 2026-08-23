"""Interactive terminal frontend with a persistent semantic branch and dashboard."""

from __future__ import annotations

import asyncio
import builtins
import os
import shutil
import signal
import sqlite3
import sys
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, TextIO

from cambium.render_markdown import render_markdown_if_tty

from .interactive import InteractiveSession, InteractiveSessionError
from .monitor import AnsiDashboard, render_agent_lines, render_dashboard
from .observability import ObservabilityState, SessionSnapshot
from .store import StoreError, read_events_file

try:
    import readline as _readline
except ImportError:  # pragma: no cover - platform dependent
    _readline = None

_RESET = "\033[0m"
_CYAN = "\033[1;36m"
_DIM_CYAN = "\033[2;36m"
_GREEN = "\033[1;32m"
_YELLOW = "\033[1;33m"
_RED = "\033[1;31m"
_MAGENTA = "\033[1;35m"
_DIM = "\033[2m"


def _color_line(line: str) -> str:
    """Color one already-padded dashboard line without affecting layout math."""
    if line.startswith("┌"):
        return f"{_CYAN}{line}{_RESET}"
    if line.startswith(("├", "└")):
        return f"{_DIM_CYAN}{line}{_RESET}"
    lowered = line.casefold()
    if " failed " in lowered or "status=failed" in lowered or " cancelled " in lowered:
        return f"{_RED}{line}{_RESET}"
    if " active " in lowered or " running " in lowered or "status=running" in lowered:
        return f"{_YELLOW}{line}{_RESET}"
    if " succeeded " in lowered or " done " in lowered or "status=succeeded" in lowered:
        return f"{_GREEN}{line}{_RESET}"
    if " m  " in lowered:
        return f"{_CYAN}{line}{_RESET}"
    if " s  " in lowered:
        return f"{_MAGENTA}{line}{_RESET}"
    return f"{_DIM}{line}{_RESET}" if "waiting for events" in lowered else line


def _use_color(stream: TextIO) -> bool:
    return (
        _is_tty(stream)
        and not os.environ.get("NO_COLOR")
        and os.environ.get("TERM", "") != "dumb"
    )


class _ColorDashboard(AnsiDashboard):
    """The existing event dashboard with semantic terminal colors."""

    def draw(self, snapshot: SessionSnapshot) -> None:
        if not self.enabled:
            return
        size = shutil.get_terminal_size((120, 40))
        lines = render_dashboard(
            snapshot,
            session_dir=self.session_dir,
            width=size.columns,
            height=size.lines,
        )
        if _use_color(self.stream):
            lines = [_color_line(line) for line in lines]
        self.stream.write("\033[H\033[2J")
        self.stream.write("\n".join(lines))
        self.stream.flush()


_PROMPT = "cambium> "
_CONTINUATION_PROMPT = "... "


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
  /usage      cumulative tokens, throughput, calls, and cost
  /agents     main/sub-agent lifecycle and provider/model rows
  /context    current trunk, raw tail, checkpoint, and epoch
  /session    persistent interactive-session identity and provider lease
  /model      current provider/model lease and branch generation
  /dashboard  render the latest full dashboard into normal scrollback
  /events     recent durable event summaries
  /new        start a fresh semantic branch; old turn artifacts remain
  /clear      clear the terminal
  /exit       leave Cambium

During a running turn, Ctrl-C cancels that turn and returns to this prompt. The
last successfully published context checkpoint remains the branch head.

Multiline input:
  enter <<< on its own line, write the prompt, then enter >>> on its own line.
"""


def _is_tty(stream: Any) -> bool:
    try:
        return bool(getattr(stream, "isatty", lambda: False)())
    except (AttributeError, OSError):
        return False


def _write_line(out: TextIO, line: str) -> None:
    if line:
        out.write(line)
        if not line.endswith("\n"):
            out.write("\n")


def _input_line(source: TextIO, out: TextIO, prompt: str, *, native: bool) -> str | None:
    if native:
        try:
            return builtins.input(prompt)
        except EOFError:
            return None
    out.write(prompt)
    out.flush()
    line = source.readline()
    if line == "":
        return None
    return line.rstrip("\r\n")


def _read_prompt(source: TextIO, out: TextIO, *, native: bool = False) -> str | None:
    value = _input_line(source, out, _PROMPT, native=native)
    if value is None:
        return None
    if value.strip() != "<<<":
        return value
    lines: list[str] = []
    while True:
        value = _input_line(source, out, _CONTINUATION_PROMPT, native=native)
        if value is None:
            return "\n".join(lines)
        if value.strip() == ">>>":
            return "\n".join(lines)
        lines.append(value)


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


def _write_static_dashboard(
    out: TextIO,
    snapshot: SessionSnapshot,
    *,
    session_dir: Path,
) -> None:
    size = shutil.get_terminal_size((120, 40))
    height = max(18, min(size.lines - 2, 40))
    lines = render_dashboard(
        snapshot,
        session_dir=session_dir,
        width=size.columns,
        height=height,
    )
    if _use_color(out):
        lines = [_color_line(line) for line in lines]
    _write_line(out, "\n".join(lines))


def _event_lines(snapshot: SessionSnapshot) -> str:
    if not snapshot.recent_events:
        return "events: none"
    lines = ["events:"]
    for event in snapshot.recent_events:
        task = event.task_id or "-"
        detail = f"  {event.detail}" if event.detail else ""
        lines.append(f"  #{event.seq:<6} {event.kind:<28} {task}{detail}")
    return "\n".join(lines)


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


async def _run_interactive(
    config: Any,
    *,
    source: TextIO,
    out: TextIO,
    err: TextIO,
    quiet: bool,
) -> int:
    """Run one persistent cache-aligned branch over many supervisor leaves."""
    from cambium import render
    from cambium.auth import AuthError
    from cambium.cli import ExitCode
    from cambium.supervisor import SessionAlreadyRunningError

    session = InteractiveSession(config)
    cumulative, last_snapshot = _restore_history(session)
    state = ObservabilityState(recent_limit=16)
    sequence = 0
    failed = False
    native_input = source is sys.stdin and out is sys.stdout
    history_path = _history_path(session)
    if native_input:
        _load_history(history_path)

    _write_line(out, "Cambium interactive session")
    _write_line(out, session.describe())
    if cumulative.calls:
        _write_line(out, cumulative.line())
        _write_line(out, _context_line(last_snapshot))
    _write_line(
        out,
        "Type /help for commands; use <<< ... >>> for multiline prompts. "
        "Ctrl-C cancels an active turn.",
    )
    out.flush()

    try:
        while True:
            prompt = _read_prompt(source, out, native=native_input)
            if prompt is None or prompt in {"/exit", "/quit"}:
                out.write("\n")
                out.flush()
                return ExitCode.FAILURE if failed else ExitCode.SUCCESS
            command = prompt.strip()
            if not command:
                continue
            if command == "/help":
                _write_line(out, _HELP)
                continue
            if command == "/usage":
                _write_line(out, cumulative.line())
                continue
            if command == "/agents":
                _write_line(out, "\n".join(render_agent_lines(last_snapshot)))
                continue
            if command == "/context":
                _write_line(out, _context_line(last_snapshot))
                continue
            if command in {"/session", "/model"}:
                _write_line(out, session.describe())
                continue
            if command == "/dashboard":
                session_dir = (
                    session.seed.source_session
                    if session.seed is not None
                    else session.root
                )
                _write_static_dashboard(
                    out,
                    last_snapshot,
                    session_dir=session_dir,
                )
                continue
            if command in {"/events", "/tail"}:
                _write_line(out, _event_lines(last_snapshot))
                continue
            if command == "/cancel":
                _write_line(out, "no turn is active; press Ctrl-C while a turn is running")
                continue
            if command == "/new":
                session.reset()
                state = ObservabilityState(recent_limit=16)
                last_snapshot = state.snapshot()
                cumulative = _Cumulative()
                sequence = 0
                _write_line(out, "started a fresh semantic branch")
                _write_line(out, session.describe())
                continue
            if command == "/clear":
                out.write("\033[2J\033[H")
                out.flush()
                continue
            if command.startswith("/"):
                _write_line(out, f"unknown command: {command}; type /help")
                continue

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
            dashboard = _ColorDashboard(
                turn.session_dir,
                stream=out,
                enabled=not quiet,
            )
            completed = False
            cancel_requested = False

            def _live_sink(
                record: dict[str, Any],
                _turn=turn,
                _state: ObservabilityState = state,
                _dashboard: _ColorDashboard = dashboard,
                _session: InteractiveSession = session,
            ) -> None:
                nonlocal sequence
                _session.observe_event(_turn, record)
                sequence += 1
                normalized = dict(record)
                normalized["seq"] = sequence
                _state.apply(normalized)
                if _dashboard.enabled:
                    _dashboard.draw(_state.snapshot(session_dir=_turn.session_dir))
                elif not quiet:
                    _write_line(out, render.render_event_line(record, stream=out))
                out.flush()

            loop = asyncio.get_running_loop()
            turn_task = loop.create_task(session.run_turn(turn, on_event=_live_sink))

            def _request_cancel(_turn_task=turn_task) -> None:
                nonlocal cancel_requested
                if not _turn_task.done():
                    cancel_requested = True
                    _turn_task.cancel()

            signal_installed = False
            try:
                try:
                    loop.add_signal_handler(signal.SIGINT, _request_cancel)
                    signal_installed = True
                except (NotImplementedError, RuntimeError, ValueError):
                    pass

                try:
                    with dashboard:
                        response = await turn_task
                        if dashboard.enabled:
                            dashboard.draw(state.snapshot(session_dir=turn.session_dir))
                except asyncio.CancelledError:
                    if not cancel_requested:
                        raise
                    session.complete_turn(turn, succeeded=False)
                    completed = True
                    snapshot = state.snapshot(session_dir=turn.session_dir)
                    last_snapshot = snapshot
                    cumulative.add(snapshot)
                    _write_line(
                        out,
                        "turn cancelled; the previous successful checkpoint remains "
                        "the branch head",
                    )
                    _write_line(out, cumulative.line())
                    _write_line(out, _branch_line(session))
                    out.flush()
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

            _write_result(out, render, response)
            snapshot = state.snapshot(session_dir=turn.session_dir)
            last_snapshot = snapshot
            cumulative.add(snapshot)
            _write_line(out, cumulative.line())
            _write_line(out, _branch_line(session))
            _write_line(out, _context_line(snapshot))
            out.flush()
    except KeyboardInterrupt:
        out.write("\n")
        out.flush()
        return ExitCode.INTERRUPTED
    except BrokenPipeError:
        return ExitCode.SUCCESS
    finally:
        if native_input:
            _save_history(history_path)


async def run_tui(
    config, *, input_stream=None, output_stream=None, error_stream=None, quiet=False
) -> int:
    """Run Cambium's terminal frontend.

    Real terminals receive the persistent semantic-branch UI. Pipes and injected
    streams retain the deterministic line-oriented adapter used by scripts and
    tests.
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
