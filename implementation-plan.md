# Implementation plan

Ordered open work only. This file is not a merge log. Source, tests, durable
records, and accepted Git state decide what is implemented. A completed step is
removed or moved to the appropriate architecture/reference document rather than
left here as a historical diary.

The target system is defined in
[`docs/architecture/agent-operating-model.md`](docs/architecture/agent-operating-model.md).

## Goal

Make Cambium a coherent cognitive control system in which an agent can cheaply
and accurately answer:

```text
What is the mission?
What is authoritative now?
What changed?
What remains uncertain or unfinished?
What may I change?
Which evidence should I inspect next?
What do my actions cost?
What must be verified before I can finish?
```

The implementation strategy is additive and reviewable. Reuse existing events,
checkpoints, CAST, Git, provider leases, quota records, and TUI reducers. Do not
introduce another memory database, scheduler, worker class hierarchy, or hidden
reasoning store.

## Current baseline

Current `main` already provides:

- isolated/fenced workers, generation lifecycle, salvage, and ref-only Git
  publication;
- durable events and immutable checkpoints;
- append-only semantic summaries and K0 rollover;
- provider leases, usage debt, cache capabilities, quota reservations, and
  call-time failover;
- static and dynamic task trees, explicit child context/placement behavior when
  declared, transactional joins, and resolver support;
- a persistent interactive branch and event-sourced operator projection;
- implementations of branch-history projection, bounded code indexing, and
  optional one-shot LSP queries.

Current coherence gaps:

- the model does not receive a canonical current-state projection;
- `observability.py` serves the human surface but is not the shared branch-state
  owner;
- child policy can still be omitted and silently enter the automatic
  compatibility path, while normative docs describe explicit policy;
- `branch_history.py`, `code_index.py`, and `lsp_query.py` are not in the active
  six-tool worker roster;
- docs claim named branch-decision/history prompt components that
  `prompts.py` does not currently expose;
- semantic summaries preserve useful strings but do not assign stable
  evidence-linked identities to claims, decisions, obligations, and
  verification;
- child results do not yet form the target versioned ResultCapsule;
- provider/resource state is observable to the operator but not presented to
  the agent as a bounded decision surface.

## Sequencing rule

Each phase must land as the smallest end-to-end vertical slice:

```text
pure value/reducer
    -> source integration
    -> durable event or replay proof
    -> model/operator projection
    -> focused scenario
    -> affected fast and slow gates
    -> documentation truth update
```

Do not build a broad abstraction without one live consumer. Do not change prompt
policy before the facts it relies on are deterministic and measurable.

The live coding gate outranks every phase. `tests/acceptance/test_live_coding_gate.py`
runs one real provider-backed coding task through `run_plan` and asserts the
commit, the tool calls, and the durable usage events. Any phase that touches the
execution loop ends with that gate run against a real provider; a phase whose
only green evidence is synthetic (marker fixtures, loopback stubs) is BLOCKED,
not done.

---

## Phase 0 — converge repository truth

### Purpose

Remove ambiguity before adding another layer. Every active document, schema,
prompt, and event must use one vocabulary for current behavior and target work.

### Work

1. Decide the public child-policy contract:
   - preferred target: every model-originated `delegate` proposal explicitly
     declares `context_mode` and `placement`;
   - remove omission from the model schema and prompt;
   - if harness-originated automatic compatibility remains necessary, give it
     an explicit internal policy name and event value rather than overloading
     missing fields.
2. Align `child_policy.py`, `schemas.py`, `supervisor.py`, tests, reference docs,
   and TUI lineage values.
3. Correct prompt documentation:
   - either add real, separately exported prompt components for branch
     decisions and history recall;
   - or remove claims that they already exist until they do.
4. Decide the three dormant model-facing capabilities:
   - wire `branch_history.py`, `code_index.py`, and `lsp_query.py` through the
     active tool schema/dispatch path;
   - or explicitly mark them library-only and remove worker-facing claims.
   Phase 3 below assumes they are retained and wired.
