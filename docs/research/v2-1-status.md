# Cambium v2.1 milestone status tracker

> **Living status document.** Agents MUST update this file when a milestone lands,
> is blocked, or changes evidence. Do not treat a branch-local green test as a
> merged milestone.
>
> **Baseline:** `main@63a0110` on 2026-08-10. A commit counts as merged only when
> it is reachable from that baseline. Branch names and branch-local SHAs below
> are in-flight evidence only.

Note: the Feedback-7 accepted-action register (`F7-01..F7-09`) was appended to
`implementation-plan.md` (repo root) and is on this baseline via `cb8c434`.

## Status summary

| Scope | Definition | STATUS | Evidence at `main@63a0110` |
|---|---|---|---|
| **v2 base** | The complete v2 stack is one authoritative, integrated runtime from deterministic substrate through provider execution and release evidence. | **IN-FLIGHT** | The deterministic substrate and Custos seed are merged, but the canonical runtime still has slice/fallback paths and no real provider-to-merge run is evidenced. |
| **M1** | Canonical runtime and audit baseline. | **IN-FLIGHT** | Store, merge, IPC, worker, task-tree, doctor, and a Custos seed are merged; the M1 deletion set still sits on main and the M1 executor is in-flight unmerged, so canonicalization and post-integration audits are not complete. |
| **M2** | Protocol and pipe hardening. | **IN-FLIGHT** | IPC/worker and DLQ parts are merged, and write deadlines are now in the supervisor; FD 3, bounded runtime queues, and overflow death are not integrated. |
| **M3** | Security boundary and fencing. | **IN-FLIGHT** | Approval, fencing, redaction, and env-keyed provider modules are merged; strict spawn policy, durable approval, ref validation, and runtime fencing are incomplete. |
| **M4** | Gate, resource, and deep-budget hardening. | **IN-FLIGHT** | Resource and health helpers are merged; GateRunner, budget enforcement, and bounded store durability are not wired. |
| **M5** | Architectus task-tree execution and conversations. | **IN-FLIGHT** | Conversation storage, Architectus design, and an Architectus execution core are merged; the core is not wired into the supervisor and the orchestrator remains a skeleton. |
| **M6** | First real LLM end-to-end task. | **IN-FLIGHT** | Provider configuration, Diffundo routing, and CambiumLM are merged, and provider staging is real; the real-provider provider-to-gate-to-Unio proof is not complete. |
| **M7** | Persistent worker pool. | **BLOCKED** | A worker-pool state-machine seed is merged (explicitly not M7 acceptance); acceptance is gated by incomplete M2-M6. |
| **M8** | DSPy `should_decompose` refinement. | **IN-FLIGHT** | Bench, eval-cache, and the `Decision` enum migration are merged; package rename and SIMBA evidence are absent. |
| **M9** | Tree-sitter context-compression research and falsifiable adoption trial. | **DONE** for the research deliverable | The tree-sitter feasibility/prototype report is merged. Runtime adoption remains explicitly deferred because the compile-success trial is unverified. |

## v2 base — whole v2 stack

- **Definition:** One authoritative v2 stack must connect the deterministic substrate, orchestration, provider path, gates, atomic merge, and release evidence.
- **STATUS: IN-FLIGHT**
- **Evidence:** `3d27ba3` merged `EventStore`; `c7e19b0` merged `MergeSequencer`; `38e1d43` merged Nuntius/Opifex IPC and worker; `06ce0dc` merged Task Tree validation; `2822139` merged doctor; and `0867572` merged the Custos multi-worker seed. Later v2.1 parts are also present through `63a0110`.
- **Blocker:** `src/cambium/supervisor.py` still contains the old slice `EventLog`, `_FallbackEventStore`, and `_FallbackSequencer`. The review's full-stack warning still applies: there is no aggregate release test or post-integration audit proving the whole stack.

## M1 — Canonical runtime and audit baseline

