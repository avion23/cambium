"""Pure full-screen terminal view model for Cambium's interactive frontend.

The cockpit is intentionally a presentation layer over immutable session and
observability snapshots.  It owns no provider, worker, branch, or context
state.  The only mutable value is a bounded local transcript used for the
operator's current terminal view.
"""

from __future__ import annotations

import os
import re
import shutil
import signal
import textwrap
from collections import deque
from dataclasses import dataclass
from typing import Any, TextIO

_RESET = "\x1b[0m"
_DIM = "\x1b[2m"
_CYAN = "\x1b[1;36m"
_DIM_CYAN = "\x1b[2;36m"
_BLUE = "\x1b[1;34m"
_GREEN = "\x1b[1;32m"
_YELLOW = "\x1b[1;33m"
_RED = "\x1b[1;31m"
_WHITE = "\x1b[1;37m"

_ALT_ENTER = "\x1b[?1049h\x1b[?25l"
_ALT_EXIT = "\x1b[?25h\x1b[?1049l"
_HOME_CLEAR = "\x1b[H\x1b[2J"
_CLEAR_LINE = "\x1b[2K"

_CONTROLS = re.compile(r"[\x00-\x08\x0b-\x1f\x7f-\x9f]")
_ANSI = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")

_ROLE_COLORS = {
    "user": _BLUE,
    "assistant": _WHITE,
    "tool": _YELLOW,
    "system": _DIM,
    "error": _RED,
}
_ROLE_LABELS = {
    "user": "YOU",
    "assistant": "CAMBIUM",
    "tool": "TOOL",
    "system": "SYSTEM",
    "error": "ERROR",
}


def _is_tty(stream: Any) -> bool:
    try:
        return bool(getattr(stream, "isatty", lambda: False)())
    except (AttributeError, OSError, ValueError):
        return False


def _sanitize(value: Any) -> str:
    text = str(value)
    text = _ANSI.sub("", text)
    return _CONTROLS.sub("", text).replace("\r", "")


def _clip(text: str, width: int) -> str:
    clean = _sanitize(text)
    if width <= 0:
        return ""
    if len(clean) <= width:
        return clean
    if width == 1:
        return "…"
    return clean[: width - 1] + "…"


def _pad(text: str, width: int) -> str:
    clean = _clip(text, width)
    return clean + " " * max(0, width - len(clean))


def _human_count(value: int) -> str:
    if value < 1_000:
        return str(value)
    if value < 1_000_000:
        return f"{value / 1_000:.1f}".rstrip("0").rstrip(".") + "k"
    return f"{value / 1_000_000:.1f}".rstrip("0").rstrip(".") + "m"


def _human_bytes(value: int) -> str:
    if value < 1_024:
        return f"{value}B"
    if value < 1_024 * 1_024:
        return f"{value / 1_024:.1f}".rstrip("0").rstrip(".") + "KiB"
    return f"{value / (1_024 * 1_024):.1f}".rstrip("0").rstrip(".") + "MiB"


def _color_enabled(stream: Any) -> bool:
    return (
        _is_tty(stream)
        and not os.environ.get("NO_COLOR")
        and os.environ.get("TERM", "") != "dumb"
    )


def _paint(text: str, color: str, enabled: bool) -> str:
    return f"{color}{text}{_RESET}" if enabled else text


@dataclass(frozen=True, slots=True)
class TranscriptEntry:
    """One bounded, terminal-only transcript item."""

    role: str
    text: str


