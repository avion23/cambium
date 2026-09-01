# Implementation plan

Ordered open work only. Source, executable scenarios, durable records, and
accepted Git state decide what is implemented. Completed work belongs in current
contracts or Git history, not in this file.

The target operating model is defined in
[`docs/architecture/agent-operating-model.md`](docs/architecture/agent-operating-model.md).

## Goal

Make Cambium a coherent cognitive control system in which an agent can cheaply
answer:

```text
What is my mission and authority?
What state is accepted now?
What changed and what remains open?
Which evidence should I inspect next?
What will the next action cost?
What must be verified before I finish?
```

Reuse current events, checkpoints, CAST, Git objects, provider leases, quota
records, and TUI reducers. Do not add another memory store, scheduler, worker
class hierarchy, compatibility wrapper, or implementation with no live
consumer.

## Current baseline

Current runtime provides:

- isolated and generation-fenced workers;
- worktree-confined `write_file` and `edit_file` effects;
- ref-only Git publication, child integration, salvage, and recovery;
- durable events and immutable checkpoints;
- append-only semantic summaries and K0 rollover;
- provider admission, leases, call-time failover, usage debt, and quota records;
- static and dynamic task trees with bounded recursive children;
- explicit model-originated child context/placement policy across schema,
  prompt, call-time tool validation, and supervisor admission;
- a persistent interactive branch and event-sourced operator projection;
- six active model tools: `write_file`, `edit_file`, `git_op`, `run_shell`,
  `read_batch`, and `delegate`;
- library implementations for branch-history projection, bounded code indexing,
  and optional one-shot LSP queries.

Current gaps:

- harness-originated static proposals may still use automatic compatibility when
  both policy fields are absent; that mode has no explicit wire/event identity;
- no canonical BranchState is shared by model, TUI, monitor, and recovery;
- no deterministic SituationFrame is supplied before each model decision;
- `branch_history.py`, `code_index.py`, and `lsp_query.py` are not wired through
  the active model schema/dispatcher/prompt/tool hash;
- `inspect_state` has no implementation;
- named orientation/history/location prompt components have no live callers;
- semantic summaries do not assign stable evidence-linked identities to claims,
  decisions, obligations, and verification;
- child results are not the target versioned ResultCapsule;
- provider/resource state is visible to operators but not presented to the
  model as one bounded decision surface.

## Sequencing rule

Every phase lands in the smallest end-to-end slice:

```text
value or reducer
    -> owning runtime path
    -> durable observation
    -> model or operator projection
    -> focused scenario through the public boundary
    -> affected fast/slow verification
    -> documentation truth update
```

Do not prebuild alternate paths or tests for a later phase. A helper is justified
only by the live slice that consumes it. Any change to the execution loop also
runs the credential-gated live coding acceptance gate.

---

## Phase 0 — finish public-contract convergence

### Work

1. Decide the harness-originated automatic child mode:
   - preferred: give every internal proposal an explicit policy before
     admission; or
   - define one explicit internal policy value and durable event representation.
   Missing fields must stop carrying long-term behavior.
2. Align static proposal producers, supervisor materialization, fixtures, event
   values, TUI lineage, and references with that decision.
3. Keep model `context_mode` and `placement` required at schema, prompt, tool,
   and supervisor boundaries.
4. Keep the active tool roster defined once and project it into documentation.
5. Export a named prompt decision component only when a live caller and
   evaluation consume it.
6. Add a narrow generated vocabulary check only if it compares structured
   identifiers such as enum/tool/event names; it must not lint prose.

### Acceptance

```text
[ ] every child path has one explicit policy representation
[ ] omission carries no runtime semantics
[ ] public lineage values are defined once
[ ] every documented prompt export has a live consumer
[ ] active tool names match schema, dispatcher, hash, and docs
[ ] fast and slow suites pass
```

### Non-goal

Do not introduce BranchState or SituationFrame here.

---

## Phase 1 — canonical BranchState

### Work

1. Add one immutable value model and pure reducer over ordered durable events
   plus an explicit validated Git snapshot.
2. Represent branch identity/lifecycle, mission/authority, context epoch,
   artifact heads, provider lease, plan/open work, children, usage/resources,
   verification, and stable evidence anchors.
3. Refactor `observability.py` to derive operator snapshots from this reducer
   rather than maintaining parallel semantics.
4. Version canonical serialization for deterministic replay tests. The
   serialization is a projection, not persistence authority.

### Acceptance

