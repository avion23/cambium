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
import json
import os
import signal
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

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
    """Next message, or None at EOF. Raises asyncio.TimeoutError when deadline passes."""
    remaining = deadline - asyncio.get_running_loop().time()
    if remaining <= 0:
        raise asyncio.TimeoutError
    return await asyncio.wait_for(messages.get(), remaining)


async def _run_gate(
    command: str, cwd: Path, log: EventLog, task_id: str, timeout: float
) -> int:
    """Run the gate command in the worker's worktree. Raises TimeoutError on gate timeout."""
    proc = await asyncio.create_subprocess_exec(
        "sh", "-c", command, cwd=cwd, stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        _out, err = await asyncio.wait_for(proc.communicate(), timeout)
    except asyncio.TimeoutError:
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
    )
    _out, err = await proc.communicate()
    if proc.returncode != 0:
        log.emit("merge", task_id=task_id, branch=branch, exit_code=proc.returncode,
                 stderr=err.decode("utf-8", "replace")[:512])
        return None
    tip = await asyncio.create_subprocess_exec(
        "git", "rev-parse", "HEAD", cwd=scratch_repo,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
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
        env={**os.environ, "PYTHONUNBUFFERED": "1",
             "CAMBIUM_TASK_ID": task_id, "CAMBIUM_GENERATION": "1"},
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
        except asyncio.TimeoutError:
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
            except asyncio.TimeoutError:
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
            except asyncio.TimeoutError:
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


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Cambium vertical-slice supervisor")
    parser.add_argument("--session-dir", required=True)
    parser.add_argument(
        "--task-spec",
        help="path to task spec JSON (default: <session-dir>/task.json, else built-in defaults)",
    )
    args = parser.parse_args(argv)
    session_dir = Path(args.session_dir)
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
