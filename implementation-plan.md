# Implementation Plan (TRANSIENT — delete when implementation is done)

Status date: 2026-08-09. Current merged baseline: `main@6109a6a`. This is the
orchestrator-owned tracker for the 20-agent implementation wave. Worktrees live
under `/tmp/opencode/cambium-<name>` on `wt-*` branches.

## Current merged baseline

The following deterministic modules are in `main`:

| Capability | Main merge commit |
|---|---|
| Foundation: `events.py`, `orchestrator.py`, `modules/base.py`, example module, test scaffold | `f66bdc6` (`merge: build(scaffold)`) |
| Vertical-slice supervisor and fake worker | `c053d35` (`merge: feat(slice) — vertical slice milestone`) |
| `doctor.py` diagnostics and ruff tooling | `2822139` (`merge: feat(tooling) — ruff + doctor`) |
| `merge.py` / `MergeSequencer` | `c7e19b0` (`merge: feat(merge) — unio sequencer`) |
| `store.py` / `EventStore` | `3d27ba3` (`merge: feat(store) — sqlite wal event store`) |
| `tasktree.py` / deterministic DAG validation | `06ce0dc` (`merge: feat(tasktree) — dag builder`) |
| `ipc.py` + `worker.py` / Nuntius framing and Opifex seed | `38e1d43` (`merge: feat(ipc) — nuntius framing + worker runtime`) |
| Split dataset loader and dataset scenarios | `df8ed81` (`merge: feat(datasets) — split loaders`) |
| Final architecture/D7 and recovery-gap specification fold | `e8f0d0f`, `d67cd5e` |
| Ruff-clean test gate | `d7971cc` (`merge: chore(lint) — ruff-clean tests`) |
| v2.1 architecture review and roadmap | `6109a6a` (`merge: research(v2.1) — architecture review and roadmap`) |

Current source inventory: `events.py`, `orchestrator.py`, `supervisor.py`,
`store.py`, `merge.py`, `ipc.py`, `worker.py`, `tasktree.py`, `doctor.py`,
and `modules/{base,example}`. `diffundo.py`, `bench.py`, and `redact.py` are
still branch-local implementation artifacts, not modules in `main`.

Verification on current `main`:

- `uv run --python 3.14.7 --extra test pytest --collect-only -q` → **108 tests collected**.
- `uv run --python 3.14.7 --extra test pytest -q` → **108 passed**.
- `uv run --python 3.14.7 --with ruff ruff check src` → **All checks passed**.

## In-flight: 20-agent wave

The implementation and follow-up wave remains in isolated worktrees. The
active surfaces are:

- `wt-impl-super`: Custos canonical runtime and store/merge wiring.
- `wt-impl-diffundo`, `wt-impl-bench`, `wt-redact`: provider routing,
  benchmark gate, and redaction components.
- `wt-luna-fence`, `wt-luna-dlq`, `wt-luna-pipe`, `wt-luna-cli`,
  `wt-luna-conv`: fencing, dead-lettering, protocol/pipeline, CLI, and
  conversation-store work.
- `wt-doc-*`, `wt-audit-*`, `wt-modtests`, and `wt-research-treesitter`:
  audit, documentation, test, and v2.1 research follow-up.

Branch-local green tests are not merged-state evidence. Do not mark an item
complete until it is on one main SHA and the full suite is rerun.

## Pending milestones

Milestones and dependencies are defined in `docs/research/v2-1-review.md`:

1. **Hardening pass** — protocol deadlines/caps, redaction, environment
   allowlists, fencing, bounded queues, gate/resource limits, and audit fixes.
2. **M1 — canonical runtime and audit baseline** — wire one Custos path to
   `EventStore`, `MergeSequencer`, Nuntius, worker, redactor, and doctor;
   remove slice and fallback paths; rerun all three audits on one SHA.
3. **M6 — first real LLM end-to-end task** — after M1–M5, connect Diffundo,
   one OpenAI-compatible provider, `CambiumLM`, one atomic coding task, a
   deterministic gate, and Unio publication. Keep it manual and key-gated.

M2–M5 are the hard gates between M1 and M6. M7 persistent workers, M8 DSPy
refinement, and M9 tree-sitter research follow the first real end-to-end task.

## Recorded decisions

1. Python: `>=3.14,<3.15`, regular GIL build.
2. Public interface: headless library plus JSON-Lines; TUI is optional.
3. Provider caching is upstream; no local LLM response cache.
4. Modules use a DSPy seam, frozen datasets, metrics, and canaries.
5. Workers are subprocesses, never in-process.
6. No DSPy runtime dependency enters the deterministic scaffold.
7. The task tree is a first-class DAG of sub-LLM sessions.
8. No sandboxing in the harness; containment is worktree isolation, permission
   allowlists, approval gates, and host-owned controls.
