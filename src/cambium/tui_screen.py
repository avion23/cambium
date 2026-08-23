"""Terminal presentation model for Cambium's interactive frontend.

The cockpit is intentionally a presentation layer over immutable session and
observability snapshots.  It owns no provider, worker, branch, or context
state.  The only mutable value is a bounded local transcript used for the
operator's current terminal view.  Live output is appended to the terminal's
primary buffer so the terminal, rather than a private alternate screen, owns
scrollback.

``render_cockpit`` remains available as a deterministic framed renderer for
presentation tests and callers that need a bounded snapshot.  ``Cockpit``
uses ``render_primary`` for the live interactive path.
"""

from __future__ import annotations

import math
import os
import re
import shutil
import signal
import sqlite3
import textwrap
from collections import deque
from collections.abc import Mapping
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any, TextIO

from .provider_scheduler import QuotaLedger

_RESET = "\x1b[0m"
_DIM = "\x1b[2m"
_CYAN = "\x1b[1;36m"
_DIM_CYAN = "\x1b[2;36m"
_BLUE = "\x1b[1;34m"
_GREEN = "\x1b[1;32m"
_YELLOW = "\x1b[1;33m"
_RED = "\x1b[1;31m"
_WHITE = "\x1b[1;37m"

_CONTROLS = re.compile(r"[\x00-\x08\x0b-\x1f\x7f-\x9f]")
_ANSI = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")

_ROLE_COLORS = {
    "user": _BLUE,
    "assistant": _WHITE,
    "tool": _YELLOW,
    "system": _DIM,
    "error": _RED,
    "dim": _DIM,
}
_ROLE_LABELS = {
    "user": "YOU",
    "assistant": "CAMBIUM",
    "tool": "TOOL",
    "system": "SYSTEM",
    "error": "ERROR",
}

# The supervisor currently exposes lifecycle/tool events rather than a
# provider-specific token stream.  Keep the accepted presentation events
# explicit: arbitrary log payloads must never become terminal prose.
_ASSISTANT_STREAM_KINDS = frozenset(
    {
        "assistant_delta",
        "assistant_message",
        "assistant_output",
        "assistant_output_delta",
        "assistant_text",
        "assistant_text_delta",
        "content_delta",
        "message",
        "message_delta",
        "output_text_delta",
        "partial_output",
        "response.output_text.delta",
        "stream_chunk",
        "text_delta",
    }
)
_TOOL_STREAM_KINDS = frozenset(
    {
        "tool_message",
        "tool_message_delta",
        "tool_output",
        "tool_output_delta",
    }
)
_STREAM_DELTA_KINDS = frozenset(
    {
        "assistant_delta",
        "assistant_output_delta",
        "assistant_text_delta",
        "content_delta",
        "message_delta",
        "output_text_delta",
        "partial_output",
        "response.output_text.delta",
        "stream_chunk",
        "text_delta",
        "tool_message_delta",
        "tool_output_delta",
    }
)
_STREAM_TEXT_LIMIT = 16_384
_STREAM_RENDER_LIMIT = 8_192
_TOOL_DETAIL_RENDER_LIMIT = 40
_TOOL_DETAIL_KEYS = (
    "cmd",
    "command",
    "error",
    "failure_reason",
    "reason",
    "message",
    "output",
    "stdout",
    "stderr",
    "detail",
)
_FAILURE_CONTEXT_PREFIX = "↳ "
_FAILURE_BLOCK_LIMIT = 64
_FAILURE_EVENT_KINDS = frozenset(
    {
        "child_failed",
        "compaction_failed",
        "context_resume_failed",
        "error",
        "fatal_error",
        "merge_failed",
        "plan_failed",
        "session_failed",
        "task_failed",
        "turn_failed",
        "turn_failure",
        "worker_failed",
    }
)
_FAILURE_STATUSES = frozenset({"error", "failed", "timeout"})


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
        _is_tty(stream) and not os.environ.get("NO_COLOR") and os.environ.get("TERM", "") != "dumb"
    )


def _paint(text: str, color: str, enabled: bool) -> str:
    return f"{color}{text}{_RESET}" if enabled else text


def _event_data(record: Mapping[str, Any]) -> Mapping[str, Any]:
    payload = record.get("payload")
    return payload if isinstance(payload, Mapping) else record


def _message_role(kind: str, data: Mapping[str, Any]) -> str | None:
    role = data.get("role")
    if role not in {"assistant", "tool"}:
        message = data.get("message")
        if isinstance(message, Mapping):
            role = message.get("role")
    if role in {"assistant", "tool"}:
        return role
    if kind == "tool_event" or kind in _TOOL_STREAM_KINDS:
        return "tool"
    if kind in _ASSISTANT_STREAM_KINDS and kind != "message":
        return "assistant"
    return None


def _text_value(value: Any, *, depth: int = 0) -> str | None:
    """Extract text from the small set of provider message shapes we display."""
    if depth > 4:
        return None
    if isinstance(value, str):
        return value
    if isinstance(value, Mapping):
        for key in ("delta", "text", "content", "output_text", "partial", "chunk"):
            if key in value:
                text = _text_value(value[key], depth=depth + 1)
                if text is not None:
                    return text
        message = value.get("message")
        if message is not None:
            return _text_value(message, depth=depth + 1)
        return None
    if isinstance(value, list | tuple):
        parts: list[str] = []
        for item in value:
            text = _text_value(item, depth=depth + 1)
            if text is not None:
                parts.append(text)
        return "".join(parts) if parts else None
    return None


def _result_text(data: Mapping[str, Any]) -> str | None:
    parts: list[str] = []
    for key in ("summary", "assistant_text", "output_text"):
        value = data.get(key)
        if isinstance(value, str) and value:
            parts.append(value)
    results = data.get("results")
    if isinstance(results, list | tuple):
        for result in results:
            if not isinstance(result, Mapping):
                continue
            summary = result.get("summary")
            if isinstance(summary, str) and summary:
                parts.append(summary)
    return "\n\n".join(parts) if parts else None


def _stream_update(
    record: Mapping[str, Any],
) -> tuple[str, str, bool, str | None] | None:
    """Return ``(role, text, append, message_id)`` for displayable output."""
    kind = record.get("kind")
    if not isinstance(kind, str):
        return None
    data = _event_data(record)
    if kind == "result":
        if data.get("status") in _FAILURE_STATUSES:
            return None
        text = _result_text(data)
        if text is None:
            return None
        return "assistant", text, False, None
    if kind == "tool_event" or kind in _ASSISTANT_STREAM_KINDS or kind in _TOOL_STREAM_KINDS:
        if kind == "tool_event" and _tool_status(data) is False:
            return None
        role = _message_role(kind, data)
        if role is None:
            return None
        text = _text_value(data)
        if not text:
            return None
        append = kind in _STREAM_DELTA_KINDS
        if data.get("cumulative") is True or data.get("replace") is True:
            append = False
        if data.get("append") is True:
            append = True
        message_id = data.get("message_id") or data.get("id")
        return role, text, append, message_id if isinstance(message_id, str) else None
    return None


