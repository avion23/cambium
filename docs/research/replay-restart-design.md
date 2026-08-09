# Research — Crash Recovery: Event-Log Replay and Supervisor Restart Semantics

**Status:** Design research (docs only). Companion to `docs/architecture/architecture.md` (v2, wt-arch branch — pending merge into main per `implementation-plan.md`).
**Date:** 2026-08-09
**Scope:** Supervisor (Custos, M4) crash recovery; worker-originated crashes covered only where they interact with supervisor restart. Merge recovery (§7.8) covered for the idempotency and sequence diagrams.

**Normative sources (cited inline by section):**
- `docs/architecture/architecture.md` — v2 architecture. **Authoritative.** §5 (Nuntius IPC / liveness), §6 (event log), §7 (lifecycle: state machine, spawn, fencing, restart policy, worktree recovery, shutdown, publish), §16 (session-dir contract), §18.1 (DS-C6/C5/M3 resolution matrix).
- `docs/architecture/reviews/review-distributed-systems.md` — DS review. **C6** (supervisor crash orphans workers, split-brain), **M3** (event log has no durability guarantees), **C5** (worktree locks survive crashes).
- `docs/architecture/system-design.md` — v0.1 draft (superseded by v2). **M4 decision notes 6–7** ("No `.pid` files to go stale", "stdout EOF = dead").

**Verification convention:** statements that are explicit in the architecture are cited to a section. Statements that this document **proposes** (gaps the architecture leaves open) are marked **[PROPOSED]**. Claims that could not be verified against a source are marked **UNVERIFIED**.

---

## 1. Crash taxonomy

Crash sources relevant to supervisor restart, in descending order of blast radius.

### 1.1 Supervisor crash (SIGKILL / power loss)

The supervisor process dies without running `shutdown()` (§7.7). Consequence set:

1. **Every worker pipe pair breaks.** The kernel closes the supervisor's stdin/stdout FDs on process exit. A worker's next `stdout.write()` raises `BrokenPipeError` or triggers `SIGPIPE`; a worker blocked on `stdin.readline()` sees EOF. No handler for this is specified in the worker protocol (v0.1 reviewed at DS-C6; v2 does not re-specify worker-side pipe-break handling).
2. **Orphaned workers.** Workers not currently blocked on a pipe write (e.g., mid-LLM-call) are reparented to PID 1 and may keep running for minutes, mutating their worktrees. The v2 fencing token exists precisely for this case (§7.3).
3. **In-flight events are lost at the queue boundary.** The event log writer thread (§6.2) is in the same process. Events enqueued in the bounded queue but not yet committed are lost. Per §6.5: critical events are fsync-d *before* the producer ack, so a committed critical event survives; non-critical events within the last `fsync_interval_s` (default 1 s) may be lost.
4. **Distinction SIGKILL vs power loss.** `kill -9` (or a crash/panic) keeps the page cache intact: un-fsync'd data may still survive a plain process restart, but must not be relied on. Power loss destroys the page cache — only fsync'd data survives. Recovery must therefore treat **only fsync'd/committed events as durable** (§6.5), regardless of which kind of loss occurred.
5. **`result.json` may be missing or stale.** It is written atomically at completion (§3.4, §16.4 invariant 1); a crash after the durable `result` event but before the rename leaves the task completed-in-the-log but missing-in-`result.json`. §2.2 reconstructs it.
6. **Worktree dirt.** A worker killed mid-operation leaves `index.lock`, rebase/merge state, untracked build artifacts (DS-C5). `Surculus.recover()` (§7.5) is the fix — see §2.4.

### 1.2 Worker crash

A worker exits abnormally (SIGKILL, segfault, OOM, unhandled exception, or `exit reason="crash"`). This is the *normal* restart path, handled entirely inside a running supervisor: liveness layer 1 (`proc.wait()`, definitive) or layer 2 (`exit` message, authoritative) fires (§5.3), the state machine moves to CRASHED (§7.1), restart policy engages (§7.4), `Surculus.recover()` runs before respawn (§7.5).

Worker crash interacts with supervisor restart only when it happens *before* a supervisor crash (it is recorded in the log as `worker_exit`) or *after* (the orphan case above). The restart protocol (§2) must not double-count a worker that already exited.

### 1.3 Network / pipe loss

Cambium is single-host (§1 non-goal 2), so "network loss" means:

- **Worker↔supervisor pipe loss** — EOF on stdout. v2 explicitly does **not** equate EOF with death: EOF is advisory (liveness layer 4); the supervisor runs a 5 s grace timer, `proc.poll()`, and escalates to `ping`/`pong` and process-group kill (§5.3). A supervisor-induced drain stall is flagged as `supervisor_stall`, never blamed on the worker (§5.3, §5.4 mode d).
- **LLM provider outage** — `Diffundo.AllProvidersFailed` is retried inside the worker for `provider_patience_s` (default 180 s) and is *not* a worker failure (§7.4); it does not enter the crash-recovery path.
- Pipe loss does **not** damage the event log; it is a liveness event. It is listed here because a supervisor crash *causes* pipe loss (1.1.1), and the restart protocol must distinguish "pipe broke because supervisor died" (recover) from "pipe broke but worker alive" (liveness escalation, §5.3).

### 1.4 Partial event-log write (torn last line)

Two distinct surfaces:

- **Durable log torn write.** With the SQLite primary store this cannot produce a corrupt row (atomic commit; see §4.1). With the JSONL mirror (off by default, §6.1) a SIGKILL/power loss mid-`write()` leaves a truncated final line (§4.2).
- **Protocol torn write.** A worker killed mid-`write()` of the `result` line on stdout leaves a partial JSON line (DS-C2 mode c). The v2 mitigation: the worker persists the result to the checkpoint store **before** emitting it; the supervisor recovers the result from checkpoints at the next watchdog tick (§5.4 mode c). Not a log-corruption issue, but it motivates why "result in the log" and "result in checkpoints" are both kept.

---

## 2. Restart protocol

### 2.1 What "state" must be reconstructed

On supervisor start for an existing session, the following is rebuilt from durable sources:

| Reconstructed state | Source | Arch section |
|---|---|---|
| Per-task lifecycle state (which tasks exist, terminal vs in-flight) | event log: last durable event per task | §6.5, §7.1 |
| Per-task resume point | latest `checkpoint` event (`state_ref`, `commits_so_far`) + checkpoint file | §6.4 |
| Restart budget (`crash_times`, restart count) | event log: `worker_exit` / `heartbeat_timeout` events; `init.budget.max_restarts` | §7.4 |
| Generation counter | max `generation` observed per task, or `worktree/.cambium/generation` file | §7.3 |
| Orphan kill targets | `worker_spawned` events (pid in payload) | §2.3 |
| Merge state | `refs/heads/main` vs latest `merge_committed` event | §7.8 |
| `result.json` | durable `result` event payload (reconstruct if missing) | §3.4, §2.2 |
| Subscriber stream | replay `events` since last `snapshots` row; emit `recovery_gap` on seq gaps | §6.5 |

**Claim (consistent with §6.4):** the event log reconstructs *what happened* (lifecycle); checkpoints reconstruct *where to resume*. The two are distinct stores and both are needed.

### 2.2 Reconstruction: last durable event per task

Replay `events` since the last `snapshots` row in `seq` order (§6.1, §6.5). For each `task_id`, keep the highest-`seq` event per kind; apply the terminal-decision table:

| Last durable event(s) | Reconstructed task state | Restart action |
|---|---|---|
| `result` (critical) | DONE | Do **not** resume. If `result.json` missing (crash between DB commit and atomic rename), reconstruct it from the `result` event payload [PROPOSED; §6.5 supports "reconstruct", §3.4 defines result.json]. |
| `task_failed` (critical) | FAILED | Do **not** resume. Return failure. |
| `worker_exit` (critical) after `worker_spawned`, no `result`/`task_failed` | CRASHED | Resume per §2.4 (recover worktree, restart policy, respawn). |
| `checkpoint` (critical), no terminal event | RUNNING at checkpoint N | Resume from checkpoint N (§6.4 `resume_from_checkpoint`). |
| `task_assigned` only (no `worker_ready`, no `checkpoint`) | PENDING/SPAWNING, no durable progress | Rerun from scratch (no checkpoint to restore). |
| `task_assigned` + `worker_spawned`/`worker_ready`, no terminal event | In-flight | Treat worker as dead-or-orphaned (kill/fence, §2.3), then CRASHED → resume. |