- **Definition:** Merge one Custos path with real `EventStore`, `MergeSequencer`, Nuntius, worker, redactor, and doctor; remove slice/fallback runtime paths; rerun all three audits against one SHA.
- **Acceptance criteria (review §3, M1):**
  1. `git grep` finds one event-store implementation, one merge sequencer, and one supervisor entry path; no `_FallbackEventStore`, `_FallbackSequencer`, or slice `EventLog` remains.
  2. One fake worker edits one file, passes its gate, publishes through `git update-ref`, emits fsynced `merge_committed`, writes `result.json`, and leaves no process/worktree.
  3. The full scenario suite passes on Python 3.14; scenario count and commit SHA are recorded.
  4. Fresh security, conformance, and constitution audits contain no N-A caused by unmerged modules.
- **STATUS: IN-FLIGHT**
- **Evidence:** The M1 substrate is merged in `3d27ba3` (`store`), `c7e19b0` (`merge`), `38e1d43` (`ipc`/`worker`), `06ce0dc` (`tasktree`), and `2822139` (`doctor`). Custos is present through `0867572`; the pipeline proof was merged by `c219edd`. The redactor is merged in `39005fa` (`src/cambium/redact.py` and `tests/scenarios/test_redact.py`) but is not wired into the supervisor: `_redacted_provider_metadata` only scrubs `provider_metadata` in the event payload, with no enqueue/INSERT redaction. The current supervisor still exposes `EventLog`, both fallback classes, and the slice `run_session`, so the M1 deletion set remains on main. The M1 executor is in-flight on `wt-m1-executor` (tip `6244edd`) and unmerged; it carries the canonicalization and `events.py`-deletion work. `docs/research/m1-canonicalization-plan.md` records the canonicalization steps and explicitly marks execution of those steps as unverified. No fresh three-audit result exists for this baseline.

## M2 — Protocol and pipe hardening

- **Definition:** Add the FD-3 channel, per-worker decoded-byte and message caps, stdin write deadlines, a bounded redacted DLQ, fail-fast read handling, and process-group kill.
- **Acceptance criteria (review §3, M2):**
  1. A worker writing arbitrary stdout cannot corrupt protocol; valid FD-3 messages still complete the task.
  2. A worker that stops reading control input is killed by the active phase deadline; test wall time is deadline + at most 1 s.
  3. Output exceeding either 256 queued messages or 8 MiB decoded bytes causes one `protocol_overflow` event and process-group death; supervisor RSS stays below a fixed 16 MiB delta in the flood test.
  4. Unknown/out-of-order/stale-generation messages enter a 1,000-row bounded DLQ with reason, task, generation, request ID, digest, and redacted preview; they are never retried.
- **STATUS: IN-FLIGHT**
- **Evidence:** Nuntius/Opifex framing is merged in `38e1d43`; the pipe integration test is in `c219edd`/`8779d28`; and the durable DLQ is merged in `be8261b`/`ef15a95` from `e4cd771`. Write deadlines are now present in the supervisor: `_stdin_deadline` (`supervisor.py:86-88`) and `await asyncio.wait_for(proc.stdin.drain(), remaining)` at `supervisor.py:172` (from `b709375`). Still open: the supervisor reads stdout into an unbounded `asyncio.Queue` (`supervisor.py:448`), no FD-3 transport exists (`pass_fds=()` at every spawn), and the DLQ is not connected to those runtime paths; no overflow-event proof is on the baseline.

## M3 — Security boundary and fencing

- **Definition:** Wire redaction at enqueue and INSERT, strict worker/gate environment allowlists, ref-name validation, sequencer-owned worktree markers, the D7 approval protocol, and the generation fencing file.
- **Acceptance criteria (review §3, M3):**
  1. Security audit F-01, F-02, F-04, and F-05 are closed by tests; injected secrets do not occur in events DB, DLQ, stderr logs, or gate output.
  2. A stale worker whose generation file changes cannot perform its next git operation or checkpoint write and exits `fatal`.
  3. External-path write and non-allowlisted network requests block on a durable approval ID; approve resumes once, deny fails, timeout denies, and replay never asks twice for the same `(generation, operation_digest)`.
  4. An unknown registered worktree path and a branch containing a refspec are rejected before any destructive git command.
