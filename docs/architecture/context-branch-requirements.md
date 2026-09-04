# Agent and context-branch requirements

**Status:** target design requirements, not a list of shipped features or new
per-turn gates. `MUST`, `MUST NOT`, `SHOULD`, and `MAY` describe the intended
interfaces when implemented. Current behavior is listed in section 13;
`implementation-plan.md` identifies the remaining small implementation slices.

Rationale is in [`agent-operating-model.md`](agent-operating-model.md).

## 1. Terminology

- **Branch:** one bounded unit of agency with task, context, authority,
  resources, children, result, and artifact state.
- **Task tree:** parent/child ownership and lifetime structure.
- **Conversation branch:** one model branch's transcript, tool calls, and
  checkpoints.
- **Git artifact graph:** commits and accepted integration order.
- **Cache lineage:** exact provider-request-prefix compatibility.
- **BranchState:** canonical derived state for one branch.
- **SituationFrame:** bounded model-facing projection of BranchState.
- **WorkLedger:** derived current semantic items and append-only transitions.
- **ResultCapsule:** bounded child result returned to a parent.
- **Evidence reference:** stable identifier for a tool, event, checkpoint,
  source window, check, branch, or commit.

These concepts MUST remain distinct in source, events, documentation, and UI.

## 2. Authority and truth

1. Durable events, immutable checkpoints, provider evidence, and Git objects
   MUST remain the reconstructible authority for runtime state.
2. BranchState MUST be a pure materialized view; it MUST NOT become another
   mutable store.
3. A model response MAY propose a claim, decision, plan, tool call, child, or
   finish verdict. It MUST NOT directly mutate worker, supervisor, provider,
   checkpoint, quota, or Git state.
4. A semantic claim MUST NOT be treated as proof of a tool result, provider
   result, verification, or artifact publication.
5. Unknown, stale, inferred, and directly observed values MUST be represented
   distinctly.
6. Every accepted effect MUST retain the branch/generation identity that owned
   it.
7. Every lossy projection item SHOULD retain one or more stable evidence refs.
8. Higher layers MUST use validated lower-layer interfaces rather than reach
   into their mutable internals.

## 3. Canonical BranchState

1. One BranchState reducer MUST own shared derived semantics for supervisor
   inspection, model context, TUI, monitor, and reconnect.
2. The same ordered sources and validated Git snapshot MUST produce
   byte-identical canonical state.
3. BranchState MUST distinguish at least:
   - task, parent, generation, and lifecycle;
   - mission, constraints, done criteria, and authority;
   - context checkpoint/epoch/lineage;
   - base, worktree, and accepted integration heads;
   - provider/model lease and resource facts;
   - plan, open obligations, blockers, and verification;
   - children in deterministic admission order;
   - stable evidence anchors.
4. Context epoch, worker generation, and artifact head MUST NOT be inferred from
   one another.
5. Completion-time ordering MUST NOT replace admission or event ordering.
6. A reducer MUST fail or mark unknown when required source identity is missing;
   it MUST NOT invent a plausible current value.
7. Frontends MAY derive display-specific fields, but shared semantic fields MUST
   come from BranchState.

## 4. SituationFrame

1. Every normal model decision SHOULD receive one SituationFrame built from the
   latest BranchState immediately before dispatch.
2. The frame MUST contain a projection version, source watermark, branch,
   generation, context epoch, artifact head, and digest.
3. Canonical section order MUST be:

   ```text
   MISSION, AUTHORITY, ACCEPTED, DELTA, OPEN, CHILDREN, RESOURCES, ANCHORS
   ```
4. The frame MUST be bounded globally and per section.
5. Truncation MUST be visible and SHOULD expose an `inspect_state` continuation.
6. The frame MUST be harness-authored and MUST label model proposals,
   observations, accepted facts, unknowns, and stale data accurately.
7. It MUST NOT contain credentials, secrets, hidden reasoning, or an unbounded
   transcript.
