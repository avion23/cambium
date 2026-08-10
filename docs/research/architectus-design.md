# Architectus — RLM Task-Tree Orchestrator (v2.1 M5)

**Historical snapshot — 2026-08-09.** **DRAFT** from branch `wt-doc-architectus`,
v2.1 M5, based on `main@6109a6a`; amendments also inspected `main@baeb9a0`,
`wt-impl-super@9746b96`, `wt-impl-diffundo@f5ae0d3`, and `wt-doc-difflag@16e61cf`.
Nothing here is merged behavior. Current authority is
[`docs/architecture/architecture.md`](../architecture/architecture.md), source/tests,
and [`v2-1-status.md`](v2-1-status.md).

**Current note (not retroactive):** active `supervisor.run_plan` is flat;
`task_decomposed` remains unsupported; provider cascade is source-defined and honors
`Retry-After`; worker stdout/event admission is bounded; no per-worker OS sandbox or
approval; DLQ and eval cache are absent. Historical DAG scheduling, blackboard,
steering, and decomposition below remain proposals.

The explicit-tree direction is part of this historical boundary: harness-owned
validation/admission runs on a static DAG before dynamic task admission; each child gets
a fresh bounded context; only the strict I2.7 upward envelope crosses the child boundary.
No implicit single-context recursion is proposed. Static-prefix/cache behavior requires
measurement and must not be described as a guaranteed discount.

## 0. Scope and provenance

Architectus is the proposed RLM/task-tree orchestrator: validate a plan, select ready
nodes, compose bounded contexts, steer workers, aggregate child results, and decide
replan/resolve/abort. Vocabulary is NodeSession (`session_id == task_id`), upward
envelope (I2.7), steer, wave, and deterministic gate (D4). Current merged code at the
snapshot had `tasktree.py` pure/ tested, a slice `supervisor.py`, an
`orchestrator.py` submit/drain skeleton, and `worker.py` steer hook; Custos,
conversations, and Diffundo implementation references were **UNVERIFIED** on main.
Critique-4 additions (blackboard, speculative reads, `include_diff`) were directive
provided; no critique-4 file exists.

## 1. Role split

**Custos (thin deterministic watcher):** process lifecycle, IPC transport, hard budgets,
generation fencing, resource permits, durable events, gates, restarts, worktree recovery,
and merge requests. It never calls an LLM or imports DSPy. Proposed primitives:

| Primitive | Historical contract |
|---|---|
| `spawn(node_session)` | admission before RUNNING (D3). |
| `cancel` / restart | SIGTERM→SIGKILL; graceful 10 s, term 5 s; generation bump; burst 5/60 s, absolute 10. |
| `gate` / `merge` | content-addressed GateRunner (D4); serialized Unio outcomes. |
| `emit` | redacted durable event; critical fsync before yield. |
| budgets | supervisor-owned `max_wall_s`, `max_turns`, `max_tokens`, `timeout_ms`, `gate_max_retries` (D4). |

**Architectus (orchestration layer):** plan/decompose via `should_decompose` (LLM-C6
fast path), build with `tasktree.build_tree` (I2.1–I2.3), schedule bounded waves,
compose context, steer, aggregate I2.7 `upward_result`, evaluate (arch §10), and
replan on typed outcomes. It never spawns/kills, writes worktrees, runs gates, or emits
durable events directly.

## 2. Wave scheduler proposal

The immutable tree is validated before dispatch; `ready_tasks` order is depth,
`width_idx`, then ID. Admission requires `in_flight < max_width`.

```text
PENDING → READY → SPAWNING → RUNNING → GATING → DONE
  ├→ REJECTED (dispatch validation)
  ├→ CRASHED → SPAWNING (bounded restart + generation)
  ├→ GATE_FAILED → RUNNING (retry) → FAILED
  └→ FAILED (nonrecoverable/budget); failed subtree is never dispatched
```

At each wave: dispatch ready nodes; await terminal envelopes; persist each node's
conversation; add successful I2.7 results; apply failure policy; merge shared proposals;
optionally call pure `decide(tree_state, events)` for replan actions. Root DONE requires
all descendants (I2.5). A dead-end marks only its subtree (`subtree_failed`); siblings
continue. `build_tree` rejects cycles, duplicate/multi-parent, unknown deps, depth/fanout
violations (DS-M6); no cycle can leave a node pending forever. Replay uses durable
`submitted`/`task_decomposed` links and deterministic wave order.