Notes:
- Only **critical** events are trusted for the terminal decision. Non-critical tail events (`heartbeat`, `tool_event`, `worker_spawned`) are advisory (may be lost within `fsync_interval_s`, §6.5) and never determine terminal state. **[PROPOSED]** This is the consistent reading of §6.5's tiering.
- The supervisor re-injects `resume_from_checkpoint` via the `init` message (§6.4); workers without a checkpoint accept a fresh start.
- A `result` event already received but followed by a crash-before-`exit`-message is still DONE: per §5.2 the supervisor cross-checks exit against `result`, but for *recovery* the durable `result` event is authoritative. **[PROPOSED]**

### 2.3 Orphan detection: three candidate signals

The question: on restart, how does the new supervisor know a worker from the previous instance is still alive, and which one is "mine"?

**(a) Worker pid file.** v0.1 explicitly rejected pid files ("No lock files: the supervisor is the parent process. It knows the PID directly. No `.pid` files to go stale." — system-design.md M4, decision 6). v2 dropped `.pid` files (§0 "Primary patterns dropped"). Problems: (1) the new supervisor is *not* the parent (orphans reparent to PID 1), so the file is the only link and it is exactly the stale link v0.1 worried about; (2) PID recycling makes a live PID a false-positive owner; (3) a pid file captures one PID, not the process group (grandchildren escape). **Verdict: rejected by design; do not reintroduce.**

**(b) OS process scan** (`/proc`, `os.kill(pid, 0)`). Useful only as a *liveness probe of a candidate*: the new supervisor cannot call `wait()` on a reparented child, and scanning cannot tell a Cambium worker from an unrelated process without a side signal. **Verdict: corroborating only — a kill target must first be derived from a trusted source, and the result of `kill(pid, 0)` is only "some process with this PID exists right now".**

**(c) Event log + generation fencing (the design's own signals).** The event log durably records, per task, `worker_spawned` with `generation` (and the worker's `pid` in the `ready` message payload, §5.2; the `worker_spawned` event must carry the pid in `payload` for this to work **[PROPOSED]**, consistent with v0.1 which logged `pid` at system-design.md M4 `_log_event({"type":"worker_spawned","pid":proc.pid})`). Because workers are spawned with `start_new_session=True` (§7.2), each worker is a **process-group leader: `pgid == pid`**, so the logged pid addresses the whole subtree (§5.1 invariant 6, §7.2).

The restart protocol therefore uses, in order:

1. **Identity:** the event log is the authoritative list of what the previous supervisor owned — per task, the last `worker_spawned` pid + generation with no subsequent terminal event.
2. **Cleanup:** `os.kill(pgid, 0)` to probe the candidate; if alive, `os.killpg(pgid, SIGKILL)`. This is the "process-group kill at startup" that §7.3 references (and that §18.1 DS-C6 lists as resolved by "generation fencing token; `start_new_session=True` + process-group kill on startup"). **UNVERIFIED:** the architecture references process-group kill at startup (§7.3, §18.1) but does not specify the trigger or the source of the pgid list; §2.6 below is the concrete protocol.
3. **Enforcement backstop:** `Surculus.recover()` writes the bumped `generation` to `worktree/.cambium/generation` (§7.5 step 5). Any orphan that survives the kill (kill raced with worker exit, or the probe missed) reads the mismatch before its next git operation or `state_ref` write and self-terminates with `exit reason="fatal"` (§7.3). **This is the definitive signal: split-brain becomes detectable rather than silent** (§7.3).

**Preferred signal: the design's own (event log + generation fencing), with the OS probe as the kill-delivery mechanism — not as an identity source.**

### 2.4 In-flight tasks: resume vs rerun vs fail

After orphan cleanup, every task that was in flight is **terminated and restarted as a fresh worker** — the v2 model never reattaches to a live orphan (that is the v0.1 split-brain failure, DS-C6). The resume/rerun/fail decision is purely a function of durable state:

| Has durable checkpoint? | Terminal event? | Outcome |
|---|---|---|
| yes | no | **Resume** from checkpoint N (`resume_from_checkpoint`, §6.4). Worktree recovered to base + cherry-pick of `commits_so_far` (§7.5 step 6). |
| no | no | **Rerun** from scratch (fresh start; read-only/opt-out tasks per §6.4). |
| — | `result` / `task_failed` | **Fail-as-done** / **fail**; never resume. |
| — | restart budget exceeded (burst cap ≥5 in 60 s, absolute cap ≥10, or `max_wall_s` exhausted) | **FAILED** (mark `task_failed` with reason, §7.4). |

