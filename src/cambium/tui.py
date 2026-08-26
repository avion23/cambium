"""Interactive terminal frontend with a persistent semantic branch and cockpit."""

from __future__ import annotations

import asyncio
import builtins
import json
import math
import os
import signal
import sqlite3
import sys
from collections import deque
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, TextIO

from cambium.render_markdown import render_markdown_if_tty

from .interactive import (
    InteractiveSession,
    InteractiveSessionBusyError,
    InteractiveSessionError,
)
from .monitor import AnsiDashboard, render_agent_lines
from .observability import ObservabilityState, SessionSnapshot
from .store import StoreError, read_events_file
from .tui_screen import ActivityState, Cockpit, Transcript, render_quota_rows

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
    billing_labels: dict[str, str] = field(default_factory=dict)
    providers_seen: set[str] = field(default_factory=set)

    @staticmethod
    def _providers(snapshot: Any) -> set[str]:
        return {
            provider
            for agent in getattr(snapshot, "agents", ())
            if (provider := getattr(agent, "provider", None)) and isinstance(provider, str)
        }

    def add(self, snapshot: SessionSnapshot) -> None:
        self.providers_seen.update(self._providers(snapshot))
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

    def line(self, *, snapshot: SessionSnapshot | None = None) -> str:
        providers = set(self.providers_seen)
        if snapshot is not None:
            providers.update(self._providers(snapshot))
        subscription = any(
            self.billing_labels.get(provider) == "subscription" for provider in providers
        )
        if subscription and self.estimated_cost_usd <= 0:
            cost = "subscription"
        elif self.estimated_cost_usd <= 0:
            cost = "free"
        else:
            cost = f"${self.estimated_cost_usd:.6f}"
        return (
            "usage: "
            f"calls={self.calls} summaries={self.summary_calls} "
            f"tokens={self.total_tokens} "
            f"(in={self.input_tokens} out={self.output_tokens} "
            f"cached={self.cached_tokens}) "
            f"out/s={self.latest_output_tokens_per_s:.1f} "
            f"cost={cost}"
        )


def _provider_billing_labels(config: Any, repo: Path) -> dict[str, str]:
    """Load non-secret billing labels for providers used by the live TUI.

    Codex OAuth entries from older provider files may omit ``billing_mode``.
    When their auth mode is ``codex_chatgpt`` and every token tariff is zero,
    the UI treats that combination as subscription-backed.  This is a
    presentation-only heuristic; ordinary zero-tariff API-key providers stay
    ``free`` unless their config explicitly declares ``subscription``.
    """
    try:
        from . import oneshot
        from .provider_config import AuthMode, BillingMode, load_providers

        provider_path = oneshot._provider_config_path(config, repo)
        providers = load_providers(provider_path)
    except (ImportError, OSError, ValueError):
        return {}

    labels: dict[str, str] = {}
    for provider in providers:
        name = getattr(provider, "name", None)
        if not isinstance(name, str) or not name:
            continue
        mode = getattr(getattr(provider, "billing_mode", None), "value", None)
        if mode == BillingMode.SUBSCRIPTION.value:
            labels[name] = "subscription"
            continue
        if mode == BillingMode.FREE.value:
            labels[name] = "free"
            continue

        prices = (
            getattr(provider, "price_per_1m_in", 0.0),
            getattr(provider, "price_per_1m_cached_in", 0.0),
            getattr(provider, "price_per_1m_out", 0.0),
        )
        zero_tariffs = all(
            not isinstance(price, bool)
            and isinstance(price, int | float)
            and math.isfinite(price)
            and price == 0.0
            for price in prices
        )
        auth = getattr(getattr(provider, "auth", None), "value", None)
        if zero_tariffs and auth == AuthMode.CODEX_CHATGPT.value:
            labels[name] = "subscription"
        elif zero_tariffs:
            labels[name] = "free"
        else:
            labels[name] = "metered"
    return labels


