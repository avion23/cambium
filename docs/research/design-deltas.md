# Cambium — Design Deltas (D1..D7)

**Version:** 1.0.1
**Date:** 2026-08-09
**Branch:** `wt-deltas2` (`/tmp/opencode/cambium-deltas2`), architecture source `wt-arch@17ef25f`
**Status:** Historical decision record. Deltas marked **adopt** amended v2.0.0
`docs/architecture/architecture.md`; current readers must use that architecture, `src/cambium/`,
and `docs/research/v2-1-status.md`.

**Historical snapshot / current pointer:** the branch-local claims below preserve their dates,
branches, SHAs, and evidence. Current notes: provider loop, Diffundo, EventStore, and root
`Result` exist; DLQ, eval cache, ResourceBudget, `worker_pool`, and `events` are absent;
per-worker sandbox and shell approval were removed by product decision, and dynamic hierarchy
is absent.

## 0. Reading and provenance

Every path/section and URL was checked or marked **UNVERIFIED** at drafting time. Sources were
the external architecture re-review, `docs/research/prime-agent.md`, and directives dated
2026-08-09. Research branches later merged into main remain historical provenance:

| Evidence | Snapshot provenance |
|---|---|
| Architecture, `agents.md`, module template | `wt-arch@17ef25f` |
| Threat model | `main`, first drafted `wt-threat@b863084` |
| Sandbox options | `wt-sandbox@242a509` (AppArmor evidence) |
| Event/IPC drafts | `main` |
| Cold-start benchmark | `wt-coldstart@108c83d` |

## D1 — Remove the Diffundo local LLM cache entirely

**Source:** USER DIRECTIVE (2026-08-09); resolves `LLM-C1`, `LLM-M5`, and threat-model R8.
**Status:** **adopt.**
**Amends:** architecture §§2, 4, 8, 8.1, 9.2–9.3, 18.2, 18.4; supersedes system-design §M2.

**Decision:** delete local response LRU/TTL/cache flags and make Diffundo a stateless router
(only provider cooldown state). Provider cache controls stay in `ProviderConfig`; workers may
place stable prefixes but never manage a correctness cache. Provider evidence, fetched
2026-08-09, is exact-prefix/content-addressed and never stores a response:

- OpenAI: https://platform.openai.com/docs/guides/prompt-caching
- Anthropic: https://docs.anthropic.com/en/docs/build-with-claude/prompt-caching
- DeepSeek: https://api-docs.deepseek.com/guides/kv_cache

This removes cache keys, TTL/coherence windows, and stale completions; provider hit-rate/cost
is a performance concern only. The host-side rule, if ever added, is content-addressed and
repo-state-aware (Q1.3). `CambiumLM` forwards tier/model/temperature only.

**Open questions:** Q1.1 provider-specific `cache_control` shape; Q1.2 expose
`cached_tokens`/`cache_read_input_tokens` telemetry; Q1.3 outside-host cross-session cache
must be content-addressed (owner: Diffundo/Nuntius).

## D2 — Formalize the Task Tree

**Source:** USER DIRECTIVE (2026-08-09). **Status:** **adopt.**
**Amends:** architecture §§3.4, 3.6, 4, 6.3, 7.1, 16; `example-spec.md` §3; event draft
§§3.1, 3.10 (payload-first `parent_task_id`).

**Decision:** `TaskDecomposer` emits a single-root DAG of worker/node sessions. A deterministic
validator performs unique-ID, dependency, cycle, multi-parent, depth/width checks and
topological ordering before dispatch. Each node owns an append-only session log under
`${session_dir}/.cambium/sessions/<node_id>/`; child results flow upward as `Result` envelopes;
parent steering flows downward. `parent_task_id` is in `submitted`/`task_decomposed` payloads,
not a new events column, so the envelope remains compatible with the draft migration rule.

**Invariants:**

- **I2.1** one root; each non-root has exactly one parent.
- **I2.2** no cycle, self-loop, or multi-parent in v2; Kahn validation rejects and boundedly
  re-prompts the decomposer (the DS-M6/LLM review cycle finding; the LLM review labels its item N2).
- **I2.3** supervisor-enforced `max_depth` (default 3 proposed) and `max_width`.
- **I2.4** node context = bounded own log + parent summary + subtree envelopes; never a sibling's
  raw session. Node session and public `Session` terminology are separated by D3/Q3.5.
