# Cambium v2.1 Architecture Review and Roadmap

**Date:** 2026-08-09
**Review branch:** `wt-sol2` from `main@d67cd5e`
**Verdict:** v2 has credible deterministic components and a tested vertical slice, but no
coherent production harness. v2.1 must integrate and harden one runtime before adding optimizer
breadth.

**Historical snapshot / current pointer:** this review preserves branch-state evidence. The
review branch contained store/merge/IPC/worker/task-tree/doctor/slice source and 108 scenario
functions (29 task tree, 22 IPC, 19 dataset, 14 merge, 8 slice, 7 store, 6 module, 3 tooling).
Custos `wt-impl-super@9746b96`, Diffundo `wt-impl-diffundo@f5ae0d3`, bench
`wt-impl-bench@21257b3`, and redaction `wt-redact@1b449df` were branch-local, not ancestors;
audits were `wt-audit-security@6a137fb`, `wt-audit-conformance@30832d1`, and
`wt-audit-constitution@cb3dde2`. These counts/SHAs are not release certification.

For current behavior use `docs/architecture/architecture.md`, `src/cambium/`, and
`docs/research/v2-1-status.md`. Current notes: provider loop, Diffundo, EventStore, and root
`Result` exist; DLQ, eval cache, ResourceBudget, `worker_pool`, and `events` are absent; there is
no per-worker sandbox or production shell approval, and no dynamic hierarchy.

## 1. State assessment

### 1.1 Deterministic substrate delivered in the snapshot

1. **EventStore:** one writer, SQLite WAL, critical append ack after checkpoint/fsync, reader
   connections (`store.py:93–158,172–272`; `conformance-report.md` §§1 (Check 1), 2 (Store)). `recovery_gap` was folded out
   in favor of no-gap-by-construction plus phantom-read semantics (architecture §§6.3, 6.5).
2. **MergeSequencer:** throwaway staging worktree, reachable staging ref, quarantine refusal,
   ancestry/expected-old `update-ref` publish (`merge.py:158–177,281–482`; `conformance-report.md` §3).
3. **Nuntius/Opifex seed:** 1 MiB cap/resync, malformed-object skip/torn-tail handling,
   request correlation, heartbeat/cancel/health, result/exit ordering, 64 KiB diff and deadlines
   (`ipc.py:28–129`; `worker.py:66–93,219–317,356–494`; merged `38e1d43`).
4. **TaskTree validator:** unique IDs, one root, dependencies, no multi-parent/cycles, depth/
   width, deterministic order, subtree isolation, exact upward envelope (`tasktree.py:233–478`;
   29 scenario tests). It validates input; it is not Architectus execution.
5. **Doctor/datasets:** diagnostics and split train/eval/canary loaders (`doctor.py`,
   `modules/example/datasets/`, tooling tests).

### 1.2 Branch-local implementation evidence (not release proof)

6. Custos branch `wt-impl-super@9746b96` supervises workers, heartbeats/generations, recovery,
   gates, serialized merge, and store writes, but contains fallback store/sequencer classes and
   the old slice in one ~1,600-line file.
7. Diffundo `wt-impl-diffundo@f5ae0d3` has tier filtering, ordered cascade, breaker/buckets,
   pause/recovery, prefix lint, retries, and HTTP adapter; `call_race` conflicts with folded
   default and has no v2.1 requirement.
8. Bench `wt-impl-bench@21257b3` scores splits and gates drift; redaction `wt-redact@1b449df`
   handles credentials/headers/JWT/private keys and env construction. Value is unrealized until
   Custos applies redaction at every enqueue/log boundary. These branch states are historical;
   current DLQ/eval-cache/worker-pool status is the pointer above.

### 1.3 Audit findings

- **Security:** 22 findings (1 HIGH, 7 MEDIUM, 4 LOW, 6 PASS, 4 INFO); F-01 full env,
  F-02 absent redaction, F-04 worktree reuse, F-05 refs, F-06 writes/merge, F-07 queue, F-20
  runtime bypass. Approval and generation-file fencing are absent/UNVERIFIED (`security-audit.md` §§1, 3).
