# Agent operating model

**Status:** design rationale. The [runtime map](architecture.md) states what is
implemented; the [open plan](../../implementation-plan.md) lists the gaps.

## Purpose

Cambium is a small harness around model calls, tools, isolated worktrees, and
reusable context. It should finish useful work with little orchestration and
little repeated input. It is not a framework for constructing a second control
system around an agent.

Optimize correct completed work per wall-clock second and per account budget.
Output tokens/second measures provider service speed; tokens/week measures a
resource allowance, not usefulness. Burning more tokens or spawning more agents
is not an improvement by itself. Count retries, summaries, duplicated reads,
verification, and integration in the cost of a result.

## The unit of work

A root and a child use the same worker:

```text
task + worktree + context + provider lease + resource budget
    -> model action -> tool observation -> next action
    -> result + verified artifact change, when applicable
```

The model proposes work. The harness executes it and records what happened.
Keep the code direct: explicit data, ordinary functions, local ownership,
existing implementations before new abstractions, standard library before new
dependencies. Split a large function at a real ownership boundary, not to add
another layer of interfaces.

**Child agent** describes a task's parent/child relationship. **Subagent** is the
ordinary name for an agent doing delegated work. Neither names a separate
runtime class, a provider tier, or a context policy. In particular, a child does
not necessarily get the complete parent context and a subagent is not
necessarily stateless. Choose context and placement explicitly; see
[context branches](context-branches.md).

## What belongs in the harness

Keep deterministic mechanics outside prompts: process lifetime, filesystem
ownership, provider configuration, usage accounting, checkpoint identity, and
Git publication. A model saying that a test passed or a child was merged is not
a substitute for the corresponding observation.

These checks protect real invariants. They do not justify approval workflows,
role hierarchies, extra databases, or a new validation service. Do not add a
certificate, digest, receipt, or policy phase unless an actual consumer needs
it to detect a specific failure.

A small task can start with a tool call or finish. Planning is useful for
multi-step work, not a mandatory paid round trip. A read-only answer does not
need a code edit. A code edit needs relevant verification, not every available
check regardless of scope.

## Context is a working set

The model is stateless. Cambium retains a stable instruction/tool head, an
append-only semantic trunk, and a recent raw tail. Earlier detail remains in
checkpoints and events and can be requested when needed. Provider caching only
accelerates sending that context; it never supplies correctness state.

Prefer a source location and a small window over dumping a file. Prefer one
historical tool result over replaying a transcript. New evidence is appended at
the end of the request; it must not rewrite the stable instruction head.

The detailed persistence and compaction contract belongs in
[context engine](context-engine.md), not in every agent document.

## Delegation must repay its cost

Continue locally when work is small, tightly coupled, or likely to require
several clarification round trips. Delegate when independent work can shorten
the critical path or provide genuinely independent evidence.

Each child needs an objective, ownership boundary, completion check, and
explicit context/placement. The parent owns its lifetime and integrates its
result. A shared provider lane can make delegation slower even when there are
many worker slots. A different provider can help only when the work is separable
and its capabilities and available quota fit.

Do not create permanent researcher/reviewer/planner subclasses. Task text and
explicit policy express those roles without another orchestration mechanism.

## Providers are resources

Separate request rate, concurrent calls, output service speed, account token
windows, cash, context capacity, and cache affinity. One number cannot honestly
represent all of them. Reuse a viable root lease for coupled work; spread
independent work when another usable lane saves time or scarce quota.

Unknown quota is unknown, not an invented weekly allowance. A cache hit requires
provider evidence. A total-token count is not an output-token rate. Current
ranking limitations and the real quota implementation are documented in
[provider routing](provider-routing.md).

## State and interface discipline

Durable events, immutable checkpoints, and Git objects are the existing sources.
`BranchState` is a derived read model, not another database. The model and TUI
should eventually share its common meanings, but adding a projection does not
justify requiring a large frame before every trivial action.

A useful state suffix answers: what changed, what remains, which children are
running, and which resource is tight? Add a field only when it changes a
real decision or helps diagnose a failure. Keep exact evidence available on
request rather than copying it into every call.

`SituationFrame`, structured obligations, and richer result capsules are useful
proposals where they solve observed long-session failures. They are not a
prerequisite for navigation, delegation, or a usable TUI. Their unimplemented
parts remain proposals, not a list of compulsory infrastructure to build.

## How to improve it

Start with a real task or trace. Fix the owning path, add a focused executable
regression, and rerun the relevant frontend. Keep unsuccessful trials: a model
syntax error, provider outage, and harness bug require different fixes.

For prompt experiments compare accepted outcomes, total provider usage, cache
usage, wall time, malformed actions, and repair work on held-out tasks. DSPy can
help with an explicit program and metric; installing it or optimizing an unused
module does not improve the coding agent. See [optimization](optimization.md).