## 3. Context composition (I2.4, D8c)

The rule is **own bounded log + parent summary + subtree envelopes**; a node never reads
a sibling raw session. Static content is byte-stable at the top; dynamic content is the
tail. Static prefix: (1) root Core Directive, hard-capped `CORE_DIRECTIVE_MAX=200`
estimated tokens with `... [truncated]`; (2) role/system prompt; (3) AGENTS.md-derived
guidelines; (4) tool definitions; (5) module instructions. No timestamps, request IDs,
nonces, or volatile values enter the prefix (provider KV-cache/prompt-lint invariant).

Dynamic tail, in order: own `context_for(node_id)` turns (`init`, `steer`, `tool_event`,
`checkpoint`, `result`), parent summary ≤2k, each child's exact I2.7 keys
(`parent_task_id`, `unified_diff`, `diff_truncated`, `summary`, `metric_score`,
`metric_breakdown`, `commits`, `files_changed`, `status`), then relevant file names.
Token budget is supervisor-owned `init.budget.max_tokens` (D4). Fill static once, then
evict oldest own turns, older child envelopes, and file-list entries; log
`context_truncated`. Proposed ratios own 40/children 30/parent 10/files 10/reserve 10
are **UNVERIFIED**.

Compaction anchors `compaction-design.md`: worker-local between-turn summary, append-only
store node, claim refs, open-question/TODO canary, ≥60% reduction proposal, 100% canary,
no metric regression. `conversations.py` and worker checkpoint/context binding were
absent at the snapshot (**UNVERIFIED**).

### 3.1 Info hiding and critique-4 blackboard

`context_for` queries own node/subtree only; upward envelopes are validated against the
fixed key set and reject unknown `scratchpad`/CoT fields (D8b). For opt-in
`spec.cross_cutting=true`, a separate `.cambium/sessions/shared.db` (SQLite WAL) holds
global schema/interface/file-index facts. Architectus is its sole writer; Custos owns
`conversations.db`; workers read `_shared` and propose facts via `shared_update`, never
write the DB. Proposals are validated/redacted and merged at wave boundaries; same-key
conflict is last-arrival wins and recorded. Non-cross-cutting tasks are unchanged.
`shared.db`, flag, and event are new, **UNVERIFIED**.

### 3.2 `include_diff` (adopted-lite, payload level)

The normative note on `wt-doc-difflag@16e61cf` says `unified_diff` is default, capped
64 KiB; `include_diff:false` omits the field for higher tiers, while
`diff_truncated`/`files_changed` remain and merge-failed resolution can request the
diff. Absence is validated as an optional key, not an empty placeholder. Token savings
are structural (up to N×64 KiB), **UNVERIFIED**. This supersedes the earlier
context-only reading.

## 4. Pure module and LLM seam (D8a/D8d)

```text
OrchestrationInput = {tree_state, events}
OrchestrationOutput = {next_actions}
Action = Spawn | Steer | Resolve | Abort | Replan | Finalize
decide(input) -> output    # pure; I/O at wave loop
```

The thin CLI was proposed as `python -m cambium.modules.architectus` (JSON stdin/stdout,
diagnostics stderr). A deterministic rule engine is the default; an injected
`LLMProvider.call(prompt, tier, temperature)` (D8d) may interpret replan/conflict
choices. `ScriptedLLM` supplies deterministic tests; no provider/network in harness
tests. The deterministic layer never imports DSPy (decision F).

Speculative read Proposal 2: one model response may batch bounded `read_file` calls;
execute concurrently but return call order and emit per-call events/heartbeats. M6
falsifies on a three-file fixture: target ≥30% latency reduction; the headline 60% is
**UNVERIFIED**; failure retains sequential reads.

## 5. Protocol and event additions

Verified at the snapshot: arch `steer` existed (`worker.py:460-469` hook), result
envelopes, `init.parent_task_id`, supervisor-owned budget, and resume checkpoint. Gaps:
the IPC draft had no `steer`, and arch `steer.context` diverged from worker
`steer.payload` (worker ignored direction content).

