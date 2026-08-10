#!/usr/bin/env python3
"""Worker that sends a stale pong before closing stdout.

The stale response must not satisfy the supervisor's post-EOF ping probe.
"""

from __future__ import annotations

import json
import os
import select
import sys
import time
from pathlib import Path


def send(message: dict) -> None:
    sys.stdout.write(json.dumps(message) + "\n")
    sys.stdout.flush()


def main() -> int:
    init = json.loads(sys.stdin.readline())
    if init.get("type") != "init":
        return 1
    task_id = init["task_id"]
    generation = init.get("generation", 1)
    send({"type": "ready", "request_id": init["request_id"],
          "task_id": task_id, "pid": os.getpid(),
          "generation": generation, "proto": 1})
    if not sys.stdin.readline():
        return 1
    # This pong belongs to init, not to the ping the supervisor will issue
    # after EOF. It is deliberately stale.
    send({"type": "pong", "request_id": init["request_id"],
          "task_id": task_id, "generation": generation})
    os.close(1)
    deadline = time.monotonic() + 5.0
    while time.monotonic() < deadline:
        ready, _write, _error = select.select([sys.stdin], [], [], 0.1)
        if not ready:
            continue
        line = sys.stdin.readline()
        if not line:
            return 0
        message = json.loads(line)
        if message.get("type") == "ping":
            marker = Path(message.get("worktree", ".")) / ".cambium" / "ping_received"
            marker.parent.mkdir(parents=True, exist_ok=True)
            marker.write_text(json.dumps(message) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