def _duration_ms(value: Any) -> int | float | None:
    if type(value) not in (int, float):
        return None
    return value


def _tool_status(data: Mapping[str, Any]) -> bool | None:
    status = data.get("ok")
    if type(status) is bool:
        return status
    status_name = data.get("status")
    if isinstance(status_name, str):
        normalized = status_name.casefold()
        if normalized in {"failed", "failure", "error"}:
            return False
        if normalized in {"ok", "success", "succeeded"}:
            return True
    for key in ("error", "failure_reason"):
        value = data.get(key)
        if isinstance(value, str) and value:
            return False
    return None


def _tool_detail_value(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, str):
        return value
    text = _text_value(value)
    if text is not None:
        return text
    if isinstance(value, list | tuple):
        return " ".join(_sanitize(item) for item in value)
    if isinstance(value, Mapping):
        parts = [f"{key}={_sanitize(item)}" for key, item in value.items()]
        return ", ".join(parts) if parts else None
    return _sanitize(value)


def _tool_entry_text(
    data: Mapping[str, Any],
    tool: str,
    ok: bool | None,
    duration_ms: int | float | None,
) -> str:
    state = "ok" if ok is True else "failed" if ok is False else "done"
    duration = f" · {_format_duration(duration_ms)}" if duration_ms is not None else ""
    lines = [f"{tool}: {state}{duration}"]
    for key in _TOOL_DETAIL_KEYS:
        detail = _tool_detail_value(data.get(key))
        if detail:
            lines.append(f"{key}: {detail}")
    return "\n".join(lines)


def _format_duration(duration_ms: int | float | None) -> str:
    if duration_ms is None:
        return ""
    if isinstance(duration_ms, float) and duration_ms.is_integer():
        return f"{int(duration_ms)}ms"
    return f"{duration_ms}ms"


def _tool_line(
    entry: TranscriptEntry,
    *,
    count: int = 1,
    last_duration_ms: int | float | None = None,
) -> str:
    glyph = "✓" if entry.tool_ok is True else "✗" if entry.tool_ok is False else "•"
    name = entry.tool_name or "?"
    if count > 1:
        line = f"{glyph} {name} ×{count}"
        duration = _format_duration(last_duration_ms)
        return f"{line} · last {duration}" if duration else line
    duration = _format_duration(entry.duration_ms)
    return f"{glyph} {name} {duration}".rstrip() if duration else f"{glyph} {name}"


@dataclass(frozen=True, slots=True)
class TranscriptEntry:
    """One bounded, terminal-only transcript item."""

    role: str
    text: str
    tool_name: str | None = None
    tool_ok: bool | None = None
    duration_ms: int | float | None = None


@dataclass(slots=True)
class _FailureBlock:
    """Presentation state for one task failure within one turn."""

    task_id: str
    turn: int | None
    cause: str | None
    context: list[str]
    entry: TranscriptEntry | None = None


def _task_id(record: Mapping[str, Any], data: Mapping[str, Any]) -> str | None:
    for value in (record.get("task_id"), data.get("task_id")):
        if isinstance(value, str) and value:
            return value
    return None


def _event_turn(data: Mapping[str, Any]) -> int | None:
    value = data.get("turn")
    return value if type(value) is int and value >= 0 else None


def _failure_context_line(kind: str, data: Mapping[str, Any]) -> str | None:
    """Return a short, safe line for a failure's preceding-event context."""
    if kind == "tool_event" and _tool_status(data) is False:
        tool = data.get("tool")
        if not isinstance(tool, str) or not tool:
            return "tool failed"
        line = f"{tool}: failed"
        for key in _TOOL_DETAIL_KEYS:
            detail = _tool_detail_value(data.get(key))
            if detail:
                line += f" · {key}: {detail}"
        return _sanitize(line)

    if kind == "timeout":
        phase = data.get("phase")
        return _sanitize(f"timeout: {phase}" if isinstance(phase, str) and phase else "timeout")

    if kind == "restart_scheduled":
        count = data.get("restart_count")
        maximum = data.get("max_restarts")
        if type(count) is int and type(maximum) is int:
            return f"restart scheduled: {count}/{maximum}"
        return "restart scheduled"

    if kind == "usage_event":
        reason = data.get("failure_reason")
        if isinstance(reason, str) and reason:
            return _sanitize(f"provider call failed: {reason}")

    if kind == "protocol":
        detail = data.get("note") or data.get("error_type")
        if isinstance(detail, str) and detail:
            return _sanitize(f"protocol: {detail}")

    if kind == "log" and data.get("stream") == "worker-error":
        detail = data.get("message") or data.get("error_type")
        if isinstance(detail, str) and detail:
            return _sanitize(f"worker error: {detail}")
    return None


def _failure_cause(kind: str, data: Mapping[str, Any]) -> str | None:
    """Extract the most actionable failure cause from one terminal event."""
    cause: str | None = None
    for key in ("failure_reason", "reason", "message", "error"):
        value = data.get(key)
        if isinstance(value, str) and value.strip():
            cause = _sanitize(value).strip()
            break
    maximum = data.get("max_restarts")
    if (
        kind == "worker_failed"
        and type(maximum) is int
        and maximum >= 0
        and cause
        and not cause.startswith("max_restarts (")
    ):
        cause = f"max_restarts ({maximum}): {cause}"
    if cause:
        return cause
    if kind == "result":
        status = data.get("status")
        if isinstance(status, str) and status:
            return _sanitize(f"worker reported {status}")
    return _sanitize(kind.replace("_", " ")) if kind else None