The supervisor does not restore "live" worker memory (trajectory buffer, in-progress tool call); it restores the **checkpoint** only. The interrupted tool call is re-executed by the fresh worker from the checkpoint. This matches "resume, not rerun" at the granularity the design guarantees: per-tool-call checkpoints (§6.4).

Restart budget rebuild: `crash_times` from durable `worker_exit`/`heartbeat_timeout` events, then apply the jittered backoff from the restart policy (§7.4: `delay = random.uniform(0, base_delay * backoff_base ** n)`), so a thundering herd after a mass supervisor crash is avoided (DS-C4).

### 2.5 Worktree policy on restart

The task asks: recreate from main, or discard dirty? Per §7.5 (and the DS-C5/IMPL-M9 resolution):

- **Reuse the worktree path, never reuse its dirty state.** Before every respawn (first spawn *and* every restart), `Surculus.recover(worktree, base_commit)` runs: remove `*.lock` under `worktree/.git` and `repo/.git/worktrees/<id>`; abort in-progress `rebase`/`merge`/`cherry-pick`/`revert`; `git reset --hard ${base_commit}` (drop working-tree changes); `git clean -fd` (remove untracked build artifacts); write the new `generation`; optionally restore checkpoint commits by cherry-picking `commits_so_far` onto `base_commit` (fall back to fresh start if the cherry-pick fails).
- **base_commit, not current main.** The reset target is the task's fork-point `base_commit` from the `init` message (§5.2, §7.2), so the worktree is deterministically reconstructible regardless of how far `main` moved during the crash. §7.5's normative text says `git reset --hard ${base_commit}`.
- **Quarantine on failure.** If recovery fails (step 3 non-zero), the worktree is moved to `${session_dir}/.cambium/quarantine/${task_id}-${generation}/` and a fresh worktree is created from `base_commit` (§7.5). This is the C6 option-3 fallback (fresh worktree per restart) applied only where in-place recovery cannot guarantee a clean tree.
- **`Surculus.prune()`** runs on supervisor startup (and shutdown) to clean stale `git worktree` admin entries (§7.5).

Result: IMPL-M9 ("restart reuses possibly-corrupted worktrees") is resolved by construction — the reused path is reset to a known-good state before any new worker touches it.

### 2.6 Supervisor-start sequence (proposed, closing the §7.3 gap)

**UNVERIFIED / [PROPOSED]** — the architecture mandates fencing + process-group kill at startup (§7.3, §18.1) but not the concrete steps. The following is the minimal consistent protocol:

1. Open `${session_dir}/.cambium/events.db` (SQLite replays the WAL automatically — §6.5). Detect `seq` gaps → emit `recovery_gap` event (§6.5).
2. Replay `events` since the last `snapshots` row; rebuild the per-task state machine (§2.2) and restart budgets.
3. For each task whose last durable event is a `worker_spawned` (with pid) and no terminal event: probe `os.kill(pid, 0)`; if alive, `os.killpg(pid, SIGKILL)`. Log a `worker_exit`-style recovery event for each killed group. **The sweep is best-effort**: `worker_spawned` is a non-critical (advisory) event that may be lost within `fsync_interval_s` (§2.2), so a task whose spawn record never became durable has no kill target. The guarantee of convergence is generation fencing (§7.3), not the sweep: an orphan that survives the kill (or was never targeted) reads the bumped `.cambium/generation` before its next git operation or `state_ref` write and self-terminates with `exit reason="fatal"`.
4. `Surculus.prune()` (§7.5).
5. For each in-flight task: run `Surculus.recover()` (§7.5), then apply restart policy (§7.4) and respawn with `generation+1`, `resume_from_checkpoint` set per §2.4.
6. `Unio.reconcile()`: compare `refs/heads/main` to the latest `merge_committed` event; emit `merge_reconciled` if the ref advanced without a durable event (§7.8).
7. Reconstruct `result.json` from any durable `result` event whose file is missing (§2.2).
8. Re-publish the replayed stream to fresh `Session.events()` subscribers (§6.5).

---

## 3. Idempotency