Proposed `steer` request (valid after ready; additive, no proto bump):

```jsonc
{"type":"steer","request_id":"01J…","session_id":"wt-abc-001",
 "payload":{"turn":4,"kind":"direction|gate_retry","context":"<parent turn>"}}
```

Route parent→child only; sibling requests go through parent. New event-log kinds:
`child_result` (NC, accepted upward envelope), `subtree_failed` (C, failed root/reason/
children; prevents replay dispatch), and `replan` (NC, trigger/revision/added tasks).
`shared_update` is a separate worker event carrying proposed `facts` and
`changed_files`, NC; Architectus persists accepted proposals. All are draft additions,
not current kinds.

## 6. Failure policy (deterministic fallback)

| Event | Custos | Architectus |
|---|---|---|
| Crash / missing exit | bounded restart, jitter, generation | observe `restart_scheduled`. |
| Failed result with retries | gate/run again | steer evidence; stop at `gate_max_retries`. |
| First exhausted gate | reset base worktree | one `reset_retry`, rerun once. |
| Second exhausted/replayed failure | — | `subtree_failed` C; descendants stop. |
| Budget cap | kill/process failure | abort subtree. |
| `merge_failed:conflict` | — | resolver child/replan. |
| `merge_failed:test_failure` | — | one evidence steer, then abort. |
| `merge_failed:non_fast_forward` | — | reverify/remerge moved base. |
| Providers exhausted | queue pause/monitor (D8f) | pause dispatch, no worker restart loop. |
| Nonrecoverable config/fencing | fail immediately | no retry. |
| Cyclic/multi-parent/over-limit plan | no dispatch | typed reject and bounded re-prompt. |

Process failures are Custos; task/result policy is Architectus. Gate evidence must change
content; byte-identical retries are no-ops (D4). Accepted retry aliases are
`retries_remaining`, `retries_left`, `attempts_remaining`; one-shot reset state is
Architectus-owned, not an input marker.

## 7. Scenario canaries (14)

1 bounded wave/root completion; 2 reject cyclic/multi-parent/depth/width; 3 replan
`merge_failed`; 4 subtree abort/sibling survival; 5 gate steer→reset→abort; 6 crash
restart/generation; 7 static/dynamic context and prompt-lint; 8 scratchpad rejection;
9 dead-end propagation; 10 conversation queries/reconstruction; 11 deterministic wave
order; 12 steer routing/sibling isolation; 13 scripted-LLM replan; 14 root-directive
199/200/201-token boundary, reset replay, retry aliases. Scenarios 7–10 are M5 AC3/AC4.

## 8. M5 slicing and provenance

| Chunk | Scope |
|---|---|
| S1 | Architectus dataclasses, `Action`, pure core, D8a CLI/rule policy. |
| S2 | conversations.db, composition/token budget/prompt-lint, shared.db, `include_diff`. |
| S3 | wave scheduler, bounded admission, envelope/subtree aggregation. |
| S4 | steer wire/routing, shared_update, speculative reads. |
| S5 | child_result/subtree_failed/replan events and failure table. |
| S6 | ResultEvaluator, parent aggregation, root finalization, diff omission. |
| S7 | full loop with real worker/ScriptedLLM/gate/Unio and scenarios. |

Critical path S1→S2→S3→S5→S7. Hard predecessors M1 (canonical Custos), M3 (fencing,
redaction, approval), M4 (GateRunner/budgets). M7 persistent pool, M8 DSPy refinement,
and M9 tree-sitter compression were explicitly out of M5. Speculative reads falsify at
M6; blackboard/include_diff are M5 proposals. IDs retained: D2/D3/D4/D5/D7/D8a/D8b/
D8c/D8d/D8f/D8g, I2.1–I2.5/I2.7, DS-M6, LLM-C6, M1–M9, and Q3/Q8.

## 9. Verification record