class Transcript:
    """Bounded semantic transcript for the current interactive frontend."""

    def __init__(self, *, max_entries: int = 160) -> None:
        if max_entries < 8:
            raise ValueError("max_entries must be at least 8")
        self._entries: deque[TranscriptEntry] = deque(maxlen=max_entries)

    @property
    def entries(self) -> tuple[TranscriptEntry, ...]:
        return tuple(self._entries)

    def clear(self) -> None:
        self._entries.clear()

    def add(self, role: str, text: str) -> None:
        if role not in _ROLE_LABELS:
            raise ValueError(f"unknown transcript role: {role}")
        clean = _sanitize(text).strip("\n")
        if clean:
            self._entries.append(TranscriptEntry(role=role, text=clean))

    def user(self, text: str) -> None:
        self.add("user", text)

    def assistant(self, text: str) -> None:
        self.add("assistant", text)

    def system(self, text: str) -> None:
        self.add("system", text)

    def error(self, text: str) -> None:
        self.add("error", text)

    def observe_event(self, record: dict[str, Any]) -> None:
        """Promote only operator-relevant runtime events into the transcript."""
        kind = record.get("kind")
        payload = record.get("payload")
        if not isinstance(kind, str):
            return
        data = payload if isinstance(payload, dict) else record

        if kind == "tool_event":
            tool = data.get("tool")
            ok = data.get("ok")
            duration = data.get("duration_ms")
            if isinstance(tool, str):
                state = "ok" if ok is True else "failed" if ok is False else "done"
                suffix = f" · {duration}ms" if isinstance(duration, int) else ""
                self.add("tool", f"{tool}: {state}{suffix}")
            return

        if kind in {"child_admitted", "child_rejected"}:
            child = data.get("child_task_id") or data.get("task_id") or "child"
            reason = data.get("reason")
            message = f"{kind.replace('_', ' ')}: {child}"
            if isinstance(reason, str) and reason:
                message += f" · {reason}"
            self.system(message)
            return

        if kind in {"context_epoch_advanced", "context_checkpoint"}:
            epoch = data.get("epoch")
            segments = data.get("summary_segments")
            message = f"context checkpoint · epoch={epoch if isinstance(epoch, int) else '?'}"
            if isinstance(segments, int):
                message += f" · summaries={segments}"
            self.system(message)
            return

        if kind in {"merge_failed", "compaction_failed", "worker_failed", "fatal_error"}:
            reason = data.get("message") or data.get("reason") or data.get("failure_reason")
            self.error(f"{kind.replace('_', ' ')}: {reason or 'unknown failure'}")
            return

        if kind in {"merge_committed", "merge_published"}:
            sha = data.get("merge_sha") or data.get("commit") or data.get("sha")
            self.system(f"repository integrated{f' · {str(sha)[:12]}' if sha else ''}")


def _wrap_markdown(text: str, width: int) -> list[str]:
    """Render Markdown structure as safe plain terminal lines before coloring."""
    width = max(8, width)
    output: list[str] = []
    in_fence = False
    for raw_line in _sanitize(text).splitlines() or [""]:
        stripped = raw_line.strip()
        if stripped.startswith("```"):
            in_fence = not in_fence
            output.append(_clip(raw_line, width))
            continue
        if in_fence:
            output.append(_clip("  " + raw_line, width))
            continue
        if stripped.startswith("#"):
            heading = stripped.lstrip("#").strip()
            output.extend(textwrap.wrap(heading, width=width) or [""])
            continue
        prefix = ""
        body = raw_line
        if stripped.startswith(("- ", "* ")):
            prefix, body = "• ", stripped[2:]
        elif stripped.startswith(">"):
            prefix, body = "│ ", stripped[1:].lstrip()
        continuation = " " * len(prefix)
        wrapped = textwrap.wrap(
            body,
            width=max(4, width - len(prefix)),
            replace_whitespace=False,
            drop_whitespace=True,
            break_long_words=True,
            break_on_hyphens=False,
        )
        if not wrapped:
            output.append("")
        else:
            output.append(prefix + wrapped[0])
            output.extend(continuation + line for line in wrapped[1:])
    return output


def _transcript_lines(transcript: Transcript, width: int, capacity: int) -> list[tuple[str, str]]:
    rendered: list[tuple[str, str]] = []
    for entry in transcript.entries:
        label = _ROLE_LABELS[entry.role]
        rendered.append((entry.role, f" {label}"))
        body_width = max(8, width - 3)
        for line in _wrap_markdown(entry.text, body_width):
            rendered.append((entry.role, "   " + line))
        rendered.append((entry.role, ""))
    if not rendered:
        rendered = [("system", " Waiting for a prompt. Type /help for commands.")]
    return rendered[-max(1, capacity) :]


def _agent_model(agent: Any) -> str:
    provider = getattr(agent, "provider", None)
    model = getattr(agent, "model", None)
    if provider and model:
        return f"{provider}/{model}"
    return model or provider or "?"