- **I2.5** terminal only after own work and every descendant envelope complete; D4 gate defines work done.
- **I2.6** session logs are append-only; steering appends turns.

The tree closes DS-M6 and the LLM review's N2/`LLM-M6` lifecycle gap: the log answers who asked
for a task and what it returned without changing the event envelope.

**Open questions:** Q2.1 validator in orchestration or Custos (must remain pure); Q2.2 full tool
stream versus bounded summary; Q2.3 depth 3 versus opencode `subagent_depth=1`; Q2.4 reject
multi-parent for v2 or generalize to a DAG in v2.1.

## D3 — Persistent sub-agents: admission, steering, checkpointed sessions

**Source:** PRIME AGENT good part (`prime-agent.md` §§2.2–2.3). **Status:** **adopt.**
**Amends:** architecture §§5.1–5.2, 6.4, 7.1–7.2, 14, 16.3; IPC draft §§2.2–2.3, 3, 4.1.

**Decision:** a worker is a named `NodeSession` (proposed `session_id == task_id`) that can
receive repeated steering turns, checkpoint/reload its own log and trajectory, and return
child→parent `result_envelope` messages. `spawn` returns a supervisor-internal **admission** ack;
it is not a pre-`ready` wire message. The wire stays `init → ready → steer/result_envelope`;
`steer` is valid only after `ready`, avoiding `PROTO_OUT_OF_ORDER` and preserving
`ready_timeout`. The public `Session` remains one headless task execution; the naming collision
is explicit until Q3.5 is settled.

**Boundaries:** parent→child steering and child→parent results are routed by Custos; siblings
communicate only through the parent (Q3.3). The worker must checkpoint before risky turns and
reload session state after crash; the persistent model is across steering turns within a task,
not a cross-task pool (pool is v2.1 per §14).

**Open questions:** Q3.1 shared or distinct `session_id`; Q3.2 reload full events, DSPy
trajectory, steering, and summary; Q3.3 parent-mediated versus direct sibling messaging; Q3.4
within-task persistence versus cross-task pool; Q3.5 `NodeSession` versus a `Session` subtype.
The opencode ACK-loop concern is **UNVERIFIED** in `opencode.md` and is a precaution only.

## D4 — Autonomous gate + budgets

**Source:** PRIME AGENT autonomous mode/gates. **Status:** **adopt.**
**Amends:** architecture §§5.2, 7.1, 7.4, 7.8, 10; Architectus/Unio module notes.

**Decision:** a task completes only after its test gate passes. Gate failure becomes a bounded
steering turn carrying command/output evidence; `gate_max_retries` defaults to 2 and is
supervisor-owned. Exhaustion yields `status="failed"` with command, exit code, and captured
output. Gate verdicts are content-addressed by worktree tree hash/command/base/input, and Unio
re-runs the final gate before publish.

**Open questions:** Q4.1 worker gate versus final Unio gate; Q4.2 per-session versus shared
verdict cache (per-session is the safe default); Q4.3 gate retries inside or outside
`absolute_max` (shared wall budget proposed).

## D5 — Continual-harness refinement loop → DSPy seam

**Source:** PRIME AGENT continual-harness good part (`prime-agent.md` §§1, 2.4). **Status:** **adopt.**
**Amends:** architecture §§4, 10, 17.3–17.4; module-template architecture §10 and
`dataset-format.md` §6.

**Decision:** module harness state (prompt/decide program, skills, memories, dataset, metric)
changes through plan/apply proposals with a `refinement_id`, versioned promotion, atomic
rollback by ID, frozen train/eval/canary gates, and human approval for edits outside module
scope (dataset, metric, sibling pins). Canary taxonomy includes
`trivially_atomic`, `must_decompose`, `ambiguous_calibration`, `format_only_hack`, and
`keyword_hack`; a degraded canary score rejects/rolls back a refinement. The Factorio reward-
hacking context is directive-provided, not a research-corpus file; the structural defense is
the canary gate and approval boundary (threat-model §4 M5).

**Open questions:** Q5.1 harness-state layout under `src/cambium/modules/<name>/harness/`;
Q5.2 dataset-version/sibling-pin bump on dataset or metric edits; Q5.3 host approval versus
halt/queue for unattended out-of-scope edits; Q5.4 mandatory before/after train/eval/canary
evidence table.