Snapshot claims were checked against `v2-1-review.md` M5 AC1–AC4 and decisions A/C/D/F;
architecture §§3.4/3.7, 4, 5.2, 6.6, 7.1/7.4/16.2; `tasktree.py` build/topo/ready/
subtree/upward; IPC draft §§2.2/3/5; event draft catalog; design-deltas D2/D3;
feedback-2 D8a/b/c/d/g; and custos design. `feedback-4-assessment.md` #21,
`feedback-5-assessment.md`, and critique-4 source were respectively absent/directive
provided where noted. Canonical `run_plan` (`wt-impl-super@9746b96`), Diffundo
(`wt-impl-diffundo@f5ae0d3`), conversation store, worker steer consumption, shared.db,
shared_update, include_diff token savings, and ≥30%/60% read claims were **UNVERIFIED**
at the snapshot. `docs/research/compaction-design.md` merged via `b50ba71` but remained
DRAFT. This verification record is historical; re-check source/tests before adoption.

## Appendix A — detailed historical algorithm (retained for design review)

The scheduler's intended loop was deliberately explicit because the architecture's DAG
is a target, not proof of a scheduler. The pseudo-code below is the proposed ownership
boundary:

```python
tree = tasktree.build_tree(plan)                 # rejects before dispatch
finished, in_flight, subtree_failed = {}, {}, set()
while not tree.complete(finished):
    ready = tasktree.ready_tasks(tree, finished | set(in_flight))
    for node in ready[: max_width - len(in_flight)]:
        admission = custos.spawn(node.session)   # synchronous control-plane ack
        in_flight[node.id] = admission
    events = await custos.next_events()
    for envelope in events.terminal_envelopes:
        node_id = envelope.task_id
        store.append(node_id, envelope)
        if envelope.status == "succeeded":
            finished[node_id] = tasktree.upward_result(node, envelope)
        else:
            action = apply_failure_policy(node, envelope)
            if action == "abort_subtree":
                subtree_failed.update(tasktree.subtree_of(tree, node_id))
        in_flight.pop(node_id, None)
    architectus.merge_shared_updates()           # only at a wave boundary
    actions = decide(serialize(tree, finished), events)
    execute_actions(actions)                     # Custos API, never direct I/O
```

The root cannot finish when only its own envelope exists: I2.5 requires every descendant
envelope and gate. A failed node marks descendants dead but leaves sibling branches
eligible. `ready_tasks` receives `finished ∪ in_flight`, so dependency order is stable
even when events arrive in different process order. `topological_order` is a second,
dispatch-time assertion. Replay reconstructs the same ready waves from `submitted` and
`task_decomposed` payloads rather than from an in-memory queue.

### A.1 Context examples and eviction

For a node `task-3`, the static prefix was intended to be byte-identical across all
turns:

```text
CORE DIRECTIVE: update the parser without changing its public contract
SYSTEM ROLE: worker; allowed tools: read_file, write_file, git_op
AGENTS RULES: protocol stdout only; run the gate before declaring success
MODULE: TaskRouter decision instructions
```

The dynamic tail then appends the newest own turns, parent summary, child envelopes, and
path names. If the tail exceeds `max_tokens`, eviction is deterministic: oldest own
turns first, then the oldest child envelope, then path names. The root directive is never
evicted; a 201-token directive is truncated to 198 content tokens plus the two-token
marker `... [truncated]`, making 199/200/201 boundary tests meaningful. `request_id`,
timestamps, `generation`, and random cache-busters are forbidden in the prefix because
they defeat exact-prefix provider caching (D8c).

The upward envelope is intentionally smaller than this local context. The proposed
validator rejects any top-level key outside the fixed I2.7 set. A malicious child that
adds `scratchpad`, `chain_of_thought`, or raw trajectory therefore fails at the Nuntius/
Custos boundary before a parent context query can see it. This is structural information
hiding, not a prompt instruction.

### A.2 Blackboard mechanics and conflict evidence

`shared.db` was a separate WAL file to avoid two writers on `conversations.db`: Custos
writes every node transcript, while Architectus writes only accepted cross-cutting facts.
The proposed `shared_update` payload was:

```json
{"type":"shared_update","task_id":"wt-abc-001","generation":3,
 "payload":{"facts":{"schema.users.v2":"definition","api.v1.contract":"contract"},
            "changed_files":["src/schema.py"]}}
```

