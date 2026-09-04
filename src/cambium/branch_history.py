"""Read-only projection over existing branch session artifacts.

This module deliberately creates no memory, evidence, or index database.  It
projects the event log and immutable checkpoint files that Cambium already
writes.  Tool-call references include the task branch, worker generation, turn,
and batch index, so a call can be listed globally and then reopened
independently.

The feature has no branch access-control model: every task in the current
session is visible.  Bounds below are resource and response-shape limits, not
permissions.
"""

from __future__ import annotations

import json
import re
import stat
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any
from urllib.parse import quote, unquote

from .store import StoreError, read_events_file

MAX_HISTORY_EVENTS = 100_000
MAX_HISTORY_ROWS = 64
MAX_HISTORY_OUTPUT_BYTES = 32 * 1024
MAX_CHECKPOINT_BYTES = 4 * 1024 * 1024
MAX_MESSAGE_BYTES = 8 * 1024


class HistoryAction(StrEnum):
    """Queries exposed by the branch-history tool."""

    BRANCHES = "branches"
    TOOLS = "tools"
    TOOL = "tool"
    TRANSCRIPT = "transcript"


class BranchHistoryError(ValueError):
    """A branch-history query or referenced artifact is invalid."""


@dataclass(frozen=True, slots=True)
class _Event:
    order: tuple[int, int]
    kind: str
    payload: dict[str, Any]
    task_id: str | None
    session: str = ""


@dataclass(slots=True)
class _Branch:
    task_id: str
    parent_task_id: str | None = None
    status: str = "unknown"
    provider: str | None = None
    context_mode: str | None = None
    placement: str | None = None
    last_turn: int = 0
    tool_count: int = 0


def branch_ref(task_id: str) -> str:
    """Stable printable reference for one task branch."""
    return f"branch:{quote(task_id, safe='')}"


def tool_ref(
    task_id: str, generation: int, turn: int, batch_index: int = 0, *, session: str = ""
) -> str:
    """Identify a call, including its interactive turn when counters can repeat."""
    suffix = f"@{session}" if session else ""
    return f"tool:{quote(task_id, safe='')}:{generation}:{turn}:{batch_index}{suffix}"


def _parse_tool_ref(value: Any) -> tuple[str, int, int, int, str | None]:
    if not isinstance(value, str):
        raise BranchHistoryError("branch_history action=tool requires ref")
    identity, separator, session = value.partition("@")
    if separator and not re.fullmatch(r"turn-[0-9]+", session):
        raise BranchHistoryError("tool ref session must be turn-<number>")
    parts = identity.split(":")
    if len(parts) not in (4, 5) or parts[0] != "tool":
        raise BranchHistoryError("tool ref must be tool:<task>:<generation>:<turn>[:<index>]")
    legacy = len(parts) == 4
    try:
        task_id = unquote(parts[1])
        generation = int(parts[2])
        turn = int(parts[3])
        batch_index = int(parts[4]) if not legacy else 0
    except ValueError:
        raise BranchHistoryError("tool ref generation, turn, and index must be integers") from None
    if not task_id or generation < 0 or turn < 1 or batch_index < 0:
        raise BranchHistoryError("tool ref contains an invalid task, generation, turn, or index")
    return task_id, generation, turn, batch_index, session if separator else None


def _positive_limit(value: Any, default: int = 20) -> int:
    if value is None:
        return default
    if type(value) is not int or value < 1 or value > MAX_HISTORY_ROWS:
        raise BranchHistoryError(f"limit must be between 1 and {MAX_HISTORY_ROWS}")
    return value


def _non_negative_offset(value: Any) -> int:
    if value is None:
        return 0
    if type(value) is not int or value < 0:
        raise BranchHistoryError("offset must be a non-negative integer")
    return value


def _session_event_stores(session_dir: Path) -> tuple[Path, ...]:
    """Return the root/turn event stores that form one visible session."""
    selected = session_dir.expanduser().resolve()
    root = selected.parent if re.fullmatch(r"turn-[0-9]+", selected.name) else selected
    candidates: list[Path] = []

    def add(path: Path) -> None:
        if path not in candidates and path.is_file() and not path.is_symlink():
            candidates.append(path)

    add(root / ".cambium" / "events.db")
    try:
        children = sorted(
            (child for child in root.iterdir() if re.fullmatch(r"turn-[0-9]+", child.name)),
            key=lambda path: int(path.name[5:]),
        )
    except OSError:
        children = []
    for child in children:
        if child.is_dir() and not child.is_symlink():
            add(child / ".cambium" / "events.db")
    return tuple(candidates)