8. It MUST be appended as a changing suffix and MUST NOT rewrite the stable
   system/tool head or published CAST entries.
9. Summary mode MUST NOT accidentally promote frame control text into semantic
   evidence.
10. The action produced from a frame MUST be auditable against its frame digest
    and source watermark.

## 5. Context and accretion

1. The semantic trunk MUST remain append-only within an epoch.
2. A summary entry MUST cover one exact disjoint raw range.
3. Existing summary bytes MUST NOT be rewritten during a normal fold.
4. Raw history MUST remain durable and reopenable after compaction or K0
   rollover.
5. Current active decisions, valid facts, constraining failed approaches,
   verification state, and open obligations MUST survive compaction.
6. Corrections MUST append supersession or invalidation transitions; they MUST
   NOT erase prior history.
7. Routine execution noise SHOULD be excluded from active semantic state.
8. An expensive observation SHOULD retain an exact evidence ref.
9. A verification MUST identify the artifact state it tested.
10. A later overlapping accepted artifact change MUST mark affected
    verification stale until rerun.
11. Missing evidence MUST NOT be hidden by a numeric confidence value.
12. No vector database, second evidence store, or hidden-reasoning archive MAY be
    introduced solely for agent memory.

## 6. Inspection and retrieval

1. Current state, historical evidence, and repository location MUST have
   separate model-facing interfaces.
2. `inspect_state` MUST read BranchState only and MUST NOT execute effects.
3. `branch_history` MUST read existing events/checkpoints only; it MUST NOT
   re-execute a historical tool call.
4. `repo_query` MUST stay inside the assigned repository root and return bounded
   locations/windows.
5. Canonical tool-history references MUST include task, generation, turn, and
   zero-based batch index. A legacy reference without batch index MAY resolve to
   index zero for previously recorded sessions.
6. State/history cursors MUST be tied to a source watermark. A stale cursor MUST
   fail explicitly or state that it was rebased.
7. Retrieval SHOULD follow progressive disclosure:

   ```text
   SituationFrame -> capsule/state section -> exact ref -> transcript window -> raw artifact
   ```
8. Typed code navigation SHOULD precede broad shell search when it can answer the
   same question.
9. Large tool output SHOULD retain a bounded artifact/spill reference rather
   than be silently discarded.
10. All outputs MUST have deterministic row, item, and byte caps.

## 7. Agent action protocol

1. A model MAY produce a short plan for multi-step work. A small task MAY start
   directly with a tool call or valid finish; planning MUST NOT require an
   otherwise unnecessary provider round trip.
2. The agent SHOULD use the loop:

   ```text
   orient -> locate -> act -> observe -> verify -> externalize -> finish
   ```
3. Plans MAY change after new evidence and MUST NOT be treated as authority.
4. Independent read-only calls MAY run in one batch.
5. Mutating calls MUST execute in declared order and MUST NOT be parallelized
   unless the effect boundary provides a stronger transactional contract.
6. A failed tool call MUST be returned as evidence; the agent SHOULD diagnose it
   before changing source.
7. The model MUST NOT be required to expose hidden chain of thought. Short plans,
   typed actions, claims, and externally useful summaries are sufficient.
8. `objective_met=true` MUST require agreement with task done criteria and
   required current verification.

## 8. Recursive child branches

1. Root and children MUST use the same branch/worker abstraction.
2. A child MAY delegate recursively within supervisor depth, width, budget, and
   lifetime bounds.
3. Every model-originated child proposal MUST contain a self-contained task
   contract and explicit `context_mode` and `placement`.
4. There MUST be no silent downgrade of an explicit policy.
5. `trunk + spread` MUST be rejected as contradictory.
6. `trunk + inherit` MUST require exact provider/model/protocol/reasoning/tool/
   prompt/checkpoint/authorization compatibility.
7. `semantic` MUST import only the immutable semantic state under a fresh
   provider-specific head.
8. `fresh` MUST receive no parent checkpoint, semantic trunk, or parent result
   envelope.
