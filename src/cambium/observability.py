"""Event-sourced operator view of a Cambium session.

The reducer consumes the durable supervisor event stream and derives a compact
read model for terminal frontends.  It owns no runtime state and never reaches
into live workers: replaying the same ordered events produces the same agent
and usage view.  Optional checkpoint inspection adds context-trunk byte counts;
provider-reported token counts remain the authority for exact prompt usage.
"""

from __future__ import annotations

import json
import math
from collections import deque
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .summary_trunk import SUMMARY_ENTRY_OPEN

_TERMINAL_STATES = frozenset({"succeeded", "failed", "cancelled", "exited"})
_ACTIVE_STATES = frozenset({"starting", "active", "merging"})
_STATE_PRIORITY = {
    "queued": 0,
    "starting": 1,
    "active": 2,
    "merging": 3,
    "exited": 4,
    "cancelled": 5,
    "failed": 6,
    "succeeded": 7,
}
_CONTEXT_EVENT_KINDS = frozenset(
    {
        "context_checkpoint",
        "context_epoch_advanced",
        "context_resume",
        "context_fork",
    }
)


@dataclass(frozen=True, slots=True)
class AgentSnapshot:
    """One task/agent row in the operator read model."""

    task_id: str
    parent_task_id: str | None
    role: str
    state: str
    generation: int
    turn: int
    epoch: int
    provider: str | None
    model: str | None
    tool: str | None
    calls: int
    summary_calls: int
    input_tokens: int
    output_tokens: int
    cached_tokens: int
    total_tokens: int
    output_tokens_per_s: float | None
    estimated_cost_usd: float
    last_seq: int
    last_kind: str | None


@dataclass(frozen=True, slots=True)
class ContextSnapshot:
    """Latest known prompt/trunk shape for the selected root context."""

    task_id: str | None
    checkpoint_ref: str | None
    epoch: int
    exact_prompt_tokens: int | None
    active_context_bytes: int
    active_context_messages: int
    stable_head_bytes: int
    summary_trunk_bytes: int
    summary_segments: int
    raw_tail_bytes: int
    estimated_trunk_tokens: int
    estimated_raw_tail_tokens: int
    approximate: bool


@dataclass(frozen=True, slots=True)
class RecentEvent:
    seq: int
    kind: str
    task_id: str | None
    detail: str


@dataclass(frozen=True, slots=True)
class SessionSnapshot:
    """Immutable dashboard state derived from one ordered event prefix."""

    session_status: str
    agents: tuple[AgentSnapshot, ...]
    active_agents: int
    queued_agents: int
    succeeded_agents: int
    failed_agents: int
    calls: int
    summary_calls: int
    input_tokens: int
    output_tokens: int
    cached_tokens: int
    total_tokens: int
    output_tokens_per_s: float
    estimated_cost_usd: float
    elapsed_s: float | None
    context: ContextSnapshot
    recent_events: tuple[RecentEvent, ...]
    last_seq: int


@dataclass(slots=True)
class _Agent:
    task_id: str
    parent_task_id: str | None = None
    state: str = "queued"
    generation: int = 0
    turn: int = 0
    epoch: int = 0
    provider: str | None = None
    model: str | None = None
    tool: str | None = None
    calls: int = 0
    summary_calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cached_tokens: int = 0
    total_tokens: int = 0
    output_tokens_per_s: float | None = None
    estimated_cost_usd: float = 0.0
    last_seq: int = 0
    last_kind: str | None = None
    checkpoint_ref: str | None = None
    exact_prompt_tokens: int | None = None
    active_context_bytes: int = 0
    active_context_messages: int = 0
    summary_trunk_bytes: int = 0
    summary_segments: int = 0
    raw_tail_bytes: int = 0


def _payload(event: Mapping[str, Any]) -> Mapping[str, Any]:
    value = event.get("payload")
    return value if isinstance(value, Mapping) else {}