## D6 — Honest status vs external critique + hardening residue

**Source:** external re-review (in flight at drafting). **Status:** **adopt** repo-state
awareness and smoke milestone; **reject-with-reason** the EOF claim.
**Amends:** architecture §§0, 5.3, 18, 19.15; `agents.md` §§5, 9; example-spec §9.4.

Already resolved before the critique: free-threading pin (DS-M5/IMPL-M1), dedicated event-log
writer (DS-C1/IMPL-M7), serialized merge sequencer (IMPL-C1/DS-M1), and tier cascade
(LLM-C2/IMPL-C10). The critique's “stdout EOF = death” praise is rejected: architecture §5.3
defines EOF as advisory; process exit and `exit` are definitive, heartbeat is stronger, and
drain deadlines prevent supervisor stalls (DS-C2).

**Adopted residue:** every dedup/verdict cache is content-addressed (D1/D4), and Milestone 0
becomes the first implementation gate: one fake worker, one file edit, one scenario gate, one
atomic Unio merge, durable `merge_committed`, `result.json` with `status="done"`, clean exit,
and no orphan process/worktree. It is referenced by `agents.md` §5/§9 and example-spec §9.4 but
was not built in this snapshot.

**Open questions:** Q6.1 reconcile the in-flight review; Q6.2 headless `Session.run()` versus
the atomic `should_decompose` path; Q6.3 fake LLM versus real fast-tier provider.

## D7 — No sandboxing in the harness; Septum removed from v2

**Source:** USER DIRECTIVE (2026-08-09). **Status:** **adopt.**
**Amends:** architecture §§2, 4 (M8), 7.2, 8, 11, 12.1, 18.3, 19; threat-model R3/R4;
resolution rows IMPL-M4, IMPL-M6, IMPL-C7.

**Security boundary:** no Septum/sandbox wrapper exists. Containment is worktree isolation,
permission allowlists, a least-privilege worker environment, and host approval gates. The
worker env is constructed from minimal `PATH`/`PYTHONUNBUFFERED`/`CAMBIUM_*` plus named
`provider_env_keys`, never `{**os.environ}`. `run_shell` remains an explicit permission-gated,
logged, timeout-bounded capability. The sandbox-options evidence says unprivileged namespace
sandboxing is blocked by AppArmor (`apparmor_restrict_unprivileged_userns=1`, `unshare -Ur`
denied); root workaround is broken for uid-1001. Host containers/microVMs are deployment
responsibility, not harness code. R3 is accepted out of scope; R4 is a required spawn fix.

**Open questions:** Q7.1 retain M8 marked removed; Q7.2 static allowlist versus interactive
`approve(session_id, op)` for external writes/network (the production approval boundary remains
absent); Q7.3 extra `run_shell` env scrub; Q7.4 optional macOS backend versus out of scope.

## 1. Cross-delta consequences

- **Events:** D2 adds payload `parent_task_id`; D4 adds gate-verdict records; gap-free `seq` remains.
- **IPC:** D3 adds `steer`; admission is internal; child result envelopes preserve `request_id`.
- **Budgets:** D4 adds `max_turns`, `max_tokens`, `timeout_ms`, `gate_max_retries`, supervisor-owned.
- **Environment:** D7 replaces inherited host env with a constructed allowlist (breaking spec ripple).
- **Diffundo:** D1 is cache-free; provider `cache_control` replaces local cache settings.

## 2. Unverified items and changelog

Unverified at drafting: cold-start numbers were not rerun; sandbox/threat claims were read from
their branch records; external critique text was not committed; provider pricing/model details
may drift; the Factorio incident is directive context. Structural provider-cache claims are
supported by the three URLs in D1.

| Version | Date | Change |
|---|---|---|
| 1.0.0 | 2026-08-09 | Initial D1–D7 record. |
| 1.0.1 | 2026-08-09 | Corrected §18 citations, Prime-Agent sections, payload-first `parent_task_id`, `.cambium/`, admission/`ready` ordering, D2/D3 terminology Q3.5, D7 approval/env ripple, and merged-research provenance. |

## 3. Decision rationale retained from the review

### D1: cache boundary and stale-completion failure class