9. `inherit` MUST preserve the parent provider/model lease when known and
   feasible.
10. `spread` MUST remove inherited hard pinning, prefer another feasible lane,
    and fall back to the full hard-feasible set rather than fail solely for lack
    of an alternative.
11. A child MUST NOT widen parent filesystem, tool, credential, or provider
    authority.
12. Admission MUST be durable before child creation/spawn.
13. Parents MUST own child lifetime under structured concurrency unless a named
    policy explicitly transfers ownership.
14. Completion order MUST NOT determine result or artifact join order.

## 9. ResultCapsule and joins

1. A child result MUST be versioned, schema-validated, and bounded.
2. A ResultCapsule SHOULD contain outcome, evidence-linked claims/decisions,
   artifacts, verification, open obligations/blockers, usage, and a recommended
   parent action.
3. A capsule MUST NOT contain the complete child transcript.
4. A parent MAY retrieve exact child history after reading the capsule.
5. Semantic result acceptance MUST NOT imply artifact acceptance.
6. `artifacts.changed=true` MUST NOT imply the parent contains the child change.
7. Parent resume after child code changes MUST require:

   ```text
   child terminal result represented
   AND accepted artifact integration
   AND parent worktree HEAD == accepted integration HEAD
   AND required combined-tree verification state
   ```
8. Conflict evidence MUST be bounded and routed through one ordered resolver
   authority.
9. A failed critical child MUST remain a blocking parent obligation unless the
   parent explicitly changes the plan.

## 10. Resources and providers

1. Provider hard feasibility MUST be resolved before ranking.
2. Credentials, authorization, context/output capacity, required capabilities,
   quota blocks, and artifact/context compatibility MUST remain hard
   constraints.
3. Request rate, in-flight capacity, token windows, wall time, cash, cache
   state, and verification cost MUST remain separate accounting dimensions.
4. Unknown tariff/quota/cache evidence MUST remain unknown.
5. The model SHOULD receive a bounded ResourceEnvelope containing
   decision-relevant pressure and availability, not credentials or raw internal
   scheduler state.
6. Cache warmth MAY be estimated from capability and elapsed time, but a cache
   hit MUST be claimed only from provider evidence.
7. The agent MAY express context/placement/capability intent. The supervisor MUST
   choose the actual provider/model from the feasible set.
8. Root provider migration MUST be an explicit durable transition from a safe
   checkpoint; it MUST NOT be hidden as an ordinary retry.
9. Summaries, children, retrieval, retries, and verification MUST consume
   explicit branch/session budgets.
10. Resource pressure MUST be visible before the next decision when it can
    change the optimal action.

## 11. Human/model control agreement

1. TUI, monitor, status commands, SituationFrame, and `inspect_state` MUST derive
   shared fields from BranchState.
2. At the same watermark they MUST agree on branch lifecycle, generation,
   context lineage, accepted artifact head, children, blockers, verification,
   provider lease, and resources.
3. Operator steering MUST enter through a validated durable event.
4. An accepted steer MUST appear once in the next relevant SituationFrame
   delta/open state.
5. Frontend exit/reconnect MUST NOT require reconstructing state from widget
   memory.
6. Monitoring MUST remain read-only.
7. Color or glyphs MUST NOT be the only representation of semantic state.

## 12. Evaluation and promotion

1. New agent-facing state or policy MUST be evaluated on frozen repository/task
   fixtures with executable acceptance criteria.
2. Correct accepted outcome is the primary metric.
3. Resource claims MUST keep uncached input, cached input, cache write, output,
   summary, retrieval, navigation, verification, cash, quota, and wall time
   separate.
4. Evaluation MUST include long-session compaction, restart, child join,
   provider migration, and reconnect.
5. Promotion MUST require held-out and canary non-inferiority on severe
   correctness failures.
6. Prompt optimization MUST freeze runtime validators, tool schemas, provider
   configuration, repository state, and budgets within a comparison.