```text
[ ] identical inputs produce byte-identical BranchState JSON
[ ] replay produces the same final state
[ ] task, context, Git, provider, and child identities remain independent
[ ] model text cannot become accepted artifact or provider fact
[ ] existing TUI and monitor scenarios pass through the new owner
[ ] no new database or mutable global state exists
```

### Dependency

Phase 0.

---

## Phase 2 — deterministic SituationFrame

### Work

1. Render BranchState in fixed order:

   ```text
   MISSION, AUTHORITY, ACCEPTED, DELTA, OPEN, CHILDREN, RESOURCES, ANCHORS
   ```
2. Bound the whole frame and each section. Truncation names the omitted section
   and supplies an `inspect_state` continuation.
3. Build the frame immediately before each provider call from the latest event
   watermark and validated Git state.
4. Append it as a late control message; never rewrite the stable system/tool
   head or prior CAST entries.
5. Record only projection version, watermark, identity, digest, size, and
   truncation metadata when the frame is reproducible.
6. Keep harness state out of model-authored semantic summaries.

### Acceptance

```text
[ ] every normal model action has one frame digest and watermark
[ ] equal state renders equal bytes
[ ] unknown and stale values stay explicit
[ ] secrets and hidden reasoning never enter the frame
[ ] frame remains within hard bounds at maximum branch width
[ ] model and operator shared fields agree at one watermark
```

### Dependency

Phase 1.

---

## Phase 3 — direct inspection tools

Implement each tool only as an end-to-end vertical slice. Reuse the existing
history/index/LSP libraries where they satisfy the live contract; do not create
duplicate implementations.

### 3A. `inspect_state`

Expose bounded BranchState sections with opaque cursors tied to one source
watermark. A cursor used against another watermark fails as stale.

### 3B. `branch_history`

Wire the existing branch-history projection through `schemas.py`, `tools.py`,
worker initialization, prompt text, provider-tool hashes, bounded durable
observations, and public scenarios.

Use canonical refs:

```text
tool:<task-id>:<generation>:<turn>:<batch-index>
```

Reading history never re-executes a tool and creates no second store.

### 3C. `repo_query`

Wrap the existing bounded code-index primitives behind one typed repository
navigation tool. Start with tree, symbol/search locations, references, and source
windows inside the repository root. Add the existing one-shot LSP boundary only
for explicitly supported definition/reference/diagnostic actions whose measured
benefit justifies the process cost.

Unavailable capabilities return explicit unsupported results; they do not
silently perform a different operation.

### Acceptance

```text
[ ] active roster includes only fully wired tools
[ ] state cursors cannot cross watermarks
[ ] history refs reopen exact recorded observations
[ ] history reads never execute effects
[ ] repo_query cannot escape its repository root
[ ] returned navigation bytes beat equivalent shell search on fixtures
[ ] provider tool-schema hash changes intentionally and is covered
```

### Dependency

Phase 2 for `inspect_state`; Phase 0 for history/navigation contract convergence.

---

## Phase 4 — accretive WorkLedger

### Work

1. Derive immutable Observation, Claim, Decision, Obligation, and Verification
   items from current durable records and `SummaryEntry` fields.
2. Assign stable harness identities and evidence refs. Do not fuzzy-merge
   similar strings.
3. Express acceptance, invalidation, supersession, satisfaction, cancellation,
   failure, and staleness as append-only transitions.
4. Tie verification to artifact head and scope; later overlapping accepted
   changes mark it stale.
5. Preserve active decisions, valid facts, constraining failed approaches,
   current verification, and open obligations through flush, K0 rollover,
   restart, and reconnect.
6. Change the model summary schema only through a measured versioned migration.

### Acceptance

```text
[ ] open obligations survive every context boundary
[ ] invalidated and superseded items are not projected as current
[ ] verification cannot outlive an overlapping artifact change
[ ] every current non-trivial claim has evidence or an inferred label
[ ] raw history remains immutable and reconstructible
[ ] active context remains within existing bounds
```

### Dependency

Phases 1–3.

---

## Phase 5 — ResultCapsule and branch control

### Work

1. Add one bounded versioned child ResultCapsule containing outcome, evidence
   refs, artifact head/files, verification and tested head, open obligations,
   blockers, usage, and recommended parent action.
2. Preserve semantic acceptance and artifact integration as separate supervisor
   decisions.
3. Add typed child task fields only where capsule and supervisor consume them:
   objective, ownership, done criteria, verification, context policy,
   placement, and budget.