Workers could propose but never overwrite or delete a key. Architectus checked schema,
redacted values, and persisted proposals only at a wave boundary. Same-key proposals
used last-arrival-wins, with the winning and losing values recorded in the event trail.
Cross-cutting workers received `_shared` after the parent summary and before child
envelopes; `_shared` was last-evicted under pressure. Ordinary nodes did not read it,
so I2.4 sibling isolation remained the default.

`include_diff=false` had a similarly narrow boundary. At worker emit time, it removed
`unified_diff` from the validated upward payload for higher tiers. It did not replace it
with a placeholder and did not remove `diff_truncated` or `files_changed`. A
`merge_failed` resolver could request the quarantined diff on demand. This avoided a
common mistaken implementation that left a 64 KiB empty field in every parent prompt.

### A.3 LLM seam, fake provider, and speculative reads

`decide()` accepted serialized state and an event batch, returning typed `Spawn`, `Steer`,
`Resolve`, `Abort`, `Replan`, or `Finalize` actions. The rule table in §6 was the
production default, not an LLM requirement. The optional `LLMProvider` port was injected
at the composition root; the deterministic layer never imported DSPy. `ScriptedLLM`
responses were keyed by prompt digest or sequence index and could force each branch of
the failure table. This kept replan tests offline and made provider cost zero.

Proposal 2 allowed one model response to carry several `read_file` calls. The worker
executed them concurrently but appended observations in model-call order. Each call
still emitted its own tool event and heartbeat, and a single failed call did not fail
the batch. The M6 falsifier used the same three-file fixture for sequential and batched
time-to-first-token; ≥30% reduction was the adoption bar, while the 60% headline was not
evidence. If the bar failed, the sequential path remained the accepted alternative.

## Appendix B — failure evidence and canary detail

| Historical trigger | Evidence required before an action |
|---|---|
| Gate failure | Raw gate exit, bounded stdout/stderr, and content hash; identical retry is a no-op. |
| Merge conflict | `merge_failed` paths and moved base SHA; resolver receives affected diff. |
| Provider outage | `AllProvidersFailed` health/cost events; no worker crash/restart. |
| Budget exhaustion | Supervisor-owned counters; worker self-report is insufficient. |
| Plan rejection | Cycle/duplicate/multi-parent/depth/width error and zero dispatches. |
| Fencing violation | Expected/observed generation; fatal exit, no retry. |

The 14 scenarios assert typed events, not merely return codes. Scenario 11 compares two
identical scripted runs after removing wall timestamps; scenario 10 deletes the
conversation projection and rebuilds it from protocol events; scenario 14 replays the
same exhausted gate twice to prove one reset action and one subtree abort. These canaries
distinguish causal decisions from fallback behavior.

## Appendix C — milestone dependency record

S1 depended on the M5 base and could run beside S2/S4. S2 needed the conversation-store
gap (v2-1-review §1.3 gap 11), then S3 consumed S1/S2. S4 needed M2 IPC hardening; S5
needed M4 gate verdicts; S6 needed evaluator/envelope retention; S7 needed M1, M3, M4
and ran a real worker, ScriptedLLM, gate, and Unio. M7's persistent pool (multiple init
messages), M8 DSPy refinement, and M9 tree-sitter compression were explicitly excluded.
The proposal's predecessor gates were canonical Custos, generation/redaction/approval,
and GateRunner/deep budget enforcement. A merged implementation must re-check all of
these against source/tests. This appendix preserves historical acceptance intent only.

## Appendix D — full policy rows and boundaries

The compact table in §6 hides no fallback policy. The original row-by-row contract was:

| # | Event / evidence | Custos action | Architectus action |
|---:|---|---|---|
| 1 | Node crash, no `exit_message` or `reason="crash"` | restart under burst/absolute caps, jitter, generation bump | observe only. |
| 2 | Failed envelope with retries left | rerun gate/process | steer with failure evidence; decrement supervisor budget. |
| 3 | `GATE_FAILED` with retries left | content-addressed gate verdict | steer a gate-evidence turn. |
| 4 | First exhausted gate (`retries_remaining==0`) | reset worktree to base | emit one `reset_retry`; rerun task/gate once. |
| 5 | Second exhausted/replayed failure | no further process action | critical `subtree_failed`; stop descendants, inform parent. |
| 6 | `max_turns`, `max_tokens`, `timeout_ms` exceeded | kill process group; mark failed | abort subtree; no worker self-report can extend budget. |
| 7 | `merge_failed(reason="conflict")` | Unio leaves main unchanged | create resolver child under parent; requeue affected subtree. |
| 8 | `merge_failed(reason="test_failure")` | retain gate output | one evidence steer, then subtree failure. |
| 9 | `merge_failed(reason="non_fast_forward")` | expected-old-SHA rejects publish | reverify and remerge moved base. |
| 10 | all providers exhausted | pause queue and monitor health | park dispatch; never restart healthy workers. |
| 11 | `recoverable:false` config/tool error | fail immediately, no restart | fail node and preserve evidence. |
| 12 | cycle/multi-parent/depth/width invalid | dispatch zero workers | typed reject, bounded decomposer re-prompt. |
| 13 | generation mismatch | fatal exit, no side effect | fail node; no retry. |

The rule engine owns one-shot reset state. Input aliases
`retries_remaining`, `retries_left`, and `attempts_remaining` are accepted for replay
compatibility but cannot create extra budget. The persisted booleans
`reset_retry_attempted`, `reset_attempted`, and `step_back_attempted` are evidence, not
the state machine. A byte-identical retry is rejected as a no-op; every resolve action
must cite gate output, test output, or conflict paths. This preserves the coding
constitution's causal-chain rule and prevents retry loops hidden behind defaults.

## Appendix E — historical source audit anchors

The design's verification appendix intentionally distinguished facts from proposals:

| Anchor | Snapshot result |
|---|---|
| `v2-1-review.md` **M5 — Architectus RLM/task-tree execution and conversations** | M5 scope and AC1–AC4 read; AC4 requires indexed `last_turns`, `cost_by_node`, and `context_for` queries plus reconstruction from durable protocol events after projection deletion; target only. |
| Review decision A lines 215–233 | Thin Custos/Architectus split adopted. |
| Review decision C lines 257–268 | One conversations DB and per-node rows proposed. |
| Review decision D lines 270–282 | `max_width`/pool trigger recorded; pool deferred. |
| Review decision F lines 296–310 | DSPy stays behind `decide.py`. |
| `tasktree.py:233-478` | build/topological/ready/subtree/upward contracts read. |
| `ipc-protocol-draft.md` §§2.2/3/5 | steer gap and result/version proposals read. |
| `event-schema-draft.md` catalog | kind/tier mapping read; new kinds remain draft. |
| `costos-asyncio-design.md` §§1–2 | loop-affine WorkerHandle and writer handoff read. |

Still **UNVERIFIED** at the snapshot: canonical `run_plan` on main, `conversations.py`,
Diffundo, worker-side steer consumption, `shared.db`, `shared_update`, include-diff
measurement, and the critique-4 source. `feedback-4-assessment.md` was absent;
`feedback-5-assessment.md` was directive-provided for the Core Directive boundary. The
compaction document had merged via `b50ba71` but remained a DRAFT. Prefix-cache savings,
latency discounts, consensus/90% claims, MCTS, and universally cheap branching were not
part of this proposal because no primary measurement supported them. These anchors were
checked against source/tests; this appendix preserves historical acceptance intent only.

## Appendix F — admission, steering, and result contracts

Admission was a synchronous control-plane operation. Architectus first validated the
node's task spec, dependency set, budget, and parent link; Custos then returned an
admission acknowledgement before the node entered `RUNNING`. A failed admission was a
typed rejection and did not consume worker restart budget. The scheduler never dispatched
a descendant merely because its parent emitted `task_decomposed`; it waited for the next
validated static-tree wave. This distinction prevented dynamic child messages from
mutating the tree while a wave was being replayed.

Steering had two proposed kinds: `direction` (a parent focus change) and `gate_retry`
(bounded evidence from a failed gate). Both carried a parent turn and ≤2k context, were
routed by `session_id`, and were valid only after `ready`. A sibling could not steer a
sibling directly; it submitted a proposal to its parent. Custos transported the request,
but Architectus decided whether a steer was justified. The worker hook at
`worker.py:460-469` logged/continued at the snapshot, so real direction consumption was
explicitly **UNVERIFIED**.