- **STATUS: IN-FLIGHT**
- **Evidence:** The generation-file helper was merged in `407ce7c`/`bc77e5c` from `5d91ec9`; the fail-closed approval helper was merged in `0903d66` from `bcf5014`; env-keyed provider configuration was merged in `228a4e1` from `edd0e60`; and the deterministic redactor was merged in `39005fa`. `src/cambium/fencing.py`, `src/cambium/approval.py`, and `src/cambium/redact.py` are standalone modules, but the current supervisor does not enforce the fencing/approval protocol at worker git/checkpoint boundaries and only uses `redact.py` for `provider_metadata` scrubbing. The approval helper has no durable approval ID/replay protocol, and strict spawn-time environment allowlisting is not complete — `_worker_environment` (`supervisor.py:824-826`) forwards every `CAMBIUM_PROVIDER_*_API_KEY` env var regardless of `provider_env_keys`.

## M4 — Gate/resource hardening and deep budgets

- **Definition:** Extract GateRunner, add the compile-heavy resource semaphore, use a full gate-verdict key, enforce `max_turns`/tokens/process deadlines, and bound store backpressure with SQLite busy handling.
- **Acceptance criteria (review §3, M4):**
  1. With ten workers requesting `make`/`cargo`/compile-heavy `pytest`, active compile gates never exceed configured capacity (default 1); ordinary non-compile checks can overlap.
  2. Gate key is exactly worktree tree hash + command + base commit + gate input spec; changing any component reruns the gate, changing none reuses the verdict.
  3. A heartbeat/checkpoint/tool/result reporting turn `max_turns + 1` is rejected by Custos; a real ReAct adapter cannot issue another LLM call after the budget closes.
  4. Every subprocess communicate/drain/wait and critical-store wait has a testable deadline. SQLite checkpoint `busy` never produces a durability acknowledgement.
- **STATUS: IN-FLIGHT**
- **Evidence:** The compile semaphore and heavy-operation budget were merged in `c8f9b08`/`a0a9652` from `d5f362b`; host-health probing was merged in `4966f4a` from `d4db2ff`; and the base durable store is `3d27ba3`. `src/cambium/resources.py` provides `CompileGate` and `ResourceBudget`, but its own contract requires supervisor wiring. The current supervisor's gate path is still inline, with no accepted full gate key or ten-worker capacity proof. Store backpressure, critical wait deadlines, and checkpoint-`busy` handling remain open. M3 must be accepted before M4 can be accepted.

## M5 — Architectus RLM/task-tree execution and conversations

- **Definition:** Implement `should_decompose → TaskDecomposer → TaskTree validation → TaskRouter → node dispatch → envelope aggregation → ResultEvaluator`, one shared `conversations.db`, steering, and recursive completion.
- **Acceptance criteria (review §3, M5):**
  1. A three-level fixture runs only dependency-ready nodes, obeys session width/depth, and reaches root completion only after all descendant envelopes and gates succeed.
  2. Cyclic, multi-parent, over-depth, and over-width plans dispatch zero workers and produce typed rejection evidence.
  3. Parent LLM context contains own bounded turns + parent summary + child envelopes only; a canary scratchpad string in a child conversation never appears in parent context.
  4. `conversations.db` answers `last_turns`, `cost_by_node`, and `context_for` with indexed query plans and reconstructs from durable protocol events after projection deletion.
