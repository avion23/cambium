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
import hashlib
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

PROTO = 1
WORKER_STDIN_LIMIT = 1_048_576

EventSink = Callable[[dict[str, Any]], None]


def make_request_id(seq: int) -> str:
    """Monotonic-ish request id. Not a ULID (no deps in the slice)."""
    return f"{time.time_ns():x}-{seq:04x}"


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


async def _write_json(proc: asyncio.subprocess.Process, msg: dict[str, Any]) -> bool:
    try:
        proc.stdin.write((json.dumps(msg) + "\n").encode("utf-8"))
        await proc.stdin.drain()
        return True
    except (BrokenPipeError, ConnectionResetError):
        return False


async def _kill_worker(proc: asyncio.subprocess.Process) -> None:
    """SIGKILL the worker's process group (worker is its own session/group leader)."""
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
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
        env=_strip_sensitive_env(dict(os.environ)),
    )
    try:
        _out, err = await asyncio.wait_for(proc.communicate(), timeout)
    except TimeoutError:
        proc.kill()
        await proc.wait()
        log.emit("gate", task_id=task_id, command=command, exit_code=None, timed_out=True)
        raise
    log.emit("gate", task_id=task_id, command=command, exit_code=proc.returncode,
             stderr=err.decode("utf-8", "replace")[:512])
    return proc.returncode


async def _merge_branch(scratch_repo: Path, branch: str, log: EventLog, task_id: str) -> str | None:
    proc = await asyncio.create_subprocess_exec(
        "git", "merge", "--ff-only", branch, cwd=scratch_repo,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        env=_strip_sensitive_env(dict(os.environ)),
    )
    _out, err = await proc.communicate()
    if proc.returncode != 0:
        log.emit("merge", task_id=task_id, branch=branch, exit_code=proc.returncode,
                 stderr=err.decode("utf-8", "replace")[:512])
        return None
    tip = await asyncio.create_subprocess_exec(
        "git", "rev-parse", "HEAD", cwd=scratch_repo,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        env=_strip_sensitive_env(dict(os.environ)),
    )
    out, _ = await tip.communicate()
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
        env=_strip_sensitive_env({**os.environ, "PYTHONUNBUFFERED": "1",
                                  "CAMBIUM_TASK_ID": task_id, "CAMBIUM_GENERATION": "1"}),
        start_new_session=True,
    )

    messages: asyncio.Queue[dict[str, Any] | None] = asyncio.Queue()

    async def _read_stdout() -> None:
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
        await messages.put(None)  # EOF sentinel; not death by itself

    async def _read_stderr() -> None:
        async for raw in proc.stderr:
            line = raw.decode("utf-8", "replace").rstrip("\n")
            if line.strip():
                log.emit("log", task_id=task_id, stream="stderr", message=line[:512])

    stdout_task = asyncio.create_task(_read_stdout())
    stderr_task = asyncio.create_task(_read_stderr())

    init_rid = make_request_id(1)
    await _write_json(proc, {
        "type": "init", "request_id": init_rid, "task_id": task_id,
        "proto": PROTO, "generation": 1, "spec": task_spec.get("spec", ""),
    })
    log.emit("init", task_id=task_id, request_id=init_rid)

    run_payload = {
        "scratch_repo": str(scratch_repo),
        "worktree_path": str(worktree_path),
        "branch": task_spec["branch"],
        "target_file": task_spec["target_file"],
        "marker": task_spec["marker"],
        "write_marker": bool(task_spec.get("write_marker", True)),
    }

    # Phase 1: init -> ready (bounded by ready_timeout and the wall budget).
    timeout_kind: str | None = None
    run_rid: str | None = None
    saw_ready = False
    ready_deadline = min(loop.time() + ready_timeout, wall_deadline)
    while True:
        try:
            msg = await _next_message(messages, ready_deadline)
        except TimeoutError:
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
            if not await _write_json(proc, {"type": "run_task", "request_id": run_rid,
                                            "task_id": task_id, **run_payload}):
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

    if timed_out:
        status, exit_code = "failed", 3
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
                merge_sha = await _merge_branch(scratch_repo, run_payload["branch"], log, task_id)
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
# cambium.store and cambium.merge are runtime dependency contracts.
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
    "merge_staging_quarantined", "merge_staging_cleanup_failed",
    "merge_staging_prune_started", "merge_staging_pruned",
})

_API_KEY_RE = re.compile(
    r"(api|key|token|secret|password|passwd|credential|authorization)", re.IGNORECASE
)