The result evaluator received a worker envelope plus authoritative gate/merge verdict.
It scored status, commits, changed files, diff (unless `include_diff=false`), summary,
metric score/breakdown, and canary outcome. A worker's `status="succeeded"` did not
override a failed gate, merge conflict, timeout, cancellation, or rejection. Upward
serialization stripped all keys except I2.7; a parent received a result, never a
trajectory. A flat plan wrote an aggregate status record, not an invented synthetic root.

`merge_failed` had three distinct resolver inputs: conflict paths and moved base for a
conflict, raw gate/test evidence for test failure, and expected/observed SHAs for
non-fast-forward. The first could spawn a resolver child; the second could consume one
steer then abort; the third reverified and retried the merge on the new base. The table
did not authorize a generic “try again” fallback.

The proposed `child_result`, `subtree_failed`, and `replan` event kinds separated
bookkeeping from worker wire messages. `child_result` was reconstructible (NC),
`subtree_failed` was critical because replay must not redispatch descendants, and
`replan` was advisory but auditable. Their absence from the current event catalog was
left as an explicit draft reconciliation, not silently added to the source.

## Appendix G — current-boundary cross-check

The historical proposal is intentionally narrower than the old architecture diagrams.
It does not treat a module name as proof of a runtime: `tasktree.py` validates a DAG but
does not schedule it; `orchestrator.py` submits/drains but does not own a worker; and a
worker hook named `steer` does not prove direction content is consumed. A future
implementation must trace route registration, command tables, imports, and tests before
calling any of these seams active.

The static-DAG rule also limits dynamic decomposition. A worker may propose children only
through an explicit event; Architectus validates duplicate IDs, parent links,
dependencies, depth, and width before adding them to a new wave. The proposal rejects
implicit recursive calls that let a child allocate an unbounded sibling context. A child
receives a fresh context assembled from its own bounded store, a parent summary, and
strictly typed envelopes; parent code never receives raw child session text.

Prefix stability was a design constraint, not a performance result. The static prefix
could be reused by a provider's cache only if the provider actually exposes exact-prefix
reuse; token savings, latency, and any “discount” require a pinned model, dataset, and
measurement. No 90% savings, universally cheap branching, consensus, or mandatory MCTS
is implied by the task-tree shape. These claims were deliberately left outside the
historical acceptance criteria.

## Appendix H — task-tree acceptance record

The tree validator's historical acceptance record required: one root; unique task IDs;
known dependency IDs; no cycle; one parent per node; bounded depth and fan-out; valid
`width_idx`; and budgets present before admission. A plan failing any check returned a
typed rejection with the offending path and dispatched zero workers. `topological_order`
was used as a second assertion immediately before a wave, so a mutable or replayed plan
could not bypass the original validation.

The scheduler recorded `submitted` before spawn, `worker_started` before ready, and
`task_decomposed` only after Architectus validated child payloads. `child_result` was
written when an envelope entered a parent context; `subtree_failed` was critical because
replay must not redispatch descendants; `replan` recorded the trigger and plan revision.
These events were control evidence, not an implicit permission for a worker to mutate
the tree. A parent steer was queued only for a live child session and carried bounded
context; a dead child required a restart or new node decision.

The fresh-context rule applied to every node, including a resolver spawned for a merge
conflict. The resolver saw parent summary, conflict paths, and the requested diff, not
the failed sibling's raw history. A cross-cutting `_shared` proposal was the sole
exception, and it was an orchestrator-written facts segment, not a sibling transcript.
This boundary made information hiding testable and avoided romanticizing implicit
single-context recursion.

The proposed node lifecycle also carried explicit admission acknowledgements and
generation tokens. `SPAWNING` did not mean process started; it meant Custos accepted the
request and had reserved a worktree/budget. `RUNNING` began only after `ready` for the
current generation. `GATING` was a supervisor verdict phase and could not be skipped by a
worker result. `DONE` required both terminal result and gate/merge success. `REJECTED`
was a plan/config verdict, while `FAILED` represented exhausted recovery or a
nonrecoverable error. This vocabulary kept replay and event tiers unambiguous.