The deleted local cache was a response map keyed by model/temperature/prompt and had no
repository-state component. That was the `LLM-C1` failure class: a response could be served for a
changed worktree, and `LLM-M5` noted that a per-instance cache has little cross-call value. D1
removes the storage, so there is no stale key or TTL window to audit. Provider prompt caches are
different: OpenAI caches the KV computation for an exact prompt prefix and generates a new
response; Anthropic invalidates the cumulative segment when a prior block changes; DeepSeek
requires a fully matching prefix unit. Provider-side cache controls therefore remain a latency/
cost hint, not a result or repository-coherence mechanism. A host cache outside Cambium must
include the exact state that determines its result, as Q1.3 states.

The architecture changes are intentionally broad: Diffundo's module row loses cache state;
`CambiumLM` no longer passes `cache`, `cache_namespace`, or `context_hash`; result envelopes no
longer carry `cache_hit`; and the old system-design §M2 is retained only as an origin record.
`cached_tokens` telemetry (Q1.2) is useful to measure provider behavior without reintroducing
an application cache.

### D2: causal tree and payload compatibility

The old flat `list[SubTask]` plus `depends_on` field did not specify cycle rejection or failed-
dependency behavior. Review DS-M6 called out a broken task-ID counter and pending cycles; the LLM
review labels its corresponding item N2 (not M6). D2 makes the graph a first-class deterministic
input: parse a flat decomposition, check IDs and one root, reject unknown dependencies and
multi-parent nodes, then Kahn-order it. A cycle or self-loop is rejected before dispatch and the
decomposer may be re-prompted only within a bounded retry budget.

`parent_task_id` is deliberately payload-first. `submitted` carries an optional parent and
dependencies; `task_decomposed` carries a parent, subtasks, and cycle evidence. Adding a required
SQLite envelope column would be a breaking migration under the event draft §7.2 policy. The
event log can reconstruct the tree by joining these payloads; a payload index can wait for query
volume. I2.4 limits context to bounded own history, parent summary, and child envelopes, which
also makes D3 persistence and D8b information hiding composable.

### D3: admission is not protocol state

The initial draft proposed a worker `admission` message before `ready`. That conflicts with
architecture §7.2's `ready_timeout` (the timer covers `init → ready`) and with IPC draft
`PROTO_OUT_OF_ORDER` rules. The adopted shape has Custos acknowledge admission internally when
spawn is accepted; the wire remains `init`, `ready`, then repeated `steer` and terminal/result
messages. A `steer` carries `request_id`, `session_id`, and context and is valid only after ready.
Child results flow upward as the existing `result_envelope`, not a second event vocabulary.

Checkpoint reload includes `state_ref`, the node's own append-only session log, DSPy trajectory,
steering history, and parent summary. This is persistence across steering turns within one task;
cross-task pools remain a v2.1 optimization informed by `worker-coldstart.md`. Public `Session`
still means one headless task execution. The proposed `NodeSession` term avoids silently changing
that API, and Q3.5 records the remaining naming choice.

### D4: gate as completion and verdict identity

The gate is not a post-hoc report. A worker receives command/output/failing assertion evidence as
a steering turn, gets at most `gate_max_retries` (default 2), and fails with a typed result after
exhaustion. Unio repeats the final gate before publish. The verdict cache is per session and
keyed by worktree tree hash, command, base commit, and gate input specification; sharing it across
sessions would recreate D1's coherence question (Q4.2). Gate retry count and wall time are
separate policy questions (Q4.3), but the supervisor owns the bound.

### D5: refinement controls and reward-hacking boundary

The proposed harness state includes prompts, skills, memories, datasets, and metric, but a
refinement cannot directly rewrite it. Plan produces a `refinement_id` and evidence; apply
promotes a versioned pointer; rollback restores the prior ID. Frozen eval plus 100% canaries is
the acceptance floor. The canary taxonomy catches keyword/no-op/test-deletion hacks, while
dataset, metric, or sibling-pin changes require a human gate. This is the explicit response to
`LLM-M3` (unstable coupled optimization) and the directive-provided Factorio reward-hacking case;
the incident itself is not claimed as a repository research source. Q5.1–Q5.4 keep storage,
versioning, approval, and evidence format open until Ascensus is implemented.