- **STATUS: IN-FLIGHT**
- **Evidence:** The branchable SQLite conversation store was merged in `548be1e` from `ee944b1`; Task Tree validation is in `06ce0dc`; the Architectus design was merged in `3b0b6b6` from `5b20605`; and an Architectus execution core is merged (`src/cambium/architectus.py` implements `decide`/`step`/`compose_context`/`aggregate`). The core is not wired: nothing in `src/cambium/` imports `architectus`, and `src/cambium/orchestrator.py` still keeps the submit/drain placeholder and only forwards a prebuilt plan to `run_plan`; it does not implement decomposition, ready-node scheduling, context composition, steering, aggregation, or evaluation beyond the architectus module's standalone logic.

## M6 — First real LLM end-to-end task

- **Definition:** Connect Diffundo, one real OpenAI-compatible provider, `CambiumLM`, one atomic coding task, one deterministic gate, and Unio publication; keep it manual and key-gated, not default CI.
- **Acceptance criteria (review §3, M6):**
  1. With one provider key, a worker receives a real completion, edits a fixture repo, passes a predeclared test gate, publishes exactly one fast-forward commit, and returns a durable result envelope.
  2. Provider identity/model/usage/latency/cost metadata are recorded without prompt, key, or chain-of-thought content.
  3. A forced 429 falls through to a second `FAST` provider; total exhaustion pauses and then resumes after recovery without worker restart.
  4. The same task with a failing gate cannot publish to main. This is the first release evidence that joins LLM, Diffundo, Opifex, Custos, GateRunner, store, and Unio.
- **STATUS: IN-FLIGHT**
- **Evidence:** Strict provider configuration is merged in `228a4e1` from `edd0e60`, with the guarded import fix in `ed0c51a`. Diffundo is merged in `77f3d52` (worker-provider): `src/cambium/diffundo.py` is on the baseline and `worker.py` builds the provider router at `worker.py:253,1069`. `CambiumLM` is merged in `c69ec92` (`src/cambium/lm.py` and `tests/scenarios/test_lm.py` are on the baseline; nothing in `src/cambium/` imports it yet), and the provider-backed worker agent loop is merged in `d0568a9` (`worker.py:931`). Provider staging is real: `test_m6_staging.py` drives `run_plan` against a loopback fake provider with M6-hygiene quota/publish-scope assertions. Real-provider E2E is unverified: there is no `CambiumLM` real-provider run (`cambium auth run supervisor` with a real key) and no provider-to-worker-to-gate-to-Unio acceptance proof on the baseline. M2-M5 are hard predecessors.

## M7 — Persistent worker pool

- **Definition:** Implement pre-spawned reusable subprocesses, pool admission/retirement, NodeSession bind/reset, health, and leak checks; never use warm fork.
- **Acceptance criteria (review §3, M7):**
  1. Ten DSPy-capable workers become task-ready at p50 <100 ms and p90 <250 ms after pool warmup; cold pool startup is reported separately.
  2. Sequential tasks cannot observe the predecessor's cwd, env, conversation, open FDs, subprocesses, generation, or provider state; fault injection retires the contaminated worker.
  3. Pool disabled and pool enabled produce byte-equivalent protocol/event semantics except worker PID and timing fields.
  4. Production config rejects `max_width >= 4` when the pool is disabled unless an explicit development override is set.
- **STATUS: BLOCKED**
- **Evidence:** A persistent-pool state-machine seed is merged (`src/cambium/worker_pool.py`, `tests/scenarios/test_worker_pool.py`); its module docstring self-declares it is "a seed for the pool boundary, not M7 acceptance evidence." No pool subprocess supervision, admission/retirement I/O, or leak checks are on the baseline. The merged Custos work is per-task subprocess supervision, not reusable-worker reset. The review makes the pool a release gate for configured `max_width >= 4`, but M2-M6 are not accepted; therefore M7 cannot be accepted even if implementation starts in parallel.

## M8 — DSPy `should_decompose` refinement

