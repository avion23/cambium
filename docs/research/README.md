# Research docs index

Research files preserve experiments, evidence, and design history. They do not
define the runtime by themselves.

## Authority order

When documents disagree, use:

1. The task request for scope and required behavior.
2. [`../../agents.md`](../../agents.md) for process and current-truth notes.
3. `src/cambium/` for implementation and `tests/` plus
   `src/cambium/modules/example/tests/` for observed behavior.
4. [`../architecture/architecture.md`](../architecture/architecture.md) for
   current-versus-target boundaries.
5. Research files for context or measured evidence.

Check imports, callers, and tests. A matching name in a proposal is not proof
that a module is present or wired.

## Live references

- [`v2-1-status.md`](v2-1-status.md) — the detailed capability/gap table.
- [`python-3.14.md`](python-3.14.md) — runtime assumptions.
- [`sqlite-wal-durability.md`](sqlite-wal-durability.md) — measured store behavior.
- [`worktree-concurrency.md`](worktree-concurrency.md) — measured Git behavior.
- [`vertical-slice-report.md`](vertical-slice-report.md) — historical worker/IPC/merge evidence (gate removed by decision).
- [`test-strategy.md`](test-strategy.md), [`security-audit.md`](security-audit.md),
  [`conformance-report.md`](conformance-report.md), and
  [`constitution-compliance.md`](constitution-compliance.md) — point-in-time
  evidence; recheck claims against source.
- [`coding-constitution.md`](coding-constitution.md) — coding-principles pointer.

The accepted target shape is defined in the architecture and plan: a
harness-owned validated tree, static ready-node scheduling before dynamic child
admission, fresh bounded child contexts, strict upward envelopes, and
prompt-prefix/cache-hit metrics. These are targets, not current runtime proof.

## Historical drafts

Protocol, event, orchestration, cascade, canonicalization, replay, and
compaction drafts are retained for provenance. In particular,
[`ipc-protocol-draft.md`](ipc-protocol-draft.md),
[`event-schema-draft.md`](event-schema-draft.md),
[`custos-asyncio-design.md`](custos-asyncio-design.md),
[`architectus-design.md`](architectus-design.md), and
[`cascade-design.md`](cascade-design.md) do not override current imports and
callers. Some older drafts name modules that are no longer tracked.

The benchmark and example-module documents describe offline evaluation, not
the production supervisor path. Use [`bench-harness-design.md`](bench-harness-design.md)
with `src/cambium/bench.py` and the example evaluator when reproducing those
experiments.

## Complete file index

Every `.md` file in this directory, one line each. Status words follow each
file's own header.

