"""Minimal asyncio supervisor — the vertical-slice milestone.

End-to-end proof of the harness shape with ONE worker: spawn a worker
subprocess, speak JSON-Lines over stdio (``init`` -> ``ready`` ->
``run_task`` -> ``result_envelope`` -> ``exit_message``, request_id
correlated), append events to ``<session_dir>/.cambium/events.jsonl``,
run the task's gate command, and merge the worker's branch back with
``git merge --ff-only`` in the scratch repo. Exits 0 only when every
step succeeded.

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
    worker_status: str | None = None  # from the result_envelope
    gate_exit_code: int | None = None
    merge_sha: str | None = None


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


async def _write_json(proc: asyncio.subprocess.Process, msg: dict[str, Any]) -> None:
    proc.stdin.write((json.dumps(msg) + "\n").encode("utf-8"))
    await proc.stdin.drain()


async def _run_gate(command: str, cwd: Path, log: EventLog, task_id: str) -> int:
    proc = await asyncio.create_subprocess_exec(
        "sh", "-c", command, cwd=cwd, stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    _out, err = await proc.communicate()
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
    (shell command run in the worker's worktree), spec (optional).
    """
    session_dir = Path(session_dir)
    log = EventLog(session_dir / ".cambium" / "events.jsonl", on_event)
    task_id = task_spec["task_id"]
    worker_script = str(task_spec["worker"])

    log.emit("spawned", task_id=task_id, worker=worker_script)
    proc = await asyncio.create_subprocess_exec(
        sys.executable, "-u", worker_script,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        limit=WORKER_STDIN_LIMIT,
        env={**os.environ, "PYTHONUNBUFFERED": "1",
             "CAMBIUM_TASK_ID": task_id, "CAMBIUM_GENERATION": "1"},
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
        "scratch_repo": str(Path(task_spec["scratch_repo"]).resolve()),
        "worktree_path": str(Path(task_spec["worktree_path"]).resolve()),
        "branch": task_spec["branch"],
        "target_file": task_spec["target_file"],
        "marker": task_spec["marker"],
        "write_marker": bool(task_spec.get("write_marker", True)),
    }

    result_envelope: dict[str, Any] | None = None
    exit_reason: str | None = None
    saw_ready = False
    while True:
        msg = await messages.get()
        if msg is None:
            log.emit("eof_without_exit", task_id=task_id)
            break
        mtype = msg.get("type")
        if mtype == "ready":
            saw_ready = True
            log.emit("ready", task_id=task_id, request_id=msg.get("request_id"),
                     pid=msg.get("pid"))
            run_rid = make_request_id(2)
            await _write_json(proc, {"type": "run_task", "request_id": run_rid,
                                     "task_id": task_id, **run_payload})
            log.emit("run_task", task_id=task_id, request_id=run_rid)
        elif mtype == "result_envelope":
            result_envelope = msg
            log.emit("result", task_id=task_id, request_id=msg.get("request_id"),
                     status=msg.get("status"))
        elif mtype == "exit_message":
            exit_reason = msg.get("reason")
            log.emit("exit", task_id=task_id, request_id=msg.get("request_id"),
                     reason=exit_reason)
            break
        else:
            log.emit("protocol", task_id=task_id, type=mtype or "<missing>")

    worker_exit_code = await proc.wait()
    await asyncio.gather(stdout_task, stderr_task, return_exceptions=True)

    worker_status = result_envelope.get("status") if result_envelope else None
    gate_rc: int | None = None
    merge_sha: str | None = None

    if result_envelope is None:
        status = "failed"
    else:
        worktree = Path(run_payload["worktree_path"])
        if worktree.exists():
            gate_rc = await _run_gate(task_spec["gate"], worktree, log, task_id)
        if worker_status == "succeeded" and gate_rc == 0:
            status = "succeeded"
            merge_sha = await _merge_branch(
                Path(run_payload["scratch_repo"]), run_payload["branch"], log, task_id)
            if merge_sha is None:
                status = "failed"
        else:
            status = "failed"

    exit_code = 0 if status == "succeeded" else 1
    log.emit("session_ended", task_id=task_id, status=status, exit_code=exit_code,
             worker_exit_code=worker_exit_code, saw_ready=saw_ready,
             exit_reason=exit_reason)
    return SliceResult(status=status, exit_code=exit_code,
                       worker_exit_code=worker_exit_code,
                       worker_status=worker_status, gate_exit_code=gate_rc,
                       merge_sha=merge_sha)


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
    _bootstrap_scratch(Path(task_spec["scratch_repo"]), task_spec)

    def print_event(record: dict[str, Any]) -> None:
        print(f'{record["kind"]:>14}  {json.dumps(record["payload"])}', flush=True)

    result = asyncio.run(run_session(session_dir, task_spec, on_event=print_event))
    print(
        f"result: status={result.status} exit_code={result.exit_code} "
        f"worker_exit={result.worker_exit_code} worker_status={result.worker_status} "
        f"gate_exit={result.gate_exit_code} merge={result.merge_sha}",
        flush=True,
    )
    return result.exit_code


if __name__ == "__main__":
    sys.exit(main())
