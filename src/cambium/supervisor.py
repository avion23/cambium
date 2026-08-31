"""Cambium supervisor — the canonical asyncio runtime.

Speaks the Nuntius JSON-Lines wire protocol (docs/architecture.md §5) with N
worker subprocesses under one ``asyncio.TaskGroup``: spawn ``python -m
cambium.worker`` (or a task's ``worker`` script) inside a git worktree,
correlate ``init`` -> ``ready`` -> ``run_task`` -> ``result_envelope`` ->
``exit_message`` by request_id, and publish a changed worker branch onto
``refs/heads/main`` atomically through ``cambium.merge.MergeSequencer``. A
successful clean worker already at the resolved base is a no-op; otherwise a
successful worker must merge. There is no pre-merge gate.

Every event is persisted to ``<session_dir>/.cambium/events.db`` through the
canonical ``cambium.store.EventStore`` (readable via ``read_events``).
Redaction is session-scoped: one ``cambium.redact.Redactor`` built from every
worker-forwardable declared secret value redacts the complete event record
before the store, the non-critical queue, and event observers.

Workers additionally emit one redacted ``usage_event`` per provider call
(implementation plan step 3); the supervisor validates the field allowlist
(``_invalid_usage_event_fields``) and persists the record through the same
EventStore path, so provider usage/quota evidence is durable without ever
exposing credentials.

``run_plan`` drives a multi-task plan and returns a ``PlanResult``;
``run_session`` is a one-task adapter that keeps the historical
``SliceResult`` return shape. ``cambium.store`` and ``cambium.merge`` are
hard runtime dependency contracts: import failure fails at load.

Dynamic child admission (implementation-plan step 2): a worker may propose a
child task with the ``propose_child`` wire message ({request_id,
parent_task_id, child_task_id, kind, spec}), correlated by request_id like
the other wire messages. The supervisor validates each revision against the
session task tree — ``tasktree.build_tree`` over the accumulated tasks list,
root = the single plan root. A duplicate, cyclic, multi-parent, over-depth,
or over-width revision is durably rejected with a ``child_rejected`` event
(reason + spawn nothing); a valid revision is durably recorded as
``child_admitted`` through the existing EventStore path (redacted) and then
spawned as a new session task through the same ``supervise_task`` machinery,
with context limited to its own spec plus the parent's strict-key envelope
(the ``tasktree.upward_result`` key set). The child's upward envelope uses
that same strict key set and reaches only its parent. Top-level flat fan-out
is unchanged; plan tasks without an explicit ``kind`` default to the session
kind for tree validation only.

Decision port and conversation persistence (implementation-plan step 2,
items 23-24): ``run_plan`` accepts an optional ``architectus`` decision core
(an ``ArchitectusCore`` or a small adapter port exposing ``aggregate``/``step``)
and an optional ``conversations`` flag that opens ``ConversationStore`` at
``<session_dir>/.cambium/conversations.db`` with the same session lifecycle
as the event store. When the port is configured it is the ONLY provider-side
channel whose response can become a child proposal: each admitted parent's
terminal envelope is fed to ``core.aggregate``/``core.step`` and the resulting
typed proposals are routed through the existing ``_admit_child`` validation —
a provider response never mutates the live session tree directly. Every
admitted/rejected revision is also appended to the conversation store (one
row per revision, ``node_id`` = child task id, parent task in ``meta``); a
store open failure raises and a store append failure is never silent. With
neither backend configured, ``run_plan`` is byte-for-byte the historical
behavior.
"""

from __future__ import annotations

import argparse
import asyncio
import copy
import fcntl
import hashlib
import importlib.util
import json
import math
import os
import random
import re
import shutil
import signal
import sqlite3
import subprocess
import sys
import tempfile
import time
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from typing import Any, cast

from cambium.child_policy import ChildPolicyError, parse_child_policy
from cambium.fencing import (
    is_cache_artifact_path,
    next_generation,
    process_is_alive,
    read_generation,
    write_generation,
)
from cambium.process_env import build_subprocess_env
from cambium.provider_config import (
    DEFAULT_PROVIDER_PATH,
    AuthMode,
    load_providers,
)
from cambium.system_health import can_run_heavy

from .architectus import ActionKind, ArchitectusCore
from .auth import MIN_API_KEY_BYTES, AuthStore, oauth_env_suffix, scrub_environment
from .conversations import ConversationStore, ConversationStoreError
from .ipc import MAX_LINE_BYTES, encode_message, write_frame
from .merge import MergeConflictError, MergeSequencer
from .oauth import (
    DEFAULT_REFRESH_MARGIN_S,
    OAuthError,
    OAuthMissingError,
    OAuthStore,
    TokenManager,
)
from .redact import (
    EVENT_RECORD_STRUCTURAL_FIELDS,
    WORKER_RESULT_STRUCTURAL_FIELDS,
    Redactor,
    build_session_redactor,
)
from .results import EXIT_CODES, Result, write_result
from .routing import (
    DebtStore,
    LaneCapacityExhausted,
    LaneState,
    ProviderDebt,
    resolve_assignment,
    validate_requirements,
)
from .store import (
    CRITICAL_KINDS,
    EventStore,
    StoreError,
    count_events_file,
    read_events_file,
)
from .tasktree import (
    _ENVELOPE_KEYS,
    MAX_WIDTH,
    TaskKind,
    TaskTree,
    TaskTreeError,
    build_tree,
    ready_tasks,
    topological_order,
)
from .terminal import sanitize_terminal_text
from .worker import (
    _SHA256_HEX_RE,
    MAX_ENVELOPE_FIELD_CHARS,
    MAX_ENVELOPE_ITEMS,
    _cap_utf8,
    _safe_task_id,
    _validate_checkpoint_ref_shape,
    _validate_epoch_checkpoint_data,
    _validate_provider_boundary,
    _workspace_hash,
)
from .worker import (
    _fork_cache_compatible as _worker_fork_cache_compatible,
)

PROTO = 1
WORKER_STDIN_LIMIT = MAX_LINE_BYTES
# Four full-cap messages bound each worker's decoded stdout backlog.
WORKER_STDOUT_QUEUE_MAXSIZE = max(1, MAX_LINE_BYTES // (256 * 1024))
OUTBOUND_MESSAGE_TOO_LONG = "outbound_message_too_long"
STDIN_WRITE_TIMEOUT_S = 5.0
PONG_DEADLINE_S = 10.0
DURABLE_EVENT_TIMEOUT_S = 5.0

# Index status pairs that porcelain v1 reports for unmerged (conflicted) paths.
# Kept local to the supervisor because resolver staging uses a normal merge
# worktree rather than the merge sequencer's rebase worktree.
_RESOLVER_UNMERGED_PAIRS = frozenset({"DD", "AU", "UD", "UA", "DU", "AA", "UU"})
_HEARTBEAT_PHASES = frozenset({"waiting", "thinking", "streaming"})

EventSink = Callable[[dict[str, Any]], None | Awaitable[None]]

# Session-tree kind for plan tasks that do not declare one. ``build_tree``
# requires a kind per node; flat run_plan specs are code-editing tasks, so
# FEATURE is the schema default. The proposal's own ``kind`` is always used
# for admitted children.
_DEFAULT_SESSION_KIND = TaskKind.FEATURE.value


def make_request_id(seq: int) -> str:
    """Monotonic-ish request id. Not a ULID (no deps in the slice)."""
    return f"{time.time_ns():x}-{seq:04x}"


def _stdin_write_timeout_s() -> float:
    """Return the bounded stdin-drain budget without inheriting child env."""
    value = os.environ.get("CAMBIUM_WRITE_TIMEOUT_S")
    if value is None:
        return STDIN_WRITE_TIMEOUT_S
    try:
        timeout = float(value)
    except ValueError:
        return STDIN_WRITE_TIMEOUT_S
    return max(0.0, timeout)


def _durable_event_timeout_s() -> float:
    """Return the bounded wait for a durable terminal emit (env-configurable)."""
    value = os.environ.get("CAMBIUM_DURABLE_EVENT_TIMEOUT_S")
    if value is None:
        return DURABLE_EVENT_TIMEOUT_S
    try:
        timeout = float(value)
    except ValueError:
        return DURABLE_EVENT_TIMEOUT_S
    return timeout if timeout > 0 else DURABLE_EVENT_TIMEOUT_S


def _invalid_propose_child_fields(msg: dict[str, Any]) -> list[str]:
    """Return propose_child fields whose values cannot be admitted."""
    invalid: list[str] = []
    for field in ("request_id", "parent_task_id", "child_task_id", "kind"):
        value = msg.get(field)
        if not isinstance(value, str) or not value:
            invalid.append(field)
    if not isinstance(msg.get("spec"), dict):
        invalid.append("spec")
    return invalid


def _validate_child_budget_fields(spec: Mapping[str, Any]) -> None:
    """Validate optional model-requested child budget fields at admission."""
    max_turns = spec.get("max_turns")
    if "max_turns" in spec and (type(max_turns) is not int or max_turns < 1):
        raise ValueError("child max_turns must be an integer >= 1")
    max_wall_s = spec.get("max_wall_s")
    if "max_wall_s" in spec and (type(max_wall_s) is not int or max_wall_s < 30):
        raise ValueError("child max_wall_s must be an integer >= 30")


def _parent_budget_limits(
    parent_spec: Mapping[str, Any], proposal: Mapping[str, Any]
) -> dict[str, int]:
    """Return the parent's remaining turn and wall budgets for one proposal."""
    configured_turns = parent_spec.get("max_turns", DEFAULT_MAX_TURNS)
    if type(configured_turns) is not int or configured_turns < 1:
        configured_turns = DEFAULT_MAX_TURNS
    configured_wall = _cfg_float(
        dict(parent_spec), "max_wall_s", "CAMBIUM_WALL_BUDGET_S", DEFAULT_WALL_BUDGET_S
    )
    limits = {
        "max_turns": configured_turns,
        "max_wall_s": max(0, math.floor(configured_wall)),
    }
    snapshot = proposal.get("_parent_budget")
    if isinstance(snapshot, dict):
        for field in limits:
            value = snapshot.get(field)
            if type(value) is int and value >= 0:
                limits[field] = value
    return limits


def _prepare_child_budget(
    parent_spec: Mapping[str, Any], proposal: dict[str, Any]
) -> tuple[dict[str, Any], dict[str, Any] | None]:
    """Clamp requested child budgets and return the admission decision."""
    raw_spec = proposal.get("spec")
    if not isinstance(raw_spec, dict):
        return proposal, None
    _validate_child_budget_fields(raw_spec)
    requested = {
        field: raw_spec[field] for field in ("max_turns", "max_wall_s") if field in raw_spec
    }
    prepared = {**proposal, "_parent_budget": None}
    if not requested:
        prepared.pop("_parent_budget", None)
        return prepared, None

    limits = _parent_budget_limits(parent_spec, proposal)
    admitted: dict[str, int] = {}
    clamped: list[str] = []
    for field, value in requested.items():
        limit = limits[field]
        if limit < 1:
            raise ValueError(f"child {field} has no remaining parent budget")
        effective = min(value, limit)
        if effective != value:
            clamped.append(field)
        admitted[field] = effective
    prepared["spec"] = {**raw_spec, **admitted}
    prepared.pop("_parent_budget", None)
    return prepared, {
        "requested": requested,
        "admitted": admitted,
        "parent_remaining": {field: limits[field] for field in requested},
        "clamped": clamped,
    }


def _proposal_parent_budget(state: Any) -> dict[str, int]:
    """Snapshot a generation's remaining budget before buffering its proposal."""
    configured_turns = state.spec.get("max_turns", DEFAULT_MAX_TURNS)
    if type(configured_turns) is not int or configured_turns < 1:
        configured_turns = DEFAULT_MAX_TURNS
    current_turn = state.turn
    if type(current_turn) is not int or current_turn < 0:
        current_turn = 0
    if current_turn == 0 and isinstance(state.envelope, dict):
        result_turn = state.envelope.get("turn")
        if type(result_turn) is int and result_turn >= 0:
            current_turn = result_turn
    return {
        "max_turns": max(0, configured_turns - current_turn),
        "max_wall_s": max(0, math.floor(state.wall_deadline - state.loop.time())),
    }


_CONTEXT_CHECKPOINT_FIELDS = frozenset(
    {
        "type",
        "request_id",
        "task_id",
        "generation",
        "epoch",
        "turn",
        "checkpoint_ref",
        "cache_key",
    }
)
_CACHE_KEY_FIELDS = frozenset(
    {
        "provider",
        "model",
        "protocol",
        "reasoning_effort",
        "system_sha256",
        "tools_sha256",
        "prefix_sha256",
        "suffix_sha256",
        "full_sha256",
        "prefix_bytes",
        "message_count",
        "redacted",
        "provider_boundary",
    }
)
_CACHE_KEY_INT_FIELDS = ("prefix_bytes", "message_count")
_CONTEXT_EPOCH_ADVANCED_FIELDS = frozenset(
    {
        "type",
        "request_id",
        "task_id",
        "generation",
        "epoch",
        "turn",
        "checkpoint_ref",
        "cache_key",
        "folded_from_epoch",
        "reason",
    }
)
_COMPACTION_FAILED_FIELDS = frozenset(
    {
        "type",
        "request_id",
        "task_id",
        "generation",
        "epoch",
        "reason",
    }
)
_PROVIDER_BOUNDARY_DEGRADED_FIELDS = frozenset(
    {"type", "request_id", "task_id", "generation", "error_type"}
)


def _invalid_context_checkpoint_fields(msg: dict[str, Any]) -> list[str]:
    """Return context_checkpoint fields whose values must not enter the runtime.

    Field names only; values are never echoed back. The checkpoint is the
    fork/resume trust anchor, so unknown fields and malformed cache-key
    values fail the whole message instead of being trimmed.
    """
    unknown = sorted(set(msg) - _CONTEXT_CHECKPOINT_FIELDS, key=str)
    if unknown:
        return unknown
    cache_key = msg.get("cache_key")
    if not isinstance(cache_key, dict):
        return ["cache_key"]
    invalid: list[str] = []
    unknown_cache_key = sorted(set(cache_key) - _CACHE_KEY_FIELDS, key=str)
    invalid.extend(f"cache_key.{field}" for field in unknown_cache_key)
    for field in ("epoch", "turn"):
        if not (type(msg.get(field)) is int and cast(int, msg.get(field)) > 0):
            invalid.append(field)
    checkpoint_ref = msg.get("checkpoint_ref")
    if not isinstance(checkpoint_ref, str) or not checkpoint_ref:
        invalid.append("checkpoint_ref")
    for field in _CACHE_KEY_INT_FIELDS:
        value = cache_key.get(field)
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            invalid.append(f"cache_key.{field}")
    for field in ("provider", "model", "protocol", "reasoning_effort"):
        if field not in cache_key:
            invalid.append(f"cache_key.{field}")
            continue
        value = cache_key.get(field)
        if field in ("provider", "reasoning_effort"):
            valid = value is None or (isinstance(value, str) and bool(value))
        else:
            valid = isinstance(value, str) and bool(value)
        if not valid:
            invalid.append(f"cache_key.{field}")
    required_digests = (
        "system_sha256",
        "tools_sha256",
        "prefix_sha256",
        "suffix_sha256",
        "full_sha256",
    )
    for field in required_digests:
        value = cache_key.get(field)
        if field not in cache_key:
            invalid.append(f"cache_key.{field}")
        elif not isinstance(value, str) or _SHA256_HEX_RE.match(value) is None:
            invalid.append(f"cache_key.{field}")
    if type(cache_key.get("redacted")) is not bool:
        invalid.append("cache_key.redacted")
    if "provider_boundary" not in cache_key:
        invalid.append("cache_key.provider_boundary")
    else:
        try:
            _validate_provider_boundary(cache_key["provider_boundary"])
        except ValueError:
            invalid.append("cache_key.provider_boundary")
    return invalid


def _invalid_context_epoch_advanced_fields(msg: dict[str, Any]) -> list[str]:
    """Return context_epoch_advanced fields whose values are invalid."""
    unknown = sorted(set(msg) - _CONTEXT_EPOCH_ADVANCED_FIELDS, key=str)
    if unknown:
        return unknown
    checkpoint = {
        "type": "context_checkpoint",
        "request_id": msg.get("request_id"),
        "task_id": msg.get("task_id"),
        "generation": msg.get("generation"),
        "epoch": msg.get("epoch"),
        "turn": msg.get("turn"),
        "checkpoint_ref": msg.get("checkpoint_ref"),
        "cache_key": msg.get("cache_key"),
    }
    invalid = _invalid_context_checkpoint_fields(checkpoint)
    if msg.get("type") != "context_epoch_advanced":
        invalid.append("type")
    for field in ("request_id", "task_id", "checkpoint_ref"):
        value = msg.get(field)
        if not isinstance(value, str) or not value:
            invalid.append(field)
    for field in ("generation", "epoch", "turn", "folded_from_epoch"):
        value = msg.get(field)
        if type(value) is not int or value <= 0:
            invalid.append(field)
    if "reason" not in msg:
        invalid.append("reason")
    elif msg["reason"] is not None and not isinstance(msg["reason"], str):
        invalid.append("reason")
    return invalid


def _invalid_compaction_failed_fields(msg: dict[str, Any]) -> list[str]:
    """Return compaction_failed fields whose values are invalid."""
    unknown = sorted(set(msg) - _COMPACTION_FAILED_FIELDS, key=str)
    if unknown:
        return unknown
    invalid: list[str] = []
    if msg.get("type") != "compaction_failed":
        invalid.append("type")
    for field in ("request_id", "task_id"):
        value = msg.get(field)
        if not isinstance(value, str) or not value:
            invalid.append(field)
    for field in ("generation", "epoch"):
        value = msg.get(field)
        if type(value) is not int or value <= 0:
            invalid.append(field)
    reason = msg.get("reason")
    if not isinstance(reason, str) or not reason:
        invalid.append("reason")
    return invalid


def _invalid_provider_boundary_degraded_fields(msg: dict[str, Any]) -> list[str]:
    """Return malformed provider-boundary degradation event fields."""
    unknown = sorted(set(msg) - _PROVIDER_BOUNDARY_DEGRADED_FIELDS, key=str)
    if unknown:
        return unknown
    invalid: list[str] = []
    if msg.get("type") != "provider_boundary_degraded":
        invalid.append("type")
    for field in ("task_id", "error_type"):
        if not isinstance(msg.get(field), str) or not msg[field]:
            invalid.append(field)
    if type(msg.get("generation")) is not int or msg["generation"] <= 0:
        invalid.append("generation")
    if (
        "request_id" in msg
        and msg["request_id"] is not None
        and not isinstance(msg["request_id"], str)
    ):
        invalid.append("request_id")
    return invalid


def _epoch_checkpoint_path(session_dir: Path, task_id: str, checkpoint_ref: str) -> Path:
    """Return one session-owned epoch checkpoint path after strict validation."""
    try:
        task_component, _epoch, _address_pre, _address_persisted = _validate_checkpoint_ref_shape(
            checkpoint_ref
        )
    except ValueError as exc:
        raise ValueError("invalid checkpoint_ref path") from exc
    if task_component != _safe_task_id(task_id):
        raise ValueError("checkpoint_ref task mismatch")

    root = (Path(session_dir).resolve() / ".cambium" / "checkpoints").resolve()
    relative = Path(checkpoint_ref)
    current = root
    for part in relative.parts:
        current /= part
        if current.is_symlink():
            raise ValueError("checkpoint path is a symlink")
    path = root / relative
    if not path.is_relative_to(root):
        raise ValueError("checkpoint_ref escapes the checkpoint root")
    return path


def _load_epoch_checkpoint_messages(
    session_dir: Path, task_id: str, checkpoint_ref: str
) -> dict[str, list[dict[str, str]]]:
    """Load the raw message lists from one immutable epoch checkpoint."""
    path = _epoch_checkpoint_path(session_dir, task_id, checkpoint_ref)
    try:
        if path.stat().st_size > MAX_LINE_BYTES * 4:
            raise ValueError("checkpoint exceeds the size cap")
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ValueError("checkpoint unreadable") from exc
    if not isinstance(data, dict):
        raise ValueError("checkpoint is not an object")

    loaded: dict[str, list[dict[str, str]]] = {}
    for field in ("provider_messages", "continuation_suffix"):
        raw_messages = data.get(field)
        if not isinstance(raw_messages, list):
            raise ValueError(f"checkpoint {field} is invalid")
        messages: list[dict[str, str]] = []
        for message in raw_messages:
            if (
                not isinstance(message, dict)
                or set(message) != {"role", "content"}
                or message.get("role") not in {"system", "user", "assistant", "tool"}
                or not isinstance(message.get("content"), str)
            ):
                raise ValueError(f"checkpoint {field} contains an invalid message")
            messages.append(
                {
                    "role": message["role"],
                    "content": message["content"],
                }
            )
        loaded[field] = messages
    if not loaded["provider_messages"]:
        raise ValueError("checkpoint provider_messages is empty")
    return loaded


def _reject_duplicate_checkpoint_pairs(
    pairs: list[tuple[str, Any]],
) -> dict[str, Any]:
    values: dict[str, Any] = {}
    for key, value in pairs:
        if key in values:
            raise ValueError("checkpoint contains duplicate JSON fields")
        values[key] = value
    return values


def _reject_checkpoint_json_constant(value: str) -> object:
    raise ValueError("checkpoint contains non-standard JSON constant")


def _load_epoch_checkpoint_data(
    session_dir: Path, task_id: str, checkpoint_ref: str
) -> dict[str, Any]:
    path = _epoch_checkpoint_path(session_dir, task_id, checkpoint_ref)
    try:
        if path.stat().st_size > MAX_LINE_BYTES * 4:
            raise ValueError("checkpoint exceeds the size cap")
        data = json.loads(
            path.read_text(encoding="utf-8"),
            object_pairs_hook=_reject_duplicate_checkpoint_pairs,
            parse_constant=_reject_checkpoint_json_constant,
        )
    except (OSError, ValueError) as exc:
        raise ValueError("checkpoint unreadable") from exc
    if not isinstance(data, dict):
        raise ValueError("checkpoint is not an object")
    return data


def _validate_advanced_epoch_checkpoint(
    session_dir: Path,
    task_id: str,
    generation: int,
    msg: Mapping[str, Any],
) -> None:
    checkpoint_ref = msg["checkpoint_ref"]
    data = _load_epoch_checkpoint_data(session_dir, task_id, checkpoint_ref)
    checkpoint = _validate_epoch_checkpoint_data(
        data,
        checkpoint_ref,
        expected_task_id=task_id,
        expected_generation=generation,
    )
    if (
        checkpoint.epoch != msg["epoch"]
        or checkpoint.turn != msg["turn"]
        or asdict(checkpoint.cache_key) != msg["cache_key"]
    ):
        raise ValueError("checkpoint descriptor mismatch")


def _wire_str(value: Any) -> str | None:
    """Coerce an unvalidated wire value for a JSON-safe event payload."""
    return value if isinstance(value, str) else None


def _protocol_version_mismatch(msg: dict[str, Any]) -> bool:
    if msg.get("type") == "ready":
        return msg.get("proto") != PROTO
    return "proto" in msg and msg["proto"] != PROTO


def _result_identity_note(msg: Mapping[str, Any], task_id: str, generation: int) -> str | None:
    """Return why a result envelope fails worker identity, or None."""
    claimed_task = msg.get("task_id")
    if claimed_task is not None and claimed_task != task_id:
        return "result task_id mismatch"
    claimed_generation = msg.get("generation")
    if claimed_generation is not None and (
        isinstance(claimed_generation, bool)
        or not isinstance(claimed_generation, int)
        or claimed_generation != generation
    ):
        return "result generation mismatch"
    return None


def _terminal_action_for_event(value: Any) -> dict[str, Any] | None:
    """Project a worker terminal action into a small, bounded event payload."""
    if not isinstance(value, Mapping):
        return None
    if value.get("type") != "finish" or type(value.get("objective_met")) is not bool:
        return None
    summary = value.get("summary")
    if not isinstance(summary, str):
        summary = ""
    summary_present = value.get("summary_present")
    if type(summary_present) is not bool:
        summary_present = bool(summary)
    return {
        "type": "finish",
        "objective_met": value["objective_met"],
        "summary_present": summary_present,
        "summary": _cap_utf8(summary, MAX_ENVELOPE_FIELD_CHARS),
    }


_TOOL_EVENT_INT_FIELDS = ("batch_index", "batch_size", "turn")
_TOOL_EVENT_DURATION_FIELDS = ("duration_ms",)
_TOOL_OUTPUT_DELTA_MAX_BYTES = 2048
_TOOL_OUTPUT_STREAMS = frozenset({"stdout", "stderr"})
_USAGE_EVENT_FORWARD_FIELDS = frozenset(
    {
        "turn",
        "provider",
        "model",
        "usage",
        "estimated_cost_usd",
        "latency_s",
        "retry_after_s",
        "request_rate_status",
        "account_quota_owner",
        "prompt_prefix_bytes",
        "provider_cache_hit",
        "failure_reason",
        "call_kind",
        "active_context_bytes",
        "active_context_messages",
        "summary_trunk_bytes",
        "summary_segments",
        "raw_tail_bytes",
        "epoch",
        "fork_of",
        "quota_windows",
    }
)


def _invalid_tool_event_fields(msg: dict[str, Any]) -> list[str]:
    """Return worker tool_event fields whose values must not enter durable events.

    Only field names are reported; invalid values are never echoed back.
    """
    invalid: list[str] = []
    for field in _TOOL_EVENT_INT_FIELDS:
        if field in msg and not (type(msg[field]) is int and msg[field] >= 0):
            invalid.append(field)
    if "ok" in msg and type(msg["ok"]) is not bool:
        invalid.append("ok")
    for field in _TOOL_EVENT_DURATION_FIELDS:
        value = msg.get(field)
        if field in msg and not (
            type(value) in (int, float)
            and cast(int | float, value) >= 0
            and math.isfinite(cast(int | float, value))
        ):
            invalid.append(field)
    return invalid


def _invalid_tool_output_delta_fields(msg: dict[str, Any]) -> list[str]:
    """Validate the bounded, non-terminal process-output wire message."""
    allowed = {
        "type",
        "task_id",
        "generation",
        "tool",
        "turn",
        "stream",
        "delta",
        "monotonic_ms",
    }
    invalid = sorted(set(msg) - allowed)
    for field in ("turn", "monotonic_ms"):
        value = msg.get(field)
        if field in msg and not (type(value) is int and value >= 0):
            invalid.append(field)
    tool = msg.get("tool")
    if not isinstance(tool, str) or not tool:
        invalid.append("tool")
    if msg.get("stream") not in _TOOL_OUTPUT_STREAMS:
        invalid.append("stream")
    delta = msg.get("delta")
    if (
        not isinstance(delta, str)
        or not delta
        or len(delta.encode("utf-8", errors="replace")) > _TOOL_OUTPUT_DELTA_MAX_BYTES
    ):
        invalid.append("delta")
    return sorted(set(invalid))


def _invalid_usage_event_fields(msg: dict[str, Any]) -> list[str]:
    """Return worker usage_event fields whose values must not enter durable events.

    Field names only; values are never echoed back. Unknown fields fail the
    whole event so a schema drift cannot smuggle data into the log. Known
    usage counts must be finite and non-negative; rejecting the whole event
    keeps invalid values out of the routing-debt ledger.
    """
    unknown = sorted(set(msg) - ({"type", "task_id", "generation"} | _USAGE_EVENT_FORWARD_FIELDS))
    if unknown:
        return unknown
    invalid: list[str] = []
    for field in (
        "turn",
        "prompt_prefix_bytes",
        "active_context_bytes",
        "active_context_messages",
        "summary_trunk_bytes",
        "summary_segments",
        "raw_tail_bytes",
        "epoch",
    ):
        if field in msg and not (type(msg[field]) is int and msg[field] >= 0):
            invalid.append(field)
    if "fork_of" in msg and not (type(msg["fork_of"]) is str and msg["fork_of"]):
        invalid.append("fork_of")
    if "call_kind" in msg and msg["call_kind"] not in {"agent", "summary"}:
        invalid.append("call_kind")
    for field in ("estimated_cost_usd", "latency_s", "retry_after_s"):
        value = msg.get(field)
        if field in msg and not (
            type(value) in (int, float)
            and cast(int | float, value) >= 0
            and math.isfinite(cast(int | float, value))
        ):
            invalid.append(field)
    if "provider_cache_hit" in msg and type(msg["provider_cache_hit"]) is not bool:
        invalid.append("provider_cache_hit")
    for field in (
        "provider",
        "model",
        "request_rate_status",
        "account_quota_owner",
        "failure_reason",
    ):
        if field in msg and not (type(msg[field]) is str and msg[field]):
            invalid.append(field)
    quota_windows = msg.get("quota_windows")
    if "quota_windows" in msg:
        allowed_quota_fields = {
            "provider",
            "name",
            "reset_at",
            "allowance_tokens",
            "used_tokens",
            "allowance_requests",
            "used_requests",
            "reserve_fraction",
            "remaining_tokens",
            "remaining_requests",
        }
        if (
            not isinstance(quota_windows, list)
            or len(quota_windows) > 16
            or any(
                not isinstance(item, dict)
                or set(item) - allowed_quota_fields
                or not isinstance(item.get("provider"), str)
                or not isinstance(item.get("name"), str)
                for item in quota_windows
            )
        ):
            invalid.append("quota_windows")

    usage = msg.get("usage")
    if "usage" in msg and (
        not isinstance(usage, dict)
        or any(
            key not in _PROVIDER_METADATA_USAGE_FIELDS or not _valid_usage_count(value)
            for key, value in usage.items()
        )
    ):
        invalid.append("usage")
    return invalid


def _stdin_deadline(wall_deadline: float) -> float:
    loop = asyncio.get_running_loop()
    return min(wall_deadline, loop.time() + _stdin_write_timeout_s())


def _wall_timeout_detail(wall_budget: float, wall_deadline: float, restarts: int) -> str:
    elapsed = max(0.0, time.monotonic() - (wall_deadline - wall_budget))
    return f"timeout: wall (elapsed={elapsed:g}s > budget={wall_budget:g}s, restarts={restarts})"


@dataclass(frozen=True, slots=True)
class SliceResult:
    """Outcome of one supervised worker run."""

    status: str  # "succeeded" | "failed"
    exit_code: int  # supervisor exit code: 0 only when everything succeeded
    worker_exit_code: int | None = None
    worker_status: str | None = None  # from the result_envelope (advisory)
    merge_sha: str | None = None
    timed_out: bool = False
    timeout_phase: str | None = None  # "ready" | "wall" | "heartbeat" | "pong" | "stdin"


_TIMEOUT_PHASES = ("ready", "wall", "heartbeat", "pong", "stdin")


def _status_line_is_fence(line: str) -> bool:
    """Whether a porcelain status line only touches the supervisor's fence dir
    or an incidental cache/build artifact of the agent's tool use."""
    if len(line) < 4 or line[2] != " ":
        return False
    path = line[3:].strip()
    if path == ".cambium" or path.startswith(".cambium/"):
        return True
    return is_cache_artifact_path(path)


def _bounded_salvage_diff(diff: bytes) -> tuple[bytes, bool]:
    """Bound salvage bytes while retaining an explicit clipping marker."""
    if len(diff) <= MAX_SALVAGE_BYTES:
        return diff, False
    keep = max(0, MAX_SALVAGE_BYTES - len(_SALVAGE_TRUNCATION_MARKER))
    return diff[:keep] + _SALVAGE_TRUNCATION_MARKER, True


def _write_salvage_artifacts(
    directory: Path,
    diff: bytes,
    metadata: dict[str, Any],
) -> None:
    """Atomically publish one salvage diff and its metadata sidecar."""
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    files = {
        directory / "workspace.diff": diff,
        directory / "salvage.json": (json.dumps(metadata, sort_keys=True) + "\n").encode("utf-8"),
    }
    for target, content in files.items():
        fd, temporary_name = tempfile.mkstemp(
            prefix=f".{target.name}.", suffix=".tmp", dir=directory
        )
        temporary_path = Path(temporary_name)
        published = False
        try:
            with os.fdopen(fd, "wb") as stream:
                stream.write(content)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary_path, target)
            published = True
        finally:
            if not published:
                try:
                    temporary_path.unlink()
                except FileNotFoundError:
                    pass


def _cfg_float(task_spec: dict[str, Any], key: str, env: str, default: float) -> float:
    spec_value = task_spec.get(key)
    value = spec_value if spec_value is not None else os.environ.get(env, default)
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        raise ValueError(f"invalid {key}: {value!r}") from None
    if not math.isfinite(parsed):
        raise ValueError(f"invalid {key}: value must be finite")
    return parsed


def _pool_env_key(env: dict[str, str]) -> frozenset[tuple[str, str]]:
    """Env identity for pool matching, ignoring rebindable per-task values.

    ``_worker_environment`` stamps values a rebind cannot change (the child's
    env is fixed at spawn): a pooled worker may only serve a task whose
    remaining env matches exactly. Two stamped values are excluded because
    they are per-task/per-worktree by construction and rebinding re-sends the
    full init:

    - ``CAMBIUM_TASK_ID`` / ``CAMBIUM_GENERATION``: per-task identity, rebuilt
      from the rebind init.
    ``CAMBIUM_SESSION_ID``, ``CAMBIUM_PROVIDERS``, and the allowlisted
    provider credentials remain in the key: a worker whose env cannot serve
    the new task (different session, provider config, or credentials) is
    never popped.
    """
    return frozenset(
        (name, value)
        for name, value in env.items()
        if name not in ("CAMBIUM_TASK_ID", "CAMBIUM_GENERATION")
    )


async def _write_json(
    proc: asyncio.subprocess.Process,
    msg: dict[str, Any],
    *,
    deadline: float | None = None,
) -> bool:
    """Write one wire message before ``deadline`` or kill its process group.

    Framing is centralized in :mod:`cambium.ipc`; the oversize pre-check,
    deadline-bounded drain, and kill-on-failure stay local.
    """
    if proc.stdin is None or proc.stdin.is_closing():
        return False
    frame = encode_message(msg)
    if frame is None:
        await _kill_worker(proc)
        return False
    loop = asyncio.get_running_loop()
    write_deadline = deadline
    if write_deadline is None:
        write_deadline = loop.time() + _stdin_write_timeout_s()
    remaining = write_deadline - loop.time()
    if remaining <= 0:
        await _kill_worker(proc)
        return False
    try:
        write_frame(proc.stdin, frame)
        await asyncio.wait_for(proc.stdin.drain(), remaining)
        return True
    except (BrokenPipeError, ConnectionResetError, OSError, TimeoutError):
        await _kill_worker(proc)
        return False


async def _kill_worker(proc: asyncio.subprocess.Process) -> None:
    """SIGKILL the worker's process group (worker is its own session/group leader)."""
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError, OSError):
        pass


def _kill_worktree_process_groups(
    worktree: Path, skip_groups: frozenset[int] = frozenset()
) -> None:
    """Kill process groups whose cwd is inside a tree before removing it.

    Workers put their own process group in the tree, while fenced git helpers
    may create descendant sessions of their own. The latter cannot be found
    from the worker handle alone; on Linux, ``/proc/*/cwd`` gives us a bounded
    best-effort sweep without adding a process-management dependency.

    ``skip_groups`` excludes groups owned by the supervisor's warm pool: an
    idle pooled worker keeps its cwd inside its finished task's worktree, but
    it must survive pruning so a later task can rebind it (Eval-3 ADOPT).
    """
    if os.name != "posix":
        return
    root = Path(worktree).resolve()
    own_group = os.getpgrp()
    groups: set[int] = set()
    proc_root = Path("/proc")
    try:
        entries = tuple(proc_root.iterdir())
    except OSError:
        return
    for entry in entries:
        if not entry.name.isdigit():
            continue
        try:
            pid = int(entry.name)
            cwd = Path(os.readlink(entry / "cwd")).resolve()
            if not (cwd == root or cwd.is_relative_to(root)):
                continue
            pgid = os.getpgid(pid)
        except (FileNotFoundError, PermissionError, OSError, ValueError):
            continue
        if pgid != own_group and pgid not in skip_groups:
            groups.add(pgid)
    for pgid in groups:
        try:
            os.killpg(pgid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError, OSError):
            pass


async def run_session(
    session_dir: str | Path,
    task_spec: dict[str, Any],
    on_event: EventSink | None = None,
) -> SliceResult:
    """Run one worker end to end and return the slice outcome.

    Thin one-task adapter over :func:`run_plan`. The caller's slice-shaped
    spec (``scratch_repo`` / ``spec`` / ``wall_budget_s``) is mapped to a
    canonical one-task plan (``repo`` / ``task`` / ``max_wall_s``) with
    ``max_restarts=0``; the resulting :class:`TaskResult` is mapped back to
    :class:`SliceResult` to keep the public return shape.
    """
    plan_task = _slice_to_plan_task(dict(task_spec))
    plan_result = await run_plan(session_dir, {"tasks": [plan_task]}, on_event=on_event)
    return _task_result_to_slice_result(plan_result.results[0])


def _slice_to_plan_task(spec: dict[str, Any]) -> dict[str, Any]:
    """Map a slice-shaped task spec to a canonical one-task plan entry."""
    plan_task = dict(spec)
    if "scratch_repo" in plan_task:
        plan_task["repo"] = str(Path(plan_task.pop("scratch_repo")).resolve())
    if "wall_budget_s" in plan_task:
        plan_task["max_wall_s"] = plan_task.pop("wall_budget_s")
    if "spec" in plan_task:
        plan_task["task"] = plan_task.pop("spec")
    task = plan_task.get("task")
    if not isinstance(task, str) or not task.strip():
        raise ValueError("run_session requires a non-empty task")
    plan_task.setdefault("max_restarts", 0)
    return plan_task


def _task_result_to_slice_result(result: TaskResult) -> SliceResult:
    """Map one :class:`TaskResult` back to the historical slice shape.

    ``TaskResult`` does not retain the worker's own exit code or wire status
    token (the supervisor verdict is authoritative); those slice fields are
    ``None``. Timeout state is recovered from the canonical failure reason.
    """
    reason = result.reason or ""
    timeout_phase = next((phase for phase in _TIMEOUT_PHASES if phase in reason), None)
    return SliceResult(
        status=result.status,
        exit_code=result.exit_code,
        worker_exit_code=None,
        worker_status=None,
        merge_sha=result.merge_sha,
        timed_out=timeout_phase is not None,
        timeout_phase=timeout_phase,
    )


# =====================================================================
# Multi-worker supervisor runtime
# (docs/architecture/architecture.md §5.3, §7.1-§7.8)
#
# Drives N worker subprocesses concurrently under an asyncio.TaskGroup.
# Each worker runs in its own process group (start_new_session) inside a
# git worktree; liveness is the four-layer model (process exit, exit
# message, heartbeat watchdog, EOF-as-advisory); restarts use full-jitter
# backoff with a per-task cap; worktrees are hard-reset before every
# respawn; a clean worker whose branch differs from its resolved base and whose
# envelope reports "succeeded" is merged atomically onto refs/heads/main (no
# pre-merge gate).
#
# cambium.store and cambium.merge are runtime dependency contracts.
# =====================================================================

DEFAULT_READY_TIMEOUT_S = 10.0
DEFAULT_HEARTBEAT_INTERVAL_S = 15.0
DEFAULT_HEARTBEAT_TIMEOUT_S = 90.0
DEFAULT_WALL_BUDGET_S = 300.0
DEFAULT_MAX_RESTARTS = 3
DEFAULT_MAX_TURNS = 50
DEFAULT_MAX_TOKENS = 200_000
RESTART_BASE_DELAY_S = 1.0
RESTART_MAX_DELAY_S = 30.0
EOF_GRACE_S = 5.0
WORKER_EXIT_WAIT_S = 10.0
TERM_GRACE_S = 5.0
MAX_PARSE_ERRORS = 500
PROTO_UNKNOWN_REQUEST_ID = "PROTO_UNKNOWN_REQUEST_ID"
MAX_SALVAGE_BYTES = 1_000_000
_SALVAGE_TRUNCATION_MARKER = b"\n... [salvage truncated]\n"
_CRITICAL_EVENT_KINDS = CRITICAL_KINDS | {"worktree_salvaged"}


_ENV_NAME_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*\Z")
_PROVIDER_METADATA_USAGE_FIELDS = frozenset(
    {
        "prompt_tokens",
        "completion_tokens",
        "total_tokens",
        "input_tokens",
        "output_tokens",
        "cached_tokens",
    }
)


def _valid_usage_count(value: Any) -> bool:
    """Return whether a provider token count is finite and non-negative."""
    if isinstance(value, bool):
        return False
    if isinstance(value, int):
        return value >= 0
    return isinstance(value, float) and math.isfinite(value) and value >= 0


def _provider_env_keys(spec: dict[str, Any]) -> frozenset[str]:
    """Return only validated provider-key names declared by a task."""
    values: list[Any] = []
    explicit = spec.get("provider_env_keys")
    if isinstance(explicit, list | tuple):
        values.extend(explicit)
    fanout_config = spec.get("fanout_config")
    if isinstance(fanout_config, dict):
        configured = fanout_config.get("provider_env_keys")
        if isinstance(configured, list | tuple):
            values.extend(configured)
        providers = fanout_config.get("providers")
        if isinstance(providers, list | tuple):
            values.extend(
                provider.get("api_key_env") for provider in providers if isinstance(provider, dict)
            )
    return frozenset(
        value for value in values if isinstance(value, str) and _ENV_NAME_RE.fullmatch(value)
    )


def _provider_environment_value(key: str, provider_environment: Mapping[str, str] | None) -> object:
    """Return the value that the worker environment would forward for *key*."""
    if provider_environment is not None:
        override = provider_environment.get(key)
        if override is not None:
            return override
    return os.environ.get(key)


def _provider_credential_ready_at_admission(
    provider: Any,
    provider_environment: Mapping[str, str] | None = None,
    oauth_store: OAuthStore | None = None,
) -> bool:
    """Reuse one-shot credential readiness without changing process state."""
    auth = getattr(provider, "auth", None)
    if getattr(auth, "value", auth) == AuthMode.CODEX_CHATGPT.value:
        if oauth_store is None:
            from .oneshot import _provider_credential_ready

            return _provider_credential_ready(provider, AuthStore())
        store = oauth_store
        try:
            return store.read_document(provider.name) is not None
        except OAuthMissingError:
            return False

    if getattr(auth, "value", auth) == AuthMode.NONE.value:
        return True

    env_name = getattr(provider, "api_key_env", "")
    if provider_environment is not None and env_name in provider_environment:
        return bool(provider_environment[env_name])

    # oneshot owns the API-key/AuthStore readiness definition.  Keep this
    # local import because oneshot imports this module for its run adapter.
    from .oneshot import _provider_credential_ready

    return _provider_credential_ready(provider, AuthStore())


def _validate_provider_credential(value: object) -> None:
    """Reject a credential that is unsafe to use as an unrestricted redaction needle."""
    if not isinstance(value, str):
        raise TypeError("provider credential must be a string")
    try:
        encoded = value.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise ValueError("provider credential is not valid UTF-8") from exc
    if len(encoded) < MIN_API_KEY_BYTES:
        raise ValueError("provider credential is too short")


def _fanout_provider_names(spec: Mapping[str, Any]) -> frozenset[str]:
    """Provider names a task references through fanout_config or assignment."""
    names: set[str] = set()
    fanout_config = spec.get("fanout_config")
    if isinstance(fanout_config, dict):
        providers = fanout_config.get("providers")
        if isinstance(providers, list | tuple):
            names.update(
                entry["name"]
                for entry in providers
                if isinstance(entry, dict) and isinstance(entry.get("name"), str)
            )
    assigned = spec.get("assigned_provider")
    if isinstance(assigned, str):
        names.add(assigned)
    return frozenset(names)


def _codex_oauth_provider_names(
    spec: Mapping[str, Any], source: Mapping[str, str]
) -> frozenset[str]:
    """The authorized codex_chatgpt providers that need an OAuth token injected.

    Identity is carried by provider name (``authorized_providers``) so OAuth
    providers are never dropped the way env-key filtering dropped them. When a
    task carries no authorized set (legacy plans), the referenced-provider
    fallback preserves prior behavior; an explicit model pin limits the
    unrestricted fanout fallback to matching providers. A config that cannot
    be loaded yields no names: the worker's own config load surfaces file
    errors at its boundary (transport authoritative), so this preflight only
    adds the oauth-document gate on top of a loadable config.
    """
    try:
        providers = load_providers(_provider_config_path(source, spec))
    except (OSError, ValueError):
        return frozenset()
    codex = frozenset(
        provider.name for provider in providers if provider.auth is AuthMode.CODEX_CHATGPT
    )
    authorized_raw = spec.get("authorized_providers")
    authorized_explicit = spec.get("authorized_providers_explicit", bool(authorized_raw))
    if isinstance(authorized_raw, list | tuple):
        authorized = frozenset(name for name in authorized_raw if isinstance(name, str) and name)
        # An explicit empty authorization is a deliberate deny-all set.  Only
        # plans from before provider identities were carried use the legacy
        # unrestricted fallback below.
        if authorized_explicit or authorized:
            return codex & authorized
    referenced = _fanout_provider_names(spec)
    if referenced:
        return codex & referenced
    # No explicit provider restriction: a pinned model narrows the OAuth
    # providers to the same model the worker will route to. Without a model
    # pin, the worker loads every configured provider and may cascade over any
    # codex provider. Marker-mode tasks never build a router and stay empty.
    fanout_config = spec.get("fanout_config")
    if isinstance(fanout_config, dict) and fanout_config:
        model = fanout_config.get("model")
        if isinstance(model, str) and model:
            return frozenset(
                provider.name
                for provider in providers
                if provider.auth is AuthMode.CODEX_CHATGPT and provider.model == model
            )
        return codex
    return frozenset()


def _oauth_document_is_usable(doc: Any, *, now: float | None = None) -> bool:
    """Local freshness gate: unexpired, or refreshable via a stored refresh token.

    Never touches the network: an unexpired access token passes; an expired
    one passes only when the stored document still carries a refresh token
    (the transport refreshes at spawn, never per request).
    """
    if doc.expires_at - (time.time() if now is None else now) > DEFAULT_REFRESH_MARGIN_S:
        return True
    return bool(doc.refresh_token)


def _require_oauth_document(provider: str, oauth_store: OAuthStore | None) -> None:
    """Fail-closed preflight for one codex_chatgpt provider: LOCAL store read only."""
    store = OAuthStore() if oauth_store is None else oauth_store
    try:
        doc = store.validate(provider)
    except OAuthMissingError:
        raise ValueError(
            f"task references codex_chatgpt provider {provider!r} but no oauth "
            "session is stored for it; run `cambium auth oauth --import-codex-cli` "
            "or `cambium auth oauth <provider> --client-id ID`"
        ) from None
    except OAuthError as exc:
        raise ValueError(
            f"task references codex_chatgpt provider {provider!r} but the oauth "
            f"store is invalid: {exc}"
        ) from None
    if not _oauth_document_is_usable(doc):
        raise ValueError(
            f"task references codex_chatgpt provider {provider!r} whose stored "
            "oauth session is expired and has no usable refresh token"
        )
    if TokenManager(store=store, provider=provider, client_id=None).disabled(provider):
        raise ValueError(
            f"task references codex_chatgpt provider {provider!r} whose oauth "
            "session is disabled (refresh was rejected); re-login is required"
        )


def _validate_provider_environment(
    specs: list[dict[str, Any]],
    provider_environment: Mapping[str, str] | None,
    *,
    oauth_store: OAuthStore | None = None,
) -> None:
    """Validate every non-missing value a declared provider key can forward.

    OAuth preflight (fail closed): a task that references a codex_chatgpt
    provider must have a present, unexpired-or-refreshable oauth document in
    the LOCAL store; missing or corrupt credentials raise a clear ValueError.
    The preflight is local-only (no network probe); the transport (worker
    refresh at spawn) stays authoritative.
    """
    for spec in specs:
        for key in _provider_env_keys(spec):
            value = _provider_environment_value(key, provider_environment)
            if value is not None:
                _validate_provider_credential(value)
        for provider in sorted(
            _codex_oauth_provider_names(spec, provider_environment or os.environ)
        ):
            _require_oauth_document(provider, oauth_store)


def _provider_config_path(source: Mapping[str, str], spec: Mapping[str, Any] | None = None) -> str:
    """Resolve the absolute provider-config path a provider-mode worker loads."""
    if spec is not None:
        configured = spec.get("provider_config_path")
        if isinstance(configured, str) and configured:
            path = Path(configured).expanduser()
            if not path.is_absolute():
                path = Path.cwd() / path
            return str(path.resolve())
    configured = source.get("CAMBIUM_PROVIDERS")
    if configured:
        path = Path(configured).expanduser()
    else:
        path = DEFAULT_PROVIDER_PATH
    if not path.is_absolute():
        path = Path.cwd() / path
    return str(path.resolve())


def _oauth_worker_environment(
    spec: dict[str, Any],
    source: Mapping[str, str],
    oauth_store: OAuthStore | None,
) -> tuple[dict[str, str], list[str]]:
    """Ensure-fresh at spawn for referenced codex providers.

    Returns ``(env_additions, access_values)``: the former carries ONLY the
    access token and account id as ``CAMBIUM_OAUTH_ACCESS_<PROVIDER>`` /
    ``CAMBIUM_OAUTH_ACCOUNT_<PROVIDER>``; the refresh token never leaves the
    supervisor process. A refresh at spawn is acceptable (never per request).
    """
    providers = _codex_oauth_provider_names(spec, source)
    if not providers:
        return {}, []
    store = OAuthStore() if oauth_store is None else oauth_store
    client_id = source.get("CAMBIUM_CODEX_CLIENT_ID") or None
    additions: dict[str, str] = {}
    access_values: list[str] = []
    for provider in sorted(providers):
        manager = TokenManager(store=store, provider=provider, client_id=client_id)
        try:
            access_token, account_id = manager.ensure_fresh()
        except OAuthError as exc:
            raise ValueError(
                f"task references codex_chatgpt provider {provider!r} but its "
                f"oauth session could not be ensured fresh: {exc}"
            ) from None
        additions[f"CAMBIUM_OAUTH_ACCESS_{oauth_env_suffix(provider)}"] = access_token
        access_values.append(access_token)
        if account_id:
            additions[f"CAMBIUM_OAUTH_ACCOUNT_{oauth_env_suffix(provider)}"] = account_id
    return additions, access_values


def _worker_environment(
    spec: dict[str, Any],
    generation: int,
    *,
    session_dir: Path | None = None,
    provider_environment: Mapping[str, str] | None = None,
    oauth_store: OAuthStore | None = None,
    redactor: Redactor | None = None,
) -> dict[str, str]:
    """Build a strict worker env with authorized provider credentials.

    For a task that references a codex_chatgpt provider, the access token and
    account id are ensured fresh once at spawn from the supervisor's
    ``TokenManager`` and injected as ``CAMBIUM_OAUTH_ACCESS_<PROVIDER>`` /
    ``CAMBIUM_OAUTH_ACCOUNT_<PROVIDER>``. Worker processes never receive the
    refresh token. Injected access tokens are registered with the session
    redactor via ``register_secret`` so a rotated token stays redacted in
    every later event record.
    """
    _validate_provider_environment([spec], provider_environment, oauth_store=oauth_store)
    source = dict(os.environ)
    allowed_provider_keys = set(_provider_env_keys(spec))
    if provider_environment is not None:
        for name in allowed_provider_keys:
            value = provider_environment.get(name)
            if value is not None:
                source[name] = value
    oauth_environment, oauth_access_values = _oauth_worker_environment(spec, source, oauth_store)
    for name, value in oauth_environment.items():
        source[name] = value
        allowed_provider_keys.add(name)
    overrides = {
        "CAMBIUM_TASK_ID": spec["task_id"],
        "CAMBIUM_GENERATION": str(generation),
    }
    if session_dir is not None:
        overrides["CAMBIUM_SESSION_ID"] = str(session_dir.resolve())
    worktree = Path(spec["worktree_path"]).resolve() if "worktree_path" in spec else None
    env = _strip_sensitive_env(
        source,
        allowed_keys=allowed_provider_keys,
        worktree=worktree,
        overrides=overrides,
    )
    if spec.get("fanout_config") is not None:
        env["CAMBIUM_PROVIDERS"] = _provider_config_path(source, spec)
    if redactor is not None:
        for value in oauth_access_values:
            redactor.register_secret(value)
    return env


def _redacted_provider_metadata(value: Any) -> dict[str, Any] | None:
    """Keep only scalar provider provenance safe for event serialization.

    Invalid known usage counts are omitted at this untrusted result boundary;
    no negative or non-finite count is serialized.
    """
    if not isinstance(value, dict):
        return None
    provider = value.get("provider")
    model = value.get("model")
    latency = value.get("latency_s")
    if not isinstance(provider, str) or not isinstance(model, str):
        return None
    if isinstance(latency, bool) or not isinstance(latency, int | float):
        return None
    usage = value.get("usage")
    if not isinstance(usage, dict):
        usage = {}
    usage_counts = {
        key: count
        for key, count in usage.items()
        if key in _PROVIDER_METADATA_USAGE_FIELDS and _valid_usage_count(count)
    }
    metadata = {
        "provider": provider,
        "model": model,
        "usage": usage_counts,
        "latency_s": max(0.0, float(latency)),
    }
    fell_back_from = value.get("fell_back_from")
    if isinstance(fell_back_from, str) and fell_back_from:
        metadata["fell_back_from"] = fell_back_from
    return metadata


def _strip_sensitive_env(
    env: dict[str, str],
    *,
    allowed_keys: Any = None,
    worktree: Path | None = None,
    overrides: dict[str, str] | None = None,
) -> dict[str, str]:
    """Build the strict child environment used by every supervisor spawn.

    The historical name remains because it is part of the supervisor's
    internal call-site contract.  This is now a fail-closed allowlist, not a
    name-based secret scrub.
    """
    scrubbed = scrub_environment(env)
    for name in allowed_keys or ():
        if name in env:
            scrubbed[name] = env[name]
    return build_subprocess_env(
        scrubbed,
        allowed_keys=allowed_keys,
        worktree=worktree,
        overrides=overrides,
    )


def _session_redactor(
    specs: list[dict[str, Any]],
    provider_environment: Mapping[str, str] | None = None,
    *,
    oauth_store: OAuthStore | None = None,
) -> Redactor:
    """Build one session redactor from explicitly authorized provider values.

    The registry covers every value ``_worker_environment`` can forward from
    the declared ``provider_env_keys``. Every non-empty value is registered for
    substring redaction regardless of its naming or value shape. OAuth access
    tokens are ensured fresh and registered at spawn time (per task) via
    ``register_secret``, so rotated tokens never reach a durable event.
    """
    _validate_provider_environment(specs, provider_environment, oauth_store=oauth_store)
    secret_values: list[str] = []
    for spec in specs:
        for key in _provider_env_keys(spec):
            value = _provider_environment_value(key, provider_environment)
            if not isinstance(value, str) or not value:
                continue
            secret_values.append(value)
    return build_session_redactor(secret_values)


def _interactive_turn_event_stores(session_dir: Path) -> list[tuple[int, Path]]:
    """Return durable event stores for an interactive root, oldest first."""
    stores: list[tuple[int, Path]] = []
    try:
        children = tuple(session_dir.iterdir())
    except OSError:
        return stores
    for child in children:
        match = re.fullmatch(r"turn-(\d+)", child.name)
        if match is None or child.is_symlink() or not child.is_dir():
            continue
        event_db = child / ".cambium" / "events.db"
        if not event_db.is_symlink() and event_db.is_file():
            stores.append((int(match.group(1)), event_db))
    stores.sort(key=lambda item: item[0])
    return stores


def _interactive_event_timestamp(event: Mapping[str, Any]) -> float | None:
    value = event.get("ts")
    if isinstance(value, bool) or not isinstance(value, int | float | str):
        return None
    try:
        timestamp = float(value)
    except (OverflowError, ValueError):
        return None
    return timestamp if math.isfinite(timestamp) else None


_INTERACTIVE_READ_BUSY_TIMEOUT_MS = 200


@dataclass(frozen=True, slots=True)
class EventCursor:
    """Monotonic interactive replay state.

    ``watermark`` is the number of events delivered through this cursor;
    ``positions`` stores each source store's last delivered local ``seq``.
    Sorting is only applied to newly read rows, so a late event with a lower
    turn/payload sort key is delivered after the existing watermark instead of
    being hidden behind it.  The legacy integer ``read_events`` API remains
    available; monitors use this explicit cursor because one integer cannot
    represent independent per-store positions.
    """

    watermark: int = 0
    positions: tuple[tuple[str, int], ...] = ()

    def position(self, store_key: str) -> int:
        for key, sequence in self.positions:
            if key == store_key:
                return sequence
        return 0


def _cursor_positions(cursor: EventCursor) -> dict[str, int]:
    if type(cursor.watermark) is not int or cursor.watermark < 0:
        raise ValueError("event cursor watermark must be a non-negative integer")
    positions: dict[str, int] = {}
    for key, sequence in cursor.positions:
        if not isinstance(key, str) or not key:
            raise ValueError("event cursor store key must be a non-empty string")
        if type(sequence) is not int or sequence < 0:
            raise ValueError("event cursor position must be a non-negative integer")
        positions[key] = sequence
    return positions


def _interactive_store_key(turn: int) -> str:
    return f"turn:{turn}"


def _transient_event_store_lock(exc: StoreError) -> bool:
    cause = exc.__cause__
    return isinstance(cause, sqlite3.Error) and any(
        marker in str(cause).lower() for marker in ("locked", "busy")
    )


def _read_interactive_events_with_cursor(
    session_dir: Path, cursor: EventCursor
) -> tuple[list[dict[str, Any]], EventCursor]:
    """Read only rows beyond each source's cursor and append a new watermark."""
    positions = _cursor_positions(cursor)
    next_positions = dict(positions)
    turn_stores = _interactive_turn_event_stores(session_dir)
    records: list[tuple[int, int, float | None, int, dict[str, Any]]] = []
    for turn, event_db in turn_stores:
        store_key = _interactive_store_key(turn)
        try:
            events = read_events_file(
                event_db,
                positions.get(store_key, 0),
                busy_timeout_ms=_INTERACTIVE_READ_BUSY_TIMEOUT_MS,
            )
        except StoreError as exc:
            if _transient_event_store_lock(exc):
                continue
            raise
        if events:
            next_positions[store_key] = max(event["seq"] for event in events)
        for event in events:
            records.append(
                (
                    turn,
                    event["seq"],
                    _interactive_event_timestamp(event),
                    0,
                    event,
                )
            )

    root_event_db = session_dir / ".cambium" / "events.db"
    try:
        root_events = read_events_file(
            root_event_db,
            positions.get("root", 0),
            busy_timeout_ms=_INTERACTIVE_READ_BUSY_TIMEOUT_MS,
        )
    except StoreError as exc:
        if not _transient_event_store_lock(exc):
            raise
        root_events = []
    if root_events:
        next_positions["root"] = max(event["seq"] for event in root_events)
    fallback_turn = turn_stores[-1][0] + 1 if turn_stores else 0
    for event in root_events:
        payload = event.get("payload")
        event_turn = payload.get("turn") if isinstance(payload, Mapping) else None
        turn = event_turn if type(event_turn) is int and event_turn >= 0 else fallback_turn
        records.append(
            (
                turn,
                event["seq"],
                _interactive_event_timestamp(event),
                1,
                event,
            )
        )

    records.sort(
        key=lambda item: (
            item[0],
            item[1],
            item[2] is None,
            item[2] if item[2] is not None else 0.0,
            item[3],
        )
    )
    merged: list[dict[str, Any]] = []
    for offset, (_turn, _local_seq, _timestamp, _source, event) in enumerate(records, 1):
        normalized = dict(event)
        normalized["seq"] = cursor.watermark + offset
        merged.append(normalized)
    next_cursor = EventCursor(
        watermark=cursor.watermark + len(merged),
        positions=tuple(sorted(next_positions.items())),
    )
    return merged, next_cursor


def read_events_with_cursor(
    session_dir: Path | str, cursor: EventCursor | None = None
) -> tuple[list[dict[str, Any]], EventCursor]:
    """Replay an interactive session with a per-store monotonic cursor."""
    if cursor is None:
        cursor = EventCursor()
    if not isinstance(cursor, EventCursor):
        raise TypeError("cursor must be an EventCursor")
    return _read_interactive_events_with_cursor(Path(session_dir), cursor)


def _read_interactive_events(session_dir: Path, after_seq: int) -> list[dict[str, Any]]:
    """Replay the legacy integer-watermark view of interactive turn stores.

    New monitor polling uses :func:`read_events_with_cursor`.  For this
    compatibility API, turn stores without parent records can still push the
    integer watermark into SQL by subtracting each earlier store's row count;
    late inserts require the explicit cursor because an integer has no room for
    one local position per store.
    """
    turn_stores = _interactive_turn_event_stores(session_dir)
    if not turn_stores:
        return read_events_file(session_dir / ".cambium" / "events.db", after_seq)

    root_event_db = session_dir / ".cambium" / "events.db"
    root_events = read_events_file(root_event_db)
    if root_events:
        # ``/compact`` is the one legacy path that can write the parent store;
        # retain its historical ordering and filtering semantics.
        records: list[tuple[int, int, float | None, int, dict[str, Any]]] = []
        for turn, event_db in turn_stores:
            try:
                events = read_events_file(
                    event_db,
                    busy_timeout_ms=_INTERACTIVE_READ_BUSY_TIMEOUT_MS,
                )
            except StoreError as exc:
                if not _transient_event_store_lock(exc):
                    raise
                continue
            for event in events:
                records.append((turn, event["seq"], _interactive_event_timestamp(event), 0, event))
        fallback_turn = turn_stores[-1][0] + 1
        for event in root_events:
            payload = event.get("payload")
            event_turn = payload.get("turn") if isinstance(payload, Mapping) else None
            turn = event_turn if type(event_turn) is int and event_turn >= 0 else fallback_turn
            records.append((turn, event["seq"], _interactive_event_timestamp(event), 1, event))
        records.sort(
            key=lambda item: (
                item[0],
                item[1],
                item[2] is None,
                item[2] if item[2] is not None else 0.0,
                item[3],
            )
        )
    else:
        records = []
        remaining = max(after_seq, 0)
        for turn, event_db in turn_stores:
            try:
                store_count = count_events_file(
                    event_db,
                    busy_timeout_ms=_INTERACTIVE_READ_BUSY_TIMEOUT_MS,
                )
                local_after = min(remaining, store_count)
                events = read_events_file(
                    event_db,
                    local_after,
                    busy_timeout_ms=_INTERACTIVE_READ_BUSY_TIMEOUT_MS,
                )
            except StoreError as exc:
                if not _transient_event_store_lock(exc):
                    raise
                continue
            remaining = max(remaining - store_count, 0)
            records.extend(
                (turn, event["seq"], _interactive_event_timestamp(event), 0, event)
                for event in events
            )

    records.sort(
        key=lambda item: (
            item[0],
            item[1],
            item[2] is None,
            item[2] if item[2] is not None else 0.0,
            item[3],
        )
    )
    merged: list[dict[str, Any]] = []
    sequence_base = 0 if root_events else max(after_seq, 0)
    for sequence, (_turn, _local_seq, _timestamp, _source, event) in enumerate(records, 1):
        normalized = dict(event)
        normalized["seq"] = sequence_base + sequence
        merged.append(normalized)
    return [event for event in merged if event["seq"] > after_seq]


def read_events(session_dir: Path | str, after_seq: int | EventCursor = 0) -> list[dict[str, Any]]:
    """Replay durable events; use :class:`EventCursor` for interactive polling.

    Passing an ``int`` preserves the historical public replay contract.
    Passing an ``EventCursor`` reads only rows newer than each store's local
    cursor; :func:`read_events_with_cursor` also returns the updated cursor.
    """
    if isinstance(after_seq, EventCursor):
        return read_events_with_cursor(session_dir, after_seq)[0]
    return _read_interactive_events(Path(session_dir), after_seq)


@dataclass(frozen=True, slots=True)
class TaskResult:
    """Outcome of one planned task."""

    task_id: str
    status: str  # "succeeded" | "failed"
    exit_code: int  # 0 succeeded; 1 failed
    reason: str | None = None
    merge_sha: str | None = None
    restarts: int = 0
    summary: str | None = None
    provider: str | None = None
    fell_back_from: str | None = None
    salvage_ref: str | None = None


@dataclass(frozen=True, slots=True)
class PlanResult:
    """Aggregate outcome of a run_plan session."""

    results: tuple[TaskResult, ...]

    @property
    def exit_code(self) -> int:
        if not self.results:
            return 1
        return 0 if all(r.status == "succeeded" for r in self.results) else 1


def _envelope_text(envelope: Mapping[str, Any] | None, key: str) -> str | None:
    """Return one non-empty, already-redacted worker envelope string."""
    value = envelope.get(key) if envelope is not None else None
    return value if isinstance(value, str) and value else None


def _worker_failure_reason(
    envelope: Mapping[str, Any] | None,
    fallback: str | None,
    stderr_tail: str | None,
) -> str | None:
    """Prefer the worker's reason and retain the last redacted stderr line."""
    reason = _envelope_text(envelope, "failure_reason") or fallback
    if not stderr_tail:
        return reason
    detail = f"stderr: {stderr_tail}"
    return f"{reason}; {detail}" if reason else detail


def _sandbox_usage_reason(value: Any) -> str | None:
    """Return a worker usage reason that carries the sandbox outcome."""
    if not isinstance(value, str):
        return None
    lowered = value.casefold()
    return value if "sandbox_restricted" in lowered else None


def _finite_metric_score(value: Any) -> int | float | None:
    """Return a JSON-safe metric score, never a non-finite number."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float) and math.isfinite(value):
        return value
    return None


def _bounded_metric_value(value: Any, depth: int = 0) -> tuple[Any, bool]:
    """Bound one JSON-like metric value without accepting NaN or infinity."""
    if isinstance(value, str):
        capped = _cap_utf8(value, MAX_ENVELOPE_FIELD_CHARS)
        return capped, capped != value
    if value is None or isinstance(value, bool):
        return value, False
    if isinstance(value, int):
        return value, False
    if isinstance(value, float):
        return (value, False) if math.isfinite(value) else (None, True)
    if depth >= 3:
        return None, True
    if isinstance(value, list):
        bounded: list[Any] = []
        truncated = len(value) > MAX_ENVELOPE_ITEMS
        for item in value[:MAX_ENVELOPE_ITEMS]:
            safe, item_truncated = _bounded_metric_value(item, depth + 1)
            bounded.append(safe)
            truncated = truncated or item_truncated
        return bounded, truncated
    if isinstance(value, dict):
        bounded_dict: dict[str, Any] = {}
        truncated = False
        for raw_key in sorted(value, key=lambda key: str(key))[:MAX_ENVELOPE_ITEMS]:
            if not isinstance(raw_key, str):
                truncated = True
                continue
            key = _cap_utf8(raw_key, MAX_ENVELOPE_FIELD_CHARS)
            safe, item_truncated = _bounded_metric_value(value[raw_key], depth + 1)
            bounded_dict[key] = safe
            truncated = truncated or item_truncated or key != raw_key
        if len(value) > MAX_ENVELOPE_ITEMS:
            truncated = True
        return bounded_dict, truncated
    return None, True


def _bounded_metric_breakdown(value: Any) -> dict[str, Any]:
    """Deterministically cap metric breakdowns to the envelope field budget.

    ``metric_breakdown`` is part of the fixed nine-key envelope, so the
    truncation marker lives inside the mapping instead of adding a tenth key.
    Entries are considered in lexical key order and the marker is retained
    whenever any entry or value was dropped.
    """
    if not isinstance(value, dict):
        return {}
    bounded: dict[str, Any] = {}
    truncated = False
    for raw_key in sorted(value, key=lambda key: str(key)):
        if len(bounded) >= MAX_ENVELOPE_ITEMS - 1:
            truncated = True
            break
        if not isinstance(raw_key, str):
            truncated = True
            continue
        key = _cap_utf8(raw_key, MAX_ENVELOPE_FIELD_CHARS)
        safe, item_truncated = _bounded_metric_value(value[raw_key])
        candidate = {**bounded, key: safe}
        try:
            encoded_size = len(
                json.dumps(
                    {**candidate, "_truncated": True},
                    sort_keys=True,
                    ensure_ascii=False,
                    allow_nan=False,
                ).encode("utf-8")
            )
        except (TypeError, ValueError, UnicodeEncodeError):
            truncated = True
            continue
        if encoded_size > MAX_ENVELOPE_FIELD_CHARS:
            truncated = True
            continue
        bounded = candidate
        truncated = truncated or item_truncated or key != raw_key
    if not truncated:
        return bounded
    bounded["_truncated"] = True
    while len(bounded) > 1:
        try:
            encoded_size = len(
                json.dumps(
                    bounded,
                    sort_keys=True,
                    ensure_ascii=False,
                    allow_nan=False,
                ).encode("utf-8")
            )
        except (TypeError, ValueError, UnicodeEncodeError):
            encoded_size = MAX_ENVELOPE_FIELD_CHARS + 1
        if encoded_size <= MAX_ENVELOPE_FIELD_CHARS:
            break
        removable = next((key for key in reversed(list(bounded)) if key != "_truncated"), None)
        if removable is None:
            break
        del bounded[removable]
    return bounded if len(bounded) > 1 else {"_truncated": True}


def _bounded_strict_envelope(envelope: Mapping[str, Any]) -> dict[str, Any]:
    """Produce the bounded, exact nine-key parent-result envelope."""
    raw_diff = envelope.get("unified_diff", envelope.get("diff", ""))
    diff = raw_diff if isinstance(raw_diff, str) else ""
    bounded_diff = _cap_utf8(diff, MAX_ENVELOPE_FIELD_CHARS)
    raw_summary = envelope.get("summary", "")
    summary = raw_summary if isinstance(raw_summary, str) else ""
    raw_status = envelope.get("status", "failed")
    status = raw_status if isinstance(raw_status, str) else "failed"

    def bounded_strings(value: Any) -> list[str]:
        if not isinstance(value, list):
            return []
        return [
            _cap_utf8(item, MAX_ENVELOPE_FIELD_CHARS)
            for item in value[:MAX_ENVELOPE_ITEMS]
            if isinstance(item, str)
        ]

    parent_task_id = envelope.get("parent_task_id")
    if not (parent_task_id is None or isinstance(parent_task_id, str)):
        parent_task_id = None
    return {
        "parent_task_id": parent_task_id,
        "unified_diff": bounded_diff,
        "diff_truncated": bool(envelope.get("diff_truncated", False)) or bounded_diff != diff,
        "summary": _cap_utf8(summary, MAX_ENVELOPE_FIELD_CHARS),
        "metric_score": _finite_metric_score(envelope.get("metric_score")),
        "metric_breakdown": _bounded_metric_breakdown(envelope.get("metric_breakdown")),
        "commits": bounded_strings(envelope.get("commits")),
        "files_changed": bounded_strings(envelope.get("files_changed")),
        "status": _cap_utf8(status, MAX_ENVELOPE_FIELD_CHARS),
    }


def _bounded_resume_envelope(envelope: Mapping[str, Any]) -> dict[str, Any]:
    """Normalize one child-result envelope to the strict parent-envelope caps."""
    return _bounded_strict_envelope(envelope)


def _success_invariant_violation(
    spec: Mapping[str, Any], envelope: Mapping[str, Any], actual_head: str
) -> bool:
    """Return whether a successful envelope disagrees with its worktree."""
    requires_commit = envelope.get("requires_commit", False)
    if type(requires_commit) is not bool:
        return True
    commits = envelope.get("commits", [])
    files_changed = envelope.get("files_changed", [])
    unified_diff = envelope.get("diff", envelope.get("unified_diff", ""))
    if (
        not isinstance(commits, list)
        or not all(isinstance(commit, str) for commit in commits)
        or not isinstance(files_changed, list)
        or not all(isinstance(path, str) for path in files_changed)
        or not isinstance(unified_diff, str)
    ):
        return True
    base_head = spec.get("base_commit")
    if actual_head != base_head:
        return not commits or commits[-1] != actual_head
    return bool(commits) or bool(files_changed) or unified_diff != "" or requires_commit


class DuplicateTaskIDError(ValueError):
    """The plan cannot be dispatched because a task id is repeated."""


class InvalidBaseCommitError(ValueError):
    """A task base does not resolve to a commit in its repository."""


class NoCredentialFeasibleProvidersError(ValueError):
    """No authorized provider has a credential usable at admission."""


class WorktreeRecoveryError(RuntimeError):
    """A destructive worktree recovery command failed."""


class ResolverJoinInvariantError(RuntimeError):
    """A resolver lost the parent join barrier before its ref publication."""


class ConversationAppendError(RuntimeError):
    """A revision could not be persisted to the conversation store."""


class SessionAlreadyRunningError(RuntimeError):
    """Another supervisor already owns the requested session."""


class ArchitectusAdmissionPort:
    """Adapt an ``ArchitectusCore`` decision model to the ``propose_child`` wire shape.

    ``aggregate`` records an admitted parent's terminal envelope in the core;
    ``step`` runs one decision wave and maps the core's accepted ``spawn``
    actions into ``propose_child``-shaped proposals ({request_id,
    parent_task_id, child_task_id, kind, spec}) whose ``kind`` and ``spec``
    are recovered from the core's frozen tree node. The supervisor routes
    every returned proposal through the existing ``_admit_child`` revision
    validation, so a provider response never mutates the live session tree
    directly.
    """

    def __init__(self, core: ArchitectusCore) -> None:
        if not isinstance(core, ArchitectusCore):
            raise TypeError("architectus core must be a cambium.architectus.ArchitectusCore")
        self._core = core
        self._nodes = {node.task_id: node for node in core.tree.nodes}
        self._seq = 0

    def aggregate(self, task_id: str, envelope: dict[str, Any]) -> None:
        """Accept one admitted parent's strict-key envelope into the core."""
        self._core.aggregate(task_id, envelope)

    async def step(self, events: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Run one decision wave; return the typed proposals for the runtime."""
        actions = await self._core.step(events)
        proposals: list[dict[str, Any]] = []
        for action in actions:
            if not isinstance(action, dict) or action.get("action") != ActionKind.SPAWN.value:
                continue
            task_id = action.get("task_id")
            if not isinstance(task_id, str) or task_id not in self._nodes:
                raise ValueError(
                    f"decision port spawn action references unknown task_id {task_id!r}"
                )
            node = self._nodes[task_id]
            self._seq += 1
            proposals.append(
                {
                    "request_id": make_request_id(self._seq),
                    "parent_task_id": node.parent_task_id,
                    "child_task_id": task_id,
                    "kind": node.kind.value,
                    "spec": copy.deepcopy(node.spec),
                }
            )
        return proposals


class _SessionAdmission:
    """Process-wide and cross-process ownership of one session directory.

    The lock file also carries the owning supervisor PID. ``flock`` releases
    automatically after SIGKILL, but the PID remains durable long enough for
    the next supervisor to identify and reclaim the interrupted session.
    """

    def __init__(self, session_dir: Path) -> None:
        self._path = session_dir.resolve() / ".cambium" / "session.lock"
        self._fd: int | None = None
        self.previous_owner_pid: int | None = None

    @staticmethod
    def _read_owner_pid(fd: int) -> int | None:
        try:
            os.lseek(fd, 0, os.SEEK_SET)
            raw = os.read(fd, 64).decode("ascii").strip()
            pid = int(raw, 10)
        except (OSError, UnicodeError, ValueError):
            return None
        return pid if pid > 0 else None

    @staticmethod
    def _write_owner_pid(fd: int, pid: int | None) -> None:
        value = "" if pid is None else f"{pid}\n"
        encoded = value.encode("ascii")
        os.ftruncate(fd, 0)
        os.lseek(fd, 0, os.SEEK_SET)
        if encoded:
            os.write(fd, encoded)
        os.fsync(fd)

    def acquire(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        fd = os.open(self._path, os.O_RDWR | os.O_CREAT | os.O_CLOEXEC, 0o600)
        try:
            os.fchmod(fd, 0o600)
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            self.previous_owner_pid = self._read_owner_pid(fd)
            self._write_owner_pid(fd, os.getpid())
        except BlockingIOError as exc:
            os.close(fd)
            raise SessionAlreadyRunningError(
                f"session is already running: {self._path.parent.parent}"
            ) from exc
        except BaseException:
            try:
                fcntl.flock(fd, fcntl.LOCK_UN)
            finally:
                os.close(fd)
            raise
        self._fd = fd

    def release(self) -> None:
        if self._fd is None:
            return
        fd = self._fd
        self._fd = None
        try:
            try:
                self._write_owner_pid(fd, None)
            except OSError:
                pass
            fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)


@dataclass(slots=True)
class WorkerHandle:
    """Loop-affine per-generation worker state (custos design §3.1)."""

    task_id: str
    generation: int
    proc: asyncio.subprocess.Process | None = None
    state: str = "PENDING"  # PENDING | SPAWNING | RUNNING | EXITED
    exit_reason: str | None = None
    exit_code: int | None = None
    last_heartbeat: float | None = None


@dataclass(slots=True)
class _PooledWorker:
    """One idle reuse-ready worker in the session warm pool (eval-3 ADOPT).

    The pool is session-scoped on the ``_Runtime``; entries are matched by
    the exact spawn command and the task env modulo the per-task overrides,
    so a pooled process only serves tasks it can actually rebind to.
    """

    proc: asyncio.subprocess.Process
    cmd: tuple[str, ...]
    env_key: frozenset[tuple[str, str]]


@dataclass(frozen=True, slots=True)
class _GenOutcome:
    """Outcome of one generation's drive loop."""

    clean: bool  # worker delivered a terminal verdict (correlated envelope + exit message)
    fatal: bool = False  # restarting cannot help (spawn or terminal protocol error)
    reason: str | None = None
    timeout_phase: str | None = None
    exit_code: int | None = None
    exit_reason: str | None = None
    envelope: dict[str, Any] | None = None
    correlated: bool = False
    # Eval-3 ADOPT: true when the worker reported reuse-ready after its
    # terminal envelope and the live process was returned to the session pool.
    reuse_ready: bool = False
    # Dynamic admission: the child task ids admitted at this generation's
    # terminal envelope, in deterministic order (Cache-first step 2/§5.3).
    admitted_children: tuple[str, ...] = ()
    # Proposals observed by this generation. They are not admitted while the
    # worker's success verdict is still provisional: _supervise admits them
    # only after the supervisor's integrity/merge verdict (or, for a suspended
    # generation, before the bounded child wait).
    proposals: tuple[dict[str, Any], ...] = ()


@dataclass(slots=True)
class _GenerationState:
    """Mutable state shared by one generation's explicitly staged phases."""

    task_id: str
    spec: dict[str, Any]
    handle: WorkerHandle
    worktree: Path
    generation: int
    loop: asyncio.AbstractEventLoop
    wall_deadline: float
    cmd: list[str]
    env: dict[str, str]
    init_rid: str
    init_msg: dict[str, Any]
    proc: asyncio.subprocess.Process
    messages: asyncio.Queue[dict[str, Any] | None]
    heartbeat_timeout: float
    ready_deadline: float = 0.0
    stdout_task: asyncio.Task[None] | None = None
    stderr_task: asyncio.Task[None] | None = None
    parse_errors: int = 0
    message_too_long: bool = False
    stderr_tail: str | None = None
    phase: Any = "ready"
    last_heartbeat: float | None = None
    turn: int = 0
    run_rid: str | None = None
    envelope: dict[str, Any] | None = None
    exit_reason: str | None = None
    correlated: bool = False
    protocol_reason: str | None = None
    protocol_failure: str | None = None
    timeout_phase: str | None = None
    sandbox_failure_reason: str | None = None
    reuse_ready: bool = False
    keep_alive: bool = False


class _Runtime:
    """Multi-worker supervisor. One instance per run_plan session.

    All WorkerHandle mutation happens on the event loop from the supervise
    tasks; disk I/O (event store writes, git calls) escapes via asyncio.to_thread.
    """

    def __init__(
        self,
        session_dir: Path,
        store: Any,
        on_event: EventSink | None = None,
        *,
        redactor: Redactor | None = None,
        resource_thresholds: dict[str, Any] | None = None,
        provider_environment: Mapping[str, str] | None = None,
        max_concurrent_tasks: int | None = 0,
        debt_store: DebtStore | None = None,
        oauth_store: OAuthStore | None = None,
        architectus: Any = None,
        conversations: Any = None,
        warm_pool_size: int = 0,
        context_reuse: bool = True,
        resolver_child_enabled: bool = False,
        resolver_max_attempts: int = 1,
        orphan_owner_pid: int | None = None,
    ) -> None:
        self._session_dir = Path(session_dir)
        self._store = store
        self._on_event = on_event
        self._redactor = redactor
        self._resource_thresholds = (
            None if resource_thresholds is None else dict(resource_thresholds)
        )
        self._provider_environment = (
            None if provider_environment is None else dict(provider_environment)
        )
        self._debt_store = debt_store
        self._oauth_store = oauth_store
        self._event_append_lock = asyncio.Lock()
        # max_concurrent_tasks=0 (and None for direct callers) disables the
        # cap; a positive value creates the admission semaphore.
        self._admission_semaphore = (
            None if not max_concurrent_tasks else asyncio.Semaphore(max_concurrent_tasks)
        )
        self._handles: dict[str, WorkerHandle] = {}
        self._results: dict[str, TaskResult] = {}
        self._task_envelopes: dict[str, dict[str, Any]] = {}
        self._salvage_refs: dict[str, str] = {}
        self._salvaged_generations: set[tuple[str, int]] = set()
        self._worktree_lock = asyncio.Lock()
        self._merge_lock = asyncio.Lock()
        self._rid = 0
        self._last_envelope: dict[str, Any] | None = None
        # Dynamic child admission state (implementation-plan step 2).
        self._session_tasks: list[dict[str, Any]] = []
        # Each proposal is tagged with the generation that emitted it. A
        # proposal must never cross a restart boundary, and rejected terminal
        # envelopes must explicitly reject rather than silently dropping it.
        self._pending_children: dict[str, list[tuple[int, dict[str, Any]]]] = {}
        self._child_envelopes: dict[str, list[dict[str, Any]]] = {}
        self._task_group: asyncio.TaskGroup | None = None
        # Per-provider concurrency lanes (H1): in-flight admission counts and
        # rpm-derived caps; incremented at admission, released in
        # ``supervise_task``'s finally on every exit path.
        self._lanes: dict[str, LaneState] = {}
        # Eval-3 ADOPT warm pool: idle reuse-ready worker processes. The pool
        # is bounded by ``_warm_pool_size`` (0 disables) and never survives
        # this runtime (shutdown kills every pooled process).
        self._warm_pool_size = warm_pool_size
        self._pool: list[_PooledWorker] = []
        # Decision port and revision conversation persistence (step 2 items
        # 23-24): both optional; None keeps the historical byte-for-byte path.
        self._admission_port = self._make_admission_port(architectus)
        self._admission_port_lock = asyncio.Lock()
        self._conversations = conversations
        self._orphan_owner_pid = orphan_owner_pid
        # Cache-first context reuse (step 2): session-level flag; per-parent
        # epoch checkpoints keyed by task id, the admitted child ids per
        # parent in admission order, and the strict child-result envelope per
        # child task, captured at the child's terminal result envelope.
        self._context_reuse = context_reuse
        if type(resolver_child_enabled) is not bool:
            raise ValueError("resolver_child_enabled must be a boolean")
        if type(resolver_max_attempts) is not int or resolver_max_attempts < 0:
            raise ValueError("resolver_max_attempts must be a non-negative int")
        # Conflict resolution is deliberately opt-in.  The attempt budget is
        # session-scoped, with an optional per-task override validated at
        # admission; a fresh resolver worktree is created for every attempt.
        self._resolver_child_enabled = resolver_child_enabled
        self._resolver_max_attempts = resolver_max_attempts
        self._task_epochs: dict[str, dict[str, Any]] = {}
        # Completion futures are registered before dynamic child tasks are
        # created.  A suspended parent waits on these futures, not on a
        # post-spawn task lookup, so a child cannot finish between admission
        # and registration.
        self._child_tasks: dict[str, list[asyncio.Future[dict[str, Any]]]] = {}
        self._child_completion: dict[str, asyncio.Future[dict[str, Any]]] = {}
        self._child_parent: dict[str, str] = {}
        self._admitted_children: dict[str, list[str]] = {}
        self._child_result_by_task: dict[str, dict[str, Any]] = {}
        self._child_result_meta: dict[str, tuple[str | None, int | None]] = {}
        self._child_result_by_generation: dict[
            str, dict[int, tuple[dict[str, Any], tuple[str | None, int | None]]]
        ] = {}
        # Accepted child publication heads, keyed by the suspended parent.
        # These are consumed by the join barrier immediately before a parent
        # resume; retaining only the latest head makes repeated child joins
        # deterministic while avoiding a stale check on a later epoch.
        self._accepted_integration_heads: dict[str, str] = {}
        # Conflict envelopes are retained until _supervise has had a chance to
        # route an opt-in resolver child.  The merge API still returns None on
        # conflict for compatibility with direct callers.
        self._merge_conflicts: dict[str, dict[str, Any]] = {}
        self._resolver_failures: dict[str, str] = {}
        self._cancelled_tasks: set[str] = set()
        # A task's normal finally block owns its first cleanup attempt.  The
        # shutdown pass only retries cancellation cleanup for admitted child
        # coroutines that never reached that block; otherwise ordinary deferred
        # cleanup would be reported twice.
        self._cleanup_attempted: set[str] = set()
        self._child_result_emitted: set[str] = set()

    @staticmethod
    def _make_admission_port(architectus: Any) -> Any:
        """Normalize the optional decision port to an ``aggregate``/``step`` seam.

        An ``ArchitectusCore`` is adapted to the ``propose_child`` wire shape;
        any object already exposing ``aggregate``/``step`` is used directly as
        a caller-provided port. ``None`` disables provider-side admission.
        """
        if architectus is None:
            return None
        if isinstance(architectus, ArchitectusCore):
            return ArchitectusAdmissionPort(architectus)
        if hasattr(architectus, "aggregate") and hasattr(architectus, "step"):
            return architectus
        raise TypeError("architectus must be an ArchitectusCore or a port with aggregate()/step()")

    @property
    def last_envelope(self) -> dict[str, Any] | None:
        """The terminal correlated worker envelope, redacted before retention.

        Retained from ``_GenOutcome.envelope`` until after shutdown so the
        session result can reuse its sanitized commits/files/diff/summary
        while the supervisor verdict stays authoritative.
        """
        return self._last_envelope

    def _redact_envelope(self, envelope: dict[str, Any]) -> dict[str, Any]:
        if self._redactor is None:
            return dict(envelope)
        redacted = self._redactor.redact_protocol_record(
            envelope, structural_fields=WORKER_RESULT_STRUCTURAL_FIELDS
        )
        return cast(dict[str, Any], redacted)

    # -- event path ---------------------------------------------------------

    def _next_rid(self) -> str:
        self._rid += 1
        return make_request_id(self._rid)

    async def emit(
        self,
        kind: str,
        *,
        task_id: str | None = None,
        generation: int | None = None,
        request_id: str | None = None,
        _observer_failure_is_fatal: bool | None = None,
        _deferred_observers: list[tuple[dict[str, Any], bool]] | None = None,
        **payload: Any,
    ) -> None:
        record: dict[str, Any] = {
            "kind": kind,
            "task_id": task_id,
            "worker_id": f"{task_id}:{generation}" if generation is not None else task_id,
            "generation": generation,
            "request_id": request_id,
            "ts": time.time(),
            "monotonic_ms": time.monotonic_ns() // 1_000_000,
            "payload": dict(payload),
        }
        if self._redactor is not None:
            redacted_record = self._redactor.redact_protocol_record(
                record, structural_fields=EVENT_RECORD_STRUCTURAL_FIELDS
            )
            record = cast(dict[str, Any], redacted_record)
            kind = cast(str, record["kind"])
        durable_record = self._copy_event(record)
        if self._store is not None:
            try:
                async with self._event_append_lock:
                    await asyncio.to_thread(self._store.append, durable_record)
            except (OSError, RuntimeError, StoreError, TypeError, ValueError) as exc:
                if kind in _CRITICAL_EVENT_KINDS:
                    raise
                print(f"cambium: event store error: {exc}", file=sys.stderr)
        if self._on_event is None:
            return
        observer_failure_is_fatal = (
            _observer_failure_is_fatal
            if _observer_failure_is_fatal is not None
            else kind not in _CRITICAL_EVENT_KINDS
        )
        observer_record = self._copy_event(record)
        if _deferred_observers is not None:
            _deferred_observers.append((observer_record, observer_failure_is_fatal))
            return
        await self._notify_observer(observer_record, observer_failure_is_fatal)

    @staticmethod
    def _copy_event(record: dict[str, Any]) -> dict[str, Any]:
        copied = dict(record)
        payload = record.get("payload")
        if isinstance(payload, dict):
            copied["payload"] = dict(payload)
        return copied

    async def _notify_observer(
        self, record: dict[str, Any], observer_failure_is_fatal: bool
    ) -> None:
        if self._on_event is None:
            return
        try:
            result = self._on_event(record)
            if asyncio.iscoroutine(result):
                await result
        except asyncio.CancelledError:
            task = asyncio.current_task()
            if task is not None and task.cancelling():
                raise
            if observer_failure_is_fatal:
                raise
        except BaseException:
            if observer_failure_is_fatal:
                raise

    async def _notify_deferred_observers(self, deferred: list[tuple[dict[str, Any], bool]]) -> None:
        for record, observer_failure_is_fatal in deferred:
            await self._notify_observer(record, observer_failure_is_fatal)

    async def reclaim_orphaned_worktrees(self, specs: list[dict[str, Any]]) -> None:
        """Reclaim worktrees left by a supervisor that died without cleanup.

        ``flock`` makes a session lock immediately available after SIGKILL, but
        the PID written to that lock survives. A new owner can therefore make a
        causal startup decision instead of guessing from a directory's mtime:
        only a dead previous owner triggers this destructive pass, while the
        event stream supplies the audit sequence for each terminated task. The
        normal terminal cleanup contract (including
        deferred dirty evidence trees) is untouched for completed sessions.
        """
        owner_pid = self._orphan_owner_pid
        self._orphan_owner_pid = None
        if owner_pid is None or process_is_alive(owner_pid):
            return

        events = await asyncio.to_thread(self._store.events_after, 0)
        terminal_session_seqs = [
            event["seq"]
            for event in events
            if event.get("kind") == "session_ended" and type(event.get("seq")) is int
        ]
        last_session_end = max(terminal_session_seqs, default=0)
        post_crash = [
            event
            for event in events
            if type(event.get("seq")) is int and event["seq"] > last_session_end
        ]
        task_events: dict[str, list[dict[str, Any]]] = {}
        active_kinds = frozenset(
            {
                "task_assigned",
                "spawned",
                "init",
                "ready",
                "run_task",
                "heartbeat",
                "timeout",
                "result",
                "exit",
                "worker_failed",
                "task_failed",
                "merge_started",
                "merge_committed",
            }
        )
        for event in post_crash:
            task_id = event.get("task_id")
            if isinstance(task_id, str) and event.get("kind") in active_kinds:
                task_events.setdefault(task_id, []).append(event)

        # A crash can happen between worktree creation and task_assigned. In
        # that case the stale owner is the only durable ownership evidence, so
        # inspect every path in the accepted plan. Once a task event exists,
        # retain its event sequence numbers in the termination marker for
        # audit/replay consumers.
        for spec in specs:
            task_id = spec["task_id"]
            history = task_events.get(task_id, [])
            worktree = Path(spec["worktree_path"]).resolve()
            if not history and not worktree.exists():
                continue
            ready_events = [
                event
                for event in history
                if event.get("kind") in {"ready", "reuse_ready"}
                and isinstance(event.get("payload", {}).get("pid"), int)
            ]
            worker_pid = ready_events[-1]["payload"]["pid"] if ready_events else None
            generations = [
                event["generation"] for event in history if type(event.get("generation")) is int
            ]
            terminated_event_seqs = [
                event["seq"] for event in history if type(event.get("seq")) is int
            ]
            await self.emit(
                "worker_terminated",
                task_id=task_id,
                generation=max(generations, default=None),
                pid=worker_pid,
                supervisor_pid=owner_pid,
                status="terminated",
                reason="orphaned_supervisor",
                terminated_event_seqs=terminated_event_seqs[-MAX_ENVELOPE_ITEMS:],
                terminated_event_count=len(terminated_event_seqs),
            )
            await self._prune_worktree(spec, force=True)

            # ``_prune_worktree`` intentionally ignores an unregistered path;
            # remove a session-owned directory left between ``git worktree
            # add`` and registration only after confirming it is still not a
            # registered worktree. This closes the crash window before the
            # first durable worker event without touching another worktree.
            if worktree.exists():
                repo = Path(spec["repo"]).resolve()
                listing = await self._git(repo, "worktree", "list", "--porcelain", check=False)
                if listing.returncode == 0:
                    registered = any(
                        line.startswith("worktree ")
                        and Path(line.removeprefix("worktree ").strip()).resolve() == worktree
                        for line in listing.stdout.splitlines()
                    )
                    if not registered:
                        await self._salvage_worktree(spec, generation=max(generations, default=1))
                        shutil.rmtree(worktree, ignore_errors=True)
                        await self._git(repo, "branch", "-D", spec["branch"], check=False)

    async def start(self) -> None:
        return

    async def shutdown(self, session_status: str = "ended") -> None:
        """Steps 2-8 of the custos shutdown sequence (design §4)."""
        alive = [
            h.proc
            for h in self._handles.values()
            if h.proc is not None and h.proc.returncode is None
        ]
        # Eval-3 ADOPT pool hygiene: idle pooled workers are killed with the
        # session; a pooled process that already died is dropped silently.
        alive += [entry.proc for entry in self._pool if entry.proc.returncode is None]
        self._pool.clear()
        unique_alive = list({id(proc): proc for proc in alive}.values())
        for proc in unique_alive:
            try:
                os.killpg(proc.pid, signal.SIGTERM)
            except (ProcessLookupError, PermissionError, OSError):
                pass
        if unique_alive:
            try:
                await asyncio.wait_for(
                    asyncio.gather(
                        *(proc.wait() for proc in unique_alive),
                        return_exceptions=True,
                    ),
                    TERM_GRACE_S,
                )
            except TimeoutError:
                for proc in unique_alive:
                    if proc.returncode is not None:
                        continue
                    try:
                        os.killpg(proc.pid, signal.SIGKILL)
                    except (ProcessLookupError, PermissionError, OSError):
                        pass
                    try:
                        proc.kill()
                    except ProcessLookupError:
                        pass
                await asyncio.gather(
                    *(proc.wait() for proc in unique_alive), return_exceptions=True
                )

        # A task can be cancelled before its coroutine gets a chance to enter
        # supervise_task (especially a dynamically admitted child). Account for
        # every admitted spec before publishing the session terminal event.
        task_specs = {
            spec["task_id"]: spec
            for entry in self._session_tasks
            if isinstance((spec := entry.get("spec")), dict)
        }
        for task_id in task_specs:
            if task_id in self._results:
                continue
            status = "cancelled" if session_status == "cancelled" else "failed"
            if status == "cancelled":
                self._cancelled_tasks.add(task_id)
            self._results[task_id] = TaskResult(
                task_id=task_id,
                status=status,
                exit_code=1,
                reason="cancelled" if status == "cancelled" else "supervision ended",
            )

        # Cleanup is normally attempted by each task's finally block.  Only
        # retry cancellation cleanup for an admitted task whose coroutine never
        # reached that block; re-running ordinary terminal cleanup would emit a
        # second deferred record for retained evidence trees.
        for spec in task_specs.values():
            task_id = spec["task_id"]
            result = self._results.get(task_id)
            if not (
                task_id in self._cancelled_tasks
                or (result is not None and result.status == "cancelled")
            ):
                continue
            if task_id in self._cleanup_attempted:
                continue
            try:
                await self._prune_worktree(spec, force=True)
                self._cleanup_attempted.add(task_id)
            except (OSError, RuntimeError, TypeError, ValueError):
                pass
        try:
            await self.emit(
                "session_ended",
                task_id=None,
                session_status=session_status,
                results={tid: r.status for tid, r in self._results.items()},
            )
        except BaseException:
            pass
        await asyncio.to_thread(self._store.close)
        if self._conversations is not None:
            await asyncio.to_thread(self._conversations.close)

    def plan_result(self) -> PlanResult:
        results: list[TaskResult] = []
        for task_id, result in self._results.items():
            salvage_ref = self._salvage_refs.get(task_id)
            if salvage_ref is not None and result.salvage_ref != salvage_ref:
                result = replace(result, salvage_ref=salvage_ref)
            envelope = self._task_envelopes.get(task_id)
            metadata = envelope.get("provider_metadata") if isinstance(envelope, dict) else None
            if isinstance(metadata, dict):
                provider = metadata.get("provider")
                fell_back_from = metadata.get("fell_back_from")
                if isinstance(provider, str) or isinstance(fell_back_from, str):
                    result = replace(
                        result,
                        provider=provider if isinstance(provider, str) else result.provider,
                        fell_back_from=(
                            fell_back_from
                            if isinstance(fell_back_from, str)
                            else result.fell_back_from
                        ),
                    )
            results.append(result)
        return PlanResult(results=tuple(results))

    # -- git plumbing (off the loop) -----------------------------------------

    def _git_sync(
        self, path: Path, args: tuple[str, ...], check: bool
    ) -> subprocess.CompletedProcess[str]:
        result = subprocess.run(
            ["git", "-C", str(path), *args],
            capture_output=True,
            text=True,
            env=_strip_sensitive_env(scrub_environment(), worktree=path),
            start_new_session=True,
        )
        if check and result.returncode != 0:
            raise RuntimeError(
                f"git {args[0]} failed (rc={result.returncode}) in {path}: "
                f"{(result.stderr + result.stdout).strip()[:512]}"
            )
        return result

    async def _git(
        self, path: Path, *args: str, check: bool = True
    ) -> subprocess.CompletedProcess[str]:
        return await asyncio.to_thread(self._git_sync, path, args, check)

    async def _git_stdout(self, path: Path, *args: str, check: bool = True) -> str | None:
        result = await self._git(path, *args, check=check)
        return result.stdout.strip() or None

    async def _git_diff_bytes(self, worktree: Path, base_commit: str) -> bytes:
        """Return the raw tracked diff used by salvage artifacts."""
        result = await asyncio.to_thread(
            subprocess.run,
            [
                "git",
                "-C",
                str(worktree),
                "diff",
                "--no-ext-diff",
                "--no-color",
                "--binary",
                base_commit,
                "--",
            ],
            capture_output=True,
            env=_strip_sensitive_env(scrub_environment(), worktree=worktree),
            start_new_session=True,
        )
        return result.stdout if result.returncode == 0 else b""

    def _retain_salvage_ref(self, task_id: str, salvage_ref: str) -> None:
        self._salvage_refs[task_id] = salvage_ref
        result = self._results.get(task_id)
        if result is not None and result.salvage_ref != salvage_ref:
            self._results[task_id] = replace(result, salvage_ref=salvage_ref)
        envelope = self._task_envelopes.get(task_id)
        if envelope is None:
            envelope = {
                "task_id": task_id,
                "status": result.status if result is not None else "failed",
            }
            self._task_envelopes[task_id] = envelope
            if self._last_envelope is None:
                self._last_envelope = envelope
        envelope["salvage_ref"] = salvage_ref

    async def _salvage_worktree(
        self,
        spec: dict[str, Any],
        *,
        generation: int | None = None,
        deferred_observers: list[tuple[dict[str, Any], bool]] | None = None,
    ) -> str | None:
        """Capture a dirty worktree before recovery or terminal cleanup."""
        task_id = spec["task_id"]
        worktree = Path(spec["worktree_path"]).resolve()
        if generation is None:
            handle = self._handles.get(task_id)
            generation = handle.generation if handle is not None else read_generation(worktree)
        generation = generation or 1
        key = (task_id, generation)
        existing = self._salvage_refs.get(task_id)
        if key in self._salvaged_generations:
            return existing
        if not worktree.is_dir():
            return None
        status = await self._git(
            worktree,
            "status",
            "--porcelain=v1",
            "--untracked-files=all",
            "--ignored=matching",
            check=False,
        )
        if status.returncode != 0 or not any(
            not _status_line_is_fence(line) for line in status.stdout.splitlines()
        ):
            return None
        base_commit = spec.get("base_commit")
        base = base_commit if isinstance(base_commit, str) and base_commit else "HEAD"
        diff = await self._git_diff_bytes(worktree, base)
        bounded, truncated = _bounded_salvage_diff(diff)
        salvage_ref = Path("salvage") / _safe_task_id(task_id) / str(generation) / "workspace.diff"
        directory = self._session_dir.resolve() / salvage_ref.parent
        metadata = {
            "task_id": task_id,
            "generation": generation,
            "base_commit": base,
            "branch": spec["branch"],
            "captured_at": time.time(),
            "truncated": truncated,
        }
        await asyncio.to_thread(_write_salvage_artifacts, directory, bounded, metadata)
        salvage_ref_text = salvage_ref.as_posix()
        self._salvaged_generations.add(key)
        self._retain_salvage_ref(task_id, salvage_ref_text)
        await self.emit(
            "worktree_salvaged",
            task_id=task_id,
            generation=generation,
            path=salvage_ref_text,
            bytes=len(bounded),
            _deferred_observers=deferred_observers,
        )
        return salvage_ref_text

    def _latest_turn_checkpoint(self, spec: Mapping[str, Any]) -> tuple[str, dict[str, Any]] | None:
        """Return the newest usable ordinary turn checkpoint for a task."""
        task_id = spec.get("task_id")
        if not isinstance(task_id, str) or not task_id:
            return None
        directory = (
            self._session_dir.resolve() / ".cambium" / "checkpoints" / _safe_task_id(task_id)
        )
        try:
            candidates = sorted(
                (path for path in directory.iterdir() if path.is_file() and not path.is_symlink()),
                key=lambda path: int(path.stem.removeprefix("turn-"))
                if re.fullmatch(r"turn-[0-9]+", path.stem)
                else -1,
                reverse=True,
            )
        except (FileNotFoundError, OSError, ValueError):
            return None
        for path in candidates:
            match = re.fullmatch(r"turn-(?P<turn>[0-9]+)\.json", path.name)
            if match is None:
                continue
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError, UnicodeDecodeError):
                continue
            if not isinstance(payload, dict):
                continue
            turn = payload.get("turn")
            workspace_hash = payload.get("workspace_hash")
            if (
                isinstance(turn, bool)
                or not isinstance(turn, int)
                or turn != int(match.group("turn"))
                or not isinstance(workspace_hash, str)
                or _SHA256_HEX_RE.fullmatch(workspace_hash) is None
            ):
                continue
            ref = Path(_safe_task_id(task_id)) / path.name
            return ref.as_posix(), payload
        return None

    async def _checkpoint_resume_payload(self, spec: dict[str, Any]) -> dict[str, Any] | None:
        """Build a resume envelope only when the worktree matches its checkpoint."""
        worktree = Path(spec["worktree_path"]).resolve()
        current_hash = await asyncio.to_thread(_workspace_hash, worktree)
        checkpoint = self._latest_turn_checkpoint(spec)
        if current_hash is None or checkpoint is None:
            return None
        checkpoint_ref, payload = checkpoint
        if payload.get("workspace_hash") != current_hash:
            return None
        turn = payload["turn"]
        return {
            "checkpoint_ref": checkpoint_ref,
            "epoch": turn,
            "child_results": [],
            "child_results_truncated": False,
            "workspace_changed": False,
        }

    async def _reuse_worktree(self, spec: dict[str, Any], generation: int) -> int:
        """Advance only the fence for a checkpoint-bound restart."""
        async with self._worktree_lock:
            worktree = Path(spec["worktree_path"]).resolve()
            if not worktree.exists():
                return await self._ensure_worktree_locked(spec, generation)
            persisted_generation = await asyncio.to_thread(next_generation, worktree)
            new_generation = max(persisted_generation, generation)
            await asyncio.to_thread(write_generation, worktree, new_generation)
            return new_generation

    @staticmethod
    def _registered_worktree_paths(listing: str) -> set[Path]:
        paths: set[Path] = set()
        for field in listing.split("\0"):
            if field.startswith("worktree "):
                paths.add(Path(field.removeprefix("worktree ")).resolve())
        return paths

    # -- worktree lifecycle --------------------------------------------------

    async def _ensure_worktree(self, spec: dict[str, Any]) -> int:
        async with self._worktree_lock:
            return await self._ensure_worktree_locked(spec)

    async def _ensure_worktree_locked(
        self, spec: dict[str, Any], generation: int | None = None
    ) -> int:
        repo = Path(spec["repo"]).resolve()
        worktree = Path(spec["worktree_path"]).resolve()
        if worktree == repo:
            raise WorktreeRecoveryError(f"worktree_path must not be the repo itself: {worktree}")
        branch = spec["branch"]
        base = spec["base_commit"]
        await self._git(repo, "worktree", "prune", check=False)
        listing = await self._git_stdout(repo, "worktree", "list", "--porcelain", "-z") or ""
        if worktree in self._registered_worktree_paths(listing):
            return await self._recover_worktree_locked(spec, generation)
        stale_generation = 0
        if worktree.exists():
            stale_generation = await asyncio.to_thread(read_generation, worktree)
        if worktree.exists():
            await self._salvage_worktree(spec, generation=stale_generation or generation)
            # stale unregistered directory; it is session-owned, so drop it
            shutil.rmtree(worktree, ignore_errors=True)
        await self._git(repo, "branch", "-D", branch, check=False)
        result = await self._git(
            repo, "worktree", "add", "-b", branch, str(worktree), base, check=False
        )
        if result.returncode != 0:
            raise RuntimeError(
                f"worktree add for {branch} failed: {(result.stderr + result.stdout).strip()[:512]}"
            )
        initial_generation = max(generation or 1, stale_generation + 1, 1)
        await asyncio.to_thread(write_generation, worktree, initial_generation)
        return initial_generation

    async def _recover_worktree(self, spec: dict[str, Any], generation: int | None = None) -> int:
        async with self._worktree_lock:
            return await self._recover_worktree_locked(spec, generation)

    async def _recover_worktree_locked(
        self, spec: dict[str, Any], generation: int | None = None
    ) -> int:
        """Worktree recovery before a respawn (arch §7.5): reset + clean."""
        repo = Path(spec["repo"]).resolve()
        worktree = Path(spec["worktree_path"]).resolve()
        if worktree == repo:
            raise WorktreeRecoveryError(f"worktree_path must not be the repo itself: {worktree}")
        await self._git(repo, "worktree", "prune", check=False)
        if not worktree.exists():
            return await self._ensure_worktree_locked(spec, generation)
        await self._salvage_worktree(spec, generation=read_generation(worktree) or generation)
        # Advance the durable token before touching the worktree, then exclude
        # the supervisor-owned fence directory from the destructive clean.
        # A supervisor crash after clean must leave this generation durable.
        persisted_generation = await asyncio.to_thread(next_generation, worktree)
        new_generation = max(persisted_generation, generation or 0)
        await asyncio.to_thread(write_generation, worktree, new_generation)
        for op in ("rebase", "merge", "cherry-pick"):
            await self._git(worktree, op, "--abort", check=False)
        for args in (
            ("reset", "--hard", spec["base_commit"]),
            ("clean", "-fd", "-e", ".cambium/"),
        ):
            result = await self._git(worktree, *args, check=False)
            if result.returncode != 0:
                detail = (result.stderr + result.stdout).strip()[:512]
                raise WorktreeRecoveryError(
                    f"git {args[0]} failed during recovery (rc={result.returncode}) "
                    f"in {worktree}: {detail}"
                )
        await self.emit(
            "recover",
            task_id=spec["task_id"],
            generation=new_generation,
            base_commit=spec["base_commit"],
        )
        return new_generation

    async def _prune_worktree(self, spec: dict[str, Any], *, force: bool = False) -> None:
        """Remove a terminal task's worker worktree and branch.

        Ordinary failures retain dirty trees as evidence. Cancellation is the
        explicit force path: no caller can safely consume partial edits after
        the worker has lost ownership of its generation.
        """
        task_id = spec["task_id"]
        repo = Path(spec["repo"]).resolve()
        worktree = Path(spec["worktree_path"]).resolve()
        branch = spec["branch"]

        deferred: list[tuple[dict[str, Any], bool]] = []
        try:
            async with self._worktree_lock:
                await self._git(repo, "worktree", "prune", check=False)
                listing = await self._git(repo, "worktree", "list", "--porcelain", check=False)
                if listing.returncode != 0:
                    await self.emit(
                        "worktree_cleanup_deferred",
                        task_id=task_id,
                        reason="list_failed",
                        _deferred_observers=deferred,
                    )
                    return
                registered = any(
                    line.startswith("worktree ")
                    and Path(line[len("worktree ") :].strip()).resolve() == worktree
                    for line in listing.stdout.splitlines()
                )
                if not registered:
                    return
                if worktree == repo:
                    await self.emit(
                        "worktree_cleanup_deferred",
                        task_id=task_id,
                        reason="repo_path",
                        _deferred_observers=deferred,
                    )
                    return
                if branch != "main":
                    branch_ref = f"branch refs/heads/{branch}"
                    for block in listing.stdout.split("\n\n"):
                        lines = block.splitlines()
                        path_line = next(
                            (line for line in lines if line.startswith("worktree ")), None
                        )
                        if path_line is None:
                            continue
                        registered_path = Path(path_line[len("worktree ") :].strip()).resolve()
                        if registered_path != worktree and branch_ref in lines:
                            await self.emit(
                                "worktree_cleanup_deferred",
                                task_id=task_id,
                                reason="branch_in_use",
                                _deferred_observers=deferred,
                            )
                            return

                handle = self._handles.get(task_id)
                cleanup_generation = (
                    handle.generation if handle is not None else read_generation(worktree)
                )
                await self._salvage_worktree(
                    spec,
                    generation=cleanup_generation or 1,
                    deferred_observers=deferred,
                )
                generation_invalidated = False
                if force:
                    try:
                        # Cancellation has no useful evidence contract. Move
                        # the durable fence first, before killing descendants
                        # or deleting anything, so a detached child that wins
                        # a process-group race cannot write with its old token.
                        await asyncio.to_thread(next_generation, worktree)
                        generation_invalidated = True
                    except (OSError, RuntimeError, ValueError):
                        await self.emit(
                            "worktree_cleanup_deferred",
                            task_id=task_id,
                            reason="generation_invalidation_failed",
                            _deferred_observers=deferred,
                        )
                        return
                if handle is not None and handle.proc is not None:
                    await _kill_worker(handle.proc)
                    try:
                        await asyncio.wait_for(handle.proc.wait(), WORKER_EXIT_WAIT_S)
                    except (TimeoutError, ProcessLookupError):
                        pass
                # Pooled workers keep their cwd inside their finished
                # worktree; killing them here would silently disable reuse
                # for every later task in the session.
                pooled_pgids = frozenset(
                    entry.proc.pid for entry in self._pool if entry.proc.returncode is None
                )
                await asyncio.to_thread(_kill_worktree_process_groups, worktree, pooled_pgids)
                status = await self._git(
                    worktree,
                    "status",
                    "--porcelain=v1",
                    "--untracked-files=all",
                    "--ignored=matching",
                    check=False,
                )
                if status.returncode != 0:
                    await self.emit(
                        "worktree_cleanup_deferred",
                        task_id=task_id,
                        reason="status_failed",
                        _deferred_observers=deferred,
                    )
                    return
                if not force and (
                    spec.get("_defer_cleanup") is True
                    or any(not _status_line_is_fence(line) for line in status.stdout.splitlines())
                ):
                    await self.emit(
                        "worktree_cleanup_deferred",
                        task_id=task_id,
                        reason="dirty",
                        _deferred_observers=deferred,
                    )
                    return

                if not generation_invalidated:
                    try:
                        # Invalidate the durable token while the tree is still
                        # registered, then remove the fence and worktree. A
                        # stale worker that survived the process-group sweep
                        # can no longer pass its next fenced write check.
                        await asyncio.to_thread(next_generation, worktree)
                        generation_invalidated = True
                    except (OSError, RuntimeError, ValueError):
                        await self.emit(
                            "worktree_cleanup_deferred",
                            task_id=task_id,
                            reason="generation_invalidation_failed",
                            _deferred_observers=deferred,
                        )
                        return
                fence_dir = worktree / ".cambium"
                if fence_dir.is_dir():
                    shutil.rmtree(fence_dir, ignore_errors=True)
                remove_args = (
                    ("worktree", "remove", "--force", str(worktree))
                    if force
                    else ("worktree", "remove", str(worktree))
                )
                removed = await self._git(repo, *remove_args, check=False)
                if removed.returncode != 0:
                    await self.emit(
                        "worktree_cleanup_deferred",
                        task_id=task_id,
                        reason="remove_failed",
                        _deferred_observers=deferred,
                    )
                    return
                if branch != "main":
                    deleted = await self._git(repo, "branch", "-D", branch, check=False)
                    if deleted.returncode != 0:
                        restored = await self._git(
                            repo, "worktree", "add", str(worktree), branch, check=False
                        )
                        if restored.returncode != 0:
                            restored = await self._git(
                                repo,
                                "worktree",
                                "add",
                                "--detach",
                                str(worktree),
                                branch,
                                check=False,
                            )
                        await self.emit(
                            "worktree_cleanup_deferred",
                            task_id=task_id,
                            reason="branch_delete_failed",
                            restored=restored.returncode == 0,
                            _deferred_observers=deferred,
                        )
                        return
                await self._git(repo, "worktree", "prune", check=False)
                await self.emit(
                    "worktree_pruned",
                    task_id=task_id,
                    branch=branch,
                    _deferred_observers=deferred,
                )
        finally:
            await self._notify_deferred_observers(deferred)

    # -- spawn environment ---------------------------------------------------

    def _worker_command(self, spec: dict[str, Any]) -> list[str]:
        worker = spec.get("worker")
        if worker is None or worker == "cambium.worker":
            if importlib.util.find_spec("cambium.worker") is None:
                raise ValueError(
                    f"task {spec.get('task_id')!r} has no 'worker' and the "
                    "cambium.worker module is not installed; add 'worker' (a script "
                    "path or the literal 'cambium.worker') to the plan task"
                )
            return [sys.executable, "-u", "-m", "cambium.worker"]
        return [sys.executable, "-u", str(worker)]

    def _worker_env(self: _Runtime | None, spec: dict[str, Any], generation: int) -> dict[str, str]:
        session_dir = self._session_dir if self is not None else None
        provider_environment = self._provider_environment if self is not None else None
        oauth_store = self._oauth_store if self is not None else None
        redactor = self._redactor if self is not None else None
        return _worker_environment(
            spec,
            generation,
            session_dir=session_dir,
            provider_environment=provider_environment,
            oauth_store=oauth_store,
            redactor=redactor,
        )

    def _run_payload(
        self, spec: dict[str, Any], wall_budget: float, generation: int
    ) -> dict[str, Any]:
        repo = Path(spec["repo"])
        payload = {
            "task_id": spec["task_id"],
            "task": spec.get("task", ""),
            "repo": str(repo),
            "scratch_repo": str(repo),
            "worktree_path": str(Path(spec["worktree_path"]).resolve()),
            "branch": spec["branch"],
            "base_commit": spec["base_commit"],
            "generation": generation,
            "max_turns": int(spec.get("max_turns", DEFAULT_MAX_TURNS)),
            "max_tokens": int(spec.get("max_tokens", DEFAULT_MAX_TOKENS)),
            "max_wall_s": wall_budget,
        }
        if isinstance(spec.get("requirements"), dict) and spec["requirements"]:
            payload["requirements"] = dict(spec["requirements"])
        if spec.get("requires_commit") is not None:
            payload["requires_commit"] = bool(spec["requires_commit"])
        if spec.get("fanout_config") is None:
            write_marker = spec.get("write_marker", True)
            if not isinstance(write_marker, bool):
                raise ValueError(f"task {spec['task_id']} write_marker must be a boolean")
            payload.update(
                target_file=spec.get("target_file"),
                marker=spec.get("marker"),
                write_marker=write_marker,
            )
        # Dynamic child admission: the child's context is its own spec plus
        # the parent's envelope; a child may itself declare proposals.
        if spec.get("parent_envelope") is not None:
            payload["parent_envelope"] = spec["parent_envelope"]
        if spec.get("proposed_children") is not None:
            payload["proposed_children"] = spec["proposed_children"]
        # Cache-first resume: the payload carries the bounded resume dict so a
        # restarted (or resumed) parent re-seeds its transcript from the epoch.
        if spec.get("resume") is not None:
            payload["resume"] = spec["resume"]
        if isinstance(spec.get("summary_trunk_ref"), str):
            payload["summary_trunk_ref"] = spec["summary_trunk_ref"]
        if spec.get("_resolver_child"):
            # Resolver context is explicit and bounded.  Do not rely on the
            # worker inferring the conflict from a dirty index: the dedicated
            # child receives the two parent intents and the evidence that led
            # to its admission, while the staged worktree grants it the only
            # write authority it needs.
            resolver = {
                "conflicted_files": list(spec.get("conflicted_files", ()))[:MAX_ENVELOPE_ITEMS],
                "diff_evidence": _cap_utf8(
                    spec.get("diff_evidence", "")
                    if isinstance(spec.get("diff_evidence", ""), str)
                    else "",
                    MAX_ENVELOPE_FIELD_CHARS,
                ),
                "diff_truncated": bool(spec.get("diff_truncated", False)),
                "parent_intent_summaries": dict(
                    spec.get("parent_intent_summaries", {})
                    if isinstance(spec.get("parent_intent_summaries"), dict)
                    else {}
                ),
                "source_branch": spec.get("_resolver_source_branch"),
                "integration_head": spec.get("resolver_integration_head"),
                "attempt": spec.get("resolver_attempt"),
                "max_attempts": spec.get("resolver_max_attempts"),
            }
            payload["resolver"] = resolver
            # Keep the fields available to small resolver workers that do not
            # consume the nested object yet; all values have the same caps.
            payload["conflicted_files"] = resolver["conflicted_files"]
            payload["diff_evidence"] = resolver["diff_evidence"]
            payload["parent_intent_summaries"] = resolver["parent_intent_summaries"]
        return payload

    # -- dynamic child admission (implementation-plan step 2) ----------------

    def set_session_tasks(self, specs: list[dict[str, Any]]) -> None:
        """Seed the accumulated session task list used for revision validation.

        Plan tasks are the session roots (``depends_on`` empty). The list
        grows with every admitted child, so ``tasktree.build_tree`` over it
        reproduces the session tree (root = the single plan root). A flat
        multi-root plan has no single session tree: proposals are then
        rejected with the build_tree root-count reason.
        """
        self._session_tasks = [
            {
                "task_id": spec["task_id"],
                "kind": spec.get("kind") or _DEFAULT_SESSION_KIND,
                "depends_on": list(spec.get("depends_on") or []),
                "spec": spec,
            }
            for spec in specs
        ]

    def _strict_envelope(self, spec: dict[str, Any], msg: dict[str, Any]) -> dict[str, Any]:
        """The strict upward envelope for a worker result (I2.7 key set).

        Exactly the nine ``tasktree.upward_result`` keys — parent_task_id,
        unified_diff, diff_truncated, summary, metric_score,
        metric_breakdown, commits, files_changed, status. There is no
        transcript/scratchpad field to send, so a parent can never receive
        one. Used both for the parent envelope admitted into a child's
        context and for a dynamic child's own upward result.
        """
        values = _bounded_strict_envelope(
            {
                "parent_task_id": spec.get("parent_task_id"),
                "unified_diff": msg.get("diff", ""),
                "diff_truncated": msg.get("diff_truncated", False),
                "summary": msg.get("summary", ""),
                "metric_score": msg.get("metric_score"),
                "metric_breakdown": msg.get("metric_breakdown", {}),
                "commits": msg.get("commits", []),
                "files_changed": msg.get("files_changed", []),
                "status": msg.get("status", "failed"),
            }
        )
        return {key: values[key] for key in _ENVELOPE_KEYS}

    async def _admit_child(
        self,
        parent_spec: dict[str, Any],
        proposal: dict[str, Any],
        parent_envelope: dict[str, Any],
        *,
        private_integration_base: str | None = None,
    ) -> list[str]:
        """Validate one child revision, record it durably, then spawn it.

        Returns the admitted child task ids in admission order (one per
        proposal). A compatible cached-epoch child is pinned to the epoch's
        (provider, model) and carries the ``context_fork`` descriptor; an
        incompatible one runs the legacy summary-passing path.

        The revision is validated with ``tasktree.build_tree`` over the
        accumulated session tasks plus the proposed child. A duplicate,
        cyclic, multi-parent, over-depth, or over-width revision (or an
        invalid child spec) is durably rejected with ``child_rejected`` and
        spawns nothing. A valid revision is appended to the session tree,
        durably recorded as ``child_admitted`` through the existing
        EventStore path (redacted), and spawned as a new session task with
        context limited to its own spec plus the parent's envelope — never
        sibling context or a parent transcript.
        """
        # The worker's run request identifies the whole model turn, so several
        # delegate calls can legitimately carry the same wire id.  Admission
        # is the durable identity boundary: allocate one supervisor id for
        # every proposal that crosses it, including rejected revisions.
        proposal = {**proposal, "request_id": self._next_rid()}
        request_id = proposal["request_id"]
        parent_task_id = parent_spec["task_id"]
        child_task_id = proposal["child_task_id"]
        kind = proposal["kind"]
        original_proposal = proposal
        try:
            proposal, budget_decision = _prepare_child_budget(parent_spec, proposal)
        except ValueError as exc:
            await self.emit(
                "child_rejected",
                task_id=parent_task_id,
                request_id=request_id,
                parent_task_id=parent_task_id,
                child_task_id=child_task_id,
                child_kind=kind,
                reason=exc.__class__.__name__,
                message=str(exc)[:512],
            )
            await self._record_revision_conversation(
                outcome="rejected",
                parent_task_id=parent_task_id,
                child_task_id=child_task_id,
                child_kind=kind,
                request_id=request_id,
                reason=exc.__class__.__name__,
                proposal=original_proposal,
            )
            return []
        try:
            parse_child_policy(proposal.get("spec", {}))
        except ChildPolicyError as exc:
            await self.emit(
                "child_rejected",
                task_id=parent_task_id,
                request_id=request_id,
                parent_task_id=parent_task_id,
                child_task_id=child_task_id,
                child_kind=kind,
                reason=exc.__class__.__name__,
                message=str(exc)[:512],
            )
            await self._record_revision_conversation(
                outcome="rejected",
                parent_task_id=parent_task_id,
                child_task_id=child_task_id,
                child_kind=kind,
                request_id=request_id,
                reason=exc.__class__.__name__,
                proposal=original_proposal,
            )
            return []
        candidate = {
            "task_id": child_task_id,
            "kind": kind,
            "depends_on": [parent_task_id],
            "spec": proposal.get("spec", {}),
        }
        try:
            build_tree({"tasks": [*self._session_tasks, candidate]})
        except TaskTreeError as exc:
            await self.emit(
                "child_rejected",
                task_id=parent_task_id,
                request_id=request_id,
                parent_task_id=parent_task_id,
                child_task_id=child_task_id,
                child_kind=kind,
                reason=exc.__class__.__name__,
                message=str(exc)[:512],
            )
            await self._record_revision_conversation(
                outcome="rejected",
                parent_task_id=parent_task_id,
                child_task_id=child_task_id,
                child_kind=kind,
                request_id=request_id,
                reason=exc.__class__.__name__,
                proposal=proposal,
            )
            return []
        try:
            child_spec = _child_spec(self._session_dir, parent_spec, proposal, parent_envelope)
            if private_integration_base is not None:
                if Path(child_spec["repo"]).resolve() != Path(parent_spec["repo"]).resolve():
                    raise ValueError("a suspended parent and its child must share one repository")
                child_spec["base_commit"] = private_integration_base
                child_spec["_private_parent_integration"] = True
        except ValueError as exc:
            await self.emit(
                "child_rejected",
                task_id=parent_task_id,
                request_id=request_id,
                parent_task_id=parent_task_id,
                child_task_id=child_task_id,
                child_kind=kind,
                reason=exc.__class__.__name__,
                message=str(exc)[:512],
            )
            await self._record_revision_conversation(
                outcome="rejected",
                parent_task_id=parent_task_id,
                child_task_id=child_task_id,
                child_kind=kind,
                request_id=request_id,
                reason=exc.__class__.__name__,
                proposal=proposal,
            )
            return []
        try:
            _reject_duplicate_task_ownership(
                [*(task["spec"] for task in self._session_tasks), child_spec]
            )
        except (KeyError, TypeError, ValueError) as exc:
            await self.emit(
                "child_rejected",
                task_id=parent_task_id,
                request_id=request_id,
                parent_task_id=parent_task_id,
                child_task_id=child_task_id,
                child_kind=kind,
                reason=exc.__class__.__name__,
                message=str(exc)[:512],
            )
            await self._record_revision_conversation(
                outcome="rejected",
                parent_task_id=parent_task_id,
                child_task_id=child_task_id,
                child_kind=kind,
                request_id=request_id,
                reason=exc.__class__.__name__,
                proposal=proposal,
            )
            return []
        if self._task_group is None:
            no_task_group_error = RuntimeError("no active task group")
            await self.emit(
                "child_rejected",
                task_id=parent_task_id,
                request_id=request_id,
                parent_task_id=parent_task_id,
                child_task_id=child_task_id,
                child_kind=kind,
                reason="NoActiveTaskGroup",
                message=str(no_task_group_error)[:512],
            )
            await self._record_revision_conversation(
                outcome="rejected",
                parent_task_id=parent_task_id,
                child_task_id=child_task_id,
                child_kind=kind,
                request_id=request_id,
                reason="NoActiveTaskGroup",
                proposal=proposal,
            )
            return []

        # Append synchronously before the first await so concurrent proposals
        # observe this child and duplicate detection stays exact.  Everything
        # below has a rollback path until the durable admission event and the
        # task creation both succeed.
        self._session_tasks.append(
            {
                "task_id": child_task_id,
                "kind": kind,
                "depends_on": [parent_task_id],
                "spec": child_spec,
            }
        )
        try:
            await self._pin_fork_child(child_spec, parent_task_id, child_task_id, kind)
            await self._record_revision_conversation(
                outcome="admitted",
                parent_task_id=parent_task_id,
                child_task_id=child_task_id,
                child_kind=kind,
                request_id=request_id,
                proposal=original_proposal,
            )
            # This is the durable-before-spawn barrier.  A child is not an
            # admitted runtime object until this critical event succeeds.
            admitted_payload: dict[str, Any] = {
                "task_id": parent_task_id,
                "request_id": request_id,
                "parent_task_id": parent_task_id,
                "child_task_id": child_task_id,
                "child_kind": kind,
                "branch": child_spec.get("branch"),
            }
            if budget_decision is not None:
                admitted_payload["budget"] = budget_decision
            await self.emit("child_admitted", **admitted_payload)
        except BaseException as admission_error:
            self._rollback_child_admission(parent_task_id, child_task_id, child_spec)
            try:
                await self.emit(
                    "child_rejected",
                    task_id=parent_task_id,
                    request_id=request_id,
                    parent_task_id=parent_task_id,
                    child_task_id=child_task_id,
                    child_kind=kind,
                    reason="AdmissionPersistenceFailed",
                    message=str(admission_error)[:512],
                )
                await self._record_revision_conversation(
                    outcome="rejected",
                    parent_task_id=parent_task_id,
                    child_task_id=child_task_id,
                    child_kind=kind,
                    request_id=request_id,
                    reason="AdmissionPersistenceFailed",
                    proposal=proposal,
                )
            except BaseException:
                pass
            raise

        # Register the completion future synchronously immediately before the
        # create_task call.  The future is the parent-facing terminality
        # signal; supervise_task resolves it exactly once in its finally.
        loop = asyncio.get_running_loop()
        completion = loop.create_future()
        self._child_tasks.setdefault(parent_task_id, []).append(completion)
        self._child_completion[child_task_id] = completion
        self._child_parent[child_task_id] = parent_task_id
        child_coroutine = self.supervise_task(child_spec)
        try:
            self._task_group.create_task(child_coroutine)
        except BaseException as create_error:
            child_coroutine.close()
            self._rollback_child_admission(parent_task_id, child_task_id, child_spec)
            try:
                await self.emit(
                    "child_rejected",
                    task_id=parent_task_id,
                    request_id=request_id,
                    parent_task_id=parent_task_id,
                    child_task_id=child_task_id,
                    child_kind=kind,
                    reason="ChildSpawnFailed",
                    message=str(create_error)[:512],
                )
                await self._record_revision_conversation(
                    outcome="rejected",
                    parent_task_id=parent_task_id,
                    child_task_id=child_task_id,
                    child_kind=kind,
                    request_id=request_id,
                    reason="ChildSpawnFailed",
                    proposal=proposal,
                )
            except BaseException:
                pass
            return []
        self._admitted_children.setdefault(parent_task_id, []).append(child_task_id)
        return [child_task_id]

    def _rollback_child_admission(
        self, parent_task_id: str, child_task_id: str, child_spec: dict[str, Any]
    ) -> None:
        """Remove every in-memory child admission artifact before spawn."""
        self._session_tasks[:] = [
            task for task in self._session_tasks if task.get("spec") is not child_spec
        ]
        admitted = self._admitted_children.get(parent_task_id)
        if admitted is not None:
            self._admitted_children[parent_task_id] = [
                task_id for task_id in admitted if task_id != child_task_id
            ]
        completion = self._child_completion.pop(child_task_id, None)
        self._child_parent.pop(child_task_id, None)
        if completion is not None and not completion.done():
            completion.cancel()
        futures = self._child_tasks.get(parent_task_id)
        if futures is not None and completion is not None:
            self._child_tasks[parent_task_id] = [
                future for future in futures if future is not completion
            ]
        _release_lane(self._lanes, child_spec)

    def _child_results_for_resume(
        self,
        parent_task_id: str,
        child_ids: list[str],
        *,
        checkpoint_ref: Any,
        epoch: Any,
    ) -> dict[str, Any]:
        """One bounded resume payload: every admitted child's strict envelope.

        Children are ordered by admission (deterministic); a child with no
        terminal envelope yet synthesizes a bounded failure envelope so the
        resume never blocks on a missing result. Every envelope is normalized
        to the strict parent-envelope caps (the worker re-validates each
        child result as a strict envelope), and the list is capped at
        ``MAX_ENVELOPE_ITEMS`` with ``child_results_truncated`` set when
        dropped.
        """
        child_results: list[dict[str, Any]] = []
        truncated = False
        workspace_changed = False
        for child_id in child_ids:
            child_spec = self._session_spec(child_id)
            result = self._results.get(child_id)
            if (
                child_spec is not None
                and child_spec.get("_private_parent_integration") is True
                and result is not None
                and result.status == "succeeded"
                and result.merge_sha is not None
            ):
                workspace_changed = True
            envelope = self._child_result_by_task.get(child_id)
            if envelope is None:
                result = self._results.get(child_id)
                summary = result.reason if result is not None else "child result missing"
                envelope = {
                    "parent_task_id": parent_task_id,
                    "unified_diff": "",
                    "diff_truncated": False,
                    "summary": _cap_utf8(cast(str, summary), MAX_ENVELOPE_FIELD_CHARS),
                    "metric_score": None,
                    "metric_breakdown": {},
                    "commits": [],
                    "files_changed": [],
                    "status": (
                        "cancelled"
                        if result is not None and result.reason == "cancelled"
                        else "failed"
                    ),
                }
            if len(child_results) >= MAX_ENVELOPE_ITEMS:
                truncated = True
                continue
            child_results.append(_bounded_resume_envelope(envelope))
        return {
            "checkpoint_ref": checkpoint_ref,
            "epoch": epoch,
            "child_results": child_results,
            "child_results_truncated": truncated,
            "workspace_changed": workspace_changed,
        }

    async def _await_suspend_children(self, parent_task_id: str, remaining: float) -> None:
        """Await the suspended parent's children, bounded by the wall budget.

        Each child is awaited under a shield so a resume-timeout never cancels
        the child's own supervision; the bounded wait prevents one hung child
        from consuming the parent's entire remaining budget.
        """
        loop = asyncio.get_running_loop()
        deadline = loop.time() + max(0.0, remaining)
        pending = [
            future
            for future in self._child_tasks.get(parent_task_id, ())
            if future is not None and not future.done()
        ]
        for future in pending:
            timeout = max(0.0, deadline - loop.time())
            try:
                await asyncio.wait_for(asyncio.shield(future), timeout=timeout)
            except TimeoutError:
                pass

    async def _pin_fork_child(
        self,
        child_spec: dict[str, Any],
        parent_task_id: str,
        child_task_id: str,
        kind: str | None,
    ) -> None:
        """Resolve one child's context representation and placement.

        When the child declares ``context_mode``/``placement``, that policy is
        authoritative (owner spec): trunk requires an exact compatible parent
        checkpoint and is REJECTED (raised) when impossible; semantic imports
        only the immutable summary trunk; fresh removes all parent context.
        ``placement=spread`` prefers another feasible provider lane and falls
        back to the full feasible set; ``inherit`` keeps the parent
        provider/model. An undeclared child keeps the automatic
        compatibility resolution (exact fork when legal, otherwise semantic).
        """
        if not self._context_reuse:
            return
        epoch = self._task_epochs.get(parent_task_id)
        cache_key = epoch.get("cache_key") if isinstance(epoch, dict) else None
        declared = _declared_child_policy(child_spec)
        if declared is not None:
            context_mode, placement = declared
            parent_provider = cache_key.get("provider") if isinstance(cache_key, dict) else None
            if context_mode == "trunk":
                if not self._pin_exact_fork(child_spec, epoch, parent_task_id, child_task_id, kind):
                    raise ChildPolicyError(
                        "child context_mode=trunk requires an exact compatible "
                        "parent checkpoint; use semantic or fresh instead"
                    )
                if placement == "spread":
                    self._apply_spread(child_spec, parent_provider)
                await self._emit_child_fork_event(
                    parent_task_id,
                    child_task_id,
                    kind,
                    epoch,
                    compatible=True,
                    semantic_reuse=False,
                    context_mode=context_mode,
                    placement=placement,
                    spread_from_provider=(parent_provider if placement == "spread" else None),
                )
                return
            checkpoint_ref = epoch.get("checkpoint_ref") if isinstance(epoch, dict) else None
            if context_mode == "semantic":
                if not (
                    isinstance(cache_key, dict)
                    and cache_key.get("redacted") is False
                    and isinstance(checkpoint_ref, str)
                ):
                    raise ChildPolicyError(
                        "child context_mode=semantic requires an unredacted parent checkpoint"
                    )
                child_spec["summary_trunk_ref"] = checkpoint_ref
                if placement == "spread":
                    self._apply_spread(child_spec, parent_provider)
                else:
                    self._pin_parent_provider(child_spec, parent_provider, cache_key)
                await self._emit_child_fork_event(
                    parent_task_id,
                    child_task_id,
                    kind,
                    epoch,
                    compatible=False,
                    semantic_reuse=True,
                    context_mode=context_mode,
                    placement=placement,
                    spread_from_provider=(parent_provider if placement == "spread" else None),
                )
                return
            # context_mode == "fresh": remove all parent context.
            child_spec.pop("summary_trunk_ref", None)
            child_spec.pop("context_fork", None)
            child_spec.pop("parent_envelope", None)
            if placement == "spread":
                self._apply_spread(child_spec, parent_provider)
            else:
                self._pin_parent_provider(child_spec, parent_provider, cache_key)
            await self._emit_child_fork_event(
                parent_task_id,
                child_task_id,
                kind,
                epoch,
                compatible=False,
                semantic_reuse=False,
                context_mode=context_mode,
                placement=placement,
                spread_from_provider=(parent_provider if placement == "spread" else None),
            )
            return

        # Undeclared: automatic compatibility resolution (legacy path).
        if not isinstance(epoch, dict):
            return
        authorized = frozenset(child_spec.get("authorized_providers") or ())
        compatible, reason = _fork_cache_compatible_supervisor(child_spec, epoch, authorized)
        semantic_reuse = (
            not compatible
            and isinstance(cache_key, dict)
            and cache_key.get("redacted") is False
            and isinstance(epoch.get("checkpoint_ref"), str)
        )
        fork_payload: dict[str, Any] = {
            "parent_task_id": parent_task_id,
            "child_task_id": child_task_id,
            "child_kind": kind,
            "epoch": epoch.get("epoch"),
            "compatible": compatible,
            "semantic_reuse": semantic_reuse,
        }
        if reason is not None:
            fork_payload["reason"] = reason
        await self.emit(
            "context_fork",
            task_id=parent_task_id,
            **fork_payload,
        )
        if semantic_reuse:
            # Cold semantic children choose an independent provider lane. Only
            # exact cache-compatible forks inherit the parent provider/model.
            child_spec.pop("assigned_provider", None)
            fanout = child_spec.get("fanout_config")
            if isinstance(fanout, dict):
                fanout.pop("provider", None)
                fanout.pop("assigned_provider", None)
            child_spec["summary_trunk_ref"] = epoch["checkpoint_ref"]
            return
        if not compatible or not isinstance(cache_key, dict):
            await self.emit(
                "context_fork_skipped",
                task_id=parent_task_id,
                parent_task_id=parent_task_id,
                child_task_id=child_task_id,
                epoch=epoch.get("epoch"),
                reason=reason or "incompatible epoch",
            )
            return
        self._apply_exact_fork(child_spec, epoch, parent_task_id, child_task_id, kind, cache_key)

    def _pin_exact_fork(
        self,
        child_spec: dict[str, Any],
        epoch: dict[str, Any] | None,
        parent_task_id: str,
        child_task_id: str,
        kind: str | None,
    ) -> bool:
        """Pin an exact cache-compatible fork; return False when impossible."""
        if not isinstance(epoch, dict):
            return False
        cache_key = epoch.get("cache_key")
        if not isinstance(cache_key, dict):
            return False
        authorized = frozenset(child_spec.get("authorized_providers") or ())
        compatible, _reason = _fork_cache_compatible_supervisor(child_spec, epoch, authorized)
        if not compatible:
            return False
        self._apply_exact_fork(child_spec, epoch, parent_task_id, child_task_id, kind, cache_key)
        return True

    def _apply_exact_fork(
        self,
        child_spec: dict[str, Any],
        epoch: dict[str, Any],
        parent_task_id: str,
        child_task_id: str,
        kind: str | None,
        cache_key: dict[str, Any],
    ) -> None:
        """Pin the child to the epoch's provider/model with a context fork."""
        provider = cache_key.get("provider")
        if not isinstance(provider, str):
            return
        boundary = cache_key.get("provider_boundary")
        if not isinstance(boundary, dict):
            return
        descriptor = {
            "checkpoint_ref": epoch["checkpoint_ref"],
            "provider": provider,
            "model": cache_key["model"],
            "system_sha256": cache_key["system_sha256"],
            "tools_sha256": cache_key["tools_sha256"],
            "prefix_sha256": cache_key["prefix_sha256"],
            "suffix_sha256": cache_key["suffix_sha256"],
            "full_sha256": cache_key["full_sha256"],
            "prefix_bytes": cache_key["prefix_bytes"],
            "provider_boundary": boundary,
        }
        fanout = child_spec.get("fanout_config")
        if not isinstance(fanout, dict):
            fanout = child_spec["fanout_config"] = {}
        fanout["model"] = descriptor["model"]
        child_spec["assigned_provider"] = provider
        child_spec["context_fork"] = descriptor
        lane = self._lanes.get(provider)
        if lane is not None and not child_spec.get("_lane_reserved", False):
            lane.in_flight += 1
            child_spec["_lane_reserved"] = True

    def _pin_parent_provider(
        self,
        child_spec: dict[str, Any],
        parent_provider: Any,
        cache_key: dict[str, Any] | None,
    ) -> None:
        """Pin the parent provider/model (semantic/fresh + inherit)."""
        if not isinstance(parent_provider, str) or not isinstance(cache_key, dict):
            return
        model = cache_key.get("model")
        if not isinstance(model, str):
            return
        child_spec["assigned_provider"] = parent_provider
        fanout = child_spec.get("fanout_config")
        if not isinstance(fanout, dict):
            fanout = child_spec["fanout_config"] = {}
        fanout["model"] = model

    def _apply_spread(self, child_spec: dict[str, Any], parent_provider: Any) -> None:
        """Prefer another feasible provider; parent remains the fallback."""
        child_spec.pop("assigned_provider", None)
        fanout = child_spec.get("fanout_config")
        if isinstance(fanout, dict):
            fanout.pop("provider", None)
            fanout.pop("assigned_provider", None)
        if isinstance(parent_provider, str):
            child_spec["spread_from_provider"] = parent_provider

    async def _emit_child_fork_event(
        self,
        parent_task_id: str,
        child_task_id: str,
        kind: str | None,
        epoch: dict[str, Any] | None,
        *,
        compatible: bool,
        semantic_reuse: bool,
        context_mode: str,
        placement: str,
        spread_from_provider: str | None,
    ) -> None:
        """Emit one context_fork event with requested and resolved policy."""
        await self.emit(
            "context_fork",
            task_id=parent_task_id,
            parent_task_id=parent_task_id,
            child_task_id=child_task_id,
            child_kind=kind,
            epoch=epoch.get("epoch") if isinstance(epoch, dict) else None,
            compatible=compatible,
            semantic_reuse=semantic_reuse,
            context_mode=context_mode,
            placement=placement,
            resolved_context_mode=context_mode,
            resolved_placement=placement,
            spread_from_provider=spread_from_provider,
        )

    async def _record_revision_conversation(
        self,
        *,
        outcome: str,
        parent_task_id: str,
        child_task_id: str,
        child_kind: str | None,
        request_id: str | None,
        reason: str | None = None,
        proposal: dict[str, Any],
    ) -> None:
        """Durably append one conversation row per admitted/rejected revision.

        One ``kind="system"`` row per revision, keyed by ``node_id`` = child
        task id; the parent task is recorded in ``meta`` (the schema's
        ``parent_id`` column is a row id, not a task id). The proposal is
        redacted through the session redactor before it enters the store. A
        store failure raises — the boundary never silently succeeds.
        """
        if self._conversations is None:
            return
        record: dict[str, Any] = {
            "outcome": outcome,
            "parent_task_id": parent_task_id,
            "child_task_id": child_task_id,
            "child_kind": child_kind,
            "request_id": request_id,
        }
        if reason is not None:
            record["reason"] = reason
        record["proposal"] = self._redact_proposal(proposal)
        try:
            await asyncio.to_thread(
                self._conversations.append,
                child_task_id,
                "system",
                json.dumps(record, sort_keys=True, default=str),
                kind="system",
                meta={"parent_task_id": parent_task_id},
            )
        except (ConversationStoreError, RuntimeError) as exc:
            raise ConversationAppendError("conversation store append failed") from exc

    def _redact_proposal(self, proposal: dict[str, Any]) -> dict[str, Any]:
        """Redact a propose_child wire record without flattening its structure."""
        if self._redactor is None:
            return dict(proposal)
        redacted = self._redactor.redact_protocol_record(
            proposal,
            structural_fields=("request_id", "parent_task_id", "child_task_id", "kind"),
        )
        return cast(dict[str, Any], redacted)

    async def _admit_port_proposals(
        self,
        parent_spec: dict[str, Any],
        parent_envelope: dict[str, Any],
        *,
        failure_reason: str | None = None,
        admit_proposals: bool = True,
    ) -> list[str]:
        """Feed one admitted parent's envelope to the decision port and admit its proposals.

        The port is the only provider-side channel whose response can become a
        child proposal: its aggregate/step output is routed through the
        existing ``_admit_child`` revision validation — never the live tree
        directly. A malformed or mismatched proposal is durably rejected with
        ``child_rejected`` and spawns nothing. A finished task outside the
        port's decision domain (unknown to its tree) yields no wave: the port
        has nothing to propose for it. Returns the admitted child task ids.
        ``failure_reason`` is decision-only metadata and never widens the
        strict envelope passed to ``aggregate``. Failure observations set
        ``admit_proposals`` false so they cannot spawn after a failed parent.
        """
        parent_task_id = parent_spec["task_id"]
        malformed: str | None = None
        proposals: list[dict[str, Any]] = []
        async with self._admission_port_lock:
            try:
                self._admission_port.aggregate(parent_task_id, parent_envelope)
            except ValueError:
                return []
            decision_payload = dict(parent_envelope)
            if failure_reason is not None:
                decision_payload["failure_reason"] = failure_reason
            try:
                proposals = await self._admission_port.step(
                    [
                        {
                            "kind": "child_result",
                            "task_id": parent_task_id,
                            "payload": decision_payload,
                        }
                    ]
                )
            except (TypeError, ValueError) as exc:
                malformed = repr(exc)
        if malformed is not None:
            await self.emit(
                "child_rejected",
                task_id=parent_task_id,
                parent_task_id=parent_task_id,
                child_task_id=None,
                child_kind=None,
                reason="MalformedProposal",
                message=f"decision port error: {malformed}"[:512],
            )
            return []
        if not admit_proposals:
            return []
        admitted: list[str] = []
        for proposal in proposals:
            admitted.extend(await self._admit_port_proposal(parent_spec, parent_envelope, proposal))
        return admitted

    def _take_generation_proposals(
        self, task_id: str, generation: int
    ) -> tuple[dict[str, Any], ...]:
        """Detach only one generation's buffered child proposals."""
        entries = self._pending_children.pop(task_id, [])
        selected: list[dict[str, Any]] = []
        remaining: list[tuple[int, dict[str, Any]]] = []
        for entry_generation, proposal in entries:
            if entry_generation == generation:
                selected.append(proposal)
            else:
                remaining.append((entry_generation, proposal))
        if remaining:
            self._pending_children[task_id] = remaining
        return tuple(selected)

    async def _reject_child_proposals(
        self,
        parent_task_id: str,
        proposals: Sequence[dict[str, Any]],
        *,
        reason: str,
        message: str,
    ) -> None:
        """Durably reject proposals that cannot cross a terminal boundary."""
        for proposal in proposals:
            proposal = {**proposal, "request_id": self._next_rid()}
            child_task_id = _wire_str(proposal.get("child_task_id"))
            child_kind = _wire_str(proposal.get("kind"))
            await self.emit(
                "child_rejected",
                task_id=parent_task_id,
                request_id=_wire_str(proposal.get("request_id")),
                parent_task_id=parent_task_id,
                child_task_id=child_task_id,
                child_kind=child_kind,
                reason=reason,
                message=message,
            )
            if child_task_id is not None:
                await self._record_revision_conversation(
                    outcome="rejected",
                    parent_task_id=parent_task_id,
                    child_task_id=child_task_id,
                    child_kind=child_kind,
                    request_id=_wire_str(proposal.get("request_id")),
                    reason=reason,
                    proposal=proposal,
                )

    async def _admit_generation_children(
        self,
        parent_spec: dict[str, Any],
        parent_envelope: dict[str, Any],
        proposals: Sequence[dict[str, Any]],
        *,
        include_port: bool,
        failure_reason: str | None = None,
        private_integration_base: str | None = None,
    ) -> list[str]:
        """Admit proposals after the permitted parent lifecycle verdict."""
        admitted: list[str] = []
        for proposal in proposals:
            admitted.extend(
                await self._admit_child(
                    parent_spec,
                    proposal,
                    parent_envelope,
                    private_integration_base=private_integration_base,
                )
            )
        if include_port and self._admission_port is not None:
            admitted.extend(
                await self._admit_port_proposals(
                    parent_spec,
                    parent_envelope,
                    failure_reason=failure_reason,
                )
            )
        return admitted

    async def _admit_port_proposal(
        self,
        parent_spec: dict[str, Any],
        parent_envelope: dict[str, Any],
        proposal: Any,
    ) -> list[str]:
        """Route one decision-port proposal through the revision boundary."""
        parent_task_id = parent_spec["task_id"]
        if not isinstance(proposal, dict):
            await self.emit(
                "child_rejected",
                task_id=parent_task_id,
                parent_task_id=parent_task_id,
                child_task_id=None,
                child_kind=None,
                reason="MalformedProposal",
                message="decision port returned a non-object proposal",
            )
            return []
        invalid_fields = _invalid_propose_child_fields(proposal)
        if invalid_fields:
            proposal = {**proposal, "request_id": self._next_rid()}
            await self.emit(
                "child_rejected",
                task_id=parent_task_id,
                request_id=_wire_str(proposal.get("request_id")),
                parent_task_id=parent_task_id,
                child_task_id=_wire_str(proposal.get("child_task_id")),
                child_kind=_wire_str(proposal.get("kind")),
                reason="MalformedProposal",
                message=f"decision port proposal rejected: invalid field(s) {invalid_fields}",
            )
            await self._record_port_rejection(
                parent_task_id,
                proposal,
                "MalformedProposal",
                f"invalid field(s) {invalid_fields}",
            )
            return []
        if proposal.get("parent_task_id") != parent_task_id:
            proposal = {**proposal, "request_id": self._next_rid()}
            await self.emit(
                "child_rejected",
                task_id=parent_task_id,
                request_id=_wire_str(proposal.get("request_id")),
                parent_task_id=parent_task_id,
                child_task_id=proposal["child_task_id"],
                child_kind=proposal["kind"],
                reason="ParentTaskIdMismatch",
                message="decision port proposal parent_task_id does not match the finished task",
            )
            await self._record_port_rejection(
                parent_task_id,
                proposal,
                "ParentTaskIdMismatch",
                "parent_task_id does not match the finished task",
            )
            return []
        return await self._admit_child(parent_spec, proposal, parent_envelope)

    async def _record_port_rejection(
        self,
        parent_task_id: str,
        proposal: dict[str, Any],
        reason: str,
        message: str,
    ) -> None:
        """Persist a conversation row when a rejected port proposal has identity."""
        child_task_id = proposal.get("child_task_id")
        if not isinstance(child_task_id, str) or not child_task_id:
            return
        await self._record_revision_conversation(
            outcome="rejected",
            parent_task_id=parent_task_id,
            child_task_id=child_task_id,
            child_kind=_wire_str(proposal.get("kind")),
            request_id=_wire_str(proposal.get("request_id")),
            reason=f"{reason}: {message}"[:512],
            proposal=proposal,
        )

    # -- per-task supervision ------------------------------------------------

    async def _emit_provider_infeasible(self, spec: Mapping[str, Any]) -> None:
        """Persist each credential-infeasible provider once for this task."""
        records = spec.get("_provider_infeasible", ())
        if not isinstance(records, list | tuple):
            return
        if isinstance(spec, dict):
            spec.pop("_provider_infeasible", None)
        for record in records:
            if not isinstance(record, tuple) or len(record) != 2:
                continue
            provider, reason = record
            if isinstance(provider, str) and isinstance(reason, str):
                await self.emit(
                    "provider_infeasible",
                    task_id=spec.get("task_id"),
                    provider=provider,
                    reason=reason,
                )

    def _resolve_assignment(self, spec: dict[str, Any]) -> None:
        """Admission-time (provider, model) selection for un-pinned tasks.

        Mutates ``spec`` when it declares ``model_candidates`` and its
        fanout_config has no pinned model (solution C), and books the chosen
        provider's lane +1 in_flight (H1). Tasks pre-assigned by
        ``_preassign_lanes`` already carry ``fanout_config.model`` and
        ``assigned_provider``, so this is a no-op for them — their lane slot
        was reserved in the batch pass. Tasks that arrive without
        pre-assignment (dynamic children proposed mid-session) resolve against
        the live ledger and current lanes and book their slot here.
        """
        if self._debt_store is None:
            return
        if _resolve_model_candidates(
            spec,
            self._debt_store.as_mapping(),
            self._lanes,
            provider_environment=self._provider_environment,
            oauth_store=self._oauth_store,
        ):
            self._lanes[spec["assigned_provider"]].in_flight += 1
            spec["_lane_reserved"] = True

    def _redact_checkpoint_message(self, message: dict[str, str]) -> dict[str, str]:
        """Redact one provider message before it enters or leaves recovery."""
        if self._redactor is None:
            return dict(message)
        redacted = self._redactor.redact_protocol_record(message, structural_fields=("role",))
        return {
            "role": cast(str, redacted["role"]),
            "content": cast(str, redacted["content"]),
        }

    async def _record_context_checkpoint_conversation(
        self, task_id: str, checkpoint_ref: str, epoch: int
    ) -> None:
        """Append one redacted raw row for every message in an epoch checkpoint."""
        if self._conversations is None:
            return
        try:
            checkpoint = await asyncio.to_thread(
                _load_epoch_checkpoint_messages,
                self._session_dir,
                task_id,
                checkpoint_ref,
            )
        except (OSError, ValueError) as exc:
            raise ConversationAppendError("conversation checkpoint load failed") from exc

        meta = {"checkpoint_ref": checkpoint_ref, "epoch": epoch}
        for message in (
            *checkpoint["provider_messages"],
            *checkpoint["continuation_suffix"],
        ):
            redacted = self._redact_checkpoint_message(message)
            try:
                await asyncio.to_thread(
                    self._conversations.append,
                    task_id,
                    redacted["role"],
                    redacted["content"],
                    kind="turn",
                    meta=meta,
                )
            except (ConversationStoreError, RuntimeError) as exc:
                raise ConversationAppendError("conversation store append failed") from exc

    def replay_raw_record(self, task_id: str) -> dict[str, Any]:
        """Return a read-only recovery bundle from rows and immutable checkpoints.

        Conversation rows remain the append-only raw record.  The checkpoint
        files are loaded separately as immutable replay anchors, so a caller can
        verify both sources without changing the active prompt projection.
        """
        if self._conversations is None:
            return {"node_id": task_id, "rows": [], "checkpoint_files": []}
        rows = self._conversations.history(task_id)
        checkpoint_refs: dict[str, Any] = {}
        for row in rows:
            meta = row.get("meta")
            checkpoint_ref = meta.get("checkpoint_ref") if isinstance(meta, dict) else None
            if not isinstance(checkpoint_ref, str) or not checkpoint_ref:
                continue
            checkpoint_refs.setdefault(checkpoint_ref, meta.get("epoch"))

        checkpoint_dir = (
            self._session_dir.resolve() / ".cambium" / "checkpoints" / _safe_task_id(task_id)
        )
        try:
            checkpoint_paths = sorted(checkpoint_dir.iterdir(), key=lambda path: path.name)
        except FileNotFoundError:
            checkpoint_paths = []
        for checkpoint_path in checkpoint_paths:
            if checkpoint_path.is_symlink() or not checkpoint_path.is_file():
                continue
            checkpoint_ref = f"{_safe_task_id(task_id)}/{checkpoint_path.name}"
            try:
                _task_component, checkpoint_epoch, _pre, _persisted = (
                    _validate_checkpoint_ref_shape(checkpoint_ref)
                )
            except ValueError:
                continue
            checkpoint_refs.setdefault(checkpoint_ref, checkpoint_epoch)

        checkpoint_files: list[dict[str, Any]] = []
        for checkpoint_ref, epoch in checkpoint_refs.items():
            loaded = _load_epoch_checkpoint_messages(self._session_dir, task_id, checkpoint_ref)
            checkpoint_files.append(
                {
                    "checkpoint_ref": checkpoint_ref,
                    "epoch": epoch,
                    "provider_messages": [
                        self._redact_checkpoint_message(message)
                        for message in loaded["provider_messages"]
                    ],
                    "continuation_suffix": [
                        self._redact_checkpoint_message(message)
                        for message in loaded["continuation_suffix"]
                    ],
                }
            )
        return {
            "node_id": task_id,
            "rows": rows,
            "checkpoint_files": checkpoint_files,
        }

    def _capture_child_result(
        self,
        spec: dict[str, Any],
        msg: Mapping[str, Any],
        *,
        request_id: str | None = None,
        generation: int | None = None,
    ) -> None:
        """Capture the latest terminal-generation envelope for one child.

        A worker may emit a correlated envelope and then crash, causing a
        restart. The envelope is provisional until the supervisor accepts that
        generation's integrity/merge verdict, so never use a first-envelope
        rule here. Keep the generation-indexed record as an audit/debug seam
        and expose only the greatest terminal generation to the parent.
        """
        task_id = spec["task_id"]
        parent_task_id = self._child_parent.get(task_id)
        if parent_task_id is None:
            return
        terminal_generation = generation if isinstance(generation, int) else 0
        envelope = self._strict_envelope(spec, dict(msg))
        by_generation = self._child_result_by_generation.setdefault(task_id, {})
        by_generation[terminal_generation] = (envelope, (request_id, generation))
        latest_generation = max(by_generation)
        latest_envelope, latest_meta = by_generation[latest_generation]
        previous = self._child_result_by_task.get(task_id)
        self._child_result_by_task[task_id] = latest_envelope
        self._child_result_meta[task_id] = latest_meta
        siblings = self._child_envelopes.setdefault(parent_task_id, [])
        if previous is not None:
            siblings[:] = [item for item in siblings if item is not previous]
        if latest_envelope not in siblings:
            siblings.append(latest_envelope)

    def _synthetic_child_result(
        self, spec: dict[str, Any], *, cancelled: bool = False
    ) -> dict[str, Any]:
        """Build a bounded terminal envelope when a child has no wire result."""
        task_id = spec["task_id"]
        parent_task_id = self._child_parent.get(task_id, spec.get("parent_task_id"))
        result = self._results.get(task_id)
        if cancelled:
            status = "cancelled"
            reason = "child cancelled"
        elif result is None:
            status = "failed"
            reason = "child supervision ended without a result"
        else:
            status = "failed" if result.status != "succeeded" else "succeeded"
            reason = result.reason or "child failed"
        return _bounded_strict_envelope(
            {
                "parent_task_id": parent_task_id,
                "unified_diff": "",
                "diff_truncated": False,
                "summary": reason,
                "metric_score": None,
                "metric_breakdown": {},
                "commits": [],
                "files_changed": [],
                "status": status,
            }
        )

    async def _publish_child_result(self, task_id: str, envelope: dict[str, Any]) -> None:
        """Publish one correlated child result event, first result wins."""
        if task_id in self._child_result_emitted:
            return
        parent_task_id = self._child_parent.get(task_id)
        if parent_task_id is None:
            return
        self._child_result_emitted.add(task_id)
        request_id, generation = self._child_result_meta.get(task_id, (None, None))
        await self.emit(
            "child_result",
            task_id=task_id,
            request_id=request_id,
            generation=generation,
            **_bounded_resume_envelope(envelope),
        )

    async def _complete_child(self, spec: dict[str, Any], *, cancelled: bool) -> None:
        """Resolve an admitted child's completion future exactly once."""
        task_id = spec["task_id"]
        if task_id not in self._child_parent:
            return
        result = self._results.get(task_id)
        envelope = self._child_result_by_task.get(task_id)
        # A worker can report success and then fail supervisor-owned integrity
        # or merge checks.  The upward result follows the final supervisor
        # verdict, not the provisional wire status.
        if envelope is None or (
            result is not None
            and result.status != "succeeded"
            and envelope.get("status") == "succeeded"
        ):
            previous = envelope
            envelope = self._synthetic_child_result(spec, cancelled=cancelled)
            self._child_result_by_task[task_id] = envelope
            parent_task_id = self._child_parent[task_id]
            self._child_envelopes[parent_task_id] = [
                item
                for item in self._child_envelopes.get(parent_task_id, ())
                if item is not previous
            ]
            self._child_envelopes[parent_task_id].append(envelope)
            self._child_result_meta.setdefault(task_id, (None, None))
        await self._publish_child_result(task_id, envelope)
        future = self._child_completion.get(task_id)
        if future is not None and not future.done():
            future.set_result(envelope)

    async def supervise_task(self, spec: dict[str, Any]) -> None:
        task_id = spec["task_id"]
        cancelled = False
        try:
            if task_id in self._results:
                return
            try:
                await self._supervise(spec)
            except NoCredentialFeasibleProvidersError:
                reason = "no credential-feasible providers"
                await self._emit_provider_infeasible(spec)
                await self.emit("worker_failed", task_id=task_id, reason=reason)
                self._results[task_id] = TaskResult(
                    task_id=task_id, status="failed", exit_code=1, reason=reason
                )
            except InvalidBaseCommitError:
                reason = "invalid_base_commit"
                await self._emit_provider_infeasible(spec)
                await self.emit("worker_failed", task_id=task_id, reason=reason)
                self._results[task_id] = TaskResult(
                    task_id=task_id, status="failed", exit_code=1, reason=reason
                )
            except WorktreeRecoveryError:
                reason = "worktree_recovery_failed"
                await self._emit_provider_infeasible(spec)
                await self.emit("worker_failed", task_id=task_id, reason=reason)
                self._results[task_id] = TaskResult(
                    task_id=task_id, status="failed", exit_code=1, reason=reason
                )
            except Exception as exc:
                # Configuration, provider-routing, worktree, and spawn errors
                # belong to this task. Never let one malformed task escape its
                # coroutine and have TaskGroup cancel unrelated siblings.
                detail = str(exc).strip().replace("\n", " ")[:512]
                reason = f"{exc.__class__.__name__}: {detail}" if detail else exc.__class__.__name__
                await self._emit_provider_infeasible(spec)
                await self.emit("worker_failed", task_id=task_id, reason=reason, internal=True)
                self._results[task_id] = TaskResult(
                    task_id=task_id, status="failed", exit_code=1, reason=reason
                )
        except asyncio.CancelledError:
            cancelled = True
            self._cancelled_tasks.add(task_id)
            self._results.setdefault(
                task_id,
                TaskResult(
                    task_id=task_id,
                    status="cancelled",
                    exit_code=1,
                    reason="cancelled",
                ),
            )
            raise
        finally:
            try:
                # Lane release (H1): only an explicit ownership token may
                # decrement a lane.  Provider identity alone does not prove
                # ownership.
                _release_lane(self._lanes, spec)
                result = self._results.get(task_id)
                retain_resolver_worktree = spec.get("_retain_worktree") is True
                if result is not None and not retain_resolver_worktree:
                    try:
                        await self._prune_worktree(
                            spec,
                            force=cancelled
                            or task_id in self._cancelled_tasks
                            or result.status == "cancelled",
                        )
                        self._cleanup_attempted.add(task_id)
                    except asyncio.CancelledError:
                        raise
                    except Exception as exc:
                        # Cleanup is best effort and must not cancel sibling
                        # task supervision after a task-local failure.
                        await self.emit(
                            "worktree_cleanup_deferred",
                            task_id=task_id,
                            reason=f"cleanup_exception:{exc.__class__.__name__}",
                        )
                # Proposals buffered but never processed are durably rejected.
                pending = self._pending_children.pop(task_id, [])
                pending_proposals = tuple(proposal for _generation, proposal in pending)
                if pending_proposals:
                    await self._reject_child_proposals(
                        task_id,
                        pending_proposals,
                        reason="ParentTerminatedWithoutResult",
                        message="parent ended without a usable result envelope",
                    )
                parent_task_id = spec.get("parent_task_id")
                child_result = self._results.get(task_id)
                if (
                    parent_task_id is not None
                    and child_result is not None
                    and child_result.status != "succeeded"
                ):
                    await self.emit(
                        "child_failed",
                        task_id=task_id,
                        parent_task_id=parent_task_id,
                        reason=child_result.reason or child_result.status,
                    )
            finally:
                # This is deliberately the last lifecycle step: parent
                # suspension can proceed only after the final result (or a
                # synthesized failure/cancellation envelope) is published.
                await self._complete_child(spec, cancelled=cancelled)

    async def _supervise(self, spec: dict[str, Any]) -> None:
        task_id = spec["task_id"]
        repo = Path(spec["repo"])
        worktree = Path(spec["worktree_path"])
        max_restarts = int(spec.get("max_restarts", DEFAULT_MAX_RESTARTS))
        ready_timeout = _cfg_float(
            spec, "ready_timeout_s", "CAMBIUM_READY_TIMEOUT_S", DEFAULT_READY_TIMEOUT_S
        )
        heartbeat_interval = _cfg_float(
            spec,
            "heartbeat_interval_s",
            "CAMBIUM_HEARTBEAT_INTERVAL_S",
            DEFAULT_HEARTBEAT_INTERVAL_S,
        )
        heartbeat_timeout = _cfg_float(
            spec,
            "heartbeat_timeout_s",
            "CAMBIUM_HEARTBEAT_TIMEOUT_S",
            DEFAULT_HEARTBEAT_TIMEOUT_S,
        )
        wall_budget = _cfg_float(spec, "max_wall_s", "CAMBIUM_WALL_BUDGET_S", DEFAULT_WALL_BUDGET_S)
        # Cache-first: one absolute deadline accounts for every suspend/resume
        # cycle and the time spent waiting for children. The window STARTS
        # when the first generation actually runs (the task may wait through
        # earlier dependency waves first). An explicit restart (crash/failure
        # recovery, bounded by ``max_restarts``) grants a fresh window — see
        # the restart block below.
        deadline: float | None = None
        if spec.get("base_commit") is None:
            base = await self._git_stdout(repo, "rev-parse", "refs/heads/main", check=False)
            if not base:
                raise ValueError(
                    f"task {task_id}: no 'base_commit' in plan and {repo} has no refs/heads/main"
                )
            spec["base_commit"] = base
        requested_base = str(spec["base_commit"])
        resolved_base = await self._git_stdout(
            repo,
            "rev-parse",
            "--verify",
            "--end-of-options",
            f"{requested_base}^{{commit}}",
            check=False,
        )
        if not resolved_base:
            raise InvalidBaseCommitError(
                f"task {task_id}: base_commit {requested_base!r} does not resolve "
                f"to a commit in {repo}"
            )
        spec["base_commit"] = resolved_base

        thresholds = (
            spec["resource_thresholds"]
            if "resource_thresholds" in spec
            else self._resource_thresholds
        )
        if thresholds is not None:
            allowed, reasons = await asyncio.to_thread(can_run_heavy, thresholds)
            if not allowed:
                await self.emit(
                    "resource_denied",
                    task_id=task_id,
                    resource_denied=True,
                    reasons=reasons,
                )
                await self._emit_provider_infeasible(spec)
                self._results[task_id] = TaskResult(
                    task_id=task_id,
                    status="failed",
                    exit_code=126,
                    reason="resource_denied",
                )
                return
        generation = await self._ensure_worktree(spec)
        if spec.get("_resolver_child"):
            await self._prepare_resolver_worktree(spec)

        # The admission semaphore bounds concurrent worker processes only.  A
        # suspended parent releases it as soon as its generation exits, waits
        # for children without a worker slot, then reacquires it for resume.
        semaphore = self._admission_semaphore
        semaphore_held = False

        async def drive_with_admission_slot(
            handle: WorkerHandle, *, allow_pool: bool
        ) -> _GenOutcome:
            acquired = False
            if semaphore is not None:
                await semaphore.acquire()
                acquired = True
            try:
                return await self._drive_generation(
                    spec,
                    handle,
                    ready_timeout=ready_timeout,
                    heartbeat_interval=heartbeat_interval,
                    heartbeat_timeout=heartbeat_timeout,
                    wall_budget=wall_budget,
                    wall_deadline=deadline,
                    allow_pool=allow_pool,
                )
            finally:
                if acquired and semaphore is not None:
                    semaphore.release()

        try:
            # Admission-time balancing (solution C): resolve (provider, model)
            # for un-pinned ``model_candidates`` tasks only now that the task
            # owns an admission slot, so the usage-debt ledger reflects every
            # usage event already folded by earlier admissions. The decision
            # is idempotent across restarts (a resolved spec carries a model).
            self._resolve_assignment(spec)
            await self._emit_provider_infeasible(spec)
            assigned_payload: dict[str, Any] = {
                "task_id": task_id,
                "repo": str(repo),
                "branch": spec["branch"],
                "base_commit": spec["base_commit"],
                "task": spec.get("task", ""),
            }
            fanout_config = spec.get("fanout_config")
            if isinstance(fanout_config, dict) and isinstance(fanout_config.get("model"), str):
                assigned_payload["model"] = fanout_config["model"]
            if isinstance(spec.get("assigned_provider"), str):
                assigned_payload["assigned_provider"] = spec["assigned_provider"]
            if isinstance(spec.get("requirements"), dict) and spec["requirements"]:
                assigned_payload["requirements"] = spec["requirements"]
            await self.emit("task_assigned", **assigned_payload)
            restarts = 0
            worker_summary: str | None = None
            resume_payload: dict[str, Any] | None
            # Eval-3 ADOPT: only the first generation may pop the warm pool.
            # Restart generations always spawn a fresh process (a restarted
            # worker must never reuse a pooled process).
            first_generation = True
            while True:
                if deadline is None:
                    deadline = time.monotonic() + wall_budget
                if deadline - time.monotonic() <= 0:
                    detail = _wall_timeout_detail(wall_budget, deadline, restarts)
                    reason = f"wall ({detail})"
                    await self.emit(
                        "timeout",
                        task_id=task_id,
                        generation=generation,
                        phase="wall",
                        detail=detail,
                    )
                    self._results[task_id] = TaskResult(
                        task_id=task_id,
                        status="failed",
                        exit_code=1,
                        reason=reason,
                        restarts=restarts,
                        summary=worker_summary,
                    )
                    return
                handle = WorkerHandle(task_id=task_id, generation=generation)
                self._handles[task_id] = handle
                outcome = await drive_with_admission_slot(handle, allow_pool=first_generation)
                first_generation = False
                sanitized_envelope: dict[str, Any] | None = None
                if outcome.envelope is not None and outcome.correlated:
                    sanitized_envelope = self._redact_envelope(outcome.envelope)
                    worker_summary = _envelope_text(sanitized_envelope, "summary")
                    # A correlated envelope from a crashed generation is only
                    # provisional. Retain it for the result only after that
                    # generation supplied its required terminal exit message.
                    if outcome.clean:
                        self._last_envelope = sanitized_envelope
                        salvage_ref = self._salvage_refs.get(task_id)
                        if salvage_ref is not None:
                            sanitized_envelope["salvage_ref"] = salvage_ref
                        self._task_envelopes[task_id] = sanitized_envelope
                wall_detail = (
                    _wall_timeout_detail(wall_budget, deadline, restarts)
                    if outcome.timeout_phase == "wall"
                    else None
                )
                generation_reason = (
                    f"wall ({wall_detail})" if wall_detail is not None else outcome.reason
                )
                if outcome.clean:
                    envelope_status = (
                        outcome.envelope.get("status") if outcome.envelope is not None else None
                    )
                    parent_envelope = (
                        self._redact_envelope(
                            self._strict_envelope(spec, cast(dict[str, Any], outcome.envelope))
                        )
                        if outcome.envelope is not None
                        else {}
                    )
                    decision_failure_reason = _envelope_text(sanitized_envelope, "failure_reason")
                    if envelope_status == "suspended" and not self._context_reuse:
                        # Fail closed: without the flag a suspended verdict is
                        # an unsupported status, never a resume loop.
                        await self._reject_child_proposals(
                            task_id,
                            outcome.proposals,
                            reason="UnsupportedSuspension",
                            message="context reuse is disabled for this parent",
                        )
                        envelope_status = None
                    if envelope_status == "suspended":
                        # Snapshot isolation: the worker owns the suspension
                        # commit; children integrate privately; only the
                        # resumed and verified parent may publish to main.
                        (
                            snapshot_head,
                            snapshot_error,
                        ) = await self._accept_parent_suspension_snapshot(
                            spec, worktree, generation
                        )
                        if snapshot_error is not None or snapshot_head is None:
                            reason = snapshot_error or "parent_snapshot_failed"
                            await self.emit(
                                "worker_failed",
                                task_id=task_id,
                                generation=generation,
                                reason=reason,
                            )
                            await self._reject_child_proposals(
                                task_id,
                                outcome.proposals,
                                reason="ParentSnapshotFailed",
                                message="parent suspension snapshot failed integrity checks",
                            )
                            self._results[task_id] = TaskResult(
                                task_id=task_id,
                                status="failed",
                                exit_code=1,
                                reason=reason,
                                restarts=restarts,
                                summary=worker_summary,
                            )
                            return
                        child_ids = await self._admit_generation_children(
                            spec,
                            parent_envelope,
                            outcome.proposals,
                            include_port=False,
                            private_integration_base=snapshot_head,
                        )
                        remaining = deadline - time.monotonic()
                        if remaining <= 0:
                            detail = _wall_timeout_detail(wall_budget, deadline, restarts)
                            reason = f"wall ({detail})"
                            await self.emit(
                                "context_resume_failed",
                                task_id=task_id,
                                generation=generation,
                                reason=reason,
                            )
                            self._results[task_id] = TaskResult(
                                task_id=task_id,
                                status="failed",
                                exit_code=1,
                                reason=reason,
                                restarts=restarts,
                                summary=worker_summary,
                            )
                            return
                        await self._await_suspend_children(task_id, remaining)
                        if not await self._assert_parent_join_invariant(
                            spec, child_ids, generation
                        ):
                            await self._reject_child_proposals(
                                task_id,
                                outcome.proposals,
                                reason="ParentJoinInvariantFailed",
                                message=(
                                    "parent worktree was not at the accepted child integration head"
                                ),
                            )
                            self._results[task_id] = TaskResult(
                                task_id=task_id,
                                status="failed",
                                exit_code=1,
                                reason="join_invariant_failed",
                                restarts=restarts,
                                summary=worker_summary,
                            )
                            return
                        resume_payload = self._child_results_for_resume(
                            task_id,
                            child_ids,
                            checkpoint_ref=cast(dict[str, Any], outcome.envelope).get(
                                "checkpoint_ref"
                            ),
                            epoch=cast(dict[str, Any], outcome.envelope).get("epoch"),
                        )
                        # This critical lifecycle event is the last durable
                        # barrier before the next worker spawn.  A store
                        # failure raises and the resume is not attempted.
                        await self.emit(
                            "context_resume",
                            task_id=task_id,
                            generation=generation,
                            epoch=cast(dict[str, Any], outcome.envelope).get("epoch"),
                            checkpoint_ref=cast(dict[str, Any], outcome.envelope).get(
                                "checkpoint_ref"
                            ),
                            child_count=len(child_ids),
                            workspace_changed=resume_payload["workspace_changed"],
                        )
                        spec["resume"] = resume_payload
                        spec.pop("context_fork", None)
                        continue
                    if envelope_status != "succeeded":
                        failure_reason = (
                            outcome.reason
                            or _envelope_text(sanitized_envelope, "failure_reason")
                            or "worker_verdict_failed"
                        )
                        if self._admission_port is not None:
                            await self._admit_port_proposals(
                                spec,
                                parent_envelope,
                                failure_reason=decision_failure_reason or failure_reason,
                                admit_proposals=False,
                            )
                        await self._reject_child_proposals(
                            task_id,
                            outcome.proposals,
                            reason="ParentResultRejected",
                            message="parent result did not report succeeded",
                        )
                        if spec.get("resume") is not None:
                            await self.emit(
                                "context_resume_failed",
                                task_id=task_id,
                                generation=generation,
                                reason=failure_reason,
                            )
                        self._results[task_id] = TaskResult(
                            task_id=task_id,
                            status="failed",
                            exit_code=1,
                            reason=failure_reason,
                            restarts=restarts,
                            summary=worker_summary,
                        )
                        return
                    head: str | None = None
                    integrity = await self._worker_success_integrity(spec, worktree)
                    if integrity is None or integrity == "worker_tree_dirty":
                        head = await self._git_stdout(
                            worktree, "rev-parse", "--verify", "HEAD^{commit}", check=False
                        )
                        if head is None:
                            integrity = "worker_head_failed"
                    if (
                        integrity == "worker_tree_dirty"
                        and head is not None
                        and _success_invariant_violation(
                            spec,
                            cast(dict[str, Any], outcome.envelope),
                            head,
                        )
                    ):
                        integrity = "success invariant violated"
                        spec["_defer_cleanup"] = True
                    if integrity is not None:
                        await self.emit(
                            "worker_failed",
                            task_id=task_id,
                            generation=generation,
                            reason=integrity,
                        )
                        await self._reject_child_proposals(
                            task_id,
                            outcome.proposals,
                            reason="ParentIntegrityFailed",
                            message="parent success was rejected by supervisor integrity checks",
                        )
                        self._results[task_id] = TaskResult(
                            task_id=task_id,
                            status="failed",
                            exit_code=1,
                            reason=integrity,
                            restarts=restarts,
                            summary=worker_summary,
                        )
                        return
                    if _success_invariant_violation(
                        spec,
                        cast(dict[str, Any], outcome.envelope),
                        cast(str, head),
                    ):
                        spec["_defer_cleanup"] = True
                        reason = "success invariant violated"
                        await self.emit(
                            "worker_failed",
                            task_id=task_id,
                            generation=generation,
                            reason=reason,
                        )
                        await self._reject_child_proposals(
                            task_id,
                            outcome.proposals,
                            reason="ParentIntegrityFailed",
                            message="parent success was rejected by supervisor integrity checks",
                        )
                        self._results[task_id] = TaskResult(
                            task_id=task_id,
                            status="failed",
                            exit_code=1,
                            reason=reason,
                            restarts=restarts,
                            summary=worker_summary,
                        )
                        return
                    if spec.get("_resolver_child"):
                        # The parent task owns publication ordering for a
                        # resolver.  Keep this clean, committed branch alive
                        # until _resolve_merge_conflict has checked the join
                        # invariant immediately before publishing it.
                        spec["_retain_worktree"] = True
                        await self._reject_child_proposals(
                            task_id,
                            outcome.proposals,
                            reason="ResolverResultRejected",
                            message="resolver children cannot admit nested children",
                        )
                        self._results[task_id] = TaskResult(
                            task_id=task_id,
                            status="succeeded",
                            exit_code=0,
                            reason=None,
                            merge_sha=None,
                            restarts=restarts,
                            summary=worker_summary,
                        )
                        return
                    if head == spec["base_commit"] and bool(spec.get("_base_is_published", True)):
                        await self._admit_generation_children(
                            spec,
                            parent_envelope,
                            outcome.proposals,
                            include_port=True,
                            failure_reason=decision_failure_reason,
                        )
                        if spec.get("parent_task_id") is not None:
                            self._capture_child_result(
                                spec,
                                outcome.envelope or {},
                                request_id=(outcome.envelope or {}).get("request_id"),
                                generation=generation,
                            )
                        self._results[task_id] = TaskResult(
                            task_id=task_id,
                            status="succeeded",
                            exit_code=0,
                            reason=None,
                            merge_sha=None,
                            restarts=restarts,
                            summary=worker_summary,
                        )
                        return
                    merged = await self._merge_task(spec, handle)
                    if merged is None:
                        conflict = self._merge_conflicts.get(task_id)
                        if conflict is not None:
                            merged = await self._resolve_merge_conflict(
                                spec,
                                handle,
                                conflict,
                                sanitized_envelope,
                                sanitized_envelope,
                            )
                    if merged is not None:
                        spec["base_commit"] = merged
                        spec["_base_is_published"] = True
                        await self._admit_generation_children(
                            spec,
                            parent_envelope,
                            outcome.proposals,
                            include_port=True,
                            failure_reason=decision_failure_reason,
                        )
                        if spec.get("parent_task_id") is not None:
                            self._capture_child_result(
                                spec,
                                outcome.envelope or {},
                                request_id=(outcome.envelope or {}).get("request_id"),
                                generation=generation,
                            )
                        self._results[task_id] = TaskResult(
                            task_id=task_id,
                            status="succeeded",
                            exit_code=0,
                            reason=None,
                            merge_sha=merged,
                            restarts=restarts,
                            summary=worker_summary,
                        )
                    else:
                        await self._reject_child_proposals(
                            task_id,
                            outcome.proposals,
                            reason="ParentMergeFailed",
                            message="parent success was not accepted by the merge sequencer",
                        )
                        failure_reason = self._resolver_failures.get(task_id, "merge_failed")
                        self._results[task_id] = TaskResult(
                            task_id=task_id,
                            status="failed",
                            exit_code=1,
                            reason=failure_reason,
                            restarts=restarts,
                            summary=worker_summary,
                        )
                    return
                if outcome.fatal:
                    await self._reject_child_proposals(
                        task_id,
                        outcome.proposals,
                        reason="ParentResultRejected",
                        message="parent generation failed before a usable terminal result",
                    )
                    self._results[task_id] = TaskResult(
                        task_id=task_id,
                        status="failed",
                        exit_code=1,
                        reason=generation_reason,
                        restarts=restarts,
                        summary=worker_summary,
                    )
                    return
                reason = generation_reason or "crash"
                if outcome.timeout_phase:
                    timeout_payload: dict[str, Any] = (
                        {"detail": wall_detail} if wall_detail is not None else {}
                    )
                    await self.emit(
                        "timeout",
                        task_id=task_id,
                        generation=generation,
                        phase=outcome.timeout_phase,
                        **timeout_payload,
                    )
                if restarts >= max_restarts:
                    await self.emit(
                        "worker_failed",
                        task_id=task_id,
                        generation=generation,
                        restarts=restarts,
                        max_restarts=max_restarts,
                        reason=reason,
                    )
                    await self._reject_child_proposals(
                        task_id,
                        outcome.proposals,
                        reason="ParentResultRejected",
                        message="parent generation exhausted its restart budget",
                    )
                    self._results[task_id] = TaskResult(
                        task_id=task_id,
                        status="failed",
                        exit_code=1,
                        reason=f"max_restarts ({max_restarts}): {reason}",
                        restarts=restarts,
                        summary=worker_summary,
                    )
                    return
                restarts += 1
                delay = random.uniform(
                    0.0, min(RESTART_MAX_DELAY_S, RESTART_BASE_DELAY_S * 2**restarts)
                )
                await self.emit(
                    "restart_scheduled",
                    task_id=task_id,
                    generation=generation,
                    restart_count=restarts,
                    max_restarts=max_restarts,
                    delay_s=round(delay, 3),
                    reason=reason,
                )
                # Backoff is bounded by RESTART_MAX_DELAY_S and is not charged
                # to the restarted attempt's fresh wall window.
                await asyncio.sleep(delay)
                # A restart is a fresh attempt, not a continuation: grant a
                # new wall window so a tight budget cannot make
                # ``max_restarts`` unreachable (suspensions never get this
                # reset; they stay on the original deadline). ``None`` makes
                # the window start lazily when the restarted generation
                # actually runs — worktree recovery above all else must not
                # consume it.
                deadline = None
                resume_payload = await self._checkpoint_resume_payload(spec)
                if resume_payload is not None:
                    spec["resume"] = resume_payload
                    spec["_turn_resume_ref"] = resume_payload["checkpoint_ref"]
                    generation = await self._reuse_worktree(spec, generation + 1)
                else:
                    turn_resume_ref = spec.pop("_turn_resume_ref", None)
                    current_resume = spec.get("resume")
                    if (
                        isinstance(turn_resume_ref, str)
                        and isinstance(current_resume, dict)
                        and current_resume.get("checkpoint_ref") == turn_resume_ref
                    ):
                        spec.pop("resume", None)
                    generation = await self._recover_worktree(spec, generation + 1)
                if spec.get("_resolver_child"):
                    await self._prepare_resolver_worktree(spec)
        finally:
            if semaphore is not None and semaphore_held:
                semaphore.release()

    # -- warm worker pool (eval-3 ADOPT) ------------------------------------

    def _pool_pop(self, cmd: list[str], env: dict[str, str]) -> asyncio.subprocess.Process | None:
        """Return a matching live pooled process, or ``None`` to spawn fresh.

        Matching requires the exact spawn command and the task env modulo the
        per-task overrides (``_pool_env_key``): a pooled worker's env is fixed
        at spawn, so a rebind is only valid when the new task's env is
        identical on every key the worker can observe. Entries whose process
        died while idle are dropped silently. Synchronous (no awaits) so
        concurrent supervise tasks never pop the same process.
        """
        if not self._pool:
            return None
        want_cmd = tuple(cmd)
        want_env = _pool_env_key(env)
        for index in range(len(self._pool) - 1, -1, -1):
            entry = self._pool[index]
            if entry.cmd != want_cmd or entry.env_key != want_env:
                continue
            if entry.proc.returncode is not None:
                self._pool.pop(index)
                continue
            self._pool.pop(index)
            return entry.proc
        return None

    async def _pool_return(
        self, proc: asyncio.subprocess.Process, cmd: list[str], env: dict[str, str]
    ) -> None:
        """Return a live reuse-ready process to the warm pool, or kill it.

        The process is pooled only when the pool is enabled, not full, and
        the process is still alive; otherwise it is killed and reaped as a
        fresh spawn would be. Dead idle entries are dropped silently.
        """
        if self._warm_pool_size <= 0 or proc.returncode is not None:
            await self._kill_pooled(proc)
            return
        self._pool = [entry for entry in self._pool if entry.proc.returncode is None]
        if len(self._pool) >= self._warm_pool_size:
            await self._kill_pooled(proc)
            return
        self._pool.append(_PooledWorker(proc=proc, cmd=tuple(cmd), env_key=_pool_env_key(env)))

    @staticmethod
    async def _kill_pooled(proc: asyncio.subprocess.Process) -> None:
        """Kill one pooled process and reap it (no zombies left behind)."""
        await _kill_worker(proc)
        try:
            await asyncio.wait_for(proc.wait(), WORKER_EXIT_WAIT_S)
        except (TimeoutError, asyncio.CancelledError):
            pass

    async def _report_outbound_message_too_long(self, task_id: str, generation: int) -> None:
        await self.emit(
            "protocol",
            task_id=task_id,
            generation=generation,
            error_type="OUTBOUND_MESSAGE_TOO_LONG",
            note="outbound message exceeds MAX_LINE_BYTES",
        )

    def _build_generation_init_message(
        self,
        spec: dict[str, Any],
        worktree: Path,
        task_id: str,
        generation: int,
        heartbeat_interval: float,
        heartbeat_timeout: float,
        remaining_wall_budget: float,
    ) -> tuple[str, dict[str, Any]]:
        init_rid = self._next_rid()
        init_msg: dict[str, Any] = {
            "type": "init",
            "request_id": init_rid,
            "task_id": task_id,
            "proto": PROTO,
            "generation": generation,
            "worktree": str(worktree),
            "base_commit": spec["base_commit"],
            "spec": spec.get("task", ""),
            "max_turns": int(spec.get("max_turns", DEFAULT_MAX_TURNS)),
            "max_tokens": int(spec.get("max_tokens", DEFAULT_MAX_TOKENS)),
            "heartbeat": {"interval_s": heartbeat_interval, "timeout_s": heartbeat_timeout},
            "budget": {
                "max_wall_s": max(0.0, remaining_wall_budget),
                "max_restarts": DEFAULT_MAX_RESTARTS,
            },
            "permissions": {"shell": True, "network": False},
            "provider_env_keys": list(spec.get("provider_env_keys", ())),
            "authorized_providers": list(spec.get("authorized_providers", ())),
            "authorized_providers_explicit": bool(spec.get("authorized_providers_explicit")),
        }
        if isinstance(spec.get("requirements"), dict) and spec["requirements"]:
            # Keep the validated task contract on the worker init message; the
            # worker owns the live Diffundo instance and must rebuild the same
            # hard request fields for every provider call.
            init_msg["requirements"] = dict(spec["requirements"])
        if self._debt_store is not None:
            debt = self._debt_store.as_mapping()
            if debt:
                init_msg["debt"] = {
                    name: {
                        "requests": entry.requests,
                        "cache_hit_count": entry.cache_hit_count,
                        "latency_total_s": entry.latency_total_s,
                        "latency_count": entry.latency_count,
                        "last_seen": entry.last_seen,
                    }
                    for name, entry in debt.items()
                }
        if self._warm_pool_size > 0:
            # Eval-3 ADOPT opt-in: the worker stays alive after its task and
            # accepts a rebind init instead of exiting. 0 disables the pool
            # and keeps the single-init worker behavior unchanged.
            init_msg["worker_reuse"] = True
        if spec.get("fanout_config") is not None:
            init_msg["fanout_config"] = spec["fanout_config"]
            init_msg["provider_env_keys"] = sorted(_provider_env_keys(spec))
        if isinstance(spec.get("assigned_provider"), str):
            # Admission balancing (solution C): the worker presets Diffundo's
            # sticky primary from this value instead of the seeded first pick.
            init_msg["assigned_provider"] = spec["assigned_provider"]
        if self._context_reuse:
            init_msg["context_reuse"] = True
            init_msg["rolling_compact"] = True
        if isinstance(spec.get("context_fork"), dict):
            init_msg["context_fork"] = spec["context_fork"]
        if isinstance(spec.get("summary_trunk_ref"), str):
            init_msg["summary_trunk_ref"] = spec["summary_trunk_ref"]
        if isinstance(spec.get("resume"), dict):
            init_msg["resume"] = spec["resume"]
        if spec.get("_resolver_child"):
            init_msg["resolver_child"] = True
            init_msg["resolver_write_authority"] = {
                "worktree": str(worktree),
                "branch": spec["branch"],
            }
        return init_rid, init_msg

    async def _admit_generation(
        self,
        spec: dict[str, Any],
        handle: WorkerHandle,
        *,
        ready_timeout: float,
        heartbeat_interval: float,
        heartbeat_timeout: float,
        wall_budget: float,
        wall_deadline: float | None,
        allow_pool: bool,
    ) -> _GenerationState | _GenOutcome:
        task_id = spec["task_id"]
        worktree = Path(spec["worktree_path"])
        generation = handle.generation
        loop = asyncio.get_running_loop()
        absolute_wall_deadline = (
            loop.time() + wall_budget if wall_deadline is None else wall_deadline
        )
        remaining_wall_budget = absolute_wall_deadline - loop.time()
        if remaining_wall_budget <= 0:
            return _GenOutcome(
                clean=False,
                fatal=True,
                reason="wall budget exhausted",
                timeout_phase="wall",
            )
        cmd = self._worker_command(spec)
        env = self._worker_env(spec, generation)
        # Proposals are tagged with this generation and returned with its
        # outcome. They are admitted only by _supervise after the appropriate
        # parent verdict; a restart can therefore never consume stale input.
        init_rid, init_msg = self._build_generation_init_message(
            spec,
            worktree,
            task_id,
            generation,
            heartbeat_interval,
            heartbeat_timeout,
            remaining_wall_budget,
        )
        if encode_message(init_msg) is None:
            await self._report_outbound_message_too_long(task_id, generation)
            return _GenOutcome(
                clean=False,
                fatal=True,
                reason=OUTBOUND_MESSAGE_TOO_LONG,
            )

        # Eval-3 ADOPT warm pool: the first generation of a task may pop a
        # matching idle worker instead of spawning. Restart generations pass
        # allow_pool=False and always spawn fresh: a restarted task must
        # never reuse a pooled process (it may have run the same task).
        pooled = self._pool_pop(cmd, env) if allow_pool else None
        if pooled is not None:
            await self.emit("worker_reused", task_id=task_id, generation=generation, pid=pooled.pid)
        if pooled is None:
            # Record which provider credential NAMES the worker env carries —
            # never values. A cascade fallback failing with AUTH_ERROR for a
            # name absent here is a supervisor injection defect, not a
            # provider outage; without this the delivery hop is unauditable.
            await self.emit(
                "spawned",
                task_id=task_id,
                generation=generation,
                worker=" ".join(cmd),
                provider_env_keys=sorted(
                    key for key in env if key.startswith("CAMBIUM_PROVIDER_") and env[key]
                ),
            )
        try:
            if pooled is not None:
                proc = pooled
            else:
                proc = await asyncio.create_subprocess_exec(
                    *cmd,
                    stdin=asyncio.subprocess.PIPE,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    limit=WORKER_STDIN_LIMIT,
                    cwd=str(worktree),
                    env=env,
                    start_new_session=True,
                    pass_fds=(),
                    close_fds=True,
                )
        except (FileNotFoundError, OSError, PermissionError) as exc:
            return _GenOutcome(clean=False, fatal=True, reason=f"spawn failed: {exc}")
        handle.proc = proc
        handle.state = "SPAWNING"
        return _GenerationState(
            task_id=task_id,
            spec=spec,
            handle=handle,
            worktree=worktree,
            generation=generation,
            loop=loop,
            wall_deadline=absolute_wall_deadline,
            cmd=cmd,
            env=env,
            init_rid=init_rid,
            init_msg=init_msg,
            proc=proc,
            messages=asyncio.Queue(maxsize=WORKER_STDOUT_QUEUE_MAXSIZE),
            heartbeat_timeout=heartbeat_timeout,
        )

    async def _read_generation_stdout(
        self, state: _GenerationState, stdout: asyncio.StreamReader
    ) -> None:
        proc = state.proc
        try:
            async for raw in stdout:
                line = raw.decode("utf-8", "replace").rstrip("\n")
                if not line.strip():
                    continue
                try:
                    msg = json.loads(line)
                except json.JSONDecodeError as exc:
                    state.parse_errors += 1
                    await self.emit(
                        "parse_error",
                        task_id=state.task_id,
                        generation=state.generation,
                        message=str(exc)[:256],
                    )
                    if state.parse_errors > MAX_PARSE_ERRORS:
                        try:
                            os.killpg(proc.pid, signal.SIGKILL)
                        except (ProcessLookupError, PermissionError, OSError):
                            pass
                    continue
                if not isinstance(msg, dict):
                    # A valid JSON line that is not an object cannot be a
                    # protocol message; count and skip it up to the same
                    # bound as unparseable lines (agents.md boundary
                    # invariants: framing never fails supervision on
                    # non-object JSON).
                    state.parse_errors += 1
                    await self.emit(
                        "parse_error",
                        task_id=state.task_id,
                        generation=state.generation,
                        message="valid JSON line is not an object",
                    )
                    if state.parse_errors > MAX_PARSE_ERRORS:
                        try:
                            os.killpg(proc.pid, signal.SIGKILL)
                        except (ProcessLookupError, PermissionError, OSError):
                            pass
                    continue
                await state.messages.put(msg)
        except (ValueError, asyncio.LimitOverrunError) as exc:
            state.message_too_long = True
            await self.emit(
                "protocol",
                task_id=state.task_id,
                generation=state.generation,
                note="MessageTooLong",
                message=str(exc)[:256],
            )
            await _kill_worker(proc)
        finally:
            current = asyncio.current_task()
            if current is None or not current.cancelling():
                await state.messages.put(None)

    async def _read_generation_stderr(
        self, state: _GenerationState, stderr: asyncio.StreamReader
    ) -> None:
        async for raw in stderr:
            line = raw.decode("utf-8", "replace").rstrip("\n")
            if line.strip():
                if self._redactor is not None:
                    line = self._redactor.redact_escaped(line)
                state.stderr_tail = line[:512]
                await self.emit(
                    "log",
                    task_id=state.task_id,
                    generation=state.generation,
                    stream="stderr",
                    message=line[:512],
                )

    async def _start_generation(self, state: _GenerationState, ready_timeout: float) -> None:
        stdout = cast(asyncio.StreamReader, state.proc.stdout)
        stderr = cast(asyncio.StreamReader, state.proc.stderr)
        state.stdout_task = asyncio.create_task(self._read_generation_stdout(state, stdout))
        state.stderr_task = asyncio.create_task(self._read_generation_stderr(state, stderr))
        await self.emit(
            "init",
            task_id=state.task_id,
            request_id=state.init_rid,
            generation=state.generation,
        )
        init_written = await _write_json(
            state.proc,
            state.init_msg,
            deadline=_stdin_deadline(state.wall_deadline),
        )
        state.phase = "ready"
        state.ready_deadline = (
            state.loop.time() + ready_timeout if init_written else state.loop.time()
        )
        state.timeout_phase = "stdin" if not init_written else None

    async def _cancel_generation(self, state: _GenerationState) -> None:
        cancel_msg = {
            "type": "cancel",
            "request_id": self._next_rid(),
            "reason": state.timeout_phase or "timeout",
        }
        try:
            if encode_message(cancel_msg) is None:
                await self._report_outbound_message_too_long(state.task_id, state.generation)
            else:
                await _write_json(
                    state.proc,
                    cancel_msg,
                    deadline=_stdin_deadline(state.wall_deadline),
                )
        except (
            OSError,
            subprocess.SubprocessError,
            TimeoutError,
            TypeError,
            ValueError,
        ) as exc:
            print(f"cambium: cancel message error: {exc}", file=sys.stderr)
        await _kill_worker(state.proc)

    async def _probe_after_eof(self, state: _GenerationState) -> bool:
        """Require one exact pong before treating an EOF survivor as live."""
        if state.proc.returncode is not None:
            return False
        pong_rid = self._next_rid()
        pong_deadline = min(state.wall_deadline, state.loop.time() + PONG_DEADLINE_S)
        ping_msg = {
            "type": "ping",
            "request_id": pong_rid,
            "task_id": state.task_id,
            "generation": state.generation,
        }
        await self.emit(
            "ping",
            task_id=state.task_id,
            generation=state.generation,
            request_id=pong_rid,
        )
        if encode_message(ping_msg) is None:
            state.protocol_failure = OUTBOUND_MESSAGE_TOO_LONG
            await self._report_outbound_message_too_long(state.task_id, state.generation)
            await _kill_worker(state.proc)
            return False
        if not await _write_json(
            state.proc,
            ping_msg,
            deadline=pong_deadline,
        ):
            state.timeout_phase = "pong"
            await self.emit(
                "protocol",
                task_id=state.task_id,
                generation=state.generation,
                note="missing correlated pong after EOF",
                expected=pong_rid,
            )
            return False
        while state.loop.time() < pong_deadline:
            remaining = pong_deadline - state.loop.time()
            try:
                response = await asyncio.wait_for(state.messages.get(), remaining)
            except TimeoutError:
                break
            if response is None:
                break
            if _protocol_version_mismatch(response):
                state.protocol_failure = "PROTO_VERSION_MISMATCH"
                await self.emit(
                    "protocol",
                    task_id=state.task_id,
                    generation=state.generation,
                    error_type=state.protocol_failure,
                    expected=PROTO,
                    got=response.get("proto"),
                )
                return False
            if response.get("type") != "pong":
                await self.emit(
                    "protocol",
                    task_id=state.task_id,
                    generation=state.generation,
                    note="unexpected message during EOF pong probe",
                    type=response.get("type"),
                )
                continue
            if response.get("request_id") != pong_rid:
                await self.emit(
                    "protocol",
                    task_id=state.task_id,
                    generation=state.generation,
                    note="pong request_id mismatch",
                    expected=pong_rid,
                    got=response.get("request_id"),
                )
                continue
            await self.emit(
                "pong",
                task_id=state.task_id,
                generation=state.generation,
                request_id=pong_rid,
            )
            return True
        state.timeout_phase = "pong"
        await self.emit(
            "protocol",
            task_id=state.task_id,
            generation=state.generation,
            note="missing correlated pong after EOF",
            expected=pong_rid,
        )
        return False

    async def _handle_generation_eof(self, state: _GenerationState) -> None:
        await self.emit(
            "log",
            task_id=state.task_id,
            generation=state.generation,
            message="stdout EOF; grace then poll",
        )
        await asyncio.sleep(min(EOF_GRACE_S, max(0.0, state.wall_deadline - state.loop.time())))
        if state.proc.returncode is None:
            probe_ok = await self._probe_after_eof(state)
            if probe_ok:
                try:
                    await asyncio.wait_for(
                        state.proc.wait(),
                        min(EOF_GRACE_S, max(0.0, state.wall_deadline - state.loop.time())),
                    )
                except TimeoutError:
                    await self.emit(
                        "log",
                        task_id=state.task_id,
                        generation=state.generation,
                        message="EOF survivor did not exit after correlated pong",
                    )
                    await _kill_worker(state.proc)
            else:
                await self.emit(
                    "log",
                    task_id=state.task_id,
                    generation=state.generation,
                    message="EOF survivor has no correlated pong; killing process group",
                )
                await _kill_worker(state.proc)

    async def _check_generation_deadline(self, state: _GenerationState) -> bool:
        now = state.loop.time()
        if now >= state.wall_deadline:
            state.timeout_phase = "wall"
            await self._cancel_generation(state)
            return True
        if state.phase == "ready" and now >= state.ready_deadline:
            if state.timeout_phase is None:
                state.timeout_phase = "ready"
            await self._cancel_generation(state)
            return True
        if (
            state.phase == "run"
            and state.last_heartbeat is not None
            and now - state.last_heartbeat > state.heartbeat_timeout
        ):
            state.timeout_phase = "heartbeat"
            await self._cancel_generation(state)
            return True
        return False

    def _generation_next_deadline(self, state: _GenerationState) -> float:
        next_deadline = state.wall_deadline
        if state.phase == "ready":
            next_deadline = min(next_deadline, state.ready_deadline)
        if state.phase == "run" and state.last_heartbeat is not None:
            next_deadline = min(next_deadline, state.last_heartbeat + state.heartbeat_timeout)
        return next_deadline

    async def _handle_ready_message(self, state: _GenerationState, msg: dict[str, Any]) -> bool:
        if msg.get("request_id") != state.init_rid:
            state.protocol_reason = "ready_request_id_mismatch"
            await self.emit(
                "protocol",
                task_id=state.task_id,
                generation=state.generation,
                request_id=msg.get("request_id"),
                code=PROTO_UNKNOWN_REQUEST_ID,
                note="ready request_id mismatch",
                expected=state.init_rid,
                got=msg.get("request_id"),
            )
            await _kill_worker(state.proc)
            return True
        state.phase = "run"
        state.last_heartbeat = state.loop.time()
        state.handle.state = "RUNNING"
        await self.emit(
            "ready",
            task_id=state.task_id,
            request_id=msg.get("request_id"),
            generation=state.generation,
            pid=msg.get("pid"),
            proto=msg.get("proto"),
        )
        state.run_rid = self._next_rid()
        payload = self._run_payload(
            state.spec,
            max(0.0, state.wall_deadline - state.loop.time()),
            state.generation,
        )
        run_msg = {
            "type": "run_task",
            "request_id": state.run_rid,
            "task_id": state.task_id,
            **payload,
        }
        if encode_message(run_msg) is None:
            state.protocol_failure = OUTBOUND_MESSAGE_TOO_LONG
            await self._report_outbound_message_too_long(state.task_id, state.generation)
            await _kill_worker(state.proc)
            return True
        if not await _write_json(
            state.proc,
            run_msg,
            deadline=_stdin_deadline(state.wall_deadline),
        ):
            state.timeout_phase = "stdin"
            await self.emit("protocol", task_id=state.task_id, note="run_task write failed")
            await _kill_worker(state.proc)
            return True
        await self.emit(
            "run_task",
            task_id=state.task_id,
            request_id=state.run_rid,
            generation=state.generation,
        )
        return False

    async def _handle_result_message(self, state: _GenerationState, msg: dict[str, Any]) -> None:
        result_turn = msg.get("turn")
        if type(result_turn) is int and result_turn >= 0:
            state.turn = max(state.turn, result_turn)
        identity_note = _result_identity_note(msg, state.task_id, state.generation)
        state.correlated = state.run_rid is not None and msg.get("request_id") == state.run_rid
        if not state.correlated and identity_note is None:
            identity_note = "result request_id mismatch"
        if identity_note is not None:
            await self.emit(
                "protocol",
                task_id=state.task_id,
                note=identity_note,
                expected=state.run_rid,
                got=msg.get("request_id"),
            )
        if state.envelope is not None:
            # One accepted terminal envelope per run request; a stale or
            # duplicate result never triggers lifecycle side effects a second time.
            await self.emit(
                "protocol",
                task_id=state.task_id,
                generation=state.generation,
                note="duplicate result envelope ignored",
            )
        result_payload: dict[str, Any] = {"status": msg.get("status")}
        provider_metadata = _redacted_provider_metadata(msg.get("provider_metadata"))
        if provider_metadata is not None:
            result_payload["provider_metadata"] = provider_metadata
        terminal_action = _terminal_action_for_event(msg.get("terminal_action"))
        if terminal_action is not None:
            result_payload["terminal_action"] = terminal_action
        await self.emit(
            "result",
            task_id=state.task_id,
            request_id=msg.get("request_id"),
            generation=state.generation,
            **result_payload,
        )
        accepted = state.correlated and identity_note is None and state.envelope is None
        if accepted:
            if state.sandbox_failure_reason is not None and msg.get("status") != "succeeded":
                msg = {**msg, "failure_reason": state.sandbox_failure_reason}
            state.envelope = msg
        # Proposals are retained until this generation returns. In particular,
        # a correlated worker ``succeeded`` envelope is still provisional until
        # _supervise has passed integrity and merge; admitting here would orphan
        # children when that later verdict fails.

    async def _handle_context_checkpoint_message(
        self, state: _GenerationState, msg: dict[str, Any]
    ) -> None:
        invalid = _invalid_context_checkpoint_fields(msg)
        if invalid:
            await self.emit(
                "protocol",
                task_id=state.task_id,
                generation=state.generation,
                note="context_checkpoint rejected: invalid field(s)",
                fields=invalid,
            )
            return
        if (
            msg.get("task_id") != state.task_id
            or type(msg.get("generation")) is not int
            or msg.get("generation") != state.generation
        ):
            await self.emit(
                "protocol",
                task_id=state.task_id,
                generation=state.generation,
                note="context_checkpoint rejected: identity mismatch",
                expected_task_id=state.task_id,
                expected_generation=state.generation,
            )
            return
        self._task_epochs[state.task_id] = dict(msg)
        await self._record_context_checkpoint_conversation(
            state.task_id,
            msg["checkpoint_ref"],
            msg["epoch"],
        )
        await self.emit(
            "context_checkpoint",
            task_id=state.task_id,
            generation=state.generation,
            epoch=msg.get("epoch"),
            turn=msg.get("turn"),
            checkpoint_ref=msg.get("checkpoint_ref"),
            cache_key=msg.get("cache_key"),
        )

    async def _handle_context_epoch_advanced_message(
        self, state: _GenerationState, msg: dict[str, Any]
    ) -> None:
        invalid = _invalid_context_epoch_advanced_fields(msg)
        if invalid:
            await self.emit(
                "protocol",
                task_id=state.task_id,
                generation=state.generation,
                note="context_epoch_advanced rejected: invalid field(s)",
                fields=invalid,
            )
            return
        if msg.get("task_id") != state.task_id or msg.get("generation") != state.generation:
            await self.emit(
                "protocol",
                task_id=state.task_id,
                generation=state.generation,
                note="context_epoch_advanced rejected: identity mismatch",
                expected_task_id=state.task_id,
                expected_generation=state.generation,
            )
            return
        active = self._task_epochs.get(state.task_id)
        if (
            active is None
            or active.get("epoch") != msg["folded_from_epoch"]
            or msg["epoch"] != msg["folded_from_epoch"] + 1
        ):
            await self.emit(
                "protocol",
                task_id=state.task_id,
                generation=state.generation,
                note="context_epoch_advanced rejected: stale transition",
            )
            return
        try:
            await asyncio.to_thread(
                _validate_advanced_epoch_checkpoint,
                self._session_dir,
                state.task_id,
                state.generation,
                msg,
            )
        except (OSError, TypeError, ValueError):
            await self.emit(
                "protocol",
                task_id=state.task_id,
                generation=state.generation,
                note="context_epoch_advanced rejected: invalid checkpoint",
                fields=["checkpoint_ref"],
            )
            return
        await self.emit(
            "context_epoch_advanced",
            task_id=state.task_id,
            generation=state.generation,
            request_id=msg["request_id"],
            epoch=msg["epoch"],
            turn=msg["turn"],
            checkpoint_ref=msg["checkpoint_ref"],
            cache_key=msg["cache_key"],
            folded_from_epoch=msg["folded_from_epoch"],
            reason=msg["reason"],
        )
        self._task_epochs[state.task_id] = dict(msg)

    async def _handle_compaction_failed_message(
        self, state: _GenerationState, msg: dict[str, Any]
    ) -> None:
        invalid = _invalid_compaction_failed_fields(msg)
        if invalid:
            await self.emit(
                "protocol",
                task_id=state.task_id,
                generation=state.generation,
                note="compaction_failed rejected: invalid field(s)",
                fields=invalid,
            )
            return
        if msg.get("task_id") != state.task_id or msg.get("generation") != state.generation:
            await self.emit(
                "protocol",
                task_id=state.task_id,
                generation=state.generation,
                note="compaction_failed rejected: identity mismatch",
                expected_task_id=state.task_id,
                expected_generation=state.generation,
            )
            return
        await self.emit(
            "compaction_failed",
            task_id=state.task_id,
            generation=state.generation,
            request_id=msg["request_id"],
            epoch=msg["epoch"],
            reason=msg["reason"],
        )

    async def _handle_provider_boundary_degraded_message(
        self, state: _GenerationState, msg: dict[str, Any]
    ) -> None:
        invalid = _invalid_provider_boundary_degraded_fields(msg)
        if invalid:
            await self.emit(
                "protocol",
                task_id=state.task_id,
                generation=state.generation,
                note="provider_boundary_degraded rejected: invalid field(s)",
                fields=invalid,
            )
            return
        if msg.get("task_id") != state.task_id or msg.get("generation") != state.generation:
            await self.emit(
                "protocol",
                task_id=state.task_id,
                generation=state.generation,
                note="provider_boundary_degraded rejected: identity mismatch",
                expected_task_id=state.task_id,
                expected_generation=state.generation,
            )
            return
        await self.emit(
            "provider_boundary_degraded",
            task_id=state.task_id,
            generation=state.generation,
            request_id=msg.get("request_id"),
            error_type=msg["error_type"],
        )

    async def _handle_propose_child_message(
        self, state: _GenerationState, msg: dict[str, Any]
    ) -> None:
        invalid_fields = _invalid_propose_child_fields(msg)
        if invalid_fields:
            await self.emit(
                "protocol",
                task_id=state.task_id,
                generation=state.generation,
                note="propose_child rejected: invalid field(s)",
                fields=invalid_fields,
            )
            return
        if msg.get("parent_task_id") != state.task_id:
            await self.emit(
                "protocol",
                task_id=state.task_id,
                generation=state.generation,
                note="propose_child parent_task_id mismatch",
                parent_task_id=msg.get("parent_task_id"),
                child_task_id=msg.get("child_task_id"),
            )
            return
        # Buffered until the parent's terminal envelope arrives; admission then
        # validates the revision against the session tree (build_tree over the
        # accumulated tasks list).
        msg = {**msg, "_parent_budget": _proposal_parent_budget(state)}
        self._pending_children.setdefault(state.task_id, []).append((state.generation, msg))

    async def _handle_reuse_ready_message(
        self, state: _GenerationState, msg: dict[str, Any]
    ) -> bool:
        # Eval-3 ADOPT: the worker delivered its terminal result and waits for a
        # rebind init; keep the live process for the session pool instead of
        # letting it exit.
        if state.envelope is None or not state.correlated:
            state.protocol_reason = "reuse_ready_without_result"
            await self.emit(
                "protocol",
                task_id=state.task_id,
                generation=state.generation,
                error_type=state.protocol_reason,
                note="reuse_ready before a correlated terminal result",
            )
            await _kill_worker(state.proc)
            return True
        state.reuse_ready = True
        state.keep_alive = True
        await self.emit(
            "reuse_ready",
            task_id=state.task_id,
            generation=state.generation,
            pid=msg.get("pid"),
        )
        return True

    async def _handle_exit_message(self, state: _GenerationState, msg: dict[str, Any]) -> bool:
        state.exit_reason = msg.get("reason")
        state.handle.exit_reason = state.exit_reason
        await self.emit(
            "exit",
            task_id=state.task_id,
            reason=state.exit_reason,
            generation=state.generation,
        )
        return True

    async def _handle_heartbeat_message(self, state: _GenerationState, msg: dict[str, Any]) -> None:
        heartbeat_turn = msg.get("turn")
        if type(heartbeat_turn) is int and heartbeat_turn >= 0:
            state.turn = max(state.turn, heartbeat_turn)
        state.last_heartbeat = state.loop.time()
        state.handle.last_heartbeat = state.last_heartbeat
        forwarded = {
            "turn": msg.get("turn"),
            "tool": msg.get("tool"),
            "status": msg.get("status"),
        }
        phase = msg.get("phase")
        # Preserve the original protocol phase value, including its interaction
        # with the ready/run loop phase.
        state.phase = phase
        if type(phase) is str and phase in _HEARTBEAT_PHASES:
            forwarded["phase"] = phase
        tail = msg.get("tail")
        if isinstance(tail, str):
            forwarded["tail"] = sanitize_terminal_text(tail, single_line=True)[:120]
        await self.emit(
            "heartbeat",
            task_id=state.task_id,
            generation=state.generation,
            **forwarded,
        )

    async def _handle_checkpoint_message(
        self, state: _GenerationState, msg: dict[str, Any]
    ) -> None:
        await self.emit(
            "checkpoint",
            task_id=state.task_id,
            turn=msg.get("turn"),
            state_ref=msg.get("state_ref"),
            generation=state.generation,
            commits_so_far=msg.get("commits_so_far"),
        )

    async def _handle_usage_event_message(
        self, state: _GenerationState, msg: dict[str, Any]
    ) -> None:
        identity_valid = (
            msg.get("task_id") == state.task_id
            and type(msg.get("generation")) is int
            and msg.get("generation") == state.generation
        )
        if not identity_valid:
            await self.emit(
                "protocol",
                task_id=state.task_id,
                generation=state.generation,
                note="usage_event rejected: identity mismatch",
                expected_task_id=state.task_id,
                expected_generation=state.generation,
            )
            return
        invalid_fields = _invalid_usage_event_fields(msg)
        if invalid_fields:
            await self.emit(
                "protocol",
                task_id=state.task_id,
                generation=state.generation,
                note="usage_event rejected: invalid field(s)",
                fields=invalid_fields,
            )
            return
        state.sandbox_failure_reason = _sandbox_usage_reason(msg.get("failure_reason"))
        forwarded = {field: msg[field] for field in _USAGE_EVENT_FORWARD_FIELDS if field in msg}
        await self.emit(
            "usage_event",
            task_id=state.task_id,
            generation=state.generation,
            **forwarded,
        )
        # Admission balancing (solution C): fold the redacted usage event into
        # the session debt ledger so later admissions in this session see
        # updated utilization.
        if self._debt_store is not None:
            self._debt_store.record(msg)

    async def _handle_tool_or_pong_message(
        self, state: _GenerationState, msg: dict[str, Any]
    ) -> None:
        mtype = msg.get("type")
        if mtype == "tool_output_delta":
            invalid_fields = _invalid_tool_output_delta_fields(msg)
            if invalid_fields:
                await self.emit(
                    "protocol",
                    task_id=state.task_id,
                    generation=state.generation,
                    note="tool_output_delta rejected: invalid field(s)",
                    fields=invalid_fields,
                )
                return
            event_turn = msg.get("turn")
            if type(event_turn) is int and event_turn >= 0:
                state.turn = max(state.turn, event_turn)
            delta = sanitize_terminal_text(msg["delta"])
            if not delta:
                return
            await self.emit(
                "tool_output_delta",
                task_id=state.task_id,
                generation=state.generation,
                tool=msg["tool"],
                turn=event_turn,
                stream=msg["stream"],
                delta=_cap_utf8(delta, _TOOL_OUTPUT_DELTA_MAX_BYTES),
                monotonic_ms=msg.get("monotonic_ms"),
            )
            return
        forwarded = {"tool": msg.get("tool"), "cmd": msg.get("cmd")}
        if mtype == "tool_event":
            invalid_fields = _invalid_tool_event_fields(msg)
            if invalid_fields:
                await self.emit(
                    "protocol",
                    task_id=state.task_id,
                    generation=state.generation,
                    note="tool_event rejected: invalid field(s)",
                    fields=invalid_fields,
                )
                return
            event_turn = msg.get("turn")
            if type(event_turn) is int and event_turn >= 0:
                state.turn = max(state.turn, event_turn)
            for field in ("batch_index", "batch_size", "ok", "duration_ms", "turn"):
                if field in msg:
                    forwarded[field] = msg[field]
            await self.emit(
                "tool_event",
                task_id=state.task_id,
                generation=state.generation,
                **forwarded,
            )
            return
        await self.emit(
            "log",
            task_id=state.task_id,
            generation=state.generation,
            **forwarded,
        )

    async def _handle_error_message(self, state: _GenerationState, msg: dict[str, Any]) -> None:
        await self.emit(
            "log",
            task_id=state.task_id,
            generation=state.generation,
            stream="worker-error",
            error_type=msg.get("error_type"),
            message=str(msg.get("message", ""))[:512],
        )

    async def _handle_log_message(self, state: _GenerationState, msg: dict[str, Any]) -> None:
        await self.emit(
            "log",
            task_id=state.task_id,
            generation=state.generation,
            message=str(msg.get("message", ""))[:512],
        )

    async def _handle_generation_protocol_message(
        self, state: _GenerationState, msg: dict[str, Any]
    ) -> bool | None:
        mtype = msg.get("type")
        if mtype == "ready":
            return await self._handle_ready_message(state, msg)
        if mtype in ("result", "result_envelope"):
            await self._handle_result_message(state, msg)
            return False
        if mtype == "context_checkpoint":
            await self._handle_context_checkpoint_message(state, msg)
            return False
        if mtype == "context_epoch_advanced":
            await self._handle_context_epoch_advanced_message(state, msg)
            return False
        if mtype == "compaction_failed":
            await self._handle_compaction_failed_message(state, msg)
            return False
        if mtype == "provider_boundary_degraded":
            await self._handle_provider_boundary_degraded_message(state, msg)
            return False
        return None

    async def _handle_generation_lifecycle_message(
        self, state: _GenerationState, msg: dict[str, Any]
    ) -> bool | None:
        mtype = msg.get("type")
        if mtype == "context_fork_skipped":
            await self.emit(
                "context_fork_skipped",
                task_id=state.task_id,
                generation=state.generation,
                reason=msg.get("reason"),
            )
            return False
        if mtype == "propose_child":
            await self._handle_propose_child_message(state, msg)
            return False
        if mtype == "reuse_ready":
            return await self._handle_reuse_ready_message(state, msg)
        if mtype in ("exit", "exit_message"):
            return await self._handle_exit_message(state, msg)
        if mtype == "heartbeat":
            await self._handle_heartbeat_message(state, msg)
            return False
        if mtype == "checkpoint":
            await self._handle_checkpoint_message(state, msg)
            return False
        return None

    async def _handle_generation_event_message(
        self, state: _GenerationState, msg: dict[str, Any]
    ) -> bool | None:
        mtype = msg.get("type")
        if mtype == "usage_event":
            await self._handle_usage_event_message(state, msg)
            return False
        if mtype in ("tool_event", "tool_output_delta", "pong"):
            await self._handle_tool_or_pong_message(state, msg)
            return False
        if mtype == "error":
            await self._handle_error_message(state, msg)
            return False
        if mtype == "log":
            await self._handle_log_message(state, msg)
            return False
        return None

    async def _handle_generation_message(
        self, state: _GenerationState, msg: dict[str, Any]
    ) -> bool:
        mtype = msg.get("type")
        if _protocol_version_mismatch(msg):
            state.protocol_failure = "PROTO_VERSION_MISMATCH"
            await self.emit(
                "protocol",
                task_id=state.task_id,
                generation=state.generation,
                error_type=state.protocol_failure,
                expected=PROTO,
                got=msg.get("proto"),
            )
            await _kill_worker(state.proc)
            return True
        handled = await self._handle_generation_protocol_message(state, msg)
        if handled is not None:
            return handled
        handled = await self._handle_generation_lifecycle_message(state, msg)
        if handled is not None:
            return handled
        handled = await self._handle_generation_event_message(state, msg)
        if handled is not None:
            return handled
        await self.emit(
            "protocol",
            task_id=state.task_id,
            type=mtype,
            note="unhandled message",
            generation=state.generation,
        )
        return False

    async def _drive_generation_loop(self, state: _GenerationState) -> None:
        while True:
            if await self._check_generation_deadline(state):
                break
            next_deadline = self._generation_next_deadline(state)
            remaining = next_deadline - state.loop.time()
            try:
                msg = await asyncio.wait_for(state.messages.get(), max(remaining, 0.0))
            except TimeoutError:
                continue
            if msg is None:
                await self._handle_generation_eof(state)
                break
            if await self._handle_generation_message(state, msg):
                break

    async def _cleanup_generation(self, state: _GenerationState) -> None:
        if not state.keep_alive:
            try:
                await asyncio.wait_for(state.proc.wait(), WORKER_EXIT_WAIT_S)
            except BaseException:
                try:
                    await _kill_worker(state.proc)
                except BaseException:
                    pass
                try:
                    await asyncio.wait_for(state.proc.wait(), WORKER_EXIT_WAIT_S)
                except BaseException:
                    pass
        stdout_task = cast(asyncio.Task[None], state.stdout_task)
        stderr_task = cast(asyncio.Task[None], state.stderr_task)
        for rt in (stdout_task, stderr_task):
            if not rt.done():
                rt.cancel()
        try:
            await asyncio.gather(stdout_task, stderr_task, return_exceptions=True)
        except BaseException:
            pass

    async def _finalize_generation(self, state: _GenerationState) -> _GenOutcome:
        generation_proposals = self._take_generation_proposals(state.task_id, state.generation)
        terminal_verdict = (
            state.envelope is not None
            and state.correlated
            and state.envelope.get("status")
            in ("succeeded", "failed", "cancelled", "suspended", "unresolvable")
        )
        if state.reuse_ready and not state.message_too_long:
            # The worker stays alive and owns no task state; the handle no
            # longer owns the process (the pool does). The generation verdict
            # is clean exactly when the terminal envelope is correlated.
            await self._pool_return(state.proc, state.cmd, state.env)
            state.handle.proc = None
            return _GenOutcome(
                clean=terminal_verdict,
                fatal=False,
                reason=None,
                exit_code=None,
                exit_reason=None,
                envelope=state.envelope,
                correlated=state.correlated,
                reuse_ready=True,
                proposals=generation_proposals,
            )
        exit_code = state.proc.returncode
        state.handle.exit_code = exit_code
        state.handle.state = "EXITED"
        clean = (
            state.exit_reason is not None
            and terminal_verdict
            and (
                exit_code == 0 or cast(dict[str, Any], state.envelope).get("status") != "succeeded"
            )
        )
        reason: str | None
        if state.message_too_long:
            clean = False
            reason = "message_too_long"
        elif clean:
            reason = (
                _worker_failure_reason(
                    self._redact_envelope(state.envelope) if state.envelope is not None else None,
                    None,
                    state.stderr_tail,
                )
                if state.envelope is not None and state.envelope.get("status") != "succeeded"
                else None
            )
        elif state.timeout_phase is not None:
            reason = state.timeout_phase
        elif state.protocol_reason is not None:
            reason = state.protocol_reason
        elif exit_code != 0:
            reason = state.sandbox_failure_reason or (
                f"sandbox_restricted: worker_exit_{exit_code}"
                if exit_code in (126, 127)
                else f"worker_exit_{exit_code}"
            )
        elif state.exit_reason is None:
            reason = "missing_exit_message"
        elif state.envelope is None:
            reason = "missing_result_envelope"
        else:
            reason = "result_request_id_mismatch"
        if not clean:
            reason = _worker_failure_reason(
                self._redact_envelope(state.envelope) if state.envelope is not None else None,
                reason,
                state.stderr_tail,
            )
        return _GenOutcome(
            clean=clean,
            fatal=state.protocol_failure is not None
            or state.protocol_reason == "ready_request_id_mismatch",
            reason=state.protocol_failure or state.protocol_reason or reason,
            timeout_phase=state.timeout_phase,
            exit_code=exit_code,
            exit_reason=state.exit_reason,
            envelope=state.envelope,
            correlated=state.correlated,
            proposals=generation_proposals,
        )

    async def _drive_generation(
        self,
        spec: dict[str, Any],
        handle: WorkerHandle,
        *,
        ready_timeout: float,
        heartbeat_interval: float,
        heartbeat_timeout: float,
        wall_budget: float,
        wall_deadline: float | None = None,
        allow_pool: bool = True,
    ) -> _GenOutcome:
        # Keep the generation lifecycle visible: admission, I/O setup, drive,
        # probe (from the drive loop), cleanup, and finalization.
        admitted = await self._admit_generation(
            spec,
            handle,
            ready_timeout=ready_timeout,
            heartbeat_interval=heartbeat_interval,
            heartbeat_timeout=heartbeat_timeout,
            wall_budget=wall_budget,
            wall_deadline=wall_deadline,
            allow_pool=allow_pool,
        )
        if isinstance(admitted, _GenOutcome):
            return admitted
        state = admitted
        await self._start_generation(state, ready_timeout)
        try:
            await self._drive_generation_loop(state)
        except asyncio.CancelledError:
            try:
                await _kill_worker(state.proc)
            except BaseException:
                pass
            raise
        finally:
            await self._cleanup_generation(state)
        return await self._finalize_generation(state)

    # -- publish eligibility --------------------------------------------------

    async def _worker_success_integrity(self, spec: dict[str, Any], worktree: Path) -> str | None:
        """Reject an unpublishable worker verdict before merging.

        Returns a failure reason when the worker's success claim is not
        backed by a clean, attached worktree: a detached HEAD means the
        worker's commits may be lost, and tracked/untracked modifications
        mean the merge would capture state the worker never claimed. The
        supervisor-owned ``.cambium`` fence directory is exempt.
        """
        worktree = Path(worktree)
        symbolic = await self._git_stdout(worktree, "symbolic-ref", "--quiet", "HEAD", check=False)
        if not symbolic:
            return "worker_detached_head"
        if symbolic != f"refs/heads/{spec['branch']}":
            return "worker_wrong_branch"
        status = await self._git(
            worktree,
            "status",
            "--porcelain=v1",
            "--untracked-files=all",
            "--ignored=matching",
            check=False,
        )
        if status.returncode != 0:
            return "worker_status_failed"
        if any(not _status_line_is_fence(line) for line in status.stdout.splitlines()):
            return "worker_tree_dirty"
        return None

    # -- merge ---------------------------------------------------------------

    def _make_sequencer(
        self,
        task_id: str,
        deferred_observers: list[tuple[dict[str, Any], bool]] | None = None,
    ) -> MergeSequencer:
        loop = asyncio.get_running_loop()

        def persist_terminal(kind: str, payload: dict[str, Any]) -> None:
            event_payload = dict(payload)
            event_task_id = event_payload.pop("task", task_id)
            future = asyncio.run_coroutine_threadsafe(
                self.emit(
                    kind,
                    task_id=event_task_id,
                    _observer_failure_is_fatal=False,
                    _deferred_observers=deferred_observers,
                    **event_payload,
                ),
                loop,
            )
            timeout_s = _durable_event_timeout_s()
            try:
                future.result(timeout=timeout_s)
            except TimeoutError as exc:
                # The emit keeps running on the loop and still appends the
                # event; only the wait is bounded, so a saturated pool cannot
                # circularly deadlock every merge thread. Fail the merge
                # closed rather than silently dropping the terminal event.
                raise RuntimeError(
                    f"durable terminal event {kind!r} not persisted within {timeout_s}s"
                ) from exc

        return MergeSequencer(
            task_id=task_id, session_dir=self._session_dir, durable_event=persist_terminal
        )

    async def _flush_sequencer_events(
        self,
        seq: Any,
        task_keys: dict[str, str] | None = None,
        deferred_observers: list[tuple[dict[str, Any], bool]] | None = None,
    ) -> set[str]:
        if not hasattr(seq, "drain_events"):
            return set()
        prior = {
            (event["kind"], event["payload"].get("quarantine_id"))
            for event in await asyncio.to_thread(self._store.events_after, 0)
            if event["kind"] in ("merge_staging_quarantined", "merge_staging_pruned")
            and event["payload"].get("quarantine_id") is not None
        }
        emitted: set[str] = set()
        for kind, payload in seq.drain_events():
            artifact = (kind, payload.get("quarantine_id"))
            if artifact in prior:
                continue
            task_id = payload.pop("task", None)
            if task_id is None and task_keys is not None:
                quarantine_id = payload.get("quarantine_id", "")
                match = re.match(r"merge/task-([0-9a-f]{16})/", quarantine_id)
                if match:
                    task_id = task_keys.get(match.group(1))
            await self.emit(
                kind, task_id=task_id, _deferred_observers=deferred_observers, **payload
            )
            recovered = kind == "merge_committed" and payload.get("reason") == (
                "recovered-ref-advance"
            )
            if (kind == "merge_reconciled" or recovered) and task_id is not None:
                self._results[task_id] = TaskResult(
                    task_id=task_id,
                    status="succeeded",
                    exit_code=0,
                    reason=None,
                    merge_sha=payload.get("new"),
                )
            emitted.add(kind)
        return emitted

    async def reconcile(self, specs: list[dict[str, Any]]) -> None:
        """Reconcile staging moves and the git-ref/event publish gap on startup."""
        scanned_repos: set[Path] = set()
        durable_quarantines_by_id: dict[str, dict[str, Any]] = {}
        for event in await asyncio.to_thread(self._store.events_after, 0):
            quarantine_id = event["payload"].get("quarantine_id")
            if not isinstance(quarantine_id, str):
                continue
            if event["kind"] == "merge_staging_quarantined":
                durable_quarantines_by_id[quarantine_id] = event["payload"]
            elif event["kind"] == "merge_staging_pruned":
                durable_quarantines_by_id.pop(quarantine_id, None)
        durable_quarantines = list(durable_quarantines_by_id.values())
        task_keys = {
            hashlib.sha256(spec["task_id"].encode()).hexdigest()[:16]: spec["task_id"]
            for spec in specs
        }
        for spec in specs:
            repo = Path(spec["repo"])
            task_id = spec["task_id"]
            task_key = hashlib.sha256(task_id.encode()).hexdigest()[:16]
            throwaway = self._session_dir / ".cambium" / "merge-wt" / f"task-{task_key}"
            seq = self._make_sequencer(task_id)
            current = await asyncio.to_thread(
                seq.reconcile,
                repo,
                throwaway,
                scan_quarantine=repo not in scanned_repos,
                quarantine_events=durable_quarantines,
            )
            scanned_repos.add(repo)
            emitted = await self._flush_sequencer_events(seq, task_keys)
            if "merge_committed" in emitted and getattr(seq, "staging_ref", None) is not None:
                await asyncio.to_thread(seq.cleanup_staging, repo)
                await self._flush_sequencer_events(seq, task_keys)
            if current is None:
                continue
            events = await asyncio.to_thread(self._store.events_after, 0)
            terminal = next(
                (
                    event
                    for event in reversed(events)
                    if event["kind"] in ("merge_committed", "merge_reconciled")
                    and event["payload"].get("new") == current
                    and event.get("task_id") == task_id
                ),
                None,
            )
            if terminal is not None:
                self._results[task_id] = TaskResult(
                    task_id=task_id,
                    status="succeeded",
                    exit_code=0,
                    reason=None,
                    merge_sha=current,
                )
                continue
            refs = (
                await self._git_stdout(
                    repo,
                    "for-each-ref",
                    "--format=%(refname:strip=3) %(objectname)",
                    "refs/cambium/staging",
                    check=False,
                )
                or ""
            )
            owner: str | None = None
            for line in refs.splitlines():
                suffix, _, tip = line.partition(" ")
                key = suffix.split("-", 1)[0]
                if tip == current and key in task_keys:
                    owner = task_keys[key]
                    break
            if owner is not None:
                await self.emit(
                    "merge_reconciled",
                    task_id=owner,
                    new=current,
                    repo=str(repo),
                    reason="ref-advanced-before-event",
                )
                self._results[owner] = TaskResult(
                    task_id=owner,
                    status="succeeded",
                    exit_code=0,
                    reason=None,
                    merge_sha=current,
                )

    def _session_spec(self, task_id: str) -> dict[str, Any] | None:
        """Return an admitted task spec by id, if it is still in the session."""
        for entry in self._session_tasks:
            if entry.get("task_id") == task_id and isinstance(entry.get("spec"), dict):
                return cast(dict[str, Any], entry["spec"])
        return None

    async def _advance_parent_worktree(
        self,
        child_spec: dict[str, Any],
        accepted_head: str,
        expected_old: str,
    ) -> None:
        """Fast-forward a clean suspended parent to a child integration head.

        Publication advances ``refs/heads/main`` without touching any
        worktree.  A suspended parent is the one deliberate exception: when
        its branch is still at the publication's expected old head and its
        tree is clean, fast-forwarding that branch makes the accepted child
        artifact visible to the next parent generation.  If the precondition
        is not true, leave the tree untouched; the join barrier below reports
        the mismatch instead of destroying parent-owned state.
        """
        parent_task_id = child_spec.get("parent_task_id")
        if not isinstance(parent_task_id, str):
            return
        parent_spec = self._session_spec(parent_task_id)
        if parent_spec is None:
            return
        worktree = Path(parent_spec["worktree_path"])
        try:
            parent_head = await self._git_stdout(
                worktree, "rev-parse", "--verify", "HEAD^{commit}", check=False
            )
        except (OSError, RuntimeError, ValueError, subprocess.SubprocessError):
            return
        if parent_head is None or parent_head == accepted_head or parent_head != expected_old:
            return
        try:
            status = await self._git(
                worktree,
                "status",
                "--porcelain=v1",
                "--untracked-files=all",
                "--ignored=matching",
                check=False,
            )
        except (OSError, RuntimeError, ValueError, subprocess.SubprocessError):
            return
        if status.returncode != 0 or any(
            not _status_line_is_fence(line) for line in status.stdout.splitlines()
        ):
            return
        try:
            await self._git(worktree, "merge", "--ff-only", "--no-edit", accepted_head, check=False)
        except (OSError, RuntimeError, ValueError, subprocess.SubprocessError):
            return

    async def _accept_parent_suspension_snapshot(
        self,
        spec: dict[str, Any],
        worktree: Path,
        generation: int,
    ) -> tuple[str | None, str | None]:
        """Accept one worker-owned suspension commit as a private base.

        The worker has already exited and fenced every dirty file into at most
        one commit.  The supervisor verifies a clean attached branch, records
        the transition durably, and only then allows children to branch from
        that immutable snapshot.  The snapshot is not considered published.
        """
        integrity = await self._worker_success_integrity(spec, worktree)
        if integrity is not None:
            return None, integrity
        head = await self._git_stdout(
            worktree, "rev-parse", "--verify", "HEAD^{commit}", check=False
        )
        if head is None:
            return None, "worker_head_failed"
        prior_base = str(spec["base_commit"])
        base_was_published = bool(spec.get("_base_is_published", True))
        base_is_published = base_was_published and head == prior_base
        await self.emit(
            "parent_snapshot",
            task_id=spec["task_id"],
            generation=generation,
            old=prior_base,
            new=head,
            changed=head != prior_base,
            base_is_published=base_is_published,
            branch=spec["branch"],
            repo=spec["repo"],
        )
        spec["base_commit"] = head
        spec["_base_is_published"] = base_is_published
        return head, None

    async def _assert_parent_join_invariant(
        self,
        parent_spec: dict[str, Any],
        child_ids: list[str],
        generation: int,
        *,
        consume: bool = True,
    ) -> bool:
        """Require the parent worktree to be at the accepted child head.

        Resolver publication performs a non-consuming check immediately
        before its ref update, then consumes the newly accepted head after the
        update.  Existing suspend/resume callers retain the consuming default.
        """
        parent_task_id = parent_spec["task_id"]
        integration_head = self._accepted_integration_heads.get(parent_task_id)
        if integration_head is None:
            return True
        worktree = Path(parent_spec["worktree_path"])
        parent_head = await self._git_stdout(
            worktree, "rev-parse", "--verify", "HEAD^{commit}", check=False
        )
        if parent_head == integration_head:
            integrity = await self._worker_success_integrity(parent_spec, worktree)
            if integrity is None:
                if consume:
                    self._accepted_integration_heads.pop(parent_task_id, None)
                return True
        summary = "parent worktree HEAD does not match accepted integration head"
        await self.emit(
            "join_invariant_failed",
            task_id=parent_task_id,
            parent_task_id=parent_task_id,
            generation=generation,
            status="join_invariant_failed",
            reason="parent_worktree_head_mismatch",
            summary=summary,
            integration_head=integration_head,
            accepted_integration_head=integration_head,
            parent_head=parent_head,
            expected_head=integration_head,
            child_task_ids=child_ids[:MAX_ENVELOPE_ITEMS],
        )
        return False

    def _resolver_enabled_for(self, spec: Mapping[str, Any]) -> bool:
        """Return whether this task may admit an automatic resolver child."""
        if spec.get("_resolver_child"):
            return False
        configured = spec.get("resolver_child_enabled")
        if configured is None:
            return self._resolver_child_enabled
        if type(configured) is not bool:
            raise ValueError("resolver_child_enabled must be a boolean")
        return configured

    def _resolver_attempt_limit(self, spec: Mapping[str, Any]) -> int:
        """Resolve the bounded resolver-attempt budget for one task."""
        configured = spec.get("resolver_max_attempts")
        if configured is None:
            configured = spec.get("resolver_attempts", self._resolver_max_attempts)
        if type(configured) is not int or configured < 0:
            raise ValueError("resolver_max_attempts must be a non-negative int")
        return configured

    def _resolver_intent_summaries(
        self,
        spec: Mapping[str, Any],
        envelope: Mapping[str, Any] | None,
        integration_head: str,
    ) -> dict[str, str]:
        """Build bounded intent summaries for both sides of a conflict."""
        worker_intent = _envelope_text(envelope, "summary")
        if worker_intent is None:
            worker_intent = spec.get("task") if isinstance(spec.get("task"), str) else None
        if worker_intent is None:
            worker_intent = f"worker branch {spec.get('branch', '<unknown>')}"

        parent_task_id = spec.get("parent_task_id")
        parent_spec = (
            self._session_spec(parent_task_id) if isinstance(parent_task_id, str) else None
        )
        integration_intent = (
            parent_spec.get("task")
            if parent_spec is not None and isinstance(parent_spec.get("task"), str)
            else None
        )
        if integration_intent is None:
            integration_intent = f"integrate onto refs/heads/main at {integration_head}"
        return {
            "worker": _cap_utf8(worker_intent, MAX_ENVELOPE_FIELD_CHARS),
            "integration": _cap_utf8(integration_intent, MAX_ENVELOPE_FIELD_CHARS),
        }

    def _build_resolver_spec(
        self,
        spec: dict[str, Any],
        conflict: Mapping[str, Any],
        envelope: Mapping[str, Any] | None,
        *,
        attempt: int,
        max_attempts: int,
    ) -> dict[str, Any]:
        """Create a fresh, write-authorized child spec for one conflict attempt."""
        repo = Path(spec["repo"]).resolve()
        integration_head = conflict.get("integration_head")
        if not isinstance(integration_head, str) or not integration_head:
            raise ValueError("merge conflict has no integration head")
        raw_files = conflict.get("conflicted_files")
        conflicted_files = (
            [
                _cap_utf8(path, MAX_ENVELOPE_FIELD_CHARS)
                for path in raw_files[:MAX_ENVELOPE_ITEMS]
                if isinstance(path, str)
            ]
            if isinstance(raw_files, list)
            else []
        )
        raw_evidence = conflict.get("diff_evidence", "")
        diff_evidence = (
            _cap_utf8(raw_evidence, MAX_ENVELOPE_FIELD_CHARS)
            if isinstance(raw_evidence, str)
            else ""
        )
        task_id = spec["task_id"]
        digest = hashlib.sha256(f"{task_id}:{attempt}:{time.time_ns()}".encode()).hexdigest()[:16]
        resolver_task_id = f"{task_id}-resolver-{attempt}-{digest}"
        resolver_branch = f"cambium-resolver/{digest}-{attempt}"
        resolver_worktree = (
            self._session_dir / ".cambium" / "resolver-wt" / f"task-{digest}-{attempt}"
        )
        parent_task_id = spec.get("parent_task_id")
        parent_task_id = parent_task_id if isinstance(parent_task_id, str) else None
        parent_envelope = self._strict_envelope(
            spec, dict(envelope) if envelope is not None else {}
        )
        resolver_worker = spec.get("resolver_worker", spec.get("worker", "cambium.worker"))
        if not isinstance(resolver_worker, str) or not resolver_worker:
            resolver_worker = "cambium.worker"
        intent_summaries = self._resolver_intent_summaries(spec, envelope, integration_head)

        resolver_spec = copy.deepcopy(spec)
        for field in ("proposed_children", "resume", "context_fork", "summary_trunk_ref"):
            resolver_spec.pop(field, None)
        resolver_spec.update(
            {
                "task_id": resolver_task_id,
                "kind": "resolver",
                "task": (
                    "Resolve the merge conflict in "
                    f"{', '.join(conflicted_files) or 'the staged files'}; "
                    "produce a committed merged result or report an explicit unresolvable verdict."
                ),
                "repo": str(repo),
                "worktree_path": str(resolver_worktree.resolve()),
                "branch": resolver_branch,
                "base_commit": integration_head,
                "worker": resolver_worker,
                "parent_task_id": parent_task_id,
                "parent_envelope": parent_envelope,
                "resolver_child_enabled": False,
                "resolver_max_attempts": 0,
                "resolver_attempt": attempt,
                "resolver_integration_head": integration_head,
                "resolver_conflict_task_id": task_id,
                "resolver_write_authority": True,
                "conflicted_files": conflicted_files,
                "diff_evidence": diff_evidence,
                "diff_truncated": bool(conflict.get("diff_truncated", False)),
                "parent_intent_summaries": intent_summaries,
                "_resolver_child": True,
                "_resolver_source_branch": spec["branch"],
                "_resolver_prepared": False,
                "_resolver_join_invariant_failed": False,
                "_retain_worktree": False,
            }
        )
        # A resolver is not a provider-scheduling escape hatch: it inherits
        # the parent's authorization and any already-assigned provider.
        resolver_spec["resolver_max_attempts"] = max_attempts
        return _validate_plan_task(self._session_dir, resolver_spec)

    async def _prepare_resolver_worktree(self, spec: dict[str, Any]) -> None:
        """Seed a fresh resolver branch with the unresolved two-parent merge."""
        if spec.get("_resolver_prepared"):
            return
        repo = Path(spec["repo"])
        worktree = Path(spec["worktree_path"])
        source_branch = spec.get("_resolver_source_branch")
        if not isinstance(source_branch, str) or not source_branch:
            raise ValueError("resolver source branch is missing")
        async with self._merge_lock:
            integration_head = await self._git_stdout(
                repo, "rev-parse", "refs/heads/main", check=False
            )
            if not integration_head:
                raise RuntimeError("no refs/heads/main to seed resolver staging")
            spec["base_commit"] = integration_head
            await self._salvage_worktree(
                spec,
                generation=read_generation(worktree) or 1,
            )
            reset = await self._git(worktree, "reset", "--hard", integration_head, check=False)
            if reset.returncode != 0:
                raise RuntimeError(
                    f"resolver worktree reset failed: {(reset.stderr + reset.stdout).strip()[:512]}"
                )
            clean = await self._git(worktree, "clean", "-fd", "-e", ".cambium/", check=False)
            if clean.returncode != 0:
                raise RuntimeError(
                    f"resolver worktree clean failed: {(clean.stderr + clean.stdout).strip()[:512]}"
                )
            merge = await self._git(
                worktree,
                "merge",
                "--no-commit",
                "--no-ff",
                "--no-edit",
                source_branch,
                check=False,
            )
            status = await self._git(
                worktree,
                "status",
                "--porcelain=v1",
                "--untracked-files=no",
                "-z",
                check=False,
            )
            if status.returncode != 0:
                raise RuntimeError("cannot inspect resolver staging worktree")
            conflicted = [
                record[3:]
                for record in status.stdout.split("\0")
                if len(record) >= 4 and record[:2] in _RESOLVER_UNMERGED_PAIRS and record[3:]
            ]
            if merge.returncode != 0 and not conflicted:
                raise RuntimeError(
                    f"resolver seed merge failed: {(merge.stderr + merge.stdout).strip()[:512]}"
                )
            diff = await self._git(
                worktree, "diff", "--no-ext-diff", "--no-color", "--binary", "--", check=False
            )
            if conflicted:
                bounded_conflicted = [
                    _cap_utf8(path, MAX_ENVELOPE_FIELD_CHARS) for path in conflicted
                ]
                spec["conflicted_files"] = list(
                    dict.fromkeys([*spec.get("conflicted_files", ()), *bounded_conflicted])
                )[:MAX_ENVELOPE_ITEMS]
            if diff.returncode == 0 and diff.stdout:
                spec["diff_evidence"] = _cap_utf8(diff.stdout, MAX_ENVELOPE_FIELD_CHARS)
            spec["resolver_integration_head"] = integration_head
            spec["_resolver_prepared"] = True
        await self.emit(
            "resolver_staging_prepared",
            task_id=spec["task_id"],
            parent_task_id=spec.get("parent_task_id"),
            source_branch=source_branch,
            integration_head=spec.get("resolver_integration_head"),
            conflicted_files=spec.get("conflicted_files", ()),
            write_authority=True,
        )

    async def _cleanup_resolver_worktree(self, spec: dict[str, Any]) -> None:
        """Remove a resolver's private worktree and branch after its attempt."""
        try:
            await self._prune_worktree(spec, force=True)
        except (OSError, RuntimeError, TypeError, ValueError, subprocess.SubprocessError) as exc:
            await self.emit(
                "resolver_cleanup_failed",
                task_id=spec["task_id"],
                reason=exc.__class__.__name__,
            )
        finally:
            self._cleanup_attempted.add(spec["task_id"])

    async def _resolve_merge_conflict(
        self,
        spec: dict[str, Any],
        handle: WorkerHandle,
        conflict: dict[str, Any],
        envelope: Mapping[str, Any] | None,
        sanitized_envelope: dict[str, Any] | None,
    ) -> str | None:
        """Run bounded resolver children and publish only after the join check."""
        if not self._resolver_enabled_for(spec):
            return None
        max_attempts = self._resolver_attempt_limit(spec)
        parent_task_id = spec.get("parent_task_id")
        parent_spec = (
            self._session_spec(parent_task_id) if isinstance(parent_task_id, str) else None
        )
        integration_head = conflict.get("integration_head")
        if not isinstance(integration_head, str) or not integration_head:
            integration_head = await self._git_stdout(
                Path(spec["repo"]), "rev-parse", "refs/heads/main", check=False
            )
        if not integration_head:
            self._resolver_failures[spec["task_id"]] = "resolver_missing_integration_head"
            return None
        # A conflict itself establishes the head against which the suspended
        # parent must be joined.  Keep a pre-existing accepted head if another
        # child won the merge race before this resolver was admitted.
        if isinstance(parent_task_id, str):
            self._accepted_integration_heads.setdefault(parent_task_id, integration_head)
        if max_attempts == 0:
            self._resolver_failures[spec["task_id"]] = "resolver_attempts_exhausted"
            await self.emit(
                "resolver_failed",
                task_id=spec["task_id"],
                parent_task_id=parent_task_id,
                status="attempts_exhausted",
                reason="resolver_attempts_exhausted",
                conflicted_files=conflict.get("conflicted_files", ()),
                diff_evidence=conflict.get("diff_evidence", ""),
                attempts=0,
                max_attempts=max_attempts,
            )
            return None

        last_status = "failed"
        last_reason = "resolver_failed"
        for attempt in range(1, max_attempts + 1):
            resolver_spec = self._build_resolver_spec(
                spec,
                conflict,
                envelope,
                attempt=attempt,
                max_attempts=max_attempts,
            )
            resolver_task_id = resolver_spec["task_id"]
            self._session_tasks.append(
                {
                    "task_id": resolver_task_id,
                    "kind": "resolver",
                    "depends_on": [spec["task_id"]],
                    "spec": resolver_spec,
                }
            )
            try:
                await self.emit(
                    "resolver_child_admitted",
                    task_id=spec["task_id"],
                    parent_task_id=parent_task_id,
                    resolver_task_id=resolver_task_id,
                    attempt=attempt,
                    max_attempts=max_attempts,
                    conflicted_files=resolver_spec["conflicted_files"],
                    diff_evidence=resolver_spec["diff_evidence"],
                    diff_truncated=resolver_spec["diff_truncated"],
                    parent_intent_summaries=resolver_spec["parent_intent_summaries"],
                    source_branch=resolver_spec["_resolver_source_branch"],
                    staging_branch=resolver_spec["branch"],
                    write_authority=True,
                )
                coroutine = self.supervise_task(resolver_spec)
                if self._task_group is None:
                    await coroutine
                else:
                    child_task = self._task_group.create_task(coroutine)
                    await child_task
            except asyncio.CancelledError:
                await self._cleanup_resolver_worktree(resolver_spec)
                raise

            resolver_result = self._results.get(resolver_task_id)
            resolver_envelope = self._task_envelopes.get(resolver_task_id)
            explicit_unresolvable = bool(
                resolver_envelope is not None
                and (
                    resolver_envelope.get("status") == "unresolvable"
                    or str(resolver_envelope.get("failure_reason", "")).lower()
                    in {"unresolvable", "resolver_unresolvable", "unresolvable_verdict"}
                )
            )
            if resolver_result is not None and resolver_result.status == "succeeded":
                if parent_spec is not None and not await self._assert_parent_join_invariant(
                    parent_spec,
                    [spec["task_id"], resolver_task_id],
                    handle.generation,
                    consume=False,
                ):
                    last_status = "join_invariant_failed"
                    last_reason = "join_invariant_failed"
                    self._results[resolver_task_id] = replace(
                        resolver_result,
                        status="failed",
                        exit_code=1,
                        reason=last_reason,
                    )
                    await self.emit(
                        "resolver_failed",
                        task_id=spec["task_id"],
                        parent_task_id=parent_task_id,
                        resolver_task_id=resolver_task_id,
                        attempt=attempt,
                        max_attempts=max_attempts,
                        status=last_status,
                        reason=last_reason,
                        conflicted_files=resolver_spec["conflicted_files"],
                        diff_evidence=resolver_spec["diff_evidence"],
                    )
                    await self._cleanup_resolver_worktree(resolver_spec)
                    self._resolver_failures[spec["task_id"]] = last_reason
                    return None
                resolver_handle = self._handles.get(resolver_task_id)
                if resolver_handle is not None:
                    merged = await self._merge_task(resolver_spec, resolver_handle)
                else:
                    merged = None
                if merged is not None:
                    if parent_spec is not None and not await self._assert_parent_join_invariant(
                        parent_spec,
                        [spec["task_id"], resolver_task_id],
                        handle.generation,
                    ):
                        last_status = "join_invariant_failed"
                        last_reason = "join_invariant_failed"
                        self._results[resolver_task_id] = replace(
                            resolver_result,
                            status="failed",
                            exit_code=1,
                            reason=last_reason,
                        )
                        await self.emit(
                            "resolver_failed",
                            task_id=spec["task_id"],
                            parent_task_id=parent_task_id,
                            resolver_task_id=resolver_task_id,
                            attempt=attempt,
                            max_attempts=max_attempts,
                            status=last_status,
                            reason=last_reason,
                            merge_sha=merged,
                        )
                        await self._cleanup_resolver_worktree(resolver_spec)
                        self._resolver_failures[spec["task_id"]] = last_reason
                        return None
                    self._results[resolver_task_id] = replace(resolver_result, merge_sha=merged)
                    await self.emit(
                        "resolver_succeeded",
                        task_id=spec["task_id"],
                        parent_task_id=parent_task_id,
                        resolver_task_id=resolver_task_id,
                        attempt=attempt,
                        max_attempts=max_attempts,
                        status="succeeded",
                        merge_sha=merged,
                    )
                    if sanitized_envelope is not None:
                        self._last_envelope = sanitized_envelope
                        self._task_envelopes[spec["task_id"]] = sanitized_envelope
                    await self._cleanup_resolver_worktree(resolver_spec)
                    return merged
                if resolver_spec.get("_resolver_join_invariant_failed"):
                    last_status = "join_invariant_failed"
                    last_reason = "join_invariant_failed"
                    self._results[resolver_task_id] = replace(
                        resolver_result,
                        status="failed",
                        exit_code=1,
                        reason=last_reason,
                    )
                    await self.emit(
                        "resolver_failed",
                        task_id=spec["task_id"],
                        parent_task_id=parent_task_id,
                        resolver_task_id=resolver_task_id,
                        attempt=attempt,
                        max_attempts=max_attempts,
                        status=last_status,
                        reason=last_reason,
                    )
                    await self._cleanup_resolver_worktree(resolver_spec)
                    self._resolver_failures[spec["task_id"]] = last_reason
                    return None
                last_reason = "resolver_merge_failed"
            else:
                last_reason = (
                    _envelope_text(resolver_envelope, "failure_reason")
                    if resolver_envelope is not None
                    else None
                ) or (
                    resolver_result.reason
                    if resolver_result is not None and resolver_result.reason
                    else "resolver_failed"
                )
            last_status = "unresolvable" if explicit_unresolvable else "failed"
            await self.emit(
                "resolver_failed",
                task_id=spec["task_id"],
                parent_task_id=parent_task_id,
                resolver_task_id=resolver_task_id,
                attempt=attempt,
                max_attempts=max_attempts,
                status=last_status,
                reason=last_reason,
                summary=_envelope_text(resolver_envelope, "summary"),
                conflicted_files=resolver_spec["conflicted_files"],
                diff_evidence=resolver_spec["diff_evidence"],
            )
            if resolver_result is None or resolver_result.status == "succeeded":
                self._results[resolver_task_id] = TaskResult(
                    task_id=resolver_task_id,
                    status="failed",
                    exit_code=1,
                    reason=last_reason,
                )
            await self._cleanup_resolver_worktree(resolver_spec)
            if explicit_unresolvable:
                self._resolver_failures[spec["task_id"]] = "resolver_unresolvable"
            elif attempt == max_attempts:
                self._resolver_failures[spec["task_id"]] = "resolver_attempts_exhausted"
        return None

    async def _integrate_child_into_suspended_parent(
        self, spec: dict[str, Any], handle: WorkerHandle
    ) -> str | None:
        """Integrate a child into its suspended parent without publishing main.

        ``prepare_staging`` rebases the child onto the parent's current private
        base.  A critical prepared event is the write-ahead record; then one
        fast-forward updates the clean parent branch and worktree; finally a
        critical committed event makes the new private base visible to resume.
        The staging ref is retained when the second barrier is not reached.
        """
        task_id = spec["task_id"]
        parent_task_id = spec.get("parent_task_id")
        if not isinstance(parent_task_id, str):
            return None
        parent_spec = self._session_spec(parent_task_id)
        if parent_spec is None:
            return None
        repo = Path(spec["repo"])
        if repo.resolve() != Path(parent_spec["repo"]).resolve():
            return None
        branch = spec["branch"]
        parent_worktree = Path(parent_spec["worktree_path"])
        await self.emit(
            "merge_started",
            task_id=task_id,
            branch=branch,
            generation=handle.generation,
            target="suspended_parent",
            parent_task_id=parent_task_id,
        )
        task_key = hashlib.sha256(task_id.encode()).hexdigest()[:16]
        throwaway = self._session_dir / ".cambium" / "merge-wt" / f"task-{task_key}"
        deferred: list[tuple[dict[str, Any], bool]] = []
        seq = self._make_sequencer(task_id, deferred)
        prepared_persisted = False
        integrated_persisted = False
        cleanup_failed = False
        merge_failed = False
        staging_tip: str | None = None
        parent_head: str | None = None
        try:
            async with self._merge_lock:
                integrity = await self._worker_success_integrity(parent_spec, parent_worktree)
                if integrity is not None:
                    raise RuntimeError(f"parent integration precondition failed: {integrity}")
                parent_head = await self._git_stdout(
                    parent_worktree,
                    "rev-parse",
                    "--verify",
                    "HEAD^{commit}",
                    check=False,
                )
                if parent_head is None or parent_head != parent_spec.get("base_commit"):
                    raise RuntimeError("parent private base changed before child integration")
                staging_tip = await asyncio.to_thread(
                    seq.prepare_staging, repo, throwaway, branch, parent_head
                )
                await self._flush_sequencer_events(seq, deferred_observers=deferred)
                if hasattr(seq, "ensure_staging_clean"):
                    await asyncio.to_thread(seq.ensure_staging_clean, repo)
                    await self._flush_sequencer_events(seq, deferred_observers=deferred)
                await self.emit(
                    "child_integration_prepared",
                    task_id=task_id,
                    parent_task_id=parent_task_id,
                    old=parent_head,
                    new=staging_tip,
                    repo=str(repo),
                    parent_branch=parent_spec["branch"],
                    child_branch=branch,
                    staging_ref=seq.staging_ref,
                    staging_branch=seq.staging_branch,
                    staging_worktree=str(throwaway),
                    generation=handle.generation,
                    _deferred_observers=deferred,
                )
                prepared_persisted = True
                advanced = await self._git(
                    parent_worktree,
                    "merge",
                    "--ff-only",
                    "--no-edit",
                    staging_tip,
                    check=False,
                )
                if advanced.returncode != 0:
                    raise RuntimeError("parent private integration fast-forward failed")
                accepted = await self._git_stdout(
                    parent_worktree,
                    "rev-parse",
                    "--verify",
                    "HEAD^{commit}",
                    check=False,
                )
                if accepted != staging_tip:
                    raise RuntimeError("parent private integration head mismatch")
                integrity = await self._worker_success_integrity(parent_spec, parent_worktree)
                if integrity is not None:
                    raise RuntimeError(f"parent integration postcondition failed: {integrity}")
                await self.emit(
                    "child_integrated",
                    task_id=task_id,
                    parent_task_id=parent_task_id,
                    old=parent_head,
                    new=staging_tip,
                    repo=str(repo),
                    parent_branch=parent_spec["branch"],
                    child_branch=branch,
                    generation=handle.generation,
                    recovered=False,
                    _deferred_observers=deferred,
                )
                integrated_persisted = True
                parent_spec["base_commit"] = staging_tip
                parent_spec["_base_is_published"] = False
                self._accepted_integration_heads[parent_task_id] = staging_tip
        except (OSError, RuntimeError, ValueError, subprocess.SubprocessError) as exc:
            merge_failed = True
            error_type = exc.__class__.__name__
            if isinstance(exc, MergeConflictError):
                summary = str(exc)[:512]
                diff_evidence = exc.diff_evidence
                await self.emit(
                    "merge_failed",
                    task_id=task_id,
                    merge_error=error_type,
                    message=summary,
                    status="merge_conflict",
                    conflicted_files=exc.conflicted_files,
                    summary=summary,
                    diff_evidence=diff_evidence,
                    evidence=diff_evidence,
                    diff=diff_evidence,
                    unified_diff=diff_evidence,
                    diff_truncated=exc.diff_truncated,
                    integration_head=exc.integration_head or parent_head,
                    generation=handle.generation,
                )
            else:
                await self.emit(
                    "merge_failed",
                    task_id=task_id,
                    merge_error=error_type,
                    message=str(exc)[:512],
                    generation=handle.generation,
                    internal=True,
                )
        finally:
            try:
                if hasattr(seq, "cleanup_staging") and not (
                    prepared_persisted and not integrated_persisted
                ):
                    await asyncio.to_thread(seq.cleanup_staging, repo)
            except (OSError, RuntimeError, subprocess.SubprocessError) as exc:
                cleanup_failed = True
                emitted = await self._flush_sequencer_events(seq, deferred_observers=deferred)
                if integrated_persisted and "merge_staging_cleanup_failed" not in emitted:
                    await self.emit(
                        "merge_staging_cleanup_failed",
                        task_id=task_id,
                        staging_sha=staging_tip,
                        reason=exc.__class__.__name__,
                    )
            else:
                await self._flush_sequencer_events(seq, deferred_observers=deferred)
        try:
            await self._notify_deferred_observers(deferred)
        except (
            OSError,
            RuntimeError,
            TypeError,
            ValueError,
            subprocess.SubprocessError,
        ) as exc:
            await self.emit(
                "merge_failed",
                task_id=task_id,
                merge_error=exc.__class__.__name__,
                message=str(exc)[:512],
                generation=handle.generation,
                internal=True,
            )
            if not integrated_persisted:
                return None
        if merge_failed or (cleanup_failed and not integrated_persisted):
            return None
        return staging_tip

    async def _merge_task(self, spec: dict[str, Any], handle: WorkerHandle) -> str | None:
        """Stage and atomically publish the worker branch onto refs/heads/main.

        On a non-fast-forward refusal a backward-compatible ``merge_failed``
        event is appended.  A conflict uses that same event kind but carries a
        structured ``status=merge_conflict`` envelope.  The envelope is kept
        in ``_merge_conflicts`` so the task supervisor can optionally admit a
        dedicated resolver child without changing this method's ``None``
        return contract.
        """
        if spec.get("_private_parent_integration") is True:
            return await self._integrate_child_into_suspended_parent(spec, handle)
        task_id = spec["task_id"]
        repo = Path(spec["repo"])
        branch = spec["branch"]
        await self.emit(
            "merge_started", task_id=task_id, branch=branch, generation=handle.generation
        )
        task_key = hashlib.sha256(task_id.encode()).hexdigest()[:16]
        throwaway = self._session_dir / ".cambium" / "merge-wt" / f"task-{task_key}"
        deferred: list[tuple[dict[str, Any], bool]] = []
        seq = self._make_sequencer(task_id, deferred)
        ref_published = False
        committed_persisted = False
        cleanup_failed = False
        merge_failed = False
        staging_tip: str | None = None
        current_main: str | None = None
        self._merge_conflicts.pop(task_id, None)
        try:
            async with self._merge_lock:  # Unio single-writer: serialized merges
                current_main = await self._git_stdout(
                    repo, "rev-parse", "refs/heads/main", check=False
                )
                if not current_main:
                    raise RuntimeError("no refs/heads/main to publish onto")
                if spec.get("_resolver_child"):
                    parent_task_id = spec.get("parent_task_id")
                    parent_spec = (
                        self._session_spec(parent_task_id)
                        if isinstance(parent_task_id, str)
                        else None
                    )
                    if parent_spec is not None and not await self._assert_parent_join_invariant(
                        parent_spec,
                        [
                            str(spec.get("resolver_conflict_task_id", task_id)),
                            task_id,
                        ],
                        handle.generation,
                        consume=False,
                    ):
                        spec["_resolver_join_invariant_failed"] = True
                        raise ResolverJoinInvariantError(
                            "parent worktree was not at the accepted head before resolver publish"
                        )
                    # A resolver commits the already-resolved two-parent
                    # merge in its fresh staging worktree. Rebasing that merge
                    # commit would replay the source parent and recreate the
                    # conflict, so publish its verified tip directly through
                    # the same atomic fast-forward primitive.
                    staging_tip = await self._git_stdout(
                        repo,
                        "rev-parse",
                        "--verify",
                        f"refs/heads/{branch}^{{commit}}",
                        check=False,
                    )
                    if not staging_tip:
                        raise RuntimeError("resolver branch has no commit to publish")
                else:
                    staging_tip = await asyncio.to_thread(
                        seq.prepare_staging, repo, throwaway, branch, current_main
                    )
                # publish_merge is ref-only by contract. If this repository's
                # primary worktree has ``main`` checked out, advancing the ref
                # leaves its files and index at the old commit; git status can
                # therefore report a staged delta even though this operation
                # did not mutate the main working tree. Do not reset or
                # checkout here: that would violate ref-only publication and
                # could destroy caller-owned edits.
                await self._flush_sequencer_events(seq, deferred_observers=deferred)
                if hasattr(seq, "ensure_staging_clean") and not spec.get("_resolver_child"):
                    await asyncio.to_thread(seq.ensure_staging_clean, repo)
                    await self._flush_sequencer_events(seq, deferred_observers=deferred)
                await asyncio.to_thread(seq.publish_merge, repo, staging_tip, current_main)
                ref_published = True
                await self.emit(
                    "merge_committed",
                    task_id=task_id,
                    old=current_main,
                    new=staging_tip,
                    repo=str(repo),
                    branch=branch,
                    generation=handle.generation,
                    _deferred_observers=deferred,
                )
                committed_persisted = True
                parent_task_id = spec.get("parent_task_id")
                if isinstance(parent_task_id, str) and staging_tip is not None:
                    self._accepted_integration_heads[parent_task_id] = staging_tip
                    await self._advance_parent_worktree(spec, staging_tip, current_main)
        except (OSError, RuntimeError, ValueError, subprocess.SubprocessError) as exc:
            merge_failed = True
            error_type = exc.__class__.__name__
            if isinstance(exc, ResolverJoinInvariantError):
                await self.emit(
                    "merge_failed",
                    task_id=task_id,
                    merge_error=error_type,
                    status="join_invariant_failed",
                    reason="parent_worktree_head_mismatch",
                    message=str(exc)[:512],
                    generation=handle.generation,
                )
            elif isinstance(exc, MergeConflictError):
                summary = str(exc)[:512]
                diff_evidence = exc.diff_evidence
                conflict_payload: dict[str, Any] = {
                    # Keep the old event kind and fields so renderers and
                    # operators consuming merge_failed remain compatible.
                    "merge_error": error_type,
                    "message": summary,
                    "status": "merge_conflict",
                    "conflicted_files": exc.conflicted_files,
                    "summary": summary,
                    "diff_evidence": diff_evidence,
                    "evidence": diff_evidence,
                    "diff": diff_evidence,
                    "unified_diff": diff_evidence,
                    "diff_truncated": exc.diff_truncated,
                    "integration_head": exc.integration_head or current_main,
                    "generation": handle.generation,
                }
                self._merge_conflicts[task_id] = dict(conflict_payload)
                await self.emit(
                    "merge_failed",
                    task_id=task_id,
                    **conflict_payload,
                )
            elif error_type == "NonFastForwardError":
                await self.emit(
                    "merge_failed",
                    task_id=task_id,
                    merge_error=error_type,
                    message=str(exc)[:512],
                    generation=handle.generation,
                )
            else:
                await self.emit(
                    "merge_failed",
                    task_id=task_id,
                    merge_error=error_type,
                    message=str(exc)[:512],
                    generation=handle.generation,
                    internal=True,
                )
        finally:
            try:
                if hasattr(seq, "cleanup_staging") and not (
                    ref_published and not committed_persisted
                ):
                    await asyncio.to_thread(seq.cleanup_staging, repo)
            except (OSError, RuntimeError, subprocess.SubprocessError) as exc:
                cleanup_failed = True
                emitted = await self._flush_sequencer_events(seq, deferred_observers=deferred)
                if committed_persisted and "merge_staging_cleanup_failed" not in emitted:
                    await self.emit(
                        "merge_staging_cleanup_failed",
                        task_id=task_id,
                        staging_sha=staging_tip,
                        reason=exc.__class__.__name__,
                    )
            else:
                await self._flush_sequencer_events(seq, deferred_observers=deferred)
        try:
            await self._notify_deferred_observers(deferred)
        except (
            OSError,
            RuntimeError,
            TypeError,
            ValueError,
            subprocess.SubprocessError,
        ) as exc:
            await self.emit(
                "merge_failed",
                task_id=task_id,
                merge_error=exc.__class__.__name__,
                message=str(exc)[:512],
                generation=handle.generation,
                internal=True,
            )
            return None
        if merge_failed or cleanup_failed:
            return None
        return staging_tip


def _reject_reused_session(session_dir: str | Path) -> None:
    """Reject a session leaf that already contains run artifacts.

    The explicit one-shot session contract (``oneshot.run_oneshot``) rejects a
    reused leaf; the check is re-verified here while the caller holds the
    session admission lock so a leaf that became used while the caller waited
    (TOCTOU across provider resolution) is rejected before any write.
    """
    path = Path(session_dir).resolve()
    artifacts = (
        path / "plan.json",
        path / ".cambium" / "events.db",
        path / ".cambium" / "result.json",
    )
    if any(artifact.exists() for artifact in artifacts):
        raise ValueError(f"one-shot session directory has already been used: {path}")


def _write_plan(session_dir: Path, plan: dict[str, Any]) -> Path:
    """Persist the accepted plan once as ``<session_dir>/plan.json``.

    Mirrors the ``cambium.results.write_result`` JSON conventions:
    ``mkstemp`` in the target directory, compact ``ensure_ascii=False`` /
    ``allow_nan=False`` JSON with a trailing newline and fsync. The caller
    holds the session lock. A resume accepts the byte-identical manifest but
    never replaces it.
    """
    target = Path(session_dir) / "plan.json"
    content = (
        json.dumps(
            plan,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
        )
        + "\n"
    )
    if target.exists():
        existing_bytes = target.read_bytes()
        if existing_bytes == content.encode("utf-8"):
            return target
        try:
            existing_plan = json.loads(existing_bytes)
            existing_specs = [
                _validate_plan_task(session_dir, task) for task in _plan_tasks(existing_plan)
            ]
        except (TypeError, ValueError):
            existing_specs = []
        if plan == {"tasks": existing_specs}:
            return target
        raise ValueError("session plan.json does not match the submitted plan")
    fd, temporary_name = tempfile.mkstemp(
        prefix=f".{target.name}.", suffix=".tmp", dir=Path(session_dir)
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temporary, 0o600)
        os.link(temporary, target)
        temporary.unlink()
        directory_fd = os.open(Path(session_dir), os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass
    return target


def _plan_tasks(plan: dict[str, Any] | list[dict[str, Any]]) -> list[dict[str, Any]]:
    if isinstance(plan, dict):
        tasks = plan.get("tasks")
        if not isinstance(tasks, list):
            raise ValueError("plan dict must contain a 'tasks' list")
        return list(tasks)
    if isinstance(plan, list | tuple):
        return list(plan)
    raise ValueError("plan must be a dict with 'tasks' or a list of task specs")


_ROUTING_REQUIREMENT_KEYS = frozenset(
    {
        "quality",
        "min_context_window",
        "needs_native_tools",
        "needs_python_tool",
        "allow_paid",
        "allow_free",
    }
)


def _task_requirements(spec: Mapping[str, Any]) -> dict[str, Any]:
    """Normalize task and fanout requirement declarations into one contract."""
    merged: dict[str, Any] = {}
    fanout_config = spec.get("fanout_config")
    if isinstance(fanout_config, dict):
        section = fanout_config
        for nested_key in ("diffundo", "router"):
            nested = fanout_config.get(nested_key)
            if isinstance(nested, dict):
                section = nested
                break
        nested_requirements = fanout_config.get("requirements")
        if nested_requirements is None:
            nested_requirements = section.get("requirements")
        if nested_requirements is not None:
            merged.update(validate_requirements(nested_requirements))
        for key in _ROUTING_REQUIREMENT_KEYS:
            value = fanout_config.get(key)
            if value is None:
                value = section.get(key)
            if value is not None:
                merged[key] = value
    task_requirements = spec.get("requirements")
    if task_requirements is not None:
        merged.update(validate_requirements(task_requirements))
    return validate_requirements(merged)


def _validate_plan_task(session_dir: Path, task: dict[str, Any]) -> dict[str, Any]:
    """Path safety and required-field checks for one plan task."""
    session_root = Path(session_dir).resolve()
    if not isinstance(task, dict):
        raise ValueError("plan task must be an object")
    spec = dict(task)
    task_id = spec.get("task_id")
    if not isinstance(task_id, str) or not task_id:
        raise ValueError("plan task requires 'task_id'")
    task_text = spec.get("task")
    if not isinstance(task_text, str) or not task_text.strip():
        raise ValueError(f"task {task_id} requires a non-empty 'task'")
    if "repo" not in spec:
        raise ValueError(f"task {task_id} requires 'repo'")
    if "worktree_path" not in spec:
        raise ValueError(f"task {task_id} requires 'worktree_path'")
    if "branch" not in spec:
        raise ValueError(f"task {task_id} requires 'branch'")
    if not isinstance(spec["branch"], str) or not spec["branch"]:
        raise ValueError(f"task {task_id} branch must be a non-empty name")
    worktree = Path(spec["worktree_path"]).resolve()
    if not worktree.is_relative_to(session_root):
        raise ValueError(f"worktree_path {worktree} is outside the session dir {session_root}")
    spec["repo"] = str(Path(spec["repo"]).resolve())
    spec["worktree_path"] = str(worktree)
    if Path(spec["repo"]).resolve() == worktree:
        raise ValueError(f"task {task_id}: worktree_path must not be the repo itself ({worktree})")
    provider_env_keys = spec.get("provider_env_keys", ())
    if isinstance(provider_env_keys, str | bytes):
        raise ValueError(f"task {task_id} provider_env_keys must be a list of names")
    if not isinstance(provider_env_keys, list | tuple):
        raise ValueError(f"task {task_id} provider_env_keys must be a list of names")
    spec["provider_env_keys"] = list(provider_env_keys)
    authorized_providers = spec.get("authorized_providers", ())
    if (
        isinstance(authorized_providers, str | bytes)
        or not isinstance(authorized_providers, list | tuple)
        or not all(isinstance(name, str) and name for name in authorized_providers)
    ):
        raise ValueError(f"task {task_id} authorized_providers must be a list of names")
    spec["authorized_providers"] = list(authorized_providers)
    spec["authorized_providers_explicit"] = bool(
        spec.get("authorized_providers_explicit", "authorized_providers" in task)
    )
    model_candidates = spec.get("model_candidates")
    if model_candidates is not None:
        if (
            not isinstance(model_candidates, list | tuple)
            or not model_candidates
            or not all(isinstance(model, str) and bool(model.strip()) for model in model_candidates)
        ):
            raise ValueError(
                f"task {task_id} model_candidates must be a non-empty list of model ids"
            )
        spec["model_candidates"] = list(model_candidates)
    try:
        requirements = _task_requirements(spec)
    except ValueError as exc:
        raise ValueError(f"task {task_id}: {exc}") from exc
    if requirements:
        spec["requirements"] = requirements
    else:
        spec.pop("requirements", None)
    spec.setdefault("base_commit", None)
    # Internal ownership token.  A provider identity alone does not prove
    # that this task booked a lane; releases use this flag to stay balanced
    # for explicit and cache-pinned tasks.
    spec["_lane_reserved"] = False
    spec.setdefault("write_marker", True)
    if not isinstance(spec["write_marker"], bool):
        raise ValueError(f"task {task_id} write_marker must be a boolean")
    if "resolver_child_enabled" in spec and type(spec["resolver_child_enabled"]) is not bool:
        raise ValueError(f"task {task_id} resolver_child_enabled must be a boolean")
    for key in ("resolver_max_attempts", "resolver_attempts"):
        if key in spec and (type(spec[key]) is not int or spec[key] < 0):
            raise ValueError(f"task {task_id} {key} must be a non-negative int")
    return spec


def _reject_duplicate_task_ownership(specs: Sequence[Mapping[str, Any]]) -> None:
    """Reject worktree and branch aliases before any task can reset a tree."""
    worktrees: dict[Path, str] = {}
    branches: dict[tuple[Path, str], str] = {}
    for spec in specs:
        task_id = str(spec.get("task_id", "<unknown>"))
        worktree = Path(spec["worktree_path"]).resolve()
        previous = worktrees.get(worktree)
        if previous is not None:
            raise ValueError(
                f"duplicate worktree_path {str(worktree)!r} for tasks {previous!r} and {task_id!r}"
            )
        worktrees[worktree] = task_id
        repo = Path(spec["repo"]).resolve()
        branch = spec["branch"]
        branch_key = (repo, branch)
        previous = branches.get(branch_key)
        if previous is not None:
            raise ValueError(
                f"duplicate branch {branch!r} in repository {str(repo)!r} for "
                f"tasks {previous!r} and {task_id!r}"
            )
        branches[branch_key] = task_id


def _validate_task_repositories(specs: Sequence[Mapping[str, Any]]) -> None:
    """Verify plan repositories without changing their files or Git metadata."""
    for spec in specs:
        task_id = spec["task_id"]
        repo = Path(spec["repo"])
        if not repo.is_dir():
            raise ValueError(f"task {task_id} repo is not a directory")
        probe = subprocess.run(
            ["git", "-C", str(repo), "rev-parse", "--verify", "HEAD^{commit}"],
            capture_output=True,
            text=True,
            env=_strip_sensitive_env(scrub_environment(), worktree=repo),
        )
        if probe.returncode != 0:
            raise ValueError(f"task {task_id} repo must contain a git commit")


def _ensure_lanes(lanes: dict[str, LaneState], providers: Sequence[Any]) -> None:
    """Create a LaneState for every configured provider not yet tracked.

    ``rpm_allowance`` comes from the provider's optional ``rpm`` field
    (default 60.0); first config wins when multiple specs configure the same
    provider name.
    """
    for provider in providers:
        if provider.name in lanes:
            continue
        rpm = getattr(provider, "rpm", 60)
        lanes[provider.name] = LaneState(rpm_allowance=float(rpm or 60))


def _resolve_model_candidates(
    spec: dict[str, Any],
    debt: Mapping[str, Any],
    lanes: dict[str, LaneState],
    *,
    provider_environment: Mapping[str, str] | None = None,
    oauth_store: OAuthStore | None = None,
) -> bool:
    """Resolve a task's (provider, model) when it declares ``model_candidates``
    and its fanout_config carries no pinned model.

    The pure pick lives in :func:`cambium.routing.resolve_assignment`; this
    function loads the provider config, restricts the pool to the task's
    authorized provider identities (carried by name, so OAuth providers are
    never dropped the way env-key filtering dropped them), intersects that set
    with credential readiness, and applies the returned assignment to ``spec``
    at the runtime edge (mutates ``fanout_config`` and records
    ``assigned_provider``). Returns True when an assignment was written; pinned
    tasks and tasks without a fanout_config are left untouched and return False.
    """
    fanout_config = spec.get("fanout_config")
    candidates = spec.get("model_candidates")
    if (
        not isinstance(fanout_config, dict)
        or bool(fanout_config.get("model"))
        or not isinstance(candidates, list)
        or not candidates
    ):
        return False
    providers = load_providers(_provider_config_path(os.environ, spec))
    authorized_raw = spec.get("authorized_providers")
    authorized_explicit = spec.get("authorized_providers_explicit", "authorized_providers" in spec)
    authorized = (
        frozenset(name for name in authorized_raw if isinstance(name, str) and name)
        if isinstance(authorized_raw, list | tuple) and (authorized_explicit or authorized_raw)
        else None
    )
    if authorized is None:
        # Legacy auto-mode plans carried only env-key names. Keep them working
        # by deriving identity from the env keys, but prefer the explicit
        # authorized carrier when present (OAuth providers have no env name).
        authorized_provider_keys = _provider_env_keys(spec)
        if authorized_provider_keys:
            providers = [
                provider
                for provider in providers
                if provider.api_key_env in authorized_provider_keys
            ]
    else:
        feasible: list[Any] = []
        infeasible: list[tuple[str, str]] = []
        for provider in providers:
            if provider.name not in authorized:
                continue
            if not getattr(provider, "enabled", True):
                continue
            if not _provider_credential_ready_at_admission(
                provider,
                provider_environment,
                oauth_store,
            ):
                infeasible.append((provider.name, "credential unavailable"))
                continue
            feasible.append(provider)
        if infeasible:
            recorded = spec.setdefault("_provider_infeasible", [])
            if not isinstance(recorded, list):
                recorded = spec["_provider_infeasible"] = []
            known = {
                item[0]
                for item in recorded
                if isinstance(item, tuple) and len(item) == 2 and isinstance(item[0], str)
            }
            for provider_name, reason in infeasible:
                if provider_name not in known:
                    recorded.append((provider_name, reason))
                    known.add(provider_name)
        if not feasible:
            raise NoCredentialFeasibleProvidersError()
        providers = feasible
        authorized = frozenset(provider.name for provider in feasible)
        spec["authorized_providers"] = [provider.name for provider in feasible]
    # A caller-pinned tier is a hard constraint: only providers in that tier
    # may serve the task, so the assignment can never contradict it.
    raw_pinned_tier = fanout_config.get("tier")
    pinned_tier = raw_pinned_tier if isinstance(raw_pinned_tier, str) and raw_pinned_tier else None
    _ensure_lanes(lanes, providers)
    requirements = spec.get("requirements")
    spread_from = spec.get("spread_from_provider")
    spread_providers = (
        [provider for provider in providers if provider.name != spread_from]
        if isinstance(spread_from, str) and spread_from
        else providers
    )

    def _resolve(pool: list[Any]) -> Any:
        try:
            return resolve_assignment(
                pool,
                candidates,
                debt,
                lanes,
                requirements=requirements if requirements else None,
                authorized=authorized,
                pinned_tier=pinned_tier,
            )
        except ValueError as exc:
            raise ValueError(
                f"task {spec.get('task_id')}: provider assignment failed: {exc}"
            ) from exc

    # Spread is a strong soft preference: consume another feasible provider
    # lane when one exists, otherwise fall back to the full feasible set so
    # spread can never turn a feasible task into an unnecessary failure.
    if spread_providers is providers:
        assignment = _resolve(providers)
    else:
        assignment = _resolve(spread_providers)
        if assignment is None:
            assignment = _resolve(providers)
    if assignment is None:
        return False
    # The (provider, model, tier) assignment is one atomic unit: the worker
    # routes calls by tier, so the assigned provider's tier must be the call
    # tier or the assignment is filtered out before any request is sent.
    spec["fanout_config"] = {
        **fanout_config,
        "model": assignment.model,
        "tier": assignment.tier,
    }
    spec["assigned_provider"] = assignment.provider
    return True


def _preassign_lanes(
    specs: Sequence[dict[str, Any]],
    debt: Mapping[str, ProviderDebt] | None,
    lanes: dict[str, LaneState],
    *,
    provider_environment: Mapping[str, str] | None = None,
    oauth_store: OAuthStore | None = None,
) -> None:
    """Batch-aware (provider, model) pre-assignment for a plan wave (H1).

    Resolves every un-pinned ``model_candidates`` task in ONE pass, in plan
    order, against a batch debt view seeded from the ledger snapshot plus the
    lanes, so concurrent admissions in the same wave spread across providers
    instead of all picking the same max-min winner (the known C limitation).
    Each pre-assigned task folds +1 request (0 tokens) into the batch view and
    reserves +1 in_flight on its chosen lane; ``supervise_task`` releases the
    reservation when the task completes. Tasks left untouched here (pinned,
    no fanout_config, or already finished before the pass) resolve at
    admission against the live ledger instead.
    """
    batch_debt = {name: replace(entry) for name, entry in (debt or {}).items()}
    for spec in specs:
        try:
            assigned = _resolve_model_candidates(
                spec,
                batch_debt,
                lanes,
                provider_environment=provider_environment,
                oauth_store=oauth_store,
            )
        except LaneCapacityExhausted:
            continue
        except NoCredentialFeasibleProvidersError:
            continue
        if not assigned:
            continue
        provider_name = spec["assigned_provider"]
        lanes[provider_name].in_flight += 1
        spec["_lane_reserved"] = True
        entry = batch_debt.get(provider_name)
        if entry is None:
            entry = batch_debt[provider_name] = ProviderDebt()
        entry.requests += 1


def _release_lane(lanes: dict[str, LaneState], spec: Mapping[str, Any]) -> None:
    """Release a task's lane reservation (H1).

    Every supervised task that holds one (batch pre-assignment or
    admission-time assignment) frees it exactly once on every exit path —
    success, failure, exception, and cancellation. Tasks without an
    ``assigned_provider`` never held a reservation.
    """
    if not spec.get("_lane_reserved", False):
        return
    assigned = spec.get("assigned_provider")
    if not isinstance(assigned, str):
        if isinstance(spec, dict):
            spec["_lane_reserved"] = False
        return
    lane = lanes.get(assigned)
    if lane is not None and lane.in_flight > 0:
        lane.in_flight -= 1
    if isinstance(spec, dict):
        spec["_lane_reserved"] = False


def _declared_child_policy(
    child_spec: Mapping[str, Any],
) -> tuple[str, str] | None:
    """Return the child's declared (context_mode, placement), or None.

    Absent or malformed declarations fall back to automatic compatibility
    resolution; explicit values must already have passed ``parse_child_policy``
    at admission (which rejects ``trunk + spread``).
    """
    mode = child_spec.get("context_mode")
    placement = child_spec.get("placement")
    if mode is None and placement is None:
        return None
    if not isinstance(mode, str) or not isinstance(placement, str):
        return None
    if mode not in {"trunk", "semantic", "fresh"}:
        return None
    if placement not in {"inherit", "spread"}:
        return None
    return mode, placement


def _fork_cache_compatible_supervisor(
    child_spec: dict[str, Any],
    epoch: Mapping[str, Any],
    authorized_providers: frozenset[str],
) -> tuple[bool, str | None]:
    """Check every supervisor-visible cache identity before pinning a child."""
    compatible, reason = _worker_fork_cache_compatible(child_spec, epoch, authorized_providers)
    if not compatible:
        return False, reason
    cache_key = epoch.get("cache_key")
    if not isinstance(cache_key, dict):
        return False, "epoch has no cache_key"
    provider = cache_key.get("provider")
    if not isinstance(provider, str):
        return False, "epoch provider is invalid"

    fanout = child_spec.get("fanout_config")
    fanout = fanout if isinstance(fanout, dict) else {}
    configured_provider = child_spec.get("assigned_provider")
    if configured_provider is None:
        for key in ("provider", "primary_provider"):
            if isinstance(fanout.get(key), str):
                configured_provider = fanout[key]
                break
        if configured_provider is None:
            providers = fanout.get("providers")
            names = (
                [
                    entry.get("name")
                    for entry in providers
                    if isinstance(entry, dict) and isinstance(entry.get("name"), str)
                ]
                if isinstance(providers, list | tuple)
                else []
            )
            if len(names) == 1:
                configured_provider = names[0]
    if isinstance(configured_provider, str) and configured_provider != provider:
        return False, "child provider differs from the epoch provider"

    expected_protocol = cache_key.get("protocol")
    expected_reasoning = cache_key.get("reasoning_effort")
    actual_protocol = fanout.get("protocol")
    actual_reasoning = fanout.get("reasoning_effort")
    if actual_protocol is None:
        actual_protocol = child_spec.get("protocol")
    if actual_reasoning is None:
        actual_reasoning = child_spec.get("reasoning_effort")

    # Provider-backed children normally carry the trusted config path. Read
    # only that path here; marker/fake workers have no provider config and
    # retain the first-pass compatibility behavior when no identity is
    # available to compare.
    config_path = child_spec.get("provider_config_path")
    if isinstance(config_path, str) and config_path:
        try:
            configured = next(
                provider_config
                for provider_config in load_providers(Path(config_path))
                if provider_config.name == provider
            )
        except (OSError, StopIteration, ValueError):
            return False, "provider configuration is unavailable"
        actual_protocol = configured.protocol.value
        actual_reasoning = configured.reasoning_effort
    if (
        isinstance(expected_protocol, str)
        and isinstance(actual_protocol, str)
        and expected_protocol != actual_protocol
    ):
        return False, "provider protocol differs from the epoch"
    if expected_reasoning != actual_reasoning and (
        expected_reasoning is not None or actual_reasoning is not None
    ):
        return False, "reasoning effort differs from the epoch"
    return True, None


def _child_spec(
    session_dir: Path,
    parent_spec: dict[str, Any],
    proposal: dict[str, Any],
    parent_envelope: dict[str, Any],
) -> dict[str, Any]:
    """Build the spawnable child spec: its own spec plus the parent envelope.

    The child context is limited to its own proposed spec fields, its tree
    identity (``task_id``/``kind``/``parent_task_id``), and the parent's
    redacted strict-key envelope. ``_validate_plan_task`` applies the same
    path-safety and required-field checks as a plan task; the child's
    ``parent_envelope`` is carried only into its own run payload and is never
    broadcast to siblings. Raises ``ValueError`` when the proposal spec is
    not a valid task spec.
    """
    raw = proposal.get("spec")
    if not isinstance(raw, dict):
        raise ValueError("child proposal spec must be an object")
    child_spec = copy.deepcopy(raw)
    child_spec.pop("context_fork", None)
    child_spec.pop("summary_trunk_ref", None)
    child_spec.pop("resume", None)
    child_spec["task_id"] = proposal["child_task_id"]
    child_spec["kind"] = proposal["kind"]
    child_spec["parent_task_id"] = parent_spec["task_id"]

    # Children inherit, never exceed, the parent's environment and provider
    # authorization.  Reject a widening request instead of silently trimming
    # it: silent trimming turns a malformed proposal into a different task.
    parent_keys = set(parent_spec.get("provider_env_keys") or ())
    raw_keys = child_spec.get("provider_env_keys")
    if raw_keys is None:
        child_keys = set(parent_keys)
    elif isinstance(raw_keys, list | tuple):
        child_keys = set(raw_keys)
    else:
        raise ValueError("child provider_env_keys must be a list of names")
    if not all(isinstance(key, str) for key in child_keys):
        raise ValueError("child provider_env_keys must contain only names")
    if not child_keys.issubset(parent_keys):
        raise ValueError("child provider_env_keys would widen parent authorization")
    child_spec["provider_env_keys"] = sorted(child_keys & parent_keys)

    parent_authorized = set(parent_spec.get("authorized_providers") or ())
    parent_authorized_explicit = bool(
        parent_spec.get("authorized_providers_explicit", "authorized_providers" in parent_spec)
    )
    raw_authorized = child_spec.get("authorized_providers")
    if raw_authorized is None:
        requested_authorized = set(parent_authorized)
        child_authorized_explicit = parent_authorized_explicit
    elif isinstance(raw_authorized, list | tuple):
        requested_authorized = set(raw_authorized)
        child_authorized_explicit = True
    else:
        raise ValueError("child authorized_providers must be a list of names")
    if not all(isinstance(name, str) and name for name in requested_authorized):
        raise ValueError("child authorized_providers must contain only names")
    if parent_authorized_explicit and not requested_authorized.issubset(parent_authorized):
        raise ValueError("child authorized_providers would widen parent authorization")
    if parent_authorized and not requested_authorized.issubset(parent_authorized):
        raise ValueError("child authorized_providers would widen parent authorization")
    # An empty parent set is unrestricted only for legacy plans.  Preserve an
    # explicit empty child set as a deny-all narrowing rather than falling back
    # to every provider visible in the worker's config.
    child_spec["authorized_providers"] = sorted(
        requested_authorized & parent_authorized if parent_authorized else requested_authorized
    )
    child_spec["authorized_providers_explicit"] = child_authorized_explicit

    parent_configured_path = parent_spec.get("provider_config_path")
    child_configured_path = child_spec.get("provider_config_path")
    if child_configured_path is not None:
        if not isinstance(child_configured_path, str) or not child_configured_path:
            raise ValueError("child provider_config_path must be a non-empty path")
        if not isinstance(parent_configured_path, str) or not parent_configured_path:
            raise ValueError("child provider_config_path override is forbidden")
        if (
            Path(child_configured_path).expanduser().resolve()
            != Path(parent_configured_path).expanduser().resolve()
        ):
            raise ValueError("child provider_config_path override is forbidden")
    if isinstance(parent_configured_path, str) and parent_configured_path:
        child_spec["provider_config_path"] = parent_configured_path

    parent_fanout = parent_spec.get("fanout_config")
    parent_fanout = parent_fanout if isinstance(parent_fanout, dict) else {}
    child_fanout = child_spec.get("fanout_config")
    if child_fanout is not None and not isinstance(child_fanout, dict):
        raise ValueError("child fanout_config must be an object")
    if isinstance(child_fanout, dict):
        child_fanout = copy.deepcopy(child_fanout)
        parent_nested_keys = set(parent_fanout.get("provider_env_keys") or ())
        nested_keys = child_fanout.get("provider_env_keys")
        if nested_keys is None:
            nested_keys = parent_nested_keys
        elif not isinstance(nested_keys, list | tuple):
            raise ValueError("child fanout_config.provider_env_keys must be a list")
        nested_key_set = set(nested_keys)
        if not all(isinstance(key, str) for key in nested_key_set):
            raise ValueError("child fanout_config.provider_env_keys must contain names")
        if not nested_key_set.issubset(parent_keys | parent_nested_keys):
            raise ValueError(
                "child fanout_config.provider_env_keys would widen parent authorization"
            )
        child_fanout["provider_env_keys"] = sorted(
            nested_key_set & (parent_keys | parent_nested_keys)
        )

        parent_providers = parent_fanout.get("providers")
        child_providers = child_fanout.get("providers")
        if isinstance(parent_providers, list | tuple):
            parent_provider_names = {
                entry.get("name")
                for entry in parent_providers
                if isinstance(entry, dict) and isinstance(entry.get("name"), str)
            }
            if child_providers is None:
                child_fanout["providers"] = copy.deepcopy(list(parent_providers))
            elif not isinstance(child_providers, list | tuple):
                raise ValueError("child fanout_config.providers must be a list")
            else:
                child_provider_names = {
                    entry.get("name")
                    for entry in child_providers
                    if isinstance(entry, dict) and isinstance(entry.get("name"), str)
                }
                if not child_provider_names.issubset(parent_provider_names):
                    raise ValueError("child fanout_config.providers would widen parent identity")
                child_fanout["providers"] = [
                    copy.deepcopy(entry)
                    for entry in parent_providers
                    if isinstance(entry, dict) and entry.get("name") in child_provider_names
                ]
        elif child_providers is not None:
            if not isinstance(child_providers, list | tuple):
                raise ValueError("child fanout_config.providers must be a list")
            child_provider_names = {
                entry.get("name")
                for entry in child_providers
                if isinstance(entry, dict) and isinstance(entry.get("name"), str)
            }
            allowed_names = parent_authorized | {
                value
                for value in (parent_spec.get("assigned_provider"),)
                if isinstance(value, str) and not parent_authorized_explicit
            }
            if not child_provider_names.issubset(allowed_names):
                raise ValueError("child fanout_config.providers would widen parent identity")
        child_spec["fanout_config"] = child_fanout

    parent_assigned = parent_spec.get("assigned_provider")
    child_assigned = child_spec.get("assigned_provider")
    if child_assigned is not None and not isinstance(child_assigned, str):
        raise ValueError("child assigned_provider must be a provider name")
    if isinstance(parent_assigned, str) and child_assigned is None:
        child_spec["assigned_provider"] = parent_assigned
    if (
        isinstance(child_spec.get("assigned_provider"), str)
        and parent_authorized
        and child_spec["assigned_provider"] not in parent_authorized
    ):
        raise ValueError("child assigned_provider is not authorized by the parent")
    child_spec["parent_envelope"] = parent_envelope
    validated = _validate_plan_task(session_dir, child_spec)
    parent_worktree = Path(parent_spec["worktree_path"]).resolve()
    if Path(validated["worktree_path"]).resolve() == parent_worktree:
        raise ValueError("child worktree_path duplicates the parent's worktree")
    if (
        Path(validated["repo"]).resolve() == Path(parent_spec["repo"]).resolve()
        and validated["branch"] == parent_spec["branch"]
    ):
        raise ValueError("child branch duplicates the parent's branch")
    return validated


def _reject_duplicate_task_ids(tasks: list[dict[str, Any]]) -> None:
    """Reject duplicate IDs before validation can create session side effects."""
    seen: set[str] = set()
    for index, task in enumerate(tasks):
        if not isinstance(task, dict):
            continue
        task_id = task.get("task_id")
        if isinstance(task_id, str) and task_id in seen:
            raise DuplicateTaskIDError(f"duplicate task_id {task_id!r} at tasks[{index}]")
        if isinstance(task_id, str):
            seen.add(task_id)


def _task_canonical_status(result: TaskResult) -> str:
    """Map one supervisor TaskResult verdict to a canonical root status.

    The supervisor verdict is authoritative: a worker ``succeeded`` maps to
    ``done`` after either the merge passed or the worker branch was already at
    the resolved base; any failed TaskResult is ``failed`` unless its reason
    identifies a drive-phase timeout.
    """
    if result.status == "succeeded":
        return "done"
    reason = result.reason or ""
    if any(phase in reason for phase in _TIMEOUT_PHASES):
        return "timeout"
    return "failed"


def _aggregate_reason(results: list[TaskResult]) -> str | None:
    failures = [f"{r.task_id}: {r.reason or 'failed'}" for r in results if r.status != "succeeded"]
    if not failures:
        return None
    return "; ".join(failures)


def _build_session_result(
    runtime: _Runtime,
    session_dir: Path,
    started_at: float,
    *,
    cancelled: bool,
) -> Result:
    """Construct the canonical root result for one finished session.

    One task combines the retained sanitized terminal envelope fields with
    the authoritative supervisor verdict; flat multi-task sessions write an
    aggregate status record without inventing a root.
    """
    session_dir = Path(session_dir)
    results = list(runtime.plan_result().results)
    ended_at = time.time()
    failure_reason: str | None
    if cancelled:
        status = "cancelled"
        failure_reason = "cancelled"
        envelope = None
    elif len(results) == 1:
        task_result = results[0]
        status = _task_canonical_status(task_result)
        failure_reason = None if status == "done" else task_result.reason
        envelope = runtime.last_envelope
    else:
        canonical = [_task_canonical_status(result) for result in results]
        if any(item == "failed" for item in canonical):
            status = "failed"
        elif any(item == "timeout" for item in canonical):
            status = "timeout"
        else:
            status = "done"
        failure_reason = None if status == "done" else _aggregate_reason(results)
        envelope = None
    provider: str | None = None
    fell_back_from: str | None = None
    if envelope is not None:
        commits = envelope.get("commits", ())
        files_changed = envelope.get("files_changed", ())
        unified_diff = envelope.get("diff", "")
        diff_truncated = envelope.get("diff_truncated", False)
        summary = envelope.get("summary", "")
        metadata = envelope.get("provider_metadata")
        if isinstance(metadata, dict):
            raw_provider = metadata.get("provider")
            raw_origin = metadata.get("fell_back_from")
            provider = raw_provider if isinstance(raw_provider, str) else None
            fell_back_from = raw_origin if isinstance(raw_origin, str) else None
    else:
        commits = ()
        files_changed = ()
        unified_diff = ""
        diff_truncated = False
        summary = ""
    return Result(
        status=status,
        exit_code=EXIT_CODES[status],
        commits=commits,
        files_changed=files_changed,
        unified_diff=unified_diff,
        diff_truncated=diff_truncated,
        summary=summary,
        metric_score=0.0,
        metric_breakdown={},
        parent_task_id=None,
        event_log_ref=f"sqlite:{session_dir / '.cambium' / 'events.db'}",
        session_id=str(session_dir.resolve()),
        started_at=started_at,
        ended_at=ended_at,
        failure_reason=failure_reason,
        provider=provider,
        fell_back_from=fell_back_from,
    )


# =====================================================================
# Static ready-node waves (implementation-plan §1).
#
# When a plan supplies dependency specs (``depends_on``), the harness owns one
# explicit validated ``TaskTree`` and dispatches static ready-node waves: only
# nodes whose dependencies finished are admitted per wave, the wave's
# concurrency is bounded by the width limit, and a failed node cascades so its
# descendants are never spawned. A flat plan (no ``depends_on``) keeps the
# unbounded one-TaskGroup fan-out path. See ``docs/architecture/architecture.md``
# §3 (production hierarchy and admission).
# =====================================================================


def _has_dependencies(spec: dict[str, Any]) -> bool:
    deps = spec.get("depends_on")
    return isinstance(deps, list) and len(deps) > 0


def _resolve_width(max_width: int | None, plan: dict[str, Any] | list[dict[str, Any]]) -> int:
    """Resolve the per-wave dispatch width: parameter, then plan field, then default."""
    if isinstance(max_width, int) and not isinstance(max_width, bool) and max_width > 0:
        return max_width
    if isinstance(plan, dict):
        field = plan.get("max_width")
        if isinstance(field, int) and not isinstance(field, bool) and field > 0:
            return field
    return MAX_WIDTH


def _resolve_hierarchy(
    specs: list[dict[str, Any]],
    plan: dict[str, Any] | list[dict[str, Any]],
    max_width: int | None,
) -> tuple[TaskTree | None, int | None]:
    """Return ``(tree, width_limit)`` for the plan, or ``(None, None)`` for flat.

    A plan where any task carries ``depends_on`` is built into one validated
    rooted ``TaskTree`` (single root, no multi-parent, no cycle, depth/width
    bounds) before the session is admitted, so a malformed hierarchy raises
    ``TaskTreeError`` with no worker side effect. A flat plan returns the
    unbounded fast path.
    """
    if not any(_has_dependencies(spec) for spec in specs):
        return None, None
    tree_payload = {
        "tasks": [
            {
                "task_id": spec["task_id"],
                "kind": spec.get("kind", "feature"),
                "depends_on": list(spec.get("depends_on") or []),
                "spec": spec,
            }
            for spec in specs
        ]
    }
    tree = build_tree(tree_payload)
    topological_order(tree)  # dispatch-time cycle re-check (architecture §18.1 DS-M6)
    return tree, _resolve_width(max_width, plan)


def _strict_parent_envelope(
    envelope: dict[str, Any] | None, parent_task_id: str | None
) -> dict[str, Any]:
    """Project a retained worker envelope into the strict upward key set.

    The strict key set is ``tasktree._ENVELOPE_KEYS`` (architecture §3.7 I2.7,
    mirrored by ``tasktree.upward_result``). The worker envelope carries
    ``diff`` where the strict set carries ``unified_diff``; missing fields
    default to empty containers or ``None`` so a child can never receive a key
    outside the set or an unbounded transcript.
    """
    src = envelope or {}
    values = {
        "parent_task_id": parent_task_id,
        "unified_diff": src.get("unified_diff", src.get("diff", "")),
        "diff_truncated": src.get("diff_truncated", False),
        "summary": src.get("summary", ""),
        "metric_score": src.get("metric_score", None),
        "metric_breakdown": src.get("metric_breakdown", {}),
        "commits": list(src.get("commits", ())),
        "files_changed": list(src.get("files_changed", ())),
        "status": src.get("status", ""),
    }
    return {key: copy.deepcopy(values[key]) for key in _ENVELOPE_KEYS}


async def _run_wave_bounded(
    runtime: _Runtime, specs: list[dict[str, Any]], width_limit: int | None
) -> None:
    """Dispatch one wave's specs under a wave-local width bound.

    The bound is per-wave, not a global semaphore held across the whole plan:
    queued ready nodes wait on a wave-local ``asyncio.Semaphore`` until a slot
    frees, and the semaphore is released when the wave completes.
    """
    if width_limit is None or width_limit >= len(specs):
        async with asyncio.TaskGroup() as tg:
            runtime._task_group = tg
            for spec in specs:
                tg.create_task(runtime.supervise_task(spec))
        return
    semaphore = asyncio.Semaphore(width_limit)

    async def bounded(spec: dict[str, Any]) -> None:
        async with semaphore:
            await runtime.supervise_task(spec)

    async with asyncio.TaskGroup() as tg:
        runtime._task_group = tg
        for spec in specs:
            tg.create_task(bounded(spec))


async def _dispatch_static_waves(
    runtime: _Runtime, tree: TaskTree, width_limit: int | None
) -> None:
    """Dispatch a validated tree in static ready-node waves with width control.

    Each iteration computes the ready set (``ready_tasks`` over the succeeded
    set), attaches a fresh bounded ``parent_envelope`` (strict key set only)
    to each ready node's snapshotted spec, runs the wave under the width bound,
    then promotes finished nodes to ``succeeded`` or cascades failure to
    descendants that must never be spawned. Specs are deep-copied by
    ``build_tree``; the dispatcher hands ``supervise_task`` a shallow copy of
    the snapshot so per-task base-commit resolution never aliases the tree or
    a sibling.
    """
    children: dict[str, list[str]] = {node.task_id: [] for node in tree.nodes}
    for parent, child in tree.edges:
        children[parent].append(child)

    succeeded = {tid for tid, result in runtime._results.items() if result.status == "succeeded"}
    terminal = set(runtime._results.keys())

    def cascade_skip(failed_tid: str) -> None:
        """Mark every transitive descendant of ``failed_tid`` failed, unspawned."""
        stack = list(children.get(failed_tid, []))
        while stack:
            descendant = stack.pop()
            if descendant in terminal:
                continue
            terminal.add(descendant)
            runtime._results[descendant] = TaskResult(
                task_id=descendant,
                status="failed",
                exit_code=1,
                reason=f"dependency_failed:{failed_tid}",
            )
            stack.extend(children.get(descendant, []))

    for tid in list(terminal):
        if tid not in succeeded:
            cascade_skip(tid)

    while len(terminal) < len(tree.nodes):
        ready_nodes = [
            node for node in ready_tasks(tree, succeeded) if node.task_id not in terminal
        ]
        if not ready_nodes:
            break
        wave_specs: list[dict[str, Any]] = []
        for node in ready_nodes:
            spec = dict(node.spec)
            parent_id = node.parent_task_id
            envelope = runtime._task_envelopes.get(parent_id) if parent_id else None
            spec["parent_envelope"] = _strict_parent_envelope(envelope, parent_id)
            wave_specs.append(spec)

        await _run_wave_bounded(runtime, wave_specs, width_limit)

        for node in ready_nodes:
            tid = node.task_id
            terminal.add(tid)
            result = runtime._results.get(tid)
            if result is not None and result.status == "succeeded":
                succeeded.add(tid)
            else:
                cascade_skip(tid)


async def run_plan(
    session_dir: str | Path,
    plan: dict[str, Any] | list[dict[str, Any]],
    on_event: EventSink | None = None,
    *,
    resource_thresholds: dict[str, Any] | None = None,
    provider_environment: Mapping[str, str] | None = None,
    max_width: int | None = None,
    max_concurrent_tasks: int | None = 0,
    routing_state_path: str | Path | None = None,
    reject_reused_session: bool = False,
    oauth_store: OAuthStore | None = None,
    architectus: Any = None,
    conversations: bool | None = None,
    warm_pool_size: int = 0,
    context_reuse: bool = True,
    resolver_child_enabled: bool = False,
    resolver_max_attempts: int = 1,
) -> PlanResult:
    """Run every task in the plan concurrently under one supervisor session.

    Workers are spawned as ``python -m cambium.worker`` (or the task's
    ``worker`` script); a clean worker whose branch differs from its resolved
    base and whose envelope reports ``succeeded`` is merged onto
    ``refs/heads/main``. A clean branch already at its resolved base succeeds
    without a merge. There is no pre-merge gate: branch state decides whether
    the merge path is required after the worker verdict. Publication is ref-only:
    ``refs/heads/main`` advances via atomic ``update-ref`` and no checkout is
    refreshed.
    Returns a PlanResult; the exact accepted plan is persisted atomically as
    ``<session_dir>/plan.json`` at the session boundary before any worker
    starts, the session's event log is durable in
    ``<session_dir>/.cambium/events.db`` (readable via ``read_events``), and a
    canonical root result is written atomically to
    ``<session_dir>/.cambium/result.json`` before this coroutine returns.

    When ``reject_reused_session`` is set, the session is refused (ValueError
    with the one-shot reused-session message) while the admission lock is
    held, before ``plan.json`` is written, if the leaf already contains run
    artifacts. One-shot explicit sessions pass this so a leaf that became used
    while the caller resolved its provider is rejected instead of overwritten.
    The supervisor's own reconciliation path (re-running a session) keeps the
    default ``False``.

    ``architectus`` is an optional decision port (an ``ArchitectusCore`` or an
    ``aggregate``/``step`` adapter); when set, each admitted parent's envelope
    feeds the port and the resulting typed proposals are routed through the
    existing ``_admit_child`` revision validation. ``conversations=True`` opens
    ``ConversationStore`` at ``<session_dir>/.cambium/conversations.db`` for the
    session (closed on shutdown) and appends one row per admitted/rejected
    revision. Conversations and the warm pool default off; context reuse
    defaults on and can be disabled by internal callers. Conflict resolver
    children are opt-in through ``resolver_child_enabled`` and are capped at
    one attempt by default via ``resolver_max_attempts``.

    Dispatch shape:

    - A flat task list (no task carries ``depends_on``) fans out under one
      ``asyncio.TaskGroup`` — the historical canary behavior.
    - A plan where any task carries ``depends_on`` is validated as one rooted
      ``TaskTree`` (single root, no multi-parent, no cycle, depth/width bounds)
      and dispatched in static ready-node waves: only nodes whose dependencies
      have finished are admitted per wave, and each wave's concurrency is
      bounded by ``max_width`` (explicit parameter, then the plan's
      ``max_width`` field, then ``tasktree.MAX_WIDTH``). A node that fails
      cascades: its descendants are never spawned and are recorded as failed
      with reason ``dependency_failed:<parent>``.
    - ``max_concurrent_tasks`` bounds how many worker processes run at once
      (a session-wide parallel-worker cap, I2.3) on either path. Defaults to
      ``0`` for unlimited concurrency; pass a positive value to cap it. The
      cap covers the worker phase only (spawn through worker exit), never
      merge, prune, or observer notification.
    """
    session_dir = Path(session_dir)
    if type(resolver_child_enabled) is not bool:
        raise ValueError("resolver_child_enabled must be a boolean")
    if type(resolver_max_attempts) is not int or resolver_max_attempts < 0:
        raise ValueError("resolver_max_attempts must be a non-negative int")
    if isinstance(plan, dict):
        plan_resolver_enabled = plan.get("resolver_child_enabled")
        if plan_resolver_enabled is not None:
            if type(plan_resolver_enabled) is not bool:
                raise ValueError("plan resolver_child_enabled must be a boolean")
            resolver_child_enabled = resolver_child_enabled or plan_resolver_enabled
        plan_resolver_attempts = plan.get("resolver_max_attempts")
        if plan_resolver_attempts is not None:
            if type(plan_resolver_attempts) is not int or plan_resolver_attempts < 0:
                raise ValueError("plan resolver_max_attempts must be a non-negative int")
            resolver_max_attempts = plan_resolver_attempts
    tasks = _plan_tasks(plan)
    _reject_duplicate_task_ids(tasks)
    specs = [_validate_plan_task(session_dir, t) for t in tasks]
    if not specs:
        raise ValueError("plan contains no tasks")
    _reject_duplicate_task_ownership(specs)
    if max_concurrent_tasks is not None and (
        type(max_concurrent_tasks) is not int or max_concurrent_tasks < 0
    ):
        raise ValueError("max_concurrent_tasks must be a non-negative int or None")
    if max_concurrent_tasks is None:
        max_concurrent_tasks = 0
    if type(warm_pool_size) is not int or warm_pool_size < 0:
        raise ValueError("warm_pool_size must be a non-negative int")

    _validate_provider_environment(specs, provider_environment, oauth_store=oauth_store)
    _validate_task_repositories(specs)
    tree, width_limit = _resolve_hierarchy(specs, plan, max_width)

    admission = _SessionAdmission(session_dir)
    admission.acquire()
    try:
        if reject_reused_session:
            _reject_reused_session(session_dir)
        await asyncio.to_thread(_write_plan, session_dir, {"tasks": specs})
        started_at = time.time()
        redactor = _session_redactor(specs, provider_environment, oauth_store=oauth_store)
        store = EventStore(session_dir / ".cambium" / "events.db", redactor=redactor)
        # Usage-debt ledger for admission balancing (solution C): load the
        # persisted state once, feed it live from usage_event rows, and
        # persist again when the session ends. The path is injected so the
        # caller owns where routing evidence lives (oneshot defaults it to a
        # repo-scoped file; tests pass scratch paths).
        debt_store = DebtStore(routing_state_path)
        routing_state_load_error: OSError | None = None
        try:
            debt_store.load()
        except OSError as exc:
            routing_state_load_error = exc
        conversations_store = None
        try:
            if conversations:
                conversations_store = ConversationStore(
                    session_dir / ".cambium" / "conversations.db"
                )
        except BaseException:
            await asyncio.to_thread(store.close)
            raise
        runtime = _Runtime(
            session_dir,
            store,
            on_event=on_event,
            redactor=redactor,
            resource_thresholds=resource_thresholds,
            provider_environment=provider_environment,
            max_concurrent_tasks=max_concurrent_tasks,
            debt_store=debt_store,
            oauth_store=oauth_store,
            architectus=architectus,
            conversations=conversations_store,
            warm_pool_size=warm_pool_size,
            context_reuse=context_reuse,
            resolver_child_enabled=resolver_child_enabled,
            resolver_max_attempts=resolver_max_attempts,
            orphan_owner_pid=admission.previous_owner_pid,
        )
        await runtime.start()
        if routing_state_load_error is not None:
            await runtime.emit(
                "log",
                task_id=None,
                message=f"routing-state load failed: {routing_state_load_error}",
            )
        runtime.set_session_tasks(specs)
        cancelled = False
        try:
            reclaim_orphaned = getattr(runtime, "reclaim_orphaned_worktrees", None)
            if reclaim_orphaned is not None:
                await reclaim_orphaned(specs)
            await runtime.reconcile(specs)
            if tree is not None:
                await _dispatch_static_waves(runtime, tree, width_limit)
            else:
                # Batch-aware lane pre-assignment (H1): resolve every
                # un-pinned ``model_candidates`` task in one pass against the
                # persisted debt snapshot plus the lanes, so concurrent
                # admissions in this wave spread across providers. Tree-path
                # waves resolve at admission against the live ledger instead.
                _preassign_lanes(
                    [spec for spec in specs if spec["task_id"] not in runtime._results],
                    debt_store.as_mapping(),
                    runtime._lanes,
                    provider_environment=provider_environment,
                    oauth_store=oauth_store,
                )
                async with asyncio.TaskGroup() as tg:
                    # Dynamic child admission spawns into the active group;
                    # the flat fan-out path owns this group for the session.
                    runtime._task_group = tg
                    for spec in specs:
                        tg.create_task(runtime.supervise_task(spec))
        except asyncio.CancelledError:
            cancelled = True
        finally:
            try:
                if debt_store.dirty:
                    try:
                        await asyncio.to_thread(debt_store.save)
                    except Exception as exc:  # noqa: BLE001
                        # A ledger save failure (disk full, permissions) must
                        # never discard the session result: report and continue.
                        # Emitted while the event store is still open — after
                        # shutdown the record could not be persisted.
                        await runtime.emit(
                            "log",
                            task_id=None,
                            message=f"routing-state save failed: {exc}",
                        )
            finally:
                await runtime.shutdown(session_status="cancelled" if cancelled else "ended")
        result = _build_session_result(runtime, session_dir, started_at, cancelled=cancelled)
        session_id = str(session_dir.resolve())
        await asyncio.to_thread(write_result, result, session_dir, session_id=session_id)
        if cancelled:
            raise asyncio.CancelledError
        return runtime.plan_result()
    finally:
        admission.release()


def _ensure_repo_initialized(repo: Path) -> None:
    """CLI convenience: a missing/empty repo becomes a git repo with a main branch."""
    if not (repo / ".git").exists():
        repo.mkdir(parents=True, exist_ok=True)
        _sh("git", "init", "-b", "main", str(repo))
        _sh("git", "-C", str(repo), "config", "user.name", "cambium")
        _sh("git", "-C", str(repo), "config", "user.email", "cambium@example.com")
        _sh("git", "-C", str(repo), "config", "gc.auto", "0")
    rc = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "--verify", "refs/heads/main"],
        capture_output=True,
        env=_strip_sensitive_env(scrub_environment(), worktree=repo),
    )
    if rc.returncode != 0:
        _sh("git", "-C", str(repo), "commit", "--allow-empty", "-m", "cambium initial")


def _builtin_demo_spec(session_dir: Path) -> dict[str, Any]:
    """Built-in CLI demo: one protocol-fixture task against a seeded repo."""
    return {
        "task_id": "demo-001",
        "worker": str(Path(__file__).resolve().parents[2] / "scripts" / "fake_worker.py"),
        "repo": str(session_dir / "scratch"),
        "worktree_path": str(session_dir / "wt"),
        "branch": "wt-demo-001",
        "target_file": "hello.txt",
        "marker": "// cambium-slice",
        "write_marker": True,
        "task": "append the cambium-slice marker line to the target file",
    }


def _bootstrap_demo_repo(repo: Path, target_file: str) -> None:
    """Seed the CLI demo repo so the built-in worker has a target to edit."""
    _ensure_repo_initialized(repo)
    target = repo / target_file
    if not target.exists():
        target.write_text("hello from the vertical slice\n")
        _sh("git", "-C", str(repo), "add", target_file)
        _sh("git", "-C", str(repo), "commit", "-m", "cambium initial")


def _sh(*args: str, cwd: str | Path | None = None) -> None:
    subprocess.run(
        args,
        cwd=cwd,
        check=True,
        capture_output=True,
        env=_strip_sensitive_env(
            scrub_environment(), worktree=Path(cwd) if cwd is not None else None
        ),
    )


async def _amain_plan(
    session_dir: Path,
    plan: dict[str, Any],
    *,
    conversations: bool = False,
    max_concurrent_tasks: int = 0,
    warm_pool_size: int = 0,
    context_reuse: bool = True,
) -> int:
    loop = asyncio.get_running_loop()

    def print_event(record: dict[str, Any]) -> None:
        print(f"{record['kind']:>16}  {json.dumps(record['payload'])}", flush=True)

    task = asyncio.ensure_future(
        run_plan(
            session_dir,
            plan,
            on_event=print_event,
            conversations=conversations,
            max_concurrent_tasks=max_concurrent_tasks,
            warm_pool_size=warm_pool_size,
            context_reuse=context_reuse,
        )
    )
    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(sig, task.cancel)
        except (NotImplementedError, RuntimeError):
            pass
    try:
        plan_result = await task
    except asyncio.CancelledError:
        print("shutdown: cancelled by signal; workers terminated and store flushed", flush=True)
        return 130
    for r in plan_result.results:
        print(
            f"task {r.task_id}: status={r.status} exit_code={r.exit_code} "
            f"reason={r.reason} merge={r.merge_sha} "
            f"restarts={r.restarts}",
            flush=True,
        )
    print(f"plan: exit_code={plan_result.exit_code}", flush=True)
    return plan_result.exit_code


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="cambium supervisor", description="Cambium supervisor")
    parser.add_argument("--session-dir", required=True)
    inputs = parser.add_mutually_exclusive_group(required=True)
    inputs.add_argument(
        "--plan",
        help='path to plan JSON {"tasks": [{"task_id", "task", "repo", '
        '"worktree_path", "branch", "base_commit", ...}]} '
        "(multi-worker mode)",
    )
    inputs.add_argument(
        "--task-spec",
        help=("path to task spec JSON (one-task mode)"),
    )
    inputs.add_argument("--demo", action="store_true", help="run the built-in mutating demo")
    parser.add_argument(
        "--warm-pool-size",
        type=int,
        default=0,
        help="maximum reusable idle workers (default: 0)",
    )
    parser.add_argument(
        "--conversations",
        action="store_true",
        help="persist child-revision conversations at "
        "<session-dir>/.cambium/conversations.db for the session",
    )
    parser.add_argument(
        "--max-workers",
        type=int,
        default=0,
        metavar="N",
        help="maximum concurrent worker processes (default: unlimited)",
    )
    args = parser.parse_args(argv)
    session_dir = Path(args.session_dir)
    if args.warm_pool_size < 0:
        print("cambium supervisor: --warm-pool-size must be non-negative", file=sys.stderr)
        return 2
    if args.max_workers < 0:
        print("cambium supervisor: --max-workers must be non-negative", file=sys.stderr)
        return 2
    try:
        if args.plan:
            plan = json.loads(Path(args.plan).read_text())
        elif args.task_spec:
            task_spec = json.loads(Path(args.task_spec).read_text())
            plan = {"tasks": [_slice_to_plan_task(task_spec)]}
        else:
            task_spec = _builtin_demo_spec(session_dir)
            _bootstrap_demo_repo(Path(task_spec["repo"]), task_spec["target_file"])
            plan = {"tasks": [_slice_to_plan_task(task_spec)]}
        tasks = _plan_tasks(plan)
        _reject_duplicate_task_ids(tasks)
        if not tasks:
            raise ValueError("plan contains no tasks")
        specs = [_validate_plan_task(session_dir, task) for task in tasks]
        _reject_duplicate_task_ownership(specs)
        _validate_task_repositories(specs)
        try:
            return asyncio.run(
                _amain_plan(
                    session_dir,
                    plan,
                    conversations=args.conversations,
                    max_concurrent_tasks=args.max_workers,
                    warm_pool_size=args.warm_pool_size,
                )
            )
        except KeyboardInterrupt:
            return 130
    except (OSError, ValueError, TypeError, json.JSONDecodeError) as exc:
        print(f"cambium supervisor: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
