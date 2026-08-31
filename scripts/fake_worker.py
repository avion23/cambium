#!/usr/bin/env python3
"""Fake worker for the vertical-slice milestone.

Speaks the JSON-Lines wire protocol over stdio: reads ``init``, answers
``ready`` (echoes the init request_id); reads ``run_task``, does the work
in a throwaway git worktree of the scratch repo (append a marker line to
the target file, commit), and emits ``result_envelope`` (echoes the
run_task request_id) then ``exit_message`` (connection-level; no
request_id, arch §5.2). Exits 0.

Gate-failure path: when ``write_marker`` is false (or the edit did not
land) the worker reports ``status="failed"`` in the result_envelope.
The supervisor's gate command is the authoritative check.

Behavior variants for scenario tests, selected by env ``FAKE_MODE``:
healthy (default), exit5, noexit, noresult, badrid, noready,
garbage (garbage lines interleaved with a healthy protocol run),
garbage_only (pure garbage; never ready), overwrite (replace the first
'// replace-me' line instead of appending the marker), early_crash (exit
before ready after writing one stderr reason),
valid_non_object (valid JSON lines that are not objects, interleaved
with a healthy protocol run).
"""

from __future__ import annotations

import json
import math
import os
import select
import subprocess
import sys
import threading
import time
from pathlib import Path

MODE = os.environ.get("FAKE_MODE", "healthy")
MAX_DIFF_BYTES = 64 * 1024
_TIMEOUT = object()


def send(msg: dict) -> None:
    sys.stdout.write(json.dumps(msg) + "\n")
    sys.stdout.flush()


def read_msg() -> object | None:
    while True:
        line = sys.stdin.readline()
        if not line:
            return None
        line = line.strip()
        if not line:
            continue
        try:
            return json.loads(line)
        except json.JSONDecodeError:
            return None  # malformed line: treat as closed input


def git(*args: str, cwd: str | Path | None = None) -> tuple[int, str, str]:
    proc = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True)
    return proc.returncode, proc.stdout.strip(), proc.stderr.strip()


def _read_generation(worktree: Path) -> int:
    try:
        generation = int((worktree / ".cambium" / "generation").read_text(encoding="ascii"))
    except (OSError, ValueError):
        return 0
    return generation if generation > 0 else 0


def _write_generation(worktree: Path, generation: int) -> None:
    fence_dir = worktree / ".cambium"
    fence_dir.mkdir(parents=True, exist_ok=True)
    (fence_dir / "generation").write_text(f"{generation}\n", encoding="ascii")


def _generation_error(worktree: Path, generation: int) -> str | None:
    if _read_generation(worktree) == generation:
        return None
    return (
        f"generation mismatch for {worktree}: worker={generation}, "
        "persisted generation is different or missing"
    )


def _cap_diff(diff: str) -> str:
    raw = diff.encode("utf-8")
    if len(raw) <= MAX_DIFF_BYTES:
        return diff
    return raw[:MAX_DIFF_BYTES].decode("utf-8", errors="ignore") + "\n... [diff truncated]"


def _prepare_worktree(
    run: dict, scratch: Path, worktree: Path, branch: object, generation: int
) -> str | None:
    if not worktree.exists():
        if not isinstance(branch, str) or not branch:
            return "worker branch is missing"
        git("branch", "-D", branch, cwd=scratch)
        base_ref = run.get("base_commit") or "main"
        rc, _out, err = git("worktree", "add", "-b", branch, str(worktree), base_ref, cwd=scratch)
        if rc != 0:
            return f"worktree add failed: {err}"
        _write_generation(worktree, generation)
    elif _read_generation(worktree) == 0:
        _write_generation(worktree, generation)
    return _generation_error(worktree, generation)


def _delay_or_cancel(run: dict, stop: threading.Event | None) -> str | None:
    try:
        delay = max(0.0, float(run.get("work_delay_s", 0.0) or 0.0))
    except (TypeError, ValueError):
        return "work_delay_s must be a number"
    deadline = time.monotonic() + delay
    while time.monotonic() < deadline:
        if stop is not None and stop.is_set():
            return "cancelled"
        time.sleep(min(0.05, deadline - time.monotonic()))
    return "cancelled" if stop is not None and stop.is_set() else None


