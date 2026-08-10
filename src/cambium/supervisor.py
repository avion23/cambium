"""Minimal asyncio supervisor — the vertical-slice milestone.

End-to-end proof of the harness shape with ONE worker: spawn a worker
subprocess, speak JSON-Lines over stdio (``init`` -> ``ready`` ->
``run_task`` -> ``result_envelope`` -> ``exit_message``, request_id
correlated), append events to ``<session_dir>/.cambium/events.jsonl``,
run the task's gate command, and merge the worker's branch back with
``git merge --ff-only`` in the scratch repo. Exit 0 only when every step
succeeded.

Failure conditions (any one overrides the envelope's status to failed):
(a) worker process exit code != 0 — the supervisor exit code then
reflects the worker's real exit code; (b) ``exit_message`` missing at
EOF; (c) ``result_envelope`` missing or not correlated to ``run_task``'s
request_id. Timeouts: ``ready_timeout``, ``gate_timeout``, and an overall
wall budget — on timeout the worker's process group is killed
(start_new_session) and the session is failed. ``result_envelope`` must
echo ``run_task``'s request_id; ``exit_message`` is connection-level and
carries no request_id (arch §5.2).

Scope guard: this is the slice, not Custos. No heartbeats, no restart
policy, no fencing, no event-log durability beyond a per-line flush, no
worktree recovery/prune. Every divergence from the architecture drafts
is flagged in docs/research/vertical-slice-report.md.
"""

from __future__ import annotations

import argparse
import asyncio
import importlib.util
import json
import os
import random
import re
import shutil
import signal
import sqlite3
import subprocess
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from cambium.fencing import next_generation, read_generation, write_generation
from cambium.process_env import build_subprocess_env

PROTO = 1
WORKER_STDIN_LIMIT = 1_048_576
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
    timeout_phase: str | None = None  # "ready" | "run" | "gate" | "wall"


class EventLog:
    """Simple JSON-Lines event log under the session dir.

    Slice-level durability intent: each line is flushed on append, but
    there is no fsync and writes happen on the event loop. The real
    design (architecture §6, custos design §2.4) uses a SQLite WAL on a
    dedicated writer thread with an fsync cadence.
    """

    def __init__(self, path: Path, sink: EventSink | None = None) -> None:
        self._path = path
        self._sink = sink
        path.parent.mkdir(parents=True, exist_ok=True)

    def emit(self, kind: str, **payload: Any) -> None:
        record = {"kind": kind, "timestamp": time.time(), "payload": payload}
        with self._path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record) + "\n")
            handle.flush()
        if self._sink is not None:
            self._sink(record)


def _cfg_float(task_spec: dict[str, Any], key: str, env: str, default: float) -> float:
    spec_value = task_spec.get(key)
    if spec_value is not None:
        return float(spec_value)
    return float(os.environ.get(env, default))


def _validate_paths(session_dir: Path, task_spec: dict[str, Any]) -> tuple[Path, Path]:
    """Path safety: worktree inside the session dir; target_file inside the worktree.

    resolve() + prefix check so ``..`` / absolute paths are rejected.
    """
    session_root = session_dir.resolve()
    scratch_repo = Path(task_spec["scratch_repo"]).resolve()
    worktree_path = Path(task_spec["worktree_path"]).resolve()
    if not worktree_path.is_relative_to(session_root):
        raise ValueError(
            f"worktree_path {worktree_path} is outside the session dir {session_root}")
    target_path = (worktree_path / task_spec["target_file"]).resolve()
    if not target_path.is_relative_to(worktree_path):
        raise ValueError(f"target_file {task_spec['target_file']!r} escapes the worktree")
    return scratch_repo, worktree_path


async def _write_json(
    proc: asyncio.subprocess.Process,
    msg: dict[str, Any],
    *,
    deadline: float | None = None,
) -> bool:
    """Write one wire message before ``deadline`` or kill its process group."""
    if proc.stdin is None or proc.stdin.is_closing():
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
        proc.stdin.write((json.dumps(msg) + "\n").encode("utf-8"))
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


async def _next_message(
    messages: asyncio.Queue[dict[str, Any] | None], deadline: float
) -> dict[str, Any] | None:
    """Next message, or None at EOF. Raises TimeoutError when deadline passes."""
    remaining = deadline - asyncio.get_running_loop().time()
    if remaining <= 0:
        raise TimeoutError
    return await asyncio.wait_for(messages.get(), remaining)


async def _run_gate(
    command: str, cwd: Path, log: EventLog, task_id: str, timeout: float
) -> int:
    """Run the gate command in the worker's worktree. Raises TimeoutError on gate timeout."""
    proc = await asyncio.create_subprocess_exec(
        "sh", "-c", command, cwd=cwd, stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=_strip_sensitive_env(dict(os.environ), worktree=cwd),
        start_new_session=True,
        pass_fds=(),
        close_fds=True,
    )
    try:
        _out, err = await asyncio.wait_for(proc.communicate(), timeout)
    except TimeoutError:
        await _kill_process_group_and_reap(proc)
        log.emit("gate", task_id=task_id, command=command, exit_code=None, timed_out=True)
        raise
    except asyncio.CancelledError:
        await _kill_process_group_and_reap(proc)
        raise
    log.emit("gate", task_id=task_id, command=command, exit_code=proc.returncode,
             stderr=err.decode("utf-8", "replace")[:512])
    return proc.returncode