- **Conformance:** 5 MEDIUM + 8 LOW; IPC-file N-A later closed; surviving M2 production wiring,
  M4 orphan event model, M5 `worker_id`, and DDL/queue L1–L8 (`conformance-report.md` §3).
- **Constitution snapshot:** 11 COMPLIANT, 5 PARTIAL: bounds/enum/delete-over-add/module CLI/let-it-crash;
  historical events/orchestrator seed code and broad worker catch (`constitution-compliance.md` §1).

## 2. Release-blocking gaps

### P0 integration/security

1. **One canonical runtime is absent on `wt-sol2`.** Slice supervisor writes JSONL on the loop,
   inherits host env, and uses plain `git merge --ff-only` (`supervisor.py:1–24,67–87,181–217,321–372`): F-01/F-20 and M2. Branch-local Custos is not proof.
2. **Redaction is not wired.** Branch Custos emits raw stderr/errors/gate output/commands;
   F-02 closes only when the same versioned filter runs at enqueue and INSERT (architecture §§6.2,
   12.3; `redact.py` branch).
3. **Approval is design only.** D7 names `approve(session_id, op)` for external writes/network;
   Q7.2 leaves request, durable decision, denial, timeout, replay, and callback semantics open.
4. **Generation fencing is wire-only.** Worker echoes generation but does not read
   `.cambium/generation` before git/state writes; Custos bumps but does not write the file
   (`worker.py:356–383`; `wt-impl-super:929–950`; architecture §7.3).
5. **`max_turns` is transported, not enforced.** Branch Custos records heartbeat/checkpoint
   turns; no supervisor rejection at bound; no real ReAct source exists (UNVERIFIED).

### P0 liveness/resources

6. **Unbounded per-worker output queue:** cap decoded bytes and messages; kill on overflow (F-07).
7. **Unbounded stdin drain:** deadline every write and kill process group on expiry (F-06).
8. **DLQ absent:** bounded durable redacted metadata/reason for malformed, stale, unknown,
   out-of-order, and uncorrelated messages; never retry raw prompts (M2).
9. **Compile gates oversubscribe:** session `BuildResource` semaphore, default capacity 1,
   independent of worker width (`run_plan` TaskGroup; v2.1 P0 gap 9).
10. **Store waits unbounded:** queue backpressure, critical wait deadline, and checkpoint-busy
    handling (F-16, L5; `store.py:20–25,106,145–148`).

### P1 product gaps

11. **ConversationStore absent:** architecture specifies `${session_dir}/.cambium/sessions/conversations.db`, node-indexed queries (§6.6/16.2).
12. **Architectus skeleton:** no should-decompose→TaskTree→dependency scheduling→aggregation→
    steering/root evaluation (historical seed `orchestrator.py:1–59`; branch `wt-impl-super`).
13. **No real provider end-to-end evidence:** no provider→worker decision→edit→gate→Unio run;
    `CambiumLM`/DSPy integration is UNVERIFIED.
14. **No persistent cross-task pool:** measured cold 2.22 s/worker and 7.03 s/10 versus warm
    5.6 ms/38.9 ms; reusable subprocess pool is required before production fan-out
    (`worker-coldstart.md`; architecture §14).
15. **No DSPy refinement:** example is rule engine; pinned datasets/canaries/SIMBA are design,
    no optimizer run (`modules/example/decide.py`; architecture §17).

## 3. Architectural decisions

### A. Custos stays thin

Custos owns process/IPC lifecycle, hard budgets, fencing, permits, and durable emission; it does
not own workflow policy. Architectus validates/executes the DAG and chooses retries; GateRunner
owns command/resource/verdict; Unio owns stage/verify/final gate/lock/publish/events/reconcile.
The branch-local `_Runtime` mixes these concerns; “thin” means policy-poor, not incapable.