5. Add one generated source-map/truth check that verifies documented tool names,
   child-policy enum values, prompt exports, and public lifecycle values against
   source. It must not attempt to validate prose.
6. Remove stale workflow/codemod scaffolding that looks like active product
   behavior but is not part of current CI or runtime.

### Acceptance

```text
[ ] delegate schema and normative reference agree exactly
[ ] omitted policy has no undocumented semantics
[ ] prompt exports named in docs exist in source
[ ] active worker tool roster matches README and architecture
[ ] public lineage/lifecycle values are defined once
[ ] no active doc labels target behavior as implemented
[ ] fast and slow suites pass
```

### Non-goal

Do not introduce SituationFrame or new semantic state in this phase.

---

## Phase 1 — canonical BranchState reducer

### Purpose

Create one pure materialized branch state that all later model and operator
surfaces consume.

### Work

1. Add a small `branch_state.py` (name may change once) containing immutable
   value objects and a pure reducer over ordered durable events.
2. Represent:
   - identity and lifecycle;
   - mission and authority;
   - context epoch/checkpoint/lineage;
   - base, worktree, and accepted integration heads;
   - provider/model lease;
   - plan/current step;
   - blockers and open obligations available from current records;
   - children in deterministic admission order;
   - usage and resource facts;
   - stable evidence anchors.
3. Keep Git-derived fields explicit. The reducer may receive a validated Git
   snapshot input; it must not run Git internally.
4. Refactor `observability.py` to project its `AgentSnapshot`, `ContextSnapshot`,
   and `SessionSnapshot` from BranchState instead of maintaining a parallel
   semantic model.
5. Preserve replay determinism and existing public TUI output unless a field was
   already inconsistent.
6. Add versioning and canonical serialization for projection tests. The
   serialization is not a persistence authority.

### Acceptance

```text
[ ] identical event/Git inputs produce byte-identical BranchState JSON
[ ] randomized event replay respecting sequence produces the same final state
[ ] task, context, Git, provider, and child identities remain independent
[ ] model-proposed text is never promoted to accepted artifact/provider fact
[ ] current TUI/monitor reducer tests pass through the new owner
[ ] no new database or mutable global state exists
```

### Dependency

Phase 0.

---

## Phase 2 — deterministic SituationFrame

### Purpose

Give the model the smallest accurate operating picture before every decision.

### Work

1. Implement a pure `render_situation_frame(BranchState, limits)` function with
   the canonical section order:

   ```text
   MISSION, AUTHORITY, ACCEPTED, DELTA, OPEN, CHILDREN, RESOURCES, ANCHORS
   ```

2. Add explicit whole-frame and per-section byte/item caps. Truncation must name
   the omitted section and include an `inspect_state` continuation anchor.
3. Build the frame immediately before each provider call from the latest
   reduced event prefix and validated worktree/Git snapshot.
4. Append it as a late user-role control message so the stable system/tool head
   and existing CAST entries remain byte-identical.
5. Record a bounded `situation_frame_built` event containing:
   - projection version;
   - source watermark;
   - frame SHA-256 and byte count;
   - branch/generation/epoch/artifact identity;
   - truncated section names.
   Do not duplicate the full frame when deterministic replay can reconstruct it.
6. Ensure summary mode never treats the harness frame as model-owned semantic
   evidence. It may extract changed obligations only through explicit accepted
   state transitions.
7. Include the frame digest/watermark in provider-call accounting so an action
   can be matched to what the model saw.

### Acceptance

```text
[ ] every normal model action has one frame digest and source watermark
[ ] same state renders the same frame
[ ] frame contains no secrets or hidden reasoning
[ ] unknown and stale fields remain visibly unknown/stale
[ ] frame stays under the hard cap under maximum children/obligations
[ ] adding the frame does not rewrite the provider-cacheable prefix
[ ] a fresh completed tool/child event is visible on the next call
[ ] model and operator shared fields match exactly
```

### Dependency

Phase 1.

---

## Phase 3 — agent inspection and repository navigation

### Purpose

Make common information needs direct and bounded instead of forcing shell
searches or transcript reconstruction.