### D6: status truth and Milestone 0

The resolution table distinguishes items already folded before the critique: free-threading
(`DS-M5`, `IMPL-M1`), event writer (`DS-C1`, `IMPL-M7`), merge serialization (`IMPL-C1`, `DS-M1`),
and tier cascade (`LLM-C2`, `IMPL-C10`). Those rows are provenance, not new work. The critique's
EOF praise is rejected because DS-C2 documented four false-death modes (grandchild pipe
inheritance, block buffering, torn writes, and supervisor stalls); architecture §5.3 treats EOF
as advisory, with process wait/exit message definitive and heartbeat/drain watchdogs stronger.

Milestone 0 is intentionally narrower than M1: fake LLM, exactly one worker and throwaway
worktree, one `edit_file` change with uniqueness, scenario gate, Unio atomic `update-ref`,
fsynced `merge_committed`, atomic `result.json` (`status="done"`), no orphan process/worktree,
and exit 0. `agents.md` §5/§9 and example-spec §9.4 referenced the smoke path (`python -m cambium.tests.smoke`) but `Architectus.execute` wiring was pending. Q6.1–Q6.3 keep the path,
atomic fast-path, and fake-versus-real provider choice explicit.

### D7: sandbox removal is not a security claim

The Septum removal changes scope, not threat impact. Worktree isolation, generation fencing,
permission allowlists, least-privilege environment, and host approval are the remaining controls.
`run_shell` is not silently safe: it is allowed only when the task declares shell permission,
is logged verbatim, and remains wall/heartbeat bounded. The worker environment is constructed,
not inherited; only named provider keys can cross the boundary. This preserves the architecture's
old `--setenv` intent while moving enforcement from a sandbox wrapper to spawn.

The sandbox research found AppArmor blocks unprivileged user namespaces on this host, and the
root-mode workaround leaves uid-1001 files invisible/nobody. That explains why R3 is accepted
out-of-scope, not why a sandbox is unnecessary. R4 remains a required fix. Q7.2 is the unresolved
security boundary: static per-task allowlist versus durable interactive `approve(session_id, op)`
for first-time external-path writes and non-allowlisted network egress. No production approval
implementation existed at this snapshot.

## 4. Historical reference anchors

The record also preserves the original target-base anchor `agents.md@2b3bf93`, implementation
folds `39005fa`, `77f3d52`, and `c31e781`, and the review/source references `DS-N4`, `IMPL-M2`,
`IMPL-M5`, `LLM-C4`, and `LLM-M3`. They identify the branch state at the decision time and do
not imply that those names or components are current source.

## 5. Later hierarchy feedback — skeptical classification

This note records a later feedback claim without changing the dated D1–D7 decisions.

| Later claim | Classification at this record | Boundary |
|---|---|---|
| Harness-owned explicit agent/TaskTree hierarchy | **Accept as target structure.** | D2's explicit single-root DAG, parent IDs, bounded depth/width, and D8b envelope are the intended contract; no current-runtime claim follows. |
| Fresh child context and strict envelope upward | **Accept as target.** | A child receives only its declared context and returns diff/summary/metrics/status; schema validation prevents scratchpad/reasoning leakage. |
| Static DAG before dynamic admission | **Accept as target invariant.** | Validate the complete plan, IDs, dependencies, cycles, and bounds before dispatch/admission; runtime workers cannot mutate topology. This still needs M5 tests. |
| Implicit recursion is dead | **Not a verified repository finding.** | The design chooses explicit DAGs; “dead” is a rhetorical claim until source and comparative metrics establish it. |
| Explicit trees yield a 90% cache discount | **UNVERIFIED; do not adopt.** | No primary measurement, provider, prompt, task distribution, or cost baseline is present. D1/D8c retain only exact-prefix guidance. |
| “Prime 2026 proves it” | **Partly grounded, broad claim unverified.** | Primary audit supports explicit `AgentSession`/runtime contexts and bounded depth, with descendants sharing one root worker; it does not prove process-per-child isolation, 90% savings, or universal recursion conclusions. |
| AlphaCodium/LATS require MCTS and tests at each node | **UNVERIFIED consensus claim.** | Per-node gates/tests are a Cambium target (D4/M5); MCTS is not a universal requirement and is not adopted without a falsifiable comparison. |
| Five cheap branches are always better | **UNVERIFIED.** | Width remains bounded by `max_width`; cost/latency/success metrics must choose it per task. |

