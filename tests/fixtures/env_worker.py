#!/usr/bin/env python3
"""Worker that dumps its own environment, then does the fake-worker work.

Regression fixture: proves which environment variables reach the actually
spawned worker subprocess.  The dump is written before any protocol work so a
missing credential variable cannot be hidden by a later failure.  ``ENV_DUMP_PATH``
is a JSON file; the environment is written as a JSON object.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))

from fake_worker import do_work, read_msg, send  # noqa: E402


def main() -> int:
    init = read_msg()
    if init is None or init.get("type") != "init":
        return 1
    dump = Path(os.environ["ENV_DUMP_PATH"])
    dump.parent.mkdir(parents=True, exist_ok=True)
    dump.write_text(json.dumps(dict(os.environ)))

    init_rid = init["request_id"]
    task_id = init["task_id"]
    send({"type": "ready", "request_id": init_rid, "task_id": task_id,
          "pid": os.getpid(), "generation": init.get("generation", 1), "proto": 1})

    run = read_msg()
    if run is None or run.get("type") != "run_task":
        send({"type": "exit_message", "task_id": task_id,
              "generation": init.get("generation", 1), "reason": "crash"})
        return 1
    run_rid = run["request_id"]
    status, failure_reason, commits, files_changed, diff = do_work(run)
    send({"type": "result_envelope", "request_id": run_rid, "task_id": task_id,
          "generation": init.get("generation", 1), "status": status,
          "commits": commits, "files_changed": files_changed, "diff": diff,
          "failure_reason": failure_reason})
    send({"type": "exit_message", "task_id": task_id,
          "generation": init.get("generation", 1), "reason": "done"})
    return 0


if __name__ == "__main__":
    sys.exit(main())