def _cancelled(stop: threading.Event | None) -> bool:
    return stop is not None and stop.is_set()


def _write_marker(target: Path, marker: str) -> None:
    if MODE == "overwrite":
        # Replace the first '// replace-me' line so two concurrent workers
        # editing the same file are guaranteed a rebase conflict.
        text = target.read_text()
        if "// replace-me" in text:
            text = text.replace("// replace-me", marker, 1)
        else:
            text = text.rstrip("\n") + "\n" + marker + "\n"
        target.write_text(text)
        return
    target.write_text(target.read_text().rstrip("\n") + "\n" + marker + "\n")


def _commit_marker(
    run: dict, worktree: Path, target_file: str, generation: int
) -> tuple[str, str | None, list[str], list[str], str]:
    rc, _out, err = git("add", target_file, cwd=worktree)
    if rc != 0:
        return ("failed", f"git add failed: {err}", [], [], "")
    if (fence_error := _generation_error(worktree, generation)) is not None:
        return ("failed", fence_error, [], [], "")
    rc, _out, err = git("commit", "-m", f"cambium-slice: {run['task_id']}", cwd=worktree)
    if rc != 0:
        return ("failed", f"commit failed: {err}", [], [], "")
    _rc, sha, _err = git("rev-parse", "HEAD", cwd=worktree)
    base_ref = run.get("base_commit") or "main"
    _rc, diff, _err = git("diff", f"{base_ref}..HEAD", cwd=worktree)
    return ("succeeded", None, [sha], [target_file], _cap_diff(diff))


def do_work(
    run: dict, stop: threading.Event | None = None
) -> tuple[str, str | None, list[str], list[str], str]:
    """Create a throwaway worktree, append the marker, commit.

    Returns (status, failure_reason, commits, files_changed, diff).
    Refuses to touch paths outside the session scratch area.
    """
    target_file = run.get("target_file")
    marker = run.get("marker")
    if (
        not isinstance(target_file, str)
        or not target_file
        or not isinstance(marker, str)
        or not marker
    ):
        return ("failed", "marker task requires target_file and marker", [], [], "")
    scratch = Path(run["scratch_repo"]).resolve()
    worktree = Path(run["worktree_path"]).resolve()
    branch = run.get("branch")
    generation = run.get("generation", 1)
    if isinstance(generation, bool) or not isinstance(generation, int) or generation <= 0:
        return ("failed", "invalid worker generation", [], [], "")
    write_marker = bool(run.get("write_marker", True))

    session_root = scratch.parent
    if not worktree.is_relative_to(session_root):
        return (
            "failed",
            f"worktree_path {worktree} outside session scratch root {session_root}",
            [],
            [],
            "",
        )
    target = (worktree / target_file).resolve()
    if not target.is_relative_to(worktree):
        return ("failed", f"target_file {target_file!r} escapes the worktree", [], [], "")

    if (
        worktree_error := _prepare_worktree(run, scratch, worktree, branch, generation)
    ) is not None:
        return ("failed", worktree_error, [], [], "")

    if _cancelled(stop):
        return ("cancelled", None, [], [], "")

    if not write_marker:
        return ("failed", "marker not written (write_marker=false)", [], [], "")
    if not target.exists():
        return ("failed", f"target file missing: {target_file}", [], [], "")

    delay_error = _delay_or_cancel(run, stop)
    if delay_error == "cancelled":
        return ("cancelled", None, [], [], "")
    if delay_error is not None:
        return ("failed", delay_error, [], [], "")
    if (fence_error := _generation_error(worktree, generation)) is not None:
        return ("failed", fence_error, [], [], "")

    _write_marker(target, marker)
    if marker not in target.read_text():
        return ("failed", "edit missing: marker not present after write", [], [], "")
    if (fence_error := _generation_error(worktree, generation)) is not None:
        return ("failed", fence_error, [], [], "")
    if _cancelled(stop):
        return ("cancelled", None, [], [], "")
    return _commit_marker(run, worktree, target_file, generation)


def _positive_float(value: object, default: float) -> float:
    try:
        converted = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return default
    return converted


