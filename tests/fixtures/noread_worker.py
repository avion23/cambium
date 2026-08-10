#!/usr/bin/env python3
"""Ready worker that stops reading stdin to exercise the drain deadline."""

from __future__ import annotations

import json
import os
import sys
import time


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
    time.sleep(1e9)
    return 0


if __name__ == "__main__":
    sys.exit(main())
