# Vertical Slice — End-to-End Proof (ONE worker)

**Branch:** `wt-slice` · **Date:** 2026-08-09 · **Status:** built + verified.
**Purpose:** the adversarial review's gate: prove the harness works end-to-end with ONE
worker before more theory. A real supervisor subprocess-spawns a real worker script, speaks
JSON-Lines over pipes, runs a real gate, and merges a real git change — all stdlib + git,
Python 3.14.7 (uv), no LLM, no network, no DSPy, no sandbox.

---

## What was built (files)

| File | Role |
|---|---|
| `src/cambium/supervisor.py` | Minimal asyncio supervisor: `create_subprocess_exec` with pipes; JSON-Lines wire loop (`init`→`ready`→`run_task`→`result_envelope`→`exit_message`, request_id correlation); appends events to `<session_dir>/.cambium/events.jsonl`; runs the task's gate; merges with `git merge --ff-only`; exit 0 only when everything succeeded. CLI via `python -m cambium.supervisor --session-dir <dir>`. |
| `scripts/fake_worker.py` | The worker: reads JSON-Lines from stdin, answers `ready`; on `run_task` creates a throwaway git worktree of the scratch repo (path + branch from the payload), appends the marker line to the target file, commits, emits `result_envelope {task_id, status, commits, files_changed, diff, failure_reason}` then `exit_message`, exits 0. Gate-failure path: when `write_marker=false` (or the edit did not land) it reports `status="failed"`. |
| `tests/scenarios/test_vertical_slice.py` | Two end-to-end scenario tests through the real spawn path (happy path + gate-failure/no-merge). |
| `docs/research/vertical-slice-report.md` | This document. |

## Protocol sequence (as built)

```
Supervisor                          fake_worker.py                      scratch repo
─────────                           ──────────────                      ───────────
create_subprocess_exec(pipes)
  ── stdin: {"type":"init","request_id":R1,"task_id",proto:1,generation:1,"spec"}
                     ◀─ stdout: {"type":"ready","request_id":R1,"pid":…}
  ── stdin: {"type":"run_task","request_id":R2,"task_id",
               scratch_repo,worktree_path,branch,target_file,marker,write_marker}
                          │            git worktree add -b <branch> <wt> main
                          │            append "// cambium-slice" to hello.txt
                          │            git add + commit
                     ◀─ stdout: {"type":"result_envelope","request_id":R1,
                                   "status":"succeeded","commits":[sha],
                                   "files_changed":["hello.txt"],"diff":…}
                     ◀─ stdout: {"type":"exit_message","request_id":R1,"reason":"done"}
stdout EOF → proc.wait() == 0
sh -c "<gate>" in the worker worktree            → rc 0 (grep finds the marker)
git -C scratch merge --ff-only <branch>          → main == worker tip, working tree updated
exit 0   (every step succeeded)
```

Negative path: worker emits `result_envelope status="failed"` (write_marker=false), gate
rc=1, no merge, `main` unchanged, supervisor exit 1.

## Verification (exact commands + outputs)

All run from the worktree root `/tmp/opencode/cambium-slice`.

1. `uv run --python 3.14.7 --extra test pytest -q` → `8 passed in 0.27s`
   (existing 6 stay green: `6 passed` before the slice, `8 passed` after — 2 new scenario tests).
2. `uv run --python 3.14.7 python -m compileall -q src scripts` → rc=0 (no output).
3. Manual run:
   `uv run --python 3.14.7 python -m cambium.supervisor --session-dir /tmp/opencode/slice-run`
   → supervisor exit **0**; session transcript and event log below.
4. `git status --porcelain` after commit → clean (see commit hash in the final report).

Manual-run transcript (happy path):

```
       spawned  {"task_id": "slice-001", "worker": "…/scripts/fake_worker.py"}
          init  {"task_id": "slice-001", "request_id": "18ca413232bac4b3-0001"}
         ready  {"task_id": "slice-001", "request_id": "18ca413232bac4b3-0001", "pid": 1802152}
      run_task  {"task_id": "slice-001", "request_id": "18ca4132354ff140-0002"}
        result  {"task_id": "slice-001", "request_id": "18ca413232bac4b3-0001", "status": "succeeded"}
          exit  {"task_id": "slice-001", "request_id": "18ca413232bac4b3-0001", "reason": "done"}
          gate  {"command": "grep -q '// cambium-slice' hello.txt", "exit_code": 0}
         merge  {"branch": "wt-slice-001", "exit_code": 0, "sha": "24d94910838a38c83655c9003c8cfc122826c91f"}
 session_ended  {"status": "succeeded", "exit_code": 0, "worker_exit_code": 0}
result: status=succeeded exit_code=0 worker_exit=0 worker_status=succeeded gate_exit=0 merge=24d9491…
SUPERVISOR_EXIT=0
```