async def _merge_branch(
    scratch_repo: Path,
    branch: str,
    log: EventLog,
    task_id: str,
    *,
    timeout: float | None = None,
) -> str | None:
    proc = await asyncio.create_subprocess_exec(
        "git", "merge", "--ff-only", branch, cwd=scratch_repo,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        env=_strip_sensitive_env(dict(os.environ), worktree=scratch_repo),
        start_new_session=True,
        pass_fds=(),
        close_fds=True,
    )
    try:
        communicate = proc.communicate()
        if timeout is None:
            _out, err = await communicate
        else:
            _out, err = await asyncio.wait_for(communicate, timeout)
    except TimeoutError:
        await _kill_process_group_and_reap(proc)
        log.emit("merge", task_id=task_id, branch=branch, exit_code=None, timed_out=True)
        raise
    except asyncio.CancelledError:
        await _kill_process_group_and_reap(proc)
        raise
    if proc.returncode != 0:
        log.emit("merge", task_id=task_id, branch=branch, exit_code=proc.returncode,
                 stderr=err.decode("utf-8", "replace")[:512])
        return None
    tip = await asyncio.create_subprocess_exec(
        "git", "rev-parse", "HEAD", cwd=scratch_repo,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        env=_strip_sensitive_env(dict(os.environ), worktree=scratch_repo),
        start_new_session=True,
        pass_fds=(),
        close_fds=True,
    )
    try:
        communicate = tip.communicate()
        if timeout is None:
            out, _ = await communicate
        else:
            out, _ = await asyncio.wait_for(communicate, timeout)
    except TimeoutError:
        await _kill_process_group_and_reap(tip)
        log.emit("merge", task_id=task_id, branch=branch, exit_code=None, timed_out=True)
        raise
    except asyncio.CancelledError:
        await _kill_process_group_and_reap(tip)
        raise
    sha = out.decode("utf-8", "replace").strip()
    log.emit("merge", task_id=task_id, branch=branch, exit_code=0, sha=sha)
    return sha


