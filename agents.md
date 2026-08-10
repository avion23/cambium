# agents.md - Cambium agent control plane

Read this file before work. It is the short operating contract. Use the
reference map for current code, target design, commands, and evidence.

## Top 20 controls

1. **Authority order.** Follow this order: task request; this file for process;
   source and tests for current behavior; architecture for target behavior;
   research and history for context. State conflicts.
2. **Target is not proof.** Architecture describes a target. Source, tests, and
   recorded commands prove current behavior.
3. **Record before work.** Record scope, entry points, current behavior,
   reproduction, and baseline before editing.
4. **Search from entry points.** Start at route registration, command tables,
   and imports. Trace callers and tests. A failed name search is not proof of
   absence.
5. **Keep scope tight.** Change only files needed for the causal fix. Report
   required scope expansion before editing it.
6. **Use an isolated worktree.** Check `git rev-parse --show-toplevel` and
   `git worktree list`. Never commit in the shared integration checkout.
7. **No destructive git.** Do not force-push, rewrite shared history, reset
   another worktree, or delete work to hide a failure.
8. **Reproduce first.** Run a deterministic failing check before editing. If it
   does not fail, find the real entry point or mark the claim UNVERIFIED.
9. **Make the smallest causal change.** Remove the cause. Do not mask it with a
   fallback, retry, default, or catch-all path.
10. **Preserve boundaries.** Keep protocol schemas, worktree isolation,
    approval gates, public exports, and module seams intact.
11. **Keep secrets in the environment.** Never commit, print, log, or send
    keys or tokens. Redact evidence.
12. **Reserve stdout.** Worker stdout is NDJSON protocol only. Send diagnostics
    to logging or stderr.
13. **Keep I/O off-loop.** Run disk and subprocess work off the event loop.
    Use existing boundary helpers.
14. **Use what exists.** Use existing dependencies, tools, and project
    vocabulary. Add no framework or synonym without a demonstrated need.
15. **Test offline and deterministically.** Use fake providers or workers and
    fixed fixtures. Tests must not use the network.
16. **Run the narrowest check.** Then run the affected package or integration
    check when the change crosses a boundary.
17. **Use same-version canary evidence.** The canary gate rejects degradation
    only when candidate and baseline use the same dataset, canary, schema, and
    baseline versions.
18. **Approve re-anchors.** A dataset-version change re-anchors the bench gate.
    Dataset, canary, schema, or baseline changes need explicit approval. Never
    silently re-anchor.
19. **Report status exactly.** Use VERIFIED only with command, cwd, exit status,
    and evidence. Use UNVERIFIED for an unrun claim. Use BLOCKED for an
    external blocker.
20. **Resume with a block.** End each handoff with the resume template below.
    State one next action.

## Reference map

### Current versus target

Current code is a deterministic Python harness. `src/cambium/__init__.py`
exports only `__version__`; do not claim a public `Cambium`, `Session`, or
`Result` API. `src/cambium/worker.py` is a deterministic marker and commit
seed over the NDJSON protocol; no DSPy ReAct loop is present.
`src/cambium/diffundo.py`, `src/cambium/architectus.py`,
`src/cambium/tools.py`, and `src/cambium/edits.py` are repository files, not
branch-local work. Architecture is a target, not proof. Use source, tests, and
the living `docs/research/v2-1-status.md` for milestone evidence; it is being
refreshed elsewhere, so do not copy its SHA claims.

### Stable entry points

- CLI: `src/cambium/cli.py:main`; package version: `src/cambium/__init__.py:__version__`.
- Runtime: `src/cambium/ipc.py`, `src/cambium/worker.py:main`,
  `src/cambium/supervisor.py:main`, and `src/cambium/worker_pool.py`.
- Boundaries: `src/cambium/store.py`, `src/cambium/merge.py`,
  `src/cambium/tools.py`, `src/cambium/edits.py`, `src/cambium/diffundo.py`,
  `src/cambium/architectus.py`, and `src/cambium/provider_config.py`.
- Tests: `tests/scenarios/` and `src/cambium/modules/example/tests/`.

### Verified command table

| Check | Command |
|---|---|
| Full suite | `uv run --python 3.14.7 --extra test pytest -q` |
| Collect tests | `uv run --python 3.14.7 --extra test pytest --collect-only -q` |
| Focused scenario | `uv run --python 3.14.7 --extra test pytest -q tests/scenarios/test_worker_pool.py` |
| Lint | `uv run --python 3.14.7 --extra dev ruff check src tests` |
| Syntax | `python -m compileall src/cambium` |
| CLI help | `uv run --python 3.14.7 cambium --help` |
| CLI version | `uv run --python 3.14.7 cambium version` |

### Document authority map

| Need | Read |
|---|---|
| Agent process and reporting | `agents.md` |
| Current behavior | `src/cambium/` and `tests/` |
| Architecture target | `docs/architecture/architecture.md` |
| Module contracts | `docs/architecture/module-template/architecture.md`, `docs/architecture/module-template/dataset-format.md`, `docs/architecture/module-template/example-spec.md` |
| Milestone evidence | `docs/research/v2-1-status.md` (living; do not copy its SHA claims) |
| Research decisions | `docs/research/README.md`, `docs/research/design-deltas.md` |
| Older design and reviews | `docs/architecture/system-design.md`, `docs/architecture/reviews/` |

### Resume block

- **Scope:**
- **Authority and target:**
- **Entry points read:**
- **Baseline and reproduction:** command, cwd, result
- **Files in scope:**
- **Change and preserved boundary:**
- **Checks:** command, cwd, exit status, evidence
- **Status:** VERIFIED | UNVERIFIED | BLOCKED
- **Next action:**
