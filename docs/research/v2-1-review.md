# Cambium v2.1 Architecture Review and Roadmap

**Date:** 2026-08-09

**Review branch:** `wt-sol2` from `main@d67cd5e`

**Verdict:** v2 has credible deterministic components and a tested vertical slice. It is
not yet a coherent production harness. v2.1 must integrate and harden the components before
it adds optimizer breadth.

## Evidence and provenance

This review distinguishes the current review branch from implementation and audit branches.
That distinction matters:

- The current review branch contains the event store, merge sequencer, IPC/worker seed,
  task-tree helper, split datasets, doctor, and the original vertical-slice supervisor
  (`src/cambium/{store,merge,ipc,worker,tasktree,doctor,supervisor}.py`). It has **108
  scenario test functions**: task tree 29, IPC 22, dataset splits 19, merge 14, vertical
  slice 8, store 7, example module 6, tooling 3 (`tests/scenarios/test_*.py`, counted by
  `rg -c '^((async )?def test_)'`).
- Custos, Diffundo, bench, and redaction were inspected at `wt-impl-super@9746b96`,
  `wt-impl-diffundo@f5ae0d3`, `wt-impl-bench@21257b3`, and
  `wt-redact@1b449df`. Their branch-local scenario counts are respectively 21, 53, 41,
  and 65, but these overlap heavily and **must not be summed**. They are **UNVERIFIED on
  `wt-sol2`** because those commits are not ancestors of the review branch.
- The three requested audits are also not present on the review branch. They were inspected
  at `wt-audit-security@6a137fb`, `wt-audit-conformance@30832d1`, and
  `wt-audit-constitution@cb3dde2`. All audit `main@3d27ba3`, before IPC, task tree, split
  datasets, and later architecture folds. Their findings are valid point-in-time evidence,
  not a post-integration certification. There is **no aggregate full-stack test or audit
  result**. Any claim that “the full v2 stack is merged and certified” is therefore
  **UNVERIFIED** in this worktree.

That branch-state discrepancy is not clerical. It is the strongest current evidence for the
central diagnosis: Cambium has built modules faster than it has established one integrated,
audited release baseline.

## 1. State assessment

### 1.1 What v2 actually delivers

#### Deterministic substrate

1. **Durable event storage exists and has the correct core mechanics.** `EventStore` uses
   one writer thread, SQLite WAL, critical-event producer acknowledgement after checkpoint
   and fsync, and separate reader connections (`src/cambium/store.py:93-158,172-272`). The
   conformance audit calls fsync-before-ack and single-writer behavior conforming
   (`wt-audit-conformance@30832d1:docs/research/conformance-report.md` §1.2-§1.3). The
   folded architecture has already reconciled `recovery_gap` out of the contract in favor
   of no-gap-by-construction plus a phantom-read caveat
   (`docs/architecture/architecture.md` §6.3, §6.5).
2. **The merge primitive is substantially stronger than the slice.** `MergeSequencer`
   stages in a throwaway worktree, captures a reachable staging ref, rejects quarantine,
   verifies ancestry, and publishes with `git update-ref <new> <expected-old>`
   (`src/cambium/merge.py:158-177,281-344,346-482`). The conformance audit verifies every
   cited worktree-concurrency experiment and the atomic publish mechanics
   (`conformance-report.md` §3.1-§3.2). This is the right Unio core.
3. **Nuntius framing and an Opifex seed are real.** The reader enforces a 1 MiB line cap,
   resynchronizes after oversized lines, skips malformed/non-object JSON, and discards a
   torn tail at EOF (`src/cambium/ipc.py:28-129`). The worker implements init/ready,
   request correlation, heartbeats, cancellation, health, result/exit ordering, a 64 KiB
   diff cap, and idle/init deadlines (`src/cambium/worker.py:66-93,219-317,356-494`). This
   resolves the conformance audit's old N-A item M3 on the current branch, but not the
   end-to-end Custos integration that audit could not inspect.
