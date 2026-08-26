#!/usr/bin/env python3
"""Measure Cambium worker spawn/cold-start cost vs reuse (adopt/defer/reject evidence).

This is the implementation-plan.md §5 follow-on "measure worker reuse" experiment.
It does NOT change runtime behavior: supervisor.py and worker.py are untouched.

What it measures
----------------
Runs N sequential one-task plans against one throwaway clone of this repository
(default ``--tasks 3``). Each plan is a fresh supervisor session driving the
deterministic marker worker (``cambium.worker`` in marker mode — no provider
credentials, no network). The first run is cold (interpreter/imports/disk caches
unwarm); later runs are warm.

For every run the script reads the canonical session event log
(``<session>/.cambium/events.db``) and derives the per-phase monotonic deltas of
the durable event chain (arch §6.3):

    task_assigned -> spawned -> init -> ready -> run_task -> result -> exit
                                                        merge_started -> merge_committed
                                                        -> worktree_pruned -> session_ended

Reported deltas (milliseconds):

    worktree_ms        task_assigned -> spawned   (worktree add + branch ops)
    spawn_to_init_ms   spawned        -> init      (subprocess creation + init write)
    init_to_ready_ms   init           -> ready     (worker cold start: import + ready)
    spawn_to_ready_ms  spawned        -> ready     (the spawn-to-ready cold-start budget)
    work_ms            run_task       -> result    (marker write + commit; the "work")
    merge_ms           merge_started  -> merge_committed (atomic ref publish)
    setup_ms           task_assigned  -> ready     (everything before the worker works)
    total_task_ms      task_assigned  -> session_ended (full supervised task wall)

It then prints a per-run table, a cold-vs-warm summary, the setup fraction of
plan wall time, and a projection of what a persistent-worker pool would save for
(a) marker tasks and (b) provider-loop tasks (the latter projected from the
dspy import figure recorded in docs/research/worker-coldstart.md, since a
provider run needs credentials and is out of scope here).

Self-contained: no provider calls, no credentials, no network. Uses the public
supervisor API (``python -m cambium.supervisor``) with a deterministic marker
worker.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import time
from pathlib import Path
from statistics import median

REAL = Path(__file__).resolve().parents[1]
DEFAULT_PYTHON = "python3.14"
# The marker worker needs a tracked target file in the clone.
FIXTURE = "tests/fixtures/e2e/cambium-e2e-marker.txt"

# Persistent-pool IPC round-trip floor (ms). From docs/research/worker-coldstart.md:
# warm-fork from a pre-imported parent is ~1.8 ms (cambium) / ~5.6 ms (cambium+dspy).
# A reused worker pays one Nuntius IPC round-trip per task instead of cold start;
# 5 ms is the conservative reuse floor used for the savings projection.
REUSE_IPC_FLOOR_MS = 5.0

# docs/research/worker-coldstart.md measured the per-worker dspy import cost.
# Projected here for the provider-loop savings estimate because this script
# cannot run a credentialed provider task (no network / credentials).
DSPY_SPAWN_TO_READY_MS = 2221.2

# The ordered per-task phases read from events.db.
PHASE_ORDER = (
    "task_assigned",
    "spawned",
    "init",
    "ready",
    "run_task",
    "result",
    "exit",
    "merge_started",
    "merge_committed",
    "worktree_pruned",
    "session_ended",
)


def _fail(message: str) -> None:
    print(f"measure_worker_coldstart: FAIL: {message}", file=sys.stderr)
    raise SystemExit(1)


def _seed_clone(source: Path, clone: Path) -> None:
    """Clone the source repo into `clone` and seed the git identity the
    worker's fenced commit and the sequencer's rebase require."""
    if clone.exists():
        shutil.rmtree(clone)
    clone.parent.mkdir(parents=True, exist_ok=True)
    git = ["git", "clone", "-q", str(source), str(clone)]
    result = subprocess.run(git, capture_output=True, text=True)
    if result.returncode != 0:
        _fail(
            f"git clone {source} -> {clone} failed (rc={result.returncode}): "
            f"{(result.stderr + result.stdout).strip()[:512]}"
        )
    for args in (
        ("config", "user.name", "cambium-coldstart"),
        ("config", "user.email", "cambium-coldstart@example.com"),
        ("config", "gc.auto", "0"),
    ):
        r = subprocess.run(["git", "-C", str(clone), *args], capture_output=True, text=True)
        if r.returncode != 0:
            _fail(f"git {' '.join(args)} failed in clone: {r.stderr.strip()[:256]}")
    # The clone inherits the source worktree's checked-out branch (here
    # ``worker-reuse``), but the supervisor resolves base_commit and publishes
    # onto ``refs/heads/main``. Create and check out a local ``main`` for that
    # publication contract.
    rev = subprocess.run(
        ["git", "-C", str(clone), "rev-parse", "--verify", "origin/main"],
        capture_output=True,
        text=True,
    )
    main_target = "origin/main" if rev.returncode == 0 else "HEAD"
    r = subprocess.run(
        ["git", "-C", str(clone), "checkout", "-B", "main", main_target],
        capture_output=True,
        text=True,
    )
    if r.returncode != 0:
        _fail(f"git checkout -B main {main_target} failed: {r.stderr.strip()[:256]}")
    if not (clone / FIXTURE).is_file():
        _fail(f"fixture not present in clone: {clone / FIXTURE}")


def _read_phase_monos(session_dir: Path) -> dict[str, int]:
    """Return the first monotonic_ms of each per-task phase from events.db.

    All events are emitted by the supervisor process, so monotonic_ms is one
    monotonic clock within the session and the deltas are well-defined.
    """
    db = session_dir / ".cambium" / "events.db"
    if not db.is_file():
        _fail(f"events.db missing: {db}")
    conn = sqlite3.connect(db)
    try:
        conn.execute("PRAGMA busy_timeout=5000")
        rows = conn.execute("SELECT kind, monotonic_ms FROM events ORDER BY seq").fetchall()
    finally:
        conn.close()
    monos: dict[str, int] = {}
    for kind, mono in rows:
        if mono is None:
            continue
        if kind in PHASE_ORDER and kind not in monos:
            monos[kind] = int(mono)
    return monos


def _deltas(monos: dict[str, int]) -> dict[str, int | None]:
    def delta(a: str, b: str) -> int | None:
        if a in monos and b in monos:
            return monos[b] - monos[a]
        return None

    return {
        "worktree_ms": delta("task_assigned", "spawned"),
        "spawn_to_init_ms": delta("spawned", "init"),
        "init_to_ready_ms": delta("init", "ready"),
        "spawn_to_ready_ms": delta("spawned", "ready"),
        "work_ms": delta("run_task", "result"),
        "merge_ms": delta("merge_started", "merge_committed"),
        "setup_ms": delta("task_assigned", "ready"),
        "total_task_ms": delta("task_assigned", "session_ended"),
    }


def _run_one(
    *,
    index: int,
    clone: Path,
    session_root: Path,
    python: str,
    pythonpath: str,
) -> dict:
    """Run one one-task marker plan and return a measurement record."""
    task_id = f"coldstart-{index:03d}"
    branch = f"wt-{task_id}"
    session_dir = session_root / f"session-{index:02d}"
    session_dir.mkdir(parents=True, exist_ok=True)
    worktree = session_dir / f"wt-{task_id}"
    plan = {
        "tasks": [
            {
                "task_id": task_id,
                "worker": "cambium.worker",
                "task": "append the coldstart marker to the e2e fixture and commit",
                "repo": str(clone),
                "worktree_path": str(worktree),
                "branch": branch,
                "target_file": FIXTURE,
                "marker": f"cambium coldstart marker run {index}",
                "write_marker": True,
                "max_wall_s": 60,
                "max_restarts": 0,
                "provider_env_keys": [],
            }
        ]
    }
    plan_path = session_dir / "plan.json"
    plan_path.write_text(json.dumps(plan) + "\n", encoding="utf-8")

    cmd = [
        python,
        "-u",
        "-m",
        "cambium.supervisor",
        "--session-dir",
        str(session_dir),
        "--plan",
        str(plan_path),
    ]
    env = dict(os.environ)
    env["PYTHONPATH"] = pythonpath
    log_path = session_dir / "supervisor.log"
    start = time.perf_counter()
    with open(log_path, "w", encoding="utf-8") as log:
        proc = subprocess.run(cmd, env=env, stdout=log, stderr=subprocess.STDOUT)
    wall_s = time.perf_counter() - start

    monos = _read_phase_monos(session_dir)
    deltas = _deltas(monos)
    return {
        "index": index,
        "task_id": task_id,
        "returncode": proc.returncode,
        "wall_s": wall_s,
        "wall_ms": wall_s * 1000.0,
        "session_dir": str(session_dir),
        **deltas,
    }


def _fmt_ms(value: float | int | None) -> str:
    if value is None:
        return "    —"
    return f"{value:7.1f}"


def _print_per_run(records: list[dict]) -> None:
    cols = (
        ("run", "index", ">3"),
        ("rc", "returncode", ">2"),
        ("wall_ms", "wall_ms", ">9"),
        ("worktree", "worktree_ms", ">9"),
        ("spawn->init", "spawn_to_init_ms", ">11"),
        ("init->ready", "init_to_ready_ms", ">11"),
        ("spawn->ready", "spawn_to_ready_ms", ">12"),
        ("work", "work_ms", ">9"),
        ("merge", "merge_ms", ">9"),
        ("setup", "setup_ms", ">9"),
        ("total_task", "total_task_ms", ">11"),
    )
    header = "  ".join(name for name, _, _ in cols)
    print(header)
    print("-" * len(header))
    for r in records:
        cells = []
        for _, key, spec in cols:
            value = r.get(key)
            if key in ("index", "returncode"):
                cells.append(format(value, spec))
            else:
                cells.append(_fmt_ms(value))
        print("  ".join(cells))


def _median(values: list[float | int | None]) -> float | None:
    present = [v for v in values if v is not None]
    if not present:
        return None
    return float(median(present))


def _print_summary(records: list[dict]) -> None:
    if not records:
        return
    cold = records[0]
    warm_records = records[1:]
    warm_spawn = _median([r.get("spawn_to_ready_ms") for r in warm_records])
    warm_init = _median([r.get("init_to_ready_ms") for r in warm_records])
    warm_wall = _median([r.get("wall_ms") for r in warm_records])
    warm_setup = _median([r.get("setup_ms") for r in warm_records])

    print()
    print("=" * 72)
    print("COLD vs WARM (cold = run 0; warm = median of runs 1..N-1)")
    print("=" * 72)
    cold_spawn = cold.get("spawn_to_ready_ms")
    delta = cold_spawn - warm_spawn if warm_spawn is not None and cold_spawn is not None else None
    print(
        f"  spawn_to_ready  cold={_fmt_ms(cold_spawn)} ms"
        f"  warm={_fmt_ms(warm_spawn)} ms"
        f"  delta={_fmt_ms(delta)} ms"
    )
    print(
        f"  init_to_ready   cold={_fmt_ms(cold.get('init_to_ready_ms'))} ms"
        f"  warm={_fmt_ms(warm_init)} ms"
    )
    print(f"  plan wall       cold={_fmt_ms(cold.get('wall_ms'))} ms  warm={_fmt_ms(warm_wall)} ms")
    print(
        f"  setup (assigned->ready) cold={_fmt_ms(cold.get('setup_ms'))} ms"
        f"  warm={_fmt_ms(warm_setup)} ms"
    )

    # Setup fraction: share of the supervised task wall consumed before the
    # worker begins actual work. The rest is work + merge + prune.
    def frac(setup, total) -> float | None:
        if setup is None or not total:
            return None
        return 100.0 * setup / total

    cold_frac = frac(cold.get("setup_ms"), cold.get("total_task_ms"))
    warm_frac = frac(warm_setup, _median([r.get("total_task_ms") for r in warm_records]) or 0)
    print()
    print("SETUP FRACTION of supervised task wall (task_assigned -> ready / -> session_ended):")
    print(
        f"  cold: {frac(cold.get('setup_ms'), cold.get('total_task_ms'))!s:>6} %"
        if cold_frac is None
        else f"  cold: {cold_frac:6.1f} %"
    )
    if warm_frac is not None:
        print(f"  warm: {warm_frac:6.1f} %")
    else:
        print("  warm:      —")


def _print_reuse_projection(records: list[dict]) -> None:
    if not records:
        return
    spawn_values = [r.get("spawn_to_ready_ms") for r in records]
    spawn_values = [v for v in spawn_values if v is not None]
    if not spawn_values:
        return
    # Best-case (warm) spawn-to-ready observed for marker tasks.
    marker_warm = float(min(spawn_values[1:])) if len(spawn_values) > 1 else float(spawn_values[0])
    marker_saving = max(0.0, marker_warm - REUSE_IPC_FLOOR_MS)
    print()
    print("=" * 72)
    print("REUSE PROJECTION (what a persistent-worker pool would save per task)")
    print("=" * 72)
    print("  marker task (measured here):")
    print(f"    warm spawn_to_ready   = {marker_warm:7.1f} ms")
    print(f"    reuse floor (IPC)     = {REUSE_IPC_FLOOR_MS:7.1f} ms")
    pct = (100.0 * marker_saving / marker_warm) if marker_warm else 0.0
    print(f"    saving per task       = {marker_saving:7.1f} ms  ({pct:5.1f} % of spawn_to_ready)")
    print("  provider-loop task (projected from docs/research/worker-coldstart.md):")
    print(f"    dspy spawn_to_ready   = {DSPY_SPAWN_TO_READY_MS:7.1f} ms")
    dspy_saving = max(0.0, DSPY_SPAWN_TO_READY_MS - REUSE_IPC_FLOOR_MS)
    print(f"    reuse floor (IPC)     = {REUSE_IPC_FLOOR_MS:7.1f} ms")
    print(
        f"    saving per task       = {dspy_saving:7.1f} ms"
        f"  ({(100.0 * dspy_saving / DSPY_SPAWN_TO_READY_MS):5.1f} % of spawn_to_ready)"
    )


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Measure Cambium worker spawn/cold-start cost vs reuse.",
    )
    parser.add_argument(
        "--repo",
        required=True,
        help="throwaway clone path (created fresh; the repo the marker task edits)",
    )
    parser.add_argument(
        "--tasks",
        type=int,
        default=3,
        help="number of sequential one-task plans to run (default 3)",
    )
    parser.add_argument(
        "--source",
        default=str(REAL),
        help=f"source repo to clone (default {REAL})",
    )
    parser.add_argument(
        "--python",
        default=DEFAULT_PYTHON,
        help=f"interpreter for the supervisor subprocess (default {DEFAULT_PYTHON})",
    )
    parser.add_argument(
        "--pythonpath",
        default=os.environ.get("PYTHONPATH", str(REAL / "src")),
        help="harness src PYTHONPATH for the supervisor subprocess "
        "(default $PYTHONPATH or <script>/../src)",
    )
    args = parser.parse_args(argv)
    if args.tasks < 1:
        parser.error("--tasks must be >= 1")
    return args


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    for binary in (args.python, "git"):
        if shutil.which(binary) is None and not Path(binary).exists():
            _fail(f"{binary} is required")
    if not Path(args.pythonpath).is_dir():
        _fail(f"PYTHONPATH src dir not found: {args.pythonpath}")

    clone = Path(args.repo).resolve()
    source = Path(args.source).resolve()
    session_root = clone.parent / f"{clone.name}-sessions"
    if session_root.exists():
        shutil.rmtree(session_root)
    session_root.mkdir(parents=True, exist_ok=True)

    print(f"measure_worker_coldstart: cloning {source} -> {clone}")
    _seed_clone(source, clone)
    print(f"measure_worker_coldstart: running {args.tasks} sequential one-task plans")

    records: list[dict] = []
    for index in range(args.tasks):
        record = _run_one(
            index=index,
            clone=clone,
            session_root=session_root,
            python=args.python,
            pythonpath=args.pythonpath,
        )
        records.append(record)
        print(
            f"  run {index}: rc={record['returncode']} "
            f"wall={record['wall_ms']:.1f} ms "
            f"spawn_to_ready={_fmt_ms(record.get('spawn_to_ready_ms'))} ms"
        )
        if record["returncode"] != 0:
            _fail(
                f"supervisor run {index} exited {record['returncode']}; "
                f"see {record['session_dir']}/supervisor.log"
            )

    print()
    print("=" * 72)
    print("PER-RUN PHASE DELTAS (ms, from events.db monotonic_ms)")
    print("=" * 72)
    _print_per_run(records)
    _print_summary(records)
    _print_reuse_projection(records)

    print()
    print("=" * 72)
    print("SUMMARY_JSON")
    print("=" * 72)
    summary = {
        "tasks": args.tasks,
        "clone": str(clone),
        "records": records,
        "cold": records[0] if records else None,
        "warm_spawn_to_ready_ms": _median([r.get("spawn_to_ready_ms") for r in records[1:]]),
        "reuse_floor_ms": REUSE_IPC_FLOOR_MS,
        "dspy_spawn_to_ready_ms": DSPY_SPAWN_TO_READY_MS,
    }
    print(json.dumps(summary, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