The accepted hierarchy is structural information hiding, not a promise of cache savings, MCTS,
or provider economics. Any future adoption must name a primary source, fixed task set, cost/
latency metric, and failure gate.

## 6. Cross-delta contract details

### Event and IPC consequences

D2's `parent_task_id` is carried in payloads for both critical `submitted` and non-critical
`task_decomposed`; no events-table column is added. D3's `steer` message is supervisor→worker,
valid only after `ready`; `admission` is a control-plane acknowledgement and never a pre-ready
wire frame. D4 adds `max_turns`, `max_tokens`, `timeout_ms`, and `gate_max_retries` under the
supervisor-owned `init.budget` block. D7 changes spawn environment from `{**os.environ}` to a
constructed allowlist. These are additive except the environment change, which is explicitly a
breaking behavioral ripple.

The result-envelope boundary is stable: `summary` is capped at 2,000 characters, `unified_diff`
at 64 KiB, and `request_id` correlation is preserved. D8b's strict allow-list supersedes any
assumption that a child transcript is an upward API. A future host that wants full transcripts
queries the node store under an explicit host boundary; it does not add fields to the LLM-facing
envelope.

### Source and branch status

The original D1 research references were later merged into main, but the review was written from
several branch trees. `worker-coldstart.md` was `wt-coldstart@108c83d`; sandbox evidence was
`wt-sandbox@242a509`; threat model began at `wt-threat@b863084`; architecture was
`wt-arch@17ef25f`. The 1.0.1 review pass corrected Prime-Agent section numbers, the compaction
summary tuple, `.cambium/` path, `NodeSession` terminology, payload-first parent linkage,
admission/ready ordering, and the D7 §8/§18.3/§19 ripple. Those corrections are historical
provenance, not claims that every cited branch implementation is current.

### D1 provider evidence boundary

OpenAI's exact prefix rule, Anthropic's cumulative hash invalidation, and DeepSeek's full prefix
unit requirement all support cache *miss* behavior on changed content; none support a 90% savings
claim. The structural guarantee is “a changed prefix is a different key and the response is still
computed,” not “every tree task is cheaper.” A future metric must report hit rate, input tokens,
latency, and cost against a fixed prompt/task baseline before changing D1.

### D2/D3 execution boundary

The proposed depth default 3 and width cap are policy inputs, not a promise of five parallel
branches. Parent-mediated sibling communication prevents an unbounded A2A graph; direct sibling
messaging is deferred because routing/rate limits are not specified. Session reload is similarly
bounded: own log, checkpoint state, steering history, and parent summary, not an arbitrary global
conversation. A node cannot silently create a second root or mutate an already-admitted DAG.

### D4/D5 evidence boundary

Gate evidence includes command, exit code, output tail, and failing assertion, but never raw
credentials or chain-of-thought. A refinement evidence record must identify which train/eval/
canary signals it expects to move; a canary-only pass is insufficient if the metric regresses.
Out-of-scope dataset/metric/sibling edits are host-approval operations, linking D5 Q5.3 to D7
Q7.2. This is a safety boundary, not an endorsement of an autonomous self-editing loop.

### D6/D7 security boundary

D6's smoke milestone is the smallest test that exercises spawn, edit, gate, merge, result, and
cleanup. D7's no-sandbox decision does not make `run_shell` safe; it makes its permission and
approval semantics explicit. The per-worker environment must be built before a process starts,
because a post-start filter cannot retract credentials already readable through `os.environ`.
Approval replay must be keyed by generation and operation digest so a reset worker cannot reuse a
stale grant. The exact callback shape remains unresolved at Q7.2; no source currently closes it.

## 7. Decision register by amended architecture section