def _env_float(name: str, default: float) -> float:
    try:
        value = float(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default
    return max(0.0, value)


def _read_with_timeout(timeout: float) -> object:
    readable, _writable, _exceptional = select.select([sys.stdin], [], [], timeout)
    return read_msg() if readable else _TIMEOUT


def _fatal(
    message: str, msg: object = None, *, task_id: object = None, generation: object = None
) -> int:
    context = msg if isinstance(msg, dict) else {}
    send(
        {
            "type": "fatal_error",
            "request_id": context.get("request_id"),
            "task_id": context.get("task_id", task_id),
            "generation": context.get("generation", generation),
            "error_type": "invalid_message",
            "message": message[:500],
            "recoverable": False,
        }
    )
    send(
        {
            "type": "exit_message",
            "task_id": context.get("task_id", task_id),
            "generation": context.get("generation", generation),
            "reason": "fatal",
        }
    )
    return 1


def _run_task(
    run: dict, task_id: str, generation: int, heartbeat_interval: float
) -> tuple[str, str | None, list[str], list[str], str, bool, bool]:
    stop = threading.Event()
    result: list[tuple[str, str | None, list[str], list[str], str]] = []

    def work() -> None:
        result.append(do_work(run, stop))

    thread = threading.Thread(target=work, daemon=True)
    thread.start()
    send(
        {
            "type": "heartbeat",
            "task_id": task_id,
            "generation": generation,
            "turn": 0,
            "tool": None,
            "status": "working",
        }
    )
    next_heartbeat = time.monotonic() + heartbeat_interval
    shutdown = False
    while thread.is_alive():
        timeout = max(0.0, min(0.05, next_heartbeat - time.monotonic()))
        if select.select([sys.stdin], [], [], timeout)[0]:
            try:
                message = read_msg()
            except (TypeError, ValueError, json.JSONDecodeError) as exc:
                stop.set()
                thread.join()
                return ("failed", f"invalid message: {exc}", [], [], "", False, True)
            if message is None:
                stop.set()
                break
            if not isinstance(message, dict):
                stop.set()
                thread.join()
                return ("failed", "expected an object message", [], [], "", False, True)
            message_type = message.get("type")
            if message_type == "cancel":
                send(
                    {
                        "type": "ok",
                        "request_id": message.get("request_id"),
                        "task_id": task_id,
                        "generation": generation,
                        "monotonic_ms": time.time_ns() // 1_000_000,
                    }
                )
                stop.set()
            elif message_type == "steer":
                payload = message.get("payload")
                if isinstance(payload, dict) and payload.get("action") == "cancel":
                    stop.set()
            elif message_type == "check_health":
                send(
                    {
                        "type": "ok",
                        "request_id": message.get("request_id"),
                        "task_id": task_id,
                        "generation": generation,
                    }
                )
            elif message_type == "ping":
                send(
                    {
                        "type": "pong",
                        "request_id": message.get("request_id"),
                        "task_id": task_id,
                        "generation": generation,
                        "monotonic_ms": time.time_ns() // 1_000_000,
                    }
                )
            elif message_type == "shutdown":
                send(
                    {
                        "type": "ok",
                        "request_id": message.get("request_id"),
                        "task_id": task_id,
                        "generation": generation,
                    }
                )
                stop.set()
                shutdown = True
                next_heartbeat = math.inf
        if time.monotonic() >= next_heartbeat and thread.is_alive():
            send(
                {
                    "type": "heartbeat",
                    "task_id": task_id,
                    "generation": generation,
                    "turn": 0,
                    "tool": None,
                    "status": "working",
                }
            )
            next_heartbeat = time.monotonic() + heartbeat_interval
    thread.join()
    if not result:
        return ("cancelled", None, [], [], "", shutdown, False)
    status, failure_reason, commits, files_changed, diff = result[0]
    return status, failure_reason, commits, files_changed, diff, shutdown, False


def _bootstrap_mode() -> int | None:
    if MODE == "noready":
        time.sleep(1e9)  # never send ready — the supervisor's ready_timeout kills us
    if MODE == "early_crash":
        print("worker bootstrap failed: provider setup exploded", file=sys.stderr, flush=True)
        return 7
    if MODE == "garbage":
        for _ in range(3):
            sys.stdout.write("not-json-" + ("x" * 60) + "\n")
        sys.stdout.flush()
    if MODE == "valid_non_object":
        for _ in range(3):
            sys.stdout.write(json.dumps([1, 2, 3]) + "\n")
            sys.stdout.write(json.dumps("hello") + "\n")
            sys.stdout.write(json.dumps(42) + "\n")
        sys.stdout.flush()
    if MODE == "garbage_only":
        while True:
            sys.stdout.write("garbage line\n")
            sys.stdout.flush()
            time.sleep(0.01)
    return None


def _handle_ready_message(message: object, task_id: str, generation: int) -> str:
    if not isinstance(message, dict):
        return "invalid"
    message_type = message.get("type")
    if message_type == "ping":
        send(
            {
                "type": "pong",
                "request_id": message.get("request_id"),
                "task_id": task_id,
                "generation": generation,
                "monotonic_ms": time.time_ns() // 1_000_000,
            }
        )
        return "continue"
    if message_type == "check_health":
        send(
            {
                "type": "ok",
                "request_id": message.get("request_id"),
                "task_id": task_id,
                "generation": generation,
            }
        )
        return "continue"
    if message_type == "shutdown":
        send(
            {
                "type": "ok",
                "request_id": message.get("request_id"),
                "task_id": task_id,
                "generation": generation,
            }
        )
        send(
            {
                "type": "exit_message",
                "task_id": task_id,
                "generation": generation,
                "reason": "shutdown",
            }
        )
        return "shutdown"
    return "run" if message_type == "run_task" else "invalid"


def _emit_task_result(
    run: dict,
    task_id: str,
    generation: int,
    status: str,
    failure_reason: str | None,
    commits: list[str],
    files_changed: list[str],
    diff: str,
) -> None:
    run_rid = run["request_id"]
    proposals = run.get("proposed_children")
    if isinstance(proposals, list):
        for index, proposal in enumerate(proposals):
            if isinstance(proposal, dict):
                send(
                    {
                        "type": "propose_child",
                        "request_id": f"{run_rid}-child-{index}",
                        "parent_task_id": task_id,
                        "child_task_id": proposal.get("child_task_id"),
                        "kind": proposal.get("kind"),
                        "spec": proposal.get("spec"),
                    }
                )
    if MODE == "noresult":
        return
    send(
        {
            "type": "result_envelope",
            "request_id": run_rid if MODE != "badrid" else "00000000-deadbeef-rid",
            "task_id": task_id,
            "generation": generation,
            "status": status,
            "exit_code": {"succeeded": 0, "failed": 1, "cancelled": 4}.get(status, 1),
            "commits": commits,
            "files_changed": files_changed,
            "diff": diff,
            "diff_truncated": diff.endswith("[diff truncated]"),
            "summary": f"appended marker to {files_changed[0]}" if files_changed else "",
            "failure_reason": failure_reason,
        }
    )


def _finish_task(
    init: dict,
    task_id: str,
    generation: int,
    status: str,
    shutdown: bool,
    files_changed: list[str],
) -> int | None:
    if MODE == "noexit":
        return 0
    if shutdown or not init.get("worker_reuse"):
        reason = (
            "shutdown"
            if shutdown
            else "cancelled"
            if status == "cancelled"
            else "failed"
            if status == "failed"
            else "done"
        )
        send(
            {
                "type": "exit_message",
                "task_id": task_id,
                "generation": generation,
                "reason": reason,
            }
        )
        return 5 if MODE == "exit5" else 0
    send(
        {
            "type": "reuse_ready",
            "task_id": task_id,
            "generation": generation,
            "pid": os.getpid(),
        }
    )
    return None


def _init_problem(init: object) -> str | None:
    if init is _TIMEOUT:
        return "init timeout: no init message within deadline"
    if init is None:
        return "closed"
    if not isinstance(init, dict) or init.get("type") != "init":
        return "expected init as the first message"
    generation = init.get("generation", 1)
    if isinstance(generation, bool) or not isinstance(generation, int) or generation <= 0:
        return "init generation must be a positive integer"
    return None


def _read_run_or_control(
    idle_timeout: float,
) -> tuple[str, dict | None, str]:
    """Return (control, validated dict when control is "run"/"init", detail)."""
    try:
        message = _read_with_timeout(idle_timeout)
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        return "invalid", None, f"invalid message: {exc}"
    if message is _TIMEOUT:
        return "idle", None, "idle"
    if message is None:
        return "closed", None, "closed"
    if isinstance(message, dict):
        message_type = message.get("type")
        if message_type == "shutdown":
            return "shutdown", None, "shutdown"
        if message_type == "init":
            return "init", message, "rebind"
        if message_type == "run_task":
            return "run", message, "run"
    return "invalid", None, f"invalid message: {message!r}"


def _exit_message(task_id: str, generation: int, reason: str) -> None:
    send({"type": "exit_message", "task_id": task_id, "generation": generation, "reason": reason})


def _ready(init: dict, task_id: str, generation: int) -> None:
    send(
        {
            "type": "ready",
            "request_id": init["request_id"],
            "task_id": task_id,
            "pid": os.getpid(),
            "generation": generation,
            "proto": 1,
        }
    )


def _run_one_task(
    init: dict, idle_timeout: float, task_id: str, generation: int
) -> tuple[dict | None, int | None]:
    """Execute one task cycle. Returns (rebind-init, exit-code); exactly one is set."""
    control, run, detail = _read_run_or_control(idle_timeout)
    if control == "idle":
        _exit_message(task_id, generation, "idle")
        return None, 0
    if control == "closed":
        return None, 0 if init.get("worker_reuse") else 1
    if control == "shutdown":
        return None, 0
    if control != "run" or run is None:
        return None, _fatal(
            f"invalid message while awaiting run_task: {detail}",
            None,
            task_id=task_id,
            generation=generation,
        )
    run = {**run, "generation": run.get("generation", generation), "task_id": task_id}
    heartbeat = init.get("heartbeat")
    heartbeat_interval = (
        _env_float("CAMBIUM_HEARTBEAT_INTERVAL_S", 1.0)
        if not isinstance(heartbeat, dict)
        else _positive_float(heartbeat.get("interval_s", 1.0), 1.0)
    )
    status, failure_reason, commits, files_changed, diff, shutdown, fatal = _run_task(
        run, task_id, generation, heartbeat_interval
    )
    if fatal:
        return None, _fatal(
            failure_reason or "task input failed", task_id=task_id, generation=generation
        )

    _emit_task_result(
        run, task_id, generation, status, failure_reason, commits, files_changed, diff
    )
    if (
        finish_exit := _finish_task(init, task_id, generation, status, shutdown, files_changed)
    ) is not None:
        return None, finish_exit

    control, rebind, detail = _read_run_or_control(idle_timeout)
    if control == "idle":
        _exit_message(task_id, generation, "idle")
        return None, 0
    if control in ("closed", "shutdown"):
        return None, 0
    if control != "init" or rebind is None:
        return None, _fatal(
            f"expected init when rebinding worker: {detail}",
            None,
            task_id=task_id,
            generation=generation,
        )
    return rebind, None


def main() -> int:
    init = _read_with_timeout(_env_float("CAMBIUM_INIT_TIMEOUT_S", 30.0))
    if (problem := _init_problem(init)) is not None:
        if problem == "closed":
            return 1
        return _fatal(problem, init)
    if not isinstance(init, dict):  # defensive: _init_problem only passes dicts here
        return 1
    if (bootstrap_exit := _bootstrap_mode()) is not None:
        return bootstrap_exit

    idle_timeout = _env_float("CAMBIUM_IDLE_TIMEOUT_S", 300.0)
    ready_sent = False
    while True:
        task_id = init["task_id"]
        generation = init.get("generation", 1)
        if not ready_sent:
            _ready(init, task_id, generation)
            ready_sent = True
        rebind, code = _run_one_task(init, idle_timeout, task_id, generation)
        if code is not None:
            return code
        if rebind is None:  # unreachable: every non-exit path returns a rebind init
            return 1
        init = rebind
        ready_sent = False


if __name__ == "__main__":
    sys.exit(main())
