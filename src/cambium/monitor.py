"""OpenCode-style terminal dashboard for live Cambium sessions.

The monitor is a projection over durable events and immutable checkpoints.  It
can attach to an existing session, or it can be embedded by ``cambium tui``
while a one-shot run is active.  The renderer never mutates supervisor state.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import shutil
import signal
import sys
from dataclasses import asdict
from pathlib import Path
from typing import Any, TextIO

from .observability import (
    AgentSnapshot,
    ObservabilityState,
    SessionSnapshot,
)
from .store import StoreError, read_events_file

_ALT_ENTER = "\x1b[?1049h\x1b[?25l"
_ALT_EXIT = "\x1b[?25h\x1b[?1049l"
_HOME_CLEAR = "\x1b[H\x1b[2J"


def _is_tty(stream: Any) -> bool:
    try:
        return bool(getattr(stream, "isatty", lambda: False)())
    except (AttributeError, OSError):
        return False


def _clip(value: str, width: int) -> str:
    if width <= 0:
        return ""
    if len(value) <= width:
        return value
    if width == 1:
        return "…"
    return value[: width - 1] + "…"


def _human_count(value: int) -> str:
    if value < 1_000:
        return str(value)
    if value < 1_000_000:
        rendered = f"{value / 1_000:.1f}".rstrip("0").rstrip(".")
        return f"{rendered}k"
    rendered = f"{value / 1_000_000:.1f}".rstrip("0").rstrip(".")
    return f"{rendered}m"


def _human_bytes(value: int) -> str:
    if value < 1_024:
        return f"{value}B"
    if value < 1_024 * 1_024:
        rendered = f"{value / 1_024:.1f}".rstrip("0").rstrip(".")
        return f"{rendered}KiB"
    rendered = f"{value / (1_024 * 1_024):.1f}".rstrip("0").rstrip(".")
    return f"{rendered}MiB"


def _duration(value: float | None) -> str:
    if value is None:
        return "?"
    seconds = max(0, int(value))
    hours, remainder = divmod(seconds, 3600)
    minutes, seconds = divmod(remainder, 60)
    if hours:
        return f"{hours}h{minutes:02d}m"
    if minutes:
        return f"{minutes}m{seconds:02d}s"
    return f"{seconds}s"


def _model(agent: AgentSnapshot) -> str:
    if agent.provider and agent.model:
        return f"{agent.provider}/{agent.model}"
    return agent.model or agent.provider or "?"


def _rate(agent: AgentSnapshot) -> str:
    value = agent.output_tokens_per_s
    return "?" if value is None else f"{value:.1f}"


def _border(title: str, width: int) -> str:
    inner = max(0, width - 2)
    label = f" {title} "
    if len(label) > inner:
        label = _clip(label, inner)
    return "┌" + label + "─" * max(0, inner - len(label)) + "┐"


def _rule(title: str, width: int) -> str:
    inner = max(0, width - 2)
    label = f" {title} "
    if len(label) > inner:
        label = _clip(label, inner)
    return "├" + label + "─" * max(0, inner - len(label)) + "┤"


def _inside(content: str, width: int) -> str:
    inner = max(0, width - 2)
    return "│" + _clip(content, inner).ljust(inner) + "│"


def _bottom(width: int) -> str:
    return "└" + "─" * max(0, width - 2) + "┘"


def render_agent_lines(snapshot: SessionSnapshot) -> list[str]:
    """Stable non-boxed agent report used by CLI status and tests."""

    lines: list[str] = []
    for agent in snapshot.agents:
        parent = agent.parent_task_id or "-"
        state = {
            "active": "running",
            "succeeded": "done",
        }.get(agent.state, agent.state)
        lines.append(
            f"{agent.task_id:<24} {state:<9} role={agent.role} "
            f"parent={parent} gen={agent.generation} turn={agent.turn} "
            f"epoch={agent.epoch} model={_model(agent)} calls={agent.calls} "
            f"tokens={agent.total_tokens} in={agent.input_tokens} "
            f"out={agent.output_tokens} cached={agent.cached_tokens} "
            f"out/s={_rate(agent)} cost=${agent.estimated_cost_usd:.6f}"
        )
    lines.append(
        f"totals: tokens={snapshot.total_tokens} in={snapshot.input_tokens} "
        f"out={snapshot.output_tokens} cached={snapshot.cached_tokens} "
        f"calls={snapshot.calls} summaries={snapshot.summary_calls} "
        f"out/s={snapshot.output_tokens_per_s:.1f} "
        f"cost=${snapshot.estimated_cost_usd:.6f}"
    )
    return lines


def render_dashboard(
    snapshot: SessionSnapshot,
    *,
    session_dir: str | Path,
    width: int = 120,
    height: int = 40,
) -> list[str]:
    """Render one full dashboard frame without terminal control sequences."""

    width = max(60, width)
    height = max(18, height)
    session = str(Path(session_dir).expanduser())
    lines = [_border("Cambium", width)]
    lines.append(
        _inside(
            f"session {_clip(session, max(10, width - 45))}  "
            f"status={snapshot.session_status}  elapsed={_duration(snapshot.elapsed_s)}  "
            f"seq={snapshot.last_seq}",
            width,
        )
    )
    lines.append(
        _inside(
            "agents "
            f"active={snapshot.active_agents} queued={snapshot.queued_agents} "
            f"ok={snapshot.succeeded_agents} failed={snapshot.failed_agents}  "
            "usage "
            f"in={_human_count(snapshot.input_tokens)} "
            f"out={_human_count(snapshot.output_tokens)} "
            f"cache={_human_count(snapshot.cached_tokens)} "
            f"total={_human_count(snapshot.total_tokens)} "
            f"calls={snapshot.calls} summaries={snapshot.summary_calls} "
            f"out/s={snapshot.output_tokens_per_s:.1f} "
            f"cost=${snapshot.estimated_cost_usd:.4f}",
            width,
        )
    )
    context = snapshot.context
    prompt_tokens = (
        "?" if context.exact_prompt_tokens is None else _human_count(context.exact_prompt_tokens)
    )
    lines.append(
        _inside(
            "context "
            f"task={context.task_id or '?'} epoch={context.epoch} "
            f"prompt={prompt_tokens}tok "
            f"trunk≈{_human_count(context.estimated_trunk_tokens)}tok/"
            f"{_human_bytes(context.summary_trunk_bytes)} "
            f"segments={context.summary_segments} "
            f"raw≈{_human_count(context.estimated_raw_tail_tokens)}tok/"
            f"{_human_bytes(context.raw_tail_bytes)} "
            f"messages={context.active_context_messages}",
            width,
        )
    )

    lines.append(_rule("agents", width))
    compact = width < 105
    if compact:
        header = (
            " R  TASK                 STATE      MODEL                    "
            "TURN  TOKENS   OUT/S TOOL"
        )
    else:
        header = (
            " R  TASK                     STATE      MODEL                              "
            "GEN TURN EP   IN      OUT     CACHE   TOTAL   OUT/S TOOL"
        )
    lines.append(_inside(header, width))

    reserved = 6  # top/bottom/rules
    event_rows = min(8, max(3, height // 4))
    agent_capacity = max(3, height - reserved - event_rows - 3)
    agents = snapshot.agents[-agent_capacity:]
    for agent in agents:
        role = "M" if agent.role == "main" else "S"
        tool = agent.tool or "-"
        if compact:
            row = (
                f" {role}  {_clip(agent.task_id, 20):<20} "
                f"{agent.state:<10} {_clip(_model(agent), 24):<24} "
                f"{agent.turn:>4} {_human_count(agent.total_tokens):>7} "
                f"{_rate(agent):>7} {_clip(tool, 14)}"
            )
        else:
            row = (
                f" {role}  {_clip(agent.task_id, 24):<24} "
                f"{agent.state:<10} {_clip(_model(agent), 34):<34} "
                f"{agent.generation:>3} {agent.turn:>4} {agent.epoch:>2} "
                f"{_human_count(agent.input_tokens):>7} "
                f"{_human_count(agent.output_tokens):>7} "
                f"{_human_count(agent.cached_tokens):>7} "
                f"{_human_count(agent.total_tokens):>7} "
                f"{_rate(agent):>6} {_clip(tool, 16)}"
            )
        lines.append(_inside(row, width))
    if not agents:
        lines.append(_inside(" no agents observed yet", width))

    lines.append(_rule("recent durable events", width))
    recent = snapshot.recent_events[-event_rows:]
    for event in recent:
        task = event.task_id or "-"
        detail = f"  {event.detail}" if event.detail else ""
        lines.append(
            _inside(
                f" #{event.seq:<6} {_clip(event.kind, 24):<24} "
                f"{_clip(task, 24):<24}{detail}",
                width,
            )
        )
    if not recent:
        lines.append(_inside(" waiting for events", width))
    while len(lines) < height - 2:
        lines.append(_inside("", width))
    lines.append(
        _inside(
            "Ctrl-C: close monitor  •  "
            "runtime continues unless its owner cancels it",
            width,
        )
    )
    lines.append(_bottom(width))
    return lines[:height]


def snapshot_json(snapshot: SessionSnapshot) -> str:
    return json.dumps(asdict(snapshot), sort_keys=True, separators=(",", ":"))


class AnsiDashboard:
    """Small alternate-screen renderer for an already-owned event stream."""

    def __init__(
        self,
        session_dir: str | Path,
        *,
        stream: TextIO | None = None,
        enabled: bool = True,
    ) -> None:
        self.session_dir = Path(session_dir)
        self.stream = sys.stdout if stream is None else stream
        self.enabled = enabled and _is_tty(self.stream)
        self._entered = False
        self._previous_sigterm_handler: Any = None
        self._sigterm_handler_installed = False

    def _install_sigterm_handler(self) -> None:
        try:
            self._previous_sigterm_handler = signal.getsignal(signal.SIGTERM)
            signal.signal(signal.SIGTERM, self._handle_sigterm)
        except (OSError, ValueError):
            self._previous_sigterm_handler = None
        else:
            self._sigterm_handler_installed = True

    def _restore_sigterm_handler(self) -> None:
        if not self._sigterm_handler_installed:
            return
        previous = self._previous_sigterm_handler
        try:
            signal.signal(signal.SIGTERM, previous)
        finally:
            self._sigterm_handler_installed = False
            self._previous_sigterm_handler = None

    def _leave(self) -> None:
        if self._entered:
            try:
                self.stream.write(_ALT_EXIT)
                self.stream.flush()
            finally:
                self._entered = False
        self._restore_sigterm_handler()

    def _handle_sigterm(self, signum: int, frame: Any) -> None:
        del frame
        self._leave()
        raise SystemExit(128 + signum)

    def __enter__(self) -> AnsiDashboard:
        if self.enabled:
            self._install_sigterm_handler()
            self._entered = True
            try:
                self.stream.write(_ALT_ENTER)
                self.stream.flush()
            except BaseException:
                self._leave()
                raise
        return self

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
        self.stream.write(_HOME_CLEAR)
        self.stream.write("\n".join(lines))
        self.stream.flush()

    def __exit__(self, exc_type, exc, tb) -> None:
        del exc_type, exc, tb
        self._leave()


def _latest_session(repo: Path | None) -> Path | None:
    candidates: list[Path] = []
    root = Path.cwd() if repo is None else repo
    direct = root / ".cambium" / "events.db"
    if direct.is_file():
        candidates.append(root)
    roots = (root / ".cambium" / "sessions",)
    for root in roots:
        if not root.is_dir():
            continue
        candidates.extend(
            path
            for path in root.iterdir()
            if path.is_dir() and (path / ".cambium" / "events.db").is_file()
        )
    if not candidates:
        return None
    return max(
        candidates,
        key=lambda path: (path / ".cambium" / "events.db").stat().st_mtime_ns,
    )


def resolve_session(value: str | Path | None, *, repo: str | Path | None = None) -> Path:
    if value is not None:
        session = Path(value).expanduser().resolve()
    else:
        env_value = os.environ.get("CAMBIUM_SESSION_ID")
        if env_value:
            session = Path(env_value).expanduser().resolve()
        else:
            latest = _latest_session(
                None if repo is None else Path(repo).expanduser().resolve()
            )
            if latest is None:
                raise ValueError("no Cambium session with an event log was found")
            session = latest.resolve()
    event_db = session / ".cambium" / "events.db"
    if not event_db.is_file():
        raise ValueError(f"session event log is missing: {event_db}")
    return session


async def monitor_session_async(
    session_dir: str | Path,
    *,
    interval_s: float = 0.25,
    once: bool = False,
    json_output: bool = False,
    output_stream: TextIO | None = None,
) -> int:
    session = resolve_session(session_dir)
    out = sys.stdout if output_stream is None else output_stream
    state = ObservabilityState()
    dashboard = AnsiDashboard(session, stream=out, enabled=not once and not json_output)
    try:
        with dashboard:
            while True:
                events = read_events_file(
                    session / ".cambium" / "events.db",
                    after_seq=state.last_seq,
                )
                state.extend(events)
                snapshot = state.snapshot(session_dir=session)
                if json_output:
                    out.write(snapshot_json(snapshot) + "\n")
                    out.flush()
                    return 0
                frame = "\n".join(render_dashboard(snapshot, session_dir=session)) + "\n"
                if once:
                    out.write(frame)
                    out.flush()
                    return 0
                if dashboard.enabled:
                    dashboard.draw(snapshot)
                else:
                    out.write(frame)
                    out.flush()
                if (
                    snapshot.session_status in {"ended", "cancelled", "failed"}
                    and snapshot.active_agents == 0
                    and snapshot.queued_agents == 0
                ):
                    await asyncio.sleep(min(interval_s, 0.25))
                    return 0
                await asyncio.sleep(interval_s)
    except (asyncio.CancelledError, KeyboardInterrupt):
        return 130
    except (OSError, StoreError, ValueError) as exc:
        print(f"cambium monitor: {exc}", file=sys.stderr)
        return 1


def monitor_session(
    session_dir: str | Path,
    *,
    interval_s: float = 0.25,
    once: bool = False,
    json_output: bool = False,
    output_stream: TextIO | None = None,
) -> int:
    return asyncio.run(
        monitor_session_async(
            session_dir,
            interval_s=interval_s,
            once=once,
            json_output=json_output,
            output_stream=output_stream,
        )
    )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="cambium monitor",
        description="Attach an OpenCode-style dashboard to a durable Cambium session.",
    )
    parser.add_argument("session", nargs="?", help="session directory; defaults to the newest")
    parser.add_argument("--repo", default=None, help="repository used for session discovery")
    parser.add_argument("--interval", type=float, default=0.25, metavar="SECONDS")
    parser.add_argument("--once", action="store_true", help="render one frame and exit")
    parser.add_argument("--json", action="store_true", help="emit one JSON snapshot and exit")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if not math_is_positive(args.interval):
        print("cambium monitor: --interval must be a positive finite number", file=sys.stderr)
        return 2
    try:
        session = resolve_session(args.session, repo=args.repo)
    except ValueError as exc:
        print(f"cambium monitor: {exc}", file=sys.stderr)
        return 1
    return monitor_session(
        session,
        interval_s=args.interval,
        once=args.once,
        json_output=args.json,
    )


def math_is_positive(value: Any) -> bool:
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return False
    return number > 0 and number < float("inf")


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "AnsiDashboard",
    "main",
    "monitor_session",
    "monitor_session_async",
    "render_agent_lines",
    "render_dashboard",
    "resolve_session",
    "snapshot_json",
]
