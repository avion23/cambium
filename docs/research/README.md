# Research docs index

This directory keeps research evidence and design drafts in-repo without making
agents load the whole corpus. Start here. `agents.md` and
`docs/architecture/architecture.md` remain the orientation and normative design
surfaces; a research draft does not override them unless the architecture or an
adopted delta says so.

> **Agents read TIER 1; read TIER 2 only when a citation points there; never read TIER 3 unless investigating a historical decision.**

Do not bulk-read `docs/research/`. Use the tier that matches the task and follow
citations to the specific source.

## TIER 1 — READ FIRST

This is the minimal research set an agent needs before touching code: runtime
assumptions, adopted design deltas, protocol and event contracts, concurrency
ownership, persistence durability, worktree safety, and coding norms.

| Document | What it covers | Why read it first |
|---|---|---|
| [`python-3.14.md`](python-3.14.md) | Verified CPython 3.14 capabilities, including GIL/free-threading and async/process features. | Establishes the supported runtime and the Python assumptions behind concurrency and worker code. |
| [`sqlite-wal-durability.md`](sqlite-wal-durability.md) | Empirical SQLite WAL durability, checkpoint, fsync, and crash-loss validation. | Grounds the event and conversation-store durability contract in measured behavior. |
| [`worktree-concurrency.md`](worktree-concurrency.md) | Empirical Git worktree, index-lock, and concurrent merge semantics. | Prevents unsafe assumptions in worktree handling and merge serialization. |
| [`ipc-protocol-draft.md`](ipc-protocol-draft.md) | Nuntius JSON-Lines message catalogue, handshake, state machine, and result envelopes. | Gives protocol implementers the message flow and failure vocabulary; reconcile this draft with architecture §5. |
| [`event-schema-draft.md`](event-schema-draft.md) | Event-log envelope, event catalogue, durable stores, and payload-first task-tree linkage. | Defines the event vocabulary and replay/linkage assumptions used by supervisor code. |
| [`custos-asyncio-design.md`](custos-asyncio-design.md) | Custos event-loop/thread/subprocess split, single-writer persistence, state ownership, and shutdown. | Sets the concurrency boundaries that keep disk I/O and shared mutable state off the event loop. |
| [`design-deltas.md`](design-deltas.md) | Adopted D1–D7 amendments to architecture v2. | Prevents implementation of superseded choices such as a local cache or in-harness sandbox. |
| [`coding-constitution.md`](coding-constitution.md) | Python translation of the repository's coding principles and their design mapping. | Makes the expected control flow, state isolation, measurement, and interface discipline explicit. |

## TIER 2 — READ WHEN CITED

These are supporting evidence, audits, and design drafts. Read the named
document when the architecture, `agents.md`, or another task-specific source
cites it; do not preload them. Security and conformance reports belong here:
they provide important evidence for the affected change, but they are
point-in-time audits, not prerequisites for every code edit.

- [`architectus-design.md`](architectus-design.md) — Draft RLM/task-tree orchestration design for Architectus and its split from Custos.
- [`bench-harness-design.md`](bench-harness-design.md) — Objective benchmark harness for test duration, dataset health, metric baselines, and canary rates.
- [`cascade-design.md`](cascade-design.md) — Diffundo tiered fallback, capability filtering, circuit-breaker, race, and rate-limit design.
- [`compaction-design.md`](compaction-design.md) — Draft evidence-backed context-compaction protocol for worker sessions.
- [`conformance-report.md`](conformance-report.md) — Point-in-time merged-implementation conformance report against the normative specs.
- [`constitution-compliance.md`](constitution-compliance.md) — Point-in-time implementation audit against the translated coding constitution and agent norms.
- [`dspy-python-314.md`](dspy-python-314.md) — Verified DSPy compatibility on CPython 3.14 GIL builds and the free-threaded build blocker.
- [`example-datasets-v1.md`](example-datasets-v1.md) — Verified generation and validation method for the example module's train/eval/canary datasets.
- [`glossary.md`](glossary.md) — Current architecture vocabulary and implementation-surface names.
- [`logging-design.md`](logging-design.md) — Non-blocking structured logging, redaction, rotation, and writer-thread design.
- [`m1-canonicalization-plan.md`](m1-canonicalization-plan.md) — Plan for one Custos runtime, one store, one sequencer, and removal of slice/fallback paths.
- [`metric-design.md`](metric-design.md) — Automatic coding-diff metric, calibration, canary, and promotion-gate design.
- [`onboarding-checklist-draft.md`](onboarding-checklist-draft.md) — Draft workflow for taking a decision module from specification through eval, gate, and merge.
- [`provider-landscape.md`](provider-landscape.md) — Redacted local provider and model configuration inventory for Diffundo input.
- [`replay-restart-design.md`](replay-restart-design.md) — Proposed event-log replay, orphan cleanup, and supervisor crash-recovery semantics.
- [`repo-structure-plan.md`](repo-structure-plan.md) — Verified repository taxonomy, research-doc placement, and layout plan.
- [`security-audit.md`](security-audit.md) — Point-in-time read-only security audit against the threat model and containment policy.
- [`test-strategy.md`](test-strategy.md) — Scenario, integration, fault-injection, and verification strategy for the harness itself.
- [`treesitter-context.md`](treesitter-context.md) — Measured tree-sitter/AST context-compression experiment for the v2.1 roadmap.
- [`vertical-slice-report.md`](vertical-slice-report.md) — Verified one-worker end-to-end supervisor, IPC, gate, and Git-merge proof.
- [`worker-coldstart.md`](worker-coldstart.md) — Measured fork-per-task versus persistent-worker cold-start benchmark.

