"""Cambium supervisor — the canonical asyncio runtime.

Speaks the Nuntius JSON-Lines wire protocol (docs/architecture.md §5) with N
worker subprocesses under one ``asyncio.TaskGroup``: spawn ``python -m
cambium.worker`` (or a task's ``worker`` script) inside a git worktree,
correlate ``init`` -> ``ready`` -> ``run_task`` -> ``result_envelope`` ->
``exit_message`` by request_id, and publish the worker branch onto
``refs/heads/main`` atomically through ``cambium.merge.MergeSequencer`` when
the worker's envelope reports ``succeeded``. There is no pre-merge gate: the
worker verdict alone decides merge eligibility.

Every event is persisted to ``<session_dir>/.cambium/events.db`` through the
canonical ``cambium.store.EventStore`` (readable via ``read_events``).
Redaction is session-scoped: one ``cambium.redact.Redactor`` built from every
worker-forwardable declared secret value redacts the complete event record
before the store, the non-critical queue, and event observers.

``run_plan`` drives a multi-task plan and returns a ``PlanResult``;
``run_session`` is a thin one-task adapter that keeps the historical
``SliceResult`` return shape. ``cambium.store`` and ``cambium.merge`` are
hard runtime dependency contracts: import failure fails at load.
"""

from __future__ import annotations

import argparse
import asyncio
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
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from cambium.fencing import next_generation, read_generation, write_generation
from cambium.process_env import build_subprocess_env
from cambium.provider_config import DEFAULT_PROVIDER_PATH
from cambium.system_health import can_run_heavy

from .auth import scrub_environment
from .ipc import MAX_LINE_BYTES
from .merge import MergeSequencer
from .modules.base import _is_secret_shaped
from .redact import Redactor, build_session_redactor
from .results import EXIT_CODES, Result, write_result
from .store import CRITICAL_KINDS, EventStore

PROTO = 1
WORKER_STDIN_LIMIT = MAX_LINE_BYTES
# Four full-cap messages bound each worker's decoded stdout backlog.
WORKER_STDOUT_QUEUE_MAXSIZE = max(1, MAX_LINE_BYTES // (256 * 1024))
OUTBOUND_MESSAGE_TOO_LONG = "outbound_message_too_long"
STDIN_WRITE_TIMEOUT_S = 5.0
PONG_DEADLINE_S = 10.0
PROCESS_REAP_TIMEOUT_S = 5.0

EventSink = Callable[[dict[str, Any]], None]


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


def _protocol_version_mismatch(msg: dict[str, Any]) -> bool:
    if msg.get("type") == "ready":
        return msg.get("proto") != PROTO
    return "proto" in msg and msg["proto"] != PROTO


_TOOL_EVENT_INT_FIELDS = ("batch_index", "batch_size", "turn")
_TOOL_EVENT_DURATION_FIELDS = ("duration_ms",)


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
    gate_exit_code: int | None = None
    merge_sha: str | None = None
    timed_out: bool = False
    timeout_phase: str | None = None  # "ready" | "wall" | "heartbeat" | "pong" | "stdin"


_TIMEOUT_PHASES = ("ready", "wall", "heartbeat", "pong", "stdin")


def _status_line_is_fence(line: str) -> bool:
    """Whether a porcelain status line only touches the supervisor's fence dir."""
    if len(line) < 4 or line[2] != " ":
        return False
    path = line[3:].strip()
    return path == ".cambium" or path.startswith(".cambium/")


def _cfg_float(task_spec: dict[str, Any], key: str, env: str, default: float) -> float:
    spec_value = task_spec.get(key)
    if spec_value is not None:
        return float(spec_value)
    return float(os.environ.get(env, default))


def _encode_json_frame(msg: dict[str, Any]) -> bytes | None:
    content = json.dumps(msg).encode("utf-8")
    if len(content) > MAX_LINE_BYTES:
        return None
    return content + b"\n"


async def _write_json(
    proc: asyncio.subprocess.Process,
    msg: dict[str, Any],
    *,
    deadline: float | None = None,
) -> bool:
    """Write one wire message before ``deadline`` or kill its process group."""
    if proc.stdin is None or proc.stdin.is_closing():
        return False
    frame = _encode_json_frame(msg)
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
        proc.stdin.write(frame)
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


async def _kill_process_group_and_reap(proc: asyncio.subprocess.Process) -> None:
    """Kill a subprocess group, including descendants, and reap its leader."""
    await _kill_worker(proc)
    try:
        await asyncio.wait_for(proc.wait(), PROCESS_REAP_TIMEOUT_S)
        return
    except (ProcessLookupError, TimeoutError):
        pass
    try:
        proc.kill()
    except (ProcessLookupError, OSError):
        pass
    try:
        await asyncio.wait_for(proc.wait(), PROCESS_REAP_TIMEOUT_S)
    except (ProcessLookupError, TimeoutError):
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
        gate_exit_code=result.gate_exit_code,
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
# respawn; a clean worker whose envelope reports "succeeded" is merged
# atomically onto refs/heads/main (no pre-merge gate).
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


def _provider_config_path(source: Mapping[str, str]) -> str:
    """Resolve the absolute provider-config path a provider-mode worker loads."""
    configured = source.get("CAMBIUM_PROVIDERS")
    if configured:
        path = Path(configured).expanduser()
    else:
        path = DEFAULT_PROVIDER_PATH
    if not path.is_absolute():
        path = Path.cwd() / path
    return str(path.resolve())


def _worker_environment(
    spec: dict[str, Any], generation: int, *, session_dir: Path | None = None
) -> dict[str, str]:
    """Build a strict worker env with authorized provider credentials."""
    source = dict(os.environ)
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
        allowed_keys=_provider_env_keys(spec),
        worktree=worktree,
        overrides=overrides,
    )
    if spec.get("fanout_config"):
        env["CAMBIUM_PROVIDERS"] = _provider_config_path(source)
    return env


