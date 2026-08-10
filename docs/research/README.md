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
- [`vertical-slice-report.md`](vertical-slice-report.md) — deterministic worker,
  gate, and merge evidence.
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

## Finding the current surface

```sh
git ls-files src/cambium tests | sort
rg -n "run_plan|do_work|Diffundo|EventStore|ArchitectusCore|evaluate_split" src tests
```

Start at the entry point and follow imports and tests. Do not bulk-read the
research directory or infer behavior from a filename.
