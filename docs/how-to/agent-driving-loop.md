# Drive Cambium as an agent

**Status:** target operating guide. The current worker already supports bounded
planning, tools, CAST, explicit child policies when declared, and verification.
The automatic SituationFrame, `inspect_state`, and `repo_query` surfaces
described here are ordered in `implementation-plan.md`.

This guide describes the behavior Cambium should make natural rather than
behavior a model must remember from a long prompt.

## 1. Start by orienting, not searching

Read the SituationFrame in this order:

```text
MISSION    what must become true
ACCEPTED   what context, artifacts, and verification are authoritative
OPEN       what is unfinished, blocked, uncertain, or stale
DELTA      what changed since the previous decision
RESOURCES  which actions are now expensive or infeasible
ANCHORS    where exact supporting detail can be reopened
```

Before the first tool call, be able to state:

```text
objective
observable done criteria
write authority
current accepted artifact head
largest uncertainty
cheapest action that can resolve it
required final verification
```

Do not reconstruct session status from old prose when the frame supplies a
newer harness-owned value.

## 2. Make a control plan

The first plan should be short and falsifiable. Use steps that end in observable
state, not vague activity.

Bad:

```text
understand the code
make improvements
check everything
```

Better:

```text
1. locate the paging API and focused tests
2. reproduce offset=500 against the accepted head
3. repair the smallest owner module
4. run focused then affected-suite verification
5. inspect the diff and finish
```

A plan is a hypothesis. Update it when evidence invalidates a step; do not keep
following a stale plan to appear consistent.

## 3. Locate before reading broadly

Use the least expensive precision ladder:

```text
repo_query tree/symbol/search
    -> exact source windows
    -> read_batch for the selected files
    -> run_shell only when a typed query cannot answer the question
```

When `repo_query` is not yet available, use one bounded shell search or one
batched read rather than serially guessing filenames.

Good read batch:

```json
{
  "type": "tool_call",
  "calls": [{
    "name": "read_batch",
    "arguments": {
      "paths": ["src/cambium/routing.py", "tests/scenarios/test_routing.py"]
    }
  }]
}
```

Batch independent reads that share one purpose. Do not batch mutations whose
order or failure semantics matter.

## 4. Treat tool output as evidence, not conclusion

After every meaningful observation, separate:

```text
observed: command/file/provider returned X
inferred: X likely means Y
unknown: Z still has not been tested
next: smallest action that discriminates between explanations
```

A failed command is not automatically a code defect. It may be a wrong path,
wrong invocation, missing dependency, stale artifact, or genuine failure.
Diagnose before changing source.

When output is truncated, use its retained evidence reference or rerun a more
focused query. Do not reason as though omitted output was empty.

## 5. Keep authority visible

Before an effect, verify:

- the target lies inside the assigned ownership boundary;
- the worktree and branch are the current branch's;
- another child does not own the same write region;
- the accepted artifact head has not advanced past the evidence you read;
- the requested effect is allowed by the current tools and task contract.

Model claims do not update Git state. A child summary does not prove its commit
was integrated. A passed test does not remain current after an overlapping edit.

## 6. Delegate only when it repays coordination

Estimate the child benefit before calling `delegate`:

```text
benefit
  critical-path time avoided
  + independent information gained
  + better provider/model fit
  + reusable exact context

cost
  context construction
  + spawn and queue
  + join and conflict risk
  + parent verification
  + attention required to interpret the capsule
```

Delegate only when benefit clearly exceeds cost.

### Use `trunk + inherit`

Choose this when the child needs the current project decisions, raw tail, or
exact provider-compatible context.

```json
{
  "name": "delegate",
  "arguments": {
    "child_task_id": "repair-parser-window",
    "kind": "feature",
    "spec": {
      "task": "Own src/parser.py and focused parser tests. Reproduce and repair offset paging. Done when focused and existing parser tests pass. Do not edit routing.",
      "context_mode": "trunk",
      "placement": "inherit"
    }
  }
}
```

### Use `semantic + spread`

Choose this for separable work that needs accepted decisions and facts but not
the parent's exact raw tail.

```json
{
  "name": "delegate",
  "arguments": {
    "child_task_id": "audit-terminal-resize",
    "kind": "investigation",
    "spec": {
      "task": "Read-only audit of terminal resize behavior. Return concrete defects, exact evidence refs, and reproductions. Own no production files.",
      "context_mode": "semantic",
      "placement": "spread"
    }
  }
}
```

### Use `fresh + spread`

Choose this when independence is itself the objective: blind review, clean
reproduction, or an attempt to detect correlated assumptions.

```json
{
  "name": "delegate",
  "arguments": {
    "child_task_id": "blind-routing-review",
    "kind": "investigation",
    "spec": {
      "task": "Review routing from source and tests only. Do not rely on parent conclusions. Report claims with exact evidence and no edits.",
      "context_mode": "fresh",
      "placement": "spread"
    }
  }
}
```

Do not create multiple writers for the same files. Use one writer and one
read-only reviewer, or serialize the edits.