def _event_payload(event: Mapping[str, Any]) -> dict[str, Any]:
    payload = event.get("payload")
    return dict(payload) if isinstance(payload, Mapping) else {}


def _events(session_dir: Path) -> list[_Event]:
    stores = _session_event_stores(session_dir)
    events: list[_Event] = []
    for store_index, path in enumerate(stores):
        try:
            rows = read_events_file(path, max_rows=MAX_HISTORY_EVENTS)
        except StoreError as exc:
            raise BranchHistoryError(f"cannot read branch event store {path}: {exc}") from exc
        for row_index, row in enumerate(rows):
            if not isinstance(row, Mapping):
                continue
            payload = _event_payload(row)
            generation = row.get("generation")
            if type(generation) is int:
                payload["generation"] = generation
            kind = row.get("kind")
            if not isinstance(kind, str) or not kind:
                candidate = payload.get("type")
                kind = candidate if isinstance(candidate, str) else "unknown"
            task_id = payload.get("task_id", row.get("task_id"))
            if not isinstance(task_id, str) or not task_id:
                task_id = None
            seq = row.get("seq")
            sequence = seq if type(seq) is int and seq >= 0 else row_index
            name = path.parent.parent.name
            session = name if re.fullmatch(r"turn-[0-9]+", name) else ""
            events.append(_Event((store_index, sequence), kind, payload, task_id, session))
    events.sort(key=lambda event: event.order)
    return events


def _int(payload: Mapping[str, Any], key: str, default: int = 0) -> int:
    value = payload.get(key)
    return value if type(value) is int and value >= 0 else default


def _branches(events: Sequence[_Event]) -> list[_Branch]:
    branches: dict[str, _Branch] = {}

    def get(task_id: str) -> _Branch:
        return branches.setdefault(task_id, _Branch(task_id))

    for event in events:
        if event.task_id is not None:
            branch = get(event.task_id)
            branch.last_turn = max(branch.last_turn, _int(event.payload, "turn"))
        if event.kind == "child_admitted":
            child = event.payload.get("child_task_id")
            parent = event.payload.get("parent_task_id")
            if isinstance(child, str) and child:
                branch = get(child)
                branch.parent_task_id = parent if isinstance(parent, str) and parent else None
                mode = event.payload.get("context_mode")
                placement = event.payload.get("placement")
                branch.context_mode = mode if isinstance(mode, str) else branch.context_mode
                branch.placement = placement if isinstance(placement, str) else branch.placement
        if event.kind == "tool_event" and event.task_id is not None:
            get(event.task_id).tool_count += 1
        if event.kind == "usage_event" and event.task_id is not None:
            provider = event.payload.get("provider")
            if isinstance(provider, str) and provider:
                get(event.task_id).provider = provider
        if event.kind in {"result", "task_failed", "worker_exit", "worker_terminated"}:
            task_id = event.task_id
            if task_id is None:
                continue
            status = event.payload.get("status")
            if not isinstance(status, str):
                status = "failed" if event.kind == "task_failed" else event.kind
            get(task_id).status = status
    return sorted(
        branches.values(),
        key=lambda branch: (branch.parent_task_id or "", branch.task_id),
    )


def _bounded(text: str, limit: int = MAX_HISTORY_OUTPUT_BYTES) -> str:
    raw = text.encode("utf-8")
    if len(raw) <= limit:
        return text
    marker = "\n... [branch history truncated]"
    keep = max(0, limit - len(marker.encode("utf-8")))
    return raw[:keep].decode("utf-8", errors="ignore") + marker


def _page(lines: Sequence[str], offset: int, limit: int) -> str:
    if not lines:
        return ""
    header = lines[0]
    data = lines[1:]
    selected = list(data[offset : offset + limit])
    suffix = ""
    if offset + len(selected) < len(data):
        suffix = f"\nnext_offset={offset + len(selected)}"
    return _bounded("\n".join((header, *selected)) + suffix)


def _list_branches(events: Sequence[_Event], offset: int, limit: int) -> str:
    rows = _branches(events)
    lines = [f"branches={len(rows)}"]
    for branch in rows:
        lines.append(
            " ".join(
                (
                    branch_ref(branch.task_id),
                    f"parent={branch.parent_task_id or '-'}",
                    f"status={branch.status}",
                    f"provider={branch.provider or '-'}",
                    f"context={branch.context_mode or '-'}",
                    f"placement={branch.placement or '-'}",
                    f"tools={branch.tool_count}",
                    f"turn={branch.last_turn}",
                )
            )
        )
    return _page(lines, offset, limit)