### B. Adopt FD 3

Use inherited FD 3 for protocol before the first DSPy worker; stdout/stderr become captured logs.
JSONL bytes remain unchanged (`ipc.py:48–129`). Update workers, fixtures, wrappers, tests and
Windows handle adapter once; do not negotiate stdout/FD 3 at runtime. Reject workers that do not
open FD 3. This is M2 and must be atomic.

### C. One conversations database

Use `${session_dir}/.cambium/sessions/conversations.db`, `node_id` on rows, indexes
`(node_id, turn_seq)` and `(node_id, kind, turn_seq)`. Events remain append-only durable facts;
conversation projection is rebuildable, with no cross-store atomicity promise. This resolves
the D8g separate-DB alternative for the architecture target.

### D. Pool required for production fan-out

Not required for first one-worker provider milestone, but mandatory for `max_width >= 4` or
ready p90 >10% wall SLO. Pre-spawn reusable subprocesses; never warm `os.fork` from threaded
Custos. Reuse only after clean cwd/env/Fds/processes/conversation/generation reset; retire on doubt.

### E. Cheap cascade default

Health-aware round-robin within requested tier; sequential fallback, no race mode. `REASONING`
is for final evaluation/approval by default. Capability filters precede rotation; explicit
strong policy is allowed, silent fast→reasoning escalation is not. Delete `Diffundo.call_race`.

### F. DSPy behind `decide.py`

DSPy belongs only behind a module `decide.py` and injected `LLMProvider` at the composition root;
Custos/GateRunner/Unio/stores/IPC/task-tree never import it. Use SIMBA first for
`should_decompose`; 200/50/canary data, pinned model, eval improvement, 100% canaries, and
rollback-by-refinement-ID are acceptance gates.

## 4. v2.1 roadmap and acceptance gates

Dependencies are hard gates; a later milestone is not accepted until predecessors pass.

| Milestone | Scope and must-prove criteria |
|---|---|
| **M1 — Canonical runtime (L)** | One supervisor/store/sequencer; remove slice/fallback/events/orchestrator paths; one fake worker edits, gates, atomic-publishes and emits fsynced `merge_committed`; full tests and three fresh audits on one SHA. |
| **M2 — Pipe hardening (M)** | FD 3; decoded-byte/message caps; write deadlines; redacted 1,000-row DLQ; fail-fast oversized/read errors; process-group kill. Flood stays under 16 MiB RSS delta; unknown/stale messages never retry. |
| **M3 — Security/fencing (M)** | Close F-01/F-02/F-04/F-05; strict worker/gate env; generation file rejects stale writes; durable approval IDs for external paths/network; invalid worktree/ref rejected before destructive git. |
| **M4 — Gate/resources/budgets (M)** | Compile semaphore default 1; content-addressed gate key (tree hash+command+base+input); reject turn `max_turns+1`; deadlines for all waits; checkpoint busy cannot ack. |
| **M5 — Architectus/tree/conversations (L)** | Three-level dependency-ready fixture; reject cyclic/multi-parent/depth/width plans; parent context only bounded own/summary/envelopes; query/rebuild `conversations.db`. |
| **M6 — First real LLM task (M)** | One key-gated provider edits fixture, passes gate, one FF publish, durable result; record model/usage without prompt/key/CoT; forced 429 fallback/pause; failed gate cannot publish. |
| **M7 — Worker pool (L)** | Warm p50 <100 ms/p90 <250 ms; no predecessor state leak; fault retires worker; pool on/off protocol equivalence; reject `max_width >= 4` without pool. |
| **M8 — DSPy refinement (M)** | Rename example to should_decompose; schema-versioned `Decision` enum; SIMBA beats frozen eval, 100% canaries, pinned model/refinement rollback; failed budget falsifies experiment. |
| **M9 — AST compression (research M)** | Paired ≥30-task/3-language trials; primary input tokens per compile-success; adopt only ≥25% median savings with ≤2-point compile-success decline and paired CI; otherwise keep text. |

