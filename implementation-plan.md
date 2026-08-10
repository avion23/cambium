# Implementation plan

This is an ordered work plan. It is not a branch ledger, merge log, or review
archive. Source and tests decide whether a step is complete.

## 1. Canonical runtime and controls

- Make `run_plan` use one supervisor/store/sequencer path. Remove the slice and
  fallback implementations after callers and tests move to the canonical
  interfaces.
- Wire session redaction before event admission and connect the root result
  writer to the plan lifecycle.
- Bound supervisor event handoff and transport queues. Keep line limits,
  deadline-bound waits, fencing, worktree recovery, approval, resource gates,
  and fail-closed publication behavior.
- Add focused checks for each boundary: malformed plan, protocol overflow,
  worker restart, gate failure, redaction, and non-fast-forward publication.

**Exit evidence:** one deterministic `run_plan` path passes its focused
scenario set, writes a redacted event store and root result, and leaves no
slice/fallback caller.

## 2. Thin real-provider vertical proof

Run one explicit provider configuration through the bounded worker loop, a
deterministic gate, and ref-only merge. Keep credentials in the environment,
use a disposable test repository, and do not make this the default CI path.

**Exit evidence:** a recorded provider request, tool/checkpoint events, one
worker commit, a passing gate, and the expected `refs/heads/main` update; the
failure case leaves `main` unchanged.

## 3. Fixed-tree scheduling

Connect `tasktree.build_tree`, `topological_order`, and `ready_tasks` to the
supervisor. Integrate the pure Architectus core behind an injected decision
port, schedule only ready nodes, and preserve dependency validation and result
information hiding. Do not add dynamic replanning to this first integration.

**Exit evidence:** deterministic scenarios prove dependency order, bounded
parallel width, failure propagation, and no dispatch of an unready node.

## 4. Measured experiments

After the runtime path is reproducible, run isolated experiments for persistent
worker widths, provider routing, context compression, and the example module's
DSPy refinement. Pin inputs and record the metric and failure criteria before
comparing runs.

**Exit evidence:** each experiment has a reproducible command, fixed fixtures,
an explicit baseline, and a decision to adopt, defer, or reject. No experiment
changes the runtime contract without a new source/test proof.