def _tool_events(events: Sequence[_Event], task_id: str | None) -> list[_Event]:
    return [
        event
        for event in events
        if event.kind == "tool_event"
        and event.task_id is not None
        and (task_id is None or event.task_id == task_id)
    ]


def _tool_identity(event: _Event) -> tuple[str, int, int, int]:
    if event.task_id is None:
        raise BranchHistoryError("tool event has no task branch")
    return (
        event.task_id,
        _int(event.payload, "generation"),
        _int(event.payload, "turn"),
        _int(event.payload, "batch_index"),
    )


def _list_tools(events: Sequence[_Event], task_id: str | None, offset: int, limit: int) -> str:
    rows = _tool_events(events, task_id)
    lines = [f"tool_calls={len(rows)}"]
    for event in rows:
        branch, generation, turn, batch_index = _tool_identity(event)
        lines.append(
            " ".join(
                (
                    tool_ref(branch, generation, turn, batch_index, session=event.session),
                    f"branch={branch_ref(branch)}",
                    f"tool={event.payload.get('tool', '-')}",
                    f"ok={str(bool(event.payload.get('ok'))).lower()}",
                    f"duration_ms={_int(event.payload, 'duration_ms')}",
                    f"cmd={event.payload.get('cmd', '-')}",
                )
            )
        )
    return _page(lines, offset, limit)


def _regular_json(path: Path) -> dict[str, Any]:
    try:
        info = path.lstat()
        if not stat.S_ISREG(info.st_mode) or stat.S_ISLNK(info.st_mode):
            raise BranchHistoryError(f"checkpoint is not a regular file: {path}")
        if info.st_size > MAX_CHECKPOINT_BYTES:
            raise BranchHistoryError(f"checkpoint exceeds {MAX_CHECKPOINT_BYTES} bytes")
        value = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise BranchHistoryError(f"checkpoint not found: {path}") from exc
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise BranchHistoryError(f"checkpoint is unreadable: {path}") from exc
    if not isinstance(value, dict):
        raise BranchHistoryError("checkpoint must contain a JSON object")
    return value


def _checkpoint_messages(path: Path) -> list[dict[str, str]]:
    value = _regular_json(path)
    candidates: Any = value.get("transcript")
    if candidates is None:
        content = value.get("content")
        if isinstance(content, Mapping):
            candidates = [
                *(content.get("provider_messages") or []),
                *(content.get("continuation_suffix") or []),
            ]
        else:
            candidates = [
                *(value.get("provider_messages") or []),
                *(value.get("continuation_suffix") or []),
            ]
    if not isinstance(candidates, list):
        raise BranchHistoryError("checkpoint has no transcript")
    messages: list[dict[str, str]] = []
    for message in candidates:
        if not isinstance(message, Mapping):
            continue
        role = message.get("role")
        content = message.get("content")
        if isinstance(role, str) and isinstance(content, str):
            messages.append({"role": role, "content": content})
    return messages


def _checkpoint_event(
    events: Sequence[_Event],
    task_id: str,
    generation: int | None,
    turn: int | None,
    *,
    session: str | None = None,
) -> _Event | None:
    matches: list[_Event] = []
    for event in events:
        if event.kind != "checkpoint" or event.task_id != task_id:
            continue
        if session is not None and event.session != session:
            continue
        if generation is not None and _int(event.payload, "generation") != generation:
            continue
        if turn is not None and _int(event.payload, "turn") != turn:
            continue
        if isinstance(event.payload.get("state_ref"), str):
            matches.append(event)
    return matches[-1] if matches else None


def _extract_tool_exchange(
    messages: Sequence[Mapping[str, str]], tool: str, batch_index: int = 0
) -> tuple[str, str]:
    for index in range(len(messages) - 1, -1, -1):
        message = messages[index]
        if message.get("role") != "assistant":
            continue
        content = message.get("content", "")
        try:
            action = json.loads(content)
        except json.JSONDecodeError:
            continue
        if not isinstance(action, Mapping) or action.get("type") != "tool_call":
            continue
        calls = action.get("calls") if "calls" in action else [action]
        if not isinstance(calls, list) or batch_index >= len(calls):
            continue
        selected_call = calls[batch_index]
        if not isinstance(selected_call, Mapping) or selected_call.get("name") != tool:
            continue
        observation = ""
        observation_count = 0
        for candidate in messages[index + 1 :]:
            if candidate.get("role") == "assistant":
                break
            if candidate.get("role") != "user":
                continue
            candidate_content = candidate.get("content", "")
            if not candidate_content.startswith("tool "):
                continue
            if observation_count == batch_index:
                observation = candidate_content
                break
            observation_count += 1
        return content, observation
    return "", ""


