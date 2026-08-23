"""Worker runtime (Opifex seed) — ``python -m cambium.worker``.

Speaks the Nuntius JSON-Lines wire protocol over stdio
(`docs/architecture/architecture.md` §5). By default
one worker executes one task and then exits; when the init carries
``worker_reuse: true`` the worker stays alive after the task and waits for a
rebind init on stdin (eval-3 ADOPT warm pool):

    init                        ->  ready (echoes the init request_id and the
                                    generation fencing token)
    run_task                    ->  heartbeat(s) every ~1s while working
                                ->  result_envelope (echoes the run_task
                                    request_id) -> exit_message (connection
                                    level; carries NO request_id)
                                ->  with ``worker_reuse``: result_envelope
                                    then reuse_ready (keeps the process alive)
    init (rebind, reuse only)   ->  clears ALL per-task state (agent loop,
                                    transcript, tool state, LM clients), chdir
                                    to the new worktree, then ready; the
                                    single-init exit behavior is unchanged
                                    when ``worker_reuse`` is absent
    check_health                ->  ok (echoes the request_id, generation)
    steer                       ->  {"action": "cancel"} aborts the current
                                    task (status cancelled); anything else is
                                    logged and ignored (v2.1 hook)
    propose_child (outbound)    ->  after the task body, one message per
                                    entry in the run payload's
                                    ``proposed_children`` list, or during the
                                    agent loop, one message per successful
                                    ``delegate`` tool call; both shapes are
                                    {request_id, parent_task_id,
                                    child_task_id, kind, spec}. The
                                    supervisor validates the revision and
                                    replies with durable ``child_admitted`` /
                                    ``child_rejected`` events (no wire ack)
    cancel                      ->  ok (ack) then abort the current task with
                                    status "cancelled"
    shutdown                    ->  ok (ack), abort the current task, then
                                    exit_message (reason "shutdown") + exit 0

A reuse-enabled worker exits cleanly (code 0) when stdin closes while it is
idle between tasks.

Defensive timeouts (worker self-protection if the supervisor dies):
    - init deadline: no init message within ``INIT_TIMEOUT_S`` (default 30 s,
      env ``CAMBIUM_INIT_TIMEOUT_S``) -> ``fatal_error`` + exit 1.
    - idle deadline: no message from the supervisor within ``IDLE_TIMEOUT_S``
      (default 300 s, env ``CAMBIUM_IDLE_TIMEOUT_S``) after ``ready`` -> the
      worker aborts any current task and exits gracefully (``exit_message``
      reason "idle", exit 0). No ``result_envelope`` is emitted for the
      aborted task — the supervisor is presumed gone.

Task spec (the ``run_task`` body) is compatible with
``scripts/fake_worker.py``'s task spec:

    task_id         stable task id (echoed everywhere)
    scratch_repo    git repo the throwaway worktree is branched from
    worktree_path   where the throwaway worktree is created (must stay under
                    the scratch repo's parent — path safety)
    branch          name of the throwaway branch
    target_file     file inside the worktree to edit (deterministic fallback;
                    must not escape it)
    marker          line appended to the target file (deterministic fallback)
    write_marker    bool; false forces the task to fail
    work_delay_s    optional float; pause before the edit (test hook so
                    cancellation is observable)

When ``init.fanout_config`` is present, the worker runs the provider-backed
agent loop instead: it loads the provider file named by the worker's absolute
``CAMBIUM_PROVIDERS`` environment variable and iterates bounded
``Diffundo.call`` turns, each accepting exactly one JSON action:

    {"type": "plan", "steps": [<non-empty strings>]}
    {"type": "tool_call", "name": <schema name>, "arguments": {...}}
    {"type": "finish", "summary": <non-empty summary>}

The agent is instructed to emit a short ``plan`` action before any
``tool_call``; the plan is kept in the transcript. The transcript is
summarized (truncation plus a synthetic dropped-message marker, no LLM call)
when it exceeds a character budget, so it stays bounded across turns.

Tool calls execute inside the worktree (with shell/git permissions from
``init.permissions``), emit ``tool_event`` messages, and persist
``checkpoint`` state under ``$CAMBIUM_SESSION_ID/.cambium/checkpoints/``.
Every router call also emits one redacted ``usage_event`` (implementation
plan step 3): provider/model/turn, token fields, estimated cost, latency,
Retry-After, request-rate status, account-quota owner, stable prompt-prefix
bytes, provider-reported cache-hit, and failure reason; fields the provider
did not report are omitted, never an error. Every tool result, including lint
feedback from ``write_file``, is appended to the transcript so the agent sees
success or failure.
The worker owns at most one fenced commit of the resulting changes; a
successful provider loop that leaves no non-``.cambium`` changes owns none.
A true no-op succeeds only while the worktree HEAD still resolves to the
base commit and writes no final transcript checkpoint; an advanced HEAD is
reported as a failure so no unfenced commit is ever merged.

Malformed wire input is fatal: the worker emits ``fatal_error``, then
``exit_message`` (reason "fatal"), and exits nonzero (let-it-crash). The
process exit code is 0 when the worker delivered a terminal result envelope
(the task outcome lives in the envelope ``status``) or when the exit is a
graceful supervisor- or worker-initiated close (shutdown, idle).
"""

from __future__ import annotations

import asyncio
import copy
import hashlib
import json
import logging
import math
import os
import re
import secrets
import stat
import subprocess
import sys
import tempfile
import threading
import time
import zlib
from collections.abc import Callable, Mapping
from dataclasses import asdict, dataclass, replace
from enum import IntEnum, StrEnum
from pathlib import Path
from typing import Any, TypeGuard, cast

from cambium.auth import oauth_env_suffix, scrub_environment
from cambium.diffundo import (
    AllProvidersFailed,
    CallResult,
    CredentialSource,
    Diffundo,
    ProviderError,
    ProviderTier,
    prompt_prefix_bytes,
    validate_prompt_structure,
)
from cambium.fencing import is_cache_artifact_path, validate_worker_generation
from cambium.ipc import (
    MAX_LINE_BYTES,
    MessageTooLong,
    make_request_id,
    read_message,
    write_message,
)
from cambium.lint_diag import LintDiag
from cambium.provider_config import AuthMode, load_providers
from cambium.redact import Redactor, build_session_redactor
from cambium.schemas import TOOL_SCHEMAS
from cambium.summary_trunk import (
    SUMMARY_PROTOCOL_LINES,
    SummaryTrunkError,
    append_summary_entry,
    build_summary_request,
    parse_summary_response,
    partition_summary_trunk,
    semantic_summary_messages,
    summary_entries,
)
from cambium.tools import ToolContext, ToolPermissionPolicy, ToolResult, run_tool

PROTO = 1
HEARTBEAT_INTERVAL_S = 1.0
INIT_TIMEOUT_S = 30.0
IDLE_TIMEOUT_S = 300.0
MAX_SUMMARY_CHARS = 2_000
# Consecutive non-progress actions (valid plans AND invalid/unparseable
# actions) before the agent loop fails fast.
MAX_CONSECUTIVE_PLANS = 2
MAX_DIFF_BYTES = 64 * 1024  # 64 KiB bounded upward diff envelope.
DEFAULT_MAX_TURNS = 50
DEFAULT_MAX_TOKENS = 200_000
DEFAULT_MAX_WALL_S = 3600.0
CHECKPOINT_SCHEMA = 1
MAX_ACTION_CONTENT_BYTES = 16 * 1024
MAX_OBSERVATION_BYTES = 64 * 1024
MAX_CMD_BYTES = 512
MAX_TRANSCRIPT_CHARS = 120_000
TRANSCRIPT_KEEP_TURNS = 6
MAX_ENVELOPE_FIELD_CHARS = 2_000
MAX_ENVELOPE_ITEMS = 16
MAX_CONTEXT_MESSAGES = 512
CHECKPOINT_EPOCH_SCHEMA = 4


class TaskStatus(StrEnum):
    """Terminal task outcome reported in the result envelope ``status`` field."""

    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"
    SUSPENDED = "suspended"


class WorkerExitCode(IntEnum):
    """Worker process exit codes for the result envelope and ``exit_message``.

    The worker reports ``status`` as the domain verdict; ``exit_code`` is the
    numeric encoding the supervisor reads off the wire. ``SUCCEEDED`` is 0 and
    ``FAILED`` is 1; ``CANCELLED`` is 4 and ``SUSPENDED`` is 3 (distinct
    non-success codes so the supervisor never misreads a suspended or
    cancelled task as a plain failure).
    """

    SUCCEEDED = 0
    FAILED = 1
    SUSPENDED = 3
    CANCELLED = 4


_EXIT_CODE_BY_STATUS: Mapping[str, int] = {
    TaskStatus.SUCCEEDED.value: WorkerExitCode.SUCCEEDED,
    TaskStatus.FAILED.value: WorkerExitCode.FAILED,
    TaskStatus.SUSPENDED.value: WorkerExitCode.SUSPENDED,
    TaskStatus.CANCELLED.value: WorkerExitCode.CANCELLED,
}


def _exit_code_for(status: str) -> int:
    """Resolve one envelope status to its numeric exit code (fail-closed)."""
    code = _EXIT_CODE_BY_STATUS.get(status)
    return code if code is not None else WorkerExitCode.FAILED


@dataclass(frozen=True, slots=True)
class CacheKeyDescriptor:
    """Compatibility contract for forking one epoch checkpoint (plan §5.2).

    ``provider`` is the served provider of the turn that cut the epoch; the
    model is the configured slug, never a response slug. ``prefix_sha256``
    hashes the exact provider-sent message list at the boundary;
    ``suffix_sha256`` hashes the post-response continuation kept separately;
    and ``full_sha256`` hashes their concatenation. A fork appends another
    user message, so its full hash is different from this checkpoint hash.
    ``redacted`` records whether the session redactor altered any byte of the
    persisted checkpoint; a redacted checkpoint is forkable for context
    continuity but never byte-guaranteed for provider-cache reuse.
    """

    provider: str | None
    model: str
    protocol: str
    reasoning_effort: str | None
    system_sha256: str
    tools_sha256: str
    prefix_sha256: str
    suffix_sha256: str
    full_sha256: str
    prefix_bytes: int
    message_count: int
    redacted: bool
    provider_boundary: dict[str, Any]


@dataclass(frozen=True, slots=True)
class ContextCheckpoint:
    """Immutable content-addressed epoch checkpoint (plan §5.2).

    Holds the exact message list the parent sent at a safe provider-turn
    boundary and a separate post-response continuation suffix. Fork and resume
    builders append that suffix before their child/result envelope.
    """

    schema: int
    task_id: str
    generation: int
    epoch: int
    turn: int
    created_at: float
    cache_key: CacheKeyDescriptor
    provider_messages: list[dict[str, Any]]
    continuation_suffix: list[dict[str, Any]]
    checkpoint_ref: str
    code_changed: bool
    verified_after_change: bool
    verification_failed: bool
    no_progress_actions: int
    budget_new_tokens: int
    previous_prompt_tokens: int
    cumulative_usage: dict[str, int]
    wall_deadline: float

    @property
    def system_message(self) -> dict[str, Any]:
        """First-pass compatibility view of the provider message list."""
        return self.provider_messages[0]

    @property
    def transcript(self) -> list[dict[str, Any]]:
        """First-pass compatibility view without the continuation suffix."""
        return self.provider_messages[1:]

    @property
    def full_messages(self) -> list[dict[str, Any]]:
        """The immutable checkpoint context before a fork/resume envelope."""
        return [*self.provider_messages, *self.continuation_suffix]


_FORK_DESCRIPTOR_KEYS = frozenset(
    {
        "checkpoint_ref",
        "provider",
        "model",
        "system_sha256",
        "tools_sha256",
        "prefix_sha256",
        "suffix_sha256",
        "full_sha256",
        "prefix_bytes",
        "provider_boundary",
    }
)
_RESUME_KEYS = frozenset(
    {
        "checkpoint_ref",
        "epoch",
        "child_results",
        "child_results_truncated",
    }
)
_SHA256_HEX_RE = re.compile(r"[0-9a-f]{64}\Z")
_PROVIDER_BOUNDARY_KEYS = frozenset(
    {
        "provider",
        "endpoint",
        "authmode",
        "api_key_env",
        "provider_env_keys",
        "authorized_providers",
        "authorized_providers_explicit",
        "protocol",
        "model",
        "tier",
        "reasoning_effort",
        "provider_config_path",
    }
)


def _validate_provider_boundary(value: Any) -> dict[str, Any]:
    """Validate the non-secret provider boundary carried by a context fork."""
    if not isinstance(value, dict):
        raise ContextForkError("provider_boundary must be an object")
    if set(value) != _PROVIDER_BOUNDARY_KEYS:
        missing = sorted(_PROVIDER_BOUNDARY_KEYS - set(value))
        unknown = sorted(set(value) - _PROVIDER_BOUNDARY_KEYS)
        details: list[str] = []
        if missing:
            details.append(f"missing keys: {missing}")
        if unknown:
            details.append(f"unknown keys: {unknown}")
        suffix = f" ({'; '.join(details)})" if details else ""
        raise ContextForkError("provider_boundary has an invalid key set" + suffix)
    required_strings = (
        "provider",
        "endpoint",
        "authmode",
        "protocol",
        "model",
        "tier",
        "provider_config_path",
    )
    for key in required_strings:
        item = value.get(key)
        if not isinstance(item, str) or not item:
            raise ContextForkError(f"provider_boundary {key!r} must be a non-empty string")
    api_key_env = value.get("api_key_env")
    if not isinstance(api_key_env, str):
        raise ContextForkError("provider_boundary 'api_key_env' must be a string")
    env_keys = value.get("provider_env_keys")
    if (
        not isinstance(env_keys, list)
        or not all(isinstance(item, str) and item for item in env_keys)
        or len(set(env_keys)) != len(env_keys)
    ):
        raise ContextForkError("provider_boundary 'provider_env_keys' must be unique strings")
    authorized = value.get("authorized_providers")
    if authorized is not None and (
        not isinstance(authorized, list)
        or not all(isinstance(item, str) and item for item in authorized)
        or len(set(authorized)) != len(authorized)
        or len(authorized) > MAX_ENVELOPE_ITEMS
    ):
        raise ContextForkError(
            "provider_boundary 'authorized_providers' must be a string list or null"
        )
    explicit = value.get("authorized_providers_explicit")
    if type(explicit) is not bool:
        raise ContextForkError(
            "provider_boundary 'authorized_providers_explicit' must be a boolean"
        )
    reasoning = value.get("reasoning_effort")
    if reasoning is not None and (not isinstance(reasoning, str) or not reasoning):
        raise ContextForkError("provider_boundary 'reasoning_effort' must be a string or null")
    return {
        "provider": value["provider"],
        "endpoint": value["endpoint"],
        "authmode": value["authmode"],
        "api_key_env": api_key_env,
        "provider_env_keys": list(env_keys),
        "authorized_providers": None if authorized is None else list(authorized),
        "authorized_providers_explicit": explicit,
        "protocol": value["protocol"],
        "model": value["model"],
        "tier": value["tier"],
        "reasoning_effort": reasoning,
        "provider_config_path": value["provider_config_path"],
    }


class ContextForkError(ValueError):
    """A context fork or resume could not be constructed safely."""


def _validate_context_fork(value: Any) -> dict[str, Any] | None:
    """Strictly validate the init ``context_fork`` descriptor, or return None.

    Exactly the context-fork key set is accepted; any unknown key or malformed
    value is fatal (mirrors ``_validate_parent_envelope`` posture). A child
    that receives an invalid descriptor must never guess at a prefix.
    """
    if value is None:
        return None
    if not isinstance(value, dict):
        raise ContextForkError("context_fork must be an object")
    if set(value) != _FORK_DESCRIPTOR_KEYS:
        missing = sorted(_FORK_DESCRIPTOR_KEYS - set(value))
        unknown = sorted(set(value) - _FORK_DESCRIPTOR_KEYS)
        details: list[str] = []
        if missing:
            details.append(f"missing keys: {missing}")
        if unknown:
            details.append(f"unknown keys: {unknown}")
        raise ContextForkError(
            "context_fork has an invalid key set" + (f" ({'; '.join(details)})" if details else "")
        )
    checkpoint_ref = value.get("checkpoint_ref")
    if not isinstance(checkpoint_ref, str) or not checkpoint_ref:
        raise ContextForkError("context_fork 'checkpoint_ref' must be a non-empty string")
    _validate_checkpoint_ref_shape(checkpoint_ref)
    provider = value.get("provider")
    if provider is not None and not (isinstance(provider, str) and provider):
        raise ContextForkError("context_fork 'provider' must be a string or null")
    model = value.get("model")
    if not isinstance(model, str) or not model:
        raise ContextForkError("context_fork 'model' must be a non-empty string")
    for key in (
        "system_sha256",
        "tools_sha256",
        "prefix_sha256",
        "suffix_sha256",
        "full_sha256",
    ):
        digest = value.get(key)
        if not isinstance(digest, str) or _SHA256_HEX_RE.match(digest) is None:
            raise ContextForkError(f"context_fork {key!r} must be a sha256 hex digest")
    prefix_bytes = value.get("prefix_bytes")
    if isinstance(prefix_bytes, bool) or not isinstance(prefix_bytes, int) or prefix_bytes < 0:
        raise ContextForkError("context_fork 'prefix_bytes' must be a non-negative integer")
    boundary = _validate_provider_boundary(value.get("provider_boundary"))
    return {
        "checkpoint_ref": checkpoint_ref,
        "provider": provider,
        "model": model,
        "system_sha256": value["system_sha256"],
        "tools_sha256": value["tools_sha256"],
        "prefix_sha256": value["prefix_sha256"],
        "suffix_sha256": value["suffix_sha256"],
        "full_sha256": value["full_sha256"],
        "prefix_bytes": prefix_bytes,
        "provider_boundary": boundary,
    }


def _validate_summary_trunk_ref(value: Any) -> str | None:
    """Validate a cold-provider semantic summary checkpoint reference."""
    if value is None:
        return None
    if not isinstance(value, str) or not value:
        raise ContextForkError("summary_trunk_ref must be a non-empty string")
    _validate_checkpoint_ref_shape(value)
    return value


def _validate_resume(value: Any) -> dict[str, Any] | None:
    """Strictly validate the run_task ``resume`` payload, or return None.

    Exactly the plan §9 key set is accepted. Each child envelope is validated
    as a strict parent envelope and the list is bounded by
    ``MAX_ENVELOPE_ITEMS``; anything else is a supervisor/corruption defect
    and fails closed.
    """
    if value is None:
        return None
    if not isinstance(value, dict):
        raise ContextForkError("resume must be an object")
    if set(value) != _RESUME_KEYS:
        missing = sorted(_RESUME_KEYS - set(value))
        unknown = sorted(set(value) - _RESUME_KEYS)
        details: list[str] = []
        if missing:
            details.append(f"missing keys: {missing}")
        if unknown:
            details.append(f"unknown keys: {unknown}")
        raise ContextForkError(
            "resume has an invalid key set" + (f" ({'; '.join(details)})" if details else "")
        )
    checkpoint_ref = value.get("checkpoint_ref")
    if not isinstance(checkpoint_ref, str) or not checkpoint_ref:
        raise ContextForkError("resume 'checkpoint_ref' must be a non-empty string")
    _validate_checkpoint_ref_shape(checkpoint_ref)
    epoch = value.get("epoch")
    if isinstance(epoch, bool) or not isinstance(epoch, int) or epoch <= 0:
        raise ContextForkError("resume 'epoch' must be a positive integer")
    child_results = value.get("child_results")
    if not isinstance(child_results, list):
        raise ContextForkError("resume 'child_results' must be a list")
    if len(child_results) > MAX_ENVELOPE_ITEMS:
        raise ContextForkError("resume 'child_results' exceeds the item cap")
    validated_results: list[dict[str, Any]] = []
    for index, result in enumerate(child_results):
        try:
            validated = _validate_parent_envelope(result)
        except ParentEnvelopeError as exc:
            raise ContextForkError(
                f"resume 'child_results[{index}]' is not a strict envelope: {exc}"
            ) from exc
        if validated is None:
            raise ContextForkError(f"resume 'child_results[{index}]' must be a strict envelope")
        validated_results.append(validated)
    truncated = value.get("child_results_truncated")
    if type(truncated) is not bool:
        raise ContextForkError("resume 'child_results_truncated' must be a boolean")
    return {
        "checkpoint_ref": checkpoint_ref,
        "epoch": epoch,
        "child_results": validated_results,
        "child_results_truncated": truncated,
    }


INSPECTION_GIT_OPS = frozenset({"status", "diff", "log"})
_USAGE_COUNT_FIELDS = frozenset(
    {
        "prompt_tokens",
        "completion_tokens",
        "total_tokens",
        "input_tokens",
        "output_tokens",
        "cached_tokens",
    }
)
_DIFFUNDO_OPTIONS = frozenset(
    {
        "call_budget_s",
        "pause_timeout_s",
        "breaker_window_size",
        "breaker_failure_threshold",
        "open_backoff_base",
        "retry_base_delay_s",
    }
)

logger = logging.getLogger(__name__)


class GenerationFenceError(RuntimeError):
    """The worker no longer owns the persisted worktree generation."""


_MISSING = object()


class ParentEnvelopeError(ValueError):
    """The parent envelope failed strict schema validation on the worker side."""


class ChildProposalError(ValueError):
    """A declared child proposal set failed schema validation."""


_ENVELOPE_TEXT_KEYS = ("unified_diff", "summary", "status")
_ENVELOPE_LIST_KEYS = ("files_changed", "commits")
_ENVELOPE_KEYS = frozenset(
    _ENVELOPE_TEXT_KEYS
    + _ENVELOPE_LIST_KEYS
    + ("parent_task_id", "diff_truncated", "metric_score", "metric_breakdown")
)


def _validate_finite_json(value: Any, location: str, *, depth: int = 0) -> None:
    """Validate bounded JSON data without accepting NaN, infinity, or bool ints."""
    if depth > 8:
        raise ParentEnvelopeError(f"{location} is nested too deeply")
    if value is None or type(value) is bool or type(value) is str:
        return
    if type(value) is int:
        return
    if type(value) is float:
        if not math.isfinite(value):
            raise ParentEnvelopeError(f"{location} must be finite")
        return
    if type(value) is list:
        if len(value) > MAX_ENVELOPE_ITEMS:
            raise ParentEnvelopeError(f"{location} exceeds the item cap")
        for index, item in enumerate(value):
            _validate_finite_json(item, f"{location}[{index}]", depth=depth + 1)
        return
    if type(value) is dict:
        if len(value) > MAX_ENVELOPE_ITEMS:
            raise ParentEnvelopeError(f"{location} exceeds the item cap")
        for key, item in value.items():
            if type(key) is not str:
                raise ParentEnvelopeError(f"{location} keys must be strings")
            _validate_finite_json(item, f"{location}.{key}", depth=depth + 1)
        return
    raise ParentEnvelopeError(f"{location} contains an unsupported JSON value")