| Action | Idempotent? | Mechanism | Arch |
|---|---|---|---|
| Event append (SQLite INSERT) | At-most-once by construction | Single-writer thread (§6.2 inv. 3); each frozen `Event` enqueued once (§3.6); `seq` AUTOINCREMENT assigned once; writer never re-inserts. Duplicate delivery to a *consumer* is prevented by a seq high-water mark. **[PROPOSED]** hardening: `UNIQUE` index on `(task_id, monotonic_ms, kind, generation)` so an accidental duplicate INSERT fails loudly instead of silently renumbering — *requires a change to the normative §6.3 schema* (open question, §7). Note `request_id` cannot be the dedup key (absent on heartbeats, §5.2). | §6.2, §6.3, §6.5 |
| Worktree create | Guarded, not naturally idempotent | `git worktree add` errors if the path exists — hence recover-before-respawn (§7.5) and retry-on-lock-contention + `gc.auto=0` (IMPL-M3 resolution). The resulting *state* is deterministically reconstructible: `reset --hard` to the same commit, `clean -fd`, and cherry-pick of the same `commits_so_far` are all repeatable. | §7.5, §18.3 IMPL-M3 |
| Merge publish (`publish_merge`) | Idempotent-by-guard | `git update-ref <ref> <new> <old>` is atomic and fails loudly if `old` no longer matches (double-apply or concurrent move → `NonFastForward`). Repeated/post-crash state is reconciled by `Unio.reconcile()` (compares `refs/heads/main` to latest `merge_committed`, emits `merge_reconciled`). Merges run in a throwaway worktree, so no partial merge state persists. | §7.8 |
| `result.json` write | Idempotent | Atomic temp+rename (§3.4, §16.4 inv. 1); reconstruction from the durable `result` event is repeatable. | §3.4, §16.4 |
| Fencing (`generation` bump) | Idempotent | Overwriting `worktree/.cambium/generation` with the max generation is monotonic and repeatable. | §7.3, §7.5 |

**Dedup on replay (event_id).** The durable identity of an event is `(task_id, monotonic_ms, kind, generation)` — `seq` is the ordering key. Downstream consumers that read `events.db` more than once (e.g., Ascensus ingestion, host-side archival) must track the last-consumed `seq` (or apply the proposed UNIQUE index) to avoid processing a row twice. The design's own subscribers need no dedup because on supervisor restart they are *fresh* (§6.5 re-publishes to "fresh subscribers"); a subscriber that predates the crash does not exist after the process died.

---

## 4. Torn-write handling

**Context:** no SQLite-WAL experiment doc exists in `docs/research/` on this branch (verified 2026-08-09; the only SQLite mentions in research are competitive-analysis notes in `codex.md` and `opencode.md`). The architecture chose SQLite WAL as the primary store (§6.1, resolving DS-M3). This section therefore specifies semantics for both the primary (SQLite WAL) and the optional mirror (JSONL append), and records the two options.

### 4.1 Option A — SQLite WAL (primary; §6.1)

- **Torn final line is impossible at the row level.** Every event is a single transaction (`BEGIN; INSERT; COMMIT`, §6.2 diagram); SQLite commits are atomic. A crash mid-transaction rolls the transaction back — the result is a **lost** event, never a corrupt row.
- **Torn WAL frames are handled by SQLite on open.** On power loss, the `-wal` file may end in a partially-written frame; SQLite's recovery validates frame checksums and discards the invalid tail. "Replay the WAL automatically" (§6.5) is exactly this. **No Cambium code is required for this.**
- **Durability point is explicit.** `PRAGMA synchronous=NORMAL` + timer/critical `PRAGMA wal_checkpoint(TRUNCATE)` + `os.fsync(wal_fd)` + `os.fsync(db_fd)` (§6.2 mechanism, §6.5). Under WAL+NORMAL the last committed transaction is crash-safe (may be lost on power loss, never corrupted). SIGKILL (no power loss) leaves the page cache intact, so uncheckpointed commits usually survive a process-only crash — but §2.1 does not rely on this.
- **Contract outcome:** critical events — zero loss window (§6.5); non-critical — at most `fsync_interval_s`. A torn/partial event manifests strictly as a missing tail, detected on restart by the gap-free `seq` invariant → `recovery_gap` event (§6.5).

### 4.2 Option B — JSONL append (optional mirror, default off; §6.1)