_HELP = """Commands:
  /help       show this help
  /status     branch, context, agents, and usage in one view
  /usage      cumulative tokens, throughput, calls, and cost
  /agents     main/sub-agent lifecycle and provider/model rows
  /context    current trunk, raw tail, checkpoint, and epoch
  /session    persistent interactive-session identity and provider lease
  /model      list eligible provider/model targets
  /model P    select provider P (or P:M) for subsequent turns
  /branches   list durable branch heads with epoch/checkpoint references
  /fork       fork a new branch from the current checkpoint
  /quota      show provider quota-window state
  /compact    flush semantic context and check for a K0 rollover
  /dashboard  explain the visible live cockpit
  /detail     toggle the compact agents/usage/context detail row
  /events     recent durable event summaries
  /new        start a fresh semantic branch; old turn artifacts remain
  /clear      clear the visible cockpit transcript
  /exit       leave Cambium (also /quit or a prompt containing only q)

Transcript view:
  v           toggle full command/output details for every tool entry.
  Live output is appended to the terminal's native scrollback; use the
  terminal's normal PageUp/PageDown or search controls to review it.

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


def _trailing_incomplete_csi(value: str) -> str:
    """Return a trailing CSI prefix that needs the next input chunk."""
    escape = value.rfind("\x1b")
    if escape < 0:
        return ""
    suffix = value[escape:]
    if suffix == "\x1b":
        return suffix
    if not suffix.startswith("\x1b["):
        return ""
    return suffix if all("0" <= char <= "?" for char in suffix[2:]) else ""


def _strip_bracketed_paste_markers(value: str) -> str:
    """Remove every complete terminal paste marker from ``value``."""
    return value.replace(_BRACKETED_PASTE_START, "").replace(_BRACKETED_PASTE_END, "")


def _unframe_bracketed_paste(value: str, source: TextIO) -> str:
    """Remove terminal paste framing while retaining newlines in the payload."""
    if not _is_tty(source):
        return value.rstrip("\r\n")

    accumulated = value
    while True:
        pending = _trailing_incomplete_csi(accumulated)
        if pending:
            accumulated = accumulated[: -len(pending)]
        start = accumulated.find(_BRACKETED_PASTE_START)
        end = (
            accumulated.find(_BRACKETED_PASTE_END, start + len(_BRACKETED_PASTE_START))
            if start >= 0
            else -1
        )
        if end >= 0 and not pending:
            suffix = accumulated[end + len(_BRACKETED_PASTE_END) :].rstrip("\r\n")
            return _strip_bracketed_paste_markers(accumulated[:end] + suffix)
        if start < 0 and not pending:
            return _strip_bracketed_paste_markers(accumulated).rstrip("\r\n")

        line = source.readline()
        if line == "":
            return _strip_bracketed_paste_markers(accumulated).rstrip("\r\n")
        accumulated += pending + line


def _input_line(source: TextIO, out: TextIO, prompt: str, *, native: bool) -> str | None:
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


def _read_multiline(value: str, read_next: Callable[[], str | None]) -> str | None:
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


def _read_prompt(source: TextIO, out: TextIO, *, native: bool = False) -> str | None:
    """Read one prompt, preserving pasted newlines and continued lines."""
    value = _input_line(source, out, _PROMPT, native=native)
    if value is None:
        return None
    return _read_multiline(
        value,
        lambda: _input_line(source, out, _CONTINUATION_PROMPT, native=native),
    )


def _read_cockpit_prompt(source: TextIO, cockpit: Cockpit, *, native: bool) -> str | None:
    """Read input on the primary-buffer prompt line with native editing."""

    def read_one(label: str) -> str | None:
        cockpit.move_to_input(label=label, native=native)
        try:
            return _input_line(source, cockpit.stream, "", native=native)
        finally:
            # Injected streams do not echo the line terminator themselves;
            # native readline does.  Commit only the former so the append-only
            # primary buffer does not acquire an extra blank line on a real
            # terminal.
            cockpit.hide_cursor(commit=not native)

    value = read_one("›")
    if value is None:
        return None
    return _read_multiline(value, lambda: read_one("…"))


def _is_quit_prompt(prompt: str) -> bool:
    """Return whether one submitted prompt unambiguously requests exit."""
    return prompt in {"/exit", "/quit"} or prompt.strip() == "q"


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


def _event_payload(event: Mapping[str, Any]) -> Mapping[str, Any]:
    value = event.get("payload")
    return value if isinstance(value, Mapping) else {}


def _event_text(payload: Mapping[str, Any], *keys: str) -> str | None:
    for key in keys:
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            return value
    return None


def _queued_prompt_notice(prompt: str) -> str | None:
    return f"queued: {prompt}" if prompt.strip() else None


def _restore_result_summary(turn_dir: Path) -> str | None:
    result_path = turn_dir / ".cambium" / "result.json"
    try:
        with result_path.open(encoding="utf-8") as stream:
            document = json.load(stream)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return None
    if not isinstance(document, Mapping):
        return None
    summary = document.get("summary")
    return summary if isinstance(summary, str) and summary.strip() else None


def _restore_turn_transcript(
    turn_dir: Path,
    events: list[dict[str, Any]],
    transcript: Transcript,
) -> None:
    """Replay durable prompt/output events into the bounded cockpit tail."""
    prompt_seen = False
    result_summary: str | None = None
    for event in events:
        kind = event.get("kind")
        payload = _event_payload(event)
        if kind == "task_assigned" and not prompt_seen:
            task_id = event.get("task_id")
            if task_id in {None, "interactive-main"}:
                prompt = _event_text(payload, "task", "prompt", "user_prompt")
                if prompt is not None:
                    transcript.user(prompt)
                    prompt_seen = True
        elif kind in {"user_prompt", "user_message", "prompt"}:
            prompt = _event_text(payload, "text", "content", "prompt", "task")
            if prompt is not None:
                transcript.user(prompt)
                prompt_seen = True
        elif kind == "response":
            response = _event_text(payload, "text", "content", "summary", "output_text")
            if response is not None:
                transcript.assistant(response)
        if kind == "result":
            result_summary = _event_text(payload, "summary", "output_text") or result_summary
        transcript.observe_event(event)

    if not prompt_seen:
        plan_path = turn_dir / "plan.json"
        try:
            with plan_path.open(encoding="utf-8") as stream:
                plan = json.load(stream)
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            plan = None
        tasks = plan.get("tasks") if isinstance(plan, Mapping) else None
        if isinstance(tasks, list) and tasks:
            first = tasks[0]
            if isinstance(first, Mapping):
                prompt = first.get("task")
                if isinstance(prompt, str) and prompt.strip():
                    transcript.user(prompt)

    transcript.finish_stream(result_summary or _restore_result_summary(turn_dir))


def _restore_history(
    session: InteractiveSession,
    *,
    transcript: Transcript | None = None,
    billing_labels: Mapping[str, str] | None = None,
) -> tuple[_Cumulative, SessionSnapshot]:
    """Rebuild usage, dashboard state, and optionally the durable transcript tail."""
    cumulative = _Cumulative(billing_labels=dict(billing_labels or {}))
    latest = ObservabilityState(recent_limit=16).snapshot()
    for turn_dir in session.active_turn_dirs():
        event_db = turn_dir / ".cambium" / "events.db"
        if not event_db.is_file():
            continue
        state = ObservabilityState(recent_limit=16)
        try:
            events = read_events_file(event_db)
            for event in events:
                state.apply(event)
        except (OSError, ValueError, StoreError, sqlite3.Error):
            continue
        if transcript is not None:
            _restore_turn_transcript(turn_dir, events, transcript)
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
            if _is_quit_prompt(prompt):
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
    cockpit: Cockpit | None = None,
) -> str | None:
    parts = command.split(maxsplit=1)
    name = parts[0] if parts else command
    argument = parts[1].strip() if len(parts) == 2 else ""
    if name == "/help" and not argument:
        return _HELP
    if name == "/usage" and not argument:
        return cumulative.line(snapshot=snapshot)
    if name == "/agents" and not argument:
        return "\n".join(render_agent_lines(snapshot))
    if name == "/context" and not argument:
        return _context_line(snapshot)
    if name == "/session" and not argument:
        return session.describe()
    if name == "/quota" and not argument:
        rows = render_quota_rows(snapshot)
        if not rows:
            return "quota: no provider quota observations"
        return "\n".join(("quota:", *rows))
    if name == "/model":
        if not argument:
            try:
                options = session.eligible_provider_models()
            except (OSError, ValueError, InteractiveSessionError) as exc:
                return f"model: provider config/auth unavailable ({exc})"
            if not options:
                return "model: no eligible provider/model targets (enabled + credential-ready)"
            current_provider = session.provider
            current_model = session.model
            lines = ["eligible provider/model targets (enabled + credential-ready):"]
            for provider, model in options:
                current = provider == current_provider and (
                    current_model in {None, "auto"} or model == current_model
                )
                marker = "* " if current else "  "
                suffix = " (current)" if current else ""
                lines.append(f"{marker}{provider}:{model}{suffix}")
            return "\n".join(lines)
        return session.set_model_preference(argument)
    if name == "/branches" and not argument:
        heads = session.branch_heads()
        if not heads:
            return "branches: none (no durable checkpoints)"
        lines = ["branches:"]
        for head in heads:
            marker = "* " if head.current else "  "
            lines.append(
                f"{marker}turn={head.turn} epoch={head.epoch} checkpoint={head.checkpoint_ref}"
            )
        return "\n".join(lines)
    if name == "/fork":
        if argument:
            return "usage: /fork"
        try:
            return session.fork()
        except InteractiveSessionError as exc:
            return str(exc)
    if name == "/compact" and not argument:
        return session.compact()
    if name == "/dashboard" and not argument:
        return "The persistent cockpit is already the live dashboard."
    if name == "/detail" and not argument:
        if cockpit is None:
            return "detail: unavailable"
        state = "shown" if cockpit.toggle_detail() else "hidden"
        return f"detail: {state}"
    if name in {"/events", "/tail"} and not argument:
        if not snapshot.recent_events:
            return "events: none"
        lines = ["events:"]
        for event in snapshot.recent_events:
            task = event.task_id or "-"
            detail = f"  {event.detail}" if event.detail else ""
            lines.append(f"  #{event.seq:<6} {event.kind:<28} {task}{detail}")
        return "\n".join(lines)
    if name == "/cancel" and not argument:
        return "No turn is active; press Ctrl-C while a turn is running."
    if name == "/status" and not argument:
        return "\n".join(
            [
                session.describe(),
                _branch_line(session),
                _context_line(snapshot),
                cumulative.line(snapshot=snapshot),
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
    lock_acquired = False
    try:
        session.acquire()
        lock_acquired = True
    except InteractiveSessionBusyError as exc:
        err.write(f"cambium tui: {exc}\n")
        err.flush()
        return ExitCode.TEMPORARY_FAILURE

    try:
        transcript = Transcript()
        billing_labels = _provider_billing_labels(config, session.repo)
        cumulative, last_snapshot = _restore_history(
            session,
            transcript=transcript,
            billing_labels=billing_labels,
        )
    except BaseException:
        session.release()
        raise
    state = ObservabilityState(recent_limit=16)
    if session.reconnected:
        message = session.resume_summary()
        if session.recovered_stale_lock:
            message += "\nRecovered a stale frontend lock from a terminated process."
        transcript.system(message)
    elif session.turn:
        transcript.system(
            "Cambium interactive session\n"
            f"Reopened persistent branch at turn {session.turn}. "
            "Durable turn artifacts and the latest context checkpoint were restored."
        )
    else:
        transcript.system(
            "Cambium interactive session\nPersistent CAST branch ready. Type /help for commands."
        )
    sequence = 0
    failed = False
    native_input = source is sys.stdin and out is sys.stdout
    cockpit = Cockpit(out, enabled=not quiet)
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
            cockpit.flush()
            return pending_prompts.popleft()
        _start_input_read()
        if input_task is None:
            return None
        task = input_task
        input_task = None
        prompt = await task
        cockpit.flush()
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

    def _draw_final(snapshot: SessionSnapshot, *, activity_line: str = "") -> None:
        cockpit.draw(
            snapshot,
            transcript,
            session_description=session.describe(),
            branch_line=_branch_line(session),
            cumulative_line=cumulative.line(snapshot=snapshot),
            activity_line=activity_line,
            force=True,
        )

    try:
        with cockpit:
            while True:
                cockpit.draw(
                    last_snapshot,
                    transcript,
                    session_description=session.describe(),
                    branch_line=_branch_line(session),
                    cumulative_line=cumulative.line(snapshot=last_snapshot),
                )
                prompt = await _next_prompt()
                if prompt is None or _is_quit_prompt(prompt):
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
                    cumulative = _Cumulative(billing_labels=dict(billing_labels))
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
                        cockpit=cockpit,
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
                activity = ActivityState()
                activity.start()
                cockpit.draw(
                    state.snapshot(session_dir=turn.session_dir),
                    transcript,
                    session_description=session.describe(),
                    branch_line=_branch_line(session),
                    cumulative_line=cumulative.line(
                        snapshot=state.snapshot(session_dir=turn.session_dir)
                    ),
                    activity_line=activity.render(),
                    turn_active=True,
                )

                loop = asyncio.get_running_loop()

                async def _activity_ticks(_activity: ActivityState = activity) -> None:
                    try:
                        while _activity.active:
                            await asyncio.sleep(0.1)
                            if _activity.active:
                                cockpit.draw_activity(_activity.tick())
                    except asyncio.CancelledError:
                        raise
                    except (BrokenPipeError, OSError, ValueError):
                        return

                activity_task = loop.create_task(_activity_ticks())

                def _live_sink(
                    record: dict[str, Any],
                    _activity: ActivityState = activity,
                    _state: ObservabilityState = state,
                    _cumulative: _Cumulative = cumulative,
                    _turn=turn,
                ) -> None:
                    nonlocal sequence
                    session.observe_event(_turn, record)
                    transcript.observe_event(record)
                    _activity.observe_event(record)
                    sequence += 1
                    normalized = dict(record)
                    normalized["seq"] = sequence
                    _state.apply(normalized)
                    live_snapshot = _state.snapshot(session_dir=_turn.session_dir)
                    cockpit.draw(
                        live_snapshot,
                        transcript,
                        session_description=session.describe(),
                        branch_line=_branch_line(session),
                        cumulative_line=_cumulative.line(snapshot=live_snapshot),
                        activity_line=_activity.render(),
                        turn_active=True,
                    )

                _start_input_read()
                turn_task = loop.create_task(session.run_turn(turn, on_event=_live_sink))

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
                                cockpit.flush()
                                if queued_prompt is None:
                                    input_eof = True
                                elif queued_prompt.strip() == "!cancel" and not turn_task.done():
                                    _request_cancel()
                                else:
                                    notice = _queued_prompt_notice(queued_prompt)
                                    if notice is not None:
                                        pending_prompts.append(queued_prompt)
                                        if _is_quit_prompt(queued_prompt):
                                            input_closing = True
                                        transcript.system(notice)
                                        cockpit.draw(
                                            state.snapshot(session_dir=turn.session_dir),
                                            transcript,
                                            session_description=session.describe(),
                                            branch_line=_branch_line(session),
                                            cumulative_line=cumulative.line(
                                                snapshot=state.snapshot(
                                                    session_dir=turn.session_dir
                                                )
                                            ),
                                            turn_active=True,
                                        )
                                _start_input_read()

                            if turn_task in done:
                                response = await turn_task
                                break
                    except asyncio.CancelledError:
                        if not cancel_requested:
                            raise
                        session.complete_turn(turn, succeeded=False)
                        activity.cancel()
                        completed = True
                        snapshot = state.snapshot(session_dir=turn.session_dir)
                        last_snapshot = snapshot
                        cumulative.add(snapshot)
                        transcript.finish_stream()
                        transcript.system(
                            "turn cancelled; the previous successful checkpoint "
                            "remains the branch head."
                        )
                        _draw_final(snapshot, activity_line=activity.status_line())
                        continue

                    session.observe_result(turn, response)
                    succeeded = response.exit_code == 0
                    session.complete_turn(turn, succeeded=succeeded)
                    activity.complete(succeeded=succeeded)
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
                    activity.complete(succeeded=False)
                    failed = True
                    transcript.error(str(exc))
                    transcript.finish_stream()
                    err.write(f"cambium tui: {exc}\n")
                    err.flush()
                    snapshot = state.snapshot(session_dir=turn.session_dir)
                    last_snapshot = snapshot
                    cumulative.add(snapshot)
                    _draw_final(snapshot, activity_line=activity.status_line())
                    continue
                except BaseException:
                    if not completed:
                        session.complete_turn(turn, succeeded=False)
                    raise
                finally:
                    activity.stop()
                    activity_task.cancel()
                    try:
                        await activity_task
                    except asyncio.CancelledError:
                        pass
                    if signal_installed:
                        loop.remove_signal_handler(signal.SIGINT)

                snapshot = state.snapshot(session_dir=turn.session_dir)
                last_snapshot = snapshot
                cumulative.add(snapshot)
                transcript.finish_stream(_response_markdown(render, response))
                _draw_final(snapshot, activity_line=activity.status_line())
    except KeyboardInterrupt:
        return ExitCode.INTERRUPTED
    except BrokenPipeError:
        return ExitCode.SUCCESS
    finally:
        await _close_input_reader()
        if native_input:
            _save_history(history_path)
        if lock_acquired:
            session.release()


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