4. **The Task Tree validator is real and pure.** It enforces unique IDs, one root,
   no unknown dependencies, no multi-parent nodes, cycle rejection, depth/fan-out bounds,
   deterministic topological order, subtree isolation, and an exact upward envelope
   (`src/cambium/tasktree.py:233-478`). The 29 scenario tests are the broadest current
   module test surface (`tests/scenarios/test_tasktree.py`). It is a validator and scheduler
   input, not Architectus execution.
5. **Doctor and dataset mechanics are useful release tooling.** Doctor validates Python,
   git, event-store integrity, datasets, worktrees, and secret-file hygiene
   (`src/cambium/doctor.py`, exercised by `tests/scenarios/test_tooling.py`). The reference
   module now has train/eval/canary splits and loader rules
   (`src/cambium/modules/example/datasets/`, `tests/scenarios/test_dataset_splits.py`).

#### Orchestration and provider modules available on implementation branches

6. **Custos has a substantive implementation, but it is not the reviewed baseline.** The
   branch implementation supervises multiple workers, tracks heartbeats and generation,
   restarts with jitter, recovers worktrees, gates results, serializes merge publication,
   and writes through the event store (`wt-impl-super@9746b96:src/cambium/supervisor.py`,
   `_Runtime`, `_drive_generation`, `_run_gate`, `_merge_task`, `run_plan`). It also embeds
   fallback event-store and sequencer implementations and retains the old slice in the same
   1,600-line file (`supervisor.py:1-394,455-698`). Those fallbacks undermine the claim
   that integration failures fail loudly.
7. **Diffundo is implemented as a router, not a response cache.** It has tier filtering,
   ordered cascade, per-provider circuit state, token buckets, pause/recovery monitoring,
   prompt-prefix linting, bounded retries, and an OpenAI-compatible HTTP adapter
   (`wt-impl-diffundo@f5ae0d3:src/cambium/diffundo.py:57-180,203-224,362-432,514-816`).
   Its `call_race` method remains even though the folded architecture removes race mode
   from the default design (`diffundo.py:434-512`; `docs/architecture/architecture.md`
   §9.2). That is optional surface without a v2.1 requirement.
8. **Bench and redaction are independently useful.** Bench discovers decision modules,
   scores all dataset splits, records test timing, and gates metric/canary/dataset drift
   (`wt-impl-bench@21257b3:src/cambium/bench.py:104-359,367-509`). Redaction handles
   known provider credentials, authorization headers, JWTs, private keys, secret-named
   fields, emails, recursive mappings, and worker env construction
   (`wt-redact@1b449df:src/cambium/redact.py:49-122,170-238,241-275`). Redaction's value is
   still unrealized until Custos applies it before every event enqueue and log write.

### 1.2 What the audits actually say

- **Security audit:** 22 findings: **1 HIGH, 7 MEDIUM, 4 LOW, 6 PASS, 4 INFO**
  (`security-audit.md` §7). HIGH F-01 is full host-environment inheritance. MEDIUM F-02
  is absent redaction, F-04 unsafe reuse of a registered merge worktree, F-05 unvalidated
  refs/refspecs, F-06 unbounded stdin writes and merge, F-07 unbounded worker-output queue,
  and F-20 runtime bypass of the hardened store/merge. The audit also flags approval gates
  and generation-file fencing as absent/UNVERIFIED (`security-audit.md` §6).
- **Conformance audit:** **5 MEDIUM and 8 LOW** gaps, with IPC/worker and end-to-end
  store-worker identity marked N-A at its baseline (`conformance-report.md` §7-§8).
  Current `main` has since resolved the IPC-file N-A and the architecture has resolved M1,
  L6-L8 at the specification level. The major surviving evidence is M2 (production wiring),
  M4 (orphaned event model), M5 (`worker_id` derivation), and store DDL/queue details.
- **Constitution audit:** **11 COMPLIANT, 5 PARTIAL, 0 VIOLATION**
  (`constitution-compliance.md` §1). The partials are bounded concurrency, enum use,
  delete-over-add, module CLI shape, and let-it-crash behavior. It specifically identifies
  `events.py`/the old orchestrator as dead-code drift and the broad worker catch as masking
  crashes (`constitution-compliance.md` §2(l), §7 Module shape, §8.3).