This is the v0.1 M3 surface (`open(path, "a")` + `write` + no fsync, system-design.md M4). As a **mirror** it inherits weaker guarantees; if it were ever the primary store these rules would govern:

- **Write path (mirror of the SQLite mechanism):** the same single writer thread appends a `\n`-terminated line per event and applies the same durability tiers: critical events `flush()` + `os.fsync(fd)` before ack; non-critical at the `fsync_interval_s` cadence. The mirror is lag-tolerant and rebuildable — SQLite remains authoritative.
- **Torn final line:** a crash mid-`write()` leaves a truncated final line (partial JSON). Because writes are strictly append-only, **only the final line can be torn** (this is an invariant that must hold; it is what makes the recovery rule sound). Replay policy: iterate lines; a JSON parse failure on the **final** line is treated as a torn write and dropped (it was never fsync-d, so never durable); a parse failure on any non-final line is corruption and must be surfaced (fail loudly / flagged by `cambium doctor`, §13). Emit a `recovery_gap`-style marker for the dropped tail.
- **Hardening options (from DS-M3's fix list, not adopted by v2):** per-line length prefix or trailing checksum would make torn-line detection exact. The architecture chose *not* to length-prefix the stdout protocol (§5.4 mode c) and uses SQLite for the log, so for the mirror we rely on the final-line rule. **[PROPOSED]** If the mirror is enabled, document that it is advisory.
- **fsync semantics for JSONL:** `flush()` alone reaches the page cache only; `os.fsync(fd)` is required for power-loss durability (DS-M3 point (a)). "Last line torn" and "last line lost but parses" are the two crash outcomes; both are non-durable by definition and safe to drop.

### 4.3 Protocol-level torn write (worker stdout)

Not a log issue, but part of the recovery story: a worker killed mid-`result`-write leaves a partial line (DS-C2 mode c). v2 mitigates by persisting the result to the checkpoint store *before* emitting; the supervisor recovers from checkpoints (§5.4 mode c). **Open gap [PROPOSED]:** §6.4 specifies the checkpoint file write as atomic (temp + `os.rename`) but does not require `fsync` of the temp file or the containing directory. On power loss the checkpoint event could be durable in the DB while the referenced `state_ref` file never persisted. The worker should `fsync` the checkpoint file before emitting the (critical) `checkpoint` event. **UNVERIFIED** — not specified in §6.4.

---

## 5. Recovery sequence diagram

Supervisor crash at `✂` with one task T in flight (checkpointed at turn 3), one task D already `result`-committed, and merge activity possibly in flight.

```
  SUPERVISOR (gen G)                WORKER T (gen G)             EVENT LOG / FS
  ─────────────────                 ─────────────────            ─────────────
 1 spawn T (generation=G) ─────────► init{generation:G, wt}
     │                                  │
 2   │◄────────────────── ready{pid:P}  │  worker_spawned{task=T,pid=P,gen=G}   [non-crit]
 3   │◄────────────────── checkpoint{state_ref:turn-3}   │ checkpoint(durable)   [critical, fsync]
     │                                  │ (worker mid-turn-4 tool call)
 4   ✂ SUPERVISOR SIGKILL — process dies; pipes close
     │                                  │
 5   (worker's next stdout.write → SIGPIPE/BrokenPipe; or continues if mid-LLM-call)
     │                                  │  ← ORPHANED (reparented to PID 1), still mutating wt
     │
     │   ─── time passes; host restarts Cambium for the same session ───
     │
 6 NEW SUPERVISOR starts
 7   open events.db (SQLite replays WAL automatically)             [§6.5]
 8   replay since last snapshot; rebuild per-task states:
        T → last durable event = checkpoint turn-3, no terminal → in-flight, resume pt=turn-3
        D → last durable event = result → DONE
 9   orphan sweep [best-effort]: for in-flight tasks, probe kill(P,0); if alive → killpg(P,SIGKILL)
10   Surculus.prune()
11   for T: Surculus.recover(wt, base_commit)                        [§7.5]
        - remove *.lock; abort rebase/merge; reset --hard base; clean -fd
        - write .cambium/generation = G+1
        - cherry-pick commits_so_far (turn-3) onto base_commit
12   Unio.reconcile(): refs/heads/main vs latest merge_committed → merge_reconciled  [§7.8]
13   reconstruct result.json for D from the durable result event      [§2.2]
14   respawn T: generation=G+1, resume_from_checkpoint=turn-3        [§6.4, §7.2]
15   (any orphan of gen G that survived step 9 reads .cambium/generation
     = G+1 before its next git op → exit reason="fatal" gen-mismatch) [§7.3]
```

Numbered step semantics:

1. **Spawn** — event log records `worker_spawned` (pid P, generation G). Non-critical tier (§6.5).
2. **Ready** — worker echoes generation G; establishes the pid/pgid identity (worker is process-group leader, §7.2).
3. **Checkpoint** — critical event, fsync-d before ack (§6.5); checkpoint file at `state_ref` written atomically (§6.4).
4. **Supervisor crash** — all pipes break (§1.1.1); in-flight log entries lost within the queue/fsync window (§1.1.3).
5. **Orphan** — worker either dies on next pipe write or survives as an orphan (§1.1.2).
6. **Restart** — new supervisor, same `${session_dir}` (§16.2).
7. **WAL auto-recovery** — no torn rows (§4.1).
8. **State rebuild** — last-durable-event table (§2.2).
9. **Orphan sweep** — event-log-derived pgid targets, OS probe, process-group kill; **best-effort** — lost `worker_spawned` events leave no target, so fencing (step 15) is the convergence guarantee (§2.3).
10. **Prune** — stale worktree admin entries (§7.5).
11. **Worktree recovery** — discard dirty, restore base + checkpoint commits, bump generation (§7.5). Resolves IMPL-M9.
12. **Merge reconcile** — closes the crash-window between `update-ref` and `merge_committed` (§7.8).
13. **result.json reconstruction** — idempotent (§2.2, §3).
14. **Resume** — fresh worker, checkpointed state, generation G+1 (§6.4).
15. **Fencing backstop** — any orphan that escaped step 9 self-terminates (§7.3).

---

## 6. Test scenarios

**Linking note:** there is no dedicated `test-strategy.md` in main as of this branch. Test-strategy guidance lives in `docs/architecture/module-template/architecture.md` §9 ("Test Strategy") and in the scenario-test pattern already used at `tests/scenarios/test_example_module.py` (real components, no mocks). **[PROPOSED]** the scenarios below become `tests/scenarios/test_crash_recovery.py` on the same pattern, plus a `cambium doctor` consistency check (§13) as the post-crash assertion oracle.

Fault-injection primitive: a test hook that kills the supervisor process (SIGKILL) at an event-loop checkpoint, then starts a new supervisor over the same `${session_dir}` and asserts on `events.db` + `worktrees/` + `checkpoints/` + `result.json`.

| # | Kill point | Precondition | Assert on restart |
|---|---|---|---|
| 1 | After `task_assigned`, before `worker_ready` | no checkpoint | Task reruns from scratch; exactly one `worker_spawned`-generation bump; no resume. Last durable event is `task_assigned` → PENDING path (§2.2). |
| 2 | After `checkpoint` turn-3, worker alive | checkpoint file turn-3 exists, `commits_so_far` present | Resume: new worker's `init.resume_from_checkpoint == turn-3`; worktree reset to base + cherry-picked commits (§2.4, §7.5). |
| 3 | SIGKILL supervisor while orphaned worker is still alive and mid-tool-call (pipes break but worker survives) | orphan mutating worktree | No split-brain: the best-effort sweep kills the orphan's `pgid` (step 9) **or** the orphan self-terminates on generation mismatch; the generation fence — not the sweep — is what guarantees at most one writer; worktree clean after recover (§2.3, §7.3). |
| 4 | After durable `result` event committed, before `result.json` rename | task D done | `result.json` reconstructed from the `result` event; task D is DONE and is **not** re-run; status file consistent (§2.2). |
| 5 | SIGKILL before `fsync_interval_s` elapses (non-critical tail in queue) | recent heartbeats un-fsync'd | `seq` gap detected → `recovery_gap` event; critical events (checkpoints/result) all present; no corrupt rows (§1.1.3, §4.1). |
| 6 | Kill between `update-ref` and `merge_committed` (Unio publish window) | main ref advanced | `Unio.reconcile()` emits `merge_reconciled`; `refs/heads/main ==` newest `merge_committed`/reconciled SHA; no re-merge (§7.8, §3). |
| 7 | Kill leaving a torn final line in the **JSONL mirror** (mirror enabled) | final line partial JSON | Replay: final-line drop, earlier lines intact, `recovery_gap` marker; primary DB authoritative (§4.2). |
| 8 | Kill supervisor (gen G), restart, then crash the **second** supervisor before its worker is ready | three instances in one session | Generations strictly monotonic (G, G+1, G+2); restart #2's sweep targets gen G's pgid; restart #3's sweep targets gen G+1's pgid; the sweep runs before the current instance spawns, so it never targets the current instance's own worker; final worker runs at gen G+2 (§2.3, §7.3). |

Cross-cutting assertions (each scenario): event log parses cleanly; `cambium doctor` reports no lock-file/ref misalignment (§13); exit codes follow §16.4 invariant 2.

---

## 7. Open questions

1. **`worker_spawned` payload must include `pid`.** The orphan sweep (§2.3) depends on it; §5.2's `ready` message carries pid, but §6.3's schema does not mandate pid in the `worker_spawned` payload. **[PROPOSED]** — requires a small, explicit addition to the §6.3 schema/§6.5 event definitions.
2. **Process-group kill at startup is referenced but unspecified.** §7.3 and §18.1 (DS-C6) mandate it; the concrete trigger/pid-source is not in the architecture. §2.6 proposes the minimal protocol. **UNVERIFIED** against the architecture.
3. **UNIQUE dedup index on `(task_id, monotonic_ms, kind, generation)`.** Hardening for consumer-side duplicate reads (§3); conflicts with the normative §6.3 schema as written — needs an explicit decision.
4. **Checkpoint file fsync.** §6.4 specifies atomic rename but not `fsync`; power loss can leave a durable `checkpoint` event referencing a lost file. Worker-side fsync before emitting the checkpoint event is recommended (§4.3). **UNVERIFIED** — not specified in §6.4.
5. **Worker-side pipe-break handling.** When the supervisor dies, a worker's `stdout.write` fails with SIGPIPE/BrokenPipeError; §5.3 covers the supervisor's side, not the worker's reaction. A worker-side rule ("on BrokenPipeError, exit code 0 and try to persist state_ref first") would make orphan cleanup more deterministic. **UNVERIFIED** — no worker-side specification in §5.
6. **Where does the first restart's supervisor get the *previous* supervisor's session ownership?** This design assumes the host re-invokes Cambium with the same `${session_dir}` (§16.2). Host-side restart orchestration (who calls it, with what grace) is outside `docs/architecture/architecture.md` and is the host's contract (§16.1). **UNVERIFIED** — no host-restart guidance exists in the reviewed docs.
7. **`result.json` reconstruction ownership.** §2.2 proposes the new supervisor reconstruct `result.json` from the `result` event; §3.4/§16.4 say it is written by `Session.run()`'s flow. Whether reconstruction is Custos's job or the host's is a boundary decision. **[PROPOSED]**.
8. **Un-mirror durability.** If the JSONL mirror is enabled, its durability contract (final-line rule, §4.2) must be documented as advisory; it must never be treated as the recovery source of truth. Decision deferred to the config-schema work.

## 8. Sources and verification

- `docs/architecture/architecture.md`: §0 (pid files dropped), §3.4/§3.6 (Result, Event), §5.2/§5.3/§5.4 (protocol, liveness, EOF modes), §6.1–§6.5 (store, writer, schema, checkpoint, durability), §7.1–§7.8 (state machine, spawn, fencing, restart policy, worktree recovery, per-tool heartbeat, shutdown, publish), §13 (`cambium doctor`), §16 (session dir, exit codes), §18.1 (DS-C5/C6/M3 resolution).
- `docs/architecture/reviews/review-distributed-systems.md`: C6 (orphan/split-brain), C5 (stale locks), M3 (no fsync / torn line), M2 (kill-on-dead-process race).
- `docs/architecture/system-design.md`: M4 decision notes 6–7 (rejected pid files, EOF=dead — superseded by v2 §5.3).
- No SQLite-WAL experiment doc exists under `docs/research/` on this branch (verified). §4 therefore specifies both store options and notes the tradeoff.

**UNVERIFIED items:** §2.6 step ordering and the §7.3 startup-kill trigger; §2.2 terminal-decision table details; §3 UNIQUE-index proposal; §4.3 checkpoint fsync; §7 open questions 5–7. All are flagged inline; none contradict an explicit architecture statement.