def _failure_summary(text: str) -> tuple[str | None, str | None] | None:
    """Parse a rendered result summary without exposing its detail twice."""
    clean = _sanitize(text)
    if not clean:
        return None
    failed = bool(
        re.search(r"\bstatus=(?:error|failed|timeout)\b", clean)
        or re.search(r"\bplan_status=\{[^}]*\b(?:error|failed|timeout)\b", clean)
        or "plan_failures={" in clean
    )
    if not failed:
        return None

    task_id: str | None = None
    cause: str | None = None
    plan_failures = re.search(r"\bplan_failures=\{([^}]*)\}", clean)
    if plan_failures is not None:
        pair = re.search(r"([^,\s:{}]+)\s*:\s*(['\"])(.*?)\2", plan_failures.group(1))
        if pair is not None:
            task_id = pair.group(1)
            cause = pair.group(3)

    if task_id is None:
        task_match = re.search(r"\btask(?:_id)?=([^\s]+)", clean)
        if task_match is not None:
            task_id = task_match.group(1).strip("'\"")
    if cause is None:
        reason = re.search(r"\b(?:failure_reason|reason)=((['\"])(.*?)\2|[^\s]+)", clean)
        if reason is not None:
            cause = reason.group(3) if reason.group(3) is not None else reason.group(1)
            if cause is not None:
                cause = cause.strip("'\"")
    if cause:
        cause = _sanitize(cause).strip()
    return task_id, cause or None