These verdicts support a narrow conclusion: module engineering quality is generally good;
release architecture and runtime integration are not yet good enough.

### 1.3 Gaps that block a v2.1 release

#### P0 integration and security gaps

1. **One canonical runtime does not exist on the review branch.** The current
   `src/cambium/supervisor.py` explicitly says it is “the slice, not Custos,” writes JSONL
   on the event loop, inherits the full environment, and merges with `git merge --ff-only`
   (`supervisor.py:1-24,67-87,181-217,321-372`). This is security F-01/F-20 and
   conformance M2 in executable form. Branch-local Custos is progress, not proof.
2. **Redaction is not wired into supervisor events.** The redaction module exists only on
   its branch; branch-local Custos emits raw stderr, worker errors, gate output, commands,
   and event payloads (`wt-impl-super:supervisor.py:795-823,1168-1175,1315-1325,
   1419-1425`). Security F-02 remains open until both enqueue and INSERT boundaries apply
   the same versioned redactor (`security-audit.md` F-02; architecture §6.2 invariant 6,
   §12.3).
3. **Approval gates are design text, not a protocol.** The folded architecture names
   `approve(session_id, op)` for external writes and non-allowlisted network egress
   (`architecture.md` §7.2), while D7 Q7.2 leaves the exact shape open
   (`docs/research/design-deltas.md` D7). Security F-18 and §6 explicitly say this control
   is absent. v2.1 must define request, durable decision, timeout, denial, replay, and host
   callback semantics.
4. **Generation fencing is only an in-memory/wire value.** Current worker echoes a
   generation but does not read `${worktree}/.cambium/generation` before git/state writes
   (`src/cambium/worker.py:356-383`; no generation-file access). Branch-local Custos bumps
   the number but `_recover_worktree_locked` does not write the fencing file
   (`wt-impl-super:supervisor.py:929-950`). This is the audit's UNVERIFIED generation
   finding and fails architecture §7.3.
5. **`max_turns` is transported, not deeply enforced.** Branch-local Custos puts it in
   init/run payloads (`wt-impl-super:supervisor.py:973-990,1182-1193`) but only records
   heartbeat/checkpoint turns; it does not reject a turn beyond the bound
   (`supervisor.py:1297-1308`). The current worker's heartbeat counter is not an LLM turn
   budget (`src/cambium/worker.py:219-241`). Architecture §7.4/§7.9 requires supervisor
   ownership, so worker self-report alone is insufficient. **UNVERIFIED:** no real ReAct
   turn source exists yet.

#### P0 liveness and resource gaps

6. **Per-worker pipe buffering is unbounded.** Both slice and branch-local Custos create
   unbounded asyncio queues for parsed worker output (`src/cambium/supervisor.py:219`;
   `wt-impl-super:supervisor.py:1140`). Security F-07 requires a per-worker cap and kill on
   overflow. The cap must cover queued decoded bytes, not only message count; a 1 MiB line
   cap permits a small-count memory attack.
7. **Supervisor-to-worker writes have no deadline.** `_write_json` awaits
   `proc.stdin.drain()` without a timeout (`src/cambium/supervisor.py:114-121`; branch-local
   code retains the helper). Security F-06 proves this can defeat ready and wall deadlines.
   Every write needs the active phase deadline; expiry kills the process group.
8. **No dead-letter queue exists.** Malformed, out-of-order, uncorrelated, stale-generation,
   and unknown messages are logged or ignored (`src/cambium/ipc.py:100-129`;
   `wt-impl-super:supervisor.py:1255-1330`). v2.1 needs a bounded, durable DLQ containing
   redacted envelope metadata and a reason code, never raw prompts/secrets. It is an audit
   aid, not a retry path.
9. **CPU-heavy gates can oversubscribe the host.** `run_plan` starts every task in one
   `TaskGroup` and each may run `make`, `cargo`, or compile-heavy `pytest` concurrently
   (`wt-impl-super:supervisor.py:1523-1557,1386-1426`). There is no resource semaphore.
   Add a session-level `BuildResource` semaphore with capacity default 1 for compile-heavy
   gates, independent of the worker-width limit.