def _strip_sensitive_env(env: dict[str, str]) -> dict[str, str]:
    """Drop env keys with API-key-ish names; keep everything else (arch §9)."""
    return {k: v for k, v in env.items() if not _API_KEY_RE.search(k)}


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

    def __new__(cls, *args: Any, **kwargs: Any) -> Any:
        """Delegate to the canonical sequencer so fallback behavior cannot drift."""
        mod = importlib.import_module("cambium.merge")
        return mod.MergeSequencer(*args, **kwargs)

    def __init__(self, task_id: str | None = None) -> None:
        self._task_id = task_id
        self._worktree_path: Path | None = None
        self._staging_branch: str | None = None
        self._staging_ref: str | None = None

    @staticmethod
    def _env() -> dict[str, str]:
        env = dict(os.environ)
        env.pop("GIT_QUARANTINE_PATH", None)
        return env

    def _run(
        self, cwd: Path, *args: str, check: bool = True, env: dict[str, str] | None = None
    ) -> subprocess.CompletedProcess[str]:
        result = subprocess.run(
            ["git", "-C", str(cwd), *args], capture_output=True, text=True,
            env=self._env() if env is None else env,
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
            raise RuntimeError("canonical merge sequencer required for existing staging")
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
                raise RuntimeError("canonical merge sequencer required for staging cleanup")
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
        request_id: str | None = None, _observer_failure_is_fatal: bool | None = None,
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
        if kind in CRITICAL_KINDS:
            await asyncio.to_thread(self._store.append, record)
        else:
            self._queue.put_nowait(record)
        if self._on_event is not None:
            observer_failure_is_fatal = (
                _observer_failure_is_fatal
                if _observer_failure_is_fatal is not None
                else kind not in CRITICAL_KINDS
            )
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
            ["git", "-C", str(path), *args], capture_output=True, text=True
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

    async def _ensure_worktree(self, spec: dict[str, Any]) -> None:
        async with self._worktree_lock:
            await self._ensure_worktree_locked(spec)

    async def _ensure_worktree_locked(self, spec: dict[str, Any]) -> None:
        repo = Path(spec["repo"])
        worktree = Path(spec["worktree_path"]).resolve()
        branch = spec["branch"]
        base = spec["base_commit"]
        await self._git(repo, "worktree", "prune", check=False)
        listing = await self._git_stdout(repo, "worktree", "list", "--porcelain") or ""
        if str(worktree) in listing:
            await self._recover_worktree_locked(spec)
            return
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

    async def _recover_worktree(self, spec: dict[str, Any], generation: int | None = None) -> None:
        async with self._worktree_lock:
            await self._recover_worktree_locked(spec, generation)

    async def _recover_worktree_locked(
        self, spec: dict[str, Any], generation: int | None = None
    ) -> None:
        """Worktree recovery before a respawn (arch §7.5): reset + clean."""
        repo = Path(spec["repo"])
        worktree = Path(spec["worktree_path"]).resolve()
        await self._git(repo, "worktree", "prune", check=False)
        if not worktree.exists():
            await self._ensure_worktree_locked(spec)
            return
        for op in ("rebase", "merge", "cherry-pick"):
            await self._git(worktree, op, "--abort", check=False)
        await self._git(worktree, "reset", "--hard", spec["base_commit"], check=False)
        await self._git(worktree, "clean", "-fd", check=False)
        await self.emit(
            "recover", task_id=spec["task_id"], generation=generation,
            base_commit=spec["base_commit"],
        )

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
        env = _strip_sensitive_env(dict(os.environ))
        env["PYTHONUNBUFFERED"] = "1"
        env["CAMBIUM_TASK_ID"] = spec["task_id"]
        env["CAMBIUM_GENERATION"] = str(generation)
        return env

    def _run_payload(
        self, spec: dict[str, Any], run_rid: str, wall_budget: float
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
            "max_turns": int(spec.get("max_turns", DEFAULT_MAX_TURNS)),
            "max_tokens": int(spec.get("max_tokens", DEFAULT_MAX_TOKENS)),
            "max_wall_s": wall_budget,
        }

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
        await self._ensure_worktree(spec)

        restarts = 0
        generation = 1
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
            await self._recover_worktree(spec, generation + 1)
            generation += 1

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
                env=_strip_sensitive_env(env),
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

        async def _read_stdout() -> None:
            nonlocal parse_errors
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
        }
        await self.emit("init", task_id=task_id, request_id=init_rid, generation=generation)
        await _write_json(proc, init_msg)

        phase = "ready"  # "ready" | "run"
        ready_deadline = loop.time() + ready_timeout
        last_heartbeat: float | None = None
        run_rid: str | None = None
        envelope: dict[str, Any] | None = None
        exit_reason: str | None = None
        correlated = False
        timeout_phase: str | None = None

        async def _cancel_and_kill() -> None:
            try:
                await _write_json(proc, {"type": "cancel", "reason": timeout_phase or "timeout"})
            except Exception:
                pass
            await _kill_worker(proc)

        try:
            while True:
                now = loop.time()
                if now >= wall_deadline:
                    timeout_phase = "wall"
                    await _cancel_and_kill()
                    break
                if phase == "ready" and now >= ready_deadline:
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
                    # EOF alone is never death (arch §5.3): 5s grace, then poll.
                    await self.emit(
                        "log", task_id=task_id, generation=generation,
                        message="stdout EOF; grace then poll",
                    )
                    await asyncio.sleep(EOF_GRACE_S)
                    if proc.returncode is None:
                        await self.emit(
                            "log", task_id=task_id, generation=generation,
                            message="process alive after EOF; killing process group",
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
                    payload = self._run_payload(spec, run_rid, wall_budget)
                    if not await _write_json(proc, {
                        "type": "run_task", "request_id": run_rid,
                        "task_id": task_id, **payload,
                    }):
                        await self.emit("protocol", task_id=task_id, note="run_task write failed")
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
        if clean:
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
            env=_strip_sensitive_env(dict(os.environ)),
        )
        try:
            out, err = await asyncio.wait_for(proc.communicate(), timeout)
            rc = proc.returncode if proc.returncode is not None else 1
        except TimeoutError:
            try:
                proc.kill()
            except ProcessLookupError:
                pass
            await proc.wait()
            rc = 124
            await self.emit("gate", task_id=task_id, exit_code=rc, tree=tree, timed_out=True)
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
        if self._merge_cls is None:
            raise RuntimeError("cambium.merge.MergeSequencer is unavailable")
        loop = asyncio.get_running_loop()

        def persist_terminal(kind: str, payload: dict[str, Any]) -> None:
            event_payload = dict(payload)
            event_task_id = event_payload.pop("task", task_id)
            future = asyncio.run_coroutine_threadsafe(
                self.emit(
                    kind, task_id=event_task_id, _observer_failure_is_fatal=False,
                    **event_payload,
                ),
                loop,
            )
            future.result()

        return self._merge_cls(
            task_id=task_id, session_dir=self._session_dir, durable_event=persist_terminal
        )

    async def _flush_sequencer_events(
        self, seq: Any, task_keys: dict[str, str] | None = None
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
            await self.emit(kind, task_id=task_id, **payload)
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
        seq = self._make_sequencer(task_id)
        ref_published = False
        committed_persisted = False
        cleanup_failed = False
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
                await self._flush_sequencer_events(seq)
                if hasattr(seq, "ensure_staging_clean"):
                    await asyncio.to_thread(seq.ensure_staging_clean, repo)
                    await self._flush_sequencer_events(seq)
                await asyncio.to_thread(seq.publish_merge, repo, staging_tip, current_main)
                ref_published = True
                await self.emit(
                    "merge_committed", task_id=task_id, old=current_main, new=staging_tip,
                    repo=str(repo), branch=branch, generation=handle.generation,
                )
                committed_persisted = True
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
                if hasattr(seq, "cleanup_staging") and not (
                    ref_published and not committed_persisted
                ):
                    await asyncio.to_thread(seq.cleanup_staging, repo)
            except Exception as exc:
                cleanup_failed = True
                emitted = await self._flush_sequencer_events(seq)
                if committed_persisted and "merge_staging_cleanup_failed" not in emitted:
                    await self.emit(
                        "merge_staging_cleanup_failed", task_id=task_id,
                        staging_sha=staging_tip, reason=exc.__class__.__name__,
                    )
            else:
                await self._flush_sequencer_events(seq)
        if cleanup_failed:
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
    spec = dict(task)
    task_id = spec.get("task_id")
    if not task_id:
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
    spec.setdefault("base_commit", None)
    spec.setdefault("write_marker", True)
    return spec


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
    specs = [_validate_plan_task(session_dir, t) for t in _plan_tasks(plan)]
    if not specs:
        raise ValueError("plan contains no tasks")

    store = _open_store(session_dir)
    runtime = _Runtime(session_dir, store, on_event=on_event)
    await runtime.start()
    cancelled = False
    try:
        await runtime.reconcile(specs)
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
    subprocess.run(args, cwd=cwd, check=True, capture_output=True)


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
        for task in _plan_tasks(plan):
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