def _redacted_provider_metadata(value: Any) -> dict[str, Any] | None:
    """Keep only scalar provider provenance safe for event serialization."""
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
        and isinstance(count, (int, float))
        and not isinstance(count, bool)
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


def _session_redactor(specs: list[dict[str, Any]]) -> Redactor:
    """Build one session redactor from every worker-forwardable declared value.

    The registry must cover every value ``_worker_environment`` can forward to
    a worker from the declared ``provider_env_keys``. The declaration is the
    authority boundary; the variable name does not determine whether its value
    is sensitive. Compact machine-token values (``_is_secret_shaped``) are
    registered for substring redaction wherever they appear; prose-like
    declared values are registered as whole strings only, so a benign
    diagnostic that merely contains the value is not corrupted.
    """
    secret_values: list[str] = []
    whole_values: list[str] = []
    for spec in specs:
        for key in _provider_env_keys(spec):
            value = os.environ.get(key)
            if not isinstance(value, str) or not value:
                continue
            if _is_secret_shaped(value):
                secret_values.append(value)
            else:
                whole_values.append(value)
    return build_session_redactor(secret_values, whole_values=whole_values)


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
    gate_exit_code: int | None = None
    restarts: int = 0


@dataclass(frozen=True, slots=True)
class PlanResult:
    """Aggregate outcome of a run_plan session."""

    results: tuple[TaskResult, ...]

    @property
    def exit_code(self) -> int:
        if not self.results:
            return 1
        return 0 if all(r.status == "succeeded" for r in self.results) else 1


class DuplicateTaskIDError(ValueError):
    """The plan cannot be dispatched because a task id is repeated."""


class InvalidBaseCommitError(ValueError):
    """A task base does not resolve to a commit in its repository."""


class WorktreeRecoveryError(RuntimeError):
    """A destructive worktree recovery command failed."""


class SessionAlreadyRunningError(RuntimeError):
    """Another supervisor already owns the requested session."""


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