def _read_tool(events: Sequence[_Event], ref: Any) -> str:
    task_id, generation, turn, batch_index, session = _parse_tool_ref(ref)
    matches = [
        event
        for event in _tool_events(events, task_id)
        if _tool_identity(event) == (task_id, generation, turn, batch_index)
        and (session is None or event.session == session)
    ]
    if not matches:
        raise BranchHistoryError(f"tool call not found: {ref}")
    if len({event.session for event in matches}) > 1:
        raise BranchHistoryError(
            "ambiguous tool ref across interactive turns; list tools for scoped refs"
        )
    event = matches[-1]
    tool = event.payload.get("tool")
    tool_name = tool if isinstance(tool, str) else "unknown"
    lines = [
        str(ref),
        f"branch={branch_ref(task_id)} generation={generation} turn={turn} "
        f"batch_index={batch_index}",
        f"tool={tool_name} ok={str(bool(event.payload.get('ok'))).lower()} ",
        f"cmd={event.payload.get('cmd', '-')}",
    ]
    checkpoint = _checkpoint_event(events, task_id, generation, turn, session=event.session)
    if checkpoint is not None:
        state_ref = checkpoint.payload.get("state_ref")
        if isinstance(state_ref, str):
            messages = _checkpoint_messages(Path(state_ref).expanduser().resolve())
            action, observation = _extract_tool_exchange(messages, tool_name, batch_index)
            if action:
                lines.extend(("assistant_action:", _bounded(action, MAX_MESSAGE_BYTES)))
            if observation:
                lines.extend(("tool_observation:", _bounded(observation, MAX_MESSAGE_BYTES)))
    return _bounded("\n".join(lines))


def _latest_transcript(events: Sequence[_Event], task_id: str, offset: int, limit: int) -> str:
    checkpoint = _checkpoint_event(events, task_id, None, None)
    if checkpoint is None:
        raise BranchHistoryError(f"branch has no retrievable checkpoint: {task_id}")
    state_ref = checkpoint.payload.get("state_ref")
    if not isinstance(state_ref, str):
        raise BranchHistoryError(f"branch checkpoint has no state_ref: {task_id}")
    messages = _checkpoint_messages(Path(state_ref).expanduser().resolve())
    lines = [f"branch={branch_ref(task_id)} messages={len(messages)}"]
    for index, message in enumerate(messages):
        content = _bounded(message["content"], MAX_MESSAGE_BYTES)
        lines.append(f"[{index}] {message['role']}\n{content}")
    return _page(lines, offset, limit)


def query_branch_history(session_dir: Path | str, arguments: Mapping[str, Any]) -> str:
    """Execute one bounded branch-history query against existing artifacts."""
    action_value = arguments.get("action")
    if not isinstance(action_value, str):
        choices = ", ".join(action.value for action in HistoryAction)
        raise BranchHistoryError(f"action must be one of: {choices}")
    try:
        action = HistoryAction(action_value)
    except (TypeError, ValueError):
        choices = ", ".join(action.value for action in HistoryAction)
        raise BranchHistoryError(f"action must be one of: {choices}") from None
    root = Path(session_dir).expanduser().resolve()
    events = _events(root)
    offset = _non_negative_offset(arguments.get("offset"))
    limit = _positive_limit(arguments.get("limit"))
    task_id = arguments.get("task_id")
    if task_id is not None and (not isinstance(task_id, str) or not task_id):
        raise BranchHistoryError("task_id must be a non-empty string")

    if action is HistoryAction.BRANCHES:
        return _list_branches(events, offset, limit)
    if action is HistoryAction.TOOLS:
        return _list_tools(events, task_id, offset, limit)
    if action is HistoryAction.TOOL:
        return _read_tool(events, arguments.get("ref"))
    if task_id is None:
        raise BranchHistoryError("branch_history action=transcript requires task_id")
    return _latest_transcript(events, task_id, offset, limit)


__all__ = [
    "BranchHistoryError",
    "HistoryAction",
    "branch_ref",
    "query_branch_history",
    "tool_ref",
]