### Work

#### 3A. `inspect_state`

1. Expose current BranchState sections through one read-only tool.
2. Support bounded `mission`, `authority`, `accepted`, `open`, `children`,
   `resources`, `knowledge`, and `anchors` views.
3. Use opaque continuation cursors tied to the state watermark. A cursor against
   a different state fails as stale rather than returning mixed data.

#### 3B. `branch_history`

1. Wire the existing implementation into `schemas.py`, `tools.py`, worker init,
   prompt text, provider tool hashes, and scenario fixtures.
2. Preserve stable branch/tool refs and current deterministic row/byte limits.
3. Keep it read-only and reconstructive: no second index/database and no
   historical tool re-execution.

#### 3C. `repo_query`

1. Wrap `code_index.py` and optional `lsp_query.py` behind one typed repository
   navigation tool.
2. Offer portable tree/symbol/search/window actions without an LSP.
3. Offer definition/reference/hover/diagnostic actions when the configured
   one-shot LSP supports them.
4. Return compact locations and evidence refs; use `read_batch` for the exact
   source after location.
5. An unavailable LSP may fall back only to a semantically equivalent bounded
   portable query. Otherwise return explicit unsupported status.

### Acceptance

```text
[ ] active tool roster includes inspect_state, branch_history, and repo_query
[ ] provider tool-schema hash changes intentionally and exact-fork tests cover it
[ ] state cursors cannot cross watermarks silently
[ ] historical refs reopen the exact recorded action/observation
[ ] repo_query never scans outside its declared repository root
[ ] common symbol-location fixtures require fewer returned bytes than shell search
[ ] LSP absence is explicit and portable behavior remains deterministic
```

### Dependency

Phase 2 for `inspect_state`; Phase 0 for history/navigation truth convergence.

---

## Phase 4 — accretive WorkLedger

### Purpose

Preserve current decisions, expensive evidence, failed approaches,
verification, and open work without retaining transcript noise or trusting
untyped prose as environmental truth.

### Work

1. Define immutable item values:
   - Observation;
   - Claim (`observed`, `inferred`, or `hypothesis` basis);
   - Decision;
   - Obligation;
   - Verification.
2. Assign stable harness identities and source references.
3. Derive the first `WorkLedger` from existing `SummaryEntry` fields. Use
   conservative identity: do not merge similar strings by fuzzy matching.
4. Persist item transitions as append-only deltas:
   - claim accepted/invalidated;
   - decision active/superseded;
   - obligation open/blocked/satisfied/cancelled;
   - verification passed/failed/stale.
5. Connect artifact changes to verification staleness. A later overlapping
   accepted artifact change marks affected checks stale until rerun.
6. Teach K0 rollover to preserve active decisions, valid facts, constraining
   failed approaches, current verification, open obligations, and their compact
   evidence anchors.
7. Add knowledge/open-work summaries to BranchState and SituationFrame.
8. Evolve the model summary schema only after the compatibility projection is
   measured. If explicit IDs are needed, introduce a new version rather than
   mutating old entries.

### Acceptance

```text
[ ] open obligations survive flush, K0 rollover, restart, and reconnect
[ ] invalidated/superseded items never appear as current
[ ] a verification is tied to the artifact head it tested
[ ] overlapping accepted edits stale relevant verification
[ ] every current non-trivial claim can expose its source or inferred status
[ ] raw history remains immutable and reconstructible
[ ] active context stays within existing bounds
```

### Dependency

Phases 1–3.

---

## Phase 5 — versioned ResultCapsule and branch control

### Purpose

Make child work cheap to understand, safe to integrate, and controllable while
preserving the existing semantic/artifact join invariant.

### Work

1. Introduce `ResultCapsule` version 2 containing:
   - status and concise outcome;
   - claim/decision references;
   - changed artifacts and child head;
   - verification and tested head;
   - open obligations/blockers;
   - usage;
   - recommended parent action.
2. Adapt the current strict envelope during migration. Do not widen the wire
   ad hoc; version, validate, bound, and preserve old readers.