## 5. Risks and stop list

Top risks: integration illusion; same-UID compromise without a kernel sandbox; Custos becoming
workflow engine; persistent-worker leakage; proxy-metric optimization. Mitigations are M1
single-SHA/audits, least-privilege env/redaction/approvals/fencing/host containers, A-boundary
ownership, M7 reset/retire census, and frozen eval/canaries/gates/rollback.

M1 deletes slice `EventLog`/merge/session/CLI, fallback stores/sequencer, seed `events.py`,
orchestrator submit/drain skeleton, and unsupported `Diffundo.call_race`; no compatibility
fallback may hide missing components. Keep `system-design.md` immutable, mark IPC/event drafts
historical after folding accepted changes, keep `fake_worker.py` as fixture only, and rename
`modules/example/` to `should_decompose` in M8. These are planned changes, not claims of current
implementation.

**Final posture:** v2.1 is the release that makes one runtime authoritative, executes trust/
liveness boundaries, and proves one real provider task through a gate and atomic merge. Architectus,
conversation storage, worker pool, DSPy refinement, and AST compression follow only after those
falsifiable gates pass.

## 6. Evidence and causal diagnosis retained

The branch discrepancy is itself the key finding. A green branch-local Custos test cannot certify
the review branch when `wt-impl-super`, `wt-impl-diffundo`, `wt-impl-bench`, and `wt-redact` are
not ancestors. That is why this review does not sum their 21/53/41/65 scenario counts. The
reviewed 108 functions cover deterministic modules, but there is no aggregate provider-to-merge
test. A post-M1 audit must use one SHA; N-A caused by branch state is not a pass.

The integration diagnosis is testable: `git grep` should find one EventStore, one sequencer, one
supervisor entry, and no fallback classes; a fake worker must emit a durable `merge_committed`
before a result; and a fresh security/conformance/constitution audit must not point at unmerged
modules. If those checks fail, the cause is duplicate runtime paths, not an individual store or
merge primitive. This is the distinction behind F-20 and conformance M2.

The resource diagnosis is likewise specific. A 1 MiB line cap bounds one frame but not a queue of
decoded frames; a message-count cap alone permits 1 MiB × N memory growth. M2 therefore requires
both 256 messages and 8 MiB decoded bytes, one `protocol_overflow`, and process-group death. The
stdin fix wraps every `drain()` in the active phase deadline. M4 adds compile semaphore capacity
1, a gate key of tree hash + command + base + input, and checks SQLite checkpoint `busy` before
critical ack. These acceptance criteria close F-06/F-07/F-16/L5 rather than hiding them behind a
retry.

The security boundary is intentionally explicit. Without a per-worker sandbox, same-UID workers
can read host state; D7 accepts that residual and relies on worktree isolation, allowlists,
least-privilege env, approval, and host-owned containers. Approval must be durable and replay-
safe: an external write/network operation receives an ID, deny/timeout fails closed, approval
resumes once, and replay never asks twice for the same `(generation, operation_digest)`. Until
that protocol and generation file exist, the review remains UNVERIFIED for real LLM use.

## 7. Milestone detail

### M1/M2: one path and one channel

M1 removes the slice and fallback implementations rather than adding a compatibility switch.
The fake-worker proof edits one file, runs a predeclared gate, publishes through expected-old
`update-ref`, writes `result.json`, leaves no worktree/process, and records the scenario count/
SHA. M2 then moves the protocol to FD 3, reserves stdout/stderr for logs, and updates fixtures
atomically. A worker that writes arbitrary stdout must not corrupt the FD-3 stream; a worker that
stops reading must die by the phase deadline. Unknown/out-of-order/stale messages become bounded
DLQ records with task, generation, request ID, digest, redacted preview, and reason.

### M3/M4: trust, gates, and budgets

