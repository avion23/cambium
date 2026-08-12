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
``run_session`` is a thin one-task adapter that keeps the historical
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
import subprocess
import sys
import tempfile
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from cambium.fencing import (
    is_cache_artifact_path,
    next_generation,
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
from .auth import MIN_API_KEY_BYTES, scrub_environment
from .conversations import ConversationStore
from .ipc import MAX_LINE_BYTES, encode_message, write_frame
from .merge import MergeSequencer
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
    LaneState,
    ProviderDebt,
    score_providers,
    select_lane,
    validate_requirements,
)
from .store import CRITICAL_KINDS, EventStore
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

PROTO = 1
WORKER_STDIN_LIMIT = MAX_LINE_BYTES
# Four full-cap messages bound each worker's decoded stdout backlog.
WORKER_STDOUT_QUEUE_MAXSIZE = max(1, MAX_LINE_BYTES // (256 * 1024))
OUTBOUND_MESSAGE_TOO_LONG = "outbound_message_too_long"
STDIN_WRITE_TIMEOUT_S = 5.0
PONG_DEADLINE_S = 10.0
DURABLE_EVENT_TIMEOUT_S = 5.0

EventSink = Callable[[dict[str, Any]], None]

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


def _wire_str(value: Any) -> str | None:
    """Coerce an unvalidated wire value for a JSON-safe event payload."""
    return value if isinstance(value, str) else None


def _protocol_version_mismatch(msg: dict[str, Any]) -> bool:
    if msg.get("type") == "ready":
        return msg.get("proto") != PROTO
    return "proto" in msg and msg["proto"] != PROTO


_TOOL_EVENT_INT_FIELDS = ("batch_index", "batch_size", "turn")
_TOOL_EVENT_DURATION_FIELDS = ("duration_ms",)
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
            type(value) in (int, float) and value >= 0 and math.isfinite(value)
        ):
            invalid.append(field)
    return invalid


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
    for field in ("turn", "prompt_prefix_bytes"):
        if field in msg and not (type(msg[field]) is int and msg[field] >= 0):
            invalid.append(field)
    for field in ("estimated_cost_usd", "latency_s", "retry_after_s"):
        value = msg.get(field)
        if field in msg and not (
            type(value) in (int, float) and value >= 0 and math.isfinite(value)
        ):
            invalid.append(field)
    if "provider_cache_hit" in msg and type(msg["provider_cache_hit"]) is not bool:
        invalid.append("provider_cache_hit")
    for field in (
        "provider", "model", "request_rate_status", "account_quota_owner", "failure_reason"
    ):
        if field in msg and not (type(msg[field]) is str and msg[field]):
            invalid.append(field)
    usage = msg.get("usage")
    if "usage" in msg and (
        not isinstance(usage, dict)
        or any(
            key not in _PROVIDER_METADATA_USAGE_FIELDS
            or not _valid_usage_count(value)
            for key, value in usage.items()
        )
    ):
        invalid.append("usage")
    return invalid


def _stdin_deadline(wall_deadline: float) -> float:
    loop = asyncio.get_running_loop()
    return min(wall_deadline, loop.time() + _stdin_write_timeout_s())


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


def _cfg_float(task_spec: dict[str, Any], key: str, env: str, default: float) -> float:
    spec_value = task_spec.get(key)
    if spec_value is not None:
        return float(spec_value)
    return float(os.environ.get(env, default))


def _warm_pool_size() -> int:
    """Session warm-pool bound from ``CAMBIUM_WARM_POOL_SIZE`` (0 disables)."""
    value = os.environ.get("CAMBIUM_WARM_POOL_SIZE")
    if value is None:
        return DEFAULT_WARM_POOL_SIZE
    try:
        size = int(value)
    except ValueError:
        return DEFAULT_WARM_POOL_SIZE
    return max(0, size)