- [`architectus-design.md`](architectus-design.md) — RLM task-tree orchestrator (v2.1 M5), historical draft; nothing merged.
- [`bench-harness-design.md`](bench-harness-design.md) — proposed Ascensus measurement layer for scenario tests; historical snapshot.
- [`cache-first-context-reuse-plan.md`](cache-first-context-reuse-plan.md) — implementation record for immutable cache epochs, parent/child fork/resume, and rolling compaction; chat-provider acceptance gates pass; non-normative.
- [`cascade-design.md`](cascade-design.md) — Diffundo provider-cascade design, docs-only proposal extending architecture §9.
- [`cloud-code.md`](cloud-code.md) — competitive analysis of Google/Amazon "Cloud Code".
- [`codex.md`](codex.md) — competitive analysis of the OpenAI Codex CLI.
- [`coding-constitution.md`](coding-constitution.md) — historical Rust/HFT → Python 3.14 translation of coding principles.
- [`compaction-design.md`](compaction-design.md) — draft context-compaction protocol for workers; docs-only, non-normative.
- [`conformance-report.md`](conformance-report.md) — read-only audit of merged implementation vs normative specs.
- [`constitution-compliance.md`](constitution-compliance.md) — read-only audit of merged implementation vs the coding constitution.
- [`custos-asyncio-design.md`](custos-asyncio-design.md) — historical design spec resolving the proposed M4 asyncio gap.
- [`design-deltas.md`](design-deltas.md) — historical decision record of design deltas D1..D7.
- [`dspy-hillclimb-plan.md`](dspy-hillclimb-plan.md) — DSPy hill-climbing spike plan; optimizer implementation and execution remain unverified.
- [`dspy-python-314.md`](dspy-python-314.md) — historical DSPy-on-Python-3.14.7 compatibility run.
- [`event-schema-draft.md`](event-schema-draft.md) — research-stage event-log schema proposal, not frozen code.
- [`example-datasets-v1.md`](example-datasets-v1.md) — historical record of the `should_decompose` datasets v1 generator and checks.
- [`feedback-2-deltas.md`](feedback-2-deltas.md) — historical assessment of the second external critique plus deltas D8a..D8g.
- [`feedback-4-assessment.md`](feedback-4-assessment.md) — historical disposition of the fourth critique's 21 claims.
- [`feedback-5-assessment.md`](feedback-5-assessment.md) — historical disposition of the fifth critique's 19 claims.
- [`glossary.md`](glossary.md) — naming map bridging Latin architecture names and plain-English names.
- [`ipc-protocol-draft.md`](ipc-protocol-draft.md) — Nuntius IPC message-catalogue draft, non-normative.
- [`logging-design.md`](logging-design.md) — design record resolving IMPL-M7; not merged.
- [`m1-canonicalization-plan.md`](m1-canonicalization-plan.md) — design-only record for one runtime / one store / one sequencer canonicalization.
- [`metric-design.md`](metric-design.md) — automatic coding metric design (LLM-C5); historical target for Opifex-style tasks.
- [`omp.md`](omp.md) — competitive analysis of `omp` (Oh My Pi).
- [`onboarding-checklist-draft.md`](onboarding-checklist-draft.md) — compressed module-onboarding process draft, not a status report.
- [`opencode.md`](opencode.md) — competitive analysis of OpenCode (anomalyco/opencode).
- [`pi.md`](pi.md) — competitive analysis of `pi` (@earendil-works/pi-coding-agent).
- [`prime-agent.md`](prime-agent.md) — competitive analysis of Prime Agent.
- [`provider-landscape.md`](provider-landscape.md) — local provider-config matrix, input to Diffundo.
- [`pydev.md`](pydev.md) — web-only competitive analysis of py.dev / JetBrains AI; `py.dev` itself was unreachable.
- [`python-3.14.md`](python-3.14.md) — verified Python 3.14 capabilities for Cambium (historical run).
- [`replay-restart-design.md`](replay-restart-design.md) — crash-recovery event-log replay and supervisor restart semantics.
- [`repo-structure-plan.md`](repo-structure-plan.md) — historical repo-structure audit and final layout plan.
- [`rolling-context-and-agent-reuse.md`](rolling-context-and-agent-reuse.md) — historical design record for implemented immutable-epoch context reuse and rolling compaction; non-normative where it differs from architecture docs.
- [`sandbox-options.md`](sandbox-options.md) — superseded sandboxing-options research; decision 10 removed in-harness sandboxing; runtime has no per-worker containment.
- [`security-audit.md`](security-audit.md) — read-only security audit of merged implementation vs the threat model.
- [`sqlite-wal-durability.md`](sqlite-wal-durability.md) — empirical validation of SQLite WAL event-log durability.
- [`test-strategy.md`](test-strategy.md) — design answering IMPL-M8; test strategy for the harness itself.
- [`threat-model.md`](threat-model.md) — historical design-level threat model (v0.1.0); decision 10 removed Septum sandboxing; current runtime has no per-worker OS containment.
- [`treesitter-context.md`](treesitter-context.md) — Tree-sitter AST context-compression experiment (v2.1 M9, proposal 1).
- [`tui-best-practices.md`](tui-best-practices.md) — future TUI best-practices research (Janus); no current TUI.
- [`v2-1-review.md`](v2-1-review.md) — v2.1 architecture review and roadmap.
- [`v2-1-status.md`](v2-1-status.md) — the sole detailed live capability/gap table.
- [`vertical-slice-report.md`](vertical-slice-report.md) — historical one-worker end-to-end slice record (gate removed by decision).
- [`worker-coldstart.md`](worker-coldstart.md) — historical worker cold-start benchmark comparing fork-per-task with a persistent pool; corrected conclusion favors pre-spawned persistent workers.
- [`worktree-concurrency.md`](worktree-concurrency.md) — measured git worktree concurrency semantics.

## Finding the current surface

```sh
git ls-files src/cambium tests | sort
rg -n "run_plan|do_work|Diffundo|EventStore|ArchitectusCore|evaluate_split" src tests
```

Start at the entry point and follow imports and tests. Do not bulk-read the
research directory or infer behavior from a filename.