10. **Store and critical waits need hard bounds.** The store queue is intentionally
    unbounded and a critical append waits forever (`src/cambium/store.py:20-25,106,
    145-148`). Security F-16 and conformance L5 also require bounded backpressure and
    checking SQLite checkpoint `busy` before acknowledging durability
    (`security-audit.md` F-16; `conformance-report.md` L5).

#### P1 product gaps

11. **There is no conversation store.** The architecture decisively specifies one shared
    `${session_dir}/.cambium/sessions/conversations.db` with `node_id`-keyed queries
    (`architecture.md` §6.6, §16.2), but no `ConversationStore` exists in `src/cambium/`.
12. **Architectus is still a skeleton.** Current `Orchestrator` only enqueues specs and
    emits start/finish placeholders (`src/cambium/orchestrator.py:1-59`). Branch-local
    `Orchestrator.run` forwards a prebuilt flat plan to `run_plan`; it does not call
    `should_decompose`, build/validate a Task Tree, schedule dependencies, aggregate child
    envelopes, steer children, or evaluate the root (`wt-impl-super:orchestrator.py:48-75`).
13. **No real LLM end-to-end run is evidenced.** Diffundo has an HTTP adapter, but no test
    connects provider → worker decision loop → worktree edit → gate → Unio publish.
    Architecture §9.3's `CambiumLM`/DSPy worker integration does not exist in the inspected
    code. This is **UNVERIFIED**, not delivered.
14. **No persistent cross-task pool exists.** The measured cost is 2.22 s per DSPy worker
    and 7.03 s for ten subprocess workers, versus 5.6 ms/38.9 ms for a warmed-fork
    experiment (`docs/research/worker-coldstart.md` §Per-operation measurements, §10-worker
    fan-out, §Conclusion). The benchmark explicitly rejects `os.fork` from Custos and
    recommends pre-spawned reusable subprocesses over the existing IPC
    (`worker-coldstart.md` lines 121-131).
15. **Decision optimization has not crossed the DSPy seam.** `ShouldDecomposeModule` is a
    deterministic rule engine (`src/cambium/modules/example/decide.py`); architecture §17
    defines pinned siblings, split datasets, canary gates, and SIMBA, but there is no DSPy
    implementation or optimizer run. The conformance and constitution audits could not
    check these orchestration/meta-layer norms.

## 2. Architectural verdicts on the open questions

### A. Custos stays a thin process watcher

**Decision:** Custos owns process lifecycle, IPC transport, hard budgets, generation fencing,
resource permits, and durable emission. It does **not** own workflow policy.

- **Architectus owns DAG execution:** call decision modules, validate the Task Tree, select
  ready nodes, route work, aggregate child envelopes, request gates/merges, and decide
  retries that change task content.
- **A deterministic `GateRunner` owns gate execution:** command launch, output cap, timeout,
  resource semaphore, and content-addressed verdict. Architectus requests it; Custos may
  terminate its process group when a hard session budget expires.
- **Unio owns the complete merge transaction:** stage, verify/final gate, lock, publish,
  durable `merge_committed`, and reconcile. Architectus requests a merge and responds to
  typed outcomes. Custos does not contain merge policy.

This corrects the branch-local `_Runtime`, which currently mixes watching, gate policy,
gate caching, worktree recovery, merge orchestration, event writing, and result aggregation
(`wt-impl-super:src/cambium/supervisor.py`, `_Runtime`). The deterministic layer remains
LLM-free; “thin” means policy-poor, not capability-poor (`architecture.md` §2 invariants).

### B. Adopt FD 3 now

**Decision:** v2.1 uses a dedicated protocol channel on FD 3. stdin remains supervisor
control input; stdout/stderr become ordinary captured logs. Do it before the first real DSPy
worker.

The wire schema and JSON-Lines framing do not change: the bytes from `ipc.write_message` and
`ipc.read_message` stay identical (`src/cambium/ipc.py:48-129`). The compatibility cost is
transport-level:

- worker launchers, fake workers, container wrappers, and tests must map an inherited pipe
  to FD 3;
- Windows needs an equivalent inherited handle adapter behind the same channel abstraction;
- existing v2 workers that speak protocol on stdout are incompatible unless run through an
  explicit test-only legacy adapter.

Do not negotiate between stdout and FD 3 at runtime. Negotiation itself depends on a channel
and preserves the contamination failure. Bump protocol transport version once, update all
in-repo workers atomically, and reject a worker that does not open FD 3. The cost is bounded
now; after real DSPy/LiteLLM dependencies emit progress and warnings, the cost and incident
risk increase. Architecture §5.1's stdout-reservation reshim is deleted after this change.

### C. Use one `conversations.db`

**Decision:** one SQLite WAL database at
`${session_dir}/.cambium/sessions/conversations.db`, with `node_id` on every row and indexes
for `(node_id, turn_seq)` and `(node_id, kind, turn_seq)`.

Do not create one database under each `sessions/<node_id>/`. A single database preserves one
writer discipline, allows bounded cross-node accounting and subtree queries, reduces file/WAL
churn, and matches the folded normative choice (`architecture.md` §6.6, §16.2). Conversation
rows are mutable-queryable session state; events remain append-only audit history. Cross-store
atomicity is not promised: the event is the durable fact, and conversation projection is
rebuildable from protocol events.

### D. A persistent subprocess pool is mandatory for production fan-out

**Decision:** the pool is not required for the first one-worker real-provider milestone. It
is a release gate before v2.1 claims production multi-worker operation.

Mandatory trigger: any configured session with `max_width >= 4`, or measured worker-ready
p90 above 10% of the task wall-time SLO. The existing 2.22 s per-worker/7.03 s ten-worker
measurement already trips that policy for short coding tasks (`worker-coldstart.md`
§Conclusion). Implement pre-spawned reusable subprocesses; never `os.fork` a threaded asyncio
supervisor (`worker-coldstart.md` lines 121-131). A worker returns to the pool only after a
verified reset: no child processes, no open task FDs, no provider/session state, fresh
generation, clean cwd/env, and empty conversation binding. Failure retires the process.

### E. Keep the cheap cascade default

**Decision:** eligible `FAST` providers use health-aware round-robin; cascade fall-through
stays within the requested tier. `REASONING` is reserved by default for the final
`ResultEvaluator` and approval/gate adjudication, not coding turns or routine decomposition.

This is a correction to fixed-priority concentration, not permission to race requests.
Round-robin chooses the first candidate; normal sequential fallback handles failure.
Capability/context filters still precede rotation (`architecture.md` §9.1-§9.2). Explicit
task policy can request `STRONG`, but there is no silent fast→reasoning escalation. Delete
`Diffundo.call_race`; it conflicts with the folded default and adds cancellation/cost surface
without a v2.1 acceptance case (`wt-impl-diffundo:diffundo.py:434-512`).

### F. DSPy stays in `decide.py`; use SIMBA first

**Decision:** DSPy imports and programs live strictly behind each decision module's
`decide.py` seam. Custos, GateRunner, Unio, stores, IPC, and task-tree validation never import
DSPy. Construction occurs at the Architectus composition root through an injected
`LLMProvider` port (`architecture.md` §2, §4 D8d, §17.3).

Use **SIMBA** for the first `should_decompose` optimizer. The module has 200 train, 50 eval,
and canary examples, a deterministic metric, and no sibling dependency
(`architecture.md` §17.2). SIMBA matches the architecture's planned refinement loop
(`architecture.md` §17.4). BootstrapFewShot only selects demonstrations and is too narrow
for replacing the current rule policy; MIPROv2 adds instruction/search cost before Cambium
has optimizer telemetry. Promotion requires eval improvement, 100% canaries, a pinned model,
and a recorded refinement ID. No optimizer code enters supervisor.py.

## 3. v2.1 roadmap

Dependencies are hard gates. A later milestone can be developed in parallel, but it cannot
be called accepted until its listed predecessors pass.