M3 closes F-01/F-02/F-04/F-05 with injected-secret tests, stale-generation writes, ref/worktree
validation, and durable approval. M4 separates GateRunner from Custos, keeps compile-heavy work
under a resource permit, and enforces `max_turns`, tokens, process deadlines, and store waits at
the supervisor boundary. A worker heartbeat is not an LLM turn count; a real ReAct adapter must
be unable to issue call `max_turns + 1`.

### M5/M6: tree, provider, and root result

M5 exercises a three-level fixture: only dependency-ready nodes run, siblings never see raw
session transcripts, and root completion waits for all descendant envelopes/gates. Deleting the
conversation projection must still permit reconstruction from protocol events. M6 is manual and
key-gated: one OpenAI-compatible provider, one atomic coding task, one deterministic gate, and
one Unio publication. A forced 429 must fall through to another FAST provider; total exhaustion
pauses and resumes without worker restart. No prompt, key, or chain-of-thought enters durable
metadata.

### M7/M8/M9: scale and experiments

M7's pool acceptance is isolation, not just latency: no predecessor cwd/env/conversation/open FD,
subprocess, generation, or provider state; any doubt retires the worker. Pool-disabled and
enabled protocol/event semantics must match except PID/timing. M8 treats DSPy as a falsifiable
experiment: schema-versioned `Decision` enum, frozen eval/canary, pinned model, refinement ID,
and rollback; a candidate that misses any gate leaves the deterministic rule engine in place.
M9 compares raw versus AST context on paired tasks with the same provider/model/gate/budget and
adopts only with ≥25% input-token reduction and no more than two percentage points compile-success
loss. “Cleaner chunks” are not acceptance.

## 8. Historical anchors

The review's source references include the original audits (`30832d1`, `cb3dde2`), branch-local
module evidence (`9746b96`, `f5ae0d3`, `21257b3`, `1b449df`), and architecture fold commits
`39005fa`, `77f3d52`, and `c31e781` recorded by the conformance status update. They identify
evidence states only; no SHA in this document is a current-release certificate.

## 9. Later hierarchy feedback — skeptical classification

The later feedback is accepted as a refinement of D2/D8b: the harness owns an explicit
single-root DAG, validates it before dynamic worker admission, gives each child fresh declared
context, and permits only strict diff/summary/metrics/status envelopes upward. This is a target
boundary for M5, not evidence that the current branch has an agent tree. Claims that implicit
recursion is dead, explicit trees yield a 90% cache discount, “Prime 2026 proves it,” or five
cheap branches are **UNVERIFIED as broad claims**: primary audit evidence supports Prime explicit
AgentSession contexts and bounded depth, with descendants sharing one root worker, but not
process-per-child isolation or a 90% total-request/latency metric. Provider caches are
org/workspace scoped and can be shared by tasks with an exact prefix. AlphaCodium is staged
run/fix; LATS is candidate-solution MCTS with test/environment feedback, not universal
orchestration. Per-node gates/tests are required by M5; MCTS stays open until a falsifiable
comparison beats the explicit DAG baseline. Static prefix placement remains D8c guidance and
does not guarantee cache savings. Recursion evidence is task-dependent; no implicit-recursion
dead-end consensus is adopted.

## 10. Stop-list rationale

The M1 delete list is causal, not stylistic. The historical slice `EventLog`, `_merge_branch`, `run_session`,
CLI bootstrap, and duplicate helpers bypass the durable store and Unio (F-20). Fallback stores
and sequencers are more dangerous than an import failure because they silently weaken durability
and expected-old publication. The historical seed `events.py`/orchestrator submit-drain path has no caller
and disagrees with the canonical envelope; keeping it would preserve two event models. `call_race`
has no accepted v2.1 use case and cancellation/metered-provider behavior biases its result.

The stop-maintaining list is equally strict: `system-design.md` is immutable history; IPC/event
drafts become historical after accepted vocabulary is folded into architecture; point-in-time
audits are evidence, not release certification; `fake_worker.py` remains a fixture only; and the
example package is renamed only when M8's schema/eval gates are ready. No fallback or compatibility
path should be added to make an incomplete milestone appear green.