class Transcript:
    """Bounded semantic transcript for the current interactive frontend."""

    def __init__(self, *, max_entries: int = 160) -> None:
        if max_entries < 8:
            raise ValueError("max_entries must be at least 8")
        self._entries: deque[TranscriptEntry] = deque(maxlen=max_entries)
        self._stream_role: str | None = None
        self._stream_text = ""
        self._stream_message_id: str | None = None
        self._stream_truncated = False
        self._turn_serial = 0
        self._turn_by_task: dict[str, int] = {}
        self._failure_context: dict[tuple[str, int | None, int], list[str]] = {}
        self._failure_blocks: dict[tuple[str, int | None, int], _FailureBlock] = {}
        self._failure_order: deque[tuple[str, int | None, int]] = deque()
        self._failure_previews: dict[tuple[str, int | None, int], _FailureBlock] = {}
        self._failure_preview_order: deque[tuple[str, int | None, int]] = deque()
        self._tool_details_expanded = False

    @property
    def entries(self) -> tuple[TranscriptEntry, ...]:
        return tuple(self._entries)

    def clear(self) -> None:
        self._entries.clear()
        self._clear_stream()
        self._turn_serial = 0
        self._turn_by_task.clear()
        self._failure_context.clear()
        self._failure_blocks.clear()
        self._failure_order.clear()
        self._failure_previews.clear()
        self._failure_preview_order.clear()

    @property
    def tool_details_expanded(self) -> bool:
        """Whether tool command/output details are shown instead of compact lines."""
        return self._tool_details_expanded

    def toggle_tool_details(self) -> bool:
        """Toggle the presentation-only expand-all tool detail view."""
        self._tool_details_expanded = not self._tool_details_expanded
        return self._tool_details_expanded

    def add(self, role: str, text: str) -> None:
        if role not in _ROLE_LABELS:
            raise ValueError(f"unknown transcript role: {role}")
        clean = _sanitize(text).strip("\n")
        if clean:
            self._entries.append(TranscriptEntry(role=role, text=clean))

    def user(self, text: str) -> None:
        self._turn_serial += 1
        self._turn_by_task.clear()
        self._failure_previews.clear()
        self._failure_preview_order.clear()
        self.add("user", text)

    def assistant(self, text: str) -> None:
        self.add("assistant", text)

    def system(self, text: str) -> None:
        self.add("system", text)

    def error(self, text: str) -> None:
        self.add("error", text)

    @property
    def streaming_text(self) -> str:
        """The bounded in-flight model text, if a turn is still generating."""
        return self._stream_text

    @property
    def streaming_role(self) -> str | None:
        return self._stream_role

    def _clear_stream(self) -> None:
        self._stream_role = None
        self._stream_text = ""
        self._stream_message_id = None
        self._stream_truncated = False

    def _commit_stream(self) -> None:
        if self._stream_role is not None and self._stream_text:
            self.add(self._stream_role, self._stream_text)
        self._clear_stream()

    def _bounded_stream_text(self, text: str) -> str:
        if len(text) <= _STREAM_TEXT_LIMIT:
            self._stream_truncated = False
            return text
        self._stream_truncated = True
        return "…\n" + text[-(_STREAM_TEXT_LIMIT - 2) :]

    def _update_stream(
        self,
        role: str,
        text: str,
        *,
        append: bool,
        message_id: str | None,
    ) -> None:
        if self._stream_role != role or (
            message_id is not None
            and self._stream_message_id is not None
            and message_id != self._stream_message_id
        ):
            self._commit_stream()
        self._stream_role = role
        self._stream_message_id = message_id

        current = self._stream_text
        if not current:
            merged = text
        elif text == current or (append and current.endswith(text)):
            merged = current
        elif text.startswith(current):
            # Some transports call a cumulative snapshot a delta.  Prefer the
            # longer snapshot so a redraw never duplicates the existing tail.
            merged = text
        elif not append:
            merged = text
        else:
            merged = current + text
        self._stream_text = self._bounded_stream_text(_sanitize(merged))

    def finish_stream(self, final_text: str | None = None) -> None:
        """Commit the active stream and optionally replace it with final text."""
        current = self._stream_text
        role = self._stream_role
        truncated = self._stream_truncated
        self._clear_stream()
        final = _sanitize(final_text).strip("\n") if isinstance(final_text, str) else ""
        summary_failure = _failure_summary(final) if final else None
        if summary_failure is not None:
            task_id, cause = summary_failure
            self._record_failure(task_id, None, cause)
            # The detailed task/cause/context is already in the red failure
            # block. Keep the model footer useful without repeating it.
            final = "plan=failed"
        if not final:
            if current and role is not None:
                self.add(role, current)
            return
        if current and role == "assistant" and not truncated:
            if final.startswith(current) or current.startswith(final):
                final = final if len(final) >= len(current) else current
            elif current != final:
                final = f"{current}\n\n{final}"
        elif current and role is not None:
            self.add(role, current)
        self.assistant(final)

    def _failure_key(self, task_id: str | None, turn: int | None) -> tuple[str, int | None, int]:
        return task_id or "?", turn, self._turn_serial

    def _remember_turn(self, task_id: str | None, turn: int | None) -> None:
        if task_id is not None and turn is not None:
            self._turn_by_task[task_id] = turn

    def _context_key(self, task_id: str | None, turn: int | None) -> tuple[str, int | None, int]:
        if turn is None and task_id is not None:
            turn = self._turn_by_task.get(task_id)
        return self._failure_key(task_id, turn)

    def _failure_block_for(
        self, task_id: str | None, turn: int | None
    ) -> tuple[tuple[str, int | None, int], _FailureBlock] | None:
        key = self._context_key(task_id, turn)
        block = self._failure_blocks.get(key)
        if block is None:
            block = self._failure_previews.get(key)
        if block is not None:
            return key, block
        wanted = task_id or "?"
        for candidate_key in reversed(self._failure_order):
            candidate = self._failure_blocks.get(candidate_key)
            if (
                candidate is not None
                and candidate_key[2] == self._turn_serial
                and candidate.task_id == wanted
            ):
                return candidate_key, candidate
        for candidate_key in reversed(self._failure_preview_order):
            candidate = self._failure_previews.get(candidate_key)
            if (
                candidate is not None
                and candidate_key[2] == self._turn_serial
                and candidate.task_id == wanted
            ):
                return candidate_key, candidate
        return None

    def _remember_failure_preview(
        self,
        task_id: str | None,
        turn: int | None,
        cause: str | None,
    ) -> None:
        """Retain a failed tool detail until its terminal failure is known."""
        key = self._context_key(task_id, turn)
        if key in self._failure_blocks:
            return
        block = self._failure_previews.get(key)
        if block is None:
            block = _FailureBlock(
                task_id=key[0],
                turn=key[1],
                cause=cause,
                context=list(self._failure_context.get(key, ())),
            )
            self._failure_previews[key] = block
            self._failure_preview_order.append(key)
            while len(self._failure_preview_order) > _FAILURE_BLOCK_LIMIT:
                expired = self._failure_preview_order.popleft()
                self._failure_previews.pop(expired, None)
        elif self._prefer_failure_cause(block.cause, cause):
            block.cause = cause
        block.context = list(self._failure_context.get(key, block.context))

    def _failure_preview_entries(self) -> tuple[TranscriptEntry, ...]:
        return tuple(
            TranscriptEntry(role="error", text=self._failure_text(block))
            for key in self._failure_preview_order
            if (block := self._failure_previews.get(key)) is not None
        )

    @staticmethod
    def _selected_failure_context(context: list[str]) -> list[str]:
        relevant = [
            line for line in context if "failed" in line.lower() or "timeout" in line.lower()
        ]
        selected = relevant if relevant else context
        return selected[-3:]

    def _failure_text(self, block: _FailureBlock) -> str:
        lines = [
            "turn failed",
            f"task_id={block.task_id}",
            f"cause={block.cause or 'unknown failure'}",
        ]
        lines.extend(
            f"{_FAILURE_CONTEXT_PREFIX}{line}"
            for line in self._selected_failure_context(block.context)
        )
        return "\n".join(lines)

    @staticmethod
    def _prefer_failure_cause(current: str | None, candidate: str | None) -> bool:
        if not candidate or not current or candidate == current:
            return bool(candidate) and not current
        if current.startswith("worker reported "):
            return True
        return candidate.startswith("max_restarts (") and not current.startswith("max_restarts (")

    def _refresh_failure_entry(self, block: _FailureBlock) -> None:
        entry = TranscriptEntry(role="error", text=self._failure_text(block))
        if block.entry is not None:
            for index, current in enumerate(self._entries):
                if current is block.entry:
                    self._entries[index] = entry
                    block.entry = entry
                    return
        self._entries.append(entry)
        block.entry = entry

    def _record_failure(
        self,
        task_id: str | None,
        turn: int | None,
        cause: str | None,
    ) -> None:
        found = self._failure_block_for(task_id, turn)
        if found is None:
            key = self._context_key(task_id, turn)
            block = _FailureBlock(
                task_id=key[0],
                turn=key[1],
                cause=cause,
                context=list(self._failure_context.get(key, ())),
            )
            self._failure_blocks[key] = block
            self._failure_order.append(key)
            while len(self._failure_order) > _FAILURE_BLOCK_LIMIT:
                expired = self._failure_order.popleft()
                self._failure_blocks.pop(expired, None)
                self._failure_context.pop(expired, None)
        else:
            key, block = found
            if key in self._failure_previews:
                self._failure_previews.pop(key, None)
                try:
                    self._failure_preview_order.remove(key)
                except ValueError:
                    pass
                self._failure_blocks[key] = block
                self._failure_order.append(key)
                while len(self._failure_order) > _FAILURE_BLOCK_LIMIT:
                    expired = self._failure_order.popleft()
                    self._failure_blocks.pop(expired, None)
                    self._failure_context.pop(expired, None)
            if self._prefer_failure_cause(block.cause, cause):
                block.cause = cause
            block.context = list(self._failure_context.get(key, block.context))
        self._refresh_failure_entry(block)

    def _remember_failure_context(self, task_id: str | None, turn: int | None, line: str) -> None:
        key = self._context_key(task_id, turn)
        context = self._failure_context.setdefault(key, [])
        if line not in context:
            context.append(line)
        if len(context) > 12:
            del context[:-12]
        found = self._failure_blocks.get(key)
        if found is not None:
            found.context = list(context)
            self._refresh_failure_entry(found)

    def observe_event(self, record: dict[str, Any]) -> None:
        """Promote only operator-relevant runtime events into the transcript."""
        kind = record.get("kind")
        if not isinstance(kind, str):
            return
        data = _event_data(record)
        task_id = _task_id(record, data)
        turn = _event_turn(data)
        self._remember_turn(task_id, turn)

        update = _stream_update(record)
        if update is not None:
            role, text, append, message_id = update
            self._update_stream(
                role,
                text,
                append=append,
                message_id=message_id,
            )

        if kind == "tool_event":
            tool = data.get("tool")
            ok = _tool_status(data)
            duration = _duration_ms(data.get("duration_ms"))
            if ok is False:
                context_line = _failure_context_line(kind, data)
                if context_line is not None:
                    self._remember_failure_context(task_id, turn, context_line)
                if any(_tool_detail_value(data.get(key)) for key in _TOOL_DETAIL_KEYS):
                    self._remember_failure_preview(
                        task_id,
                        turn,
                        _failure_cause(kind, data),
                    )
                return
            if isinstance(tool, str):
                text = _sanitize(_tool_entry_text(data, tool, ok, duration)).strip("\n")
                if text:
                    self._entries.append(
                        TranscriptEntry(
                            role="tool",
                            text=text,
                            tool_name=tool,
                            tool_ok=ok,
                            duration_ms=duration,
                        )
                    )
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

        context_line = _failure_context_line(kind, data)
        if context_line is not None:
            self._remember_failure_context(task_id, turn, context_line)

        failed_result = kind == "result" and data.get("status") in _FAILURE_STATUSES
        if kind in _FAILURE_EVENT_KINDS or failed_result:
            self._record_failure(task_id, turn, _failure_cause(kind, data))
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