7. Training data MUST contain only visible/reproducible decisions and outcomes;
   hidden reasoning and self-reported success MUST NOT become labels.
8. Negative results MUST remain available.

## 13. Current compatibility gaps

These are current-source facts, not exceptions to the target requirements:

- The model-facing `delegate` schema requires `context_mode` and `placement`
  and rejects omission before admission. The remaining ambiguity is internal:
  a harness-originated `proposed_children` spec may omit both, and the
  supervisor's `_declared_child_policy` then falls back to automatic
  exact/semantic compatibility resolution. This internal compatibility path is
  named in the context reference and is not a model-facing default.
- `branch_history` and `repo_query` are active worker tools. Repository queries
  include the configured optional LSP path; internal filenames are not separate
  public tool names. Current fields live in the context/navigation reference.
- `prompts.py` currently exports the coding and summary prompts, not all named
  branch-decision/history components claimed by earlier documents.
- `branch_state.py` and CLI `inspect-state` exist. `observability.py` remains a
  separate operator reducer; a shared canonical model/operator projection and
  automatic SituationFrame are unfinished integration work.
- The existing strict child envelope and SummaryEntry are the migration base for
  ResultCapsule and WorkLedger; target schemas are not current wire claims.

The ordered convergence work is in `../../implementation-plan.md`.

## 14. Acceptance scenarios

### A1 — current-state agreement

```text
Given one durable event prefix and validated Git snapshot
When BranchState, SituationFrame, and TUI snapshots are produced
Then every shared semantic field agrees exactly
```

### A2 — no stale action

```text
Given a child join advances the accepted integration head
When the next model call is built
Then its SituationFrame shows the new head and stales affected verification
```

### A3 — explicit child policy

```text
When a model proposes a child without context_mode or placement
Then model-facing validation rejects the call
And no hidden automatic policy is selected
```

### A4 — exact branch

```text
Given a fully compatible parent checkpoint
When a child requests trunk + inherit
Then the exact prefix is byte-identical
And the parent provider/model lease is inherited
```

### A5 — impossible exact branch

```text
Given an incompatible parent checkpoint
When a child requests trunk + inherit
Then admission rejects it before child_admitted
And it does not silently become semantic or fresh
```

### A6 — progressive recall

```text
Given a capsule omits one required detail
When the model lists child tool refs and opens one exact batched-call ref
Then it obtains the matching action/observation without transcript replay
```

### A7 — obligation retention

```text
Given an open required check
When the branch crosses summary flush, K0 rollover, restart, and reconnect
Then the obligation remains open until matching verification satisfies it
```

### A8 — verification staleness

```text
Given check V passed at artifact head H1
When an overlapping artifact change produces H2
Then V is stale in BranchState, SituationFrame, and TUI until rerun at H2
```

### A9 — resource-aware delegation

```text
Given high delegation overhead and a small local edit
When the model chooses the next action
Then it continues locally unless independent information or critical-path gain
justifies the child
```

### A10 — crash recovery

```text
Given a worker dies after a safe checkpoint and before terminal result
When the branch is recovered
Then accepted artifact/context state is restored
And open obligations and stale verification are represented correctly
```

## 15. Definition of done

```text
[ ] one canonical BranchState reducer and vocabulary
[ ] deterministic bounded SituationFrame before every normal model action
[ ] inspect_state, branch_history, and repo_query wired and bounded
[ ] explicit model-originated child context/placement policy
[ ] evidence-linked current claims, decisions, obligations, and verification
[ ] versioned bounded ResultCapsule
[ ] semantic/artifact/verification joins cannot diverge
[ ] model-visible ResourceEnvelope with unknown-safe semantics
[ ] model and operator projections agree at one watermark
[ ] held-out evaluation proves non-inferior correctness and lower waste
[ ] long soak and fault injection preserve cognitive and artifact state
[ ] source, schemas, prompts, tests, UI, reference docs, and plan agree
```