def _finite_non_negative(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    try:
        number = float(value)
    except (OverflowError, ValueError):
        return None
    if not math.isfinite(number) or number < 0:
        return None
    return number


def _count(value: Any) -> int:
    number = _finite_non_negative(value)
    return 0 if number is None else int(number)


def _usage_counts(payload: Mapping[str, Any]) -> tuple[int, int, int, int]:
    usage = payload.get("usage")
    if not isinstance(usage, Mapping):
        return 0, 0, 0, 0

    input_tokens = 0
    for key in ("input_tokens", "prompt_tokens"):
        if key in usage:
            input_tokens = _count(usage.get(key))
            break

    output_tokens = 0
    for key in ("output_tokens", "completion_tokens"):
        if key in usage:
            output_tokens = _count(usage.get(key))
            break

    cached_tokens = 0
    for details_key in ("input_tokens_details", "prompt_tokens_details"):
        details = usage.get(details_key)
        if isinstance(details, Mapping) and "cached_tokens" in details:
            cached_tokens = _count(details.get("cached_tokens"))
            break
    else:
        for key in ("cache_read_input_tokens", "cached_tokens"):
            if key in usage:
                cached_tokens = _count(usage.get(key))
                break

    total_tokens = (
        _count(usage.get("total_tokens"))
        if "total_tokens" in usage
        else input_tokens + output_tokens
    )
    return input_tokens, output_tokens, cached_tokens, total_tokens


def _event_seq(event: Mapping[str, Any], fallback: int) -> int:
    value = event.get("seq")
    return value if type(value) is int and value > 0 else fallback


def _event_time(event: Mapping[str, Any]) -> float | None:
    value = event.get("monotonic_ms")
    number = _finite_non_negative(value)
    if number is not None:
        return number / 1000.0
    value = event.get("ts")
    if isinstance(value, str):
        try:
            value = float(value)
        except ValueError:
            return None
    return _finite_non_negative(value)


def _string(value: Any) -> str | None:
    return value if isinstance(value, str) and value else None


def _event_detail(kind: str, payload: Mapping[str, Any]) -> str:
    for key in (
        "message",
        "reason",
        "failure_reason",
        "status",
        "tool",
        "provider",
        "model",
        "assigned_provider",
    ):
        value = payload.get(key)
        if isinstance(value, str) and value:
            return value.replace("\n", " ")[:160]
    if kind == "usage_event":
        _, output_tokens, _, total_tokens = _usage_counts(payload)
        return f"tokens={total_tokens} out={output_tokens}"
    return ""


def _set_state(agent: _Agent, state: str) -> None:
    if agent.state in _TERMINAL_STATES:
        return
    if state in _TERMINAL_STATES:
        agent.state = state
        return
    if _STATE_PRIORITY.get(state, 0) >= _STATE_PRIORITY.get(agent.state, 0):
        agent.state = state


def _message_bytes(messages: Sequence[Mapping[str, Any]]) -> int:
    try:
        return len(
            json.dumps(
                list(messages),
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=False,
                allow_nan=False,
            ).encode("utf-8")
        )
    except (TypeError, ValueError, UnicodeEncodeError):
        return 0


def _checkpoint_path(session_dir: Path, checkpoint_ref: str) -> Path | None:
    relative = Path(checkpoint_ref)
    if relative.is_absolute() or ".." in relative.parts:
        return None
    session_root = session_dir.resolve()
    state_root = (session_root / ".cambium").resolve()
    try:
        state_root.relative_to(session_root)
    except ValueError:
        return None
    roots = (
        session_root / ".cambium" / "checkpoints",
        session_root / ".cambium" / "context",
        session_root / ".cambium" / "epochs",
        session_root / ".cambium",
        session_root,
    )
    for root in roots:
        try:
            candidate = (root / relative).resolve()
            candidate.relative_to(session_root)
        except (OSError, RuntimeError, ValueError):
            continue
        if candidate.is_file():
            return candidate
    if state_root.is_dir():
        matches = tuple(state_root.rglob(relative.name))
        for candidate in matches:
            try:
                candidate = candidate.resolve()
                candidate.relative_to(state_root)
            except (OSError, RuntimeError, ValueError):
                continue
            if candidate.is_file():
                return candidate
    return None


def _read_checkpoint_context(
    session_dir: Path | None,
    checkpoint_ref: str | None,
) -> tuple[int, int, int, int, int] | None:
    """Return head, summary, segments, raw-tail, message-count byte metrics."""

    if session_dir is None or checkpoint_ref is None:
        return None
    path = _checkpoint_path(session_dir, checkpoint_ref)
    if path is None:
        return None
    try:
        raw = path.read_bytes()
        if len(raw) > 8 * 1024 * 1024:
            return None
        document = json.loads(raw)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return None
    if not isinstance(document, Mapping):
        return None
    provider_messages = document.get("provider_messages")
    continuation = document.get("continuation_suffix")
    if not isinstance(provider_messages, list) or not isinstance(continuation, list):
        return None
    messages = [item for item in provider_messages if isinstance(item, Mapping)]
    suffix = [item for item in continuation if isinstance(item, Mapping)]
    if len(messages) != len(provider_messages) or len(suffix) != len(continuation):
        return None

    stable_head = messages[:2]
    summary_messages: list[Mapping[str, Any]] = []
    index = 2
    while index < len(messages):
        content = messages[index].get("content")
        if not isinstance(content, str) or not content.startswith(SUMMARY_ENTRY_OPEN):
            break
        summary_messages.append(messages[index])
        index += 1
    raw_tail = [*messages[index:], *suffix]
    return (
        _message_bytes(stable_head),
        _message_bytes([*stable_head, *summary_messages]),
        len(summary_messages),
        _message_bytes(raw_tail),
        len(messages) + len(suffix),
    )


class ObservabilityState:
    """Incremental fold of durable events into an operator read model."""

    def __init__(self, *, recent_limit: int = 12) -> None:
        self._agents: dict[str, _Agent] = {}
        self._order: list[str] = []
        self._recent: deque[RecentEvent] = deque(maxlen=max(1, recent_limit))
        self._last_seq = 0
        self._synthetic_seq = 0
        self._first_time: float | None = None
        self._last_time: float | None = None
        self._session_status = "idle"

    @property
    def last_seq(self) -> int:
        return self._last_seq

    def _ensure_agent(self, task_id: str) -> _Agent:
        agent = self._agents.get(task_id)
        if agent is None:
            agent = _Agent(task_id=task_id)
            self._agents[task_id] = agent
            self._order.append(task_id)
        return agent

    def apply(self, event: Mapping[str, Any]) -> None:
        if not isinstance(event, Mapping):
            return
        self._synthetic_seq += 1
        seq = _event_seq(event, self._synthetic_seq)
        if type(event.get("seq")) is int and seq <= self._last_seq:
            return
        self._last_seq = max(self._last_seq, seq)
        kind = _string(event.get("kind")) or "event"
        payload = _payload(event)
        task_id = _string(event.get("task_id"))
        generation = event.get("generation")
        timestamp = _event_time(event)
        if timestamp is not None:
            self._first_time = timestamp if self._first_time is None else self._first_time
            self._last_time = timestamp

        parent_id = _string(payload.get("parent_task_id"))
        child_id = _string(payload.get("child_task_id"))
        if child_id is not None:
            child = self._ensure_agent(child_id)
            if parent_id is not None:
                child.parent_task_id = parent_id
        if task_id is not None:
            agent = self._ensure_agent(task_id)
            if parent_id is not None and task_id != parent_id:
                agent.parent_task_id = parent_id
            if type(generation) is int and generation >= 0:
                agent.generation = max(agent.generation, generation)
            agent.last_seq = seq
            agent.last_kind = kind

            if kind in {"task_assigned", "child_admitted"}:
                _set_state(agent, "queued")
            elif kind == "spawned":
                _set_state(agent, "starting")
            elif kind in {
                "ready",
                "run_task",
                "heartbeat",
                "tool_event",
                "checkpoint",
                "usage_event",
                "context_checkpoint",
                "context_epoch_advanced",
                "context_resume",
                "context_fork",
            }:
                _set_state(agent, "active")
            elif kind in {"merge_progress", "merge_started"}:
                _set_state(agent, "merging")
            elif kind in {"worker_failed", "task_failed", "context_resume_failed"}:
                _set_state(agent, "failed")
            elif kind == "result":
                status = _string(payload.get("status"))
                if status in {"succeeded", "failed", "cancelled"}:
                    _set_state(agent, status)
            elif kind == "exit":
                _set_state(agent, "exited")
            elif kind == "reuse_ready" and agent.state not in _TERMINAL_STATES:
                _set_state(agent, "succeeded")
            elif kind in {"cancelled", "task_cancelled"}:
                _set_state(agent, "cancelled")

            turn = payload.get("turn")
            if type(turn) is int and turn >= 0:
                agent.turn = max(agent.turn, turn)
            epoch = payload.get("epoch")
            if type(epoch) is int and epoch >= 0:
                agent.epoch = max(agent.epoch, epoch)

            provider = _string(payload.get("provider")) or _string(payload.get("assigned_provider"))
            model = _string(payload.get("model"))
            if provider is not None:
                agent.provider = provider
            if model is not None:
                agent.model = model
            tool = _string(payload.get("tool"))
            if tool is not None:
                agent.tool = tool

            if kind == "usage_event":
                (
                    input_tokens,
                    output_tokens,
                    cached_tokens,
                    total_tokens,
                ) = _usage_counts(payload)
                agent.calls += 1
                if payload.get("call_kind") == "summary":
                    agent.summary_calls += 1
                agent.input_tokens += input_tokens
                agent.output_tokens += output_tokens
                agent.cached_tokens += cached_tokens
                agent.total_tokens += total_tokens
                latency = _finite_non_negative(payload.get("latency_s"))
                if latency is not None and latency > 0:
                    agent.output_tokens_per_s = output_tokens / latency
                cost = _finite_non_negative(payload.get("estimated_cost_usd"))
                if cost is not None:
                    agent.estimated_cost_usd += cost
                if input_tokens > 0:
                    agent.exact_prompt_tokens = input_tokens
                for field in (
                    "active_context_bytes",
                    "active_context_messages",
                    "summary_trunk_bytes",
                    "summary_segments",
                    "raw_tail_bytes",
                ):
                    value = payload.get(field)
                    if type(value) is int and value >= 0:
                        setattr(agent, field, value)

            if kind in _CONTEXT_EVENT_KINDS:
                checkpoint_ref = _string(payload.get("checkpoint_ref"))
                if checkpoint_ref is not None:
                    agent.checkpoint_ref = checkpoint_ref
                cache_key = payload.get("cache_key")
                if isinstance(cache_key, Mapping):
                    prefix_bytes = cache_key.get("prefix_bytes")
                    if type(prefix_bytes) is int and prefix_bytes >= 0:
                        agent.active_context_bytes = max(agent.active_context_bytes, prefix_bytes)
                    message_count = cache_key.get("message_count")
                    if type(message_count) is int and message_count >= 0:
                        agent.active_context_messages = max(
                            agent.active_context_messages, message_count
                        )

        if kind in {"session_started", "session_resumed"}:
            self._session_status = "running"
        elif kind == "session_ended":
            self._session_status = _string(payload.get("session_status")) or "ended"
        elif kind in {"session_cancelled", "shutdown"}:
            self._session_status = "cancelled"
        elif self._session_status == "idle" and task_id is not None:
            self._session_status = "running"

        self._recent.append(
            RecentEvent(
                seq=seq,
                kind=kind,
                task_id=task_id,
                detail=_event_detail(kind, payload),
            )
        )

    def extend(self, events: Sequence[Mapping[str, Any]]) -> None:
        for event in events:
            self.apply(event)

    def _root_task_id(self) -> str | None:
        for task_id in self._order:
            if self._agents[task_id].parent_task_id is None:
                return task_id
        return self._order[0] if self._order else None

    def snapshot(self, *, session_dir: str | Path | None = None) -> SessionSnapshot:
        root_task_id = self._root_task_id()
        snapshots: list[AgentSnapshot] = []
        for task_id in self._order:
            agent = self._agents[task_id]
            role = "main" if task_id == root_task_id else "sub"
            snapshots.append(
                AgentSnapshot(
                    task_id=agent.task_id,
                    parent_task_id=agent.parent_task_id,
                    role=role,
                    state=agent.state,
                    generation=agent.generation,
                    turn=agent.turn,
                    epoch=agent.epoch,
                    provider=agent.provider,
                    model=agent.model,
                    tool=agent.tool,
                    calls=agent.calls,
                    summary_calls=agent.summary_calls,
                    input_tokens=agent.input_tokens,
                    output_tokens=agent.output_tokens,
                    cached_tokens=agent.cached_tokens,
                    total_tokens=agent.total_tokens,
                    output_tokens_per_s=agent.output_tokens_per_s,
                    estimated_cost_usd=round(agent.estimated_cost_usd, 6),
                    last_seq=agent.last_seq,
                    last_kind=agent.last_kind,
                )
            )

        root = self._agents.get(root_task_id) if root_task_id is not None else None
        context_agent = root
        if context_agent is None or context_agent.checkpoint_ref is None:
            context_agent = next(
                (
                    self._agents[task_id]
                    for task_id in reversed(self._order)
                    if self._agents[task_id].checkpoint_ref is not None
                ),
                context_agent,
            )

        checkpoint_metrics = _read_checkpoint_context(
            Path(session_dir).expanduser().resolve() if session_dir is not None else None,
            context_agent.checkpoint_ref if context_agent is not None else None,
        )
        stable_head_bytes = 0
        summary_trunk_bytes = context_agent.summary_trunk_bytes if context_agent is not None else 0
        summary_segments = context_agent.summary_segments if context_agent is not None else 0
        raw_tail_bytes = context_agent.raw_tail_bytes if context_agent is not None else 0
        active_context_messages = (
            context_agent.active_context_messages if context_agent is not None else 0
        )
        approximate = True
        if checkpoint_metrics is not None:
            (
                stable_head_bytes,
                checkpoint_trunk_bytes,
                checkpoint_segments,
                checkpoint_raw_bytes,
                checkpoint_messages,
            ) = checkpoint_metrics
            summary_trunk_bytes = max(summary_trunk_bytes, checkpoint_trunk_bytes)
            summary_segments = max(summary_segments, checkpoint_segments)
            raw_tail_bytes = max(raw_tail_bytes, checkpoint_raw_bytes)
            active_context_messages = max(active_context_messages, checkpoint_messages)
        active_context_bytes = (
            context_agent.active_context_bytes if context_agent is not None else 0
        )
        active_context_bytes = max(active_context_bytes, summary_trunk_bytes + raw_tail_bytes)
        context = ContextSnapshot(
            task_id=context_agent.task_id if context_agent is not None else None,
            checkpoint_ref=(context_agent.checkpoint_ref if context_agent is not None else None),
            epoch=context_agent.epoch if context_agent is not None else 0,
            exact_prompt_tokens=(
                context_agent.exact_prompt_tokens if context_agent is not None else None
            ),
            active_context_bytes=active_context_bytes,
            active_context_messages=active_context_messages,
            stable_head_bytes=stable_head_bytes,
            summary_trunk_bytes=summary_trunk_bytes,
            summary_segments=summary_segments,
            raw_tail_bytes=raw_tail_bytes,
            estimated_trunk_tokens=round(summary_trunk_bytes / 4),
            estimated_raw_tail_tokens=round(raw_tail_bytes / 4),
            approximate=approximate,
        )

        calls = sum(agent.calls for agent in self._agents.values())
        summary_calls = sum(agent.summary_calls for agent in self._agents.values())
        input_tokens = sum(agent.input_tokens for agent in self._agents.values())
        output_tokens = sum(agent.output_tokens for agent in self._agents.values())
        cached_tokens = sum(agent.cached_tokens for agent in self._agents.values())
        total_tokens = sum(agent.total_tokens for agent in self._agents.values())
        cost = sum(agent.estimated_cost_usd for agent in self._agents.values())
        rates = [
            agent.output_tokens_per_s
            for agent in self._agents.values()
            if agent.output_tokens_per_s is not None and agent.state in _ACTIVE_STATES
        ]
        elapsed_s = (
            max(0.0, self._last_time - self._first_time)
            if self._first_time is not None and self._last_time is not None
            else None
        )
        return SessionSnapshot(
            session_status=self._session_status,
            agents=tuple(snapshots),
            active_agents=sum(snapshot.state in _ACTIVE_STATES for snapshot in snapshots),
            queued_agents=sum(snapshot.state == "queued" for snapshot in snapshots),
            succeeded_agents=sum(snapshot.state == "succeeded" for snapshot in snapshots),
            failed_agents=sum(snapshot.state in {"failed", "cancelled"} for snapshot in snapshots),
            calls=calls,
            summary_calls=summary_calls,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cached_tokens=cached_tokens,
            total_tokens=total_tokens,
            output_tokens_per_s=sum(rates),
            estimated_cost_usd=round(cost, 6),
            elapsed_s=elapsed_s,
            context=context,
            recent_events=tuple(self._recent),
            last_seq=self._last_seq,
        )


def snapshot_from_events(
    events: Sequence[Mapping[str, Any]],
    *,
    session_dir: str | Path | None = None,
    recent_limit: int = 12,
) -> SessionSnapshot:
    state = ObservabilityState(recent_limit=recent_limit)
    state.extend(events)
    return state.snapshot(session_dir=session_dir)


__all__ = [
    "AgentSnapshot",
    "ContextSnapshot",
    "ObservabilityState",
    "RecentEvent",
    "SessionSnapshot",
    "snapshot_from_events",
]