The later hierarchy feedback fits the roadmap only as M5 structure: an explicit parent-owned DAG,
fresh child context, strict upward envelopes, and static validation before admission. It does not
alter M7 pool economics or D8c cache guidance. Claims of a 90% discount, Prime 2026 proof, five
cheap branches, or universal AlphaCodium/LATS MCTS remain unverified and require a primary source
and paired metrics before they can change a milestone gate.

## 11. Measurement discipline for hierarchy and caching

The review accepts the architectural shape of explicit hierarchy but requires metrics before
calling it efficient. For a fixed task corpus, record node count, depth/width, child-context
bytes, envelope bytes, provider input/output tokens, cache-hit/read tokens, wall-clock, retries,
cost, gate pass rate, and root success. Compare static-DAG admission with a baseline using the
same provider/model, prompt content, and task budget. A provider cached-token rate near 0.1× input
pricing can lower input cost while leaving output, orchestration, and latency unchanged; it
cannot support a 90% total-request claim. Exact prefixes may be shared by tasks in an org/workspace
cache, so the metric must include cross-task hit behavior.

Prime's explicit `AgentSession` evidence supports fresh contexts and bounded depth, but descendants
share one root-session worker. That is a context boundary, not process isolation. LATS's
candidate-solution MCTS and AlphaCodium's staged run/fix flow are useful algorithm descriptions;
neither makes MCTS mandatory at every node. M5 should test strict envelopes and
static-DAG-before-admission first, then compare search strategies on task-dependent success/cost.
Recursion can be useful or harmful by task; no universal dead-end rule is assumed.

Static-DAG validation is also a resource boundary. Rejecting a malformed plan before admission
avoids spawning workers, spending provider tokens, or asking for approval on work that cannot
complete. Dynamic steering may change a node's context and retry content, but it cannot create a
new sibling or second root. A future M5 scenario should assert this invariant with a plan that
tries to mutate topology after admission, then check bounded failure evidence and no extra
workers.

The hierarchy target is implementable without choosing a search algorithm: M5 can validate a
static flat plan, admit ready nodes, compose bounded contexts, and aggregate strict envelopes
using deterministic code. M6 can then compare provider/cache behavior on one atomic task. This
ordering prevents a speculative 90% discount or universal MCTS claim from becoming a hidden
release dependency.

The primary-source correction is especially important for M7: Prime's shared root-session worker
does not prove a process-per-child isolation model, so pool reset/retire tests cannot be skipped.
Likewise, provider cached-token reads may be cheap while output and orchestration costs remain;
M6 must report total request cost and latency. These are independent acceptance axes, not one
headline cache number.

The milestone order is therefore deliberate: M5 proves graph/context/envelope structure with a
fake provider, M6 measures provider and cache behavior on one task, and M7 proves shared-worker
reset. A result that passes one axis cannot be reported as proof of the others.

The review's final claim is intentionally modest: explicit hierarchy and information hiding are
good structural targets, but efficiency and search strategy are empirical. A future status
refresh should cite the exact source commit, rerun static-DAG/envelope tests, and report
provider/cache metrics before changing this roadmap.

The same closure rule applies to each milestone: a branch-local source symbol, a historical test
count, or an adopted architecture sentence is evidence for planning only. Acceptance requires a
focused command, one baseline SHA, a reproducible result, and explicit handling of unresolved
security boundaries.

This evidence standard applies to later external critiques as well: retain the source claim and
date, classify the inference, and name the metric that could falsify it. It prevents a provider
pricing observation from becoming a cache contract, a context abstraction from becoming a process
isolation claim, or a search method from becoming a universal scheduler.

That reporting discipline is the release gate: one SHA, one caller path, one focused test, and a
clear status for every boundary. Until then, this review remains historical roadmap evidence.

The source pointer is the only current authority; historical SHAs and counts are anchors for
reproduction, not status labels.

This is why the final handoff reports commands and exit codes separately from historical findings.