### M1 — Canonical runtime and audit baseline (**L**, no dependencies)

**Scope:** merge one Custos path with real `EventStore`, `MergeSequencer`, Nuntius, worker,
redactor, and doctor. Remove slice/fallback runtime paths. Re-run all three audits against
one SHA.

**Acceptance criteria:**

1. `git grep` finds one event-store implementation, one merge sequencer, and one supervisor
   entry path; no `_FallbackEventStore`, `_FallbackSequencer`, or slice `EventLog` remains.
2. One fake worker edits one file, passes its gate, publishes through `git update-ref`, emits
   fsynced `merge_committed`, writes `result.json`, and leaves no process/worktree.
3. The full scenario suite passes on Python 3.14; scenario count and commit SHA are recorded.
4. Fresh security, conformance, and constitution audits contain no N-A caused by unmerged
   modules.

### M2 — Protocol and pipe hardening (**M**, depends on M1)

**Scope:** FD-3 channel; per-worker decoded-byte and message caps; stdin write deadlines;
bounded redacted DLQ; fail-fast oversized/read errors; process-group kill.

**Acceptance criteria:**

1. A worker writing arbitrary stdout cannot corrupt protocol; valid FD-3 messages still
   complete the task.
2. A worker that stops reading control input is killed by the active phase deadline; test
   wall time is deadline + at most 1 s.
3. Output exceeding either 256 queued messages or 8 MiB decoded bytes causes one
   `protocol_overflow` event and process-group death; supervisor RSS stays below a fixed
   16 MiB delta in the flood test.
4. Unknown/out-of-order/stale-generation messages enter a 1,000-row bounded DLQ with reason,
   task, generation, request ID, digest, and redacted preview; they are never retried.

### M3 — Security boundary and fencing (**M**, depends on M1; precedes real LLM)

**Scope:** wire redaction at enqueue and INSERT; strict worker/gate env allowlists; ref-name
validation; sequencer-owned worktree markers; D7 approval protocol; generation fencing file.

**Acceptance criteria:**

1. Security audit F-01, F-02, F-04, and F-05 are closed by tests; injected secrets do not
   occur in events DB, DLQ, stderr logs, or gate output.
2. A stale worker whose generation file changes cannot perform its next git operation or
   checkpoint write and exits `fatal`.
3. External-path write and non-allowlisted network requests block on a durable approval ID;
   approve resumes once, deny fails, timeout denies, and replay never asks twice for the
   same `(generation, operation_digest)`.
4. An unknown registered worktree path and a branch containing a refspec are rejected before
   any destructive git command.

### M4 — Gate/resource hardening and deep budgets (**M**, depends on M1 and M3)

**Scope:** extract GateRunner; compile-heavy resource semaphore; full gate-verdict key;
`max_turns`, tokens, and all process deadlines; bounded store backpressure and SQLite busy
handling.

**Acceptance criteria:**

1. With ten workers requesting `make`/`cargo`/compile-heavy `pytest`, active compile gates
   never exceed configured capacity (default 1); ordinary non-compile checks can overlap.
2. Gate key is exactly worktree tree hash + command + base commit + gate input spec; changing
   any component reruns the gate, changing none reuses the verdict.
3. A heartbeat/checkpoint/tool/result reporting turn `max_turns + 1` is rejected by Custos;
   a real ReAct adapter cannot issue another LLM call after the budget closes.
4. Every subprocess communicate/drain/wait and critical-store wait has a testable deadline.
   SQLite checkpoint `busy` never produces a durability acknowledgement.

### M5 — Architectus RLM/task-tree execution and conversations (**L**, depends on M1, M3,
M4)

**Scope:** implement `should_decompose → TaskDecomposer → TaskTree validation → TaskRouter →
node dispatch → envelope aggregation → ResultEvaluator`; one shared `conversations.db`;
steering and recursive completion.

**Acceptance criteria:**

1. A three-level fixture runs only dependency-ready nodes, obeys session width/depth, and
   reaches root completion only after all descendant envelopes and gates succeed.