def _bounded_render_lines(lines: list[str], limit: int) -> list[str]:
    """Bound rendered detail without changing the transcript's source text."""
    if len(lines) <= limit:
        return lines
    hidden = len(lines) - max(1, limit - 1)
    return [f"… {hidden} lines hidden", *lines[-max(1, limit - 1) :]]


def _entry_lines(
    entry: TranscriptEntry,
    width: int,
    *,
    detail_limit: int | None = None,
) -> list[tuple[str, str]]:
    label = _ROLE_LABELS[entry.role]
    body_width = max(8, width - 3)
    rendered = [(entry.role, f" {label}")]
    if entry.tool_name is not None:
        rendered.extend(
            (entry.role, "   " + line) for line in _wrap_markdown(_tool_line(entry), body_width)
        )
        summary = f"{entry.tool_name}: "
        detail = entry.text
        if detail.startswith(summary):
            detail = detail.split("\n", 1)[1] if "\n" in detail else ""
        if detail:
            detail_lines = _wrap_markdown(detail, body_width)
            if detail_limit is not None:
                detail_lines = _bounded_render_lines(detail_lines, detail_limit)
            rendered.extend((entry.role, "   " + line) for line in detail_lines)
    else:
        lines = _wrap_markdown(entry.text, body_width)
        if detail_limit is not None:
            lines = _bounded_render_lines(lines, detail_limit)
        for line in lines:
            role = (
                "dim"
                if entry.role == "error" and line.lstrip().startswith(_FAILURE_CONTEXT_PREFIX)
                else entry.role
            )
            rendered.append((role, "   " + line))
    rendered.append((entry.role, ""))
    return rendered


def _tool_compact_lines(
    entry: TranscriptEntry,
    width: int,
    *,
    count: int = 1,
    last_duration_ms: int | float | None = None,
) -> list[tuple[str, str]]:
    body_width = max(8, width - 3)
    line = _tool_line(entry, count=count, last_duration_ms=last_duration_ms)
    return [(entry.role, " " + _clip(line, body_width))]


def _transcript_blocks(
    entries: tuple[TranscriptEntry, ...],
    width: int,
    *,
    expanded: bool = False,
) -> list[list[tuple[str, str]]]:
    blocks: list[list[tuple[str, str]]] = []
    index = 0
    while index < len(entries):
        entry = entries[index]
        if entry.role != "tool" or entry.tool_name is None:
            detail_limit = _TOOL_DETAIL_RENDER_LIMIT if entry.role == "error" else None
            blocks.append(_entry_lines(entry, width, detail_limit=detail_limit))
            index += 1
            continue

        if expanded:
            blocks.append(
                _entry_lines(
                    entry,
                    width,
                    detail_limit=_TOOL_DETAIL_RENDER_LIMIT,
                )
            )
            index += 1
            continue

        if entry.tool_ok is not True:
            blocks.append(_entry_lines(entry, width))
            index += 1
            continue

        end = index + 1
        while (
            end < len(entries)
            and entries[end].role == "tool"
            and entries[end].tool_name == entry.tool_name
            and entries[end].tool_ok is True
        ):
            end += 1
        last = entries[end - 1]
        blocks.append(
            _tool_compact_lines(
                entry,
                width,
                count=end - index,
                last_duration_ms=last.duration_ms,
            )
        )
        index = end
    return blocks


def _stream_lines(transcript: Transcript, width: int, capacity: int) -> list[tuple[str, str]]:
    if not transcript.streaming_text or transcript.streaming_role is None:
        return []
    body_width = max(8, width - 3)
    text = transcript.streaming_text
    if len(text) > _STREAM_RENDER_LIMIT:
        text = "…\n" + text[-_STREAM_RENDER_LIMIT:]
    label = _ROLE_LABELS[transcript.streaming_role]
    rendered = [(transcript.streaming_role, f" {label} · generating")]
    rendered.extend(
        (transcript.streaming_role, "   " + line) for line in _wrap_markdown(text, body_width)
    )
    return rendered[-max(1, capacity) :]


def _transcript_lines(transcript: Transcript, width: int, capacity: int) -> list[tuple[str, str]]:
    capacity = max(1, capacity)
    active = _stream_lines(transcript, width, capacity)
    remaining = max(0, capacity - len(active))
    rendered: list[tuple[str, str]] = []
    if remaining:
        history = [
            line
            for block in _transcript_blocks(
                transcript.entries,
                width,
                expanded=transcript.tool_details_expanded,
            )
            for line in block
        ]
        history.extend(
            line
            for entry in transcript._failure_preview_entries()
            for line in _entry_lines(
                entry,
                width,
                detail_limit=_TOOL_DETAIL_RENDER_LIMIT,
            )
        )
        rendered = history[-remaining:]
    rendered.extend(active)
    if not rendered:
        rendered = [("system", " Waiting for a prompt. Type /help for commands.")]
    return rendered[-capacity:]


def _agent_model(agent: Any) -> str:
    provider = getattr(agent, "provider", None)
    model = getattr(agent, "model", None)
    if provider and model:
        return f"{provider}/{model}"
    return model or provider or "?"


def _side_clean(value: Any) -> str:
    """Return one terminal-safe, single-line value for the side panel."""
    return _sanitize(value).replace("\n", " ")


def _side_row(kind: str, text: Any, width: int) -> tuple[str, str]:
    """Build a side-panel row that can never wrap at the panel boundary."""
    return kind, _clip(_side_clean(text), max(1, width))


def _usage_field(line: str, key: str) -> str | None:
    match = re.search(rf"(?<![\w/]){re.escape(key)}=([^\s()]+)", _side_clean(line))
    return match.group(1) if match is not None else None


def _usage_int(value: Any, default: int = 0) -> int:
    if isinstance(value, bool):
        return default
    if isinstance(value, int):
        return max(0, value)
    if isinstance(value, float):
        return max(0, int(value)) if math.isfinite(value) else default
    if value is None:
        return default
    text = _side_clean(value).strip().lower().replace(",", "")
    if not text or text == "free":
        return default if not text else 0
    multiplier = 1
    if text[-1:] in {"k", "m"}:
        multiplier = 1_000 if text[-1] == "k" else 1_000_000
        text = text[:-1]
    try:
        number = float(text)
    except ValueError:
        return default
    return max(0, int(number * multiplier)) if math.isfinite(number) else default


def _usage_float(value: Any, default: float = 0.0) -> float:
    if isinstance(value, bool):
        return default
    if isinstance(value, str):
        value = value.strip().lstrip("$")
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    return number if math.isfinite(number) else default


