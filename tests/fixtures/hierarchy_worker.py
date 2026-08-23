#!/usr/bin/env python3
"""Hierarchy test fixture worker.

Mirrors ``scripts/fake_worker.py``'s healthy marker path but adds three
opt-in observability hooks selected by environment variables so the static
ready-node wave tests can assert on dispatch order, concurrency, and the
bounded child context:

- ``TRACE_FILE``: if set, append ``ENTER <task_id>`` before the work and
  ``EXIT <task_id> <status>`` after it. Tests parse line order to assert
  exact ready waves and width enforcement.
- ``PAYLOAD_DIR``: if set, write the full ``run_task`` payload received by
  this worker to ``<PAYLOAD_DIR>/<task_id>.json``. Tests assert the child
  sees its own spec plus exactly the strict parent envelope key set.
- ``WORKER_DELAY_S``: float seconds to sleep between ``ENTER`` and the edit,
  so width enforcement produces observable concurrency overlap.

A task whose ``marker`` starts with ``// FAIL:`` reports ``status=failed``
without doing the marker edit. The trigger rides on the existing payload
field (not an env var) so the session redactor cannot collide with a
task_id. The marker protocol (``target_file`` / ``marker`` /
``write_marker``) is otherwise unchanged: ``fake_worker.do_work`` does the
edit, commit, and diff.
"""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))

from fake_worker import (  # type: ignore[import-not-found]  # pyright: ignore[reportMissingImports]  # noqa: E402
    do_work,
    read_msg,
    send,
)


def _trace(line: str) -> None:
    path = os.environ.get("TRACE_FILE")
    if not path:
        return
    with open(path, "a", encoding="utf-8") as handle:
        handle.write(line + "\n")


def _dump_payload(run: dict, task_id: str) -> None:
    directory = os.environ.get("PAYLOAD_DIR")
    if not directory:
        return
    target = Path(directory) / f"{task_id}.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(run, ensure_ascii=False), encoding="utf-8")


def _delay() -> None:
    value = os.environ.get("WORKER_DELAY_S")
    if not value:
        return
    try:
        time.sleep(float(value))
    except ValueError:
        pass


def _is_fail_marker(marker: str) -> bool:
    return isinstance(marker, str) and marker.startswith("// FAIL:")


def main() -> int:
    init = read_msg()
    if init is None or init.get("type") != "init":
        return 1
    init_rid = init["request_id"]
    task_id = init["task_id"]
    generation = init.get("generation", 1)
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
    run_rid = run["request_id"]
    _dump_payload(run, task_id)
    _trace(f"ENTER {task_id}")

    if _is_fail_marker(run.get("marker")):
        _trace(f"EXIT {task_id} failed")
        send(
            {
                "type": "result_envelope",
                "request_id": run_rid,
                "task_id": task_id,
                "generation": generation,
                "status": "failed",
                "commits": [],
                "files_changed": [],
                "diff": "",
                "failure_reason": "injected_hierarchy_failure",
            }
        )
        send(
            {"type": "exit_message", "task_id": task_id, "generation": generation, "reason": "done"}
        )
        return 0

    _delay()
    status, failure_reason, commits, files_changed, diff = do_work(run)
    _trace(f"EXIT {task_id} {status}")
    send(
        {
            "type": "result_envelope",
            "request_id": run_rid,
            "task_id": task_id,
            "generation": generation,
            "status": status,
            "commits": commits,
            "files_changed": files_changed,
            "diff": diff,
            "failure_reason": failure_reason,
        }
    )
    send({"type": "exit_message", "task_id": task_id, "generation": generation, "reason": "done"})
    return 0


if __name__ == "__main__":
    sys.exit(main())