4. Expose deterministic admission order, critical-path status, join state, and
   accepted artifact state in BranchState/SituationFrame/TUI.
5. Add cancellation or steering only at the existing supervisor ownership
   boundary and record one durable acknowledged transition.

### Acceptance

```text
[ ] capsule size is bounded
[ ] parent normally needs no child transcript replay
[ ] capsule fields cannot forge accepted integration
[ ] completion order cannot change join order
[ ] cancellation is acknowledged once
[ ] critical child failure remains a blocking obligation
```

### Dependency

Phase 4.

---

## Phase 6 — ResourceEnvelope

### Work

1. Derive a bounded view from existing budgets, provider lease, cache
   capability, quota reservations, usage debt, and measured routing evidence.
2. Keep turns, wall time, context, uncached tokens, request rate, concurrency,
   token quota, cash, cache affinity, and alternative lanes as separate facts.
3. Version pressure thresholds; missing evidence is `unknown`.
4. Replace placeholder account limits with configured or observed values.
5. Estimate provider switching and delegation cost from measurements, not prompt
   prose or hardcoded persuasive constants.
6. Make root provider migration an explicit checkpointed transition.

### Acceptance

```text
[ ] every resource value is measured, configured, or unknown
[ ] hard provider feasibility still precedes scoring
[ ] resource changes are visible before the next model decision
[ ] root migration is durable and replayable
[ ] no credential enters the model projection
```

### Dependency

Phases 1, 2, and 5.

---

## Phase 7 — one human/model control plane

### Work

1. Render TUI, monitor, session status, and SituationFrame from BranchState.
2. Let operators inspect frame metadata that preceded a selected action.
3. Use one public vocabulary in `/agents`, `/context`, `/session`, `/quota`,
   future `/open`, and `inspect_state`.
4. Represent accepted operator steering as validated durable events applied at
   one branch boundary.
5. Preserve terminal scrollback, deterministic layout, reconnect, non-TTY
   behavior, and text labels independent of color.

### Acceptance

```text
[ ] one watermark yields identical shared human/model fields
[ ] steering appears once in the next frame
[ ] reconnect needs no hidden frontend cache
[ ] monitor remains read-only
[ ] narrow and non-TTY views retain semantic status
```

### Dependency

Phases 1–6.

---

## Phase 8 — evaluation and policy promotion

### Work

1. Run paired held-out tasks from
   [`docs/research/agent-system-evaluation.md`](docs/research/agent-system-evaluation.md).
2. Measure accepted completion, stale-state actions, bytes read, history
   precision, obligation retention, delegation contribution, resource use,
   recovery, and human/model agreement.
3. Optimize only visible decision artifacts and outcomes; never hidden
   reasoning or model self-reported success.
4. Freeze validators, schemas, provider configuration, and budgets during a
   prompt comparison.
5. Add a prompt component only when deterministic inputs and a runtime caller
   already exist.
6. Promote only held-out, canary-safe gains and preserve negative results.

### Acceptance

```text
[ ] severe correctness failures are non-inferior
[ ] stale and redundant actions decrease
[ ] relevant evidence is found with fewer resources
[ ] obligations and verification survive context boundaries
[ ] gains generalize across repositories and provider conditions
[ ] every promoted policy has a reproducible frozen report
```

### Dependency

Each landed feature runs its paired gate; whole-system promotion follows
Phases 1–7.

---

## Phase 9 — soak and fault injection

After the core control plane exists, run long sessions across repeated summary
flushes, K0 rollovers, child joins, reconnects, and provider changes. Inject
worker death, provider stalls, OAuth expiry, SQLite failure, checkpoint
interruption, cancellation races, merge conflicts, and external ref advances.

Acceptance requires deterministic recovery of cognitive and artifact state: no
lost obligation, stale verification presented as current, orphaned authority,
or duplicate publication.

---

## Definition of done

```text
[ ] one BranchState owns derived branch truth
[ ] every model action receives one bounded SituationFrame
[ ] current state, history, and repository location have distinct tools
[ ] expensive findings and obligations accrete with stable references
[ ] child results are bounded, versioned, evidence-linked capsules
[ ] semantic and artifact joins cannot diverge
[ ] resources are visible without exposing credentials or scheduler authority
[ ] human and model projections agree at one watermark
[ ] recovery restores cognitive and filesystem state
[ ] measured policies improve accepted outcomes per resource
[ ] source, schemas, prompts, tests, UI, references, and plan agree
```