def _format_cost(value: Any) -> str:
    """Format a cost with four significant digits without a noisy zero cost."""
    cost = _usage_float(value)
    if cost == 0:
        return "free"
    if cost < 0:
        return "free"
    rendered = f"{cost:.4g}"
    if "e" in rendered.lower():
        rendered = format(Decimal(rendered), "f")
        if "." in rendered:
            rendered = rendered.rstrip("0").rstrip(".")
    return f"${rendered}"


def _usage_rows(snapshot: Any, cumulative_line: str, width: int) -> list[tuple[str, str]]:
    """Render cumulative usage as aligned, compact rows."""

    def field(key: str) -> str | None:
        return _usage_field(cumulative_line, key)

    snapshot_calls = _usage_int(getattr(snapshot, "calls", 0))
    snapshot_tokens = _usage_int(getattr(snapshot, "total_tokens", 0))
    snapshot_input = _usage_int(getattr(snapshot, "input_tokens", 0))
    snapshot_output = _usage_int(getattr(snapshot, "output_tokens", 0))
    snapshot_cached = _usage_int(getattr(snapshot, "cached_tokens", 0))
    calls = _usage_int(field("calls"), snapshot_calls)
    total_tokens = _usage_int(field("tokens"), snapshot_tokens)
    input_tokens = _usage_int(field("in"), snapshot_input)
    output_tokens = _usage_int(field("out"), snapshot_output)
    cached_tokens = _usage_int(field("cached"), snapshot_cached)
    rate = _usage_float(
        field("out/s"),
        _usage_float(getattr(snapshot, "output_tokens_per_s", 0.0)),
    )
    cost = _usage_float(
        field("cost"),
        _usage_float(getattr(snapshot, "estimated_cost_usd", 0.0)),
    )

    label_width = 7

    def row(label: str, value: str) -> tuple[str, str]:
        label_column = max(label_width, len(label) + 1)
        return _side_row("normal", f" {label:<{label_column}}{value}", width)

    token_line = f" {'tokens':<{label_width}}{_human_count(total_tokens)}"
    has_details = any(
        field(key) is not None or hasattr(snapshot, snapshot_key)
        for key, snapshot_key in (
            ("in", "input_tokens"),
            ("out", "output_tokens"),
            ("cached", "cached_tokens"),
        )
    )
    detail_rows: list[tuple[str, str]] = []
    if has_details:
        input_value = _human_count(input_tokens)
        output_value = _human_count(output_tokens)
        cached_value = _human_count(cached_tokens)
        details = (
            (f"(in {input_value} · out {output_value} · cached {cached_value})", True),
            (f"(in {input_value}/out {output_value}/cached {cached_value})", True),
            (f"(in {input_value} · out {output_value})", False),
            (f"(in {input_value}/out {output_value})", False),
        )
        selected_detail = False
        for detail, includes_cached in details:
            candidate = f"{token_line} {detail}"
            if len(candidate) <= max(1, width):
                token_line = candidate
                selected_detail = True
                if not includes_cached:
                    detail_rows.append(_side_row("normal", f" cached {cached_value}", width))
                break
        if not selected_detail:
            detail_rows.extend(
                [
                    row("in", input_value),
                    row("out", output_value),
                    _side_row("normal", f" cached {cached_value}", width),
                ]
            )

    cache_rate = None
    if cached_tokens > 0 and input_tokens > 0:
        cache_rate = min(1.0, cached_tokens / input_tokens)

    rows = [
        row("calls", str(calls)),
        _side_row("normal", token_line, width),
        *detail_rows,
        row("out/s", f"{rate:.1f}"),
        row("cost", _format_cost(cost)),
    ]
    if cache_rate is not None:
        rows.append(row("cache-hit", f"{cache_rate:.0%}"))
    return rows


def _agent_rows(agents: tuple[Any, ...], width: int) -> list[tuple[str, str]]:
    """Render agents with stable glyph, task, and provider/model columns."""
    panel_width = max(1, width)
    name_start = 3
    states = [_side_clean(getattr(agent, "state", "?")).strip() or "?" for agent in agents]
    tasks = [_side_clean(getattr(agent, "task_id", "?")).strip() or "?" for agent in agents]
    state_width = min(
        max((len(state) for state in states), default=1),
        max(1, panel_width - name_start - 1),
    )
    task_width = max(
        1,
        min(
            max((len(task) for task in tasks), default=1),
            panel_width - name_start - 1 - state_width,
        ),
    )
    rows: list[tuple[str, str]] = []
    for agent, state, task in zip(agents, states, tasks, strict=True):
        role = "M" if getattr(agent, "role", "") == "main" else "S"
        rows.append(
            _side_row(
                state,
                f" {role} {_pad(task, task_width)} {_pad(state, state_width)}",
                panel_width,
            )
        )
        rows.append(
            _side_row(
                "dim",
                " " * name_start + _agent_model(agent),
                panel_width,
            )
        )

        tokens = _usage_int(getattr(agent, "total_tokens", 0))
        parts = [f"{_human_count(tokens)} tok"]
        rate = getattr(agent, "output_tokens_per_s", None)
        if isinstance(rate, int | float) and math.isfinite(float(rate)):
            parts.append(f"{rate:.1f} out/s")
        tool = _side_clean(getattr(agent, "tool", "")).strip()
        if tool:
            parts.append(tool)
        stats = " " * name_start + parts[0]
        for part in parts[1:]:
            candidate = f"{stats} · {part}"
            if len(candidate) <= panel_width:
                stats = candidate
        rows.append(_side_row("dim", stats, panel_width))
    return rows


def _recent_rows(event: Any, width: int) -> list[tuple[str, str]]:
    """Keep each recent event attached to its detail or omit that detail."""
    panel_width = max(1, width)
    kind = _side_clean(getattr(event, "kind", "event")).strip() or "event"
    detail = _side_clean(getattr(event, "detail", "")).strip()
    if not detail:
        return [_side_row("dim", f" {kind}", panel_width)]

    delimiter = " · "
    kind_width = panel_width - 1 - len(delimiter) - len(detail)
    if kind_width >= 1:
        return [
            _side_row(
                "dim",
                f" {_clip(kind, kind_width)}{delimiter}{detail}",
                panel_width,
            )
        ]

    kind_row = _side_row("dim", f" {kind}", panel_width)
    detail_row = f"   {detail}"
    if len(detail_row) <= panel_width:
        return [kind_row, _side_row("dim", detail_row, panel_width)]
    return [kind_row]


def _append_side_rows(
    lines: list[tuple[str, str]], rows: list[tuple[str, str]], capacity: int
) -> None:
    """Append a row block without leaving a trailing detail fragment."""
    room = max(0, capacity - len(lines))
    if room <= 0:
        return
    if len(rows) <= room:
        lines.extend(rows)
    else:
        lines.append(rows[0])