def _pool_env_key(env: dict[str, str]) -> frozenset[tuple[str, str]]:
    """Env identity for pool matching, ignoring rebindable per-task values.

    ``_worker_environment`` stamps values a rebind cannot change (the child's
    env is fixed at spawn): a pooled worker may only serve a task whose
    remaining env matches exactly. Three stamped values are excluded because
    they are per-task/per-worktree by construction and rebinding re-sends the
    full init:

    - ``CAMBIUM_TASK_ID`` / ``CAMBIUM_GENERATION``: per-task identity, rebuilt
      from the rebind init.
    - ``HOME``: the supervisor injects ``<worktree>/.cambium/home`` so the
      value differs for every worktree. A pooled worker's stale HOME is
      benign (worker git ops use repo-local config and ``GIT_CONFIG_NOSYSTEM``
      is set), so HOME must not block rebinding.

    ``CAMBIUM_SESSION_ID``, ``CAMBIUM_PROVIDERS``, and the allowlisted
    provider credentials remain in the key: a worker whose env cannot serve
    the new task (different session, provider config, or credentials) is
    never popped.
    """
    return frozenset(
        (name, value)
        for name, value in env.items()
        if name not in ("CAMBIUM_TASK_ID", "CAMBIUM_GENERATION", "HOME")
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
    if plan_task.get("fanout_config"):
        task = plan_task.get("task")
        if not isinstance(task, str) or not task.strip():
            raise ValueError("run_session provider mode requires a non-empty task")
    elif not isinstance(plan_task.get("task"), str) or not plan_task["task"].strip():
        plan_task["task"] = "run one task"
    plan_task.setdefault("max_restarts", 0)
    return plan_task


def _task_result_to_slice_result(result: TaskResult) -> SliceResult:
    """Map one :class:`TaskResult` back to the historical slice shape.

    ``TaskResult`` does not retain the worker's own exit code or wire status
    token (the supervisor verdict is authoritative); those slice fields are
    ``None``. Timeout state is recovered from the canonical failure reason.
    """
    reason = result.reason or ""
    timeout_phase = next(
        (phase for phase in _TIMEOUT_PHASES if phase in reason), None
    )
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
# Custos — multi-worker supervisor runtime
# (docs/architecture/architecture.md §5.3, §7.1-§7.8;
#  docs/research/custos-asyncio-design.md)
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
DEFAULT_MAX_TURNS = 20
DEFAULT_MAX_TOKENS = 200_000
RESTART_BASE_DELAY_S = 1.0
RESTART_MAX_DELAY_S = 30.0
# Session-scoped warm worker pool (eval-3 ADOPT): idle workers that reported
# reuse-ready are rebindable to later tasks in the same session, avoiding the
# spawn-to-ready cold start (interpreter boot + heavy imports) per task.
# 0 disables the pool entirely (single-init worker behavior, unchanged).
DEFAULT_WARM_POOL_SIZE = 1
EOF_GRACE_S = 5.0
WORKER_EXIT_WAIT_S = 10.0
TERM_GRACE_S = 5.0
MAX_PARSE_ERRORS = 500
PROTO_UNKNOWN_REQUEST_ID = "PROTO_UNKNOWN_REQUEST_ID"


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
    if isinstance(explicit, (list, tuple)):
        values.extend(explicit)
    fanout_config = spec.get("fanout_config")
    if isinstance(fanout_config, dict):
        configured = fanout_config.get("provider_env_keys")
        if isinstance(configured, (list, tuple)):
            values.extend(configured)
        providers = fanout_config.get("providers")
        if isinstance(providers, (list, tuple)):
            values.extend(
                provider.get("api_key_env")
                for provider in providers
                if isinstance(provider, dict)
            )
    return frozenset(
        value for value in values if isinstance(value, str) and _ENV_NAME_RE.fullmatch(value)
    )


def _provider_environment_value(
    key: str, provider_environment: Mapping[str, str] | None
) -> object:
    """Return the value that the worker environment would forward for *key*."""
    if provider_environment is not None:
        override = provider_environment.get(key)
        if override is not None:
            return override
    return os.environ.get(key)


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


def _oauth_env_suffix(provider: str) -> str:
    """Normalize a provider id into its ``CAMBIUM_OAUTH_*`` environment suffix."""
    return re.sub(r"[._-]+", "_", provider.upper())


def _fanout_provider_names(spec: Mapping[str, Any]) -> frozenset[str]:
    """Provider names a task references through fanout_config or assignment."""
    names: set[str] = set()
    fanout_config = spec.get("fanout_config")
    if isinstance(fanout_config, dict):
        providers = fanout_config.get("providers")
        if isinstance(providers, (list, tuple)):
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
    """The referenced providers whose loaded config tags ``auth=codex_chatgpt``.

    A config that cannot be loaded yields no names: the worker's own config
    load surfaces file errors at its boundary (transport authoritative), so
    this preflight only adds the oauth-document gate on top of a loadable
    config.
    """
    referenced = _fanout_provider_names(spec)
    try:
        providers = load_providers(_provider_config_path(source, spec))
    except (OSError, ValueError):
        return frozenset()
    codex = frozenset(
        provider.name
        for provider in providers
        if provider.auth is AuthMode.CODEX_CHATGPT
    )
    if referenced:
        return codex & referenced
    # No explicit provider restriction: the worker loads every configured
    # provider and cascades over the model-matching ones, so any codex
    # provider may serve this task (e.g. a pinned oneshot run whose
    # fanout_config carries no providers list). Marker-mode tasks never
    # build a router and stay empty.
    fanout_config = spec.get("fanout_config")
    if isinstance(fanout_config, dict) and fanout_config:
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
    if TokenManager(store=store, provider=provider, client_id="").disabled(provider):
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


def _provider_config_path(
    source: Mapping[str, str], spec: Mapping[str, Any] | None = None
) -> str:
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
    client_id = source.get("CAMBIUM_CODEX_CLIENT_ID") or ""
    additions: dict[str, str] = {}
    access_values: list[str] = []
    for provider in sorted(providers):
        manager = TokenManager(store=store, provider=provider, client_id=client_id)
        try:
            access_token, account_id = manager.ensure_fresh()
        except OAuthError as exc:
            hint = (
                " (set CAMBIUM_CODEX_CLIENT_ID to the codex client id "
                "so an expired access token can be refreshed)"
                if not client_id
                else ""
            )
            raise ValueError(
                f"task references codex_chatgpt provider {provider!r} but its "
                f"oauth session could not be ensured fresh: {exc}{hint}"
            ) from None
        additions[f"CAMBIUM_OAUTH_ACCESS_{_oauth_env_suffix(provider)}"] = access_token
        access_values.append(access_token)
        if account_id:
            additions[f"CAMBIUM_OAUTH_ACCOUNT_{_oauth_env_suffix(provider)}"] = account_id
    return additions, access_values


def _worker_environment(
    spec: dict[str, Any], generation: int, *, session_dir: Path | None = None,
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
    oauth_environment, oauth_access_values = _oauth_worker_environment(
        spec, source, oauth_store
    )
    for name, value in oauth_environment.items():
        source[name] = value
        allowed_provider_keys.add(name)
    overrides = {
        "CAMBIUM_TASK_ID": spec["task_id"],
        "CAMBIUM_GENERATION": str(generation),
    }
    if session_dir is not None:
        overrides["CAMBIUM_SESSION_ID"] = str(session_dir.resolve())
    worktree = (
        Path(spec["worktree_path"]).resolve() if "worktree_path" in spec else None
    )
    env = _strip_sensitive_env(
        source,
        allowed_keys=allowed_provider_keys,
        worktree=worktree,
        overrides=overrides,
    )
    if spec.get("fanout_config"):
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
    if isinstance(latency, bool) or not isinstance(latency, (int, float)):
        return None
    usage = value.get("usage")
    if not isinstance(usage, dict):
        usage = {}
    usage_counts = {
        key: count
        for key, count in usage.items()
        if key in _PROVIDER_METADATA_USAGE_FIELDS
        and _valid_usage_count(count)
    }
    return {
        "provider": provider,
        "model": model,
        "usage": usage_counts,
        "latency_s": max(0.0, float(latency)),
    }
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


def read_events(session_dir: Path | str, after_seq: int = 0) -> list[dict[str, Any]]:
    """Replay the session's durable event log from ``after_seq`` (arch §6.3)."""
    store = EventStore(Path(session_dir) / ".cambium" / "events.db")
    try:
        return store.events_after(after_seq)
    finally:
        store.close()


def _open_store(session_dir: Path, *, redactor: Redactor | None = None) -> EventStore:
    """Open the canonical session event store."""
    return EventStore(Path(session_dir) / ".cambium" / "events.db", redactor=redactor)


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


class DuplicateTaskIDError(ValueError):
    """The plan cannot be dispatched because a task id is repeated."""


class InvalidBaseCommitError(ValueError):
    """A task base does not resolve to a commit in its repository."""


class WorktreeRecoveryError(RuntimeError):
    """A destructive worktree recovery command failed."""


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
            proposals.append({
                "request_id": make_request_id(self._seq),
                "parent_task_id": node.parent_task_id,
                "child_task_id": task_id,
                "kind": node.kind.value,
                "spec": copy.deepcopy(node.spec),
            })
        return proposals


class _SessionAdmission:
    """Process-wide and cross-process ownership of one session directory."""

    def __init__(self, session_dir: Path) -> None:
        self._path = session_dir.resolve() / ".cambium" / "session.lock"
        self._fd: int | None = None

    def acquire(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        fd = os.open(self._path, os.O_RDWR | os.O_CREAT | os.O_CLOEXEC, 0o600)
        try:
            os.fchmod(fd, 0o600)
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            os.close(fd)
            raise SessionAlreadyRunningError(
                f"session is already running: {self._path.parent.parent}"
            ) from exc
        except BaseException:
            os.close(fd)
            raise
        self._fd = fd

    def release(self) -> None:
        if self._fd is None:
            return
        fd = self._fd
        self._fd = None
        try:
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


class _Runtime:
    """Custos: the multi-worker supervisor. One instance per run_plan session.

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
        max_concurrent_tasks: int | None = None,
        debt_store: DebtStore | None = None,
        oauth_store: OAuthStore | None = None,
        architectus: Any = None,
        conversations: Any = None,
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
        # max_concurrent_tasks=0 disables the cap (no semaphore); None is
        # rewritten to the auto default by run_plan before _Runtime is built.
        self._admission_semaphore = (
            None if not max_concurrent_tasks else asyncio.Semaphore(max_concurrent_tasks)
        )
        self._handles: dict[str, WorkerHandle] = {}
        self._results: dict[str, TaskResult] = {}
        self._task_envelopes: dict[str, dict[str, Any]] = {}
        self._worktree_lock = asyncio.Lock()
        self._merge_lock = asyncio.Lock()
        self._rid = 0
        self._last_envelope: dict[str, Any] | None = None
        # Dynamic child admission state (implementation-plan step 2).
        self._session_tasks: list[dict[str, Any]] = []
        self._pending_children: dict[str, list[dict[str, Any]]] = {}
        self._child_envelopes: dict[str, list[dict[str, Any]]] = {}
        self._task_group: asyncio.TaskGroup | None = None
        # Per-provider concurrency lanes (H1): in-flight admission counts and
        # rpm-derived caps; incremented at admission, released in
        # ``supervise_task``'s finally on every exit path.
        self._lanes: dict[str, LaneState] = {}
        # Eval-3 ADOPT warm pool: idle reuse-ready worker processes. The pool
        # is bounded by ``_warm_pool_size`` (0 disables) and never survives
        # this runtime (shutdown kills every pooled process).
        self._warm_pool_size = _warm_pool_size()
        self._pool: list[_PooledWorker] = []
        # Decision port and revision conversation persistence (step 2 items
        # 23-24): both optional; None keeps the historical byte-for-byte path.
        self._admission_port = self._make_admission_port(architectus)
        self._admission_port_lock = asyncio.Lock()
        self._conversations = conversations

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
        raise TypeError(
            "architectus must be an ArchitectusCore or a port with "
            "aggregate()/step()"
        )

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
        return dict(redacted)

    # -- event path ---------------------------------------------------------

    def _next_rid(self) -> str:
        self._rid += 1
        return make_request_id(self._rid)

    async def emit(
        self, kind: str, *, task_id: str | None = None, generation: int | None = None,
        request_id: str | None = None, _observer_failure_is_fatal: bool | None = None,
        _deferred_observers: list[tuple[dict[str, Any], bool]] | None = None,
        **payload: Any,
    ) -> None:
        record = {
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
            record = self._redactor.redact_protocol_record(
                record, structural_fields=EVENT_RECORD_STRUCTURAL_FIELDS
            )
            kind = record["kind"]
        durable_record = self._copy_event(record)
        try:
            async with self._event_append_lock:
                await asyncio.to_thread(self._store.append, durable_record)
        except Exception as exc:
            if kind in CRITICAL_KINDS:
                raise
            print(f"cambium: event store error: {exc}", file=sys.stderr)
        if self._on_event is None:
            return
        observer_failure_is_fatal = (
            _observer_failure_is_fatal
            if _observer_failure_is_fatal is not None
            else kind not in CRITICAL_KINDS
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

    async def _notify_deferred_observers(
        self, deferred: list[tuple[dict[str, Any], bool]]
    ) -> None:
        for record, observer_failure_is_fatal in deferred:
            await self._notify_observer(record, observer_failure_is_fatal)

    async def start(self) -> None:
        return

    async def shutdown(self, session_status: str = "ended") -> None:
        """Steps 2-8 of the custos shutdown sequence (design §4)."""
        alive = [
            h.proc for h in self._handles.values()
            if h.proc is not None and h.proc.returncode is None
        ]
        # Eval-3 ADOPT pool hygiene: idle pooled workers are killed with the
        # session; a pooled process that already died is dropped silently.
        alive += [entry.proc for entry in self._pool if entry.proc.returncode is None]
        self._pool.clear()
        for proc in alive:
            try:
                os.killpg(proc.pid, signal.SIGTERM)
            except (ProcessLookupError, PermissionError, OSError):
                pass
        if alive:
            try:
                await asyncio.wait_for(
                    asyncio.gather(*(proc.wait() for proc in alive), return_exceptions=True),
                    TERM_GRACE_S,
                )
            except TimeoutError:
                for proc in alive:
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
                await asyncio.gather(*(proc.wait() for proc in alive), return_exceptions=True)
        try:
            await self.emit(
                "session_ended", task_id=None, session_status=session_status,
                results={tid: r.status for tid, r in self._results.items()},
            )
        except BaseException:
            pass
        await asyncio.to_thread(self._store.close)
        if self._conversations is not None:
            await asyncio.to_thread(self._conversations.close)

    def plan_result(self) -> PlanResult:
        return PlanResult(results=tuple(self._results.values()))

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
            raise WorktreeRecoveryError(
                f"worktree_path must not be the repo itself: {worktree}"
            )
        branch = spec["branch"]
        base = spec["base_commit"]
        await self._git(repo, "worktree", "prune", check=False)
        listing = await self._git_stdout(
            repo, "worktree", "list", "--porcelain", "-z"
        ) or ""
        if worktree in self._registered_worktree_paths(listing):
            return await self._recover_worktree_locked(spec, generation)
        stale_generation = 0
        if worktree.exists():
            stale_generation = await asyncio.to_thread(read_generation, worktree)
        if worktree.exists():
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
            raise WorktreeRecoveryError(
                f"worktree_path must not be the repo itself: {worktree}"
            )
        await self._git(repo, "worktree", "prune", check=False)
        if not worktree.exists():
            return await self._ensure_worktree_locked(spec, generation)
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
            "recover", task_id=spec["task_id"], generation=new_generation,
            base_commit=spec["base_commit"],
        )
        return new_generation

    async def _prune_worktree(self, spec: dict[str, Any]) -> None:
        """Remove a terminal task's clean worker worktree and branch.

        A worker tree may contain edits from a crash or an uncommitted result.
        Without the staging/quarantine contract, those trees are retained and
        reported instead of being force-removed. This preserves the evidence
        for the integration point owned by the staging-quarantine work.
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
                        "worktree_cleanup_deferred", task_id=task_id, reason="list_failed",
                        _deferred_observers=deferred,
                    )
                    return
                registered = any(
                    line.startswith("worktree ")
                    and Path(line[len("worktree "):].strip()).resolve() == worktree
                    for line in listing.stdout.splitlines()
                )
                if not registered:
                    return
                if worktree == repo:
                    await self.emit(
                        "worktree_cleanup_deferred", task_id=task_id, reason="repo_path",
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
                        registered_path = Path(path_line[len("worktree "):].strip()).resolve()
                        if registered_path != worktree and branch_ref in lines:
                            await self.emit(
                                "worktree_cleanup_deferred", task_id=task_id,
                                reason="branch_in_use", _deferred_observers=deferred,
                            )
                            return

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
                        "worktree_cleanup_deferred", task_id=task_id, reason="status_failed",
                        _deferred_observers=deferred,
                    )
                    return
                if any(
                    not _status_line_is_fence(line)
                    for line in status.stdout.splitlines()
                ):
                    await self.emit(
                        "worktree_cleanup_deferred", task_id=task_id, reason="dirty",
                        _deferred_observers=deferred,
                    )
                    return

                fence_dir = worktree / ".cambium"
                if fence_dir.is_dir():
                    shutil.rmtree(fence_dir, ignore_errors=True)
                removed = await self._git(repo, "worktree", "remove", str(worktree), check=False)
                if removed.returncode != 0:
                    await self.emit(
                        "worktree_cleanup_deferred", task_id=task_id, reason="remove_failed",
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
                                repo, "worktree", "add", "--detach", str(worktree), branch,
                                check=False,
                            )
                        await self.emit(
                            "worktree_cleanup_deferred", task_id=task_id,
                            reason="branch_delete_failed", restored=restored.returncode == 0,
                            _deferred_observers=deferred,
                        )
                        return
                await self._git(repo, "worktree", "prune", check=False)
                await self.emit(
                    "worktree_pruned", task_id=task_id, branch=branch,
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

    def _worker_env(self, spec: dict[str, Any], generation: int) -> dict[str, str]:
        session_dir = self._session_dir if self is not None else None
        provider_environment = (
            self._provider_environment if self is not None else None
        )
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
        self, spec: dict[str, Any], run_rid: str, wall_budget: float, generation: int
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
        if not spec.get("fanout_config"):
            payload.update(
                target_file=spec.get("target_file"),
                marker=spec.get("marker"),
                write_marker=bool(spec.get("write_marker", True)),
            )
        # Dynamic child admission: the child's context is its own spec plus
        # the parent's envelope; a child may itself declare proposals.
        if spec.get("parent_envelope") is not None:
            payload["parent_envelope"] = spec["parent_envelope"]
        if spec.get("proposed_children") is not None:
            payload["proposed_children"] = spec["proposed_children"]
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
        values = {
            "parent_task_id": spec.get("parent_task_id"),
            "unified_diff": msg.get("diff", ""),
            "diff_truncated": bool(msg.get("diff_truncated", False)),
            "summary": msg.get("summary", ""),
            "metric_score": None,
            "metric_breakdown": {},
            "commits": msg.get("commits", []),
            "files_changed": msg.get("files_changed", []),
            "status": msg.get("status"),
        }
        return {key: values[key] for key in _ENVELOPE_KEYS}

    async def _admit_child(
        self,
        parent_spec: dict[str, Any],
        proposal: dict[str, Any],
        parent_envelope: dict[str, Any],
    ) -> None:
        """Validate one child revision, record it durably, then spawn it.

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
        request_id = proposal.get("request_id")
        parent_task_id = parent_spec["task_id"]
        child_task_id = proposal["child_task_id"]
        kind = proposal["kind"]
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
                "child_rejected", task_id=parent_task_id, request_id=request_id,
                parent_task_id=parent_task_id, child_task_id=child_task_id,
                child_kind=kind, reason=exc.__class__.__name__, message=str(exc)[:512],
            )
            await self._record_revision_conversation(
                outcome="rejected", parent_task_id=parent_task_id,
                child_task_id=child_task_id, child_kind=kind, request_id=request_id,
                reason=exc.__class__.__name__, proposal=proposal,
            )
            return
        try:
            child_spec = _child_spec(
                self._session_dir, parent_spec, proposal, parent_envelope
            )
        except ValueError as exc:
            await self.emit(
                "child_rejected", task_id=parent_task_id, request_id=request_id,
                parent_task_id=parent_task_id, child_task_id=child_task_id,
                child_kind=kind, reason=exc.__class__.__name__, message=str(exc)[:512],
            )
            await self._record_revision_conversation(
                outcome="rejected", parent_task_id=parent_task_id,
                child_task_id=child_task_id, child_kind=kind, request_id=request_id,
                reason=exc.__class__.__name__, proposal=proposal,
            )
            return
        # Append synchronously before any await so a concurrent proposal
        # validation observes this child (duplicate detection stays exact).
        self._session_tasks.append({
            "task_id": child_task_id,
            "kind": kind,
            "depends_on": [parent_task_id],
            "spec": child_spec,
        })
        try:
            if self._task_group is None:
                raise RuntimeError("no active task group")
            self._task_group.create_task(self.supervise_task(child_spec))
        except RuntimeError as exc:
            # Group already closed/cancelled: roll the tree back so the child
            # is not left durably admitted but never spawned, and record the
            # rejection instead.
            self._session_tasks[:] = [
                task for task in self._session_tasks
                if task.get("spec") is not child_spec
            ]
            await self.emit(
                "child_rejected", task_id=parent_task_id, request_id=request_id,
                parent_task_id=parent_task_id, child_task_id=child_task_id,
                child_kind=kind, reason="NoActiveTaskGroup", message=str(exc)[:512],
            )
            await self._record_revision_conversation(
                outcome="rejected", parent_task_id=parent_task_id,
                child_task_id=child_task_id, child_kind=kind, request_id=request_id,
                reason="NoActiveTaskGroup", proposal=proposal,
            )
            return
        await self.emit(
            "child_admitted", task_id=parent_task_id, request_id=request_id,
            parent_task_id=parent_task_id, child_task_id=child_task_id,
            child_kind=kind, branch=child_spec.get("branch"),
        )
        await self._record_revision_conversation(
            outcome="admitted", parent_task_id=parent_task_id,
            child_task_id=child_task_id, child_kind=kind, request_id=request_id,
            proposal=proposal,
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
        await asyncio.to_thread(
            self._conversations.append,
            child_task_id,
            "system",
            json.dumps(record, sort_keys=True, default=str),
            kind="system",
            meta={"parent_task_id": parent_task_id},
        )

    def _redact_proposal(self, proposal: dict[str, Any]) -> dict[str, Any]:
        """Redact a propose_child wire record without flattening its structure."""
        if self._redactor is None:
            return dict(proposal)
        redacted = self._redactor.redact_protocol_record(
            proposal,
            structural_fields=("request_id", "parent_task_id", "child_task_id", "kind"),
        )
        return dict(redacted)

    async def _admit_port_proposals(
        self, parent_spec: dict[str, Any], parent_envelope: dict[str, Any]
    ) -> None:
        """Feed one admitted parent's envelope to the decision port and admit its proposals.

        The port is the only provider-side channel whose response can become a
        child proposal: its aggregate/step output is routed through the
        existing ``_admit_child`` revision validation — never the live tree
        directly. A malformed or mismatched proposal is durably rejected with
        ``child_rejected`` and spawns nothing. A finished task outside the
        port's decision domain (unknown to its tree) yields no wave: the port
        has nothing to propose for it.
        """
        parent_task_id = parent_spec["task_id"]
        malformed: str | None = None
        proposals: list[dict[str, Any]] = []
        async with self._admission_port_lock:
            try:
                self._admission_port.aggregate(parent_task_id, parent_envelope)
            except ValueError:
                return
            try:
                proposals = await self._admission_port.step([
                    {
                        "kind": "child_result",
                        "task_id": parent_task_id,
                        "payload": dict(parent_envelope),
                    }
                ])
            except (TypeError, ValueError) as exc:
                malformed = repr(exc)
        if malformed is not None:
            await self.emit(
                "child_rejected", task_id=parent_task_id,
                parent_task_id=parent_task_id, child_task_id=None, child_kind=None,
                reason="MalformedProposal", message=f"decision port error: {malformed}"[:512],
            )
            return
        for proposal in proposals:
            await self._admit_port_proposal(parent_spec, parent_envelope, proposal)

    async def _admit_port_proposal(
        self,
        parent_spec: dict[str, Any],
        parent_envelope: dict[str, Any],
        proposal: Any,
    ) -> None:
        """Route one decision-port proposal through the revision boundary."""
        parent_task_id = parent_spec["task_id"]
        if not isinstance(proposal, dict):
            await self.emit(
                "child_rejected", task_id=parent_task_id,
                parent_task_id=parent_task_id, child_task_id=None, child_kind=None,
                reason="MalformedProposal",
                message="decision port returned a non-object proposal",
            )
            return
        invalid_fields = _invalid_propose_child_fields(proposal)
        if invalid_fields:
            await self.emit(
                "child_rejected", task_id=parent_task_id,
                request_id=_wire_str(proposal.get("request_id")),
                parent_task_id=parent_task_id,
                child_task_id=_wire_str(proposal.get("child_task_id")),
                child_kind=_wire_str(proposal.get("kind")),
                reason="MalformedProposal",
                message=f"decision port proposal rejected: invalid field(s) {invalid_fields}",
            )
            await self._record_port_rejection(
                parent_task_id, proposal, "MalformedProposal",
                f"invalid field(s) {invalid_fields}",
            )
            return
        if proposal.get("parent_task_id") != parent_task_id:
            await self.emit(
                "child_rejected", task_id=parent_task_id,
                request_id=_wire_str(proposal.get("request_id")),
                parent_task_id=parent_task_id,
                child_task_id=proposal["child_task_id"],
                child_kind=proposal["kind"],
                reason="ParentTaskIdMismatch",
                message="decision port proposal parent_task_id does not match the finished task",
            )
            await self._record_port_rejection(
                parent_task_id, proposal, "ParentTaskIdMismatch",
                "parent_task_id does not match the finished task",
            )
            return
        await self._admit_child(parent_spec, proposal, parent_envelope)

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
            outcome="rejected", parent_task_id=parent_task_id,
            child_task_id=child_task_id,
            child_kind=_wire_str(proposal.get("kind")),
            request_id=_wire_str(proposal.get("request_id")),
            reason=f"{reason}: {message}"[:512], proposal=proposal,
        )

    # -- per-task supervision ------------------------------------------------

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
            spec, self._debt_store.as_mapping(), self._lanes
        ):
            self._lanes[spec["assigned_provider"]].in_flight += 1

    async def supervise_task(self, spec: dict[str, Any]) -> None:
        if spec["task_id"] in self._results:
            # Reconcile may have marked the task finished before its lane was
            # reserved; release the reservation if one was made.
            _release_lane(self._lanes, spec)
            return
        try:
            await self._supervise(spec)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            await self.emit(
                "worker_failed", task_id=spec["task_id"], reason=f"supervisor error: {exc!r}"
            )
            self._results[spec["task_id"]] = TaskResult(
                task_id=spec["task_id"], status="failed", exit_code=1,
                reason=f"supervisor error: {exc.__class__.__name__}",
            )
        finally:
            # Lane release (H1): every task that holds a lane reservation
            # (batch pre-assignment or admission-time assignment) frees it on
            # every exit path — success, failure, exception, cancellation.
            _release_lane(self._lanes, spec)
            if spec["task_id"] in self._results:
                await self._prune_worktree(spec)
            # Proposals buffered but never processed (the parent ended without
            # a result envelope) are dropped with a durable rejection so they
            # neither leak nor vanish silently.
            pending = self._pending_children.pop(spec["task_id"], [])
            for proposal in pending:
                await self.emit(
                    "child_rejected", task_id=spec["task_id"],
                    request_id=proposal.get("request_id"),
                    parent_task_id=spec["task_id"],
                    child_task_id=proposal["child_task_id"],
                    child_kind=proposal.get("kind"),
                    reason="ParentTerminatedWithoutResult",
                    message="parent ended without a result envelope; proposal dropped",
                )
            # A child task that ends failed is otherwise invisible to its
            # parent: emit one correlated failure event so the parent learns
            # the child did not succeed. Succeeded/cancelled children stay
            # quiet here (their envelopes already carry the outcome).
            parent_task_id = spec.get("parent_task_id")
            if parent_task_id is not None:
                child_result = self._results.get(spec["task_id"])
                if child_result is not None and child_result.status == "failed":
                    await self.emit(
                        "child_failed", task_id=spec["task_id"],
                        parent_task_id=parent_task_id,
                        reason=child_result.reason or "failed",
                    )

    async def _supervise(self, spec: dict[str, Any]) -> None:
        task_id = spec["task_id"]
        repo = Path(spec["repo"])
        worktree = Path(spec["worktree_path"])
        max_restarts = int(spec.get("max_restarts", DEFAULT_MAX_RESTARTS))
        ready_timeout = _cfg_float(
            spec, "ready_timeout_s", "CAMBIUM_READY_TIMEOUT_S", DEFAULT_READY_TIMEOUT_S
        )
        heartbeat_interval = _cfg_float(
            spec, "heartbeat_interval_s", "CAMBIUM_HEARTBEAT_INTERVAL_S",
            DEFAULT_HEARTBEAT_INTERVAL_S,
        )
        heartbeat_timeout = _cfg_float(
            spec, "heartbeat_timeout_s", "CAMBIUM_HEARTBEAT_TIMEOUT_S",
            DEFAULT_HEARTBEAT_TIMEOUT_S,
        )
        wall_budget = _cfg_float(
            spec, "max_wall_s", "CAMBIUM_WALL_BUDGET_S", DEFAULT_WALL_BUDGET_S
        )
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
                self._results[task_id] = TaskResult(
                    task_id=task_id,
                    status="failed",
                    exit_code=126,
                    reason="resource_denied",
                )
                return
        generation = await self._ensure_worktree(spec)

        # The admission semaphore bounds concurrent worker processes only. It
        # is held across the generation loop and released before merge, prune,
        # and observer notification, so a barrier observer waiting on a peer
        # task's merge can never deadlock against tasks still queued for an
        # admission slot.
        semaphore = self._admission_semaphore
        if semaphore is not None:
            await semaphore.acquire()
        try:
            # Admission-time balancing (solution C): resolve (provider, model)
            # for un-pinned ``model_candidates`` tasks only now that the task
            # owns an admission slot, so the usage-debt ledger reflects every
            # usage event already folded by earlier admissions. The decision
            # is idempotent across restarts (a resolved spec carries a model).
            self._resolve_assignment(spec)
            assigned_payload: dict[str, Any] = {
                "task_id": task_id, "repo": str(repo), "branch": spec["branch"],
                "base_commit": spec["base_commit"], "task": spec.get("task", ""),
            }
            fanout_config = spec.get("fanout_config")
            if isinstance(fanout_config, dict) and isinstance(
                fanout_config.get("model"), str
            ):
                assigned_payload["model"] = fanout_config["model"]
            if isinstance(spec.get("assigned_provider"), str):
                assigned_payload["assigned_provider"] = spec["assigned_provider"]
            if isinstance(spec.get("requirements"), dict) and spec["requirements"]:
                assigned_payload["requirements"] = spec["requirements"]
            await self.emit("task_assigned", **assigned_payload)
            restarts = 0
            worker_summary: str | None = None
            # Eval-3 ADOPT: only the first generation may pop the warm pool.
            # Restart generations always spawn a fresh process (a restarted
            # worker must never reuse a pooled process).
            first_generation = True
            while True:
                handle = WorkerHandle(task_id=task_id, generation=generation)
                self._handles[task_id] = handle
                outcome = await self._drive_generation(
                    spec, handle,
                    ready_timeout=ready_timeout,
                    heartbeat_interval=heartbeat_interval,
                    heartbeat_timeout=heartbeat_timeout,
                    wall_budget=wall_budget,
                    allow_pool=first_generation,
                )
                first_generation = False
                sanitized_envelope: dict[str, Any] | None = None
                if outcome.envelope is not None and outcome.correlated:
                    sanitized_envelope = self._redact_envelope(outcome.envelope)
                    self._last_envelope = sanitized_envelope
                    self._task_envelopes[task_id] = sanitized_envelope
                    worker_summary = _envelope_text(sanitized_envelope, "summary")
                if outcome.clean:
                    envelope_status = (
                        outcome.envelope.get("status")
                        if outcome.envelope is not None else None
                    )
                    if envelope_status != "succeeded":
                        failure_reason = _envelope_text(sanitized_envelope, "failure_reason")
                        if failure_reason is None:
                            failure_reason = "worker_verdict_failed"
                        self._results[task_id] = TaskResult(
                            task_id=task_id, status="failed", exit_code=1,
                            reason=failure_reason, restarts=restarts, summary=worker_summary,
                        )
                        return
                    integrity = await self._worker_success_integrity(spec, worktree)
                    head: str | None = None
                    if integrity is None:
                        head = await self._git_stdout(
                            worktree, "rev-parse", "--verify", "HEAD^{commit}", check=False
                        )
                        if head is None:
                            integrity = "worker_head_failed"
                    if integrity is not None:
                        await self.emit(
                            "worker_failed", task_id=task_id, generation=generation,
                            reason=integrity,
                        )
                        self._results[task_id] = TaskResult(
                            task_id=task_id, status="failed", exit_code=1,
                            reason=integrity, restarts=restarts, summary=worker_summary,
                        )
                        return
                    if head == spec["base_commit"]:
                        self._results[task_id] = TaskResult(
                            task_id=task_id, status="succeeded", exit_code=0,
                            reason=None, merge_sha=None, restarts=restarts,
                            summary=worker_summary,
                        )
                        return
                    merged = await self._merge_task(spec, handle)
                    if merged is not None:
                        self._results[task_id] = TaskResult(
                            task_id=task_id, status="succeeded", exit_code=0,
                            reason=None, merge_sha=merged, restarts=restarts,
                            summary=worker_summary,
                        )
                    else:
                        self._results[task_id] = TaskResult(
                            task_id=task_id, status="failed", exit_code=1,
                            reason="merge_failed", restarts=restarts, summary=worker_summary,
                        )
                    return
                if outcome.fatal:
                    self._results[task_id] = TaskResult(
                        task_id=task_id, status="failed", exit_code=1,
                        reason=outcome.reason, restarts=restarts, summary=worker_summary,
                    )
                    return
                reason = outcome.reason or "crash"
                if outcome.timeout_phase:
                    await self.emit(
                        "timeout",
                        task_id=task_id,
                        generation=generation,
                        phase=outcome.timeout_phase,
                    )
                if restarts >= max_restarts:
                    await self.emit(
                        "worker_failed", task_id=task_id, generation=generation,
                        restarts=restarts, max_restarts=max_restarts, reason=reason,
                    )
                    self._results[task_id] = TaskResult(
                        task_id=task_id, status="failed", exit_code=1,
                        reason=f"max_restarts ({max_restarts}): {reason}",
                        restarts=restarts, summary=worker_summary,
                    )
                    return
                restarts += 1
                delay = random.uniform(
                    0.0, min(RESTART_MAX_DELAY_S, RESTART_BASE_DELAY_S * 2**restarts)
                )
                await self.emit(
                    "restart_scheduled", task_id=task_id, generation=generation,
                    restart_count=restarts, max_restarts=max_restarts,
                    delay_s=round(delay, 3), reason=reason,
                )
                await asyncio.sleep(delay)
                generation = await self._recover_worktree(spec, generation + 1)
        finally:
            if semaphore is not None:
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
        self._pool.append(
            _PooledWorker(proc=proc, cmd=tuple(cmd), env_key=_pool_env_key(env))
        )

    @staticmethod
    async def _kill_pooled(proc: asyncio.subprocess.Process) -> None:
        """Kill one pooled process and reap it (no zombies left behind)."""
        await _kill_worker(proc)
        try:
            await asyncio.wait_for(proc.wait(), WORKER_EXIT_WAIT_S)
        except (TimeoutError, asyncio.CancelledError):
            pass

    async def _drive_generation(
        self, spec: dict[str, Any], handle: WorkerHandle, *,
        ready_timeout: float, heartbeat_interval: float, heartbeat_timeout: float,
        wall_budget: float, allow_pool: bool = True,
    ) -> _GenOutcome:
        task_id = spec["task_id"]
        worktree = Path(spec["worktree_path"])
        generation = handle.generation
        cmd = self._worker_command(spec)
        env = self._worker_env(spec, generation)
        # A new generation is a fresh worker process: proposals buffered by a
        # previous, dead generation are stale and must not be admitted when a
        # later generation delivers its result.
        self._pending_children.pop(task_id, None)

        async def _report_outbound_message_too_long() -> None:
            await self.emit(
                "protocol", task_id=task_id, generation=generation,
                error_type="OUTBOUND_MESSAGE_TOO_LONG",
                note="outbound message exceeds MAX_LINE_BYTES",
            )

        init_rid = self._next_rid()
        init_msg = {
            "type": "init", "request_id": init_rid, "task_id": task_id,
            "proto": PROTO, "generation": generation,
            "worktree": str(worktree), "base_commit": spec["base_commit"],
            "spec": spec.get("task", ""),
            "max_turns": int(spec.get("max_turns", DEFAULT_MAX_TURNS)),
            "max_tokens": int(spec.get("max_tokens", DEFAULT_MAX_TOKENS)),
            "heartbeat": {"interval_s": heartbeat_interval, "timeout_s": heartbeat_timeout},
            "budget": {"max_wall_s": wall_budget, "max_restarts": DEFAULT_MAX_RESTARTS},
            "permissions": {"shell": True, "network": False},
            "provider_env_keys": list(spec.get("provider_env_keys", ())),
        }
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
        if spec.get("fanout_config"):
            init_msg["fanout_config"] = spec["fanout_config"]
            init_msg["provider_env_keys"] = sorted(_provider_env_keys(spec))
        if isinstance(spec.get("assigned_provider"), str):
            # Admission balancing (solution C): the worker presets Diffundo's
            # sticky primary from this value instead of the seeded first pick.
            init_msg["assigned_provider"] = spec["assigned_provider"]
        if encode_message(init_msg) is None:
            await _report_outbound_message_too_long()
            return _GenOutcome(
                clean=False, fatal=True, reason=OUTBOUND_MESSAGE_TOO_LONG,
            )

        # Eval-3 ADOPT warm pool: the first generation of a task may pop a
        # matching idle worker instead of spawning. Restart generations pass
        # allow_pool=False and always spawn fresh: a restarted task must
        # never reuse a pooled process (it may have run the same task).
        pooled = self._pool_pop(cmd, env) if allow_pool else None
        if pooled is not None:
            await self.emit(
                "worker_reused", task_id=task_id, generation=generation, pid=pooled.pid
            )
        if pooled is None:
            await self.emit("spawned", task_id=task_id, generation=generation, worker=" ".join(cmd))
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

        messages: asyncio.Queue[dict[str, Any] | None] = asyncio.Queue(
            maxsize=WORKER_STDOUT_QUEUE_MAXSIZE
        )
        parse_errors = 0
        message_too_long = False

        async def _read_stdout() -> None:
            nonlocal parse_errors, message_too_long
            try:
                async for raw in proc.stdout:
                    line = raw.decode("utf-8", "replace").rstrip("\n")
                    if not line.strip():
                        continue
                    try:
                        msg = json.loads(line)
                    except json.JSONDecodeError as exc:
                        parse_errors += 1
                        await self.emit(
                            "parse_error", task_id=task_id, generation=generation,
                            message=str(exc)[:256],
                        )
                        if parse_errors > MAX_PARSE_ERRORS:
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
                        parse_errors += 1
                        await self.emit(
                            "parse_error", task_id=task_id, generation=generation,
                            message="valid JSON line is not an object",
                        )
                        if parse_errors > MAX_PARSE_ERRORS:
                            try:
                                os.killpg(proc.pid, signal.SIGKILL)
                            except (ProcessLookupError, PermissionError, OSError):
                                pass
                        continue
                    await messages.put(msg)
            except (ValueError, asyncio.LimitOverrunError) as exc:
                message_too_long = True
                await self.emit(
                    "protocol", task_id=task_id, generation=generation,
                    note="MessageTooLong", message=str(exc)[:256],
                )
                await _kill_worker(proc)
            finally:
                current = asyncio.current_task()
                if current is None or not current.cancelling():
                    await messages.put(None)

        async def _read_stderr() -> None:
            async for raw in proc.stderr:
                line = raw.decode("utf-8", "replace").rstrip("\n")
                if line.strip():
                    if self._redactor is not None:
                        line = self._redactor.redact_escaped(line)
                    await self.emit(
                        "log", task_id=task_id, generation=generation, stream="stderr",
                        message=line[:512],
                    )

        stdout_task = asyncio.create_task(_read_stdout())
        stderr_task = asyncio.create_task(_read_stderr())
        loop = asyncio.get_running_loop()
        wall_deadline = loop.time() + wall_budget

        await self.emit("init", task_id=task_id, request_id=init_rid, generation=generation)
        init_written = await _write_json(
            proc,
            init_msg,
            deadline=_stdin_deadline(wall_deadline),
        )

        phase = "ready"  # "ready" | "run"
        ready_deadline = loop.time() + ready_timeout if init_written else loop.time()
        last_heartbeat: float | None = None
        run_rid: str | None = None
        envelope: dict[str, Any] | None = None
        exit_reason: str | None = None
        correlated = False
        protocol_reason: str | None = None
        protocol_failure: str | None = None
        timeout_phase: str | None = "stdin" if not init_written else None
        # Eval-3 ADOPT: set when the worker reported reuse-ready; the process
        # is kept alive and returned to the session pool instead of being
        # waited on and reaped as a terminal worker.
        reuse_ready = False
        keep_alive = False

        async def _cancel_and_kill() -> None:
            cancel_msg = {
                "type": "cancel",
                "request_id": self._next_rid(),
                "reason": timeout_phase or "timeout",
            }
            try:
                if encode_message(cancel_msg) is None:
                    await _report_outbound_message_too_long()
                else:
                    await _write_json(
                        proc, cancel_msg, deadline=_stdin_deadline(wall_deadline)
                    )
            except Exception:
                pass
            await _kill_worker(proc)

        async def _probe_after_eof() -> bool:
            """Require one exact pong before treating an EOF survivor as live."""
            nonlocal protocol_failure, timeout_phase
            if proc.returncode is not None:
                return False
            pong_rid = self._next_rid()
            pong_deadline = min(wall_deadline, loop.time() + PONG_DEADLINE_S)
            ping_msg = {
                "type": "ping", "request_id": pong_rid, "task_id": task_id,
                "generation": generation,
            }
            await self.emit(
                "ping", task_id=task_id, generation=generation, request_id=pong_rid
            )
            if encode_message(ping_msg) is None:
                protocol_failure = OUTBOUND_MESSAGE_TOO_LONG
                await _report_outbound_message_too_long()
                await _kill_worker(proc)
                return False
            if not await _write_json(
                proc, ping_msg,
                deadline=pong_deadline,
            ):
                timeout_phase = "pong"
                await self.emit(
                    "protocol", task_id=task_id, generation=generation,
                    note="missing correlated pong after EOF", expected=pong_rid,
                )
                return False
            while loop.time() < pong_deadline:
                remaining = pong_deadline - loop.time()
                try:
                    response = await asyncio.wait_for(messages.get(), remaining)
                except TimeoutError:
                    break
                if response is None:
                    break
                if _protocol_version_mismatch(response):
                    protocol_failure = "PROTO_VERSION_MISMATCH"
                    await self.emit(
                        "protocol", task_id=task_id, generation=generation,
                        error_type=protocol_failure, expected=PROTO,
                        got=response.get("proto"),
                    )
                    return False
                if response.get("type") != "pong":
                    await self.emit(
                        "protocol", task_id=task_id, generation=generation,
                        note="unexpected message during EOF pong probe",
                        type=response.get("type"),
                    )
                    continue
                if response.get("request_id") != pong_rid:
                    await self.emit(
                        "protocol", task_id=task_id, generation=generation,
                        note="pong request_id mismatch",
                        expected=pong_rid, got=response.get("request_id"),
                    )
                    continue
                await self.emit(
                    "pong", task_id=task_id, generation=generation, request_id=pong_rid
                )
                return True
            timeout_phase = "pong"
            await self.emit(
                "protocol", task_id=task_id, generation=generation,
                note="missing correlated pong after EOF", expected=pong_rid,
            )
            return False

        try:
            while True:
                now = loop.time()
                if now >= wall_deadline:
                    timeout_phase = "wall"
                    await _cancel_and_kill()
                    break
                if phase == "ready" and now >= ready_deadline:
                    if timeout_phase is None:
                        timeout_phase = "ready"
                    await _cancel_and_kill()
                    break
                if (
                    phase == "run" and last_heartbeat is not None
                    and now - last_heartbeat > heartbeat_timeout
                ):
                    timeout_phase = "heartbeat"
                    await _cancel_and_kill()
                    break
                next_deadline = wall_deadline
                if phase == "ready":
                    next_deadline = min(next_deadline, ready_deadline)
                if phase == "run" and last_heartbeat is not None:
                    next_deadline = min(next_deadline, last_heartbeat + heartbeat_timeout)
                remaining = next_deadline - loop.time()
                try:
                    msg = await asyncio.wait_for(messages.get(), max(remaining, 0.0))
                except TimeoutError:
                    continue
                if msg is None:
                    # EOF alone is never death (arch §5.3): grace, then an
                    # exact request_id-correlated ping/pong probe.
                    await self.emit(
                        "log", task_id=task_id, generation=generation,
                        message="stdout EOF; grace then poll",
                    )
                    await asyncio.sleep(
                        min(EOF_GRACE_S, max(0.0, wall_deadline - loop.time()))
                    )
                    if proc.returncode is None:
                        probe_ok = await _probe_after_eof()
                        if probe_ok:
                            try:
                                await asyncio.wait_for(
                                    proc.wait(),
                                    min(EOF_GRACE_S, max(0.0, wall_deadline - loop.time())),
                                )
                            except TimeoutError:
                                await self.emit(
                                    "log", task_id=task_id, generation=generation,
                                    message="EOF survivor did not exit after correlated pong",
                                )
                                await _kill_worker(proc)
                        else:
                            await self.emit(
                                "log", task_id=task_id, generation=generation,
                                    message=(
                                        "EOF survivor has no correlated pong; "
                                        "killing process group"
                                    ),
                            )
                            await _kill_worker(proc)
                    break
                mtype = msg.get("type")
                if _protocol_version_mismatch(msg):
                    protocol_failure = "PROTO_VERSION_MISMATCH"
                    await self.emit(
                        "protocol", task_id=task_id, generation=generation,
                        error_type=protocol_failure, expected=PROTO, got=msg.get("proto"),
                    )
                    await _kill_worker(proc)
                    break
                if mtype == "ready":
                    if msg.get("request_id") != init_rid:
                        protocol_reason = "ready_request_id_mismatch"
                        await self.emit(
                            "protocol", task_id=task_id, generation=generation,
                            request_id=msg.get("request_id"), code=PROTO_UNKNOWN_REQUEST_ID,
                            note="ready request_id mismatch", expected=init_rid,
                            got=msg.get("request_id"),
                        )
                        await _kill_worker(proc)
                        break
                    phase = "run"
                    last_heartbeat = loop.time()
                    handle.state = "RUNNING"
                    await self.emit(
                        "ready", task_id=task_id, request_id=msg.get("request_id"),
                        generation=generation, pid=msg.get("pid"), proto=msg.get("proto"),
                    )
                    run_rid = self._next_rid()
                    payload = self._run_payload(spec, run_rid, wall_budget, generation)
                    run_msg = {
                        "type": "run_task", "request_id": run_rid,
                        "task_id": task_id, **payload,
                    }
                    if encode_message(run_msg) is None:
                        protocol_failure = OUTBOUND_MESSAGE_TOO_LONG
                        await _report_outbound_message_too_long()
                        await _kill_worker(proc)
                        break
                    if not await _write_json(
                        proc, run_msg,
                        deadline=_stdin_deadline(wall_deadline),
                    ):
                        timeout_phase = "stdin"
                        await self.emit("protocol", task_id=task_id, note="run_task write failed")
                        await _kill_worker(proc)
                        break
                    await self.emit(
                        "run_task", task_id=task_id, request_id=run_rid, generation=generation
                    )
                elif mtype in ("result", "result_envelope"):
                    envelope = msg
                    correlated = run_rid is not None and msg.get("request_id") == run_rid
                    if not correlated:
                        await self.emit(
                            "protocol", task_id=task_id, note="result request_id mismatch",
                            expected=run_rid, got=msg.get("request_id"),
                        )
                    result_payload: dict[str, Any] = {"status": msg.get("status")}
                    provider_metadata = _redacted_provider_metadata(msg.get("provider_metadata"))
                    if provider_metadata is not None:
                        result_payload["provider_metadata"] = provider_metadata
                    await self.emit(
                        "result", task_id=task_id, request_id=msg.get("request_id"),
                        generation=generation, **result_payload,
                    )
                    # Dynamic child admission: proposals are processed only
                    # now, when the parent's terminal envelope exists (the
                    # child's context is its own spec plus that envelope).
                    pending = self._pending_children.pop(task_id, [])
                    if pending or self._admission_port is not None:
                        parent_envelope = self._redact_envelope(
                            self._strict_envelope(spec, msg)
                        )
                        for proposal in pending:
                            await self._admit_child(spec, proposal, parent_envelope)
                        if self._admission_port is not None:
                            await self._admit_port_proposals(spec, parent_envelope)
                    if spec.get("parent_task_id") is not None:
                        child_result = self._strict_envelope(spec, msg)
                        await self.emit(
                            "child_result", task_id=task_id, generation=generation,
                            **child_result,
                        )
                        self._child_envelopes.setdefault(
                            spec["parent_task_id"], []
                        ).append(child_result)
                elif mtype == "propose_child":
                    invalid_fields = _invalid_propose_child_fields(msg)
                    if invalid_fields:
                        await self.emit(
                            "protocol", task_id=task_id, generation=generation,
                            note="propose_child rejected: invalid field(s)",
                            fields=invalid_fields,
                        )
                    elif msg.get("parent_task_id") != task_id:
                        await self.emit(
                            "protocol", task_id=task_id, generation=generation,
                            note="propose_child parent_task_id mismatch",
                            parent_task_id=msg.get("parent_task_id"),
                            child_task_id=msg.get("child_task_id"),
                        )
                    else:
                        # Buffered until the parent's terminal envelope
                        # arrives; admission then validates the revision
                        # against the session tree (build_tree over the
                        # accumulated tasks list).
                        self._pending_children.setdefault(task_id, []).append(msg)
                elif mtype == "reuse_ready":
                    # Eval-3 ADOPT: the worker delivered its terminal result
                    # and waits for a rebind init; keep the live process for
                    # the session pool instead of letting it exit.
                    if envelope is None or not correlated:
                        protocol_reason = "reuse_ready_without_result"
                        await self.emit(
                            "protocol", task_id=task_id, generation=generation,
                            error_type=protocol_reason,
                            note="reuse_ready before a correlated terminal result",
                        )
                        await _kill_worker(proc)
                        break
                    reuse_ready = True
                    keep_alive = True
                    await self.emit(
                        "reuse_ready", task_id=task_id, generation=generation,
                        pid=msg.get("pid"),
                    )
                    break
                elif mtype in ("exit", "exit_message"):
                    exit_reason = msg.get("reason")
                    handle.exit_reason = exit_reason
                    await self.emit(
                        "exit", task_id=task_id, reason=exit_reason, generation=generation
                    )
                    break
                elif mtype == "heartbeat":
                    last_heartbeat = loop.time()
                    handle.last_heartbeat = last_heartbeat
                    await self.emit(
                        "heartbeat", task_id=task_id, turn=msg.get("turn"),
                        tool=msg.get("tool"), status=msg.get("status"), generation=generation,
                    )
                elif mtype == "checkpoint":
                    await self.emit(
                        "checkpoint", task_id=task_id, turn=msg.get("turn"),
                        state_ref=msg.get("state_ref"), generation=generation,
                        commits_so_far=msg.get("commits_so_far"),
                    )
                elif mtype == "usage_event":
                    invalid_fields = _invalid_usage_event_fields(msg)
                    if invalid_fields:
                        await self.emit(
                            "protocol", task_id=task_id, generation=generation,
                            note="usage_event rejected: invalid field(s)",
                            fields=invalid_fields,
                        )
                    else:
                        forwarded = {
                            field: msg[field]
                            for field in _USAGE_EVENT_FORWARD_FIELDS
                            if field in msg
                        }
                        await self.emit(
                            "usage_event", task_id=task_id, generation=generation,
                            **forwarded,
                        )
                        # Admission balancing (solution C): fold the redacted
                        # usage event into the session debt ledger so later
                        # admissions in this session see updated utilization.
                        if self._debt_store is not None:
                            self._debt_store.record(msg)
                elif mtype in ("tool_event", "pong"):
                    forwarded = {"tool": msg.get("tool"), "cmd": msg.get("cmd")}
                    if mtype == "tool_event":
                        invalid_fields = _invalid_tool_event_fields(msg)
                        if invalid_fields:
                            await self.emit(
                                "protocol", task_id=task_id, generation=generation,
                                note="tool_event rejected: invalid field(s)",
                                fields=invalid_fields,
                            )
                        else:
                            for field in (
                                "batch_index", "batch_size", "ok", "duration_ms", "turn"
                            ):
                                if field in msg:
                                    forwarded[field] = msg[field]
                            await self.emit(
                                "tool_event", task_id=task_id, generation=generation,
                                **forwarded,
                            )
                    else:
                        await self.emit(
                            "log", task_id=task_id, generation=generation, **forwarded,
                        )
                elif mtype == "error":
                    await self.emit(
                        "log", task_id=task_id, generation=generation, stream="worker-error",
                        error_type=msg.get("error_type"),
                        message=str(msg.get("message", ""))[:512],
                    )
                elif mtype == "log":
                    await self.emit(
                        "log", task_id=task_id, generation=generation,
                        message=str(msg.get("message", ""))[:512],
                    )
                else:
                    await self.emit(
                        "protocol", task_id=task_id, type=mtype, note="unhandled message",
                        generation=generation,
                    )
        except asyncio.CancelledError:
            try:
                await _kill_worker(proc)
            except BaseException:
                pass
            raise
        finally:
            if not keep_alive:
                try:
                    await asyncio.wait_for(proc.wait(), WORKER_EXIT_WAIT_S)
                except BaseException:
                    try:
                        await _kill_worker(proc)
                    except BaseException:
                        pass
                    try:
                        await asyncio.wait_for(proc.wait(), WORKER_EXIT_WAIT_S)
                    except BaseException:
                        pass
            for rt in (stdout_task, stderr_task):
                if not rt.done():
                    rt.cancel()
            try:
                await asyncio.gather(stdout_task, stderr_task, return_exceptions=True)
            except BaseException:
                pass

        terminal_verdict = (
            envelope is not None
            and correlated
            and envelope.get("status") in ("succeeded", "failed", "cancelled")
        )
        if reuse_ready and not message_too_long:
            # The worker stays alive and owns no task state; the handle no
            # longer owns the process (the pool does). The generation verdict
            # is clean exactly when the terminal envelope is correlated.
            await self._pool_return(proc, cmd, env)
            handle.proc = None
            return _GenOutcome(
                clean=terminal_verdict, fatal=False, reason=None,
                exit_code=None, exit_reason=None, envelope=envelope,
                correlated=correlated, reuse_ready=True,
            )
        exit_code = proc.returncode
        handle.exit_code = exit_code
        handle.state = "EXITED"
        clean = (
            exit_reason is not None
            and terminal_verdict
            and (exit_code == 0 or envelope.get("status") != "succeeded")
        )
        if message_too_long:
            clean = False
            reason: str | None = "message_too_long"
        elif clean:
            reason: str | None = None
        elif timeout_phase is not None:
            reason = timeout_phase
        elif protocol_reason is not None:
            reason = protocol_reason
        elif exit_code != 0:
            reason = f"worker_exit_{exit_code}"
        elif exit_reason is None:
            reason = "missing_exit_message"
        elif envelope is None:
            reason = "missing_result_envelope"
        else:
            reason = "result_request_id_mismatch"
        return _GenOutcome(
            clean=clean,
            fatal=protocol_failure is not None or protocol_reason == "ready_request_id_mismatch",
            reason=protocol_failure or protocol_reason or reason, timeout_phase=timeout_phase,
            exit_code=exit_code, exit_reason=exit_reason, envelope=envelope,
            correlated=correlated,
        )

    # -- publish eligibility --------------------------------------------------

    async def _worker_success_integrity(
        self, spec: dict[str, Any], worktree: Path
    ) -> str | None:
        """Reject an unpublishable worker verdict before merging.

        Returns a failure reason when the worker's success claim is not
        backed by a clean, attached worktree: a detached HEAD means the
        worker's commits may be lost, and tracked/untracked modifications
        mean the merge would capture state the worker never claimed. The
        supervisor-owned ``.cambium`` fence directory is exempt.
        """
        worktree = Path(worktree)
        symbolic = await self._git_stdout(
            worktree, "symbolic-ref", "--quiet", "HEAD", check=False
        )
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
        if any(
            not _status_line_is_fence(line)
            for line in status.stdout.splitlines()
        ):
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
                    kind, task_id=event_task_id, _observer_failure_is_fatal=False,
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
                    f"durable terminal event {kind!r} not persisted within "
                    f"{timeout_s}s"
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
                    task_id=task_id, status="succeeded", exit_code=0,
                    reason=None, merge_sha=payload.get("new"),
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
                seq.reconcile, repo, throwaway,
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
                    event for event in reversed(events)
                    if event["kind"] in ("merge_committed", "merge_reconciled")
                    and event["payload"].get("new") == current
                    and event.get("task_id") == task_id
                ),
                None,
            )
            if terminal is not None:
                self._results[task_id] = TaskResult(
                    task_id=task_id, status="succeeded", exit_code=0,
                    reason=None, merge_sha=current,
                )
                continue
            refs = await self._git_stdout(
                repo, "for-each-ref", "--format=%(refname:strip=3) %(objectname)",
                "refs/cambium/staging", check=False,
            ) or ""
            owner: str | None = None
            for line in refs.splitlines():
                suffix, _, tip = line.partition(" ")
                key = suffix.split("-", 1)[0]
                if tip == current and key in task_keys:
                    owner = task_keys[key]
                    break
            if owner is not None:
                await self.emit(
                    "merge_reconciled", task_id=owner, new=current, repo=str(repo),
                    reason="ref-advanced-before-event",
                )
                self._results[owner] = TaskResult(
                    task_id=owner, status="succeeded", exit_code=0,
                    reason=None, merge_sha=current,
                )

    async def _merge_task(self, spec: dict[str, Any], handle: WorkerHandle) -> str | None:
        """Stage and atomically publish the worker branch onto refs/heads/main.

        On NonFastForwardError/MergeConflictError a merge_failed event is
        appended and None is returned. The v2.1 resolver sub-task is out of
        scope for this version (documented; event only).
        """
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
        try:
            async with self._merge_lock:  # Unio single-writer: serialized merges
                current_main = await self._git_stdout(
                    repo, "rev-parse", "refs/heads/main", check=False
                )
                if not current_main:
                    raise RuntimeError("no refs/heads/main to publish onto")
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
                if hasattr(seq, "ensure_staging_clean"):
                    await asyncio.to_thread(seq.ensure_staging_clean, repo)
                    await self._flush_sequencer_events(seq, deferred_observers=deferred)
                await asyncio.to_thread(seq.publish_merge, repo, staging_tip, current_main)
                ref_published = True
                await self.emit(
                    "merge_committed", task_id=task_id, old=current_main, new=staging_tip,
                    repo=str(repo), branch=branch, generation=handle.generation,
                    _deferred_observers=deferred,
                )
                committed_persisted = True
        except Exception as exc:
            merge_failed = True
            error_type = exc.__class__.__name__
            if error_type in ("NonFastForwardError", "MergeConflictError"):
                await self.emit(
                    "merge_failed", task_id=task_id, merge_error=error_type,
                    message=str(exc)[:512], generation=handle.generation,
                )
            else:
                await self.emit(
                    "merge_failed", task_id=task_id, merge_error=error_type,
                    message=str(exc)[:512], generation=handle.generation, internal=True,
                )
        finally:
            try:
                if hasattr(seq, "cleanup_staging") and not (
                    ref_published and not committed_persisted
                ):
                    await asyncio.to_thread(seq.cleanup_staging, repo)
            except Exception as exc:
                cleanup_failed = True
                emitted = await self._flush_sequencer_events(
                    seq, deferred_observers=deferred
                )
                if committed_persisted and "merge_staging_cleanup_failed" not in emitted:
                    await self.emit(
                        "merge_staging_cleanup_failed", task_id=task_id,
                        staging_sha=staging_tip, reason=exc.__class__.__name__,
                    )
            else:
                await self._flush_sequencer_events(seq, deferred_observers=deferred)
        try:
            await self._notify_deferred_observers(deferred)
        except Exception as exc:
            await self.emit(
                "merge_failed", task_id=task_id, merge_error=exc.__class__.__name__,
                message=str(exc)[:512], generation=handle.generation, internal=True,
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
    """Atomically persist the accepted plan as ``<session_dir>/plan.json``.

    Mirrors the ``cambium.results.write_result`` JSON conventions:
    ``mkstemp`` in the target directory, compact ``ensure_ascii=False`` /
    ``allow_nan=False`` JSON with a trailing newline, fsync, then
    ``os.replace`` plus a directory fsync. The caller holds the session
    lock, so this is the canonical pre-worker boundary artifact.
    """
    target = Path(session_dir) / "plan.json"
    fd, temporary_name = tempfile.mkstemp(
        prefix=f".{target.name}.", suffix=".tmp", dir=Path(session_dir)
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as stream:
            json.dump(
                plan,
                stream,
                ensure_ascii=False,
                allow_nan=False,
                separators=(",", ":"),
            )
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, target)
        os.chmod(target, 0o600)
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
    if isinstance(plan, (list, tuple)):
        return list(plan)
    raise ValueError("plan must be a dict with 'tasks' or a list of task specs")


def _validate_plan_task(session_dir: Path, task: dict[str, Any]) -> dict[str, Any]:
    """Path safety and required-field checks for one plan task."""
    session_root = Path(session_dir).resolve()
    if not isinstance(task, dict):
        raise ValueError("plan task must be an object")
    spec = dict(task)
    task_id = spec.get("task_id")
    if not isinstance(task_id, str) or not task_id:
        raise ValueError("plan task requires 'task_id'")
    task = spec.get("task")
    if not isinstance(task, str) or not task.strip():
        raise ValueError(f"task {task_id} requires a non-empty 'task'")
    if "repo" not in spec:
        raise ValueError(f"task {task_id} requires 'repo'")
    if "worktree_path" not in spec:
        raise ValueError(f"task {task_id} requires 'worktree_path'")
    if "branch" not in spec:
        raise ValueError(f"task {task_id} requires 'branch'")
    worktree = Path(spec["worktree_path"]).resolve()
    if not worktree.is_relative_to(session_root):
        raise ValueError(
            f"worktree_path {worktree} is outside the session dir {session_root}"
        )
    spec["repo"] = str(Path(spec["repo"]).resolve())
    spec["worktree_path"] = str(worktree)
    if Path(spec["repo"]).resolve() == worktree:
        raise ValueError(
            f"task {task_id}: worktree_path must not be the repo itself ({worktree})"
        )
    provider_env_keys = spec.get("provider_env_keys", ())
    if isinstance(provider_env_keys, (str, bytes)):
        raise ValueError(f"task {task_id} provider_env_keys must be a list of names")
    if not isinstance(provider_env_keys, (list, tuple)):
        raise ValueError(f"task {task_id} provider_env_keys must be a list of names")
    spec["provider_env_keys"] = list(provider_env_keys)
    model_candidates = spec.get("model_candidates")
    if model_candidates is not None:
        if not isinstance(model_candidates, (list, tuple)) or not model_candidates or not all(
            isinstance(model, str) and bool(model.strip()) for model in model_candidates
        ):
            raise ValueError(
                f"task {task_id} model_candidates must be a non-empty list of model ids"
            )
        spec["model_candidates"] = list(model_candidates)
    requirements = spec.get("requirements")
    if requirements is not None:
        try:
            requirements = validate_requirements(requirements)
        except ValueError as exc:
            raise ValueError(f"task {task_id}: {exc}") from exc
        if requirements:
            spec["requirements"] = requirements
    spec.setdefault("base_commit", None)
    spec.setdefault("write_marker", True)
    return spec


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


def _assigned_tier(
    providers: Sequence[Any], provider_name: str, pinned_tier: str | None
) -> str:
    """The call tier for an assigned provider: the provider's own tier (or the
    caller-pinned tier when the provider matched it). The worker routes calls
    by tier, so the assignment and the call tier must agree."""
    if isinstance(pinned_tier, str) and pinned_tier:
        return pinned_tier
    for provider in providers:
        if provider.name == provider_name:
            return provider.tier.value
    raise ValueError(f"assigned provider {provider_name!r} not found among candidates")


def _resolve_model_candidates(
    spec: dict[str, Any], debt: Mapping[str, Any], lanes: dict[str, LaneState]
) -> bool:
    """Resolve a task's (provider, model) when it declares ``model_candidates``
    and its fanout_config carries no pinned model.

    The pick comes from the usage-debt ledger against the same provider config
    the worker will load, before the model filter partitions the pool (solution
    C), and skips providers whose lane is at or above its effective in-flight
    cap (H1). When the task declares ``requirements``, the pick is the lowest
    ``routing.score_providers`` score among providers that satisfy the task's
    capability constraints (H2, STRICT filter); otherwise it is the max-min
    ``routing.select_lane`` pick as before. Mutates ``spec`` in place: writes
    the chosen model into ``fanout_config`` and records the chosen provider as
    ``assigned_provider``. Returns True when an assignment was written; pinned
    tasks (``fanout_config.model`` set) and tasks without a fanout_config are
    left untouched and return False.
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
    authorized_provider_keys = _provider_env_keys(spec)
    if authorized_provider_keys:
        # Auto-mode plans carry only the provider environment names whose
        # credentials were loaded for this run.  Model ids are not unique
        # provider identities, so keep the routing, lane, and worker
        # assignment pool on that same authorized set.
        providers = [
            provider
            for provider in providers
            if provider.api_key_env in authorized_provider_keys
        ]
    # A caller-pinned tier is a hard constraint: only providers in that tier
    # may serve the task, so the assignment can never contradict it.
    pinned_tier = fanout_config.get("tier")
    if isinstance(pinned_tier, str) and pinned_tier:
        providers = [p for p in providers if p.tier.value == pinned_tier]
    _ensure_lanes(lanes, providers)
    requirements = spec.get("requirements")
    if requirements:
        # H2: capability/quality-constrained scoring — pick the lowest score
        # among providers that satisfy the task's requirements (STRICT filter,
        # fail-closed); never fall back to a provider that fails them.
        provider_name, model, _score = score_providers(
            providers, candidates, debt, lanes, requirements=requirements
        )[0]
    else:
        provider_name, model = select_lane(providers, candidates, debt, lanes)
    # The (provider, model, tier) assignment is one atomic unit: the worker
    # routes calls by tier, so the assigned provider's tier must be the call
    # tier or the assignment is filtered out before any request is sent.
    spec["fanout_config"] = {
        **fanout_config,
        "model": model,
        "tier": _assigned_tier(providers, provider_name, pinned_tier),
    }
    spec["assigned_provider"] = provider_name
    return True


def _preassign_lanes(
    specs: Sequence[dict[str, Any]],
    debt: Mapping[str, ProviderDebt] | None,
    lanes: dict[str, LaneState],
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
        if not _resolve_model_candidates(spec, batch_debt, lanes):
            continue
        provider_name = spec["assigned_provider"]
        lanes[provider_name].in_flight += 1
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
    assigned = spec.get("assigned_provider")
    if not isinstance(assigned, str):
        return
    lane = lanes.get(assigned)
    if lane is not None and lane.in_flight > 0:
        lane.in_flight -= 1


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
    child_spec = dict(raw)
    child_spec["task_id"] = proposal["child_task_id"]
    child_spec["kind"] = proposal["kind"]
    child_spec["parent_task_id"] = parent_spec["task_id"]
    # Children inherit, never exceed, the parent's environment access: a
    # worker must not widen provider_env_keys beyond what its parent (or the
    # plan) already had.
    parent_keys = set(parent_spec.get("provider_env_keys") or ())
    child_keys = set(child_spec.get("provider_env_keys") or ())
    child_spec["provider_env_keys"] = sorted(child_keys & parent_keys)
    child_spec["parent_envelope"] = parent_envelope
    return _validate_plan_task(session_dir, child_spec)


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
    if envelope is not None:
        commits = envelope.get("commits", ())
        files_changed = envelope.get("files_changed", ())
        unified_diff = envelope.get("diff", "")
        diff_truncated = envelope.get("diff_truncated", False)
        summary = envelope.get("summary", "")
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


def _resolve_width(
    max_width: int | None, plan: dict[str, Any] | list[dict[str, Any]]
) -> int:
    """Resolve the per-wave dispatch width: parameter, then plan field, then default."""
    if isinstance(max_width, int) and not isinstance(max_width, bool) and max_width > 0:
        return max_width
    if isinstance(plan, dict):
        field = plan.get("max_width")
        if (
            isinstance(field, int)
            and not isinstance(field, bool)
            and field > 0
        ):
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
    runtime: _Runtime, specs: list[dict[str, Any]], width_limit: int
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

    succeeded = {
        tid for tid, result in runtime._results.items() if result.status == "succeeded"
    }
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
            node
            for node in ready_tasks(tree, succeeded)
            if node.task_id not in terminal
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
    max_concurrent_tasks: int | None = None,
    routing_state_path: str | Path | None = None,
    reject_reused_session: bool = False,
    oauth_store: OAuthStore | None = None,
    architectus: Any = None,
    conversations: bool | None = None,
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
    revision. Both default off: the historical behavior is byte-for-byte
    unchanged.

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
      one per CPU; pass ``0`` for unlimited concurrency. The cap covers the
      worker phase only (spawn through worker exit), never merge, prune, or
      observer notification.
    """
    session_dir = Path(session_dir)
    tasks = _plan_tasks(plan)
    _reject_duplicate_task_ids(tasks)
    specs = [_validate_plan_task(session_dir, t) for t in tasks]
    if not specs:
        raise ValueError("plan contains no tasks")
    if max_concurrent_tasks is not None and (
        type(max_concurrent_tasks) is not int or max_concurrent_tasks < 0
    ):
        raise ValueError("max_concurrent_tasks must be a non-negative int or None")
    if max_concurrent_tasks is None:
        max_concurrent_tasks = max(1, os.process_cpu_count() or os.cpu_count() or 1)

    _validate_provider_environment(specs, provider_environment, oauth_store=oauth_store)
    tree, width_limit = _resolve_hierarchy(specs, plan, max_width)

    admission = _SessionAdmission(session_dir)
    admission.acquire()
    try:
        if reject_reused_session:
            _reject_reused_session(session_dir)
        await asyncio.to_thread(_write_plan, session_dir, {"tasks": specs})
        started_at = time.time()
        redactor = _session_redactor(
            specs, provider_environment, oauth_store=oauth_store
        )
        store = EventStore(session_dir / ".cambium" / "events.db", redactor=redactor)
        # Usage-debt ledger for admission balancing (solution C): load the
        # persisted state once, feed it live from usage_event rows, and
        # persist again when the session ends. The path is injected so the
        # caller owns where routing evidence lives (oneshot defaults it to a
        # repo-scoped file; tests pass scratch paths).
        debt_store = DebtStore(routing_state_path)
        debt_store.load()
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
        )
        await runtime.start()
        runtime.set_session_tasks(specs)
        cancelled = False
        try:
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
                )
                async with asyncio.TaskGroup() as tg:
                    # Dynamic child admission spawns into the active group;
                    # the flat fan-out path owns this group for the session.
                    runtime._task_group = tg
                    for spec in specs:
                        tg.create_task(runtime.supervise_task(spec))
        except asyncio.CancelledError:
            cancelled = True
        except BaseExceptionGroup as exc_group:
            await runtime.emit(
                "log", task_id=None, message=f"task group exception: {exc_group}"
            )
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
                            "log", task_id=None,
                            message=f"routing-state save failed: {exc}",
                        )
            finally:
                await runtime.shutdown(
                    session_status="cancelled" if cancelled else "ended"
                )
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
    """Built-in CLI demo: one cambium.worker task against a seeded repo."""
    return {
        "task_id": "demo-001",
        "worker": "cambium.worker",
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
    session_dir: Path, plan: dict[str, Any], *, conversations: bool = False
) -> int:
    loop = asyncio.get_running_loop()

    def print_event(record: dict[str, Any]) -> None:
        print(f'{record["kind"]:>16}  {json.dumps(record["payload"])}', flush=True)

    task = asyncio.ensure_future(
        run_plan(session_dir, plan, on_event=print_event, conversations=conversations)
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
    parser = argparse.ArgumentParser(description="Cambium supervisor")
    parser.add_argument("--session-dir", required=True)
    parser.add_argument(
        "--plan",
        help="path to plan JSON {\"tasks\": [{\"task_id\", \"task\", \"repo\", "
        "\"worktree_path\", \"branch\", \"base_commit\", ...}]} "
        "(multi-worker mode)",
    )
    parser.add_argument(
        "--task-spec",
        help=(
            "path to task spec JSON (one-task mode; default: <session-dir>/task.json, "
            "else the built-in demo)"
        ),
    )
    parser.add_argument(
        "--conversations",
        action="store_true",
        help="persist child-revision conversations at "
        "<session-dir>/.cambium/conversations.db for the session",
    )
    args = parser.parse_args(argv)
    session_dir = Path(args.session_dir)
    if args.plan:
        plan = json.loads(Path(args.plan).read_text())
        tasks = _plan_tasks(plan)
        _reject_duplicate_task_ids(tasks)
        for task in tasks:
            _ensure_repo_initialized(Path(task["repo"]).resolve())
        try:
            return asyncio.run(_amain_plan(session_dir, plan, conversations=args.conversations))
        except KeyboardInterrupt:
            return 130
    if args.task_spec:
        task_spec = json.loads(Path(args.task_spec).read_text())
    elif (session_dir / "task.json").exists():
        task_spec = json.loads((session_dir / "task.json").read_text())
    else:
        task_spec = _builtin_demo_spec(session_dir)
        _bootstrap_demo_repo(Path(task_spec["repo"]), task_spec["target_file"])
    plan_task = _slice_to_plan_task(task_spec)
    _ensure_repo_initialized(Path(plan_task["repo"]))
    try:
        return asyncio.run(
            _amain_plan(session_dir, {"tasks": [plan_task]}, conversations=args.conversations)
        )
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
