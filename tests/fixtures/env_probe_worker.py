#!/usr/bin/env python3
"""Worker fixture for strict worker/gate/git-hook environment tests."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

PROBE_KEYS = (
    "TEST_API_KEY_DEMO",
    "TEST_DB_PWD_DEMO",
    "TEST_DATABASE_URL_DEMO",
)
AUTHORIZED_KEY = "CAMBIUM_TEST_PROVIDER_KEY"
AUTHORIZED_VALUE = "authorized-provider-value"


def send(message: dict) -> None:
    sys.stdout.write(json.dumps(message) + "\n")
    sys.stdout.flush()


def read_message() -> dict | None:
    while True:
        line = sys.stdin.readline()
        if not line:
            return None
        if line.strip():
            return json.loads(line)


def git(*args: str, cwd: Path) -> tuple[int, str, str]:
    result = subprocess.run(
        ["git", *args], cwd=cwd, capture_output=True, text=True, check=False
    )
    return result.returncode, result.stdout.strip(), result.stderr.strip()


def main() -> int:
    init = read_message()
    if init is None or init.get("type") != "init":
        return 1
    task_id = init["task_id"]
    generation = init.get("generation", 1)
    send({
        "type": "ready",
        "request_id": init["request_id"],
        "task_id": task_id,
        "pid": os.getpid(),
        "generation": generation,
        "proto": 1,
    })

    run = read_message()
    if run is None or run.get("type") != "run_task":
        return 1
    worktree = Path(run["worktree_path"]).resolve()
    scratch = Path(run["scratch_repo"]).resolve()
    target_file = run["target_file"]
    marker = run.get("marker", "// cambium-probe")

    leaked = sorted(key for key in PROBE_KEYS if key in os.environ)
    authorized = os.environ.get(AUTHORIZED_KEY) == AUTHORIZED_VALUE
    if leaked:
        status = "failed"
        failure_reason = f"unrelated environment leaked: {leaked}"
    elif not authorized:
        status = "failed"
        failure_reason = f"authorized provider key missing: {AUTHORIZED_KEY}"
    else:
        if not worktree.exists():
            branch = run["branch"]
            git("branch", "-D", branch, cwd=scratch)
            rc, _out, err = git(
                "worktree", "add", "-b", branch, str(worktree),
                run.get("base_commit") or "main", cwd=scratch,
            )
            if rc != 0:
                status = "failed"
                failure_reason = f"worktree add failed: {err}"
            else:
                status = "pending"
                failure_reason = None
        else:
            status = "pending"
            failure_reason = None

        target = worktree / target_file
        if status == "pending" and not target.exists():
            status = "failed"
            failure_reason = f"target file missing: {target_file}"
        if status == "pending":
            target.write_text(target.read_text().rstrip("\n") + "\n" + marker + "\n")
            git("add", target_file, cwd=worktree)
            rc, _out, err = git("commit", "-m", f"env-probe: {task_id}", cwd=worktree)
            status = "succeeded" if rc == 0 else "failed"
            failure_reason = None if rc == 0 else f"commit failed: {err}"

    send({
        "type": "result_envelope",
        "request_id": run["request_id"],
        "task_id": task_id,
        "generation": generation,
        "status": status if status != "pending" else "failed",
        "failure_reason": failure_reason,
        "commits": [],
        "files_changed": [target_file],
    })
    send({"type": "exit_message", "task_id": task_id,
          "generation": generation, "reason": "done"})
    return 0


if __name__ == "__main__":
    sys.exit(main())