2. Cyclic, multi-parent, over-depth, and over-width plans dispatch zero workers and produce
   typed rejection evidence.
3. Parent LLM context contains own bounded turns + parent summary + child envelopes only;
   a canary scratchpad string in a child conversation never appears in parent context.
4. `conversations.db` answers `last_turns`, `cost_by_node`, and `context_for` with indexed
   query plans and reconstructs from durable protocol events after projection deletion.

### M6 — First real LLM end-to-end task (**M**, depends on M2-M5)

**Scope:** Diffundo plus one real OpenAI-compatible provider, `CambiumLM`, one atomic coding
task, one deterministic gate, and Unio publication. Keep it manual/key-gated, not default CI.

**Acceptance criteria:**

1. With one provider key, a worker receives a real completion, edits a fixture repo, passes
   a predeclared test gate, publishes exactly one fast-forward commit, and returns a durable
   result envelope.
2. Provider identity/model/usage/latency/cost metadata are recorded without prompt, key, or
   chain-of-thought content.
3. A forced 429 falls through to a second `FAST` provider; total exhaustion pauses and then
   resumes after recovery without worker restart.
4. The same task with a failing gate cannot publish to main. This is the first release
   evidence that joins LLM, Diffundo, Opifex, Custos, GateRunner, store, and Unio.

### M7 — Persistent worker pool (**L**, depends on M2-M6)

**Scope:** pre-spawn reusable subprocesses, pool admission/retirement, NodeSession bind/reset,
health and leak checks. No warm fork.

**Acceptance criteria:**

1. Ten DSPy-capable workers become task-ready at p50 <100 ms and p90 <250 ms after pool warmup;
   cold pool startup is reported separately.
2. Sequential tasks cannot observe the predecessor's cwd, env, conversation, open FDs,
   subprocesses, generation, or provider state; fault injection retires the contaminated
   worker.
3. Pool disabled and pool enabled produce byte-equivalent protocol/event semantics except
   worker PID and timing fields.
4. Production config rejects `max_width >= 4` when the pool is disabled unless an explicit
   development override is set.

### M8 — DSPy `should_decompose` refinement (**M**, depends on M5-M6)

**Scope:** DSPy strategy inside `modules/should_decompose/decide.py`, SIMBA optimizer,
enum/schema migration, bench/refinement artifacts.

**Acceptance criteria:**

1. Package is renamed from `example` to `should_decompose`; JSON CLI and eval CLI both pass.
2. `Decision` enum replaces the Python boolean boundary under a dataset schema-version bump;
   wire JSON remains explicit and versioned.
3. SIMBA candidate improves frozen eval over the rule baseline, passes 100% canaries, records
   train/eval/canary deltas and pinned model, and can roll back by refinement ID.
4. If no candidate meets all gates within the declared call/cost budget, the experiment is
   falsified and the rule engine remains production. “DSPy used” is not acceptance.

### M9 — Proposal 1: tree-sitter context compression (**M research**, depends on M6;
integrates only after M8 evidence)

**Scope:** compare raw text context with tree-sitter AST/symbol chunks for the same coding
tasks. The compressor is a context adapter, never a supervisor concern. Pin grammars and
fall back by explicit unsupported-language result, not silent text heuristics.

**Acceptance criteria / falsification metric:**

1. Freeze at least 30 tasks across three supported languages and the same provider/model,
   temperature, gate, and task budget. Run paired raw-context and AST-context trials.
2. Primary metric is **input tokens per compile-successful task**. Secondary metrics are
   compile-success rate, gate-pass rate, wall time, and changed-file recall.
3. Adopt only if median input tokens fall at least 25% while compile-success rate falls no
   more than 2 percentage points and its paired 95% confidence interval excludes a decline
   worse than 2 points.
4. If token savings miss 25% or compile-success degradation exceeds the bound, Proposal 1
   is falsified and tree-sitter stays out of the runtime. Do not ship it because chunks look
   cleaner.

## 4. Top five risks

1. **Integration illusion.** Branch-local green tests can hide a broken aggregate runtime.
   **Mitigation:** M1 creates one SHA, removes fallbacks, reruns all audits, and forbids N-A
   due to branch state.