async def run_session(
    session_dir: str | Path,
    task_spec: dict[str, Any],
    on_event: EventSink | None = None,
) -> SliceResult:
    """Run one worker end to end and return the slice outcome.

    task_spec keys: task_id, worker (script path), scratch_repo,
    worktree_path, branch, target_file, marker, write_marker, gate
    (shell command run in the worker's worktree), spec (optional),
    ready_timeout_s / gate_timeout_s / wall_budget_s (optional; else env
    CAMBIUM_READY_TIMEOUT_S / CAMBIUM_GATE_TIMEOUT_S / CAMBIUM_WALL_BUDGET_S).
    """
    session_dir = Path(session_dir)
    log = EventLog(session_dir / ".cambium" / "events.jsonl", on_event)
    task_id = task_spec["task_id"]
    worker_script = str(task_spec["worker"])

    ready_timeout = _cfg_float(task_spec, "ready_timeout_s", "CAMBIUM_READY_TIMEOUT_S", 10.0)
    gate_timeout = _cfg_float(task_spec, "gate_timeout_s", "CAMBIUM_GATE_TIMEOUT_S", 30.0)
    wall_budget = _cfg_float(task_spec, "wall_budget_s", "CAMBIUM_WALL_BUDGET_S", 120.0)

    scratch_repo, worktree_path = _validate_paths(session_dir, task_spec)
    loop = asyncio.get_running_loop()
    wall_deadline = loop.time() + wall_budget

    log.emit("spawned", task_id=task_id, worker=worker_script)
    proc = await asyncio.create_subprocess_exec(
        sys.executable, "-u", worker_script,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        limit=WORKER_STDIN_LIMIT,
        env=_strip_sensitive_env(
            dict(os.environ),
            allowed_keys=task_spec.get("provider_env_keys", ()),
            worktree=worktree_path,
            overrides={
                "CAMBIUM_TASK_ID": task_id,
                "CAMBIUM_GENERATION": "1",
                "CAMBIUM_SESSION_ID": str(session_dir.resolve()),
            },
        ),
        start_new_session=True,
        pass_fds=(),
        close_fds=True,
    )

    messages: asyncio.Queue[dict[str, Any] | None] = asyncio.Queue()
    message_too_long = False

    async def _read_stdout() -> None:
        nonlocal message_too_long
        try:
            async for raw in proc.stdout:
                line = raw.decode("utf-8", "replace").rstrip("\n")
                if not line.strip():
                    continue
                try:
                    msg = json.loads(line)
                except json.JSONDecodeError as exc:
                    log.emit("parse_error", task_id=task_id, message=str(exc))
                    continue
                await messages.put(msg)
        except (ValueError, asyncio.LimitOverrunError) as exc:
            message_too_long = True
            log.emit(
                "protocol", task_id=task_id, note="MessageTooLong", message=str(exc)[:256]
            )
            await _kill_worker(proc)
        finally:
            await messages.put(None)  # EOF sentinel; not death by itself

    async def _read_stderr() -> None:
        async for raw in proc.stderr:
            line = raw.decode("utf-8", "replace").rstrip("\n")
            if line.strip():
                log.emit("log", task_id=task_id, stream="stderr", message=line[:512])

    stdout_task = asyncio.create_task(_read_stdout())
    stderr_task = asyncio.create_task(_read_stderr())

    init_rid = make_request_id(1)
    init_deadline = _stdin_deadline(wall_deadline)
    init_written = await _write_json(
        proc,
        {
            "type": "init", "request_id": init_rid, "task_id": task_id,
            "proto": PROTO, "generation": 1, "spec": task_spec.get("spec", ""),
            "provider_env_keys": list(task_spec.get("provider_env_keys", ())),
        },
        deadline=init_deadline,
    )
    log.emit("init", task_id=task_id, request_id=init_rid)

    run_payload = {
        "scratch_repo": str(scratch_repo),
        "worktree_path": str(worktree_path),
        "branch": task_spec["branch"],
        "target_file": task_spec["target_file"],
        "marker": task_spec["marker"],
        "write_marker": bool(task_spec.get("write_marker", True)),
        "generation": 1,
    }

    # Phase 1: init -> ready (bounded by ready_timeout and the wall budget).
    timeout_kind: str | None = None
    run_rid: str | None = None
    saw_ready = False
    timeout_kind = "stdin" if not init_written else None
    ready_deadline = (
        min(loop.time() + ready_timeout, wall_deadline)
        if init_written
        else loop.time()
    )
    while True:
        try:
            msg = await _next_message(messages, ready_deadline)
        except TimeoutError:
            if timeout_kind is None:
                timeout_kind = "ready"
            break
        if msg is None:
            break  # EOF before ready
        if msg.get("type") == "ready":
            saw_ready = True
            if msg.get("request_id") != init_rid:
                log.emit("protocol", task_id=task_id, note="ready request_id mismatch",
                         expected=init_rid, got=msg.get("request_id"))
            log.emit("ready", task_id=task_id, request_id=msg.get("request_id"),
                     pid=msg.get("pid"))
            run_rid = make_request_id(2)
            run_deadline = _stdin_deadline(wall_deadline)
            if not await _write_json(
                proc,
                {"type": "run_task", "request_id": run_rid,
                 "task_id": task_id, **run_payload},
                deadline=run_deadline,
            ):
                timeout_kind = "stdin"
                log.emit("protocol", task_id=task_id, note="run_task write failed")
            log.emit("run_task", task_id=task_id, request_id=run_rid)
            break
        log.emit("protocol", task_id=task_id, type=msg.get("type"), note="message before ready")

    # Phase 2: run_task -> result_envelope -> exit_message (bounded by the wall budget).
    result_envelope: dict[str, Any] | None = None
    result_correlated = False
    exit_reason: str | None = None
    if saw_ready and timeout_kind is None:
        while True:
            try:
                msg = await _next_message(messages, wall_deadline)
            except TimeoutError:
                timeout_kind = "wall"
                break
            if msg is None:
                await asyncio.sleep(
                    min(EOF_GRACE_S, max(0.0, wall_deadline - loop.time()))
                )
                if proc.returncode is None:
                    pong_rid = make_request_id(3)
                    pong_deadline = min(wall_deadline, loop.time() + PONG_DEADLINE_S)
                    log.emit("ping", task_id=task_id, request_id=pong_rid)
                    pong_ok = await _write_json(
                        proc,
                        {"type": "ping", "request_id": pong_rid, "task_id": task_id},
                        deadline=pong_deadline,
                    )
                    pong_correlated = False
                    while pong_ok and loop.time() < pong_deadline:
                        try:
                            response = await asyncio.wait_for(
                                messages.get(), pong_deadline - loop.time()
                            )
                        except TimeoutError:
                            break
                        if response is None:
                            break
                        if response.get("type") != "pong":
                            continue
                        if response.get("request_id") == pong_rid:
                            pong_correlated = True
                            log.emit("pong", task_id=task_id, request_id=pong_rid)
                            try:
                                await asyncio.wait_for(
                                    proc.wait(),
                                    min(EOF_GRACE_S, max(0.0, wall_deadline - loop.time())),
                                )
                            except TimeoutError:
                                await _kill_worker(proc)
                            break
                        log.emit(
                            "protocol", task_id=task_id,
                            note="pong request_id mismatch",
                            expected=pong_rid, got=response.get("request_id"),
                        )
                    if not pong_correlated:
                        timeout_kind = "pong"
                        await _kill_worker(proc)
                break  # EOF without exit_message
            mtype = msg.get("type")
            if mtype == "result_envelope":
                result_envelope = msg
                result_correlated = msg.get("request_id") == run_rid
                if not result_correlated:
                    log.emit("protocol", task_id=task_id,
                             note="result_envelope request_id mismatch",
                             expected=run_rid, got=msg.get("request_id"))
                log.emit("result", task_id=task_id, request_id=msg.get("request_id"),
                         status=msg.get("status"))
            elif mtype == "exit_message":
                exit_reason = msg.get("reason")
                log.emit("exit", task_id=task_id, reason=exit_reason)
                break
            else:
                log.emit("protocol", task_id=task_id, type=mtype or "<missing>")

    if timeout_kind is not None:
        await _kill_worker(proc)
        log.emit("timeout", task_id=task_id, phase=timeout_kind)

    worker_exit_code = await proc.wait()
    await asyncio.gather(stdout_task, stderr_task, return_exceptions=True)

    worker_status = result_envelope.get("status") if result_envelope else None
    gate_rc: int | None = None
    merge_sha: str | None = None
    timed_out = timeout_kind is not None
    worker_bad = worker_exit_code != 0
    missing_exit = exit_reason is None
    missing_result = result_envelope is None or not result_correlated

    if message_too_long:
        log.emit("protocol", task_id=task_id, note="message too long; worker failed")
    if timed_out:
        status, exit_code = "failed", 3
    elif message_too_long:
        status, exit_code = "failed", 1
    elif worker_bad:
        status, exit_code = "failed", worker_exit_code if worker_exit_code > 0 else 1
    elif missing_exit:
        status, exit_code = "failed", 1
    elif missing_result:
        status, exit_code = "failed", 1
    else:
        # Primary signal: the envelope's status; the gate is the verification step.
        worktree = Path(run_payload["worktree_path"])
        if worktree.exists():
            remaining = wall_deadline - loop.time()
            try:
                gate_rc = await _run_gate(
                    task_spec["gate"], worktree, log, task_id, timeout=min(gate_timeout, remaining))
            except TimeoutError:
                gate_rc = None
                timed_out = True
                timeout_kind = "gate"
                log.emit("timeout", task_id=task_id, phase="gate")
        if not timed_out:
            if worker_status == "succeeded" and gate_rc == 0:
                status = "succeeded"
                try:
                    merge_sha = await _merge_branch(
                        scratch_repo,
                        run_payload["branch"],
                        log,
                        task_id,
                        timeout=max(0.0, wall_deadline - loop.time()),
                    )
                except TimeoutError:
                    status = "failed"
                    timed_out = True
                    timeout_kind = "merge"
                else:
                    if merge_sha is None:
                        status = "failed"
            else:
                status = "failed"
            exit_code = 0 if status == "succeeded" else 1
        else:
            status, exit_code = "failed", 3

    log.emit("session_ended", task_id=task_id, status=status, exit_code=exit_code,
             worker_exit_code=worker_exit_code, saw_ready=saw_ready,
             exit_reason=exit_reason, timed_out=timed_out, timeout_phase=timeout_kind)
    return SliceResult(status=status, exit_code=exit_code,
                       worker_exit_code=worker_exit_code,
                       worker_status=worker_status, gate_exit_code=gate_rc,
                       merge_sha=merge_sha, timed_out=timed_out,
                       timeout_phase=timeout_kind)


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
# respawn; results are gated and merged atomically onto refs/heads/main.
#
# cambium.store / cambium.merge / cambium.worker are dependency contracts
# that may live in sibling worktrees (import-guarded below). When absent,
# this module provides minimal drop-in stand-ins (_FallbackEventStore /
# _FallbackSequencer) so the runtime is fully exercisable.
# =====================================================================