def _validate_parent_envelope(value: Any) -> dict[str, Any] | None:
    """Validate a strict-key parent envelope, or return None for no parent.

    The supervisor sends the strict ``_ENVELOPE_KEYS`` set with exact types
    and fields bounded to ``MAX_ENVELOPE_FIELD_CHARS`` / ``MAX_ENVELOPE_ITEMS``.
    A ``None`` or absent value means the task has no parent (valid). A present
    dict must match the schema exactly: every expected key present with the
    declared type, list items all strings, no unknown keys, and no overlong
    field. Any deviation is a supervisor/corruption defect and raises
    :class:`ParentEnvelopeError` rather than being silently trimmed.
    """
    if value is None:
        return None
    if not isinstance(value, dict):
        raise ParentEnvelopeError(f"parent_envelope must be an object, got {type(value).__name__}")
    if set(value) != _ENVELOPE_KEYS:
        missing = sorted(_ENVELOPE_KEYS - set(value))
        unknown = sorted(set(value) - _ENVELOPE_KEYS)
        details: list[str] = []
        if missing:
            details.append(f"missing keys: {missing}")
        if unknown:
            details.append(f"unknown keys: {unknown}")
        raise ParentEnvelopeError(
            "parent_envelope has an invalid key set"
            + (f" ({'; '.join(details)})" if details else "")
        )
    validated: dict[str, Any] = {}
    parent_task_id = value.get("parent_task_id", _MISSING)
    if parent_task_id is _MISSING or not (
        parent_task_id is None or (type(parent_task_id) is str and bool(parent_task_id))
    ):
        raise ParentEnvelopeError("parent_envelope 'parent_task_id' must be a string or null")
    validated["parent_task_id"] = parent_task_id
    for key in _ENVELOPE_TEXT_KEYS:
        field = value.get(key, _MISSING)
        if field is _MISSING:
            raise ParentEnvelopeError(f"parent_envelope missing required key {key!r}")
        if type(field) is not str:
            raise ParentEnvelopeError(
                f"parent_envelope {key!r} must be a string, got {type(field).__name__}"
            )
        if len(field.encode("utf-8")) > MAX_ENVELOPE_FIELD_CHARS:
            raise ParentEnvelopeError(f"parent_envelope {key!r} exceeds the field cap")
        validated[key] = field
    for key in _ENVELOPE_LIST_KEYS:
        field = value.get(key, _MISSING)
        if field is _MISSING:
            raise ParentEnvelopeError(f"parent_envelope missing required key {key!r}")
        if not isinstance(field, list):
            raise ParentEnvelopeError(
                f"parent_envelope {key!r} must be a list, got {type(field).__name__}"
            )
        if len(field) > MAX_ENVELOPE_ITEMS:
            raise ParentEnvelopeError(f"parent_envelope {key!r} exceeds the item cap")
        for item in field:
            if type(item) is not str:
                raise ParentEnvelopeError(f"parent_envelope {key!r} must contain only strings")
            if len(item.encode("utf-8")) > MAX_ENVELOPE_FIELD_CHARS:
                raise ParentEnvelopeError(f"parent_envelope {key!r} item exceeds the field cap")
        validated[key] = list(field)
    diff_truncated = value.get("diff_truncated", _MISSING)
    if type(diff_truncated) is not bool:
        raise ParentEnvelopeError("parent_envelope 'diff_truncated' must be a boolean")
    validated["diff_truncated"] = diff_truncated
    metric_score = value.get("metric_score", _MISSING)
    if metric_score is _MISSING or not (
        metric_score is None
        or (
            type(metric_score) in (int, float)
            and not isinstance(metric_score, bool)
            and (type(metric_score) is int or math.isfinite(metric_score))
        )
    ):
        raise ParentEnvelopeError("parent_envelope 'metric_score' must be a number or null")
    validated["metric_score"] = metric_score
    metric_breakdown = value.get("metric_breakdown", _MISSING)
    if type(metric_breakdown) is not dict:
        raise ParentEnvelopeError("parent_envelope 'metric_breakdown' must be an object")
    _validate_finite_json(metric_breakdown, "parent_envelope.metric_breakdown")
    try:
        encoded_breakdown = json.dumps(
            metric_breakdown, sort_keys=True, allow_nan=False, separators=(",", ":")
        ).encode("utf-8")
    except (TypeError, ValueError, UnicodeEncodeError) as exc:
        raise ParentEnvelopeError(
            "parent_envelope 'metric_breakdown' must contain JSON values"
        ) from exc
    if len(encoded_breakdown) > MAX_ENVELOPE_FIELD_CHARS:
        raise ParentEnvelopeError("parent_envelope 'metric_breakdown' exceeds the field cap")
    validated["metric_breakdown"] = copy.deepcopy(metric_breakdown)
    if not any(validated.values()):
        return None
    return validated


def _monotonic_ms() -> int:
    return time.monotonic_ns() // 1_000_000


def _env_float(name: str, default: float) -> float:
    value = os.environ.get(name)
    if value is None:
        return default
    try:
        parsed = float(value)
    except ValueError:
        raise ValueError(f"invalid {name}: expected a finite number") from None
    if not math.isfinite(parsed):
        raise ValueError(f"invalid {name}: expected a finite number")
    return parsed


async def send(writer: asyncio.StreamWriter, msg: dict[str, Any]) -> None:
    write_message(writer, msg)
    await writer.drain()


def git(*args: str, cwd: str | Path | None = None) -> tuple[int, str, str]:
    proc = subprocess.run(
        ["git", "-c", "core.hooksPath=/dev/null", *args],
        cwd=cwd,
        capture_output=True,
        text=True,
        env=scrub_environment(),
    )
    return proc.returncode, proc.stdout.strip(), proc.stderr.strip()


def _fenced_git(
    worktree: Path,
    generation: int,
    *args: str,
    cwd: str | Path | None = None,
) -> tuple[int, str, str]:
    """Run mutating git while continuously enforcing the generation fence."""
    _require_generation(worktree, generation)
    proc = subprocess.Popen(
        ["git", "-c", "core.hooksPath=/dev/null", *args],
        cwd=cwd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=scrub_environment(),
        start_new_session=True,
    )
    while proc.poll() is None:
        if validate_worker_generation(worktree, generation):
            time.sleep(0.001)
            continue
        try:
            os.killpg(proc.pid, 9)
        except ProcessLookupError:
            pass
        stdout, stderr = proc.communicate()
        raise GenerationFenceError(
            f"generation mismatch for {worktree}: worker={generation}, "
            "persisted generation is different or missing"
        )
    stdout, stderr = proc.communicate()
    _require_generation(worktree, generation)
    return proc.returncode, stdout.strip(), stderr.strip()


def _require_generation(worktree: Path, generation: int) -> None:
    if not validate_worker_generation(worktree, generation):
        raise GenerationFenceError(
            f"generation mismatch for {worktree}: worker={generation}, "
            "persisted generation is different or missing"
        )


def _write_worktree_state(worktree: Path, generation: int, path: Path, content: str) -> None:
    """Write worker state only while this process owns the current fence."""
    _require_generation(worktree, generation)
    path.write_text(content)


def cap_diff(diff: str) -> tuple[str, bool]:
    """Cap ``diff`` to ``MAX_DIFF_BYTES`` UTF-8 bytes, never splitting a
    codepoint; returns ``(diff, truncated)``."""
    raw = diff.encode("utf-8")
    if len(raw) <= MAX_DIFF_BYTES:
        return diff, False
    truncated = raw[:MAX_DIFF_BYTES].decode("utf-8", errors="ignore")
    return truncated + "\n... [diff truncated]", True


def _provider_fanout_config(run: dict[str, Any]) -> dict[str, Any] | None:
    config = run.get("fanout_config")
    if not isinstance(config, dict) or not config:
        return None
    return config


def _provider_env_keys(value: Any) -> tuple[str, ...]:
    if value is None:
        return ()
    if type(value) not in (list, tuple):
        raise ValueError("provider environment keys must be a list")
    if len(value) > MAX_ENVELOPE_ITEMS:
        raise ValueError("provider environment keys exceed the item cap")
    if not all(type(key) is str and key for key in value):
        raise ValueError("provider environment keys must contain non-empty strings")
    if len(set(value)) != len(value):
        raise ValueError("provider environment keys must be unique")
    return tuple(value)


def _checkpoint_redactor(provider_env_keys: tuple[str, ...], credentials: Any = None) -> Redactor:
    """Build the worker's immutable redactor from its authorized credentials."""
    secret_values = [
        value
        for key in provider_env_keys
        if isinstance(value := os.environ.get(key), str) and value
    ]
    if isinstance(credentials, dict):
        secret_values.extend(
            value
            for key, value in credentials.items()
            if key in provider_env_keys and isinstance(value, str) and value
        )
    return build_session_redactor(secret_values)


def _provider_path() -> Path:
    configured = os.environ.get("CAMBIUM_PROVIDERS")
    if not configured:
        raise RuntimeError("provider configuration is not set in CAMBIUM_PROVIDERS")
    path = Path(configured).expanduser()
    if not path.is_absolute():
        path = Path.cwd() / path
    return path.resolve()


def _fanout_section(config: dict[str, Any]) -> dict[str, Any]:
    for key in ("diffundo", "router"):
        section = config.get(key)
        if isinstance(section, dict):
            return section
    return config


def _fanout_value(config: dict[str, Any], section: dict[str, Any], key: str) -> Any:
    value = config.get(key)
    if value is not None:
        return value
    return section.get(key)


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


def _task_requirements(
    source: Mapping[str, Any] | None,
    fanout_config: dict[str, Any] | None,
) -> dict[str, Any] | None:
    """Combine task requirements with the equivalent fanout declarations.

    Plan/task requirements are authoritative, while the nested/direct fanout
    form keeps direct worker callers compatible with the provider config shape.
    Every value is validated before it reaches Diffundo so malformed input
    fails closed at the worker boundary as well as at supervisor admission.
    """
    from cambium.routing import validate_requirements

    merged: dict[str, Any] = {}
    if isinstance(fanout_config, dict):
        section = _fanout_section(fanout_config)
        nested = _fanout_value(fanout_config, section, "requirements")
        if nested is not None:
            merged.update(validate_requirements(nested))
        for key in _ROUTING_REQUIREMENT_KEYS:
            value = _fanout_value(fanout_config, section, key)
            if value is not None:
                merged[key] = value
    if source is not None and source.get("requirements") is not None:
        merged.update(validate_requirements(source["requirements"]))
    validated = validate_requirements(merged)
    return validated or None


def _model_identity(
    providers: list[Any],
    tier: ProviderTier,
    model: str,
    *,
    assigned_provider: str | None,
) -> str:
    """Resolve the truthful model identity for the agent system prompt.

    ``assigned_provider`` (supervisor admission balancing) is authoritative;
    without one, the provider name is included only when exactly one configured
    provider serves the resolved tier+model, so the identity never invents a
    provider. The model name always comes from the resolved fanout config.
    """
    provider = assigned_provider
    if provider is None:
        serving = [
            candidate.name
            for candidate in providers
            if candidate.enabled and candidate.tier is tier and candidate.model == model
        ]
        if len(serving) == 1:
            provider = serving[0]
    if provider:
        return f"{provider}/{model}"
    return model


def _provider_router(
    config: dict[str, Any],
    *,
    assigned_provider: str | None = None,
    authorized_providers: tuple[str, ...] = (),
    authorized_providers_explicit: bool = False,
    debt: Mapping[str, Any] | None = None,
    task_id: str | None = None,
    requirements: Mapping[str, Any] | None = None,
) -> tuple[Diffundo, ProviderTier, str, str]:
    providers = load_providers(_provider_path())
    # An explicitly empty authorized list is the historical "unrestricted"
    # wire value (the supervisor always sends the key): restriction applies
    # only to a non-empty list. `authorized_providers_explicit` records the
    # distinction for the provider-boundary descriptor, nothing more.
    if authorized_providers:
        authorized = frozenset(authorized_providers)
        providers = [provider for provider in providers if provider.name in authorized]
    section = _fanout_section(config)
    tier_value = _fanout_value(config, section, "tier")
    model = _fanout_value(config, section, "model")
    if not isinstance(tier_value, str) or not tier_value:
        raise ValueError("fanout_config requires a provider tier")
    if not isinstance(model, str) or not model:
        raise ValueError("fanout_config requires a provider model")
    try:
        tier = ProviderTier(tier_value)
    except ValueError as exc:
        raise ValueError(f"unsupported provider tier {tier_value!r}") from exc

    options: dict[str, Any] = {}
    for key in _DIFFUNDO_OPTIONS:
        value = _fanout_value(config, section, key)
        if value is not None:
            options[key] = value
    # Seed the per-subagent sticky primary from the task id: separate worker
    # processes (separate Diffundo instances, one task each) pick different
    # primary providers, spreading requests across providers at task
    # granularity while each task keeps its context on one provider (per-
    # provider prompt-prefix caching preserved).
    resolved_task_id = task_id
    if resolved_task_id is None:
        configured_task_id = config.get("task_id") if isinstance(config, dict) else None
        resolved_task_id = configured_task_id if isinstance(configured_task_id, str) else None
    if isinstance(resolved_task_id, str) and resolved_task_id:
        options.setdefault("rotation_seed", zlib.crc32(resolved_task_id.encode("utf-8")))
    if assigned_provider is not None:
        if not any(provider.name == assigned_provider for provider in providers):
            raise ValueError(
                f"assigned_provider {assigned_provider!r} is not an authorized configured provider"
            )
        options["primary_provider"] = assigned_provider
    if debt:
        options["debt"] = debt
    codex_providers = [
        provider
        for provider in providers
        if getattr(provider, "auth", None) is AuthMode.CODEX_CHATGPT
    ]
    if codex_providers:
        # The supervisor injected CAMBIUM_OAUTH_ACCESS_/ACCOUNT_<SUFFIX> into
        # the worker env at spawn (access token ONLY — the refresh token never
        # leaves the supervisor). Diffundo carries one CredentialSource, so
        # more than one codex provider is unsupported until a per-provider
        # mapping exists: fail closed rather than silently share a credential.
        if len(codex_providers) > 1:
            raise ValueError(
                "multiple codex_chatgpt providers require per-provider "
                "credential sources (unsupported)"
            )
        codex = codex_providers[0]
        suffix = oauth_env_suffix(codex.name)
        access = os.environ.get(f"CAMBIUM_OAUTH_ACCESS_{suffix}")
        account = os.environ.get(f"CAMBIUM_OAUTH_ACCOUNT_{suffix}")
        if not access:
            raise ValueError(
                f"codex provider {codex.name!r}: CAMBIUM_OAUTH_ACCESS_{suffix} "
                "is not set in the worker environment"
            )
        options["credential_source"] = CredentialSource(
            access_token=access, account_id=account or None
        )
    if resolved_task_id:
        options["task_id"] = resolved_task_id
    if requirements is None:
        requirements = _task_requirements(None, config)
    if requirements:
        options["requirements"] = dict(requirements)
    return (
        Diffundo(providers, **options),
        tier,
        model,
        _model_identity(providers, tier, model, assigned_provider=assigned_provider),
    )


def _positive_int(value: Any, name: str, default: int) -> int:
    if value is None:
        return default
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(f"{name} must be a positive integer")
    return value


def _strict_bool(value: Any, name: str) -> bool:
    if value is None:
        return False
    if type(value) is not bool:
        raise ValueError(f"{name} must be a boolean")
    return value


def _positive_float(value: Any, name: str, default: float) -> float:
    if value is None:
        return default
    if (
        isinstance(value, bool)
        or type(value) not in (int, float)
        or not math.isfinite(float(value))
        or value <= 0
    ):
        raise ValueError(f"{name} must be a positive number")
    return float(value)