def _quota_field(window: Any, key: str, default: Any = None) -> Any:
    if isinstance(window, Mapping):
        return window.get(key, default)
    return getattr(window, key, default)


def _quota_db_exists() -> bool:
    configured = os.environ.get("CAMBIUM_QUOTA_DB")
    if configured:
        path = Path(configured).expanduser()
    else:
        state_home = os.environ.get("XDG_STATE_HOME")
        root = Path(state_home).expanduser() if state_home else Path.home() / ".local" / "state"
        path = root / "cambium" / "provider-quota.db"
    try:
        return path.is_file()
    except OSError:
        return False


def _quota_windows(snapshot: Any) -> tuple[Any, ...]:
    windows = getattr(snapshot, "quota_windows", None)
    if windows is None:
        windows = getattr(snapshot, "quota_snapshots", None)
    if windows is None:
        for agent in getattr(snapshot, "agents", ()):
            agent_windows = getattr(agent, "quota_windows", None)
            if agent_windows is not None:
                windows = agent_windows
                break
    if windows is None and _quota_db_exists():
        try:
            windows = QuotaLedger().snapshots()
        except (OSError, ValueError, sqlite3.Error):
            windows = ()
    if windows is None:
        return ()
    if isinstance(windows, Mapping):
        if "provider" in windows and "name" in windows:
            return (windows,)
        windows = windows.values()
    try:
        return tuple(windows)
    except TypeError:
        return ()