DEFAULT_READY_TIMEOUT_S = 10.0
DEFAULT_HEARTBEAT_INTERVAL_S = 15.0
DEFAULT_HEARTBEAT_TIMEOUT_S = 90.0
DEFAULT_GATE_TIMEOUT_S = 30.0
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

CRITICAL_KINDS = frozenset({
    "result", "checkpoint", "worker_exit", "task_failed",
    "merge_progress", "task_assigned", "merge_committed",
})

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
    return build_subprocess_env(
        env,
        allowed_keys=allowed_keys,
        worktree=worktree,
        overrides=overrides,
    )


class NonFastForwardError(RuntimeError):
    """``refs/heads/main`` moved away from the expected-old SHA at publish time."""

    def __init__(
        self, *, new_tip: str, expected_old: str, current: str | None = None, detail: str = ""
    ) -> None:
        self.new_tip = new_tip
        self.expected_old = expected_old
        self.current = current
        self.detail = detail
        where = current or "unknown"
        message = (
            f"non-fast-forward publish of {new_tip}: refs/heads/main is at "
            f"{where} but expected {expected_old}"
        )
        if detail:
            message += f" ({detail})"
        super().__init__(message)


class MergeConflictError(RuntimeError):
    """A rebase/merge of the worker branch onto the base hit conflicts."""

    def __init__(self, message: str, conflicts: list[str] | None = None) -> None:
        super().__init__(message)
        self.conflicts = list(conflicts or [])


class _FallbackEventStore:
    """Minimal SQLite WAL event store mirroring cambium.store.EventStore's
    append/events_after/close contract; used when cambium.store is absent.

    append() blocks for critical kinds (a WAL checkpoint + fsync, i.e. the
    durability contract of architecture §6.5 reduced to the essentials);
    events_after() replays rows in seq order from a fresh read connection.
    """

    _SCHEMA = """CREATE TABLE IF NOT EXISTS events (
        seq          INTEGER PRIMARY KEY AUTOINCREMENT,
        kind         TEXT    NOT NULL,
        payload      TEXT    NOT NULL,
        ts           REAL,
        monotonic_ms INTEGER,
        task_id      TEXT,
        worker_id    TEXT,
        generation   INTEGER,
        request_id   TEXT
    )"""

    _SELECT_AFTER = (
        "SELECT seq, kind, payload, ts, monotonic_ms, task_id, worker_id, "
        "generation, request_id FROM events WHERE seq > ? ORDER BY seq"
    )

    def __init__(self, path: Path, *, fsync_interval_s: float = 1.0) -> None:
        self._path = Path(path)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(self._path, check_same_thread=False)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._conn.execute("PRAGMA wal_autocheckpoint=0")
        self._conn.execute(self._SCHEMA)
        self._closed = False

    def append(self, event: dict[str, Any]) -> int:
        kind = event.get("kind")
        if not isinstance(kind, str) or not kind:
            raise ValueError("event requires a non-empty string 'kind'")
        with self._conn:
            cur = self._conn.execute(
                "INSERT INTO events(kind, payload, ts, monotonic_ms, task_id, "
                "worker_id, generation, request_id) VALUES(?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    kind,
                    json.dumps(event.get("payload", {})),
                    event.get("ts"),
                    event.get("monotonic_ms"),
                    event.get("task_id"),
                    event.get("worker_id"),
                    event.get("generation"),
                    event.get("request_id"),
                ),
            )
            seq = int(cur.lastrowid)
        if kind in CRITICAL_KINDS:
            self._conn.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchall()
        return seq

    def events_after(self, seq: int) -> list[dict[str, Any]]:
        conn = sqlite3.connect(self._path)
        try:
            rows = conn.execute(_FallbackEventStore._SELECT_AFTER, (seq,)).fetchall()
        finally:
            conn.close()
        return [
            {
                "seq": row[0], "kind": row[1], "payload": json.loads(row[2]),
                "ts": row[3], "monotonic_ms": row[4], "task_id": row[5],
                "worker_id": row[6], "generation": row[7], "request_id": row[8],
            }
            for row in rows
        ]

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            self._conn.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchall()
        except sqlite3.Error:
            pass
        self._conn.close()