## TIER 3 — HISTORICAL

These records are historical evidence, not normative design. Use them only
while investigating a past decision, provenance, or review finding. Current
behavior comes from `docs/architecture/architecture.md`, adopted deltas, and
the implementation.

- [`opencode.md`](opencode.md) — Local OpenCode competitive analysis covering providers, sessions, tools, permissions, and headless/TUI surfaces. **Evidence only; not normative.**
- [`codex.md`](codex.md) — OpenAI Codex CLI install analysis covering worktrees, checkpoints, diagnostics, approvals, and headless execution. **Evidence only; not normative.**
- [`pi.md`](pi.md) — Local Pi coding-agent analysis covering modes, extensions, sessions, and subagents. **Evidence only; not normative.**
- [`omp.md`](omp.md) — Oh My Pi analysis covering routing, concurrency, isolation, and configuration. **Evidence only; not normative.**
- [`prime-agent.md`](prime-agent.md) — Prime Agent analysis covering daemon-backed sessions, recursive subagents, continuity, and failure lessons. **Evidence only; not normative.**
- [`pydev.md`](pydev.md) — Web research on the JetBrains AI, Junie, Air, ACP, and Mellum product family; `py.dev` itself was unavailable. **Evidence only; not normative.**
- [`cloud-code.md`](cloud-code.md) — Google Cloud Code and Gemini Code Assist competitive analysis. **Evidence only; not normative.**
- [`tui-best-practices.md`](tui-best-practices.md) — Review of OpenCode, Codex, and Claude Code interface patterns and headless/TUI boundaries. **Evidence only; not normative.**
- [`sandbox-options.md`](sandbox-options.md) — Superseded sandbox-options record; retains the host AppArmor/user-namespace finding. **Evidence only; not normative.**
- [`threat-model.md`](threat-model.md) — Design-era security risk assessment, including sandbox-dependent analysis retained for history. **Evidence only; not normative.**
- [`feedback-2-deltas.md`](feedback-2-deltas.md) — Second external-critique assessment and D8a–D8g residue. **Evidence only; not normative.**
- [`feedback-4-assessment.md`](feedback-4-assessment.md) — Fourth external-critique assessment and disposition record. **Evidence only; not normative.**
- [`feedback-5-assessment.md`](feedback-5-assessment.md) — Fifth external-critique assessment and disposition record. **Evidence only; not normative.**
- [`v2-1-review.md`](v2-1-review.md) — v2.1 architecture review and roadmap with branch-state and integration findings. **Evidence only; not normative.**
- [`v2-1-status.md`](v2-1-status.md) — Current v2.1 milestone and integration status tracker. **Evidence only; not normative.**

## Coverage

- TIER 1: **8 tracked research documents**.
- TIER 2: **22 tracked research documents**.
- TIER 3: **14 tracked research documents**.
- Total: **44 research documents**, each listed exactly once above. The index
  itself is not a tier item.

**This index lists 44 files; run `git ls-files docs/research | wc -l` to verify.**
After this README is tracked, that command reports 45 because it includes the
index itself; the 44 tier entries are the research-document count.