3. Add typed child task contract fields once the capsule is stable:
   objective, ownership, done criteria, verification, context policy, placement,
   and budget. Preserve a rendered task string for provider compatibility.
4. Expose admission index, critical-path status, owner scope, and join state in
   BranchState/SituationFrame/TUI.
5. Add explicit branch control actions at the supervisor boundary:
   - cancel child;
   - mark non-critical/detach result wait where safe;
   - request bounded inspection;
   - queue parent steering for resume.
6. Preserve structured concurrency: a parent owns child lifetime unless a
   named session-level policy explicitly transfers ownership.
7. Keep semantic and artifact joins distinct. The parent resumes with code
   authority only after accepted integration and combined-tree requirements.

### Acceptance

```text
[ ] capsule size remains bounded under maximum child output
[ ] parent can understand a normal child without transcript replay
[ ] capsule artifact fields cannot forge accepted parent integration
[ ] child completion order does not change join order or final state
[ ] cancellation has one durable acknowledged transition
[ ] critical child failure remains a blocking obligation
[ ] parent reads child-created symbols immediately after an accepted join
```

### Dependency

Phase 4.

---

## Phase 6 — ResourceEnvelope and measured scheduling

### Purpose

Let the agent make resource-aware choices while keeping provider selection and
credentials under supervisor ownership.

### Work

1. Derive a bounded ResourceEnvelope from existing budgets, provider lease,
   cache capability/elapsed time, quota reservations, usage debt, and measured
   routing evidence.
2. Present pressure classes plus exact drill-down values:
   - turns and wall;
   - context and uncached-token pressure;
   - cache affinity and warm estimate;
   - quota and cash pressure;
   - delegation overhead;
   - alternative feasible lane availability.
3. Version pressure thresholds. Missing evidence produces `unknown`.
4. Replace placeholder 20M token-window assumptions with observed or configured
   account windows. Keep request rate, in-flight capacity, token quota,
   subscription windows, and prepaid cash as separate dimensions.
5. Add explicit quota/cash reservations where the provider contract supports
   them. Do not spend OpenRouter cash or scarce subscription capacity without a
   visible reservation policy.
6. Estimate switching and delegation cost from measured data, not hardcoded
   persuasive constants.
7. Make root provider migration explicit:
   safe checkpoint -> lease migration decision -> context construction -> event
   -> new lease. Never hide it as a routine retry.
8. Keep exact provider-cache hits provider-reported. Cache warmth is only an
   estimate used before the call.

### Acceptance

```text
[ ] every pressure value is measured/configured or unknown
[ ] request rate, concurrency, tokens, time windows, and cash stay separate
[ ] agent sees resource changes before choosing the next action
[ ] root migration is durable and replayable
[ ] spread children prefer useful idle capacity without violating feasibility
[ ] no credential or secret enters the frame
[ ] routing decisions can be explained from hard filters plus recorded scores
```

### Dependency

Phases 1, 2, and 5. Real-account evaluation follows Phase 8.

---

## Phase 7 — one control plane for human and model

### Purpose

Remove divergence between what the operator sees and what the model knows.

### Work

1. Render TUI/monitor/session status from BranchState only.
2. Add a frame-inspection command that shows the exact SituationFrame metadata
   and optionally the rendered frame for the selected branch.
3. Make `/agents`, `/context`, `/session`, `/quota`, and future `/open` views use
   the same public field vocabulary as `inspect_state`.
4. Represent operator steering as validated durable events:
   new constraint/priority, queued follow-up, branch cancellation, provider
   preference, inspection request, fork/new branch.
5. Apply accepted steering at one branch boundary and expose it in the next
   SituationFrame `DELTA`/`OPEN` sections.
6. Preserve current primary-buffer scrollback, deterministic layout,
   reconnect, non-TTY behavior, and event replay.
7. Show stale/unknown state explicitly rather than retaining the last attractive
   value after evidence expires.

### Acceptance