def _side_sections(
    snapshot: Any, cumulative_line: str, width: int, capacity: int
) -> list[tuple[str, str]]:
    lines: list[tuple[str, str]] = []
    agents = tuple(getattr(snapshot, "agents", ()))
    lines.append(("heading", " AGENTS"))
    if not agents:
        lines.append(("dim", " no agents yet"))
    else:
        for agent in agents[-6:]:
            role = "M" if getattr(agent, "role", "") == "main" else "S"
            state = str(getattr(agent, "state", "?"))
            task = _clip(getattr(agent, "task_id", "?"), 15)
            lines.append((state, f" {role} {task:<15} {state}"))
            lines.append(("dim", f"   {_clip(_agent_model(agent), width - 4)}"))
            tool = getattr(agent, "tool", None)
            tokens = int(getattr(agent, "total_tokens", 0))
            rate = getattr(agent, "output_tokens_per_s", None)
            suffix = f" · {rate:.1f} out/s" if isinstance(rate, int | float) else ""
            tool_suffix = f" · {tool}" if tool else ""
            lines.append(
                ("dim", f"   {_human_count(tokens)} tok{suffix}{tool_suffix}")
            )

    context = getattr(snapshot, "context", None)
    lines.append(("heading", " CONTEXT"))
    if context is None:
        lines.append(("dim", " unavailable"))
    else:
        approx = "≈" if getattr(context, "approximate", False) else ""
        lines.extend(
            [
                (
                    "normal",
                    f" epoch {getattr(context, 'epoch', 0)} · "
                    f"segments {getattr(context, 'summary_segments', 0)}",
                ),
                (
                    "normal",
                    " trunk "
                    f"{approx}{_human_count(getattr(context, 'estimated_trunk_tokens', 0))} tok",
                ),
                ("dim", f" {_human_bytes(getattr(context, 'summary_trunk_bytes', 0))} serialized"),
                (
                    "normal",
                    " raw "
                    f"{approx}{_human_count(getattr(context, 'estimated_raw_tail_tokens', 0))} tok",
                ),
                (
                    "dim",
                    " checkpoint "
                    + _clip(
                        getattr(context, "checkpoint_ref", None) or "none",
                        width - 13,
                    ),
                ),
            ]
        )

    lines.append(("heading", " SESSION USAGE"))
    for part in cumulative_line.replace("usage: ", "", 1).split(" "):
        if part:
            lines.append(("normal", " " + part))

    recent = tuple(getattr(snapshot, "recent_events", ()))
    if recent:
        lines.append(("heading", " RECENT"))
        for event in recent[-4:]:
            kind = _clip(str(getattr(event, "kind", "event")), 18)
            detail = _clip(str(getattr(event, "detail", "")), width - 4)
            lines.append(("dim", f" {kind}"))
            if detail:
                lines.append(("dim", f"   {detail}"))

    return lines[: max(1, capacity)]


def _style_kind(kind: str) -> str:
    if kind in {"failed", "cancelled", "error"}:
        return _RED
    if kind in {"active", "starting", "merging", "running"}:
        return _YELLOW
    if kind in {"succeeded", "done"}:
        return _GREEN
    if kind == "heading":
        return _CYAN
    if kind == "dim":
        return _DIM
    return ""


