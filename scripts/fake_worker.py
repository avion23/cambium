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
'// replace-me' line instead of appending the marker).
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

MODE = os.environ.get("FAKE_MODE", "healthy")


def send(msg: dict) -> None:
    sys.stdout.write(json.dumps(msg) + "\n")
    sys.stdout.flush()


def read_msg() -> dict | None:
    while True:
        line = sys.stdin.readline()
        if not line:
            return None
        line = line.strip()
        if not line:
            continue
        return json.loads(line)


def git(*args: str, cwd: str | Path | None = None) -> tuple[int, str, str]:
    proc = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True)
    return proc.returncode, proc.stdout.strip(), proc.stderr.strip()


def do_work(run: dict) -> tuple[str, str | None, list[str], list[str], str]:
    """Create a throwaway worktree, append the marker, commit.

    Returns (status, failure_reason, commits, files_changed, diff).
    Refuses to touch paths outside the session scratch area.
    """
    scratch = Path(run["scratch_repo"]).resolve()
    worktree = Path(run["worktree_path"]).resolve()
    branch = run["branch"]
    target_file = run["target_file"]
    marker = run["marker"]
    write_marker = bool(run.get("write_marker", True))

    session_root = scratch.parent
    if not worktree.is_relative_to(session_root):
        return ("failed", f"worktree_path {worktree} outside session scratch root {session_root}",
                [], [], "")
    target = (worktree / target_file).resolve()
    if not target.is_relative_to(worktree):
        return ("failed", f"target_file {target_file!r} escapes the worktree", [], [], "")

    if worktree.exists():
        git("worktree", "remove", "--force", str(worktree), cwd=scratch)
    git("branch", "-D", branch, cwd=scratch)
    rc, _out, err = git("worktree", "add", "-b", branch, str(worktree), "main", cwd=scratch)
    if rc != 0:
        return ("failed", f"worktree add failed: {err}", [], [], "")
    if not write_marker:
        return ("failed", "marker not written (write_marker=false)", [], [], "")
    if not target.exists():
        return ("failed", f"target file missing: {target_file}", [], [], "")
    if MODE == "overwrite":
        # Replace the first '// replace-me' line so two concurrent workers
        # editing the same file are guaranteed a rebase conflict.
        text = target.read_text()
        if "// replace-me" in text:
            text = text.replace("// replace-me", marker, 1)
        else:
            text = text.rstrip("\n") + "\n" + marker + "\n"
        target.write_text(text)
    else:
        target.write_text(target.read_text().rstrip("\n") + "\n" + marker + "\n")
    if marker not in target.read_text():
        return ("failed", "edit missing: marker not present after write", [], [], "")
    git("add", target_file, cwd=worktree)
    rc, _out, err = git("commit", "-m", f"cambium-slice: {run['task_id']}", cwd=worktree)
    if rc != 0:
        return ("failed", f"commit failed: {err}", [], [], "")
    _rc, sha, _err = git("rev-parse", "HEAD", cwd=worktree)
    _rc, diff, _err = git("diff", "main..HEAD", cwd=worktree)
    return ("succeeded", None, [sha], [target_file], diff)


def main() -> int:
    init = read_msg()
    if init is None or init.get("type") != "init":
        return 1
    init_rid = init["request_id"]
    task_id = init["task_id"]

    if MODE == "noready":
        time.sleep(1e9)  # never send ready — the supervisor's ready_timeout kills us

    if MODE == "garbage":
        for _ in range(3):
            sys.stdout.write("not-json-" + ("x" * 60) + "\n")
        sys.stdout.flush()

    if MODE == "garbage_only":
        while True:
            sys.stdout.write("garbage line\n")
            sys.stdout.flush()
            time.sleep(0.01)

    send({"type": "ready", "request_id": init_rid, "task_id": task_id,
          "pid": os.getpid(), "generation": init.get("generation", 1), "proto": 1})

    run = read_msg()
    if run is None or run.get("type") != "run_task":
        send({"type": "exit_message", "task_id": task_id,
              "generation": init.get("generation", 1), "reason": "crash"})
        return 1
    run_rid = run["request_id"]

    status, failure_reason, commits, files_changed, diff = do_work(run)

    result_rid = run_rid if MODE != "badrid" else "00000000-deadbeef-rid"
    if MODE != "noresult":
        send({"type": "result_envelope", "request_id": result_rid, "task_id": task_id,
              "generation": init.get("generation", 1), "status": status,
              "commits": commits, "files_changed": files_changed, "diff": diff,
              "failure_reason": failure_reason})
    if MODE != "noexit":
        send({"type": "exit_message", "task_id": task_id,
              "generation": init.get("generation", 1), "reason": "done"})
    return 5 if MODE == "exit5" else 0


if __name__ == "__main__":
    sys.exit(main())