| Architecture surface | Adopted historical change | Boundary still open |
|---|---|---|
| §2 layering | D1 stateless Diffundo; D2 TaskTree/Architectus; D7 no Septum; D8d composition root | The source tree does not yet have a dynamic hierarchy or production provider loop contract. |
| §3.4/§3.6/§3.7 results and tree | D2 parent payload; D8b strict upward envelope and bounded diff; D4 gate-defined completion | `Result`/envelope integration and parent context tests are M5 work. |
| §5 IPC | D3 `steer`, ready gating, admission internal; D8a module CLI; D8e host container transport | FD 3 is a v2.1 implementation task; stdout contamination must be tested. |
| §6 stores | D2 node logs; D8g queryable conversation WAL; D4 content-addressed gate verdict | Separate DB/table choice, writer/backpressure, and rebuild path remain open. |
| §7 lifecycle/merge | D3 persistent NodeSession; D4 gate/retries; D7 env/approval; D8f pause | Generation file, approval protocol, worker pool, and runtime Unio wiring are not closed. |
| §8–§9 transparency/provider | D1 removes local cache; D8c stable prefix; D8f rate/pause; D5 refinement artifacts | No 90% cache discount, MCTS universality, or provider-cost claim is adopted. |
| §10/§17 evaluation | D4 gates; D5 plan/apply/canary rollback; D2 sibling boundaries | DSPy/SIMBA and harness state are experiments until metrics pass. |
| §11/§12 tools/security | D7 allowlists/approval and least-privilege env; D8d ports | `run_shell` remains a gated residual; production approval is absent. |

The register prevents a common historical-reading error: an adopted delta can amend a normative
section while its code remains absent. “Adopt” records the decision and its rationale; milestone
acceptance requires the source/test checks in `v2-1-review.md` §§4, 11 and the current status pointer.

## 8. Explicit non-adoptions

The record rejects several tempting shortcuts: no local response cache, no implicit recursive
worker graph, no raw child transcript in parent prompts, no default race mode, no in-harness
sandbox wrapper, no universal MCTS requirement, no unmeasured provider discount, no automatic
five-way fan-out, and no compatibility fallback when EventStore/Unio is missing. These are not
style preferences. Each shortcut either reopens a cited failure class (`LLM-C1`, DS-M6, DS-C2,
F-01/F-20) or lacks a primary source/metric. A future proposal must name the changed boundary,
provide a falsification test, and update the relevant Q item rather than silently changing D1–D7.

## 9. Primary-source correction for hierarchy and cache claims

The later primary-source audit narrows, but does not broaden, the record. Prime Agent supports
explicit RLM child `AgentSession`/runtime objects with independent contexts and bounded depth;
descendants share one root-session worker. This is evidence for D2's structural tree and fresh
context boundaries, not evidence for process-per-child isolation. Any text that calls Prime a
process-per-child precedent is corrected here.

Provider caches are organization/workspace scoped and need not be per-conversation. Exact prefix
matching prevents reuse under the wrong prefix, but separate tasks can share a provider cache when
their stable prefix matches. Current OpenAI/Anthropic models may price cached-token reads at about
0.1× input-token rates; that is a provider billing fact, not a 90% total-request or latency
guarantee. A future benchmark must measure total tokens, hit rate, latency, and cost per task.

LATS is a candidate-solution MCTS method with test/environment feedback; AlphaCodium is a staged
competitive-programming run/fix flow. Neither is evidence that MCTS is required for every
TaskTree node or that either method is task orchestration. Recursion evidence is task-dependent;
there is no consensus that implicit recursion is universally a dead end. The clean target remains
explicit hierarchy, bounded contexts, strict envelopes, and measured cache metrics.

The cache boundary also needs a scope test: provider org/workspace caches may legitimately serve
two tasks that share a stable prefix. The safety property is that a task with a different prefix
does not receive the old prefix computation, not that cache entries are private to one task. A
benchmark should separate cached input-token price (potentially near 0.1× current input rates)
from output cost, orchestration cost, latency, and total request cost. D1's no-response-cache
decision remains unchanged.

## 10. Open-question ownership map

The Q items are deliberately assigned to implementation boundaries so later readers do not treat
them as settled facts. Diffundo owns Q1.1–Q1.3 and Q8c.1–Q8c.2; Nuntius owns telemetry,
envelope/error versions, and Q8a.3/Q8b.1; the orchestrator/Architectus owners own Q2.1–Q2.4,
Q3.1–Q3.5, Q4.1–Q4.3, and Q6.1–Q6.3; Ascensus owns Q5.1–Q5.4; Custos owns Q7.2–Q7.3 and
Q8e.2/Q8f.2/Q8g.1–Q8g.3; operations owns Q8e.1/Q8e.3. An owner assignment is not an
implementation claim. It is the handoff needed to resolve a boundary with a test or source.