def render_cockpit(
    snapshot: Any,
    transcript: Transcript,
    *,
    session_description: str,
    branch_line: str,
    cumulative_line: str,
    width: int,
    height: int,
    color: bool = False,
    input_label: str = "›",
) -> list[str]:
    """Render one deterministic full-screen frame without cursor controls."""
    width = max(64, width)
    height = max(20, height)
    inner = width - 2
    lines: list[str] = []

    status = str(getattr(snapshot, "session_status", "idle"))
    title = f" Cambium · {status} "
    lines.append(_paint("┌" + _pad(title, inner) + "┐", _CYAN, color))
    meta = _clip(f" {_sanitize(session_description)}", inner)
    lines.append("│" + _pad(meta, inner) + "│")
    branch = _clip(" " + _sanitize(branch_line), inner)
    lines.append("│" + _pad(branch, inner) + "│")

    body_height = height - 8
    wide = width >= 104
    if wide:
        side_width = min(44, max(34, width // 3))
        transcript_width = inner - side_width - 1
        lines.append(
            _paint(
                "├" + "─" * transcript_width + "┬" + "─" * side_width + "┤",
                _DIM_CYAN,
                color,
            )
        )
        transcript_rows = _transcript_lines(transcript, transcript_width, body_height)
        side_rows = _side_sections(snapshot, cumulative_line, side_width, body_height)
        for index in range(body_height):
            if index < len(transcript_rows):
                role, left = transcript_rows[index]
                left_plain = _pad(left, transcript_width)
                left_text = _paint(left_plain, _ROLE_COLORS.get(role, ""), color)
            else:
                left_text = " " * transcript_width
            if index < len(side_rows):
                kind, right = side_rows[index]
                right_plain = _pad(right, side_width)
                right_text = _paint(right_plain, _style_kind(kind), color)
            else:
                right_text = " " * side_width
            lines.append("│" + left_text + "│" + right_text + "│")
        lines.append(
            _paint(
                "├" + "─" * transcript_width + "┴" + "─" * side_width + "┤",
                _DIM_CYAN,
                color,
            )
        )
    else:
        summary = (
            f" agents={getattr(snapshot, 'active_agents', 0)} active "
            f"tokens={_human_count(getattr(snapshot, 'total_tokens', 0))} "
            f"out/s={getattr(snapshot, 'output_tokens_per_s', 0.0):.1f} "
            f"epoch={getattr(getattr(snapshot, 'context', None), 'epoch', 0)} "
            f"segments={getattr(getattr(snapshot, 'context', None), 'summary_segments', 0)}"
        )
        lines.append(_paint("├" + "─" * inner + "┤", _DIM_CYAN, color))
        lines.append("│" + _pad(summary, inner) + "│")
        transcript_capacity = max(1, body_height - 1)
        for role, text in _transcript_lines(transcript, inner, transcript_capacity):
            plain = _pad(text, inner)
            lines.append("│" + _paint(plain, _ROLE_COLORS.get(role, ""), color) + "│")
        while len(lines) < height - 4:
            lines.append("│" + " " * inner + "│")
        lines.append(_paint("├" + "─" * inner + "┤", _DIM_CYAN, color))

    help_line = " /help commands · <<< multiline >>> · Ctrl-C cancel frontend · /exit close"
    lines.append("│" + _pad(help_line, inner) + "│")
    input_line = f" {input_label} "
    lines.append("│" + _pad(input_line, inner) + "│")
    lines.append(_paint("└" + "─" * inner + "┘", _CYAN, color))
    return lines[:height]


class Cockpit:
    """Persistent alternate-screen owner for one interactive frontend."""

    def __init__(self, stream: TextIO, *, enabled: bool = True) -> None:
        self.stream = stream
        self.enabled = enabled and _is_tty(stream)
        self.color = self.enabled and _color_enabled(stream)
        self._entered = False
        self._previous_sigterm_handler: Any = None
        self._last_size = os.terminal_size((120, 40))

    @property
    def size(self) -> os.terminal_size:
        return self._last_size

    def __enter__(self) -> Cockpit:
        if self.enabled:
            self._previous_sigterm_handler = signal.getsignal(signal.SIGTERM)
            try:
                signal.signal(signal.SIGTERM, self._handle_sigterm)
            except (OSError, ValueError):
                self._previous_sigterm_handler = None
            self.stream.write(_ALT_ENTER)
            self.stream.flush()
            self._entered = True
        return self

    def __exit__(self, exc_type: Any, exc: Any, traceback: Any) -> None:
        del exc_type, exc, traceback
        self.close()

    def _handle_sigterm(self, signum: int, frame: Any) -> None:
        del frame
        self.close()
        raise SystemExit(128 + signum)

    def close(self) -> None:
        if self._entered:
            self.stream.write(_ALT_EXIT)
            self.stream.flush()
            self._entered = False
        if self._previous_sigterm_handler is not None:
            try:
                signal.signal(signal.SIGTERM, self._previous_sigterm_handler)
            except (OSError, ValueError):
                pass
            self._previous_sigterm_handler = None

    def draw(
        self,
        snapshot: Any,
        transcript: Transcript,
        *,
        session_description: str,
        branch_line: str,
        cumulative_line: str,
        input_label: str = "›",
    ) -> None:
        if not self.enabled:
            return
        self._last_size = shutil.get_terminal_size((120, 40))
        frame = render_cockpit(
            snapshot,
            transcript,
            session_description=session_description,
            branch_line=branch_line,
            cumulative_line=cumulative_line,
            width=self._last_size.columns,
            height=self._last_size.lines,
            color=self.color,
            input_label=input_label,
        )
        self.stream.write(_HOME_CLEAR)
        self.stream.write("\n".join(frame))
        self.stream.flush()

    def move_to_input(self, *, label: str = "›") -> None:
        if not self.enabled:
            return
        row = max(1, self._last_size.lines - 1)
        column = 4 + len(label)
        self.stream.write(f"\x1b[{row};1H{_CLEAR_LINE}│ {label} ")
        self.stream.write("\x1b[?25h")
        self.stream.write(f"\x1b[{row};{column}H")
        self.stream.flush()

    def hide_cursor(self) -> None:
        if self.enabled:
            self.stream.write("\x1b[?25l")
            self.stream.flush()


__all__ = [
    "Cockpit",
    "Transcript",
    "TranscriptEntry",
    "render_cockpit",
]