2. **Same-UID compromise leaks credentials or mutates host state.** There is intentionally
   no sandbox (`architecture.md` §7.2, §19 honest gaps). **Mitigation:** least-privilege env,
   redaction, path/ref validation, approvals, generation fencing, and host-owned containers
   for hostile workloads. State clearly that these are containment controls, not a kernel
   boundary.
3. **Custos becomes the workflow engine.** Branch-local `_Runtime` already mixes process,
   gate, merge, recovery, events, and aggregation. **Mitigation:** enforce the A verdict:
   Custos watches/enforces, GateRunner executes, Unio merges, Architectus schedules.
4. **Persistent workers leak state across tasks.** The latency win creates a larger isolation
   lifetime. **Mitigation:** M7's reset proof, generation rebinding, FD/process census, and
   retire-on-any-doubt policy; never use warm fork from Custos.
5. **Optimization and compression improve proxy metrics while reducing coding success.**
   **Mitigation:** pinned model/siblings, frozen eval, 100% canaries, compile/gate floors,
   paired AST trials, cost budgets, and rollback-by-refinement-ID. A failed experiment keeps
   the deterministic baseline.

## 5. What to delete or stop

### Delete in M1

1. **Delete the slice runtime inside `src/cambium/supervisor.py`:** `EventLog`,
   `_merge_branch`, slice `run_session`, CLI bootstrap, and duplicate helpers. They bypass
   the store and Unio and preserve security F-01/F-20 (`supervisor.py:67-451`). Keep the
   behavioral scenario, pointed at canonical Custos.
2. **Delete `_FallbackEventStore` and `_FallbackSequencer` from branch-local Custos.** Missing
   architecture components must fail import/startup, not silently select weaker durability
   and merge semantics (`wt-impl-super:supervisor.py:455-698`).
3. **Delete `src/cambium/events.py` after consumers migrate to one typed canonical event
   envelope.** Its `type/timestamp` dataclasses disagree with the store's `kind/ts` envelope
   and only feed the skeleton orchestrator (`src/cambium/events.py`; conformance M4,
   constitution §2(l)). Do not keep two event models.
4. **Delete the compatibility submit/drain skeleton from `src/cambium/orchestrator.py`.** A
   no-op path that emits start/finish without work is more dangerous than a missing API
   (`orchestrator.py:20-59`). Replace it with Architectus, not another adapter.
5. **Delete `Diffundo.call_race` and its score/quality helpers.** v2.1 has no accepted race
   use case, and architecture §9.2 rejects it as a default because cancellation and metered
   requests bias behavior (`wt-impl-diffundo:diffundo.py:349-355,434-512`).

### Stop maintaining as live specification

- **Stop editing `docs/architecture/system-design.md`.** It is the superseded v0.1 origin
  record (`architecture.md` §20; `agents.md` §2). Keep it immutable for history.
- **Stop treating `docs/research/{ipc-protocol-draft,event-schema-draft}.md` as competing
  normative specs.** Fold accepted changes into architecture and module contracts, then mark
  the drafts historical. Multiple vocabularies (`result`/`result_envelope`, `exit`/
  `exit_message`, `done`/`succeeded`) already caused conformance L6.
- **Stop citing point-in-time audits as release certification.** Keep the three audit files
  as immutable evidence with their baseline SHA, and issue one post-M1 audit rather than
  editing old findings.
- **Keep `scripts/fake_worker.py`, but only as a test fixture.** It must not be a production
  worker choice or an alternate protocol authority.
- **Rename `src/cambium/modules/example/` in M8.** “Example” is no longer accurate; it is the
  production `should_decompose` module and its package name should match architecture §17.

## Final release posture

v2.1 is not “more modules.” It is the release that makes one runtime authoritative, makes
the trust and liveness boundaries executable, and proves one real provider task through a
gate and atomic merge. Architectus, the conversation store, and the worker pool follow that
foundation. DSPy optimization and AST compression remain experiments until their falsifiable
acceptance gates pass.