class _FallbackSequencer:
    """Duck-typed stand-in for cambium.merge.MergeSequencer.

    prepare_staging(repo, worktree_path, branch, base) -> staging tip SHA,
    rebasing the branch onto base inside a throwaway worktree (raises
    MergeConflictError on conflict, capturing the staging SHA under
    refs/cambium/staging/<id> before the throwaway can die);
    publish_merge(repo, new_tip, expected_old) atomically fast-forwards
    refs/heads/main via ``git update-ref`` (raises NonFastForwardError when
    the ref moved); cleanup_staging(repo) removes the throwaway.
    """

    _UNMERGED = {"DD", "AU", "UD", "UA", "DU", "AA", "UU"}

    def __init__(self, task_id: str | None = None) -> None:
        self._task_id = task_id
        self._worktree_path: Path | None = None
        self._staging_branch: str | None = None
        self._staging_ref: str | None = None

    @staticmethod
    def _env(
        cwd: Path | None = None,
        overrides: dict[str, str] | None = None,
    ) -> dict[str, str]:
        return build_subprocess_env(worktree=cwd, overrides=overrides)

    def _run(
        self, cwd: Path, *args: str, check: bool = True, env: dict[str, str] | None = None
    ) -> subprocess.CompletedProcess[str]:
        overrides = {
            key: env[key]
            for key in ("GIT_EDITOR", "GIT_SEQUENCE_EDITOR")
            if env is not None and key in env
        }
        result = subprocess.run(
            ["git", "-C", str(cwd), *args], capture_output=True, text=True,
            env=self._env(Path(cwd), overrides),
            start_new_session=True,
        )
        if check and result.returncode != 0:
            raise RuntimeError(
                f"git {args[0]} failed (rc={result.returncode}) in {cwd}: "
                f"{(result.stderr + result.stdout).strip()[:512]}"
            )
        return result

    def _is_registered(self, repo: Path, path: Path) -> bool:
        result = self._run(repo, "worktree", "list", "--porcelain")
        wanted = os.path.abspath(path)
        for line in result.stdout.splitlines():
            if line.startswith("worktree ") and os.path.abspath(line[9:].strip()) == wanted:
                return True
        return False

    def prepare_staging(
        self, repo: Path, worktree_path: Path, branch: str, base: str
    ) -> str:
        repo = Path(repo)
        worktree_path = Path(worktree_path).resolve()
        ident = self._task_id or branch
        self._staging_branch = f"cambium-merge/{ident}"
        self._staging_ref = f"refs/cambium/staging/{ident}"
        self._worktree_path = worktree_path
        if self._is_registered(repo, worktree_path):
            self._run(worktree_path, "rebase", "--abort", check=False)
            self._run(repo, "worktree", "remove", "--force", str(worktree_path), check=False)
        base_tip = self._run(repo, "rev-parse", f"{base}^{{commit}}").stdout.strip()
        worker_tip = self._run(
            repo, "rev-parse", f"refs/heads/{branch}^{{commit}}"
        ).stdout.strip()
        self._run(
            repo, "worktree", "add", "-B", self._staging_branch, str(worktree_path), worker_tip,
            check=True,
        )
        env = dict(self._env())
        env["GIT_EDITOR"] = "true"
        env["GIT_SEQUENCE_EDITOR"] = "true"
        rebase = self._run(worktree_path, "rebase", base_tip, check=False, env=env)
        if rebase.returncode != 0:
            conflicts = self._conflicted_paths(worktree_path, rebase.stdout + rebase.stderr)
            self._run(worktree_path, "rebase", "--abort", check=False)
            raise MergeConflictError(
                f"rebase of {branch} onto {base_tip} failed; "
                f"conflicted paths: {conflicts or '(none detected)'}",
                conflicts,
            )
        staging_tip = self._run(worktree_path, "rev-parse", "HEAD").stdout.strip()
        # capture BEFORE any worktree removal so the tip survives cleanup
        self._run(repo, "update-ref", self._staging_ref, staging_tip, check=True)
        return staging_tip

    def publish_merge(self, repo: Path, new_tip: str, expected_old: str) -> None:
        result = self._run(
            repo, "update-ref", "refs/heads/main", new_tip, expected_old, check=False
        )
        if result.returncode == 0:
            return
        detail = (result.stderr + result.stdout).strip()
        match = re.search(r"is at ([0-9a-f]{40}) but expected", detail)
        current = match.group(1) if match else None
        if current is not None or "reference already exists" in detail:
            raise NonFastForwardError(
                new_tip=new_tip, expected_old=expected_old, current=current, detail=detail[:512]
            )
        raise RuntimeError(f"git update-ref refs/heads/main failed: {detail[:512]}")

    def cleanup_staging(self, repo: Path) -> None:
        repo = Path(repo)
        if self._worktree_path is not None:
            if self._is_registered(repo, self._worktree_path):
                self._run(self._worktree_path, "rebase", "--abort", check=False)
                self._run(
                    repo, "worktree", "remove", "--force", str(self._worktree_path), check=False
                )
            self._worktree_path = None
        if self._staging_branch is not None:
            self._run(repo, "branch", "-D", self._staging_branch, check=False)
        if self._staging_ref is not None:
            self._run(repo, "update-ref", "-d", self._staging_ref, check=False)
        self._staging_branch = None
        self._staging_ref = None

    def _conflicted_paths(self, worktree_path: Path, rebase_output: str) -> list[str]:
        status = self._run(worktree_path, "status", "--porcelain", check=False)
        conflicts: list[str] = []
        if status.returncode == 0:
            for line in status.stdout.splitlines():
                if len(line) < 3:
                    continue
                if line[:2] in _FallbackSequencer._UNMERGED:
                    path = line[3:].strip()
                    if " -> " in path:
                        path = path.split(" -> ", 1)[1]
                    if path:
                        conflicts.append(path)
        if conflicts:
            return list(dict.fromkeys(conflicts))
        return list(
            re.findall(r"CONFLICT \([^)]*\): Merge conflict in (\S+)", rebase_output)
        )


def _resolve_merge_sequencer() -> type | None:
    try:
        mod = importlib.import_module("cambium.merge")
        return mod.MergeSequencer
    except (ImportError, AttributeError):
        return None


def _resolve_event_store() -> type | None:
    try:
        mod = importlib.import_module("cambium.store")
        return mod.EventStore
    except (ImportError, AttributeError):
        return None


def _open_store(session_dir: Path) -> Any:
    path = Path(session_dir) / ".cambium" / "events.db"
    cls = _resolve_event_store()
    if cls is not None:
        return cls(path)
    return _FallbackEventStore(path)