def _quota_rows(snapshot: Any, width: int) -> list[tuple[str, str]]:
    """Render known provider windows without clipping quota field labels."""
    panel_width = max(1, width)
    rows: list[tuple[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for window in _quota_windows(snapshot):
        provider = _side_clean(_quota_field(window, "provider", "")).strip()
        name = _side_clean(_quota_field(window, "name", "")).strip()
        if not provider or not name or (provider, name) in seen:
            continue
        seen.add((provider, name))

        token_allowance = _usage_int(
            _quota_field(
                window,
                "allowance_tokens",
                _quota_field(window, "token_allowance", 0),
            )
        )
        request_allowance = _usage_int(
            _quota_field(
                window,
                "allowance_requests",
                _quota_field(window, "request_allowance", 0),
            )
        )
        fields: list[str] = []
        if token_allowance:
            remaining = _quota_field(window, "remaining_tokens")
            if remaining is None:
                used = _usage_int(_quota_field(window, "used_tokens"))
                remaining = max(0, token_allowance - used)
            fields.append(f"{_usage_int(remaining)}/{token_allowance} tokens")
        if request_allowance:
            remaining = _quota_field(window, "remaining_requests")
            if remaining is None:
                used = _usage_int(_quota_field(window, "used_requests"))
                remaining = max(0, request_allowance - used)
            fields.append(f"{_usage_int(remaining)}/{request_allowance} requests")
        if not fields:
            continue

        subject = f"{provider}/{name}"
        full = f" {subject}: {', '.join(fields)}"
        compact_fields = [
            field.replace(" tokens", " tok").replace(" requests", " req")
            for field in fields
        ]
        compact = f" {subject}: {', '.join(compact_fields)}"
        if len(full) <= panel_width:
            rows.append(_side_row("normal", full, panel_width))
        elif len(compact) <= panel_width:
            rows.append(_side_row("normal", compact, panel_width))
        else:
            rows.append(_side_row("normal", f" {subject}", panel_width))
            rows.extend(
                _side_row("dim", f"   {field}", panel_width) for field in fields
            )
    return rows


def _side_sections(
    snapshot: Any, cumulative_line: str, width: int, capacity: int
) -> list[tuple[str, str]]:
    panel_width = max(1, width)
    capacity = max(1, capacity)
    lines: list[tuple[str, str]] = []
    agents = tuple(getattr(snapshot, "agents", ()))
    lines.append(_side_row("heading", " AGENTS", panel_width))
    if not agents:
        lines.append(_side_row("dim", " no agents yet", panel_width))
    else:
        lines.extend(_agent_rows(agents[-6:], panel_width))

    context = getattr(snapshot, "context", None)
    lines.append(_side_row("heading", " CONTEXT", panel_width))
    if context is None:
        lines.append(_side_row("dim", " unavailable", panel_width))
    else:
        approx = "≈" if getattr(context, "approximate", False) else ""
        lines.extend(
            [
                _side_row(
                    "normal",
                    f" epoch {getattr(context, 'epoch', 0)} · "
                    f"segments {getattr(context, 'summary_segments', 0)}",
                    panel_width,
                ),
                _side_row(
                    "normal",
                    " trunk "
                    f"{approx}{_human_count(getattr(context, 'estimated_trunk_tokens', 0))} tok",
                    panel_width,
                ),
                _side_row(
                    "dim",
                    f" {_human_bytes(getattr(context, 'summary_trunk_bytes', 0))} serialized",
                    panel_width,
                ),
                _side_row(
                    "normal",
                    " raw "
                    f"{approx}{_human_count(getattr(context, 'estimated_raw_tail_tokens', 0))} tok",
                    panel_width,
                ),
                _side_row(
                    "dim",
                    " checkpoint "
                    + _side_clean(getattr(context, "checkpoint_ref", None) or "none"),
                    panel_width,
                ),
            ]
        )

    lines.append(_side_row("heading", " SESSION USAGE", panel_width))
    lines.extend(_usage_rows(snapshot, cumulative_line, panel_width))

    lines.append(_side_row("heading", " QUOTA", panel_width))
    quota_rows = _quota_rows(snapshot, panel_width)
    lines.extend(quota_rows or [_side_row("dim", " unavailable", panel_width)])

    recent = tuple(
        event
        for event in getattr(snapshot, "recent_events", ())
        if (
            (kind := _side_clean(getattr(event, "kind", "")).strip()) != "dirty"
            and not kind.startswith("worktree_cleanup")
        )
    )
    if recent:
        lines.append(_side_row("heading", " RECENT", panel_width))
        for event in recent[-4:]:
            _append_side_rows(lines, _recent_rows(event, panel_width), capacity)

    return [_side_row(kind, text, panel_width) for kind, text in lines[:capacity]]


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


def _primary_rows(transcript: Transcript, width: int) -> list[tuple[str, str]]:
    """Return safe, labelled transcript rows for the append-only view."""
    width = max(64, width)
    body_width = max(20, width - 9)
    # The transcript itself is bounded, but a large Markdown entry may occupy
    # many wrapped rows.  Leave enough capacity to render the complete local
    # view so the Cockpit can append only the suffix it has not emitted yet.
    capacity = max(64, len(transcript.entries) * 16 + 64)
    rows = _transcript_lines(transcript, body_width, capacity)
    rendered: list[tuple[str, str]] = []
    for role, text in rows:
        rendered.append((role, _clip(_sanitize(text), width)))
    return rendered


def _primary_status_line(
    snapshot: Any,
    *,
    session_description: str,
    branch_line: str,
    cumulative_line: str,
    width: int,
) -> str:
    """Build the compact status row appended after each changed draw."""
    status = _sanitize(getattr(snapshot, "session_status", "idle"))
    agents = getattr(snapshot, "active_agents", 0)
    tokens = _human_count(getattr(snapshot, "total_tokens", 0))
    rate = getattr(snapshot, "output_tokens_per_s", 0.0)
    epoch = getattr(getattr(snapshot, "context", None), "epoch", 0)
    branch = _sanitize(branch_line)
    provider_model = ""
    match = re.search(r"(provider=\S+\s+model=\S+)", branch)
    if match is not None:
        provider_model = match.group(1)
    checkpoint = ""
    checkpoint_match = re.search(r"(?:last_checkpoint|checkpoint)=(\S+)", session_description)
    if checkpoint_match is not None:
        checkpoint = f"last_checkpoint={_clip(checkpoint_match.group(1), 40)}"
    priority = " · ".join(
        part
        for part in (f"status={status}", checkpoint, provider_model)
        if part
    )
    details = (
        f"{priority} · agents={agents} · tokens={tokens} · out/s={rate:.1f}"
        f" · epoch={epoch} · {_sanitize(cumulative_line)}"
        f" · {branch if not provider_model else branch.replace(provider_model, '').strip(' ·')}"
        f" · {_sanitize(session_description)}"
    )
    line = f"┌ Cambium · {details}"
    # A checkpoint reference is content-addressed and can be much longer than
    # a terminal row.  Keep it intact in native scrollback instead of clipping
    # the durable identity; the terminal will wrap this one logical status row.
    return line if checkpoint else _clip(line, max(64, width))


def render_primary(
    snapshot: Any,
    transcript: Transcript,
    *,
    session_description: str,
    branch_line: str,
    cumulative_line: str,
    width: int,
    color: bool = False,
) -> list[str]:
    """Render an append-only transcript followed by one compact status row."""
    width = max(64, width)
    lines = [
        _paint(text, _ROLE_COLORS.get(role, ""), color)
        for role, text in _primary_rows(transcript, width)
    ]
    lines.append(
        _paint(
            _primary_status_line(
                snapshot,
                session_description=session_description,
                branch_line=branch_line,
                cumulative_line=cumulative_line,
                width=width,
            ),
            _DIM_CYAN,
            color,
        )
    )
    return lines


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

    help_line = (
        " /help commands · v expand tools · <<< multiline >>> · "
        "Ctrl-C cancel frontend · /exit close"
    )
    lines.append("│" + _pad(help_line, inner) + "│")
    input_line = f" {input_label} "
    lines.append("│" + _pad(input_line, inner) + "│")
    lines.append(_paint("└" + "─" * inner + "┘", _CYAN, color))
    return lines[:height]


class Cockpit:
    """Persistent primary-buffer writer for one interactive frontend.

    Unlike a conventional full-screen dashboard, a draw never homes the
    cursor or clears the terminal.  New transcript rows and changed status
    rows are appended to the terminal's normal buffer, which gives the
    operator native terminal scrollback for free.  While readline owns the
    input line, the newest draw is retained and flushed after that read ends;
    asynchronous event output therefore cannot overwrite text being edited.
    """

    def __init__(self, stream: TextIO, *, enabled: bool = True) -> None:
        self.stream = stream
        self.enabled = enabled and _is_tty(stream)
        self.color = self.enabled and _color_enabled(stream)
        self._entered = False
        self._previous_sigterm_handler: Any = None
        self._last_size = os.terminal_size((120, 40))
        self._input_active = False
        self._pending_draw: tuple[Any, Transcript, str, str, str] | None = None
        self._last_primary_rows: tuple[tuple[str, str], ...] = ()
        self._last_status_line = ""

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
        self.flush()
        if self._entered:
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
        """Append changed transcript rows and a compact status row."""
        del input_label
        if not self.enabled:
            return
        self._last_size = shutil.get_terminal_size((120, 40))
        request = (
            snapshot,
            transcript,
            session_description,
            branch_line,
            cumulative_line,
        )
        if self._input_active:
            self._pending_draw = request
            return
        self._draw_now(request)

    def _draw_now(
        self,
        request: tuple[Any, Transcript, str, str, str],
    ) -> None:
        snapshot, transcript, session_description, branch_line, cumulative_line = request
        rows = tuple(_primary_rows(transcript, self._last_size.columns))
        status_line = _primary_status_line(
            snapshot,
            session_description=session_description,
            branch_line=branch_line,
            cumulative_line=cumulative_line,
            width=self._last_size.columns,
        )

        if rows[: len(self._last_primary_rows)] == self._last_primary_rows:
            new_rows = rows[len(self._last_primary_rows) :]
        else:
            # A bounded transcript can evict old rows, and a failure block can
            # replace a previously emitted row.  Keep the append-only contract
            # by marking the refreshed view instead of repainting history.
            new_rows = (("dim", "··· transcript view refreshed ···"), *rows)

        if new_rows or status_line != self._last_status_line:
            for role, text in new_rows:
                self.stream.write(_paint(text, _ROLE_COLORS.get(role, ""), self.color))
                self.stream.write("\n")
            self.stream.write(_paint(status_line, _DIM_CYAN, self.color))
            self.stream.write("\n")
            self.stream.flush()

        self._last_primary_rows = rows
        self._last_status_line = status_line

    def flush(self) -> None:
        """Flush the newest draw once the input line is no longer active."""
        if not self.enabled or self._input_active or self._pending_draw is None:
            return
        request = self._pending_draw
        self._pending_draw = None
        self._draw_now(request)

    def move_to_input(self, *, label: str = "›") -> None:
        if not self.enabled:
            return
        self._input_active = True
        self.stream.write(f"{label} ")
        self.stream.flush()

    def hide_cursor(self, *, commit: bool = False) -> None:
        self._input_active = False
        if self.enabled:
            if commit:
                self.stream.write("\n")
            self.stream.flush()


__all__ = [
    "Cockpit",
    "Transcript",
    "TranscriptEntry",
    "render_primary",
    "render_cockpit",
]