@dataclass(frozen=True, slots=True)
class _GenOutcome:
    """Outcome of one generation's drive loop."""

    clean: bool  # worker delivered a verdict (result + exit + rc 0)
    fatal: bool = False  # restarting cannot help (spawn or terminal protocol error)
    reason: str | None = None
    timeout_phase: str | None = None
    exit_code: int | None = None
    exit_reason: str | None = None
    envelope: dict[str, Any] | None = None
    correlated: bool = False


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
    ) -> None:
        self._session_dir = Path(session_dir)
        self._store = store
        self._on_event = on_event
        self._redactor = redactor
        self._resource_thresholds = (
            None if resource_thresholds is None else dict(resource_thresholds)
        )
        self._event_append_lock = asyncio.Lock()
        self._handles: dict[str, WorkerHandle] = {}
        self._results: dict[str, TaskResult] = {}
        self._worktree_lock = asyncio.Lock()
        self._merge_lock = asyncio.Lock()
        self._rid = 0
        self._last_envelope: dict[str, Any] | None = None

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
        redacted = self._redactor.redact_mapping(envelope)
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
            record = self._redactor.redact_mapping(record)
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
            h for h in self._handles.values()
            if h.proc is not None and h.proc.returncode is None
        ]
        for h in alive:
            try:
                os.killpg(h.proc.pid, signal.SIGTERM)
            except (ProcessLookupError, PermissionError, OSError):
                pass
        if alive:
            try:
                await asyncio.wait_for(
                    asyncio.gather(*(h.proc.wait() for h in alive), return_exceptions=True),
                    TERM_GRACE_S,
                )
            except TimeoutError:
                for h in alive:
                    if h.proc.returncode is not None:
                        continue
                    try:
                        os.killpg(h.proc.pid, signal.SIGKILL)
                    except (ProcessLookupError, PermissionError, OSError):
                        pass
                    try:
                        h.proc.kill()
                    except ProcessLookupError:
                        pass
                await asyncio.gather(*(h.proc.wait() for h in alive), return_exceptions=True)
        try:
            await self.emit(
                "session_ended", task_id=None, session_status=session_status,
                results={tid: r.status for tid, r in self._results.items()},
            )
        except BaseException:
            pass
        await asyncio.to_thread(self._store.close)

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
        return _worker_environment(spec, generation, session_dir=session_dir)

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
        return payload

    # -- per-task supervision ------------------------------------------------

    async def supervise_task(self, spec: dict[str, Any]) -> None:
        if spec["task_id"] in self._results:
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
            if spec["task_id"] in self._results:
                await self._prune_worktree(spec)

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

        await self.emit(
            "task_assigned", task_id=task_id, repo=str(repo), branch=spec["branch"],
            base_commit=spec["base_commit"], task=spec.get("task", ""),
        )
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

        restarts = 0
        while True:
            handle = WorkerHandle(task_id=task_id, generation=generation)
            self._handles[task_id] = handle
            outcome = await self._drive_generation(
                spec, handle,
                ready_timeout=ready_timeout,
                heartbeat_interval=heartbeat_interval,
                heartbeat_timeout=heartbeat_timeout,
                wall_budget=wall_budget,
            )
            if outcome.envelope is not None and outcome.correlated:
                self._last_envelope = self._redact_envelope(outcome.envelope)
            if outcome.clean:
                integrity = await self._worker_success_integrity(spec, worktree)
                if integrity is not None:
                    await self.emit(
                        "worker_failed", task_id=task_id, generation=generation,
                        reason=integrity,
                    )
                    self._results[task_id] = TaskResult(
                        task_id=task_id, status="failed", exit_code=1,
                        reason=integrity, gate_exit_code=None, restarts=restarts,
                    )
                    return
                if outcome.envelope and outcome.envelope.get("status") == "succeeded":
                    merged = await self._merge_task(spec, handle)
                    if merged is not None:
                        self._results[task_id] = TaskResult(
                            task_id=task_id, status="succeeded", exit_code=0,
                            reason=None, merge_sha=merged, gate_exit_code=0,
                            restarts=restarts,
                        )
                    else:
                        self._results[task_id] = TaskResult(
                            task_id=task_id, status="failed", exit_code=1,
                            reason="merge_failed", gate_exit_code=0, restarts=restarts,
                        )
                else:
                    self._results[task_id] = TaskResult(
                        task_id=task_id, status="failed", exit_code=1,
                        reason="worker_verdict_failed", gate_exit_code=0, restarts=restarts,
                    )
                return
            if outcome.fatal:
                self._results[task_id] = TaskResult(
                    task_id=task_id, status="failed", exit_code=1,
                    reason=outcome.reason, restarts=restarts,
                )
                return
            reason = outcome.reason or "crash"
            if outcome.timeout_phase:
                await self.emit(
                    "timeout", task_id=task_id, generation=generation, phase=outcome.timeout_phase
                )
            if restarts >= max_restarts:
                await self.emit(
                    "worker_failed", task_id=task_id, generation=generation,
                    restarts=restarts, max_restarts=max_restarts, reason=reason,
                )
                self._results[task_id] = TaskResult(
                    task_id=task_id, status="failed", exit_code=1,
                    reason=f"max_restarts ({max_restarts}): {reason}", restarts=restarts,
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

    async def _drive_generation(
        self, spec: dict[str, Any], handle: WorkerHandle, *,
        ready_timeout: float, heartbeat_interval: float, heartbeat_timeout: float,
        wall_budget: float,
    ) -> _GenOutcome:
        task_id = spec["task_id"]
        worktree = Path(spec["worktree_path"])
        generation = handle.generation
        cmd = self._worker_command(spec)
        env = self._worker_env(spec, generation)

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
        if spec.get("fanout_config"):
            init_msg["fanout_config"] = spec["fanout_config"]
            init_msg["provider_env_keys"] = sorted(_provider_env_keys(spec))
        if _encode_json_frame(init_msg) is None:
            await _report_outbound_message_too_long()
            return _GenOutcome(
                clean=False, fatal=True, reason=OUTBOUND_MESSAGE_TOO_LONG,
            )

        await self.emit("spawned", task_id=task_id, generation=generation, worker=" ".join(cmd))
        try:
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

        async def _cancel_and_kill() -> None:
            cancel_msg = {
                "type": "cancel",
                "request_id": self._next_rid(),
                "reason": timeout_phase or "timeout",
            }
            try:
                if _encode_json_frame(cancel_msg) is None:
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
            if _encode_json_frame(ping_msg) is None:
                protocol_failure = OUTBOUND_MESSAGE_TOO_LONG
                await _report_outbound_message_too_long()
                await _kill_worker(proc)
                return False
            if not await _write_json(
                proc, ping_msg,
                deadline=pong_deadline,
            ):
                timeout_phase = "pong"
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
                    if _encode_json_frame(run_msg) is None:
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

        exit_code = proc.returncode
        handle.exit_code = exit_code
        handle.state = "EXITED"
        clean = (
            exit_code == 0
            and exit_reason is not None
            and envelope is not None
            and correlated
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
            worktree, "status", "--porcelain=v1", "--untracked-files=all", check=False
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
            future.result()

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
    spec.setdefault("base_commit", None)
    spec.setdefault("write_marker", True)
    return spec


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
    ``done`` only after the merge passed (the caller records the merged
    TaskResult); any failed TaskResult is ``failed`` unless its reason
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


async def run_plan(
    session_dir: str | Path,
    plan: dict[str, Any] | list[dict[str, Any]],
    on_event: EventSink | None = None,
    *,
    resource_thresholds: dict[str, Any] | None = None,
) -> PlanResult:
    """Run every task in the plan concurrently under one supervisor session.

    Workers are spawned as ``python -m cambium.worker`` (or the task's
    ``worker`` script); a clean worker whose envelope reports ``succeeded`` is
    merged onto ``refs/heads/main``. There is no pre-merge gate: the worker
    verdict alone decides merge eligibility. Publication is ref-only:
    ``refs/heads/main`` advances via atomic ``update-ref`` and no checkout is
    refreshed.
    Returns a PlanResult; the session's event log is durable in
    ``<session_dir>/.cambium/events.db`` (readable via ``read_events``), and a
    canonical root result is written atomically to
    ``<session_dir>/.cambium/result.json`` before this coroutine returns.
    """
    session_dir = Path(session_dir)
    tasks = _plan_tasks(plan)
    _reject_duplicate_task_ids(tasks)
    specs = [_validate_plan_task(session_dir, t) for t in tasks]
    if not specs:
        raise ValueError("plan contains no tasks")

    admission = _SessionAdmission(session_dir)
    admission.acquire()
    try:
        started_at = time.time()
        redactor = _session_redactor(specs)
        store = EventStore(session_dir / ".cambium" / "events.db", redactor=redactor)
        runtime = _Runtime(
            session_dir,
            store,
            on_event=on_event,
            redactor=redactor,
            resource_thresholds=resource_thresholds,
        )
        await runtime.start()
        cancelled = False
        try:
            await runtime.reconcile(specs)
            async with asyncio.TaskGroup() as tg:
                for spec in specs:
                    tg.create_task(runtime.supervise_task(spec))
        except asyncio.CancelledError:
            cancelled = True
        except BaseExceptionGroup as exc_group:
            await runtime.emit(
                "log", task_id=None, message=f"task group exception: {exc_group}"
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


async def _amain_plan(session_dir: Path, plan: dict[str, Any]) -> int:
    loop = asyncio.get_running_loop()

    def print_event(record: dict[str, Any]) -> None:
        print(f'{record["kind"]:>16}  {json.dumps(record["payload"])}', flush=True)

    task = asyncio.ensure_future(run_plan(session_dir, plan, on_event=print_event))
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
    args = parser.parse_args(argv)
    session_dir = Path(args.session_dir)
    if args.plan:
        plan = json.loads(Path(args.plan).read_text())
        tasks = _plan_tasks(plan)
        _reject_duplicate_task_ids(tasks)
        for task in tasks:
            _ensure_repo_initialized(Path(task["repo"]).resolve())
        try:
            return asyncio.run(_amain_plan(session_dir, plan))
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
        return asyncio.run(_amain_plan(session_dir, {"tasks": [plan_task]}))
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