Wave-level invariants were equally strict: no dependency-ready node was dispatched twice;
no failed subtree re-entered `ready_tasks`; a parent did not finalize before all child
envelopes; and a provider pause did not consume process restart budget. A dynamic
decomposition event could add work only after validation produced a new immutable tree
revision. These were proposed checks for static DAG scheduling, not claims that current
`run_plan` implements dynamic decomposition.

## Appendix I — decision-module seam and policy ownership

The proposed pure core accepted a tree, finished-envelope map, in-flight map, and typed
events, then returned actions such as `admit`, `steer`, `gate`, `merge`, `restart`,
`replan`, or `abort`. It did not open pipes, call a provider, mutate a worktree, or
write SQLite. A fake-LLM port could return a deterministic decision record for tests;
the supervisor still enforced hard wall, turn, token, line, and restart budgets. This
seam was the historical response to the review finding that an orchestration LLM must
not become the deterministic layer.

The policy table distinguished failures that changed task content from failures that
Custos could recover without consultation. A worker crash with remaining restart budget
was a Custos action. A provider outage stayed in worker patience. A failed gate could
consume a bounded `gate_retry` steer, then required an Architectus decision. A merge
conflict could create a resolver child with conflict paths and expected SHAs; a
non-fast-forward first reconciled refs. A budget violation, malformed tree, unknown
envelope key, or stale generation was a typed abort/rejection, not a generic retry.

Cross-cutting blackboard writes were validated at wave boundaries by the orchestrator's
single writer. Workers proposed facts with `shared_update`; they could not edit shared
state or read sibling transcripts. The `include_diff=false` option removed raw diff from
the upward envelope while retaining bounded `files_changed` and `diff_truncated`; a
resolver could request diff evidence through a controlled action. These were adopted
historical additions, not current source capabilities.

The scheduler's static-prefix idea was deliberately modest. A stable prefix could help a
provider cache only if the exact provider/model exposed prefix reuse. The design required
measuring serialized bytes, cache-hit metadata, latency, and cost on a pinned corpus;
it did not claim a percentage saving or universal branching benefit. No MCTS gate was
required: a decision module could use tests or deterministic rules, and any search policy
was a separate proposal with its own evidence.

## Appendix J — historical review anchors

The role split was anchored to Verdict A of `v2-1-review.md`: Custos owned lifecycle,
IPC, hard budgets, generation fencing, permits, durable events, gates, restarts,
worktree recovery, and merge requests; Architectus owned decomposition, ready waves,
context composition, steering, aggregation, evaluation, and replan/abort policy. The
proposal retained source references to `tasktree.py` validation and ordering,
`worker.py` steering hooks, and the thin `orchestrator.py` submit/drain skeleton so a
future audit can distinguish a named module from an active route. These references are
historical evidence, not a claim that the current flat runtime implements the split.

The proposal's acceptance evidence was intentionally layered: pure `tasktree` tests,
fake-Custos action tests, fake-LLM decision tests, then process-boundary scenarios. A
passing pure scheduler test could not establish worker admission, and a worker result
could not establish gate or merge success. This layering preserved the historical IDs
without turning design seams into current routes.

The historical proposal retained bounded `summary`, `files_changed`, `diff_truncated`,
and optional `unified_diff` fields so result evaluators had evidence without receiving a
trajectory. Raw child turns stayed node-local.

The proposed context composer also separated static and dynamic portions. Static
instructions were byte-stable and policy-only; dynamic content carried the node spec,
parent summary, bounded envelopes, and current gate evidence. Timestamps, request IDs,
generation values, and volatile paths stayed out of the static prefix. This was a
measurement-friendly cache boundary, not a claim that providers reuse it.

The parent received only strict envelopes and bounded summaries.

The scheduler retained immutable tree revisions. A proposal could not alter dependencies
or width in place; validation produced a new revision before admission. This prevented
replay from dispatching a node that was not ready in the recorded plan.

This was proposal evidence, not current scheduler status.

Current truth remains architecture, source, and tests.

No current consensus is implied.

Historical alternatives stay labeled.

Nothing here supersedes architecture.

Source remains normative.

Historical only.

Current.

Historical review identifiers retained: `C2`, `D8`, and `I2.2`.
