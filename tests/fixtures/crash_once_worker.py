#!/usr/bin/env python3
"""In-place worker that crashes once for any generation, then succeeds."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path


def send(message: dict) -> None:
    sys.stdout.write(json.dumps(message) + "\n")
    sys.stdout.flush()


def read_message() -> dict | None:
    line = sys.stdin.readline()
    return json.loads(line) if line else None


def git(*args: str, cwd: Path) -> tuple[int, str, str]:
    result = subprocess.run(
        ["git", *args], cwd=cwd, capture_output=True, text=True, check=False
    )
    return result.returncode, result.stdout.strip(), result.stderr.strip()


def main() -> int:
    init = read_message()
    if init is None or init.get("type") != "init":
        return 1
    generation = int(init["generation"])
    task_id = init["task_id"]
    send({"type": "ready", "request_id": init["request_id"],
          "task_id": task_id, "pid": os.getpid(),
          "generation": generation, "proto": 1})
    run = read_message()
    if run is None or run.get("type") != "run_task":
        return 1

    worktree = Path(run["worktree_path"]).resolve()
    target = worktree / run["target_file"]
    marker = run["marker"]
    target.write_text(target.read_text().rstrip("\n") + "\n" + marker + "\n")
    git("add", run["target_file"], cwd=worktree)
    git("commit", "-m", f"crash-once: {task_id}", cwd=worktree)

    sentinel = worktree.parent / ".cambium" / "crash-once"
    if not sentinel.exists():
        sentinel.parent.mkdir(parents=True, exist_ok=True)
        sentinel.write_text(str(generation), encoding="ascii")
        return 3

    send({"type": "result_envelope", "request_id": run["request_id"],
          "task_id": task_id, "generation": generation, "status": "succeeded",
          "commits": [], "files_changed": [run["target_file"]], "diff": ""})
    send({"type": "exit_message", "task_id": task_id,
          "generation": generation, "reason": "done"})
    return 0


if __name__ == "__main__":
    sys.exit(main())