```text
[ ] same watermark yields identical shared model/operator fields
[ ] operator can inspect which frame preceded a model action
[ ] steering is acknowledged once and appears in the next frame
[ ] reconnect rebuilds state without a hidden frontend cache
[ ] monitor remains read-only
[ ] narrow and non-TTY layouts preserve semantic status in text
```

### Dependency

Phases 1–6.

---

## Phase 8 — evaluation, optimization, and policy promotion

### Purpose

Prove that the added control structure improves accepted outcomes per resource
rather than merely producing more metadata.

### Work

1. Implement the paired variants and metrics in
   `docs/research/agent-system-evaluation.md`.
2. Freeze multi-repository task fixtures with starting commits, ownership,
   done criteria, relevant source, critical knowledge items, and resource
   budgets.
3. Measure:
   - orientation efficiency and stale-state actions;
   - navigation precision and bytes read;
   - history retrieval precision;
   - knowledge/obligation retention;
   - delegation contribution and join cost;
   - resource use and completion;
   - model/operator state agreement;
   - recovery after injected failures.
4. Extract only visible decisions/outcomes into reviewed datasets. Do not store
   hidden reasoning or accept self-reported model success as a label.
5. Add named optimizable prompt components only after their input state is
   stable:
   - orientation;
   - repository location;
   - delegation benefit;
   - context mode and placement;
   - history recall stopping;
   - verification depth;
   - finish decision;
   - semantic summarization.
6. Freeze runtime validators, tool schemas, provider configuration, and budgets
   during a prompt comparison.
7. Promote only held-out, canary-safe gains. Preserve negative results.

### Acceptance

```text
[ ] severe correctness failures are non-inferior
[ ] stale-state and redundant orientation actions decrease
[ ] relevant source/evidence is found with fewer resources
[ ] obligation loss and stale verification decrease
[ ] delegation improves critical path or information efficiency
[ ] gains survive multiple repositories and provider conditions
[ ] every promoted artifact has a reproducible report and frozen baseline
```

### Dependency

Each feature can run its own paired gate before later phases; complete-system
promotion follows Phases 1–7.

---

## Phase 9 — soak, fault injection, and documentation lockstep

### Purpose

Prove the tower remains coherent over long sessions and hostile boundary timing.

### Work

1. Run multi-hour and hundreds-of-turn sessions across repeated summary flushes,
   K0 rollovers, child joins, provider lease changes, and reconnects.
2. Inject worker death, provider stalls, Retry-After storms, OAuth expiry,
   SQLite busy/disk-full, checkpoint publication interruption, cancellation
   races, merge conflicts, and external ref advancement.
3. Assert recovery restores both cognitive state and artifact state:
   no lost obligation, no stale accepted claim, no stale verification presented
   as current, no orphaned worktree authority, and no duplicate publication.
4. Add a lightweight docs/source drift gate for public enum/tool/prompt/event
   identifiers and generated source maps.
5. Keep `docs/README.md` as the active map. Superseded plans/reviews remain in
   Git history, not beside current contracts.

### Acceptance

```text
[ ] deterministic BranchState after crash/replay
[ ] no obligation loss across long-session compaction
[ ] no model/operator shared-field disagreement
[ ] no publication without matching accepted artifact state
[ ] no orphaned workers/worktrees after cancellation or crash
[ ] all fast, slow, fault, and credential-gated acceptance matrices pass
[ ] active documentation matches the executable vocabulary
```

---

## Definition of done

Cambium reaches the target operating model when:

```text
[ ] one canonical BranchState owns the derived branch truth
[ ] every model action receives a bounded deterministic SituationFrame
[ ] current state, old evidence, and repository location have distinct tools
[ ] expensive findings and open obligations accrete with stable references
[ ] child results are bounded, versioned, evidence-linked capsules
[ ] semantic and artifact joins cannot diverge
[ ] resource and provider constraints are visible without exposing authority
[ ] human and model projections agree at the same watermark
[ ] recovery restores both cognitive and filesystem state
[ ] measured policies improve accepted outcomes per resource on held-out tasks
[ ] source, schemas, prompts, tests, UI, reference docs, and plans use one vocabulary
```
