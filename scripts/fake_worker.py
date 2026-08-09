#!/usr/bin/env python3
"""Fake worker for the vertical-slice milestone.

Speaks the JSON-Lines wire protocol over stdio: reads ``init``, answers
``ready``; reads ``run_task``, does the work in a throwaway git worktree
of the scratch repo (append a marker line to the target file, commit),
and emits ``result_envelope`` then ``exit_message``. Exits 0.

Gate-failure path: when ``write_marker`` is false (or the edit did not
land) the worker reports ``status="failed"`` in the result_envelope.
The supervisor's gate command is the authoritative check.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path


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


def main() -> int:
    init = read_msg()
    if init is None or init.get("type") != "init":
        return 1
    init_rid = init["request_id"]
    task_id = init["task_id"]

    send({"type": "ready", "request_id": init_rid, "task_id": task_id,
          "pid": os.getpid(), "generation": init.get("generation", 1), "proto": 1})

    run = read_msg()
    if run is None or run.get("type") != "run_task":
        send({"type": "exit_message", "request_id": init_rid, "task_id": task_id,
              "generation": init.get("generation", 1), "reason": "crash"})
        return 1

    scratch = Path(run["scratch_repo"])
    worktree = Path(run["worktree_path"])
    branch = run["branch"]
    target_file = run["target_file"]
    marker = run["marker"]
    write_marker = bool(run.get("write_marker", True))

    status = "succeeded"
    failure_reason: str | None = None
    commits: list[str] = []
    files_changed: list[str] = []
    diff = ""

    if worktree.exists():
        git("worktree", "remove", "--force", str(worktree), cwd=scratch)
    git("branch", "-D", branch, cwd=scratch)
    rc, _out, err = git("worktree", "add", "-b", branch, str(worktree), "main", cwd=scratch)
    if rc != 0:
        status, failure_reason = "failed", f"worktree add failed: {err}"
    else:
        target = worktree / target_file
        if not write_marker:
            status, failure_reason = "failed", "marker not written (write_marker=false)"
        elif not target.exists():
            status, failure_reason = "failed", f"target file missing: {target_file}"
        else:
            target.write_text(target.read_text().rstrip("\n") + "\n" + marker + "\n")
            if marker not in target.read_text():
                status, failure_reason = "failed", "edit missing: marker not present after write"
            else:
                git("add", target_file, cwd=worktree)
                rc, _out, err = git("commit", "-m", f"cambium-slice: {task_id}", cwd=worktree)
                if rc != 0:
                    status, failure_reason = "failed", f"commit failed: {err}"
                else:
                    _rc, sha, _err = git("rev-parse", "HEAD", cwd=worktree)
                    commits = [sha]
                    files_changed = [target_file]
                    _rc, diff, _err = git("diff", "main..HEAD", cwd=worktree)

    send({"type": "result_envelope", "request_id": init_rid, "task_id": task_id,
          "generation": init.get("generation", 1), "status": status,
          "commits": commits, "files_changed": files_changed, "diff": diff,
          "failure_reason": failure_reason})
    send({"type": "exit_message", "request_id": init_rid, "task_id": task_id,
          "generation": init.get("generation", 1), "reason": "done"})
    return 0


if __name__ == "__main__":
    sys.exit(main())