Event log `/tmp/opencode/slice-run/.cambium/events.jsonl` (kinds in order):
`spawned, init, ready, run_task, result, exit, gate, merge, session_ended` — the mandated
sequence (init, ready, run_task, result, exit) is present in order. Merged result:

```
$ cat /tmp/opencode/slice-run/scratch/hello.txt
hello from the vertical slice
// cambium-slice
$ git -C /tmp/opencode/slice-run/scratch log --oneline
24d9491 cambium-slice: slice-001
484feb9 initial
```

Negative-path manual run (via `<session-dir>/task.json`, `write_marker=false`):
`result status=failed exit_code=1 … gate_exit=1 merge=None`, `SUPERVISOR_EXIT=1`;
`main` still at `initial`; marker absent from `hello.txt`.

## Divergences from the architecture drafts (flagged)

1. **`run_task` wire message.** `arch §5.2` has no wire dispatch message (one task per
   process, delivered entirely by `init`; persistent pool deferred to v2.1). The slice uses
   `init` + `run_task`, the IPC draft's flagged extension (§7.1) — the task mandated it.
2. **Message names / status values.** Built with the draft names `result_envelope`,
   `exit_message`, `status ∈ {succeeded, failed}` (IPC draft §7.7–8); `arch §5.2` names them
   `result`/`exit` with `status ∈ {done, failed, …}`. Wire-shape semantics identical.
3. **No `ok` response to `run_task`.** The draft (§2.2) has the worker reply `ok` when the
   loop starts; the slice goes straight `run_task` → `result_envelope`.
4. **Event log is plain appended JSON-Lines, not SQLite WAL on a writer thread.**
   `arch §6` / custos-design §2.4 specify SQLite WAL + dedicated writer thread + fsync
   cadence + critical-event tiers; the slice appends to `.cambium/events.jsonl` with a
   per-line flush, **synchronously on the event loop** (a direct DS-C1/DS-M3 deviation, and
   the arch's JSONL is only an optional mirror). Durability intent: append+flush only; a
   crash can lose the tail. The writer-thread/fsync design is the next milestone, not this
   slice.
5. **`request_id` is not a ULID.** Draft §5 requires ULID (monotonic-ish). No new deps →
   `f"{time.time_ns():x}-{seq:04x}"` — monotonic, unique within a run, not a real ULID.
6. **Merge is `git merge --ff-only` in the scratch checkout, not Unio's
   throwaway-worktree + `update-ref refs/heads/main <tip> <old>`.** Per the
   worktree-concurrency findings (F3/F17): concurrent ff-only merges in one checkout are
   the hazard; with exactly ONE worker (serialized by construction) ff-only in the scratch
   repo is safe, and it also fast-forwards the working tree so the test/reader sees the
   merged edit. The `update-ref`-with-old-SHA publish is the concurrency-safe form needed
   when N workers merge — future Unio milestone. The findings' other rules are honored:
   worker branches are never merged into from a worker worktree, and the worker only
   advances its own branch.
7. **Worker creates its own worktree on `run_task`.** `arch §7.5`/Surculus creates/reovers
   the worktree before spawn; the slice worker does `git worktree add` (with a stale
   worktree/branch teardown first). Worktree recovery/prune/quarantine are out of scope.
8. **Event `kind` names follow the wire messages** (`init`, `ready`, `run_task`, `result`,
   `exit`, `gate`, `merge`) rather than arch §3.6 kinds (`worker_spawned`, `worker_ready`,
   …). Shape is `{"kind","timestamp","payload"}`, not the full §3.6 Event schema
   (`request_id`, `monotonic_ms`, `generation` as top-level fields).
9. **CLI bootstraps the scratch repo** (`git init -b main` + initial commit when missing)
   so the documented manual run works from an empty `--session-dir`. The library
   `run_session` never creates repos; this is a test-harness convenience only.
10. **`generation` is fixed at 1; no fencing.** `.cambium/generation` is not written and no
    mismatch check runs (arch §7.3). Fencing is explicitly out of scope (below).

## NOT in scope (explicitly, per the task)

- Heartbeats / heartbeat watchdog / liveness layers 3–4 (only `exit_message` + `proc.wait()`).
- Restart policy, burst/absolute caps, jitter, backoff.
- Real LLM, DSPy, provider cascade, cache, redaction.
- Event-log durability (SQLite WAL, writer thread, fsync cadence, critical tiers, replay).
- Logging design (stdlib structured logging, rotation, QueueListener).
- Worktree recovery/quarantine/prune, merge sequencer with `asyncio.Lock` + `update-ref`,
  generation fencing, sandboxing, timeouts.