- **Definition:** Put the DSPy strategy in `modules/should_decompose/decide.py`, add the SIMBA optimizer, migrate the enum/schema, and record bench/refinement artifacts.
- **Acceptance criteria (review §3, M8):**
  1. Package is renamed from `example` to `should_decompose`; JSON CLI and eval CLI both pass.
  2. `Decision` enum replaces the Python boolean boundary under a dataset schema-version bump; wire JSON remains explicit and versioned.
  3. SIMBA candidate improves frozen eval over the rule baseline, passes 100% canaries, records train/eval/canary deltas and pinned model, and can roll back by refinement ID.
  4. If no candidate meets all gates within the declared call/cost budget, the experiment is falsified and the rule engine remains production. “DSPy used” is not acceptance.
- **STATUS: IN-FLIGHT**
- **Evidence:** The benchmark plugin was merged from `a7a54be` through `624e27c`; the bounded eval-only cache was merged in `ea4a3ad` from `d8f9408`; the `Decision` enum migration is merged in `c43d5fa`/`4bbc7cf`; and the baseline refresh is merged in `38d46a7` (307 IDs, still not module-scoped). The current package is still `src/cambium/modules/example/`, and no SIMBA candidate, frozen-eval promotion, pinned-model artifact, or refinement rollback is evidenced. M5 and M6 remain predecessors.

## M9 — Proposal 1: tree-sitter context compression

- **Definition:** Compare raw text context with tree-sitter AST/symbol chunks for the same coding tasks; keep the compressor as a context adapter and use explicit unsupported-language results.
- **Acceptance criteria / falsification metric (review §3, M9):**
  1. Freeze at least 30 tasks across three supported languages and the same provider/model, temperature, gate, and task budget. Run paired raw-context and AST-context trials.
  2. Primary metric is **input tokens per compile-successful task**. Secondary metrics are compile-success rate, gate-pass rate, wall time, and changed-file recall.
  3. Adopt only if median input tokens fall at least 25% while compile-success rate falls no more than 2 percentage points and its paired 95% confidence interval excludes a decline worse than 2 points.
  4. If token savings miss 25% or compile-success degradation exceeds the bound, Proposal 1 is falsified and tree-sitter stays out of the runtime. Do not ship it because chunks look cleaner.
- **STATUS: DONE** for the requested research deliverable; **runtime adoption is not accepted**.
- **Evidence:** The research was merged in `4b5cca8` from `30d1043`. `docs/research/treesitter-context.md` verifies Python 3.14 feasibility and reports 80.8%–95.5% input-token reduction using a chars/4 proxy. The same report marks compile-success, gate-pass, wall-time, changed-file recall, and the required paired 30-task/three-language provider trial as **UNVERIFIED** because M6 has no real-provider harness. Its recommendation is to defer final adoption until that trial exists.

## Next actions — merge order

1. **Merge the supervisor consolidation wave:** credential isolation (fix the `_worker_environment` provider-key leak at `supervisor.py:824-826`), provider default, plan validation, and the `cambium.worker` default.
2. **Merge module baseline regeneration and the offline-test fix:** re-scope `baseline.json` to module-local node IDs (the 278 foreign scenario node IDs fail the module-test gate) and fix `test_subprocess_network_client_is_denied`, which raises `PermissionError` under the offline environment.
3. **Merge the worker tool-loop:** replace the single marker-append decision with the real tool dispatch loop.
4. **Supervisor serial wave (after the consolidation wave):** publish-integrity guards, redaction wiring at enqueue/INSERT, `result.json` production wiring, and the M1 deletion (`EventLog`, fallbacks, `events.py`, slice `run_session`).
5. **M6 real-provider E2E:** exercise Diffundo + `CambiumLM` through `cambium auth run supervisor` with one real provider key.
6. **M5 integration, then M7/M8, then M9 adoption:** wire the merged Architectus execution core into the supervisor; implement the pool before production multi-worker claims; run M8 SIMBA evidence and the `example`→`should_decompose` rename; finally run M9's paired 30-task trial before any tree-sitter runtime adoption.
7. **Final:** re-baseline module-scoped, verify the full suite, clean up worktrees, and close the tracker.