def _rolling_compact_thresholds(
    values: Mapping[str, Any],
    max_transcript_chars: int,
    source: str,
) -> tuple[int, int]:
    """Parse rolling-fold character thresholds with a hysteresis band.

    The high threshold defaults to the existing transcript budget and the low
    threshold defaults to half of that high threshold.  These are character
    counts, so the legacy transcript budget and provider token accounting stay
    unchanged.
    """
    threshold_high = _positive_int(
        values.get("rolling_compact_threshold_high"),
        f"{source} rolling_compact_threshold_high",
        max_transcript_chars,
    )
    threshold_low = _positive_int(
        values.get("rolling_compact_threshold_low"),
        f"{source} rolling_compact_threshold_low",
        max(1, threshold_high // 2),
    )
    if threshold_low > threshold_high:
        raise ValueError(
            f"{source} rolling_compact_threshold_low must not exceed rolling_compact_threshold_high"
        )
    return threshold_high, threshold_low


@dataclass(frozen=True, slots=True)
class AgentConfig:
    """Immutable per-task agent configuration parsed from init (init is authoritative)."""

    task_id: str
    generation: int
    task: str
    worktree: Path | None
    base_commit: str | None
    fanout_config: dict[str, Any] | None
    max_turns: int
    max_tokens: int
    shell_permission: bool
    network_permission: bool
    heartbeat_interval_s: float
    max_wall_s: float
    checkpoint_root: Path | None
    requirements: dict[str, Any] | None = None
    max_transcript_chars: int = MAX_TRANSCRIPT_CHARS
    # Supervisor-level admission balancing (solution C): the provider this
    # task was assigned at admission; presets Diffundo's sticky primary.
    assigned_provider: str | None = None
    authorized_providers: tuple[str, ...] = ()
    authorized_providers_explicit: bool = False
    debt: Mapping[str, Any] | None = None
    provider_env_keys: tuple[str, ...] = ()
    redactor: Redactor | None = None
    # Dynamic child admission: the parent's strict-key envelope (summary,
    # files_changed, commits, ...) rendered as a bounded prompt block so a
    # child starts from the parent's outcome without inheriting its session.
    parent_envelope: dict[str, Any] | None = None
    # Cache-first context reuse (plan phase 1): ``context_reuse`` opts the
    # task into suspending at delegate boundaries; ``context_fork`` (init,
    # supervisor to child) carries the strict fork descriptor of the epoch a
    # compatible child reuses; ``resume`` (run_task) carries the checkpoint
    # ref and bounded child-result envelopes a suspended parent continues from.
    context_reuse: bool = True
    # Rolling compaction is the default context-reuse policy. Internal callers
    # can disable it explicitly. Thresholds are character counts: high defaults
    # to the transcript budget and low defaults to half of high.
    rolling_compact: bool = True
    rolling_compact_threshold_high: int = 0
    rolling_compact_threshold_low: int = 0
    context_fork: dict[str, Any] | None = None
    # Provider-neutral summary history used when an exact cache fork is illegal.
    summary_trunk_ref: str | None = None
    resume: dict[str, Any] | None = None

    def __post_init__(self) -> None:
        """Derive threshold defaults for direct in-process configurations."""
        threshold_high = self.rolling_compact_threshold_high
        if threshold_high == 0:
            threshold_high = self.max_transcript_chars
        threshold_low = self.rolling_compact_threshold_low
        if threshold_low == 0:
            threshold_low = max(1, threshold_high // 2)
        if threshold_high <= 0 or threshold_low <= 0 or threshold_low > threshold_high:
            raise ValueError("invalid rolling compaction thresholds")
        object.__setattr__(self, "rolling_compact_threshold_high", threshold_high)
        object.__setattr__(self, "rolling_compact_threshold_low", threshold_low)

    @classmethod
    def from_init(cls, init: dict[str, Any]) -> AgentConfig:
        """Parse and validate the init message; raises ``ValueError`` on bad input."""
        permissions = init.get("permissions")
        if not isinstance(permissions, dict):
            permissions = {}
        shell_permission = permissions.get("shell", False)
        network_permission = permissions.get("network", False)
        if not isinstance(shell_permission, bool) or not isinstance(network_permission, bool):
            raise ValueError("init permissions.shell/network must be strict booleans")
        heartbeat = init.get("heartbeat")
        heartbeat_interval_s = heartbeat.get("interval_s") if isinstance(heartbeat, dict) else None
        budget = init.get("budget")
        max_wall_s = budget.get("max_wall_s") if isinstance(budget, dict) else None
        worktree = init.get("worktree")
        base_commit = init.get("base_commit")
        task = init.get("spec")
        task_id = init.get("task_id", "unknown")
        if type(task_id) is not str or not task_id:
            raise ValueError("init task_id must be a non-empty string")
        assigned_provider = init.get("assigned_provider")
        if assigned_provider is not None and (
            type(assigned_provider) is not str or not assigned_provider
        ):
            raise ValueError("init assigned_provider must be a string")
        provider_env_keys = _provider_env_keys(init.get("provider_env_keys"))
        authorized_providers = _provider_env_keys(init.get("authorized_providers"))
        authorized_explicit = init.get("authorized_providers_explicit")
        if authorized_explicit is None:
            authorized_explicit = "authorized_providers" in init
        if type(authorized_explicit) is not bool:
            raise ValueError("init authorized_providers_explicit must be a boolean")
        debt = init.get("debt")
        if debt is not None and not isinstance(debt, dict):
            raise ValueError("init debt must be a mapping")
        session_id = os.environ.get("CAMBIUM_SESSION_ID")
        checkpoint_root = (
            Path(session_id).resolve() / ".cambium" / "checkpoints" if session_id else None
        )
        max_transcript_chars = _positive_int(
            init.get("max_transcript_chars"),
            "init max_transcript_chars",
            MAX_TRANSCRIPT_CHARS,
        )
        rolling_threshold_high, rolling_threshold_low = _rolling_compact_thresholds(
            init, max_transcript_chars, "init"
        )
        return cls(
            task_id=task_id,
            generation=_positive_int(init.get("generation"), "init generation", 1),
            task=task if isinstance(task, str) else "",
            worktree=Path(worktree) if isinstance(worktree, str) else None,
            base_commit=base_commit if isinstance(base_commit, str) else None,
            fanout_config=_provider_fanout_config(init),
            assigned_provider=assigned_provider,
            authorized_providers=authorized_providers,
            authorized_providers_explicit=authorized_explicit,
            debt=debt or None,
            max_turns=_positive_int(init.get("max_turns"), "init max_turns", DEFAULT_MAX_TURNS),
            max_tokens=_positive_int(init.get("max_tokens"), "init max_tokens", DEFAULT_MAX_TOKENS),
            shell_permission=shell_permission,
            network_permission=network_permission,
            heartbeat_interval_s=_positive_float(
                heartbeat_interval_s, "init heartbeat.interval_s", HEARTBEAT_INTERVAL_S
            ),
            max_wall_s=_positive_float(max_wall_s, "init budget.max_wall_s", DEFAULT_MAX_WALL_S),
            checkpoint_root=checkpoint_root,
            requirements=_task_requirements(init, _provider_fanout_config(init)),
            max_transcript_chars=max_transcript_chars,
            provider_env_keys=provider_env_keys,
            redactor=_checkpoint_redactor(provider_env_keys, init.get("credentials")),
            parent_envelope=_validate_parent_envelope(init.get("parent_envelope")),
            context_reuse=_strict_bool(init.get("context_reuse"), "init context_reuse"),
            rolling_compact=_strict_bool(init.get("rolling_compact", True), "init rolling_compact"),
            rolling_compact_threshold_high=rolling_threshold_high,
            rolling_compact_threshold_low=rolling_threshold_low,
            context_fork=_validate_context_fork(init.get("context_fork")),
            summary_trunk_ref=_validate_summary_trunk_ref(init.get("summary_trunk_ref")),
        )


def _merge_task_config(
    config: AgentConfig, init: dict[str, Any], run: dict[str, Any]
) -> AgentConfig:
    """Fill execution fields from run_task only when init omitted them (init authoritative)."""
    max_turns = config.max_turns
    if "max_turns" not in init:
        max_turns = _positive_int(run.get("max_turns"), "run_task max_turns", DEFAULT_MAX_TURNS)
    max_tokens = config.max_tokens
    if "max_tokens" not in init:
        max_tokens = _positive_int(run.get("max_tokens"), "run_task max_tokens", DEFAULT_MAX_TOKENS)
    max_wall_s = config.max_wall_s
    init_budget = init.get("budget")
    init_provided_wall = isinstance(init_budget, dict) and "max_wall_s" in init_budget
    if not init_provided_wall:
        max_wall_s = _positive_float(
            run.get("max_wall_s"), "run_task max_wall_s", DEFAULT_MAX_WALL_S
        )
    worktree = config.worktree
    if worktree is None and isinstance(run.get("worktree_path"), str):
        worktree = Path(run["worktree_path"])
    base_commit = config.base_commit or run.get("base_commit")
    task = config.task if config.task.strip() else str(run.get("task", ""))
    fanout_config = config.fanout_config or _provider_fanout_config(run)
    requirements = config.requirements
    if requirements is None:
        requirements = _task_requirements(run, fanout_config)
    parent_envelope = config.parent_envelope or _validate_parent_envelope(
        run.get("parent_envelope")
    )
    resume = config.resume or _validate_resume(run.get("resume"))
    summary_trunk_ref = config.summary_trunk_ref or _validate_summary_trunk_ref(
        run.get("summary_trunk_ref")
    )
    rolling_compact = config.rolling_compact
    if "rolling_compact" not in init and "rolling_compact" in run:
        rolling_compact = _strict_bool(run.get("rolling_compact"), "run_task rolling_compact")
    threshold_values: dict[str, Any] = {
        "rolling_compact_threshold_high": config.rolling_compact_threshold_high,
        "rolling_compact_threshold_low": config.rolling_compact_threshold_low,
    }
    if "rolling_compact_threshold_high" not in init:
        threshold_values["rolling_compact_threshold_high"] = run.get(
            "rolling_compact_threshold_high"
        )
    if "rolling_compact_threshold_low" not in init:
        threshold_values["rolling_compact_threshold_low"] = run.get("rolling_compact_threshold_low")
    rolling_threshold_high, rolling_threshold_low = _rolling_compact_thresholds(
        threshold_values, config.max_transcript_chars, "run_task"
    )
    return AgentConfig(
        task_id=config.task_id,
        generation=config.generation,
        task=task,
        worktree=worktree,
        base_commit=base_commit if isinstance(base_commit, str) else None,
        fanout_config=fanout_config,
        assigned_provider=config.assigned_provider,
        authorized_providers=config.authorized_providers,
        authorized_providers_explicit=config.authorized_providers_explicit,
        debt=config.debt,
        max_turns=max_turns,
        max_tokens=max_tokens,
        shell_permission=config.shell_permission,
        network_permission=config.network_permission,
        heartbeat_interval_s=config.heartbeat_interval_s,
        max_wall_s=max_wall_s,
        checkpoint_root=config.checkpoint_root,
        requirements=requirements,
        max_transcript_chars=config.max_transcript_chars,
        provider_env_keys=config.provider_env_keys,
        redactor=config.redactor,
        parent_envelope=parent_envelope,
        context_reuse=config.context_reuse,
        rolling_compact=rolling_compact,
        rolling_compact_threshold_high=rolling_threshold_high,
        rolling_compact_threshold_low=rolling_threshold_low,
        context_fork=config.context_fork,
        summary_trunk_ref=summary_trunk_ref,
        resume=resume,
    )


def _config_from_run(run: dict[str, Any]) -> AgentConfig:
    """Fallback config when do_work is invoked directly (no init message)."""
    provider_env_keys = _provider_env_keys(run.get("provider_env_keys"))
    task_id = run.get("task_id", "unknown")
    if type(task_id) is not str or not task_id:
        raise ValueError("run_task task_id must be a non-empty string")
    authorized_explicit = run.get("authorized_providers_explicit")
    if authorized_explicit is None:
        authorized_explicit = "authorized_providers" in run
    if type(authorized_explicit) is not bool:
        raise ValueError("run_task authorized_providers_explicit must be a boolean")
    max_transcript_chars = _positive_int(
        run.get("max_transcript_chars"),
        "run_task max_transcript_chars",
        MAX_TRANSCRIPT_CHARS,
    )
    rolling_threshold_high, rolling_threshold_low = _rolling_compact_thresholds(
        run, max_transcript_chars, "run_task"
    )
    return AgentConfig(
        task_id=task_id,
        generation=_positive_int(run.get("generation"), "run_task generation", 1),
        task=str(run.get("task", "")),
        worktree=(
            Path(run["worktree_path"]) if isinstance(run.get("worktree_path"), str) else None
        ),
        base_commit=run.get("base_commit"),
        fanout_config=_provider_fanout_config(run),
        assigned_provider=None,
        authorized_providers=_provider_env_keys(run.get("authorized_providers")),
        authorized_providers_explicit=authorized_explicit,
        debt=None,
        max_turns=_positive_int(run.get("max_turns"), "run_task max_turns", DEFAULT_MAX_TURNS),
        max_tokens=_positive_int(run.get("max_tokens"), "run_task max_tokens", DEFAULT_MAX_TOKENS),
        shell_permission=False,
        network_permission=False,
        heartbeat_interval_s=HEARTBEAT_INTERVAL_S,
        max_wall_s=_positive_float(
            run.get("max_wall_s"), "run_task max_wall_s", DEFAULT_MAX_WALL_S
        ),
        checkpoint_root=None,
        requirements=_task_requirements(run, _provider_fanout_config(run)),
        max_transcript_chars=max_transcript_chars,
        provider_env_keys=provider_env_keys,
        redactor=_checkpoint_redactor(provider_env_keys, run.get("credentials")),
        parent_envelope=_validate_parent_envelope(run.get("parent_envelope")),
        context_reuse=_strict_bool(run.get("context_reuse"), "run_task context_reuse"),
        rolling_compact=_strict_bool(run.get("rolling_compact", True), "run_task rolling_compact"),
        rolling_compact_threshold_high=rolling_threshold_high,
        rolling_compact_threshold_low=rolling_threshold_low,
        context_fork=_validate_context_fork(run.get("context_fork")),
        summary_trunk_ref=_validate_summary_trunk_ref(run.get("summary_trunk_ref")),
        resume=_validate_resume(run.get("resume")),
    )


class AgentProgress:
    """Current turn/tool/status shared with the heartbeat loop."""

    __slots__ = ("turn", "tool", "status")

    def __init__(self) -> None:
        self.turn = 0
        self.tool: str | None = None
        self.status = "working"


def _cap_utf8(text: str, limit: int) -> str:
    raw = text.encode("utf-8")
    if len(raw) <= limit:
        return text
    return raw[:limit].decode("utf-8", errors="ignore")


def _bounded_text(text: str, limit: int) -> str:
    raw = text.encode("utf-8")
    if len(raw) <= limit:
        return text
    return raw[:limit].decode("utf-8", errors="ignore") + "\n... [truncated]"


def _safe_task_id(task_id: str) -> str:
    safe = re.sub(r"[^A-Za-z0-9_.-]", "_", task_id)
    return safe or "task"


_EPOCH_REF_RE = re.compile(
    r"epoch-(?P<epoch>[0-9]{3,})-(?P<pre>[0-9a-f]{16})-"
    r"(?P<persisted>[0-9a-f]{16})\.json\Z"
)


def _validate_checkpoint_ref_shape(checkpoint_ref: str) -> tuple[str, int, str, str]:
    """Validate the relative two-component epoch reference without resolving it."""
    if type(checkpoint_ref) is not str or not checkpoint_ref:
        raise ContextForkError("invalid checkpoint_ref")
    relative = Path(checkpoint_ref)
    if relative.is_absolute() or any(part in {"", ".", ".."} for part in relative.parts):
        raise ContextForkError("invalid checkpoint_ref path")
    if len(relative.parts) != 2:
        raise ContextForkError("invalid checkpoint_ref path")
    task_component, filename = relative.parts
    if task_component != _safe_task_id(task_component):
        raise ContextForkError("invalid checkpoint_ref task path")
    match = _EPOCH_REF_RE.fullmatch(filename)
    if match is None:
        raise ContextForkError("invalid checkpoint_ref filename")
    return (
        task_component,
        int(match.group("epoch")),
        match.group("pre"),
        match.group("persisted"),
    )


def _canonical_json_bytes(value: Any) -> bytes:
    """Encode JSON deterministically and reject non-finite values."""
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")


def _checkpoint_address(payload: Mapping[str, Any]) -> str:
    """Return the short address for a payload with its ref removed."""
    normalized = copy.deepcopy(dict(payload))
    normalized["checkpoint_ref"] = ""
    return _sha256_hex(_canonical_json_bytes(normalized))[:16]


def _atomic_json_write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    temporary_path: Path | None = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(cast(Path, temporary_path), path)
        temporary_path = None
    finally:
        if temporary_path is not None:
            try:
                temporary_path.unlink()
            except FileNotFoundError:
                pass


def _valid_usage_count(value: Any) -> TypeGuard[int | float]:
    """Return whether a provider token count is finite and non-negative."""
    if isinstance(value, bool):
        return False
    if isinstance(value, int):
        return value >= 0
    return isinstance(value, float) and math.isfinite(value) and value >= 0


def _invalid_usage_fields(usage: dict[str, Any] | None) -> tuple[str, ...]:
    """Return recognized provider usage fields with unsafe numeric values.

    Unknown provider usage fields remain omitted as before; the worker only
    accepts the scalar token-count fields in ``_USAGE_COUNT_FIELDS``.
    """
    if not isinstance(usage, dict):
        return ()
    return tuple(
        sorted(
            key
            for key, value in usage.items()
            if key in _USAGE_COUNT_FIELDS and not _valid_usage_count(value)
        )
    )


def _usage_counts(usage: dict[str, Any] | None) -> dict[str, int | float]:
    if not isinstance(usage, dict):
        return {}
    return {
        key: value
        for key, value in usage.items()
        if key in _USAGE_COUNT_FIELDS and _valid_usage_count(value)
    }


_ALL_TOOL_NAMES = frozenset(schema["name"] for schema in TOOL_SCHEMAS)


def _exposed_tool_schemas(config: AgentConfig) -> list[dict[str, Any]]:
    """Schemas offered to the model; shell and mutating git are permission-filtered."""
    schemas: list[dict[str, Any]] = []
    for schema in TOOL_SCHEMAS:
        name = schema["name"]
        if name == "run_shell":
            if config.shell_permission:
                schemas.append(schema)
            continue
        if name == "git_op":
            restricted = copy.deepcopy(schema)
            op_property = restricted["parameters"]["properties"]["op"]
            op_property["enum"] = [
                value for value in op_property.get("enum", ()) if value in INSPECTION_GIT_OPS
            ]
            schemas.append(restricted)
            continue
        schemas.append(schema)
    return schemas


def _permission_denied(name: str, args: dict[str, Any], config: AgentConfig) -> str | None:
    if name == "git_op":
        op = args.get("op")
        if op not in INSPECTION_GIT_OPS:
            return f"git_op is restricted to inspection operations {sorted(INSPECTION_GIT_OPS)!r}"
    return None


def _action_keys(parsed: dict[str, Any], required: frozenset[str]) -> bool:
    """Exactly ``required`` keys plus at most an optional ``thought`` field."""
    allowed = required | {"thought"}
    return required <= parsed.keys() and parsed.keys() <= allowed


_STRICT_ACTION_DECODER = json.JSONDecoder()
_LENIENT_ACTION_DECODER = json.JSONDecoder(strict=False)


def _decode_action_json(text: str) -> tuple[Any, int]:
    """Decode one JSON value at the start of ``text``.  When the strict
    decoder rejects the input only because a string value contains a raw
    control character, retry with the lenient decoder; every other strictness
    rule is unchanged."""
    try:
        return _STRICT_ACTION_DECODER.raw_decode(text)
    except json.JSONDecodeError:
        return _LENIENT_ACTION_DECODER.raw_decode(text)


def _parse_agent_action(content: str) -> dict[str, Any]:
    """Strictly parse ONE agent action; a response may carry several
    concatenated JSON actions, in which case the first complete action is
    used (trailing actions are ignored and surfaced to the loop).  Raises
    ``ValueError`` on any deviation.

    Accepted shapes (each may optionally carry a ``thought`` field for
    reasoning; the action fields themselves must be exact):
        {"type": "plan", "steps": [<non-empty strings>]}
        {"type": "tool_call", "name": <schema name>, "arguments": {...}}
        {"type": "finish", "summary": <non-empty str>}
    """
    text = content.strip()
    if not text:
        raise ValueError("empty agent action")
    if len(text.encode("utf-8")) > MAX_ACTION_CONTENT_BYTES:
        raise ValueError("agent action exceeds the field cap")
    try:
        parsed, _end = _decode_action_json(text)
    except (json.JSONDecodeError, UnicodeDecodeError, RecursionError) as exc:
        raise ValueError(f"action is not valid JSON: {exc}") from None
    if not isinstance(parsed, dict):
        raise ValueError("agent action must be exactly one JSON object")
    action_type = parsed.get("type")
    if action_type == "plan":
        if not _action_keys(parsed, frozenset({"type", "steps"})):
            raise ValueError("plan must carry exactly type/steps (plus optional thought)")
        steps = parsed.get("steps")
        if (
            not isinstance(steps, list)
            or not steps
            or not all(isinstance(step, str) and step.strip() for step in steps)
        ):
            raise ValueError("plan steps must be a non-empty array of non-empty strings")
        return {"type": "plan", "steps": list(steps)}
    if action_type == "tool_call":
        if not _action_keys(parsed, frozenset({"type", "name", "arguments"})):
            raise ValueError(
                "tool_call must carry exactly type/name/arguments (plus optional thought)"
            )
        name = parsed.get("name")
        arguments = parsed.get("arguments")
        if not isinstance(name, str) or not name:
            raise ValueError("tool_call name must be a non-empty string")
        if not isinstance(arguments, dict):
            raise ValueError("tool_call arguments must be an object")
        if name not in _ALL_TOOL_NAMES:
            raise ValueError(f"unknown tool: {name!r}")
        return {"type": "tool_call", "name": name, "arguments": arguments}
    if action_type == "finish":
        if not _action_keys(parsed, frozenset({"type", "summary"})):
            raise ValueError("finish must carry exactly type/summary (plus optional thought)")
        summary = parsed.get("summary")
        if not isinstance(summary, str) or not summary.strip():
            raise ValueError("finish summary must be a non-empty string")
        return {"type": "finish", "summary": summary}
    raise ValueError(f"unknown agent action type: {action_type!r}")


_TRAILING_ACTION_NOTE = (
    "only the first action was executed; trailing JSON was ignored — "
    "emit exactly one JSON action per turn"
)


def _action_trailing(content: str) -> str:
    """Return the non-whitespace content AFTER the first complete JSON object,
    or "" when there is none or the content cannot be parsed at all."""
    text = content.strip()
    if not text:
        return ""
    try:
        _obj, end = _decode_action_json(text)
    except (json.JSONDecodeError, UnicodeDecodeError, RecursionError):
        return ""
    return text[end:].strip()


def _usage_prompt_tokens(usage: dict[str, Any] | None) -> int | None:
    """Return one completion's prompt-side token count, or ``None``.

    Prefers ``prompt_tokens``, then ``input_tokens``; ``None`` when the usage
    reports neither as a valid count.
    """
    if not isinstance(usage, dict):
        return None
    for key in ("prompt_tokens", "input_tokens"):
        value = usage.get(key)
        if _valid_usage_count(value):
            return int(value)
    return None


def _usage_completion_tokens(usage: dict[str, Any] | None) -> int | None:
    """Return one completion's output-side token count, or ``None``.

    Prefers ``completion_tokens``, then ``output_tokens``; ``None`` when the
    usage reports neither as a valid count.
    """
    if not isinstance(usage, dict):
        return None
    for key in ("completion_tokens", "output_tokens"):
        value = usage.get(key)
        if _valid_usage_count(value):
            return int(value)
    return None


def _usage_total(usage: dict[str, Any] | None) -> int | None:
    """Return one completion's usable token total, or ``None`` (fail closed).

    A recognized invalid count rejects the whole completion instead of being
    silently omitted.  This is the strict ingestion choice: the caller fails
    the attempt before emitting an event or updating cumulative usage.
    """
    if not isinstance(usage, dict):
        return None
    if _invalid_usage_fields(usage):
        return None
    total = usage.get("total_tokens")
    if _valid_usage_count(total):
        return int(total)
    inputs = usage.get("input_tokens", usage.get("prompt_tokens"))
    outputs = usage.get("output_tokens", usage.get("completion_tokens"))
    if _valid_usage_count(inputs) and _valid_usage_count(outputs):
        return int(inputs) + int(outputs)
    return None


def _accumulate_usage(cumulative: dict[str, int], usage: dict[str, Any] | None) -> dict[str, int]:
    """Add one validated completion without letting a stale total poison it.

    ``total_tokens`` in the cumulative snapshot is a normalized running total:
    each call contributes its explicit total when present, otherwise its
    input+output sum.  It is not a sum of only the calls that reported an
    explicit ``total_tokens`` field.
    """
    if _invalid_usage_fields(usage):
        return cumulative
    for key, value in _usage_counts(usage).items():
        if key == "total_tokens":
            continue
        cumulative[key] = cumulative.get(key, 0) + int(value)
    total = _usage_total(usage)
    if total is not None:
        previous_total = cumulative.get("total_tokens", 0)
        if not _valid_usage_count(previous_total):
            previous_total = 0
        cumulative["total_tokens"] = int(previous_total) + total
    return cumulative


def _cumulative_total(cumulative: dict[str, int]) -> int:
    total = cumulative.get("total_tokens")
    if _valid_usage_count(total):
        return int(total)
    inputs = cumulative.get("input_tokens", cumulative.get("prompt_tokens"))
    outputs = cumulative.get("output_tokens", cumulative.get("completion_tokens"))
    if _valid_usage_count(inputs) and _valid_usage_count(outputs):
        return int(inputs) + int(outputs)
    return 0


def _transcript_chars(transcript: list[dict[str, Any]]) -> int:
    total = 0
    for message in transcript:
        content = message.get("content")
        if isinstance(content, str):
            total += len(content)
    return total


def _plan_message(transcript: list[dict[str, Any]]) -> dict[str, Any] | None:
    """Return the transcript entry carrying a valid ``plan`` action, or ``None``."""
    for message in transcript:
        if message.get("role") != "assistant":
            continue
        content = message.get("content")
        if not isinstance(content, str):
            continue
        try:
            parsed = json.loads(content)
        except (json.JSONDecodeError, UnicodeDecodeError, RecursionError):
            continue
        if isinstance(parsed, dict) and parsed.get("type") == "plan":
            return message
    return None


_OBSERVATION_HEADER_RE = re.compile(r"\Atool (?P<name>[a-z_]+) ok=(?P<ok>True|False)\n")


def _dropped_marker(dropped: int) -> dict[str, str]:
    """The counted omission marker for dropped whole turns."""
    return {
        "role": "user",
        "content": (
            f"[prior context: {dropped} earlier message(s) dropped to bound the "
            "transcript; only the plan and the most recent turns are retained]"
        ),
    }


def _turn_groups(messages: list[dict[str, Any]]) -> list[list[dict[str, Any]]]:
    """Group a message tail into whole turns.

    A turn starts at each assistant action and includes the user messages
    that follow it (observation, notes, loop state). User messages before
    the first assistant action form one leading group.
    """
    groups: list[list[dict[str, Any]]] = []
    current: list[dict[str, Any]] = []
    for message in messages:
        if message.get("role") == "assistant" and current:
            groups.append(current)
            current = []
        current.append(message)
    if current:
        groups.append(current)
    return groups


def _bound_observation(content: str, limit: int) -> str:
    """Bound one wrapped tool observation to ``limit`` chars.

    Only the observation body is truncated; the ``tool NAME ok=...`` header
    line stays intact and a counted suffix reports the omitted body chars.
    Returns ``content`` unchanged when it is not a wrapped observation or
    the header alone already exceeds ``limit``.
    """
    if len(content) <= limit:
        return content
    match = _OBSERVATION_HEADER_RE.match(content)
    if match is None:
        return content
    header = match.group(0)
    if limit <= len(header):
        return content
    body = content[len(header) :]
    body_budget = limit - len(header)
    reservation = f"\n[{len(body)} observation char(s) omitted]"
    room = body_budget - len(reservation)
    if room <= 0:
        return header + _cap_utf8(body, body_budget)
    truncated = _cap_utf8(body, room)
    omitted = len(body) - len(truncated)
    return header + truncated + f"\n[{omitted} observation char(s) omitted]"


def _newest_observation(
    groups: list[list[dict[str, Any]]],
) -> tuple[int, int, dict[str, Any]] | None:
    """The newest wrapped tool observation across ``groups``, if any."""
    for group_index in range(len(groups) - 1, -1, -1):
        group = groups[group_index]
        for message_index in range(len(group) - 1, -1, -1):
            message = group[message_index]
            content = message.get("content")
            if (
                message.get("role") == "user"
                and isinstance(content, str)
                and _OBSERVATION_HEADER_RE.match(content) is not None
            ):
                return group_index, message_index, message
    return None


def _fit_turns_to_budget(
    *,
    plan: dict[str, Any] | None,
    tail: list[dict[str, Any]],
    dropped: int,
    budget: int,
    measure: Callable[[list[dict[str, Any]]], int],
    drop_head: bool = False,
) -> list[dict[str, Any]]:
    """Assemble ``plan`` + counted marker + ``tail`` within ``budget``.

    Turn-atomic: the oldest whole turns drop first and the marker counts
    every dropped message. When the oldest remaining turn carries the newest
    tool observation, that observation is bounded inside its wrapper instead
    of dropping the turn. Inputs are never mutated. With ``drop_head`` the
    plan and marker are also droppable so a degenerate budget still fits.
    """
    groups = _turn_groups(tail)
    head = [plan] if plan is not None else []

    def assemble() -> list[dict[str, Any]]:
        return [
            *head,
            _dropped_marker(dropped),
            *(message for group in groups for message in group),
        ]

    while True:
        assembled = assemble()
        overflow = measure(assembled) - budget
        if overflow <= 0:
            return assembled
        if not groups:
            if not drop_head:
                return assembled
            if head:
                dropped += 1
                head.pop(0)
                continue
            return []
        newest = _newest_observation(groups)
        if newest is not None and newest[0] == 0:
            _, _, observation = newest
            content = str(observation["content"])
            bounded = _bound_observation(content, max(0, len(content) - overflow))
            if bounded != content:
                groups[0] = [
                    {**message, "content": bounded} if message is observation else message
                    for message in groups[0]
                ]
                continue
        dropped += len(groups.pop(0))


def _summarize_transcript(
    transcript: list[dict[str, Any]],
    budget: int,
    keep_turns: int = TRANSCRIPT_KEEP_TURNS,
    max_messages: int | None = None,
    *,
    measure: Callable[[list[dict[str, Any]]], int] = _transcript_chars,
    drop_head: bool = False,
) -> list[dict[str, Any]]:
    """Bound the transcript to ``budget`` without calling the LLM.

    Returns the transcript unchanged when it fits within the budget. When it
    does not, keeps the plan message (if any), drops whole turns oldest-first
    under one counted "prior context" marker, and bounds an oversized newest
    observation inside its wrapper rather than slicing it. ``max_messages``
    applies the same retention policy when a message-count guard is also
    active. ``measure`` sizes the assembled list (chars by default; the
    rolling renderer passes its serialized size); ``drop_head`` lets the
    plan and marker drop too so a degenerate budget still fits. The input
    transcript is never mutated.
    """
    if measure(transcript) <= budget and (max_messages is None or len(transcript) <= max_messages):
        return list(transcript)
    tail = transcript[-(keep_turns * 2) :] if keep_turns > 0 else []
    plan = _plan_message(transcript)
    tail = [message for message in tail if message is not plan]
    if max_messages is not None:
        reserved = (1 if plan is not None else 0) + 1
        tail_capacity = max(0, max_messages - reserved)
        tail = tail[-tail_capacity:] if tail_capacity else []
    dropped = len(transcript) - len(tail) - (1 if plan is not None else 0)
    fitted = _fit_turns_to_budget(
        plan=plan,
        tail=tail,
        dropped=dropped,
        budget=budget,
        measure=measure,
        drop_head=drop_head,
    )
    if max_messages is not None and len(fitted) > max_messages:
        # The plan outranks the marker under an extreme message-count guard.
        marker_index = next(
            (
                index
                for index, message in enumerate(fitted)
                if "prior context" in str(message.get("content", ""))
            ),
            None,
        )
        del fitted[marker_index if marker_index is not None else -1]
    return fitted


_READ_TOOL_NAMES = frozenset({"read_file", "read_batch"})
_EDIT_TOOL_NAMES = frozenset({"edit_file", "write_file"})


def _call_paths(name: str, arguments: dict[str, Any]) -> list[str]:
    """The file paths a tool call reads or writes (identifiers, not bodies)."""
    if name == "read_batch":
        paths = arguments.get("paths")
        if isinstance(paths, list):
            return [path for path in paths if isinstance(path, str)]
        return []
    path = arguments.get("path")
    return [path] if isinstance(path, str) else []


def _strip_for_fold(continuation: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Tier-1 deterministic semantic stripping for fold rendering (plan §9.1.7).

    Obsolete read bodies (every path re-read or edited by a later turn)
    collapse to a one-line on-disk pointer; passing run_shell outputs
    superseded by a later passing run collapse to a one-line status. Edits,
    failures, the latest passing verification, identifiers, and the plan
    survive. Pure (inputs never mutated), whole messages only, idempotent:
    the markers regenerate byte-identically on a second pass.
    """
    calls: list[tuple[int, int, str, dict[str, Any], bool]] = []
    for index, message in enumerate(continuation):
        if message.get("role") != "assistant":
            continue
        content = message.get("content")
        if not isinstance(content, str):
            continue
        try:
            action = json.loads(content)
        except (json.JSONDecodeError, RecursionError):
            continue
        if not isinstance(action, dict) or action.get("type") != "tool_call":
            continue
        name = action.get("name")
        arguments = action.get("arguments")
        if not isinstance(name, str) or not isinstance(arguments, dict):
            continue
        obs_index = index + 1
        while obs_index < len(continuation):
            candidate = continuation[obs_index]
            if candidate.get("role") == "assistant":
                break
            obs_content = candidate.get("content")
            if (
                candidate.get("role") == "user"
                and isinstance(obs_content, str)
                and (match := _OBSERVATION_HEADER_RE.match(obs_content)) is not None
                and match.group("name") == name
            ):
                calls.append((index, obs_index, name, arguments, match.group("ok") == "True"))
                break
            obs_index += 1

    # suffix_paths[p] = paths read or edited by any call after position p.
    suffix_paths: list[set[str]] = [set() for _ in range(len(calls) + 1)]
    for position in range(len(calls) - 1, -1, -1):
        _, _, name, arguments, _ = calls[position]
        suffix_paths[position] = set(suffix_paths[position + 1])
        if name in _READ_TOOL_NAMES or name in _EDIT_TOOL_NAMES:
            suffix_paths[position].update(_call_paths(name, arguments))
    last_passing_shell = max(
        (obs_index for _, obs_index, name, _, ok in calls if name == "run_shell" and ok),
        default=None,
    )
    rewrites: dict[int, str] = {}
    for position, (_, obs_index, name, arguments, ok) in enumerate(calls):
        content = str(continuation[obs_index]["content"])
        header = content[: content.index("\n") + 1]
        if name in _READ_TOOL_NAMES and ok:
            paths = _call_paths(name, arguments)
            if paths and all(path in suffix_paths[position + 1] for path in paths):
                rewrites[obs_index] = (
                    f"{header}[{name}: {', '.join(paths)} (omitted - file on disk)]"
                )
        elif name == "run_shell" and ok and obs_index != last_passing_shell:
            rewrites[obs_index] = (
                f"{header}[run_shell: passed (output omitted - superseded by a later run)]"
            )
    if not rewrites:
        return list(continuation)
    return [
        {**message, "content": rewrites[index]} if index in rewrites else message
        for index, message in enumerate(continuation)
    ]


_ROLLING_CONTEXT_OPEN = "<cambium-rolling-context>\n"
_ROLLING_CONTEXT_CLOSE = "\n</cambium-rolling-context>"


def _render_rolling_compaction(
    continuation: list[dict[str, Any]], budget: int
) -> list[dict[str, str]]:
    """Render a deterministic bounded continuation summary as user data.

    Tier-1 stripping (§9.1.7) drops obsolete read bodies and superseded
    passing run outputs first; ``_summarize_transcript`` then applies the
    retention and turn-atomic bounding semantics against the exact
    serialized size, so the embedded JSON always parses. The wrapper
    overhead is reserved up front: the ``<cambium-rolling-context>`` closing
    tag is never cut. Compacted, untrusted observations stay user-role data
    and never gain system authority.
    """
    budget = min(budget, MAX_OBSERVATION_BYTES)
    inner_budget = budget - len(_ROLLING_CONTEXT_OPEN) - len(_ROLLING_CONTEXT_CLOSE)

    def measure(messages: list[dict[str, Any]]) -> int:
        return len(
            json.dumps(
                messages,
                ensure_ascii=False,
                separators=(",", ":"),
                sort_keys=True,
            )
        )

    summarized = _summarize_transcript(
        _strip_for_fold(continuation),
        max(0, inner_budget),
        max_messages=MAX_CONTEXT_MESSAGES,
        measure=measure,
        drop_head=True,
    )
    content = (
        _ROLLING_CONTEXT_OPEN
        + json.dumps(summarized, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
        + _ROLLING_CONTEXT_CLOSE
    )
    return [{"role": "user", "content": content}]


def _parent_envelope_lines(parent_envelope: dict[str, Any]) -> str:
    """Render a bounded parent-outcome block appended to the system prompt.

    The block carries the parent's summary, changed files, and commits so a
    child starts from the parent's outcome without inheriting its session.
    Fields are already validated by ``_validate_parent_envelope``; this renders
    them as a compact, deterministic text block.
    """
    lines = ["Parent task context:"]
    if isinstance(parent_envelope.get("parent_task_id"), str):
        lines.append(f"parent: {parent_envelope['parent_task_id']}")
    summary = parent_envelope.get("summary")
    if isinstance(summary, str) and summary.strip():
        lines.append(f"parent summary: {summary}")
    files = parent_envelope.get("files_changed")
    if isinstance(files, list) and files:
        lines.append("parent files changed: " + ", ".join(map(str, files)))
    commits = parent_envelope.get("commits")
    if isinstance(commits, list) and commits:
        lines.append("parent commits: " + ", ".join(map(str, commits)))
    status = parent_envelope.get("status")
    if isinstance(status, str) and status.strip():
        lines.append(f"parent status: {status}")
    return "\n".join(lines)


def _parent_envelope_message(parent_envelope: dict[str, Any]) -> dict[str, str]:
    """Render legacy parent data as a delimited user-role message.

    Parent results are data. Keeping them outside the system message prevents
    a child result from gaining system authority while retaining the existing
    strict nine-key projection.
    """
    return {
        "role": "user",
        "content": (
            "<cambium-parent-context>\n"
            + _parent_envelope_lines(parent_envelope)
            + "\n</cambium-parent-context>"
        ),
    }


def _child_task_lines(task: str, parent_envelope: dict[str, Any] | None) -> str:
    """Render the bounded child task block appended to a forked epoch.

    The child task plus the parent-outcome block become ONE user-role data
    block (plan §5.4): bounded data, never a system directive. Fields are
    already validated by ``_validate_parent_envelope``.
    """
    lines = [f"Child task: {_bounded_text(task, MAX_ENVELOPE_FIELD_CHARS)}"]
    if parent_envelope:
        lines.extend(
            [
                "<cambium-parent-context>",
                _parent_envelope_lines(parent_envelope),
                "</cambium-parent-context>",
            ]
        )
    return "\n".join(lines)


def _child_result_lines(child_result: dict[str, Any]) -> str:
    """Render one bounded strict child-result envelope as a user message.

    The child-result envelope carries exactly the strict nine keys with
    bounded fields, so the rendered block is bounded data under the user
    role (injection posture, plan §11).
    """
    lines = ["Child task result:"]
    status = child_result.get("status")
    if isinstance(status, str) and status.strip():
        lines.append(f"status: {status}")
    summary = child_result.get("summary")
    if isinstance(summary, str) and summary.strip():
        lines.append(f"summary: {summary}")
    files = child_result.get("files_changed")
    if isinstance(files, list) and files:
        lines.append("files changed: " + ", ".join(map(str, files)))
    commits = child_result.get("commits")
    if isinstance(commits, list) and commits:
        lines.append("commits: " + ", ".join(map(str, commits)))
    return "\n".join(lines)


def _build_forked_prompt(checkpoint: ContextCheckpoint, child_task_lines: str) -> dict[str, Any]:
    """Build the fork prompt: checkpoint messages plus one child envelope.

    The child task block is appended as one user-role message so the fork's
    leading messages are byte-identical to the checkpoint the parent sent
    (plan §5.4). The checkpoint's last message is the delegate tool
    observation (user role), so the fork payload never needs a neutral tail.
    """
    messages = copy.deepcopy(checkpoint.full_messages)
    messages.append({"role": "user", "content": child_task_lines})
    return {"messages": messages}


def _fork_prompt(
    base_messages: list[dict[str, Any]] | tuple[dict[str, Any], ...],
    continuation: list[dict[str, Any]],
    tools: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Build each forked-session turn from an immutable base message list.

    ``base_messages`` is never mutated. The caller owns the mutable
    continuation and can therefore prove the checkpoint prefix remains equal
    on every later turn.
    """
    messages = list(copy.deepcopy(base_messages))
    messages.extend(copy.deepcopy(continuation))
    if not messages or messages[-1].get("role") != "user":
        messages.append({"role": "user", "content": "Continue."})
    prompt: dict[str, Any] = {"messages": messages}
    if tools is not None:
        prompt["tools"] = tools
    return prompt


def _resolve_fork_prefix(
    config: AgentConfig, tools: list[dict[str, Any]], model: str
) -> tuple[list[dict[str, Any]] | None, str | None]:
    """Resolve the forked prefix messages, or a durable skip reason.

    Build-time assertions (plan §5.4) each fail closed: a corrupt, missing,
    or incompatible checkpoint returns ``(None, reason)`` so the caller runs
    the legacy fresh-prompt path with ``parent_envelope`` only and reports
    ``context_fork_skipped``; a valid one returns the exact prefix message
    list the child's first prompt starts with.
    """
    if config.context_fork is None:
        return None, None
    descriptor = config.context_fork
    try:
        checkpoint = _load_epoch_checkpoint(
            config, descriptor["checkpoint_ref"], expect_task_id=False
        )
    except ContextForkError as exc:
        return None, str(exc)
    if checkpoint.cache_key.redacted:
        return None, "checkpoint redacted"
    tools_sha256 = _sha256_hex(json.dumps(tools, sort_keys=True).encode("utf-8"))
    cache_key = checkpoint.cache_key
    artifact_fields: dict[str, Any] = {
        "provider": cache_key.provider,
        "model": cache_key.model,
        "protocol": cache_key.protocol,
        "system_sha256": _sha256_hex(
            str(checkpoint.provider_messages[0]["content"]).encode("utf-8")
        ),
        "tools_sha256": cache_key.tools_sha256,
        "prefix_sha256": _messages_sha256(checkpoint.provider_messages),
        "suffix_sha256": _messages_sha256(checkpoint.continuation_suffix),
        "full_sha256": _messages_sha256(checkpoint.full_messages),
        "prefix_bytes": prompt_prefix_bytes({"messages": checkpoint.provider_messages}) or 0,
        "provider_boundary": cache_key.provider_boundary,
    }
    for field in (
        "provider",
        "model",
        "protocol",
        "system_sha256",
        "tools_sha256",
        "prefix_sha256",
        "suffix_sha256",
        "full_sha256",
        "prefix_bytes",
        "provider_boundary",
    ):
        descriptor_value = descriptor.get(field)
        if field == "protocol":
            # The wire descriptor carries protocol in its provider boundary;
            # accept a future top-level field without weakening today's strict
            # descriptor validator.
            descriptor_value = descriptor.get(
                "protocol", descriptor["provider_boundary"].get("protocol")
            )
        if descriptor_value != artifact_fields[field]:
            return None, f"fork descriptor {field} mismatch"
    if descriptor["tools_sha256"] != tools_sha256:
        return None, "tool schema mismatch"
    if cache_key.model != model or descriptor["model"] != model:
        return None, "model mismatch"
    forked = _build_forked_prompt(
        checkpoint, _child_task_lines(config.task, config.parent_envelope)
    )
    try:
        if prompt_prefix_bytes({"messages": checkpoint.provider_messages}) != (
            checkpoint.cache_key.prefix_bytes
        ):
            return None, "prefix byte mismatch"
        if _messages_sha256(checkpoint.provider_messages) != checkpoint.cache_key.prefix_sha256:
            return None, "prefix hash mismatch"
        if _messages_sha256(checkpoint.continuation_suffix) != checkpoint.cache_key.suffix_sha256:
            return None, "suffix hash mismatch"
        if _messages_sha256(checkpoint.full_messages) != checkpoint.cache_key.full_sha256:
            return None, "full checkpoint hash mismatch"
        if _messages_sha256(forked["messages"]) == checkpoint.cache_key.full_sha256:
            return None, "fork full hash unexpectedly equals checkpoint hash"
        if forked["messages"][: len(checkpoint.full_messages)] != checkpoint.full_messages:
            return None, "fork base message mismatch"
        validate_prompt_structure(forked)
    except Exception as exc:  # noqa: BLE001  PromptStructureError and friends
        return None, f"fork prompt invalid: {exc.__class__.__name__}"
    return forked["messages"], None


_PROVIDER_TOOLS_CONFIG = AgentConfig(
    task_id="provider-tools",
    generation=1,
    task="",
    worktree=None,
    base_commit=None,
    fanout_config=None,
    max_turns=1,
    max_tokens=1,
    shell_permission=True,
    network_permission=False,
    heartbeat_interval_s=1.0,
    max_wall_s=1.0,
    checkpoint_root=None,
)


def _provider_task_tools_hash() -> str:
    """The canonical tools-JSON hash for provider tasks.

    The supervisor sends uniform permissions for provider tasks
    (``{"shell": True, "network": False}``), so sibling schemas are identical;
    compatibility asserts this hash, never assumes it (plan §5.5).
    """
    return _sha256_hex(
        json.dumps(_exposed_tool_schemas(_PROVIDER_TOOLS_CONFIG), sort_keys=True).encode("utf-8")
    )


def _fork_cache_compatible(
    child_spec: dict[str, Any],
    epoch: Mapping[str, Any],
    authorized_providers: frozenset[str],
) -> tuple[bool, str | None]:
    """Cache-key compatibility of one child with one epoch (plan §5.5).

    Returns ``(compatible, reason)``. A compatible child is pinned by the
    supervisor before admission resolution; an incompatible one runs exactly
    today's summary-passing path with ``parent_envelope`` only.
    """
    cache_key = epoch.get("cache_key")
    if not isinstance(cache_key, dict):
        return False, "epoch has no cache_key"
    if set(cache_key) < {
        "provider",
        "model",
        "protocol",
        "reasoning_effort",
        "tools_sha256",
        "redacted",
        "provider_boundary",
    }:
        return False, "epoch cache_key is incomplete"
    if cache_key.get("redacted") is True:
        return False, "checkpoint redacted"
    provider = cache_key.get("provider")
    if not isinstance(provider, str) or provider not in authorized_providers:
        return False, f"provider {provider!r} not authorized"
    model = cache_key.get("model")
    fanout = child_spec.get("fanout_config") or {}
    if not isinstance(model, str) or model != fanout.get("model"):
        return False, "child model differs from the epoch model"
    if cache_key.get("tools_sha256") != _provider_task_tools_hash():
        return False, "child tool schema differs from the epoch"
    boundary = cache_key.get("provider_boundary")
    if not isinstance(boundary, dict):
        return False, "epoch provider boundary is missing"
    if boundary.get("provider") != provider or boundary.get("model") != model:
        return False, "provider boundary differs from the epoch"
    child_protocol = fanout.get("protocol")
    if child_protocol is not None and child_protocol != cache_key.get("protocol"):
        return False, "child provider protocol differs from the epoch"
    child_reasoning = fanout.get("reasoning_effort")
    if child_reasoning is not None and child_reasoning != cache_key.get("reasoning_effort"):
        return False, "child reasoning effort differs from the epoch"
    return True, None


def _task_message(task: str) -> dict[str, str]:
    """Render the dynamic task text as delimited user-role data (plan §9.1.6).

    The task is data, not a directive. Keeping it out of the system message
    makes the system directive plus sorted tool schemas byte-identical
    across tasks, so provider exact-prefix caches key on a stable head.
    """
    return {
        "role": "user",
        "content": f"<cambium-task>\nTask: {task}\n</cambium-task>",
    }


def _build_agent_prompt(
    task: str,
    tools: list[dict[str, Any]],
    transcript: list[dict[str, Any]],
    model_identity: str = "",
    parent_envelope: dict[str, Any] | None = None,
) -> dict[str, Any]:
    system_lines = [
        "You are Cambium's autonomous coding agent.",
        "You act inside a disposable git worktree and must complete the task.",
    ]
    if model_identity:
        system_lines.append(
            f"You are running as the configured model {model_identity}. When "
            "asked what model or provider you are, answer truthfully from this "
            "identity and never guess."
        )
    system_lines.extend(SUMMARY_PROTOCOL_LINES)
    system_lines.extend(
        [
            "In normal mode, return exactly one JSON object; it must be one action:",
            '  plan:      {"type": "plan", "steps": ["...", "..."]}',
            '  tool_call: {"type": "tool_call", "name": <tool name>, "arguments": {...}}',
            '  finish:    {"type": "finish", "summary": <non-empty summary>}',
            'An optional "thought" field may be added to the same object to record your '
            "reasoning; the action fields above must remain exact.",
            "Your FIRST action must be a short plan: list the concrete steps before any tool_call.",
            "Approach:",
            "- Reading uses only the batch read tool (read_batch); individual file "
            "reads are unavailable, so read all needed files in one batch call.",
            "- Read the relevant files before editing; verify each change before moving on.",
            "- If a tool call fails, diagnose the error and retry with a corrected call.",
            "- When the task changes code, run the relevant tests via run_shell; only emit "
            "finish after the change is verified and the tests pass. If tests fail, iterate.",
            "- Emit finish only when the task is complete and verified.",
            "Examples:",
            '  {"type": "plan", "steps": ["read src/a.py and src/b.py", "edit src/a.py", '
            '"run tests"]}',
            '  {"type": "tool_call", "name": "read_batch", '
            '"arguments": {"paths": ["src/a.py", "src/b.py"]}}',
            '  {"type": "finish", "summary": "implemented and verified the change"}',
            "Available tools:",
            json.dumps(tools, sort_keys=True),
        ]
    )
    messages = [
        {"role": "system", "content": "\n".join(system_lines)},
        _task_message(task),
    ]
    messages.extend(transcript)
    if parent_envelope:
        messages.append(_parent_envelope_message(parent_envelope))
    if messages[-1].get("role") != "user":
        # Some providers (e.g. ZAI/GLM) reject payloads whose last message is
        # not a user message: a plan action leaves the transcript ending with
        # an assistant message (1214 on the next turn). A neutral user
        # message keeps every payload valid without changing the static
        # system prefix (plan step 3 caching).
        messages.append({"role": "user", "content": "Continue."})
    return {"messages": messages, "tools": tools}


def _tool_observation(name: str, result: ToolResult) -> str:
    body = result.output if result.ok else (result.error or result.output or "")
    return _bounded_text(f"tool {name} ok={result.ok}\n{body}", MAX_OBSERVATION_BYTES)


def _native_tool_action(result: CallResult) -> dict[str, Any] | None:
    """Translate exactly one provider-native function call to Cambium's action ADT."""

    calls = getattr(result, "tool_calls", None)
    if not calls:
        return None
    if len(calls) != 1:
        raise ValueError("provider returned more than one tool call for a sequential turn")
    call = calls[0]
    function = call.get("function") if isinstance(call, dict) else None
    if not isinstance(function, dict):
        raise ValueError("provider native tool call has no function object")
    name = function.get("name")
    arguments = function.get("arguments", {})
    if not isinstance(name, str) or not name:
        raise ValueError("provider native tool call has no function name")
    if isinstance(arguments, str):
        try:
            arguments = json.loads(arguments)
        except json.JSONDecodeError as exc:
            raise ValueError("provider native tool arguments are invalid JSON") from exc
    if not isinstance(arguments, dict):
        raise ValueError("provider native tool arguments must be an object")
    return {"type": "tool_call", "name": name, "arguments": arguments}


def _bind_router_provider(router: Any, result: CallResult, task_id: str) -> None:
    """Bind provider continuity when the concrete router exposes the lease port."""

    binder = getattr(router, "bind_provider", None)
    if callable(binder):
        binder(result.provider, result.model, root_task_id=task_id)


def _canonical_action_message(action: dict[str, Any]) -> dict[str, str]:
    """Persist only the parsed action, never an optional scratchpad/thought."""
    return {
        "role": "assistant",
        "content": json.dumps(action, sort_keys=True, separators=(",", ":")),
    }


def _context_state_message(
    *,
    code_changed: bool,
    verified_after_change: bool,
    verification_failed: bool,
    no_progress_actions: int,
    budget_new_tokens: int,
    previous_prompt_tokens: int,
    turn: int,
) -> dict[str, str]:
    """Render bounded loop state as delimited user-role continuation data."""
    state = {
        "code_changed": code_changed,
        "verified_after_change": verified_after_change,
        "verification_failed": verification_failed,
        "no_progress_actions": no_progress_actions,
        "budget_new_tokens": budget_new_tokens,
        "previous_prompt_tokens": previous_prompt_tokens,
        "turn": turn,
    }
    return {
        "role": "user",
        "content": (
            "<cambium-loop-state>\n"
            + json.dumps(state, sort_keys=True, separators=(",", ":"))
            + "\n</cambium-loop-state>"
        ),
    }


def _safe_cmd(name: str, args: dict[str, Any]) -> str:
    if name == "run_shell":
        cmd = args.get("cmd")
        if isinstance(cmd, list):
            return _cap_utf8(" ".join(str(token) for token in cmd), MAX_CMD_BYTES)
    return _cap_utf8(f"{name} {json.dumps(args, sort_keys=True)}", MAX_CMD_BYTES)


async def _emit_tool_event(
    writer: asyncio.StreamWriter,
    config: AgentConfig,
    name: str,
    args: dict[str, Any],
    turn: int,
    tool_result: ToolResult,
) -> None:
    await send(
        writer,
        {
            "type": "tool_event",
            "task_id": config.task_id,
            "generation": config.generation,
            "tool": name,
            "cmd": _safe_cmd(name, args),
            "turn": turn,
            "ok": bool(tool_result.ok),
            "duration_ms": int(tool_result.duration_ms),
        },
    )


def _prompt_context_usage_fields(prompt: Mapping[str, Any], *, call_kind: str) -> dict[str, Any]:
    """Describe the active prompt shape without exposing prompt content.

    Byte counts use the exact canonical request representation Cambium handed
    to the provider. Token counts remain provider-owned in ``usage``; the UI
    labels byte-derived token estimates as approximate.
    """

    messages = prompt.get("messages")
    if not isinstance(messages, list) or not all(
        isinstance(message, Mapping) for message in messages
    ):
        return {"call_kind": call_kind}
    active_bytes = prompt_prefix_bytes(dict(prompt))
    if active_bytes is None:
        try:
            active_bytes = len(_canonical_json_bytes(list(messages)))
        except (TypeError, ValueError):
            active_bytes = 0
    try:
        trunk, _ = partition_summary_trunk(messages)
    except SummaryTrunkError:
        trunk = list(messages[:2])
    trunk_bytes = prompt_prefix_bytes({"messages": trunk})
    if trunk_bytes is None:
        try:
            trunk_bytes = len(_canonical_json_bytes(trunk))
        except (TypeError, ValueError):
            trunk_bytes = 0
    raw_tail_bytes = max(0, active_bytes - trunk_bytes)
    summary_segments = max(0, len(trunk) - 2)
    return {
        "call_kind": call_kind,
        "active_context_bytes": active_bytes,
        "active_context_messages": len(messages),
        "summary_trunk_bytes": trunk_bytes,
        "summary_segments": summary_segments,
        "raw_tail_bytes": raw_tail_bytes,
    }


def _success_usage_event(
    result: CallResult,
    turn: int,
    *,
    prompt: Mapping[str, Any] | None = None,
    call_kind: str = "agent",
) -> dict[str, Any]:
    """One redacted durable usage event for a completed router call.

    Fields the provider did not report are omitted; a missing field never
    breaks the event or the session (implementation plan step 3).
    """
    estimated_cost = float(result.estimated_cost_usd)
    latency = float(result.latency_s)
    if not math.isfinite(estimated_cost) or not math.isfinite(latency):
        raise ValueError("provider usage metadata contains a non-finite number")
    event: dict[str, Any] = {
        "turn": turn,
        "provider": result.provider,
        "model": result.model,
        "estimated_cost_usd": max(0.0, estimated_cost),
        "latency_s": max(0.0, latency),
        **_prompt_context_usage_fields(prompt or {}, call_kind=call_kind),
    }
    usage = _usage_counts(result.usage)
    if usage:
        event["usage"] = usage
    if result.retry_after_s is not None:
        retry_after = float(result.retry_after_s)
        if not math.isfinite(retry_after):
            raise ValueError("provider usage metadata contains a non-finite retry delay")
        event["retry_after_s"] = max(0.0, retry_after)
    if result.request_rate_status is not None:
        event["request_rate_status"] = result.request_rate_status
    if result.account_quota_owner is not None:
        event["account_quota_owner"] = result.account_quota_owner
    if result.prompt_prefix_bytes is not None:
        event["prompt_prefix_bytes"] = result.prompt_prefix_bytes
    if result.provider_cache_hit is not None:
        event["provider_cache_hit"] = result.provider_cache_hit
    quota_windows = getattr(result, "quota_windows", None)
    if quota_windows is not None:
        event["quota_windows"] = [dict(item) for item in quota_windows]
    return event


def _failure_usage_event(
    exc: BaseException,
    *,
    turn: int,
    model: str | None,
    router: Diffundo,
    prompt: dict[str, Any],
    call_kind: str = "agent",
) -> dict[str, Any]:
    """One redacted durable usage event for a failed router call.

    Carries the terminal failure's provider evidence and the redacted failure
    reason; fields that are unavailable are omitted, never an error.
    """
    event: dict[str, Any] = {
        "turn": turn,
        **_prompt_context_usage_fields(prompt, call_kind=call_kind),
    }
    if isinstance(model, str) and model:
        event["model"] = model
    failure_reason = exc.__class__.__name__
    provider: str | None = None
    retry_after_s: float | None = None
    request_rate_status: str | None = None
    account_quota_owner: str | None = None
    if isinstance(exc, AllProvidersFailed) and isinstance(exc.last_error, ProviderError):
        error = exc.last_error
        provider = error.provider
        retry_after_s = error.retry_after_s
        request_rate_status = error.request_rate_status
        account_quota_owner = error.account_quota_owner
        failure_reason = f"{error.outcome.value}: {error.message}"
    if provider is not None:
        event["provider"] = provider
        if request_rate_status is None:
            try:
                request_rate_status = router.status(provider).value
            except Exception:
                request_rate_status = None
    if retry_after_s is not None:
        event["retry_after_s"] = max(0.0, float(retry_after_s))
    if request_rate_status is not None:
        event["request_rate_status"] = request_rate_status
    if account_quota_owner is not None:
        event["account_quota_owner"] = account_quota_owner
    prefix_bytes = prompt_prefix_bytes(prompt)
    if prefix_bytes is not None:
        event["prompt_prefix_bytes"] = prefix_bytes
    event["failure_reason"] = _cap_utf8(failure_reason, 512)
    return event


async def _emit_usage_event(
    writer: asyncio.StreamWriter,
    config: AgentConfig,
    event: dict[str, Any],
    *,
    epoch: int | None = None,
    fork_of: str | None = None,
) -> None:
    payload: dict[str, Any] = {
        "type": "usage_event",
        "task_id": config.task_id,
        "generation": config.generation,
        **event,
    }
    if epoch is not None:
        payload["epoch"] = epoch
    if fork_of is not None:
        payload["fork_of"] = fork_of
    await send(writer, payload)


def _write_checkpoint_file(
    config: AgentConfig,
    turn: int,
    transcript: list[dict[str, Any]],
    usage: dict[str, int],
    commits_so_far: list[str],
) -> Path | None:
    if config.checkpoint_root is None:
        return None
    directory = config.checkpoint_root / _safe_task_id(config.task_id)
    path = directory / f"turn-{turn:03d}.json"
    payload: dict[str, Any] = {
        "schema": CHECKPOINT_SCHEMA,
        "task": config.task,
        "generation": config.generation,
        "turn": turn,
        "transcript": transcript,
        "usage": usage,
        "commits_so_far": commits_so_far,
    }
    redactor = config.redactor or _checkpoint_redactor(config.provider_env_keys)
    payload = cast(dict[str, Any], redactor.redact_mapping(payload))
    _atomic_json_write(path, json.dumps(payload, sort_keys=True, indent=2))
    return path


async def _emit_checkpoint(
    writer: asyncio.StreamWriter,
    config: AgentConfig,
    turn: int,
    state_ref: Path,
    commits_so_far: list[str],
) -> None:
    await send(
        writer,
        {
            "type": "checkpoint",
            "task_id": config.task_id,
            "generation": config.generation,
            "turn": turn,
            "state_ref": str(state_ref),
            "commits_so_far": commits_so_far,
        },
    )


async def _persist_checkpoint(
    writer: asyncio.StreamWriter,
    config: AgentConfig,
    turn: int,
    transcript: list[dict[str, Any]],
    usage: dict[str, int],
    commits_so_far: list[str],
) -> None:
    path = await asyncio.to_thread(
        _write_checkpoint_file, config, turn, transcript, usage, commits_so_far
    )
    if path is not None:
        await _emit_checkpoint(writer, config, turn, path, commits_so_far)


def _sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _messages_sha256(messages: list[dict[str, Any]]) -> str:
    return _sha256_hex(_canonical_json_bytes(messages))


def _default_provider_boundary(
    config: AgentConfig,
    provider: str | None,
    model: str,
    protocol: str,
    reasoning_effort: str | None,
) -> dict[str, Any]:
    """Build a conservative boundary for in-process/test routers."""
    return {
        "provider": provider or "unknown-provider",
        "endpoint": "unknown-endpoint",
        "authmode": "unknown-auth",
        "api_key_env": "",
        "provider_env_keys": list(config.provider_env_keys),
        "authorized_providers": (
            list(config.authorized_providers) if config.authorized_providers_explicit else None
        ),
        "authorized_providers_explicit": config.authorized_providers_explicit,
        "protocol": protocol or "unknown-protocol",
        "model": model,
        "tier": "unknown-tier",
        "reasoning_effort": reasoning_effort,
        "provider_config_path": "unknown-provider-config",
    }


def _provider_boundary(
    config: AgentConfig,
    provider: Any,
    *,
    provider_config_path: Path,
) -> dict[str, Any]:
    """Describe the exact provider boundary without carrying credentials."""
    auth = getattr(provider, "auth", None)
    protocol = getattr(provider, "protocol", None)
    tier = getattr(provider, "tier", None)
    auth_value = getattr(auth, "value", auth)
    protocol_value = getattr(protocol, "value", protocol)
    tier_value = getattr(tier, "value", tier)
    endpoint = getattr(provider, "base_url", None)
    api_key_env = getattr(provider, "api_key_env", None)
    model = getattr(provider, "model", None)
    name = getattr(provider, "name", None)
    boundary = {
        "provider": name,
        "endpoint": endpoint or "codex-profile",
        "authmode": auth_value,
        "api_key_env": api_key_env or "",
        "provider_env_keys": list(config.provider_env_keys),
        "authorized_providers": (
            list(config.authorized_providers) if config.authorized_providers_explicit else None
        ),
        "authorized_providers_explicit": config.authorized_providers_explicit,
        "protocol": protocol_value,
        "model": model,
        "tier": tier_value,
        "reasoning_effort": getattr(provider, "reasoning_effort", None),
        "provider_config_path": str(provider_config_path),
    }
    return _validate_provider_boundary(boundary)


def _context_message(value: Any, location: str) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != {"role", "content"}:
        raise ContextForkError(f"checkpoint {location} must have exactly role/content")
    role = value.get("role")
    content = value.get("content")
    if role not in {"system", "user", "assistant", "tool"}:
        raise ContextForkError(f"checkpoint {location}.role is invalid")
    if not isinstance(content, str):
        raise ContextForkError(f"checkpoint {location}.content must be a string")
    if len(content.encode("utf-8")) > MAX_OBSERVATION_BYTES:
        raise ContextForkError(f"checkpoint {location}.content exceeds the field cap")
    return {"role": role, "content": content}


def _ensure_private_directory(path: Path) -> None:
    """Create a directory tree without following a symlink component."""
    if not path.is_absolute():
        path = path.absolute()
    current = Path(path.anchor)
    for component in path.parts[1:]:
        current /= component
        try:
            info = current.lstat()
        except FileNotFoundError:
            try:
                current.mkdir(mode=0o700)
            except FileExistsError:
                info = current.lstat()
            else:
                continue
        if stat.S_ISLNK(info.st_mode):
            raise ContextForkError(f"checkpoint directory is a symlink: {current}")
        if not stat.S_ISDIR(info.st_mode):
            raise ContextForkError(f"checkpoint directory is not a directory: {current}")


def _fsync_directory(path: Path) -> None:
    try:
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    except OSError as exc:
        raise ContextForkError("checkpoint directory fsync failed") from exc
    try:
        os.fsync(descriptor)
    except OSError as exc:
        raise ContextForkError("checkpoint directory fsync failed") from exc
    finally:
        os.close(descriptor)


def _create_epoch_checkpoint(path: Path, content: str) -> None:
    """Publish one private checkpoint with an exclusive create operation."""
    encoded = content.encode("utf-8")
    _ensure_private_directory(path.parent)
    try:
        info = path.lstat()
    except FileNotFoundError:
        info = None
    if info is not None:
        if stat.S_ISLNK(info.st_mode):
            raise ContextForkError("checkpoint path is a symlink")
        if not stat.S_ISREG(info.st_mode):
            raise ContextForkError("checkpoint path is not a regular file")
        raise ContextForkError("checkpoint path collision")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(path, flags, 0o600)
    except FileExistsError as exc:
        raise ContextForkError("checkpoint path collision") from exc
    try:
        view = memoryview(encoded)
        while view:
            written = os.write(descriptor, view)
            if written <= 0:
                raise OSError("checkpoint write made no progress")
            view = view[written:]
        os.fchmod(descriptor, 0o600)
        os.fsync(descriptor)
    except OSError as exc:
        raise ContextForkError("checkpoint file write failed") from exc
    finally:
        os.close(descriptor)
    _fsync_directory(path.parent)


def _reject_duplicate_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    values: dict[str, Any] = {}
    for key, value in pairs:
        if key in values:
            raise ValueError("checkpoint contains duplicate JSON fields")
        values[key] = value
    return values


def _reject_json_constant(value: str) -> object:
    raise ValueError(f"checkpoint contains non-standard JSON constant {value!r}")


def _write_epoch_checkpoint(
    config: AgentConfig,
    *,
    turn: int,
    epoch: int,
    messages: list[dict[str, Any]] | None = None,
    provider_messages: list[dict[str, Any]] | None = None,
    continuation_suffix: list[dict[str, Any]] | None = None,
    provider: str | None,
    model: str,
    tools_sha256: str,
    provider_compat: Mapping[str, tuple[str, str | None]] | None = None,
    provider_boundary: Mapping[str, Any] | None = None,
    code_changed: bool = False,
    verified_after_change: bool = False,
    verification_failed: bool = False,
    no_progress_actions: int = 0,
    budget_new_tokens: int = 0,
    previous_prompt_tokens: int = 0,
    cumulative_usage: Mapping[str, int] | None = None,
    wall_deadline: float | None = None,
    created_at: float | None = None,
) -> ContextCheckpoint | None:
    """Write one immutable content-addressed epoch checkpoint (plan §5.2-5.3).

    The checkpoint holds the exact provider-sent messages separately from the
    response continuation. The content address hashes the canonical
    pre-redaction serialization, and a retry may only find identical bytes at
    the same path. Returns ``None`` when no checkpoint root is configured
    (suspend is then impossible and the loop continues).
    """
    if config.checkpoint_root is None:
        return None
    if provider_messages is None:
        provider_messages = messages if messages is not None else []
    if continuation_suffix is None:
        continuation_suffix = []
    provider_messages = [
        _context_message(message, f"provider_messages[{index}]")
        for index, message in enumerate(provider_messages)
    ]
    continuation_suffix = [
        _context_message(message, f"continuation_suffix[{index}]")
        for index, message in enumerate(continuation_suffix)
    ]
    if not provider_messages or provider_messages[0]["role"] != "system":
        raise ContextForkError("checkpoint provider_messages must start with system")
    if isinstance(turn, bool) or not isinstance(turn, int) or turn <= 0:
        raise ContextForkError("checkpoint turn must be positive")
    if isinstance(epoch, bool) or not isinstance(epoch, int) or epoch <= 0:
        raise ContextForkError("checkpoint epoch must be positive")
    for name, value in (
        ("budget_new_tokens", budget_new_tokens),
        ("previous_prompt_tokens", previous_prompt_tokens),
        ("no_progress_actions", no_progress_actions),
    ):
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise ContextForkError(f"checkpoint {name} must be a non-negative integer")
    if cumulative_usage is None:
        cumulative_usage = {}
    if not isinstance(cumulative_usage, Mapping) or any(
        not isinstance(key, str)
        or not isinstance(value, int)
        or isinstance(value, bool)
        or value < 0
        for key, value in cumulative_usage.items()
    ):
        raise ContextForkError("checkpoint cumulative_usage has invalid counts")
    if wall_deadline is None:
        wall_deadline = time.time() + config.max_wall_s
    if (
        isinstance(wall_deadline, bool)
        or not isinstance(wall_deadline, int | float)
        or not math.isfinite(float(wall_deadline))
        or wall_deadline <= 0
    ):
        raise ContextForkError("checkpoint wall_deadline must be finite and positive")
    if provider_compat is None:
        provider_compat = {}
    protocol, reasoning_effort = provider_compat.get(provider or "", ("unknown", None))
    boundary = dict(
        provider_boundary
        or _default_provider_boundary(config, provider, model, protocol, reasoning_effort)
    )
    boundary = _validate_provider_boundary(boundary)
    full_messages = [*provider_messages, *continuation_suffix]
    prefix_sha256 = _messages_sha256(provider_messages)
    suffix_sha256 = _messages_sha256(continuation_suffix)
    full_sha256 = _messages_sha256(full_messages)
    cache_key = CacheKeyDescriptor(
        provider=provider,
        model=model,
        protocol=protocol,
        reasoning_effort=reasoning_effort,
        system_sha256=_sha256_hex(str(provider_messages[0].get("content", "")).encode("utf-8")),
        tools_sha256=tools_sha256,
        prefix_sha256=prefix_sha256,
        suffix_sha256=suffix_sha256,
        full_sha256=full_sha256,
        prefix_bytes=prompt_prefix_bytes({"messages": provider_messages}) or 0,
        message_count=len(provider_messages),
        redacted=False,
        provider_boundary=boundary,
    )
    checkpoint = ContextCheckpoint(
        schema=CHECKPOINT_EPOCH_SCHEMA,
        task_id=config.task_id,
        generation=config.generation,
        epoch=epoch,
        turn=turn,
        created_at=created_at if created_at is not None else time.time(),
        cache_key=cache_key,
        provider_messages=copy.deepcopy(provider_messages),
        continuation_suffix=copy.deepcopy(continuation_suffix),
        checkpoint_ref="",
        code_changed=code_changed,
        verified_after_change=verified_after_change,
        verification_failed=verification_failed,
        no_progress_actions=no_progress_actions,
        budget_new_tokens=budget_new_tokens,
        previous_prompt_tokens=previous_prompt_tokens,
        cumulative_usage=dict(cumulative_usage),
        wall_deadline=float(wall_deadline),
    )
    raw = asdict(checkpoint)
    address_pre = _checkpoint_address(raw)
    safe_task = _safe_task_id(config.task_id)
    prefix = f"epoch-{epoch:03d}-{address_pre}"
    placeholder_ref = f"{safe_task}/{prefix}-{'0' * 16}.json"
    redactor = config.redactor or _checkpoint_redactor(config.provider_env_keys)
    payload = cast(
        dict[str, Any],
        redactor.redact_mapping(
            asdict(
                replace(
                    checkpoint,
                    checkpoint_ref=placeholder_ref,
                )
            )
        ),
    )
    redacted = payload != asdict(replace(checkpoint, checkpoint_ref=placeholder_ref))
    if redacted:
        redacted_provider_messages = payload["provider_messages"]
        redacted_continuation_suffix = payload["continuation_suffix"]
        redacted_cache_key = payload["cache_key"]
        redacted_cache_key.update(
            {
                "system_sha256": _sha256_hex(
                    str(redacted_provider_messages[0]["content"]).encode("utf-8")
                ),
                "prefix_sha256": _messages_sha256(redacted_provider_messages),
                "suffix_sha256": _messages_sha256(redacted_continuation_suffix),
                "full_sha256": _messages_sha256(
                    [
                        *redacted_provider_messages,
                        *redacted_continuation_suffix,
                    ]
                ),
                "prefix_bytes": prompt_prefix_bytes({"messages": redacted_provider_messages}) or 0,
                "message_count": len(redacted_provider_messages),
                "redacted": True,
            }
        )
        redacted_cache_key["provider_boundary"] = _validate_provider_boundary(
            redacted_cache_key["provider_boundary"]
        )
        persisted_cache_key = CacheKeyDescriptor(
            provider=redacted_cache_key["provider"],
            model=redacted_cache_key["model"],
            protocol=redacted_cache_key["protocol"],
            reasoning_effort=redacted_cache_key["reasoning_effort"],
            system_sha256=redacted_cache_key["system_sha256"],
            tools_sha256=redacted_cache_key["tools_sha256"],
            prefix_sha256=redacted_cache_key["prefix_sha256"],
            suffix_sha256=redacted_cache_key["suffix_sha256"],
            full_sha256=redacted_cache_key["full_sha256"],
            prefix_bytes=redacted_cache_key["prefix_bytes"],
            message_count=redacted_cache_key["message_count"],
            redacted=redacted_cache_key["redacted"],
            provider_boundary=redacted_cache_key["provider_boundary"],
        )
    else:
        persisted_cache_key = checkpoint.cache_key
    address_persisted = _checkpoint_address(payload)
    checkpoint_ref = f"{safe_task}/{prefix}-{address_persisted}.json"
    payload["checkpoint_ref"] = checkpoint_ref
    directory = config.checkpoint_root / safe_task
    path = directory / f"{prefix}-{address_persisted}.json"
    _create_epoch_checkpoint(path, json.dumps(payload, sort_keys=True, indent=2))
    return replace(
        checkpoint,
        checkpoint_ref=checkpoint_ref,
        cache_key=persisted_cache_key,
    )


async def _emit_context_checkpoint(
    writer: asyncio.StreamWriter,
    config: AgentConfig,
    checkpoint: ContextCheckpoint,
    request_id: str | None = None,
) -> None:
    cache_key = checkpoint.cache_key
    await send(
        writer,
        {
            "type": "context_checkpoint",
            "request_id": request_id,
            "task_id": config.task_id,
            "generation": config.generation,
            "epoch": checkpoint.epoch,
            "turn": checkpoint.turn,
            "checkpoint_ref": checkpoint.checkpoint_ref,
            "cache_key": {
                "provider": cache_key.provider,
                "model": cache_key.model,
                "protocol": cache_key.protocol,
                "reasoning_effort": cache_key.reasoning_effort,
                "system_sha256": cache_key.system_sha256,
                "tools_sha256": cache_key.tools_sha256,
                "prefix_sha256": cache_key.prefix_sha256,
                "suffix_sha256": cache_key.suffix_sha256,
                "full_sha256": cache_key.full_sha256,
                "prefix_bytes": cache_key.prefix_bytes,
                "message_count": cache_key.message_count,
                "redacted": cache_key.redacted,
                "provider_boundary": cache_key.provider_boundary,
            },
        },
    )


async def _emit_context_epoch_advanced(
    writer: asyncio.StreamWriter,
    config: AgentConfig,
    *,
    request_id: str,
    checkpoint: ContextCheckpoint,
    folded_from_epoch: int,
    reason: str | None,
) -> None:
    await send(
        writer,
        {
            "type": "context_epoch_advanced",
            "request_id": request_id,
            "task_id": config.task_id,
            "generation": config.generation,
            "epoch": checkpoint.epoch,
            "turn": checkpoint.turn,
            "checkpoint_ref": checkpoint.checkpoint_ref,
            "cache_key": asdict(checkpoint.cache_key),
            "folded_from_epoch": folded_from_epoch,
            "reason": reason,
        },
    )


async def _emit_compaction_failed(
    writer: asyncio.StreamWriter,
    config: AgentConfig,
    *,
    request_id: str,
    epoch: int,
    reason: str,
) -> None:
    safe_reason = _cap_utf8(reason, MAX_ENVELOPE_FIELD_CHARS)
    if config.redactor is not None:
        safe_reason = config.redactor.redact_escaped(safe_reason)
        safe_reason = _cap_utf8(safe_reason, MAX_ENVELOPE_FIELD_CHARS)
    await send(
        writer,
        {
            "type": "compaction_failed",
            "request_id": request_id,
            "task_id": config.task_id,
            "generation": config.generation,
            "epoch": epoch,
            "reason": safe_reason,
        },
    )


def _load_epoch_checkpoint(
    config: AgentConfig, checkpoint_ref: str, *, expect_task_id: bool
) -> ContextCheckpoint:
    """Load and integrity-check one epoch checkpoint, or fail closed.

    A missing, corrupt, escaping, or tampered checkpoint raises
    :class:`ContextForkError`; the caller then falls back to the legacy
    fresh-prompt path (fork) or fails the task (resume). The message-list
    hashes are recomputed and compared so no malformed payload ever seeds a
    prompt prefix.
    """
    root = config.checkpoint_root
    if root is None:
        raise ContextForkError("no checkpoint root configured")
    root = root.resolve()
    if not isinstance(checkpoint_ref, str) or not checkpoint_ref:
        raise ContextForkError("invalid checkpoint_ref")
    task_component, ref_epoch, _address_pre, address_persisted = _validate_checkpoint_ref_shape(
        checkpoint_ref
    )
    if expect_task_id and task_component != _safe_task_id(config.task_id):
        raise ContextForkError("invalid checkpoint_ref path")
    relative = Path(checkpoint_ref)
    current = root
    for part in relative.parts:
        current = current / part
        if current.is_symlink():
            raise ContextForkError("checkpoint path is a symlink")
    path = root / relative
    if not path.is_relative_to(root):
        raise ContextForkError("checkpoint_ref escapes the checkpoint root")
    try:
        if path.stat().st_size > MAX_LINE_BYTES * 4:
            raise ContextForkError("checkpoint exceeds the size cap")
    except FileNotFoundError as exc:
        raise ContextForkError("checkpoint unreadable: FileNotFoundError") from exc
    try:
        text = path.read_text(encoding="utf-8")
        data = json.loads(
            text,
            object_pairs_hook=_reject_duplicate_pairs,
            parse_constant=_reject_json_constant,
        )
    except (OSError, ValueError) as exc:
        raise ContextForkError(f"checkpoint unreadable: {exc.__class__.__name__}") from exc
    if not isinstance(data, dict):
        raise ContextForkError("checkpoint is not an object")
    if _checkpoint_address(data) != address_persisted:
        raise ContextForkError("checkpoint persisted-address mismatch")
    if data.get("epoch") != ref_epoch:
        raise ContextForkError("checkpoint epoch does not match its filename")
    expected_keys = frozenset(
        {
            "schema",
            "task_id",
            "generation",
            "epoch",
            "turn",
            "created_at",
            "cache_key",
            "provider_messages",
            "continuation_suffix",
            "checkpoint_ref",
            "code_changed",
            "verified_after_change",
            "verification_failed",
            "no_progress_actions",
            "budget_new_tokens",
            "previous_prompt_tokens",
            "cumulative_usage",
            "wall_deadline",
        }
    )
    if set(data) != expected_keys:
        raise ContextForkError("checkpoint has an invalid key set")
    if data.get("schema") != CHECKPOINT_EPOCH_SCHEMA:
        raise ContextForkError("checkpoint schema mismatch")
    if not isinstance(data.get("task_id"), str) or not data["task_id"]:
        raise ContextForkError("checkpoint task_id invalid")
    if expect_task_id and data.get("task_id") != config.task_id:
        raise ContextForkError("checkpoint task_id mismatch")
    if data.get("checkpoint_ref") != checkpoint_ref:
        raise ContextForkError("checkpoint_ref mismatch")
    generation = data.get("generation")
    if isinstance(generation, bool) or not isinstance(generation, int) or generation <= 0:
        raise ContextForkError("checkpoint generation invalid")
    if expect_task_id and generation != config.generation:
        raise ContextForkError("checkpoint generation mismatch")
    epoch = data.get("epoch")
    if isinstance(epoch, bool) or not isinstance(epoch, int) or epoch <= 0:
        raise ContextForkError("checkpoint epoch invalid")
    turn = data.get("turn")
    if isinstance(turn, bool) or not isinstance(turn, int) or turn <= 0:
        raise ContextForkError("checkpoint turn invalid")
    provider_messages_raw = data.get("provider_messages")
    suffix_raw = data.get("continuation_suffix")
    if not isinstance(provider_messages_raw, list) or not provider_messages_raw:
        raise ContextForkError("checkpoint provider_messages invalid")
    if not isinstance(suffix_raw, list):
        raise ContextForkError("checkpoint continuation_suffix invalid")
    provider_messages = [
        _context_message(message, f"provider_messages[{index}]")
        for index, message in enumerate(provider_messages_raw)
    ]
    continuation_suffix = [
        _context_message(message, f"continuation_suffix[{index}]")
        for index, message in enumerate(suffix_raw)
    ]
    if provider_messages[0]["role"] != "system":
        raise ContextForkError("checkpoint provider_messages must start with system")
    messages = [*provider_messages, *continuation_suffix]
    cache_key = data.get("cache_key")
    if not isinstance(cache_key, dict):
        raise ContextForkError("checkpoint cache_key missing")
    cache_keys = frozenset(
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
    if set(cache_key) != cache_keys:
        raise ContextForkError("checkpoint cache_key has an invalid key set")
    expected = {
        "system_sha256": _sha256_hex(str(provider_messages[0]["content"]).encode("utf-8")),
        "prefix_sha256": _messages_sha256(provider_messages),
        "suffix_sha256": _messages_sha256(continuation_suffix),
        "full_sha256": _messages_sha256(messages),
        "message_count": len(provider_messages),
        "prefix_bytes": prompt_prefix_bytes({"messages": provider_messages}),
    }
    for key, value in expected.items():
        if cache_key.get(key) != value:
            raise ContextForkError(f"checkpoint {key} mismatch")
    try:
        provider = cache_key.get("provider")
        if provider is not None and (not isinstance(provider, str) or not provider):
            raise ContextForkError("checkpoint cache_key provider invalid")
        for key in ("model", "protocol"):
            if not isinstance(cache_key.get(key), str) or not cache_key[key]:
                raise ContextForkError(f"checkpoint cache_key {key} invalid")
        reasoning = cache_key.get("reasoning_effort")
        if reasoning is not None and (not isinstance(reasoning, str) or not reasoning):
            raise ContextForkError("checkpoint cache_key reasoning_effort invalid")
        for key in (
            "system_sha256",
            "tools_sha256",
            "prefix_sha256",
            "suffix_sha256",
            "full_sha256",
        ):
            digest = cache_key.get(key)
            if not isinstance(digest, str) or _SHA256_HEX_RE.fullmatch(digest) is None:
                raise ContextForkError(f"checkpoint cache_key {key} invalid")
        for key in ("prefix_bytes", "message_count"):
            item = cache_key.get(key)
            if isinstance(item, bool) or not isinstance(item, int) or item < 0:
                raise ContextForkError(f"checkpoint cache_key {key} invalid")
        if type(cache_key.get("redacted")) is not bool:
            raise ContextForkError("checkpoint cache_key redacted invalid")
        boundary = _validate_provider_boundary(cache_key.get("provider_boundary"))
        cache_key_descriptor = CacheKeyDescriptor(
            provider=provider,
            model=cache_key["model"],
            protocol=cache_key["protocol"],
            reasoning_effort=reasoning,
            system_sha256=cache_key["system_sha256"],
            tools_sha256=cache_key["tools_sha256"],
            prefix_sha256=cache_key["prefix_sha256"],
            suffix_sha256=cache_key["suffix_sha256"],
            full_sha256=cache_key["full_sha256"],
            prefix_bytes=cache_key["prefix_bytes"],
            message_count=cache_key["message_count"],
            redacted=cache_key["redacted"],
            provider_boundary=boundary,
        )
    except (KeyError, TypeError) as exc:
        raise ContextForkError(f"checkpoint cache_key invalid: {exc}") from exc
    for key in ("code_changed", "verified_after_change", "verification_failed"):
        if type(data.get(key)) is not bool:
            raise ContextForkError(f"checkpoint {key} invalid")
    for key in ("budget_new_tokens", "previous_prompt_tokens", "no_progress_actions"):
        value = data.get(key)
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise ContextForkError(f"checkpoint {key} invalid")
    usage = data.get("cumulative_usage")
    if not isinstance(usage, dict) or any(
        not isinstance(key, str)
        or isinstance(value, bool)
        or not isinstance(value, int)
        or value < 0
        for key, value in usage.items()
    ):
        raise ContextForkError("checkpoint cumulative_usage invalid")
    created_at = data.get("created_at")
    wall_deadline = data.get("wall_deadline")
    if (
        isinstance(created_at, bool)
        or not isinstance(created_at, int | float)
        or not math.isfinite(float(created_at))
        or isinstance(wall_deadline, bool)
        or not isinstance(wall_deadline, int | float)
        or not math.isfinite(float(wall_deadline))
        or wall_deadline <= 0
    ):
        raise ContextForkError("checkpoint time state invalid")
    return ContextCheckpoint(
        schema=CHECKPOINT_EPOCH_SCHEMA,
        task_id=data["task_id"],
        generation=generation,
        epoch=epoch,
        turn=turn,
        created_at=float(created_at),
        cache_key=cache_key_descriptor,
        provider_messages=provider_messages,
        continuation_suffix=continuation_suffix,
        checkpoint_ref=checkpoint_ref,
        code_changed=data["code_changed"],
        verified_after_change=data["verified_after_change"],
        verification_failed=data["verification_failed"],
        no_progress_actions=data["no_progress_actions"],
        budget_new_tokens=data["budget_new_tokens"],
        previous_prompt_tokens=data["previous_prompt_tokens"],
        cumulative_usage=dict(usage),
        wall_deadline=float(wall_deadline),
    )


def _loop_failure_outcome(loop_outcome: dict[str, Any]) -> dict[str, Any]:
    outcome = {
        "status": loop_outcome.get("status", "failed"),
        "failure_reason": loop_outcome.get("failure_reason"),
        "commits": [],
        "files_changed": [],
        "diff": "",
        "diff_truncated": False,
        "summary": loop_outcome.get("summary", "")[:MAX_SUMMARY_CHARS],
    }
    if outcome["status"] == TaskStatus.SUSPENDED.value:
        outcome["epoch"] = loop_outcome.get("epoch")
        outcome["checkpoint_ref"] = loop_outcome.get("checkpoint_ref")
    return outcome


def _cumulative_provider_metadata(loop_outcome: dict[str, Any]) -> dict[str, Any] | None:
    provider = loop_outcome.get("provider")
    model = loop_outcome.get("model")
    usage = loop_outcome.get("usage")
    if not isinstance(provider, str) or not isinstance(model, str) or not isinstance(usage, dict):
        return None
    latency = loop_outcome.get("latency_s", 0.0)
    if (
        isinstance(latency, bool)
        or type(latency) not in (int, float)
        or not math.isfinite(float(latency))
    ):
        return None
    return {
        "provider": provider,
        "model": model,
        "usage": usage,
        "latency_s": max(0.0, float(latency)),
    }


def _do_work_marker(run: dict[str, Any], stop: threading.Event) -> dict[str, Any]:
    """Execute one task: throwaway worktree, one-file edit, commit.

    Returns the outcome dict:

        status          "succeeded" | "failed" | "cancelled"
        failure_reason  str | None (set when status != "succeeded")
        commits         list[str] of SHAs produced
        files_changed   list[str] of paths changed
        diff            ``git diff <base_commit>..HEAD`` in the worktree,
                        capped at ``MAX_DIFF_BYTES`` UTF-8 bytes
        diff_truncated  bool; true when the diff was capped
        summary         worker-authored, <= ``MAX_SUMMARY_CHARS``

    Cooperative cancellation via ``stop``: the worker checks it between git
    steps and reports status "cancelled" if it was set.
    """
    outcome: dict[str, Any] = {
        "status": "failed",
        "failure_reason": None,
        "commits": [],
        "files_changed": [],
        "diff": "",
        "diff_truncated": False,
        "summary": "",
    }
    try:
        scratch = Path(run["scratch_repo"]).resolve()
        worktree = Path(run["worktree_path"]).resolve()
        generation = run.get("generation")
        if isinstance(generation, bool) or not isinstance(generation, int) or generation <= 0:
            outcome["failure_reason"] = "invalid worker generation"
            return outcome
        raw_write_marker = run.get("write_marker", True)
        if not isinstance(raw_write_marker, bool):
            outcome["failure_reason"] = "write_marker must be a boolean"
            return outcome
        write_marker = raw_write_marker
        provider_metadata: dict[str, Any] | None = None

        target_file = run.get("target_file")
        marker = run.get("marker")
        if (
            not isinstance(target_file, str)
            or not target_file
            or not isinstance(marker, str)
            or not marker
        ):
            outcome["failure_reason"] = "marker task requires target_file and marker"
            return outcome

        session_root = scratch.parent
        if not worktree.is_relative_to(session_root):
            outcome["failure_reason"] = (
                f"worktree_path {worktree} outside session scratch root {session_root}"
            )
            return outcome
        target = (worktree / target_file).resolve()
        if not target.is_relative_to(worktree):
            outcome["failure_reason"] = f"target_file {target_file!r} escapes the worktree"
            return outcome

        def guarded_git(*args: str, cwd: str | Path | None = None) -> tuple[int, str, str]:
            _require_generation(worktree, generation)
            if args and args[0] in {"add", "commit"}:
                return _fenced_git(worktree, generation, *args, cwd=cwd)
            return git(*args, cwd=cwd)

        _require_generation(worktree, generation)
        rc, _out, err = guarded_git("rev-parse", "main", cwd=scratch)
        if rc != 0:
            outcome["failure_reason"] = f"no main branch in scratch repo: {err}"
            return outcome
        base_commit = _out

        if not worktree.exists():
            outcome["failure_reason"] = f"worker worktree is missing: {worktree}"
            return outcome
        rc, _out, err = guarded_git("rev-parse", "HEAD", cwd=worktree)
        if rc != 0:
            outcome["failure_reason"] = f"cannot resolve worktree HEAD: {err}"
            return outcome
        worker_identity = secrets.token_hex(16)

        # Optional work_delay_s pauses before the edit (testing hook); the
        # pause polls ``stop`` so cancellation stays responsive.
        delay = float(run.get("work_delay_s", 0.0) or 0.0)
        deadline = time.monotonic() + delay
        while time.monotonic() < deadline:
            if stop.is_set():
                outcome["status"] = "cancelled"
                return outcome
            time.sleep(min(0.05, deadline - time.monotonic()))

        if stop.is_set():
            outcome["status"] = "cancelled"
            return outcome
        _require_generation(worktree, generation)
        if not write_marker:
            outcome["failure_reason"] = "marker not written (write_marker=false)"
            return outcome
        if not target.exists():
            outcome["failure_reason"] = f"target file missing: {target_file}"
            return outcome
        _require_generation(worktree, generation)
        _write_worktree_state(
            worktree,
            generation,
            target,
            target.read_text().rstrip("\n") + "\n" + marker + "\n",
        )
        _require_generation(worktree, generation)
        if marker not in target.read_text():
            outcome["failure_reason"] = "edit missing: marker not present after write"
            return outcome
        if stop.is_set():
            outcome["status"] = "cancelled"
            return outcome

        guarded_git("add", target_file, cwd=worktree)
        rc, _out, err = guarded_git(
            "commit",
            "-m",
            f"cambium-ipc: {run['task_id']}",
            "-m",
            f"Cambium-Worker-Generation: {generation}\nCambium-Worker-Identity: {worker_identity}",
            cwd=worktree,
        )
        if rc != 0:
            outcome["failure_reason"] = f"commit failed: {err}"
            return outcome
        _rc, sha, _err = guarded_git("rev-parse", "HEAD", cwd=worktree)
        _rc, diff, _err = guarded_git("diff", f"{base_commit}..HEAD", cwd=worktree)
        diff, diff_truncated = cap_diff(diff)
        _require_generation(worktree, generation)
        outcome.update(
            status="succeeded",
            failure_reason=None,
            commits=[sha],
            files_changed=[target_file],
            diff=diff,
            diff_truncated=diff_truncated,
            summary=f"appended marker to {target_file}"[:MAX_SUMMARY_CHARS],
            provider_metadata=provider_metadata,
        )
        return outcome
    except GenerationFenceError as exc:
        outcome["failure_reason"] = str(exc)
        return outcome
    except (OSError, subprocess.SubprocessError) as exc:
        outcome["failure_reason"] = f"task crashed: {exc}"
        return outcome


async def do_work(
    run: dict[str, Any],
    stop: threading.Event,
    *,
    config: AgentConfig | None = None,
    writer: asyncio.StreamWriter | None = None,
    progress: AgentProgress | None = None,
) -> dict[str, Any]:
    """Execute one task and return the outcome dict (result-envelope shape).

    With no ``fanout_config`` this is the deterministic marker path
    (``_do_work_marker``); provider-backed tasks run the bounded agent loop
    and then ``_finalize_worktree``.
    """
    if _provider_fanout_config(run) is None:
        return await asyncio.to_thread(_do_work_marker, run, stop)
    if config is None:
        config = _config_from_run(run)
    if progress is None:
        progress = AgentProgress()
    return await _do_provider_work(run, config, stop, writer, progress)


def _fanout_budget_usd(config: dict[str, Any] | None) -> float | None:
    if not isinstance(config, dict):
        return None
    value = config.get("budget_usd")
    if isinstance(value, bool) or type(value) not in (int, float):
        return None
    numeric_value = cast(int | float, value)
    if not math.isfinite(float(numeric_value)) or numeric_value < 0:
        return None
    return float(numeric_value)


def _loop_result(
    outcome: dict[str, Any],
    status: str,
    failure_reason: str | None,
    turn: int,
    cumulative_usage: dict[str, int],
    transcript: list[dict[str, Any]],
) -> dict[str, Any]:
    return {
        **outcome,
        "status": status,
        "failure_reason": failure_reason,
        "turn": max(0, int(turn)),
        "usage": dict(cumulative_usage),
        "transcript": transcript,
    }


async def _run_agent_loop(
    *,
    config: AgentConfig,
    router: Diffundo,
    tier: ProviderTier,
    model: str,
    model_identity: str = "",
    worktree: Path,
    writer: asyncio.StreamWriter | None,
    stop: threading.Event,
    progress: AgentProgress,
    provider_compat: Mapping[str, tuple[str, str | None]] | None = None,
    provider_boundaries: Mapping[str, Mapping[str, Any]] | None = None,
    run_request_id: str | None = None,
    defer_terminal_checkpoint: bool = False,
) -> dict[str, Any]:
    """Bounded provider-backed tool loop: one router call per turn, strict
    action parsing, permission checks, tool dispatch, tool_event + checkpoint.

    With ``config.context_fork`` the first prompt reuses a checkpointed epoch
    prefix; with ``config.resume`` the transcript is seeded from a checkpoint
    plus bounded child-result envelopes. A ``context_reuse`` task suspends at
    a successful ``delegate`` boundary with a durable epoch checkpoint.

    Returns a loop outcome dict: status / failure_reason / summary / turn /
    cumulative usage / provider / latency_s / bounded transcript, plus
    ``epoch``/``checkpoint_ref`` for the suspended status.
    """
    outcome: dict[str, Any] = {
        "status": "failed",
        "failure_reason": None,
        "summary": "",
        "turn": 0,
        "usage": {},
        "provider": None,
        "model": model,
        "latency_s": 0.0,
        "transcript": [],
        "commits_so_far": [],
    }
    absolute_wall_deadline = time.time() + config.max_wall_s
    cumulative_usage: dict[str, int] = {}
    budget_new_tokens = 0
    previous_prompt_tokens = 0
    transcript: list[dict[str, Any]] = []
    tools = _exposed_tool_schemas(config)
    lint_diag = LintDiag()
    budget_usd = _fanout_budget_usd(config.fanout_config)
    no_progress_actions = 0
    verified_after_change = False
    verification_failed = False
    code_changed = False
    base_messages: tuple[dict[str, Any], ...] | None = None
    context_continuation: list[dict[str, Any]] = []
    continuation_suffix: list[dict[str, Any]] = []
    epoch_count = 0
    current_epoch_checkpoint: ContextCheckpoint | None = None
    compaction_armed = True
    usage_epoch: int | None = None
    usage_fork_of: str | None = None
    first_turn = 1
    provider_compat = provider_compat or {}
    provider_boundaries = provider_boundaries or {}

    def _sync_context_transcript() -> None:
        nonlocal transcript
        if base_messages is not None:
            transcript = copy.deepcopy([*base_messages[1:], *context_continuation])

    async def _bound_context_continuation(
        turn: int, *, force: bool = False
    ) -> tuple[bool, str | None]:
        """Flush only the raw tail into one immutable semantic summary entry.

        Existing summary entries remain byte-identical and are never model
        input to the summarized range. Legacy checkpoint transcript material
        is treated as raw tail once, then replaced by the first summary entry.
        """
        nonlocal base_messages, context_continuation, current_epoch_checkpoint
        nonlocal epoch_count, compaction_armed, usage_epoch, cumulative_usage
        nonlocal budget_new_tokens, previous_prompt_tokens

        if base_messages is None:
            return False, None
        try:
            trunk_messages, legacy_tail = partition_summary_trunk(base_messages)
        except SummaryTrunkError as exc:
            return False, str(exc)
        raw_tail = [*legacy_tail, *copy.deepcopy(context_continuation)]
        rolling_gate = (
            config.rolling_compact and config.context_reuse and config.checkpoint_root is not None
        )
        raw_size = _transcript_chars(raw_tail)
        if not force:
            if not rolling_gate:
                bounded = _summarize_transcript(
                    context_continuation,
                    config.max_transcript_chars,
                    max_messages=MAX_CONTEXT_MESSAGES,
                )
                if bounded == context_continuation:
                    return False, None
                context_continuation = bounded
                _sync_context_transcript()
                return True, None
            if raw_size <= config.rolling_compact_threshold_low:
                compaction_armed = True
            if not (
                compaction_armed
                and (
                    raw_size > config.rolling_compact_threshold_high
                    or len(raw_tail) > MAX_CONTEXT_MESSAGES
                )
            ):
                return False, None
        elif not config.context_reuse or config.checkpoint_root is None:
            return False, None
        if not raw_tail:
            return False, None

        request_id = (
            run_request_id
            if isinstance(run_request_id, str) and run_request_id
            else make_request_id("run")
        )
        prior_epoch = epoch_count
        local_checkpoint = (
            current_epoch_checkpoint is not None
            and current_epoch_checkpoint.task_id == config.task_id
            and current_epoch_checkpoint.generation == config.generation
        )
        try:
            summary_through_turn = turn
            existing_entries = summary_entries(trunk_messages)
            if existing_entries:
                summary_through_turn = max(
                    summary_through_turn,
                    existing_entries[-1].through_turn + 1,
                )
            summary_prompt, expectation = build_summary_request(
                trunk_messages,
                raw_tail,
                through_turn=summary_through_turn,
            )
            sent_summary_prompt = copy.deepcopy(summary_prompt)
            try:
                summary_result = await router.call(
                    tier,
                    summary_prompt,
                    model=model,
                    budget_usd=budget_usd,
                )
            except Exception as exc:
                if writer is not None:
                    await _emit_usage_event(
                        writer,
                        config,
                        _failure_usage_event(
                            exc,
                            turn=turn,
                            model=model,
                            router=router,
                            prompt=sent_summary_prompt,
                            call_kind="summary",
                        ),
                        epoch=usage_epoch,
                        fork_of=usage_fork_of,
                    )
                raise ContextForkError(
                    f"summary provider call failed: {exc.__class__.__name__}"
                ) from exc
            if time.monotonic() >= wall_deadline:
                raise ContextForkError("wall budget exceeded during summary flush")
            declared_summary_model = router.declared_model(summary_result.provider)
            if declared_summary_model and summary_result.model != declared_summary_model:
                raise ContextForkError("summary response model mismatch")
            _bind_router_provider(router, summary_result, config.task_id)
            invalid_usage_fields = _invalid_usage_fields(summary_result.usage)
            if invalid_usage_fields:
                raise ContextForkError("summary usage contains invalid token counts")
            summary_total = _usage_total(summary_result.usage)
            if summary_total is None:
                raise ContextForkError("summary usage missing usable token counts")
            if writer is not None:
                await _emit_usage_event(
                    writer,
                    config,
                    _success_usage_event(
                        summary_result,
                        turn,
                        prompt=sent_summary_prompt,
                        call_kind="summary",
                    ),
                    epoch=usage_epoch,
                    fork_of=usage_fork_of,
                )
            cumulative_usage = _accumulate_usage(cumulative_usage, summary_result.usage)
            summary_prompt_tokens = _usage_prompt_tokens(summary_result.usage)
            summary_completion_tokens = _usage_completion_tokens(summary_result.usage)
            if summary_prompt_tokens is None:
                budget_new_tokens += summary_total
                previous_prompt_tokens = 0
            else:
                budget_new_tokens += max(0, summary_prompt_tokens - previous_prompt_tokens)
                previous_prompt_tokens = summary_prompt_tokens
                budget_new_tokens += (
                    summary_completion_tokens
                    if summary_completion_tokens is not None
                    else max(0, summary_total - summary_prompt_tokens)
                )
            if budget_new_tokens > config.max_tokens:
                raise ContextForkError("token budget exceeded during summary flush")
            summary_entry = parse_summary_response(summary_result.content, expectation)
            new_trunk = append_summary_entry(trunk_messages, summary_entry)
            checkpoint = await asyncio.to_thread(
                _write_epoch_checkpoint,
                config,
                turn=turn,
                epoch=prior_epoch + 1,
                provider_messages=copy.deepcopy(new_trunk),
                continuation_suffix=[],
                provider=summary_result.provider,
                model=model,
                tools_sha256=_sha256_hex(json.dumps(tools, sort_keys=True).encode("utf-8")),
                provider_compat=provider_compat,
                provider_boundary=provider_boundaries.get(summary_result.provider),
                code_changed=code_changed,
                verified_after_change=verified_after_change,
                verification_failed=verification_failed,
                no_progress_actions=no_progress_actions,
                budget_new_tokens=budget_new_tokens,
                previous_prompt_tokens=previous_prompt_tokens,
                cumulative_usage=cumulative_usage,
                wall_deadline=absolute_wall_deadline,
            )
            if checkpoint is None:
                raise ContextForkError("summary flush has no checkpoint root")
            checkpoint = _load_epoch_checkpoint(
                config, checkpoint.checkpoint_ref, expect_task_id=True
            )
        except Exception as exc:
            failure_reason = str(exc).strip() or exc.__class__.__name__
            if config.redactor is not None:
                failure_reason = config.redactor.redact_escaped(failure_reason)
            failure_reason = _cap_utf8(failure_reason, MAX_ENVELOPE_FIELD_CHARS)
            if writer is not None:
                await _emit_compaction_failed(
                    writer,
                    config,
                    request_id=request_id,
                    epoch=max(1, prior_epoch),
                    reason=failure_reason,
                )
            return False, failure_reason

        # Publish only after durable checkpoint creation. Prior summary bytes
        # are shared verbatim; the raw tail disappears from the active prompt.
        base_messages = tuple(copy.deepcopy(checkpoint.full_messages))
        context_continuation = []
        current_epoch_checkpoint = checkpoint
        epoch_count = checkpoint.epoch
        usage_epoch = checkpoint.epoch
        compaction_armed = True
        _sync_context_transcript()
        if writer is not None:
            if local_checkpoint:
                await _emit_context_epoch_advanced(
                    writer,
                    config,
                    request_id=request_id,
                    checkpoint=checkpoint,
                    folded_from_epoch=prior_epoch,
                    reason=None,
                )
            else:
                await _emit_context_checkpoint(
                    writer,
                    config,
                    checkpoint,
                    request_id=request_id,
                )
        return True, None

    resume = config.resume
    if resume is not None:
        try:
            resume_checkpoint = _load_epoch_checkpoint(
                config, resume["checkpoint_ref"], expect_task_id=True
            )
            if resume["epoch"] != resume_checkpoint.epoch:
                raise ContextForkError("resume epoch does not match checkpoint")
            if resume_checkpoint.cache_key.redacted:
                raise ContextForkError("checkpoint redacted")
        except ContextForkError as exc:
            return _loop_result(
                outcome,
                "failed",
                f"context_resume_failed: {exc}",
                0,
                cumulative_usage,
                transcript,
            )
        current_epoch_checkpoint = resume_checkpoint
        base_messages = tuple(copy.deepcopy(resume_checkpoint.full_messages))
        context_continuation = [
            {"role": "user", "content": _child_result_lines(child_result)}
            for child_result in resume["child_results"]
        ]
        if resume["child_results_truncated"]:
            context_continuation.append(
                {
                    "role": "user",
                    "content": "[note: some child results were truncated and omitted]",
                }
            )
        code_changed = resume_checkpoint.code_changed
        verified_after_change = resume_checkpoint.verified_after_change
        verification_failed = resume_checkpoint.verification_failed
        no_progress_actions = resume_checkpoint.no_progress_actions
        budget_new_tokens = resume_checkpoint.budget_new_tokens
        previous_prompt_tokens = resume_checkpoint.previous_prompt_tokens
        cumulative_usage = dict(resume_checkpoint.cumulative_usage)
        absolute_wall_deadline = resume_checkpoint.wall_deadline
        first_turn = resume_checkpoint.turn + 1
        epoch_count = resume_checkpoint.epoch
        usage_epoch = resume_checkpoint.epoch
        _sync_context_transcript()
    elif config.context_fork is not None:
        fork_messages, fork_skip = _resolve_fork_prefix(config, tools, model)
        if fork_messages is not None:
            try:
                fork_checkpoint = _load_epoch_checkpoint(
                    config, config.context_fork["checkpoint_ref"], expect_task_id=False
                )
            except ContextForkError as exc:  # pragma: no cover - already validated
                fork_skip = str(exc)
            else:
                current_epoch_checkpoint = fork_checkpoint
                base_messages = tuple(copy.deepcopy(fork_checkpoint.full_messages))
                context_continuation = [copy.deepcopy(fork_messages[-1])]
                epoch_count = fork_checkpoint.epoch
                usage_epoch = fork_checkpoint.epoch
                usage_fork_of = fork_checkpoint.checkpoint_ref
                _sync_context_transcript()
        if fork_skip is not None and writer is not None:
            await send(
                writer,
                {
                    "type": "context_fork_skipped",
                    "request_id": run_request_id,
                    "task_id": config.task_id,
                    "generation": config.generation,
                    "reason": fork_skip,
                },
            )

    if base_messages is None and config.summary_trunk_ref is not None:
        try:
            semantic_checkpoint = _load_epoch_checkpoint(
                config, config.summary_trunk_ref, expect_task_id=False
            )
            if semantic_checkpoint.cache_key.redacted:
                raise ContextForkError("checkpoint redacted")
            summaries = semantic_summary_messages(semantic_checkpoint.full_messages)
            semantic_prompt = _build_agent_prompt(
                config.task,
                tools,
                summaries,
                model_identity,
                parent_envelope=config.parent_envelope,
            )
            semantic_trunk, semantic_tail = partition_summary_trunk(semantic_prompt["messages"])
            base_messages = tuple(copy.deepcopy(semantic_trunk))
            context_continuation = copy.deepcopy(semantic_tail)
            usage_fork_of = config.summary_trunk_ref
            _sync_context_transcript()
        except (ContextForkError, SummaryTrunkError) as exc:
            if writer is not None:
                await send(
                    writer,
                    {
                        "type": "context_fork_skipped",
                        "request_id": run_request_id,
                        "task_id": config.task_id,
                        "generation": config.generation,
                        "reason": f"semantic summary reuse failed: {exc}",
                    },
                )

    # The main agent starts in trunk mode too. The stable head is frozen once;
    # later summaries extend it and the raw working tail remains separate.
    if base_messages is None and config.context_reuse and config.checkpoint_root is not None:
        initial_prompt = _build_agent_prompt(
            config.task,
            tools,
            [],
            model_identity,
            parent_envelope=config.parent_envelope,
        )
        initial_trunk, initial_tail = partition_summary_trunk(initial_prompt["messages"])
        base_messages = tuple(copy.deepcopy(initial_trunk))
        context_continuation = copy.deepcopy(initial_tail)
        _sync_context_transcript()

    wall_deadline = time.monotonic() + max(0.0, absolute_wall_deadline - time.time())
    try:
        for turn in range(first_turn, config.max_turns + 1):
            progress.turn = turn
            progress.status = "working"
            if stop.is_set():
                return _loop_result(
                    outcome, "cancelled", None, turn - 1, cumulative_usage, transcript
                )
            if time.monotonic() >= wall_deadline:
                return _loop_result(
                    outcome,
                    "failed",
                    "wall budget exceeded",
                    turn - 1,
                    cumulative_usage,
                    transcript,
                )
            _require_generation(worktree, config.generation)
            if base_messages is None:
                transcript = _summarize_transcript(transcript, config.max_transcript_chars)
                prompt = _build_agent_prompt(
                    config.task,
                    tools,
                    transcript,
                    model_identity,
                    parent_envelope=config.parent_envelope,
                )
            else:
                # The tuple is immutable and every message is deep-copied at
                # the prompt boundary. No later turn can rewrite the epoch
                # prefix in place.
                if config.context_reuse:
                    _folded, compaction_failure = await _bound_context_continuation(turn)
                    if compaction_failure is not None:
                        return _loop_result(
                            outcome,
                            "failed",
                            f"compaction_failed: {compaction_failure}",
                            turn - 1,
                            cumulative_usage,
                            transcript,
                        )
                prompt = _fork_prompt(base_messages, context_continuation, tools)
            # Keep the object handed to the router immutable for checkpointing.
            # A provider adapter is allowed to normalize its local request, but
            # the epoch must describe the exact object Cambium submitted.
            sent_prompt = copy.deepcopy(prompt)
            try:
                result = await router.call(tier, prompt, model=model, budget_usd=budget_usd)
            except Exception as exc:
                if writer is not None:
                    await _emit_usage_event(
                        writer,
                        config,
                        _failure_usage_event(
                            exc,
                            turn=turn,
                            model=model,
                            router=router,
                            prompt=sent_prompt,
                            call_kind="agent",
                        ),
                        epoch=usage_epoch,
                        fork_of=usage_fork_of,
                    )
                return _loop_result(
                    outcome,
                    "failed",
                    f"provider call failed: {exc.__class__.__name__}",
                    turn - 1,
                    cumulative_usage,
                    transcript,
                )
            if time.monotonic() >= wall_deadline:
                return _loop_result(
                    outcome,
                    "failed",
                    "wall budget exceeded",
                    turn,
                    cumulative_usage,
                    transcript,
                )
            declared_model = router.declared_model(result.provider)
            if declared_model and result.model != declared_model:
                return _loop_result(
                    outcome,
                    "failed",
                    "provider response model mismatch",
                    turn - 1,
                    cumulative_usage,
                    transcript,
                )
            _bind_router_provider(router, result, config.task_id)
            invalid_usage_fields = _invalid_usage_fields(result.usage)
            if invalid_usage_fields:
                return _loop_result(
                    outcome,
                    "failed",
                    "provider usage contains invalid token counts",
                    turn - 1,
                    cumulative_usage,
                    transcript,
                )
            total = _usage_total(result.usage)
            if total is None:
                return _loop_result(
                    outcome,
                    "failed",
                    "provider usage missing usable token counts",
                    turn - 1,
                    cumulative_usage,
                    transcript,
                )
            if writer is not None:
                await _emit_usage_event(
                    writer,
                    config,
                    _success_usage_event(result, turn, prompt=sent_prompt, call_kind="agent"),
                    epoch=usage_epoch,
                    fork_of=usage_fork_of,
                )
            cumulative_usage = _accumulate_usage(cumulative_usage, result.usage)
            prompt_tokens = _usage_prompt_tokens(result.usage)
            completion_tokens = _usage_completion_tokens(result.usage)
            if prompt_tokens is None:
                budget_new_tokens += total
                previous_prompt_tokens = 0
            else:
                budget_new_tokens += max(0, prompt_tokens - previous_prompt_tokens)
                previous_prompt_tokens = prompt_tokens
                budget_new_tokens += (
                    completion_tokens
                    if completion_tokens is not None
                    else max(0, total - prompt_tokens)
                )
            if budget_new_tokens > config.max_tokens:
                return _loop_result(
                    outcome,
                    "failed",
                    "token budget exceeded",
                    turn,
                    cumulative_usage,
                    transcript,
                )
            try:
                action = _native_tool_action(result) or _parse_agent_action(result.content)
            except ValueError as exc:
                invalid_messages = [
                    {"role": "assistant", "content": "[invalid action omitted]"},
                    {
                        "role": "user",
                        "content": _bounded_text(f"invalid action: {exc}", MAX_OBSERVATION_BYTES),
                    },
                ]
                if base_messages is None:
                    transcript.extend(invalid_messages)
                else:
                    context_continuation.extend(invalid_messages)
                    _sync_context_transcript()
                no_progress_actions += 1
                if no_progress_actions > MAX_CONSECUTIVE_PLANS:
                    return _loop_result(
                        outcome,
                        "failed",
                        f"agent made no progress: {no_progress_actions} consecutive "
                        "actions without a tool call",
                        turn,
                        cumulative_usage,
                        transcript,
                    )
                continue
            trailing = _action_trailing(result.content)
            action_message = _canonical_action_message(action)
            if action["type"] == "plan":
                no_progress_actions += 1
                if no_progress_actions > MAX_CONSECUTIVE_PLANS:
                    return _loop_result(
                        outcome,
                        "failed",
                        f"agent made no progress: {no_progress_actions} consecutive "
                        "actions without a tool call",
                        turn,
                        cumulative_usage,
                        transcript,
                    )
                if base_messages is None:
                    transcript.append(action_message)
                    if trailing:
                        transcript.append({"role": "user", "content": _TRAILING_ACTION_NOTE})
                else:
                    context_continuation.append(action_message)
                    if trailing:
                        context_continuation.append(
                            {"role": "user", "content": _TRAILING_ACTION_NOTE}
                        )
                    context_continuation.append({"role": "user", "content": "Continue."})
                    _sync_context_transcript()
                progress.tool = "plan"
                continue
            no_progress_actions = 0
            if action["type"] == "finish":
                if base_messages is None:
                    transcript.append(action_message)
                else:
                    context_continuation.append(action_message)
                    _sync_context_transcript()
                if code_changed and not verified_after_change:
                    reason = (
                        "finish rejected: you changed code but did not run a "
                        "successful verification command; run the tests (e.g. "
                        "run_shell) before finishing"
                        if not verification_failed
                        else (
                            "finish rejected: your verification command failed; "
                            "run the tests successfully (e.g. run_shell) before "
                            "finishing"
                        )
                    )
                    if base_messages is None:
                        transcript.append({"role": "user", "content": reason})
                    else:
                        context_continuation.append({"role": "user", "content": reason})
                        _sync_context_transcript()
                    progress.tool = "finish"
                    continue

                # The root/parent performs one additional summary call at the
                # terminal boundary. Forked children return their strict result
                # envelope instead of publishing a competing parent trunk.
                terminal_summary_flushed = False
                terminal_had_local_checkpoint = (
                    current_epoch_checkpoint is not None
                    and current_epoch_checkpoint.task_id == config.task_id
                    and current_epoch_checkpoint.generation == config.generation
                )
                if (
                    config.context_reuse
                    and usage_fork_of is None
                    and base_messages is not None
                    and config.checkpoint_root is not None
                ):
                    _folded, compaction_failure = await _bound_context_continuation(
                        turn, force=True
                    )
                    if compaction_failure is not None:
                        return _loop_result(
                            outcome,
                            "failed",
                            f"compaction_failed: {compaction_failure}",
                            turn,
                            cumulative_usage,
                            transcript,
                        )
                    terminal_summary_flushed = _folded
                terminal_checkpoint_already_emitted = (
                    terminal_summary_flushed and not terminal_had_local_checkpoint
                )

                # A fresh root flush emits its terminal context checkpoint
                # directly. A resumed local trunk emits context_epoch_advanced,
                # so retain the historical terminal checkpoint after that flush.
                terminal_provider = result.provider
                terminal_boundary = provider_boundaries.get(result.provider)
                terminal_compat = provider_compat
                terminal_messages = copy.deepcopy(sent_prompt["messages"])
                terminal_suffix = [copy.deepcopy(action_message)]
                if base_messages is not None and current_epoch_checkpoint is not None:
                    terminal_key = current_epoch_checkpoint.cache_key
                    terminal_provider = terminal_key.provider or result.provider
                    terminal_boundary = terminal_key.provider_boundary
                    terminal_compat = (
                        {
                            terminal_provider: (
                                terminal_key.protocol,
                                terminal_key.reasoning_effort,
                            )
                        }
                        if terminal_provider is not None
                        else {}
                    )
                    terminal_messages = copy.deepcopy(list(base_messages))
                    terminal_suffix = []
                if (
                    config.context_reuse
                    and usage_fork_of is None
                    and not terminal_checkpoint_already_emitted
                ):
                    epoch_count += 1
                    terminal_epoch = {
                        "turn": turn,
                        "epoch": epoch_count,
                        "provider_messages": terminal_messages,
                        "continuation_suffix": terminal_suffix,
                        "provider": terminal_provider,
                        "model": model,
                        "tools_sha256": _sha256_hex(
                            json.dumps(tools, sort_keys=True).encode("utf-8")
                        ),
                        "provider_compat": terminal_compat,
                        "provider_boundary": terminal_boundary,
                        "code_changed": code_changed,
                        "verified_after_change": verified_after_change,
                        "verification_failed": verification_failed,
                        "no_progress_actions": no_progress_actions,
                        "budget_new_tokens": budget_new_tokens,
                        "previous_prompt_tokens": previous_prompt_tokens,
                        "cumulative_usage": cumulative_usage,
                        "wall_deadline": absolute_wall_deadline,
                    }
                    if defer_terminal_checkpoint:
                        outcome["_terminal_epoch"] = terminal_epoch
                    else:
                        terminal_checkpoint = await asyncio.to_thread(
                            _write_epoch_checkpoint, config, **terminal_epoch
                        )
                        if terminal_checkpoint is not None and writer is not None:
                            await _emit_context_checkpoint(
                                writer,
                                config,
                                terminal_checkpoint,
                                request_id=run_request_id,
                            )
                return {
                    **outcome,
                    "status": "succeeded",
                    "summary": action["summary"],
                    "turn": turn,
                    "usage": cumulative_usage,
                    "provider": terminal_provider,
                    "latency_s": max(0.0, float(result.latency_s)),
                    "transcript": transcript,
                }
            name, arguments = action["name"], action["arguments"]
            denial = _permission_denied(name, arguments, config)
            if denial is not None:
                denied_messages = [
                    action_message,
                    {"role": "user", "content": f"action rejected: {denial}"},
                ]
                if base_messages is None:
                    transcript.extend(denied_messages)
                else:
                    context_continuation.extend(denied_messages)
                    _sync_context_transcript()
                progress.tool = name
                continue
            if stop.is_set():
                return _loop_result(
                    outcome, "cancelled", None, turn - 1, cumulative_usage, transcript
                )
            progress.tool = name
            with ToolContext(
                worktree,
                lint=lint_diag,
                policy=ToolPermissionPolicy(
                    shell=config.shell_permission,
                    network=config.network_permission,
                ),
            ) as ctx:
                tool_result = await run_tool(name, arguments, ctx)
            if name == "delegate" and tool_result.ok and writer is not None:
                await _emit_delegated_child(writer, config, arguments, request_id=run_request_id)
            if tool_result.ok:
                if name in ("write_file", "edit_file"):
                    code_changed = True
                    verified_after_change = False
                    verification_failed = False
                elif name == "run_shell":
                    verified_after_change = True
                    verification_failed = False
            elif name == "run_shell":
                verification_failed = True
                verified_after_change = False
            observation = {"role": "user", "content": _tool_observation(name, tool_result)}
            if base_messages is None:
                transcript.append(action_message)
                if trailing:
                    transcript.append({"role": "user", "content": _TRAILING_ACTION_NOTE})
                transcript.append(observation)
            else:
                continuation_suffix = [action_message]
                if trailing:
                    continuation_suffix.append({"role": "user", "content": _TRAILING_ACTION_NOTE})
                continuation_suffix.append(observation)
                state_message = _context_state_message(
                    code_changed=code_changed,
                    verified_after_change=verified_after_change,
                    verification_failed=verification_failed,
                    no_progress_actions=no_progress_actions,
                    budget_new_tokens=budget_new_tokens,
                    previous_prompt_tokens=previous_prompt_tokens,
                    turn=turn,
                )
                continuation_suffix.append(state_message)
                context_continuation.extend(copy.deepcopy(continuation_suffix))
                _sync_context_transcript()
            if writer is not None:
                await _emit_tool_event(writer, config, name, arguments, turn, tool_result)
                await _persist_checkpoint(writer, config, turn, transcript, cumulative_usage, [])
            if config.context_reuse and name == "delegate" and tool_result.ok:
                checkpoint: ContextCheckpoint | None = None
                checkpoint_was_emitted = False
                if base_messages is not None and config.checkpoint_root is not None:
                    _folded, compaction_failure = await _bound_context_continuation(
                        turn, force=True
                    )
                    if compaction_failure is not None:
                        return _loop_result(
                            outcome,
                            "failed",
                            f"compaction_failed: {compaction_failure}",
                            turn,
                            cumulative_usage,
                            transcript,
                        )
                    checkpoint = current_epoch_checkpoint
                    checkpoint_was_emitted = checkpoint is not None
                if checkpoint is None:
                    epoch_count += 1
                    if base_messages is None:
                        continuation_suffix = [
                            _canonical_action_message(action),
                            *(
                                [{"role": "user", "content": _TRAILING_ACTION_NOTE}]
                                if trailing
                                else []
                            ),
                            observation,
                            _context_state_message(
                                code_changed=code_changed,
                                verified_after_change=verified_after_change,
                                verification_failed=verification_failed,
                                no_progress_actions=no_progress_actions,
                                budget_new_tokens=budget_new_tokens,
                                previous_prompt_tokens=previous_prompt_tokens,
                                turn=turn,
                            ),
                        ]
                    checkpoint = await asyncio.to_thread(
                        _write_epoch_checkpoint,
                        config,
                        turn=turn,
                        epoch=epoch_count,
                        provider_messages=copy.deepcopy(sent_prompt["messages"]),
                        continuation_suffix=continuation_suffix,
                        provider=result.provider,
                        model=model,
                        tools_sha256=_sha256_hex(json.dumps(tools, sort_keys=True).encode("utf-8")),
                        provider_compat=provider_compat,
                        provider_boundary=provider_boundaries.get(result.provider),
                        code_changed=code_changed,
                        verified_after_change=verified_after_change,
                        verification_failed=verification_failed,
                        no_progress_actions=no_progress_actions,
                        budget_new_tokens=budget_new_tokens,
                        previous_prompt_tokens=previous_prompt_tokens,
                        cumulative_usage=cumulative_usage,
                        wall_deadline=absolute_wall_deadline,
                    )
                if checkpoint is not None:
                    if writer is not None and not checkpoint_was_emitted:
                        await _emit_context_checkpoint(
                            writer, config, checkpoint, request_id=run_request_id
                        )
                    return {
                        **outcome,
                        "status": TaskStatus.SUSPENDED.value,
                        "turn": turn,
                        "usage": cumulative_usage,
                        "provider": checkpoint.cache_key.provider or result.provider,
                        "latency_s": max(0.0, float(result.latency_s)),
                        "transcript": transcript,
                        "epoch": checkpoint.epoch,
                        "checkpoint_ref": checkpoint.checkpoint_ref,
                    }
        return _loop_result(
            outcome,
            "failed",
            f"max turns exceeded ({config.max_turns})",
            config.max_turns,
            cumulative_usage,
            transcript,
        )
    except GenerationFenceError as exc:
        return _loop_result(
            outcome, "failed", str(exc), progress.turn, cumulative_usage, transcript
        )


async def _do_provider_work(
    run: dict[str, Any],
    config: AgentConfig,
    stop: threading.Event,
    writer: asyncio.StreamWriter | None,
    progress: AgentProgress,
) -> dict[str, Any]:
    worktree = Path(run["worktree_path"]).resolve()
    session_root = Path(run["scratch_repo"]).resolve().parent
    if not worktree.is_relative_to(session_root):
        return _loop_failure_outcome(
            {
                "status": "failed",
                "failure_reason": (
                    f"worktree_path {worktree} outside session scratch root {session_root}"
                ),
            }
        )
    if not worktree.exists():
        return _loop_failure_outcome(
            {
                "status": "failed",
                "failure_reason": f"worker worktree is missing: {worktree}",
            }
        )
    try:
        fanout_config = cast(dict[str, Any], config.fanout_config)
        router, tier, model, model_identity = _provider_router(
            fanout_config,
            assigned_provider=config.assigned_provider,
            authorized_providers=config.authorized_providers,
            authorized_providers_explicit=config.authorized_providers_explicit,
            debt=config.debt,
            task_id=config.task_id,
            requirements=config.requirements,
        )
    except Exception as exc:
        return _loop_failure_outcome(
            {
                "status": "failed",
                "failure_reason": f"provider routing failed: {exc.__class__.__name__}",
            }
        )
    try:
        provider_path = _provider_path()
        configured_providers = load_providers(provider_path)
        provider_compat = {
            p.name: (p.protocol.value, p.reasoning_effort) for p in configured_providers
        }
        provider_boundaries = {
            p.name: _provider_boundary(config, p, provider_config_path=provider_path)
            for p in configured_providers
        }
    except Exception:
        provider_compat = {}
        provider_boundaries = {}
    worker_identity = secrets.token_hex(16)
    loop_outcome = await _run_agent_loop(
        config=config,
        router=router,
        tier=tier,
        model=model,
        model_identity=model_identity,
        worktree=worktree,
        writer=writer,
        stop=stop,
        progress=progress,
        provider_compat=provider_compat,
        provider_boundaries=provider_boundaries,
        run_request_id=run.get("request_id"),
        defer_terminal_checkpoint=True,
    )
    if loop_outcome["status"] != "succeeded":
        return _loop_failure_outcome(loop_outcome)
    outcome = await asyncio.to_thread(
        _finalize_worktree,
        run=run,
        config=config,
        worktree=worktree,
        generation=config.generation,
        worker_identity=worker_identity,
        stop=stop,
        loop_outcome=loop_outcome,
    )
    final_checkpoint = outcome.pop("_checkpoint_path", None)
    terminal_checkpoint = outcome.pop("_context_checkpoint", None)
    if writer is not None and final_checkpoint is not None:
        await _emit_checkpoint(
            writer,
            config,
            loop_outcome.get("turn", 0),
            Path(final_checkpoint),
            outcome.get("commits", []),
        )
    if writer is not None and terminal_checkpoint is not None:
        await _emit_context_checkpoint(
            writer, config, terminal_checkpoint, request_id=run.get("request_id")
        )
    return outcome


def _finalize_worktree(
    *,
    run: dict[str, Any],
    config: AgentConfig,
    worktree: Path,
    generation: int,
    worker_identity: str,
    stop: threading.Event,
    loop_outcome: dict[str, Any],
) -> dict[str, Any]:
    """Stage the agent's changed files (excluding ``.cambium/``) and make at
    most ONE fenced worker-owned commit with generation + identity trailers.
    A clean worktree succeeds as a no-op only while HEAD still resolves to
    the base commit; such a true no-op receives no empty commit and writes
    no ordinary final checkpoint. Returns the result-envelope shape: model summary +
    cumulative safe provider metadata.

    The commit message, envelope, state paths, and provider metadata are all
    worker-authored; no model-controlled value reaches any of them.
    """
    outcome: dict[str, Any] = {
        "status": "failed",
        "failure_reason": None,
        "commits": [],
        "files_changed": [],
        "diff": "",
        "diff_truncated": False,
        "summary": loop_outcome.get("summary", "")[:MAX_SUMMARY_CHARS],
    }
    provider_metadata = _cumulative_provider_metadata(loop_outcome)
    if provider_metadata is not None:
        outcome["provider_metadata"] = provider_metadata

    def _write_terminal_epoch() -> ContextCheckpoint | None:
        terminal_epoch = loop_outcome.get("_terminal_epoch")
        if not config.context_reuse or not isinstance(terminal_epoch, dict):
            return None
        return _write_epoch_checkpoint(config, **terminal_epoch)

    try:
        if stop.is_set():
            outcome["status"] = "cancelled"
            return outcome
        _require_generation(worktree, generation)
        scratch = Path(run["scratch_repo"]).resolve()
        base_commit = config.base_commit
        if not base_commit:
            rc, base, err = git("rev-parse", "main", cwd=scratch)
            if rc != 0:
                outcome["failure_reason"] = f"no main branch in scratch repo: {err}"
                return outcome
            base_commit = base
        rc, head_sha, err = git("rev-parse", "HEAD", cwd=worktree)
        if rc != 0:
            outcome["failure_reason"] = f"cannot resolve worktree HEAD: {err}"
            return outcome
        _require_generation(worktree, generation)
        status_proc = subprocess.run(
            [
                "git",
                "-c",
                "core.hooksPath=/dev/null",
                "status",
                "--porcelain=v1",
                "--untracked-files=all",
                "--ignored=matching",
            ],
            cwd=worktree,
            capture_output=True,
            text=True,
            env=scrub_environment(),
        )
        if status_proc.returncode != 0:
            outcome["failure_reason"] = f"git status failed: {status_proc.stderr.strip()}"
            return outcome
        changed: list[str] = []
        ignored: list[str] = []
        for line in status_proc.stdout.splitlines():
            path = line[3:].strip() if len(line) > 3 else line.strip()
            if " -> " in path:
                path = path.split(" -> ", 1)[1]
            if not path or path == ".cambium" or path.startswith(".cambium/"):
                continue
            if is_cache_artifact_path(path):
                continue
            if line[:2] == "!!":
                ignored.append(path)
                continue
            changed.append(path)
        # A provider can commit directly (e.g. via permitted shell): HEAD then
        # no longer matches the base commit, whether or not the worktree is
        # dirty. Never publish such unfenced commits — the fenced-commit path
        # would stack a fenced commit on top of them and the supervisor would
        # merge both. Resolve the base once and require worktree HEAD to equal
        # it on every publish path.
        rc, resolved_base, err = git(
            "rev-parse", "--verify", f"{base_commit}^{{commit}}", cwd=worktree
        )
        if rc != 0:
            outcome["failure_reason"] = f"cannot resolve base_commit {base_commit}: {err}"
            return outcome
        if head_sha != resolved_base:
            outcome["failure_reason"] = (
                f"worktree HEAD {head_sha} advanced beyond base_commit "
                f"{resolved_base}; refusing to publish unverified changes"
            )
            return outcome
        if ignored:
            outcome["failure_reason"] = (
                f"worktree contains ignored changes {ignored!r}; refusing to publish "
                "unverified changes"
            )
            return outcome
        if not changed:
            _require_generation(worktree, generation)
            terminal_checkpoint = _write_terminal_epoch()
            # True no-op: no ordinary final checkpoint is written. The summary
            # stays in the envelope, while context reuse still records the
            # terminal provider-turn boundary.
            outcome.update(
                status="succeeded",
                failure_reason=None,
                commits=[],
                files_changed=[],
                diff="",
                diff_truncated=False,
                summary=(loop_outcome.get("summary") or f"completed {config.task_id}")[
                    :MAX_SUMMARY_CHARS
                ],
            )
            if terminal_checkpoint is not None:
                outcome["_context_checkpoint"] = terminal_checkpoint
            return outcome
        for path in changed:
            _require_generation(worktree, generation)
            rc, _out, err = _fenced_git(worktree, generation, "add", "--", path, cwd=worktree)
            if rc != 0:
                outcome["failure_reason"] = f"git add failed for {path}: {err}"
                return outcome
        _require_generation(worktree, generation)
        rc, _out, err = _fenced_git(
            worktree,
            generation,
            "commit",
            "-m",
            f"cambium-agent: {config.task_id}",
            "-m",
            f"Cambium-Worker-Generation: {generation}\nCambium-Worker-Identity: {worker_identity}",
            cwd=worktree,
        )
        if rc != 0:
            outcome["failure_reason"] = f"commit failed: {err}"
            return outcome
        _rc, sha, _err = git("rev-parse", "HEAD", cwd=worktree)
        _rc, diff, _err = git("diff", f"{resolved_base}..HEAD", cwd=worktree)
        diff, diff_truncated = cap_diff(diff)
        _require_generation(worktree, generation)
        final_checkpoint = _write_checkpoint_file(
            config,
            loop_outcome.get("turn", 0),
            loop_outcome.get("transcript", []),
            loop_outcome.get("usage", {}),
            [sha],
        )
        terminal_checkpoint = _write_terminal_epoch()
        outcome.update(
            status="succeeded",
            failure_reason=None,
            commits=[sha],
            files_changed=changed,
            diff=diff,
            diff_truncated=diff_truncated,
            summary=(loop_outcome.get("summary") or f"completed {config.task_id}")[
                :MAX_SUMMARY_CHARS
            ],
        )
        if final_checkpoint is not None:
            outcome["_checkpoint_path"] = str(final_checkpoint)
        if terminal_checkpoint is not None:
            outcome["_context_checkpoint"] = terminal_checkpoint
        return outcome
    except GenerationFenceError as exc:
        outcome["failure_reason"] = str(exc)
        return outcome
    except (OSError, subprocess.SubprocessError) as exc:
        outcome["failure_reason"] = f"task crashed: {exc}"
        return outcome


async def _heartbeat_loop(
    writer: asyncio.StreamWriter,
    task_id: str,
    generation: int,
    stop: threading.Event,
    progress: AgentProgress | None = None,
    interval_s: float = HEARTBEAT_INTERVAL_S,
) -> None:
    while not stop.is_set():
        turn = progress.turn if progress is not None else 0
        tool = progress.tool if progress is not None else None
        status = progress.status if progress is not None else "working"
        await send(
            writer,
            {
                "type": "heartbeat",
                "task_id": task_id,
                "generation": generation,
                "turn": turn,
                "tool": tool,
                "status": status,
                "monotonic_ms": _monotonic_ms(),
            },
        )
        if stop.is_set():
            # Observed the stop flag right after this send: exit at the safe
            # point (between iterations) instead of starting another send.
            break
        # Sleep in short slices so stop is observed promptly even when the
        # configured interval is large; the send cadence stays ~interval_s.
        remaining = interval_s
        while remaining > 0 and not stop.is_set():
            step = min(0.05, remaining)
            await asyncio.sleep(step)
            remaining -= step


async def _emit_proposed_children(
    writer: asyncio.StreamWriter, run: dict[str, Any], task_id: str
) -> None:
    """Emit one ``propose_child`` wire message per declared child proposal.

    The proposal set is read from the run payload's ``proposed_children``
    (declared by the caller in the plan spec; deterministic, no model
    autonomy): each entry is ``{"child_task_id", "kind", "spec"}``. Every
    message carries a fresh ``request_id`` so the supervisor can correlate the
    resulting ``child_admitted`` / ``child_rejected`` events. Emitted after
    the task body finishes and before the result envelope, so the supervisor
    can admit children as soon as the parent's terminal envelope is known.

    A malformed proposal set is a plan-spec defect, not silently skippable:
    it raises :class:`ChildProposalError` so the task fails closed instead of
    dropping revisions.
    """
    proposals = run.get("proposed_children")
    if proposals is None:
        return
    if not isinstance(proposals, list):
        raise ChildProposalError(
            f"proposed_children must be a list, got {type(proposals).__name__}"
        )
    for index, proposal in enumerate(proposals):
        if not isinstance(proposal, dict):
            raise ChildProposalError(
                f"proposed_children[{index}] must be an object, got {type(proposal).__name__}"
            )
        child_task_id = proposal.get("child_task_id")
        kind = proposal.get("kind")
        child_spec = proposal.get("spec")
        if not isinstance(child_task_id, str) or not child_task_id:
            raise ChildProposalError(
                f"proposed_children[{index}].child_task_id must be a non-empty string"
            )
        if not isinstance(kind, str) or not kind:
            raise ChildProposalError(f"proposed_children[{index}].kind must be a non-empty string")
        if not isinstance(child_spec, dict):
            raise ChildProposalError(f"proposed_children[{index}].spec must be an object")
        await send(
            writer,
            {
                "type": "propose_child",
                "request_id": make_request_id("propose"),
                "parent_task_id": task_id,
                "child_task_id": child_task_id,
                "kind": kind,
                "spec": child_spec,
            },
        )


async def _emit_delegated_child(
    writer: asyncio.StreamWriter,
    config: AgentConfig,
    arguments: dict[str, Any],
    *,
    request_id: str | None = None,
) -> None:
    """Emit the ``propose_child`` wire message for one model ``delegate`` call.

    Mirrors ``_emit_proposed_children``'s message shape exactly (type,
    fresh ``request_id``, parent_task_id, child_task_id, kind, spec). The
    supervisor buffers the proposal per parent and validates/admits it at
    this task's terminal envelope; the tool result is emitted before this
    message so the model's observation stays bounded and independent.
    """
    await send(
        writer,
        {
            "type": "propose_child",
            "request_id": request_id or make_request_id("propose"),
            "parent_task_id": config.task_id,
            "child_task_id": arguments["child_task_id"],
            "kind": arguments["kind"],
            "spec": arguments["spec"],
        },
    )


async def _run_task(
    writer: asyncio.StreamWriter,
    run: dict[str, Any],
    task_id: str,
    generation: int,
    stop: threading.Event,
    config: AgentConfig,
) -> dict[str, Any]:
    """Run the task body with heartbeats; returns the terminal outcome."""
    started_at = time.time()
    run_rid = run["request_id"]
    progress = AgentProgress()

    hb = asyncio.create_task(
        _heartbeat_loop(writer, task_id, generation, stop, progress, config.heartbeat_interval_s)
    )
    try:
        outcome = await do_work(run, stop, config=config, writer=writer, progress=progress)
    finally:
        stop.set()
        # Heartbeat stop: the write is enqueued synchronously and atomically,
        # but cancel() between the write and its drain could leave the next
        # (result) message written against a mid-drain heartbeat. Never cancel
        # mid-send: set the stop flag and let the loop observe it at its safe
        # point (after the in-flight send completes, before the next one).
        # A hard cancel is only a fallback if the loop fails to drain promptly.
        try:
            await asyncio.wait_for(hb, timeout=config.heartbeat_interval_s + 1.0)
        except (TimeoutError, asyncio.CancelledError):
            hb.cancel()
            try:
                await hb
            except asyncio.CancelledError:
                pass
    outcome["request_id"] = run_rid
    outcome["task_id"] = task_id
    outcome["generation"] = generation
    outcome["started_at"] = started_at
    outcome["ended_at"] = time.time()
    await _emit_proposed_children(writer, run, task_id)
    return outcome


def _exit_reason(status: str) -> str:
    return {
        "succeeded": "done",
        "failed": "failed",
        "cancelled": "cancelled",
        TaskStatus.SUSPENDED.value: "suspended",
    }.get(status, "failed")


async def _emit_result_envelope(writer: asyncio.StreamWriter, outcome: dict[str, Any]) -> None:
    status = outcome["status"]
    envelope = {
        "type": "result_envelope",
        "request_id": outcome["request_id"],
        "task_id": outcome["task_id"],
        "generation": outcome["generation"],
        "status": status,
        "exit_code": _exit_code_for(status),
        "commits": outcome.get("commits", []),
        "files_changed": outcome.get("files_changed", []),
        "diff": outcome.get("diff", ""),
        "diff_truncated": bool(outcome.get("diff_truncated", False)),
        "summary": (outcome.get("summary") or "")[:MAX_SUMMARY_CHARS],
        "failure_reason": outcome.get("failure_reason"),
        "started_at": outcome.get("started_at"),
        "ended_at": outcome.get("ended_at"),
    }
    if status == TaskStatus.SUSPENDED.value:
        epoch = outcome.get("epoch")
        checkpoint_ref = outcome.get("checkpoint_ref")
        if epoch is not None:
            envelope["epoch"] = epoch
        if checkpoint_ref is not None:
            envelope["checkpoint_ref"] = checkpoint_ref
    provider_metadata = outcome.get("provider_metadata")
    if isinstance(provider_metadata, dict):
        envelope["provider_metadata"] = provider_metadata
    await send(writer, envelope)


async def _emit_result(writer: asyncio.StreamWriter, outcome: dict[str, Any]) -> None:
    """Emit result_envelope + the authoritative exit_message (normal completion)."""
    await _emit_result_envelope(writer, outcome)
    await send(
        writer,
        {
            "type": "exit_message",
            "task_id": outcome["task_id"],
            "generation": outcome["generation"],
            "reason": _exit_reason(outcome["status"]),
            "monotonic_ms": _monotonic_ms(),
        },
    )


async def _await_on_shutdown(
    task: asyncio.Task[dict[str, Any]],
    task_id: str,
    generation: int,
    request_id: str | None,
) -> dict[str, Any]:
    """Wait for the active task and preserve its terminal shutdown outcome."""
    started_at = time.time()
    try:
        return await task
    except asyncio.CancelledError:
        failure_reason = "task cancelled during worker shutdown"
        status = TaskStatus.CANCELLED.value
    except Exception as exc:
        failure_reason = f"task failed during worker shutdown: {exc}"
        status = TaskStatus.FAILED.value
    return {
        "request_id": request_id,
        "task_id": task_id,
        "generation": generation,
        "status": status,
        "failure_reason": failure_reason,
        "started_at": started_at,
        "ended_at": time.time(),
    }


async def _send_ok(
    writer: asyncio.StreamWriter,
    msg: dict[str, Any],
    task_id: str,
    generation: int,
) -> None:
    await send(
        writer,
        {
            "type": "ok",
            "request_id": msg.get("request_id") if isinstance(msg, dict) else None,
            "task_id": task_id,
            "generation": generation,
            "monotonic_ms": _monotonic_ms(),
        },
    )


async def _send_pong(
    writer: asyncio.StreamWriter,
    msg: dict[str, Any],
    task_id: str,
    generation: int,
) -> None:
    await send(
        writer,
        {
            "type": "pong",
            "request_id": msg.get("request_id"),
            "task_id": task_id,
            "generation": generation,
            "monotonic_ms": _monotonic_ms(),
        },
    )


async def _fatal(writer: asyncio.StreamWriter, msg: Any, message: str) -> int:
    context = msg if isinstance(msg, dict) else {}
    await send(
        writer,
        {
            "type": "fatal_error",
            "request_id": context.get("request_id"),
            "task_id": context.get("task_id"),
            "generation": context.get("generation"),
            "error_type": "invalid_message",
            "message": message[:500],
            "recoverable": False,
        },
    )
    await send(
        writer,
        {
            "type": "exit_message",
            "task_id": context.get("task_id"),
            "generation": context.get("generation"),
            "reason": "fatal",
            "monotonic_ms": _monotonic_ms(),
        },
    )
    return 1


async def run(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> int:
    """The worker wire loop. Returns the process exit code."""
    init_timeout = _env_float("CAMBIUM_INIT_TIMEOUT_S", INIT_TIMEOUT_S)
    idle_timeout = _env_float("CAMBIUM_IDLE_TIMEOUT_S", IDLE_TIMEOUT_S)

    try:
        first = await asyncio.wait_for(read_message(reader), timeout=init_timeout)
    except TimeoutError:
        return await _fatal(writer, {}, "init timeout: no init message within deadline")
    except MessageTooLong:
        return await _fatal(writer, {}, "wire line exceeded the length cap")
    if first is None:
        return 1
    if not isinstance(first, dict) or first.get("type") != "init" or "request_id" not in first:
        return await _fatal(writer, first, "expected init as the first message")

    init_rid = first["request_id"]
    task_id = first.get("task_id", "unknown")
    generation = first.get("generation", 1)
    init_fanout_config = first.get("fanout_config")
    if isinstance(generation, bool) or not isinstance(generation, int) or generation <= 0:
        return await _fatal(writer, first, "init generation must be a positive integer")
    try:
        init_config = AgentConfig.from_init(first)
    except ValueError as exc:
        return await _fatal(writer, first, f"invalid init config: {exc}")
    await send(
        writer,
        {
            "type": "ready",
            "request_id": init_rid,
            "task_id": task_id,
            "pid": os.getpid(),
            "generation": generation,
            "proto": first.get("proto", PROTO),
            "monotonic_ms": _monotonic_ms(),
        },
    )
    # Worker-reuse opt-in (eval-3 ADOPT): when the init asks for it, the
    # worker stays alive after the task and waits for a rebind init on stdin
    # instead of exiting. The single-init exit behavior below is unchanged
    # when this flag is absent.
    worker_reuse = bool(first.get("worker_reuse"))

    current: asyncio.Task[dict[str, Any]] | None = None
    current_request_id: str | None = None
    stop = threading.Event()

    while True:
        # The idle deadline only applies BETWEEN tasks: while a task runs the
        # supervisor sends no steering messages, and a slow model (e.g. max-
        # reasoning luna) can legitimately run far past the idle timeout. A
        # timeout mid-task would abort the run and exit "idle" with no result
        # envelope. With a task in flight the read simply waits.
        read_timeout = None if current is not None else idle_timeout
        read_task = asyncio.create_task(
            asyncio.wait_for(read_message(reader), timeout=read_timeout)
        )
        pending = {read_task}
        if current is not None:
            pending.add(current)
        done, _ = await asyncio.wait(pending, return_when=asyncio.FIRST_COMPLETED)

        if current is not None and current in done:
            task = current
            current = None
            current_request_id = None
            read_task.cancel()
            try:
                await read_task
            except BaseException:
                pass
            try:
                outcome = task.result()
            except Exception as exc:
                return await _fatal(writer, {}, f"task crashed: {exc}")
            await _emit_result_envelope(writer, outcome)
            if not worker_reuse:
                await send(
                    writer,
                    {
                        "type": "exit_message",
                        "task_id": outcome["task_id"],
                        "generation": outcome["generation"],
                        "reason": _exit_reason(outcome["status"]),
                        "monotonic_ms": _monotonic_ms(),
                    },
                )
                return 0
            # Reuse path: report ready-for-reuse and loop back to read the
            # next init instead of exiting. All per-task state (agent loop,
            # transcript, tool state, LM clients) is rebuilt from the new
            # init's config on rebind.
            await send(
                writer,
                {
                    "type": "reuse_ready",
                    "task_id": outcome["task_id"],
                    "generation": outcome["generation"],
                    "pid": os.getpid(),
                    "monotonic_ms": _monotonic_ms(),
                },
            )
            continue

        try:
            msg = read_task.result()
        except TimeoutError:
            # No message from the supervisor within the idle deadline: the
            # supervisor is presumed gone. Abort any current task and exit
            # gracefully (documented in the module docstring).
            stop.set()
            if current is not None:
                task = current
                current = None
                try:
                    await task
                except BaseException:
                    pass
            await send(
                writer,
                {
                    "type": "exit_message",
                    "task_id": task_id,
                    "generation": generation,
                    "reason": "idle",
                    "monotonic_ms": _monotonic_ms(),
                },
            )
            return 0
        except MessageTooLong:
            return await _fatal(writer, {}, "wire line exceeded the length cap")
        except OSError as exc:
            return await _fatal(writer, {}, f"wire read failed: {exc}")

        if msg is None:
            if worker_reuse and current is None:
                # stdin closed while idle between tasks (supervisor closed the
                # pipe or exited): a pooled worker exits cleanly.
                return 0
            # stdin closed: no further requests can arrive.
            await send(
                writer,
                {
                    "type": "exit_message",
                    "task_id": task_id,
                    "generation": generation,
                    "reason": "crash",
                    "monotonic_ms": _monotonic_ms(),
                },
            )
            return 1

        mtype = msg.get("type") if isinstance(msg, dict) else None
        if mtype == "init":
            # Rebind: only a reuse-enabled worker accepts a second init. The
            # rebind re-sends the FULL init (worktree, spec, fanout_config,
            # assigned_provider, budgets, permissions), so every per-task
            # client is rebuilt from the new config; nothing is carried over.
            if not worker_reuse:
                return await _fatal(writer, msg, "init after init (reuse not enabled)")
            if current is not None:
                return await _fatal(writer, msg, "init while a task is already running")
            if "request_id" not in msg:
                return await _fatal(writer, msg, "init without a request_id")
            new_generation = msg.get("generation", 1)
            if (
                isinstance(new_generation, bool)
                or not isinstance(new_generation, int)
                or new_generation <= 0
            ):
                return await _fatal(writer, msg, "init generation must be a positive integer")
            try:
                new_config = AgentConfig.from_init(msg)
            except ValueError as exc:
                return await _fatal(writer, msg, f"invalid init config: {exc}")
            if new_config.worktree is not None:
                os.chdir(new_config.worktree)
            first = msg
            init_rid = msg["request_id"]
            task_id = msg.get("task_id", "unknown")
            generation = new_generation
            init_fanout_config = msg.get("fanout_config")
            init_config = new_config
            worker_reuse = bool(msg.get("worker_reuse"))
            stop = threading.Event()
            await send(
                writer,
                {
                    "type": "ready",
                    "request_id": init_rid,
                    "task_id": task_id,
                    "pid": os.getpid(),
                    "generation": generation,
                    "proto": msg.get("proto", PROTO),
                    "monotonic_ms": _monotonic_ms(),
                },
            )
            continue
        if mtype == "run_task":
            if current is not None:
                return await _fatal(writer, msg, "run_task while a task is already running")
            if "request_id" not in msg:
                return await _fatal(writer, msg, "run_task without a request_id")
            claimed_generation = msg.get("generation", generation)
            if claimed_generation != generation:
                return await _fatal(writer, msg, "run_task generation does not match init")
            msg = {**msg, "generation": generation}
            stop = threading.Event()
            task_run = dict(msg)
            if init_fanout_config is not None:
                # Provider configuration belongs to init. It is kept in the
                # worker's local task context and never sent back over IPC.
                task_run["fanout_config"] = init_fanout_config
            task_config = _merge_task_config(init_config, first, task_run)
            current_request_id = msg["request_id"]
            current = asyncio.create_task(
                _run_task(writer, task_run, task_id, generation, stop, task_config)
            )
        elif mtype == "check_health":
            await _send_ok(writer, msg, task_id, generation)
        elif mtype == "ping":
            await _send_pong(writer, msg, task_id, generation)
        elif mtype == "steer":
            payload = msg.get("payload") or {}
            # Structured parse: only an exact {"action": "cancel"} aborts.
            # Free text containing the word "cancel" must NOT abort.
            if isinstance(payload, dict) and payload.get("action") == "cancel":
                logger.info("steer: cancel requested")
                stop.set()
            else:
                logger.info("steer (v2.1 hook; continuing): %s", json.dumps(payload)[:200])
        elif mtype == "cancel":
            logger.info("cancel: aborting current task")
            await _send_ok(writer, msg, task_id, generation)
            stop.set()
        elif mtype == "shutdown":
            await _send_ok(writer, msg, task_id, generation)
            if current is not None:
                stop.set()
                task = current
                current = None
                request_id = current_request_id
                current_request_id = None
                outcome = await _await_on_shutdown(task, task_id, generation, request_id)
                await _emit_result_envelope(writer, outcome)
            await send(
                writer,
                {
                    "type": "exit_message",
                    "task_id": task_id,
                    "generation": generation,
                    "reason": "shutdown",
                    "monotonic_ms": _monotonic_ms(),
                },
            )
            return 0
        else:
            return await _fatal(writer, msg, f"unknown message type {mtype!r}")


class _WriterProtocol(asyncio.streams.FlowControlMixin):
    """Flow-control protocol for the stdout write transport.

    ``StreamWriter.wait_closed`` resolves the protocol's ``_closed`` future,
    so it must be tied to THIS transport's ``connection_lost`` — not the
    reader's (whose stdin pipe stays open for the worker's lifetime).
    """

    def __init__(self) -> None:
        super().__init__()
        self._closed = asyncio.get_running_loop().create_future()

    def connection_lost(self, exc: Exception | None) -> None:
        if not self._closed.done():
            self._closed.set_result(exc)
        super().connection_lost(exc)

    def _get_close_waiter(self, stream: asyncio.StreamWriter) -> asyncio.Future:
        return self._closed


async def _open_stdio() -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
    """Wrap stdin/stdout in asyncio streams (protocol stream = stdout)."""
    loop = asyncio.get_running_loop()
    reader = asyncio.StreamReader(limit=MAX_LINE_BYTES)
    read_protocol = asyncio.StreamReaderProtocol(reader)
    await loop.connect_read_pipe(lambda: read_protocol, sys.stdin.buffer)
    write_protocol = _WriterProtocol()
    transport, _ = await loop.connect_write_pipe(lambda: write_protocol, sys.stdout.buffer)
    writer = asyncio.StreamWriter(transport, write_protocol, reader, loop)
    return reader, writer


async def _amain() -> int:
    reader, writer = await _open_stdio()
    try:
        return await run(reader, writer)
    finally:
        writer.close()
        await writer.wait_closed()


def main() -> int:
    logging.basicConfig(
        stream=sys.stderr,
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    try:
        return asyncio.run(_amain())
    except KeyboardInterrupt:
        return 1


if __name__ == "__main__":
    sys.exit(main())
