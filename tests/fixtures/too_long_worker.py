#!/usr/bin/env python3
"""Worker that emits a stdout line above the supervisor read cap."""

from __future__ import annotations

import json
import os
import sys


def send(message: dict) -> None:
    sys.stdout.write(json.dumps(message) + "\n")
    sys.stdout.flush()


def main() -> int:
    init = json.loads(sys.stdin.readline())
    if init.get("type") != "init":
        return 1
    send({"type": "ready", "request_id": init["request_id"],
          "task_id": init["task_id"], "pid": os.getpid(),
          "generation": init.get("generation", 1), "proto": 1})
    sys.stdout.write("x" * (2 * 1024 * 1024) + "\n")
    sys.stdout.flush()
    return 0


if __name__ == "__main__":
    sys.exit(main())
