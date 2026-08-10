# Research docs index

Research files preserve evidence, experiments, and design history. They do not
define the runtime by themselves.

## Authority order

Use these sources in this order when they disagree:

1. `agents.md` for process and current-truth notes.
2. [`../architecture/architecture.md`](../architecture/architecture.md) for
   target boundaries and behavioral invariants. It marks which contracts are
   implemented and which are targets.
3. `src/cambium/` for implementation.
4. `tests/` and `src/cambium/modules/example/tests/` for observed behavior.
5. Research files for context or measured evidence only.

Matching names in a draft are not proof that a module is wired. Check imports,
callers, and tests.

## Evidence and supporting references

Read these when the architecture or task cites them:

- [`python-3.14.md`](python-3.14.md) — runtime assumptions.
- [`sqlite-wal-durability.md`](sqlite-wal-durability.md) — measured SQLite
  durability.
- [`worktree-concurrency.md`](worktree-concurrency.md) — measured Git
  worktree and merge behavior.
- [`design-deltas.md`](design-deltas.md) — adopted design decisions; verify
  each against source before implementation.
- [`vertical-slice-report.md`](vertical-slice-report.md) — the recorded
  deterministic worker/gate/merge proof.
- [`test-strategy.md`](test-strategy.md), [`security-audit.md`](security-audit.md),
  [`conformance-report.md`](conformance-report.md), and
  [`constitution-compliance.md`](constitution-compliance.md) — point-in-time
  evidence, not completion claims.
- [`treesitter-context.md`](treesitter-context.md) and
  [`worker-coldstart.md`](worker-coldstart.md) — experiment records.

## Historical drafts

These files describe proposals or earlier vocabulary. They are retained for
provenance and must not override source, tests, or the architecture:

- [`ipc-protocol-draft.md`](ipc-protocol-draft.md) and
  [`event-schema-draft.md`](event-schema-draft.md) — protocol/event drafts;
  current framing and event behavior live in `src/cambium/ipc.py`,
  `src/cambium/store.py`, and their tests.
- [`custos-asyncio-design.md`](custos-asyncio-design.md) and
  [`architectus-design.md`](architectus-design.md) — orchestration proposals;
  current wiring is shown by imports and callers.
- [`cascade-design.md`](cascade-design.md) — historical cascade proposal;
  conflicting cache or routing policy is not normative. Use
  `src/cambium/diffundo.py` and `tests/scenarios/test_diffundo*.py`.
- [`m1-canonicalization-plan.md`](m1-canonicalization-plan.md),
  [`replay-restart-design.md`](replay-restart-design.md), and
  [`compaction-design.md`](compaction-design.md) — open or superseded plans.
- Competitive analyses, feedback assessments, the v2.1 review, and other
  drafts in this directory — historical evidence only.

The milestone tracker is intentionally separate from the authority chain:
[`v2-1-status.md`](v2-1-status.md) reports current capabilities and gaps; it
does not turn a research proposal into an implementation.

## Finding the current surface

```sh
git ls-files src/cambium tests | sort
rg -n "run_plan|do_work|ArchitectusCore|worker_pool|CambiumLM" src tests
```

Start from the entry point, then follow imports and tests. Do not bulk-read the
research directory or infer behavior from a document title.