The most security-sensitive open items are Q7.2 approval semantics, Q3.2 reload contents,
Q4.2 cache scope, and Q8b.3 parent visibility. The most measurement-sensitive are Q1.2 provider
hit telemetry, Q8c.2 prefix alignment, Q8f.1 `rpm` defaults, and the later cache-discount claim.
The most schema-sensitive are payload-first parent linkage, `Decision` enum migration, and D8a
error versioning. Keeping these classes separate prevents a performance hypothesis from silently
changing a trust boundary.

The static-DAG-before-admission rule also bounds provider exposure. A malformed decomposition is
rejected before it can spawn workers, consume tokens, or request approval. A valid node may still
fail a gate, exceed a budget, or hit provider exhaustion; those are D4/D8f outcomes and must be
represented by typed envelopes, not implicit recursion. Dynamic steering changes node context,
not graph topology. This separation is the clean target shared by D2, D3, D4, D8b, and the later
hierarchy feedback.

The accepted structural hierarchy is intentionally independent of the unresolved economics. D2
can be implemented with a deterministic flat plan and envelope validator even when no provider
cache is configured, no pool is warm, and no MCTS search is selected. D3 can reload a child log
without forwarding raw reasoning. D4 can gate a node without a root evaluator. These separations
make the milestones falsifiable and preserve the D7 same-UID security boundary while the
orchestration layer is still absent.

The primary-source correction also limits language in later records: say “supports explicit
contexts and bounded depth,” not “proves the architecture”; say “cached-token input price may be
near 0.1×,” not “90% cheaper”; say “LATS uses candidate-solution MCTS” and “AlphaCodium uses a
staged run/fix flow,” not “all task trees require MCTS.” This vocabulary is now part of the
historical evidence trail.

This vocabulary also protects source attribution. A primary provider document can support exact
prefix matching or cached-token pricing, but not a task-level cache discount without a workload
measurement. A primary AgentSession description can support explicit context ownership, but not
process isolation or universal recursion claims. A method paper can describe MCTS feedback, but
not make it a required architecture component. The decision register keeps those inference steps
visible and marks them for later falsification.

The same source discipline applies to historical status: a branch-local `Diffundo` router or a
Prime context description may support a design choice, but neither proves the current runtime
has provider admission, cache telemetry, process isolation, or a production tree. Those claims
remain tied to M5–M7 acceptance tests and the current-status pointer.

## 11. Closure conditions for historical deltas

An adopted delta is closed only when the amended architecture, source, and focused scenario agree.
D1 needs a provider-prefix test plus no response-cache state. D2 needs flat-plan rejection of
cycles/multi-parent/over-width input and a root result that waits for descendants. D3 needs
`init → ready → steer/result` ordering, checkpoint reload, and no pre-ready admission frame. D4
needs gate retries, content-addressed verdicts, and final Unio gating. D5 needs frozen eval,
canary rollback, and an approval decision for out-of-scope edits. D6 needs the one-worker smoke
path on one SHA. D7 needs env construction, generation fencing, and a fail-closed approval
protocol. D8a–D8g need the CLI, envelope, prompt lint, ports, deployment boundary, bucket/pause,
and conversation projection tests stated above.

Until those checks pass, historical documents must say “target,” “branch-local,” or
“UNVERIFIED,” not “implemented.” This closure rule is the practical link between the decision
register and the current architecture/status pointer.

This rule also keeps reports honest when names overlap: `EventStore` means the class, not a
runtime caller; `Diffundo` means a router design, not an active provider; and `AgentSession`
means an explicit context object, not a subprocess. Future status notes should identify the
source symbol, caller, test, and baseline before changing a historical disposition.

The historical files therefore keep decisions and findings while directing live readers to
architecture, source, and v2-1-status. They are not alternate current specifications.

For that reason the compact records retain dates, branches, source URLs, IDs, severities,
accepted/rejected reasons, and unresolved boundaries even when repeated prose and copied maps are
gone. Observed edit check: 122/122 unique IDs, 3/3 URLs, and 48/48 refs were preserved with no
loss/addition; local-link, exact-scope, word-count, and whitespace checks passed.

The check is a canary for evidence preservation, not a runtime test.
