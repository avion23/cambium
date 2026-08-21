# Research docs index

Research files preserve experiments, evidence, competitive snapshots, and
design history. They do not define runtime behavior by themselves.

## Authority order

When documents disagree, use:

1. the task request for scope and required behavior;
2. [`../../agents.md`](../../agents.md) for the repository operating contract;
3. `src/cambium/` plus tests for current behavior;
4. active contracts under `docs/architecture/` and `docs/security/`;
5. research files for evidence and historical context.

A matching name in a proposal is not proof that a module exists or is wired.
Follow entry points, imports, callers, and tests.

## Active design contracts

- [`../architecture/architecture.md`](../architecture/architecture.md) —
  current-versus-target system contract.
- [`../architecture/context-engine.md`](../architecture/context-engine.md) —
  immutable epochs, branching, compaction, and cache accounting.
- [`../architecture/provider-routing.md`](../architecture/provider-routing.md) —
  hard routing constraints, measured soft objectives, capacity, and cache
  affinity.
- [`../architecture/terminal-interface.md`](../architecture/terminal-interface.md)
  — durable session/event UI target and token/cost presentation.
- [`../security/threat-model.md`](../security/threat-model.md) — active
  no-sandbox trust model and residual-risk boundary.

## Live evidence and status references

- [`v2-1-status.md`](v2-1-status.md) — detailed capability/gap table; recheck
  rapidly changing entries against source.
- [`python-3.14.md`](python-3.14.md) — runtime assumptions from a recorded run.
- [`sqlite-wal-durability.md`](sqlite-wal-durability.md) — measured store
  behavior.
- [`worktree-concurrency.md`](worktree-concurrency.md) — measured Git behavior.
- [`worker-coldstart.md`](worker-coldstart.md) — retained raw startup/fan-out
  measurements and their current-scope correction; relevant to the existing
  opt-in warm worker pool.
- [`vertical-slice-report.md`](vertical-slice-report.md) — historical
  worker/IPC/merge evidence; the old task-command gate was removed.
- [`test-strategy.md`](test-strategy.md),
  [`conformance-report.md`](conformance-report.md), and
  [`constitution-compliance.md`](constitution-compliance.md) — point-in-time
  evidence; not current certification.
- [`coding-constitution.md`](coding-constitution.md) — coding-principles
  pointer.

## Cache and recursive-context research

- [`cache-first-context-reuse-plan.md`](cache-first-context-reuse-plan.md) —
  corrected hypothesis, provider-cache boundary, measurement protocol, and
  remaining gaps.
- [`rolling-context-and-agent-reuse.md`](rolling-context-and-agent-reuse.md) —
  corrected rolling/fork/merge model, bounded recursion, and evaluation plan.

The two files are research records. They deliberately defer normative choices
to the active context, routing, terminal, and security contracts above.

## Historical drafts and snapshots

Protocol, event, orchestration, cascade, canonicalization, replay, and
competitive-analysis documents are retained for provenance. In particular,
[`ipc-protocol-draft.md`](ipc-protocol-draft.md),
[`event-schema-draft.md`](event-schema-draft.md),
[`custos-asyncio-design.md`](custos-asyncio-design.md),
[`architectus-design.md`](architectus-design.md), and
[`cascade-design.md`](cascade-design.md) do not override current imports and
callers. Some older drafts name modules or decisions that no longer exist.

The obsolete sandbox-options paper, old threat-model draft, old security audit,
pre-implementation compaction proposal, and “no current TUI” research note were
removed from the working tree. Git history preserves them. Their useful current
content is represented by active contracts; they must not be used as present
runtime claims.

Competitive snapshots such as OpenCode and pi remain dated evidence. They are
not product requirements and should be refreshed before relying on changing
features.

## Complete file index

Every remaining `.md` file in this directory, one line each. Status words follow
each file's own header.