def read_events(session_dir: Path | str, after_seq: int = 0) -> list[dict[str, Any]]:
    """Replay the session's durable event log from ``after_seq`` (arch §6.3)."""
    store = _open_store(session_dir)
    try:
        return store.events_after(after_seq)
    finally:
        store.close()


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
    fatal: bool = False  # restarting cannot help (spawn error)
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
    ) -> None:
        self._session_dir = Path(session_dir)
        self._store = store
        self._on_event = on_event
        self._queue: asyncio.Queue[dict[str, Any] | None] = asyncio.Queue()
        self._writer_task: asyncio.Task[None] | None = None
        self._handles: dict[str, WorkerHandle] = {}
        self._results: dict[str, TaskResult] = {}
        self._gates: dict[str, dict[str, int]] = {}
        self._worktree_lock = asyncio.Lock()
        self._merge_lock = asyncio.Lock()
        self._rid = 0
        self._merge_cls = _resolve_merge_sequencer()

    # -- event path ---------------------------------------------------------

    def _next_rid(self) -> str:
        self._rid += 1
        return make_request_id(self._rid)

    async def emit(
        self, kind: str, *, task_id: str | None = None, generation: int | None = None,
        request_id: str | None = None, **payload: Any,
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
        self._queue.put_nowait(record)
        if self._on_event is not None:
            result = self._on_event(record)
            if asyncio.iscoroutine(result):
                await result

    async def _writer_loop(self) -> None:
        while True:
            record = await self._queue.get()
            if record is None:
                return
            try:
                await asyncio.to_thread(self._store.append, record)
            except Exception as exc:  # pragma: no cover - storage must not kill the session
                print(f"cambium: event store error: {exc}", file=sys.stderr)

    async def start(self) -> None:
        self._writer_task = asyncio.create_task(self._writer_loop())

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
        if self._writer_task is not None:
            self._queue.put_nowait(None)
            try:
                await asyncio.wait_for(self._writer_task, 10.0)
            except BaseException:
                pass
        try:
            await asyncio.to_thread(self._store.close)
        except BaseException:
            pass

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
            env=_strip_sensitive_env(dict(os.environ), worktree=path),
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

    # -- worktree lifecycle --------------------------------------------------

    async def _ensure_worktree(self, spec: dict[str, Any]) -> int:
        async with self._worktree_lock:
            return await self._ensure_worktree_locked(spec)

    async def _ensure_worktree_locked(
        self, spec: dict[str, Any], generation: int | None = None
    ) -> int:
        repo = Path(spec["repo"])
        worktree = Path(spec["worktree_path"]).resolve()
        branch = spec["branch"]
        base = spec["base_commit"]
        await self._git(repo, "worktree", "prune", check=False)
        listing = await self._git_stdout(repo, "worktree", "list", "--porcelain") or ""
        if str(worktree) in listing:
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
        repo = Path(spec["repo"])
        worktree = Path(spec["worktree_path"]).resolve()
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
        await self._git(worktree, "reset", "--hard", spec["base_commit"], check=False)
        await self._git(worktree, "clean", "-fd", "-e", ".cambium/", check=False)
        await self.emit(
            "recover", task_id=spec["task_id"], generation=new_generation,
            base_commit=spec["base_commit"],
        )
        return new_generation

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
        worktree = Path(spec["worktree_path"]).resolve()
        return _strip_sensitive_env(
            dict(os.environ),
            allowed_keys=spec.get("provider_env_keys", ()),
            worktree=worktree,
            overrides={
                "CAMBIUM_TASK_ID": spec["task_id"],
                "CAMBIUM_GENERATION": str(generation),
                "CAMBIUM_SESSION_ID": str(self._session_dir.resolve()),
            },
        )

    def _run_payload(
        self, spec: dict[str, Any], run_rid: str, wall_budget: float, generation: int
    ) -> dict[str, Any]:
        repo = Path(spec["repo"])
        return {
            "task_id": spec["task_id"],
            "task": spec.get("task", ""),
            "repo": str(repo),
            "scratch_repo": str(repo),
            "worktree_path": str(Path(spec["worktree_path"]).resolve()),
            "branch": spec["branch"],
            "gate": spec.get("gate", ""),
            "base_commit": spec["base_commit"],
            "target_file": spec.get("target_file"),
            "marker": spec.get("marker"),
            "write_marker": bool(spec.get("write_marker", True)),
            "generation": generation,
            "max_turns": int(spec.get("max_turns", DEFAULT_MAX_TURNS)),
            "max_tokens": int(spec.get("max_tokens", DEFAULT_MAX_TOKENS)),
            "max_wall_s": wall_budget,
        }

    # -- per-task supervision ------------------------------------------------

    async def supervise_task(self, spec: dict[str, Any]) -> None:
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

        await self.emit(
            "task_assigned", task_id=task_id, repo=str(repo), branch=spec["branch"],
            base_commit=spec["base_commit"], task=spec.get("task", ""),
        )
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
            if outcome.clean:
                gate_rc = await self._run_gate(spec, worktree)
                verdict_ok = bool(
                    outcome.envelope and outcome.envelope.get("status") == "succeeded"
                )
                if verdict_ok and gate_rc == 0:
                    merged = await self._merge_task(spec, handle)
                    if merged is not None:
                        self._results[task_id] = TaskResult(
                            task_id=task_id, status="succeeded", exit_code=0,
                            reason=None, merge_sha=merged, gate_exit_code=gate_rc,
                            restarts=restarts,
                        )
                    else:
                        self._results[task_id] = TaskResult(
                            task_id=task_id, status="failed", exit_code=1,
                            reason="merge_failed", gate_exit_code=gate_rc, restarts=restarts,
                        )
                else:
                    reason = "gate_failed" if gate_rc != 0 else "worker_verdict_failed"
                    self._results[task_id] = TaskResult(
                        task_id=task_id, status="failed", exit_code=1, reason=reason,
                        gate_exit_code=gate_rc, restarts=restarts,
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

        await self.emit("spawned", task_id=task_id, generation=generation, worker=" ".join(cmd))
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                limit=WORKER_STDIN_LIMIT,
                cwd=str(worktree),
                env=_strip_sensitive_env(
                    env,
                    allowed_keys=spec.get("provider_env_keys", ()),
                    worktree=worktree,
                    overrides={
                        "CAMBIUM_TASK_ID": task_id,
                        "CAMBIUM_GENERATION": str(generation),
                        "CAMBIUM_SESSION_ID": str(self._session_dir.resolve()),
                    },
                ),
                start_new_session=True,
                pass_fds=(),
                close_fds=True,
            )
        except (FileNotFoundError, OSError, PermissionError) as exc:
            return _GenOutcome(clean=False, fatal=True, reason=f"spawn failed: {exc}")
        handle.proc = proc
        handle.state = "SPAWNING"

        messages: asyncio.Queue[dict[str, Any] | None] = asyncio.Queue()
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
                await messages.put(None)

        async def _read_stderr() -> None:
            async for raw in proc.stderr:
                line = raw.decode("utf-8", "replace").rstrip("\n")
                if line.strip():
                    await self.emit(
                        "log", task_id=task_id, generation=generation, stream="stderr",
                        message=line[:512],
                    )

        stdout_task = asyncio.create_task(_read_stdout())
        stderr_task = asyncio.create_task(_read_stderr())
        loop = asyncio.get_running_loop()
        wall_deadline = loop.time() + wall_budget

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
        timeout_phase: str | None = "stdin" if not init_written else None

        async def _cancel_and_kill() -> None:
            try:
                await _write_json(
                    proc,
                    {
                        "type": "cancel",
                        "request_id": self._next_rid(),
                        "reason": timeout_phase or "timeout",
                    },
                    deadline=_stdin_deadline(wall_deadline),
                )
            except Exception:
                pass
            await _kill_worker(proc)

        async def _probe_after_eof() -> bool:
            """Require one exact pong before treating an EOF survivor as live."""
            nonlocal timeout_phase
            if proc.returncode is not None:
                return False
            pong_rid = self._next_rid()
            pong_deadline = min(wall_deadline, loop.time() + PONG_DEADLINE_S)
            await self.emit(
                "ping", task_id=task_id, generation=generation, request_id=pong_rid
            )
            if not await _write_json(
                proc,
                {"type": "ping", "request_id": pong_rid, "task_id": task_id,
                 "generation": generation},
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
                if mtype == "ready":
                    phase = "run"
                    last_heartbeat = loop.time()
                    handle.state = "RUNNING"
                    if msg.get("request_id") != init_rid:
                        await self.emit(
                            "protocol", task_id=task_id, note="ready request_id mismatch",
                            expected=init_rid, got=msg.get("request_id"),
                        )
                    await self.emit(
                        "ready", task_id=task_id, request_id=msg.get("request_id"),
                        generation=generation, pid=msg.get("pid"), proto=msg.get("proto"),
                    )
                    run_rid = self._next_rid()
                    payload = self._run_payload(spec, run_rid, wall_budget, generation)
                    if not await _write_json(
                        proc,
                        {"type": "run_task", "request_id": run_rid,
                         "task_id": task_id, **payload},
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
                    await self.emit(
                        "result", task_id=task_id, request_id=msg.get("request_id"),
                        status=msg.get("status"), generation=generation,
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
                    )
                elif mtype in ("tool_event", "pong"):
                    await self.emit(
                        "tool_event" if mtype == "tool_event" else "log",
                        task_id=task_id, generation=generation, tool=msg.get("tool"),
                        cmd=msg.get("cmd"),
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
        elif exit_code != 0:
            reason = f"worker_exit_{exit_code}"
        elif exit_reason is None:
            reason = "missing_exit_message"
        elif envelope is None:
            reason = "missing_result_envelope"
        else:
            reason = "result_request_id_mismatch"
        return _GenOutcome(
            clean=clean, fatal=False, reason=reason, timeout_phase=timeout_phase,
            exit_code=exit_code, exit_reason=exit_reason, envelope=envelope,
            correlated=correlated,
        )

    # -- gate ----------------------------------------------------------------

    async def _run_gate(self, spec: dict[str, Any], worktree: Path) -> int:
        """Run the task's gate command in the worktree (30s, bounded capture);
        skip a rerun when the worktree tree hash is unchanged since the last run."""
        task_id = spec["task_id"]
        gate = spec.get("gate", "true")
        timeout = _cfg_float(
            spec, "gate_timeout_s", "CAMBIUM_GATE_TIMEOUT_S", DEFAULT_GATE_TIMEOUT_S
        )
        if not Path(worktree).exists():
            await self.emit("gate", task_id=task_id, exit_code=127, note="worktree missing")
            return 127
        tree = await self._git_stdout(Path(worktree), "write-tree", check=False)
        gates = self._gates.setdefault(task_id, {})
        if tree is not None and tree in gates:
            rc = gates[tree]
            await self.emit("gate", task_id=task_id, exit_code=rc, skipped=True, tree=tree)
            return rc
        proc = await asyncio.create_subprocess_exec(
            "sh", "-c", gate, cwd=str(worktree),
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            env=_strip_sensitive_env(dict(os.environ), worktree=worktree),
            start_new_session=True,
            pass_fds=(),
            close_fds=True,
        )
        try:
            out, err = await asyncio.wait_for(proc.communicate(), timeout)
            rc = proc.returncode if proc.returncode is not None else 1
        except TimeoutError:
            await _kill_process_group_and_reap(proc)
            rc = 124
            await self.emit("gate", task_id=task_id, exit_code=rc, tree=tree, timed_out=True)
        except asyncio.CancelledError:
            await _kill_process_group_and_reap(proc)
            raise
        else:
            if tree is not None:
                gates[tree] = rc
            await self.emit("gate", task_id=task_id, exit_code=rc, tree=tree, timed_out=False)
            output = (out or b"") + (err or b"")
            if output:
                await self.emit(
                    "log", task_id=task_id, stream="gate",
                    message=output.decode("utf-8", "replace")[:2048],
                )
        return rc

    # -- merge ---------------------------------------------------------------

    def _make_sequencer(self, task_id: str) -> Any:
        if self._merge_cls is not None:
            return self._merge_cls(task_id=task_id)
        return _FallbackSequencer(task_id=task_id)

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
        throwaway = self._session_dir / ".cambium" / "merge-wt" / task_id
        seq = self._make_sequencer(task_id)
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
                await asyncio.to_thread(seq.publish_merge, repo, staging_tip, current_main)
        except Exception as exc:
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
            return None
        finally:
            try:
                if hasattr(seq, "cleanup_staging"):
                    await asyncio.to_thread(seq.cleanup_staging, repo)
            except Exception:
                pass
        await self.emit(
            "merge_committed", task_id=task_id, old=current_main, new=staging_tip,
            branch=branch, generation=handle.generation,
        )
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


async def run_plan(
    session_dir: str | Path,
    plan: dict[str, Any] | list[dict[str, Any]],
    on_event: EventSink | None = None,
) -> PlanResult:
    """Run every task in the plan concurrently under one supervisor session.

    Workers are spawned as ``python -m cambium.worker`` (or the task's
    ``worker`` script); results are gated and merged onto ``refs/heads/main``.
    Returns a PlanResult; the session's event log is durable in
    ``<session_dir>/.cambium/events.db`` (readable via ``read_events``).
    """
    session_dir = Path(session_dir)
    tasks = _plan_tasks(plan)
    _reject_duplicate_task_ids(tasks)
    specs = [_validate_plan_task(session_dir, t) for t in tasks]
    if not specs:
        raise ValueError("plan contains no tasks")

    store = _open_store(session_dir)
    runtime = _Runtime(session_dir, store, on_event=on_event)
    await runtime.start()
    cancelled = False
    try:
        async with asyncio.TaskGroup() as tg:
            for spec in specs:
                tg.create_task(runtime.supervise_task(spec))
    except asyncio.CancelledError:
        cancelled = True
        raise
    except BaseExceptionGroup as exc_group:
        await runtime.emit(
            "log", task_id=None, message=f"task group exception: {exc_group}"
        )
    finally:
        await runtime.shutdown(session_status="cancelled" if cancelled else "ended")
    return runtime.plan_result()


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
        env=_strip_sensitive_env(dict(os.environ), worktree=repo),
    )
    if rc.returncode != 0:
        _sh("git", "-C", str(repo), "commit", "--allow-empty", "-m", "cambium initial")


def _default_spec(session_dir: Path) -> dict[str, Any]:
    root = Path(__file__).resolve().parents[2]
    return {
        "task_id": "slice-001",
        "worker": str(root / "scripts" / "fake_worker.py"),
        "scratch_repo": str(session_dir / "scratch"),
        "worktree_path": str(session_dir / "wt"),
        "branch": "wt-slice-001",
        "target_file": "hello.txt",
        "marker": "// cambium-slice",
        "write_marker": True,
        "gate": "grep -q '// cambium-slice' hello.txt",
        "spec": "append the cambium-slice marker line to the target file",
    }


def _load_task_spec(session_dir: Path, spec_path: str | None) -> dict[str, Any]:
    if spec_path:
        return json.loads(Path(spec_path).read_text())
    explicit = session_dir / "task.json"
    if explicit.exists():
        return json.loads(explicit.read_text())
    return _default_spec(session_dir)


def _sh(*args: str, cwd: str | Path | None = None) -> None:
    subprocess.run(
        args,
        cwd=cwd,
        check=True,
        capture_output=True,
        env=_strip_sensitive_env(dict(os.environ)),
    )


def _bootstrap_scratch(repo: Path, task_spec: dict[str, Any]) -> None:
    """CLI convenience: turn a missing/empty scratch dir into a git repo.

    The library's run_session never creates repos; this exists so the
    documented manual run works from an empty --session-dir.
    """
    if (repo / ".git").exists():
        return
    _sh("git", "init", "-b", "main", str(repo))
    _sh("git", "-C", str(repo), "config", "user.name", "cambium-slice")
    _sh("git", "-C", str(repo), "config", "user.email", "slice@example.com")
    _sh("git", "-C", str(repo), "config", "gc.auto", "0")
    target = repo / task_spec["target_file"]
    if not target.exists():
        target.write_text("hello from the vertical slice\n")
    _sh("git", "-C", str(repo), "add", task_spec["target_file"])
    _sh("git", "-C", str(repo), "commit", "-m", "initial")


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
            f"reason={r.reason} merge={r.merge_sha} gate={r.gate_exit_code} "
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
        "\"worktree_path\", \"branch\", \"gate\", \"base_commit\", ...}]} "
        "(multi-worker mode)",
    )
    parser.add_argument(
        "--task-spec",
        help=(
            "path to task spec JSON (slice mode; default: <session-dir>/task.json, "
            "else built-in defaults)"
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
    task_spec = _load_task_spec(session_dir, args.task_spec)
    _validate_paths(session_dir, task_spec)
    _bootstrap_scratch(Path(task_spec["scratch_repo"]), task_spec)

    def print_event(record: dict[str, Any]) -> None:
        print(f'{record["kind"]:>14}  {json.dumps(record["payload"])}', flush=True)

    result = asyncio.run(run_session(session_dir, task_spec, on_event=print_event))
    print(
        f"result: status={result.status} exit_code={result.exit_code} "
        f"worker_exit={result.worker_exit_code} worker_status={result.worker_status} "
        f"gate_exit={result.gate_exit_code} merge={result.merge_sha} "
        f"timed_out={result.timed_out} timeout_phase={result.timeout_phase}",
        flush=True,
    )
    return result.exit_code


if __name__ == "__main__":
    sys.exit(main())
