#!/usr/bin/env python3
"""In-place worker fixture for the supervisor fan-out tests (T3 crash recovery).

Speaks the JSON-Lines protocol like ``scripts/fake_worker.py`` but edits the
worktree IN PLACE (no worktree remove/re-add). Generation 1 commits its edit
and then crashes (exit 3) without a result or exit message; later generations
succeed. This is the proof that worktree recovery (``git reset --hard
base_commit`` + ``git clean -fd``, architecture §7.5) runs between respawns:
if recovery is skipped, generation 1's committed edit survives and generation
2's edit doubles the marker in the merged result.
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


def git(*args: str, cwd: Path) -> tuple[int, str, str]:
    proc = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True)
    return proc.returncode, proc.stdout.strip(), proc.stderr.strip()


def main() -> int:
    init = read_msg()
    if init is None or init.get("type") != "init":
        return 1
    generation = int(init.get("generation", 1))
    init_rid = init["request_id"]
    task_id = init["task_id"]

    send(
        {
            "type": "ready",
            "request_id": init_rid,
            "task_id": task_id,
            "pid": os.getpid(),
            "generation": generation,
            "proto": 1,
        }
    )

    run = read_msg()
    if run is None or run.get("type") != "run_task":
        send(
            {
                "type": "exit_message",
                "task_id": task_id,
                "generation": generation,
                "reason": "crash",
            }
        )
        return 1

    worktree = Path(run["worktree_path"]).resolve()
    marker = run.get("marker", "// cambium-edit")
    file = worktree / (run.get("target_file") or "edit.txt")

    text = file.read_text() if file.exists() else ""
    file.write_text(text.rstrip("\n") + "\n" + marker + "\n")
    git("add", "-A", cwd=worktree)
    git("commit", "-m", f"crash-worker: {task_id} (gen {generation})", cwd=worktree)
    _rc, commit, _err = git("rev-parse", "HEAD", cwd=worktree)

    if generation == 1:
        return 3  # crash mid-edit: no result_envelope, no exit_message

    send(
        {
            "type": "result_envelope",
            "request_id": run["request_id"],
            "task_id": task_id,
            "generation": generation,
            "status": "succeeded",
            "commits": [commit],
            "files_changed": [file.name],
            "diff": "",
        }
    )
    send({"type": "exit_message", "task_id": task_id, "generation": generation, "reason": "done"})
    return 0


if __name__ == "__main__":
    sys.exit(main())
