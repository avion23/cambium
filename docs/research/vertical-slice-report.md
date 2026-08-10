# Vertical Slice — End-to-End Proof (ONE worker)

**Snapshot:** branch `wt-slice`, 2026-08-09; historical, built + verified. The
slice proves one real supervisor/worker subprocess, JSON-Lines IPC, gate, and
git merge on Python 3.14.7 (stdlib + git; no LLM, network, DSPy, or sandbox).
Current behavior is defined by the [Python 3.14 docs](https://docs.python.org/3.14/)
and the repository tests; this record keeps only the slice evidence.

**Revision 2 (review CONDITIONAL-PASS must-fixes):** enforced worker-exit / exit_message /
result_envelope failure conditions, run_task↔result_envelope request_id correlation,
minimal timeouts (killpg), and path safety. The reviewer's two repro cases now fail
correctly (see §Verification item 4).

---

## What was built (files)

| File | Role |
|---|---|
| `src/cambium/supervisor.py` | Minimal asyncio supervisor: `create_subprocess_exec` with pipes (`start_new_session=True` for killpg); JSON-Lines wire loop (`init`→`ready`→`run_task`→`result_envelope`→`exit_message`, request_id correlation: ready echoes init R1, result_envelope echoes run_task R2, exit_message carries no request_id); appends events to `<session_dir>/.cambium/events.jsonl`; runs the task's gate; merges with `git merge --ff-only`; **exit 0 only when every step succeeded**. Session is FAILED when the worker's process exit code != 0 (supervisor exit code then reflects it), `exit_message` is missing at EOF, or `result_envelope` is missing/not correlated. Timeouts: ready_timeout (10 s), gate_timeout (30 s), wall budget (120 s), configurable via task_spec or env — on timeout the worker process group is killed. CLI via `python -m cambium.supervisor --session-dir <dir>`. |
| `scripts/fake_worker.py` | The worker: reads JSON-Lines from stdin, answers `ready` (echoes init R1); on `run_task` creates a throwaway git worktree of the scratch repo (path + branch from the payload), appends the marker line to the target file, commits, emits `result_envelope {request_id=R2, task_id, status, commits, files_changed, diff, failure_reason}` then `exit_message` (no request_id), exits 0. Gate-failure path: when `write_marker=false` (or the edit did not land) it reports `status="failed"`. Path safety: refuses to force-remove or write outside the session scratch root. `FAKE_MODE` env selects behavior variants for the scenario tests (healthy / exit5 / noexit / noresult / badrid / noready). |
| `tests/scenarios/test_vertical_slice.py` | Eight end-to-end scenario tests through the real spawn path (happy path, gate-failure/no-merge, and the review's must-fix cases: nonzero exit, missing exit_message, missing envelope, misrouted envelope, correlation, ready timeout). |
| `docs/research/vertical-slice-report.md` | This document. |

## Protocol sequence (as built)

```
Supervisor                          fake_worker.py                      scratch repo
─────────                           ──────────────                      ───────────
create_subprocess_exec(pipes, start_new_session=True)
  ── stdin: {"type":"init","request_id":R1,"task_id",proto:1,generation:1,"spec"}
                     ◀─ stdout: {"type":"ready","request_id":R1,"pid":…}     # echoes R1
  ── stdin: {"type":"run_task","request_id":R2,"task_id",
               scratch_repo,worktree_path,branch,target_file,marker,write_marker}
                          │            git worktree add -b <branch> <wt> main
                          │            append "// cambium-slice" to hello.txt
                          │            git add + commit
                     ◀─ stdout: {"type":"result_envelope","request_id":R2,    # echoes R2
                                   "status":"succeeded","commits":[sha],
                                   "files_changed":["hello.txt"],"diff":…}
                     ◀─ stdout: {"type":"exit_message","reason":"done"}       # no request_id
stdout EOF → proc.wait() == 0
sh -c "<gate>" in the worker worktree            → rc 0 (grep finds the marker)
git -C scratch merge --ff-only <branch>          → main == worker tip, working tree updated
exit 0   (every step succeeded)
```

### Session failure conditions (enforced; any one overrides the envelope's status → failed)

| # | Condition | Supervisor exit code |
|---|---|---|
| (a) | worker process exit code != 0 (envelope may still say `succeeded`) | **the worker's real exit code** (e.g. 5) |
| (b) | `exit_message` missing at EOF | 1 |
| (c) | `result_envelope` missing, or its request_id != run_task's R2 (undeliverable) | 1 |
| — | timeout (ready / gate / wall) — worker process group killed via killpg | 3 (arch §16.4) |

The envelope's `status` is the primary signal; the gate is the verification step; gate and
merge run only when none of the conditions fired.

Negative path (write_marker=false): worker emits `result_envelope status="failed"`, gate
rc=1, no merge, `main` unchanged, supervisor exit 1.

## Verification (exact commands + outputs)

All run from the worktree root `/tmp/opencode/cambium-slice`.

1. `uv run --python 3.14.7 --extra test pytest --collect-only -q`; the full
   `uv run --python 3.14.7 --extra test pytest -q`; and
   `uv run --python 3.14.7 --with ruff ruff check src` all passed in the slice
   run. Test counts are intentionally omitted; rerun the commands for a current
   count.
2. `uv run --python 3.14.7 python -m compileall -q src scripts` → rc=0 (no output).
3. Manual run:
   `uv run --python 3.14.7 python -m cambium.supervisor --session-dir /tmp/opencode/slice-run`
   → supervisor exit **0**; session transcript and event log below.
4. Reviewer-case repros (must now FAIL correctly, before the fix they returned
   `status=succeeded exit_code=0`):
   - `worker_exit5` (full worker, `sys.exit(5)` after `exit_message`):
     `status=failed exit_code=5 worker_exit=5 … merge=None`, `SUPERVISOR_EXIT=5`, `main` unchanged.
   - `worker_noexit` (full worker, `exit_message` omitted):
     `status=failed exit_code=1 … exit_reason=null merge=None`, `SUPERVISOR_EXIT=1`, `main` unchanged.
5. `git status --porcelain` after commit → clean (see commit hash in the final report).

Manual-run transcript (happy path):

```
       spawned  {"task_id": "slice-001", "worker": "…/scripts/fake_worker.py"}
          init  {"task_id": "slice-001", "request_id": "18ca41b9ad5972aa-0001"}
         ready  {"task_id": "slice-001", "request_id": "18ca41b9ad5972aa-0001", "pid": …}
      run_task  {"task_id": "slice-001", "request_id": "18ca41b9b13ca8ef-0002"}
        result  {"task_id": "slice-001", "request_id": "18ca41b9b13ca8ef-0002", "status": "succeeded"}
          exit  {"task_id": "slice-001", "reason": "done"}
          gate  {"command": "grep -q '// cambium-slice' hello.txt", "exit_code": 0}
         merge  {"branch": "wt-slice-001", "exit_code": 0, "sha": "b39339d…"}
 session_ended  {"status": "succeeded", "exit_code": 0, "worker_exit_code": 0, "timed_out": false}
result: status=succeeded exit_code=0 worker_exit=0 worker_status=succeeded gate_exit=0 merge=b39339d…
SUPERVISOR_EXIT=0
```

Correlation in the event log: `ready.request_id == init.request_id` (R1),
`result.request_id == run_task.request_id` (R2), and the `exit` event carries no
`request_id`.

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

Review must-fix additions (this revision):

11. **Correlation semantics.** `result_envelope` echoes **run_task's** request_id (R2);
    `exit_message` is connection-level and carries **no** request_id, per `arch §5.2` `exit`
    (the IPC draft §2.4 said it echoes init — the reviewer overrode this). A
    `result_envelope` whose request_id != R2 is treated as undeliverable → failed.
12. **Enforced failure conditions.** The earlier draft logged `eof_without_exit` but did not
    fail; it logged `proc.wait()` but derived the exit code only from the envelope. Now
    worker exit != 0, missing `exit_message`, and missing/undeliverable `result_envelope`
    each override the envelope's status to failed, and the supervisor exit code reflects the
    worker's real exit code (e.g. 5) instead of a flat 1. This is stricter than `arch §5.3`
    prescribes for the "exit is authoritative" cross-check and matches the review demand.
13. **Minimal timeouts (draft addition; arch defines none in the wire loop).** ready_timeout
    10 s, gate_timeout 30 s, wall budget 120 s (task_spec key or `CAMBIUM_READY_TIMEOUT_S` /
    `CAMBIUM_GATE_TIMEOUT_S` / `CAMBIUM_WALL_BUDGET_S` env). On timeout the worker's
    process group is killed (`killpg` via `start_new_session=True`, arch §7.2) and the
    session fails with exit code 3 (arch §16.4 timeout). No restart policy — a timed-out
    task is failed, not retried. The merge step is not individually timeout-wrapped; it is
    gated by the wall budget.
14. **Path safety.** `target_file` must resolve inside the worker's worktree; `worktree_path`
    must resolve inside the session dir; the worker refuses to `git worktree remove --force`
    a path that is not under the session scratch root (scratch repo's parent). Enforcement:
    `_validate_paths()` (supervisor, before spawn and before CLI bootstrap) + the same
    prefix checks in the worker. This is defense against `..`/absolute-path payloads, not a
    substitute for the (out-of-scope) sandbox.

## NOT in scope (explicitly, per the task)

- Heartbeats / heartbeat watchdog / liveness layers 3–4 (only `exit_message` + `proc.wait()` +
  the three minimal timeouts).
- Restart policy, burst/absolute caps, jitter, backoff (a timeout fails the task; no retry).
- Real LLM, DSPy, provider cascade, cache, redaction.
- Event-log durability (SQLite WAL, writer thread, fsync cadence, critical tiers, replay).
- Logging design (stdlib structured logging, rotation, QueueListener).
- Worktree recovery/quarantine/prune, merge sequencer with `asyncio.Lock` + `update-ref`,
  generation fencing, sandboxing.