- [`architectus-design.md`](architectus-design.md) — RLM task-tree orchestrator design; historical draft.
- [`bench-harness-design.md`](bench-harness-design.md) — proposed Ascensus scenario measurement layer; historical snapshot.
- [`cache-first-context-reuse-plan.md`](cache-first-context-reuse-plan.md) — corrected cache-first context research record; non-normative.
- [`cascade-design.md`](cascade-design.md) — Diffundo provider-cascade design proposal; non-normative.
- [`cloud-code.md`](cloud-code.md) — competitive analysis of Google/Amazon “Cloud Code”.
- [`codex.md`](codex.md) — competitive analysis of the OpenAI Codex CLI.
- [`coding-constitution.md`](coding-constitution.md) — historical Rust/HFT-to-Python principles translation.
- [`conformance-report.md`](conformance-report.md) — point-in-time implementation/spec audit.
- [`constitution-compliance.md`](constitution-compliance.md) — point-in-time coding-constitution audit.
- [`custos-asyncio-design.md`](custos-asyncio-design.md) — historical design for the proposed asyncio supervisor gap.
- [`design-deltas.md`](design-deltas.md) — historical design-delta record D1–D7.
- [`dspy-hillclimb-plan.md`](dspy-hillclimb-plan.md) — DSPy hill-climbing spike plan; execution remains separately verifiable.
- [`dspy-python-314.md`](dspy-python-314.md) — historical DSPy/Python 3.14.7 compatibility run.
- [`event-schema-draft.md`](event-schema-draft.md) — research-stage event schema proposal.
- [`example-datasets-v1.md`](example-datasets-v1.md) — historical `should_decompose` dataset record.
- [`feedback-2-deltas.md`](feedback-2-deltas.md) — historical second-critique assessment and D8a–D8g.
- [`feedback-4-assessment.md`](feedback-4-assessment.md) — historical fourth-critique disposition.
- [`feedback-5-assessment.md`](feedback-5-assessment.md) — historical fifth-critique disposition.
- [`glossary.md`](glossary.md) — Latin-to-plain-English naming map.
- [`ipc-protocol-draft.md`](ipc-protocol-draft.md) — Nuntius IPC catalogue draft; non-normative.
- [`logging-design.md`](logging-design.md) — historical logging design record.
- [`m1-canonicalization-plan.md`](m1-canonicalization-plan.md) — design-only runtime/store/sequencer canonicalization record.
- [`metric-design.md`](metric-design.md) — historical automatic coding-metric target.
- [`omp.md`](omp.md) — competitive analysis of Oh My Pi.
- [`onboarding-checklist-draft.md`](onboarding-checklist-draft.md) — compressed module-onboarding draft.
- [`opencode.md`](opencode.md) — dated competitive analysis of OpenCode.
- [`pi.md`](pi.md) — dated competitive analysis of pi.
- [`prime-agent.md`](prime-agent.md) — dated competitive analysis of Prime Agent.
- [`provider-landscape.md`](provider-landscape.md) — local provider-config matrix used as Diffundo research input.
- [`pydev.md`](pydev.md) — web-only py.dev/JetBrains AI competitive analysis.
- [`python-3.14.md`](python-3.14.md) — verified Python 3.14 capability record from a historical run.
- [`replay-restart-design.md`](replay-restart-design.md) — crash-recovery/restart design record.
- [`repo-structure-plan.md`](repo-structure-plan.md) — historical repository-structure audit and layout plan.
- [`rolling-context-and-agent-reuse.md`](rolling-context-and-agent-reuse.md) — corrected rolling-context and bounded-recursion research record; non-normative.
- [`sqlite-wal-durability.md`](sqlite-wal-durability.md) — empirical SQLite WAL durability study.
- [`test-strategy.md`](test-strategy.md) — harness test-strategy design.
- [`treesitter-context.md`](treesitter-context.md) — Tree-sitter context-compression experiment.
- [`v2-1-review.md`](v2-1-review.md) — architecture review and roadmap snapshot.
- [`v2-1-status.md`](v2-1-status.md) — detailed live capability/gap table, subject to source verification.
- [`vertical-slice-report.md`](vertical-slice-report.md) — historical one-worker end-to-end slice record.
- [`worker-coldstart.md`](worker-coldstart.md) — retained worker-startup benchmark with raw samples and current correction.
- [`worktree-concurrency.md`](worktree-concurrency.md) — measured Git worktree concurrency semantics.

## Finding the current surface

```sh
git ls-files src/cambium tests | sort
rg -n "run_plan|do_work|Diffundo|EventStore|ConversationStore|ContextEpoch" src tests
```

Start at the entry point and follow imports and tests. Do not bulk-read the
research directory or infer behavior from a filename.