## 7. Read child results progressively

Start with the bounded ResultCapsule:

```text
outcome
claims and evidence refs
artifacts and verification
open obligations
recommended next action
```

Drill down only when needed:

```text
inspect_state children
    -> branch_history branches
    -> branch_history tools for one child
    -> branch_history tool for one exact call
    -> bounded transcript window
```

Canonical tool refs include the batch index:

```text
tool:<task>:<generation>:<turn>:<batch-index>
```

Reproduce a child observation in the parent's accepted worktree before making a
high-impact edit when the result depends on code state that may have changed.

## 8. Edit from a proven cause

Before editing, have one of:

- a failing reproduction tied to the accepted head;
- a source invariant violation with exact locations;
- a requested deterministic transformation whose target is unambiguous.

Prefer the smallest effect that removes the cause. Avoid fallbacks, broad
exception handling, retries, defaults, compatibility wrappers, or unrelated
refactors that merely hide the observed failure.

After an edit:

1. inspect immediate lint/tool feedback;
2. read the changed region or diff;
3. run the narrowest relevant check;
4. run the affected suite when the boundary changed;
5. mark earlier overlapping verification stale until rerun.

## 9. Verify in layers

Use a verification ladder:

```text
syntax/static check
    -> focused reproduction
    -> affected module/scenario suite
    -> combined-tree/integration check
    -> full gates when the task or boundary requires them
```

Record the artifact head, command, result, and relevant configuration. “Tests
passed” without those anchors is weak accretive knowledge because it cannot be
matched to later code.

Do not spend full-suite cost before a focused failure is repaired. Do not stop
at a focused test when the change crosses a shared protocol or publication
boundary.

## 10. Externalize what should survive

Before a compaction, delegation, suspension, or finish boundary, ensure the
semantic state contains:

```text
accepted decisions
expensive observations and their refs
failed approaches that constrain future work
artifact changes
verification results and tested head
open obligations with precise next action
facts or decisions that were invalidated
```

Do not preserve routine command syntax mistakes, repeated reads, or abandoned
scratch plans unless they reveal a reusable constraint.

When a prior conclusion changes, invalidate or supersede it explicitly. Do not
simply emit a contradictory sentence and force a future agent to guess which is
current.

## 11. Respond to resource pressure

Use the ResourceEnvelope rather than token anxiety or blind thrift:

- **context high:** inspect exact anchors, finish current reasoning, allow CAST
  to fold; avoid broad transcript replay;
- **turns low:** stop exploratory branches, prioritize done criteria and
  verification;
- **wall low:** cancel non-critical children, run the minimum decisive checks,
  and report unmet obligations honestly;
- **quota high:** keep the root lease if feasible, spread only independent work,
  and avoid speculative calls;
- **cache warm estimate:** favor exact continuation for coupled work, but never
  treat warmth as a cache-hit fact;
- **delegation overhead high:** continue locally unless the child is clearly
  critical-path or independently informative.

Unknown pressure is not low pressure. Use inspection when the decision depends
on it.

## 12. Recover deliberately

After a restart, provider migration, conflict, cancellation, or failed check:

1. read the new SituationFrame rather than assuming continuity;
2. confirm accepted context epoch and artifact head;
3. identify which claims or verification became stale;
4. inspect salvage or exact history only when needed;
5. choose retry, migration, resolver, re-verification, or stop;
6. preserve the failure classification and next action.

A recovery that restores code but loses obligations is incomplete. A recovery
that restores semantic state but resumes on the wrong worktree head is also
incomplete.

## 13. Finish against the contract

Emit `objective_met=true` only when:

```text
all required outcomes are present
AND no blocking obligation remains
AND required verification applies to the accepted artifact head
AND the reported semantic result agrees with accepted Git state
```

A complete investigation that correctly finds no defect may be complete. An
edit with unrun required tests is not. A child still on the critical path is not
complete merely because the parent has nothing else to do.

The final summary should say:

- what changed or what was concluded;
- exact verification performed;
- accepted artifact/branch outcome;
- remaining limitations or open obligations;
- important resource or provider caveats only when they affect trust.

## 14. Example end-to-end loop

```text
SituationFrame: objective=repair paging, head=A, no verification, 31 turns

plan
  locate -> reproduce -> repair -> focused check -> affected suite

repo_query(symbol="read_lines")
  returns src/pager.py:72 and tests/test_pager.py:41

read_batch(two exact files)
  observation: cap is applied before offset slicing
  evidence: tool:root:2:6:0

run_shell(focused failing case)
  observation: offset=500 returns empty; check ref recorded

edit_file(src/pager.py)
  effect: slice before cap

run_shell(focused case)
  verification passed at head/worktree state B

run_shell(affected suite)
  verification passed at B

inspect_state(open)
  no blockers; obligations satisfied

finish(objective_met=true)
```

The ideal system makes this path shorter than an improvised sequence, keeps the
state legible after compaction or restart, and leaves enough evidence for the
next branch to trust or challenge the result without paying to rediscover it.
