# agents.md — Cambium operating contract

Read this file before work. Use source, tests, and recorded commands for
current behavior. Do not infer implementation from a role name or a draft.

## Authority and workflow

Authority order is: task request; this contract; source and tests; architecture
targets; research history. Start at route registration, command tables, and
imports. Trace callers and tests. A failed name search is not proof of absence.
Record scope, entry points, baseline, reproduction, and the check that
distinguishes the diagnosis from alternatives.

- Keep the requested file scope. Report any required expansion before editing.
- Work in an isolated worktree. Children do not merge branches; the root
  integrates, verifies, and cleans up.
- Reproduce before changing code. Remove the cause; do not mask it with a
  fallback, retry, default, or catch-all. Preserve protocol, schema, worktree,
  approval, export, and module boundaries.
- Use adversarial review for consequential changes. Report exact commands,
  working directories, exit statuses, and observed evidence. Use `UNVERIFIED`
  for an unrun check and `BLOCKED` only for an external blocker.
- Do not force-push, rewrite shared history, reset another worktree, or delete
  work to hide a failure. Secrets stay in the environment and never in task
  specs, events, logs, gate output, or commits.

## Current state

- The redaction canary fails: mixed raw/Unicode-escaped stderr retains `\u005c`
  through the module wire → redaction boundary. Fix it before live use.

## Boundary invariants

- IPC framing is bounded and request/generation-correlated. Malformed advisory
  lines are logged or skipped; fatal framing, missing correlated results,
  non-zero exits, and deadline failures fail or restart the task according to
  the boundary policy.
- A failed gate, merge conflict, non-fast-forward, quarantine violation, or
  stale expected-old ref never publishes `main`.
- Provider keys are allowlisted environment values. Never put credentials or
  sensitive content in task specs, event payloads, gate commands/output, or
  durable artifacts.
- Use enums for domain alternatives (`Decision`, `NodeStatus`). Keep blocking
  disk and subprocess I/O at existing thread/process boundaries.

## Checks and handoff

Tests are example data-in/data-out pairs: deterministic module input produces
the expected module output. Use `uv` (not pip) for environments and commands.
Run the narrowest real check, then the affected package check when a boundary
changes. Useful system commands from the repository root:

```sh
python3.14 -m pytest -q src/cambium/modules/example/tests/
python3.14 -m compileall src tests
PYTHONPATH=src python3.14 -m cambium.cli --help
git diff --check
```

Before commit, inspect the diff, stage only intended files, and verify a clean
worktree. Every handoff states:

- Scope and files in scope.
- Authority and entry points read.
- Baseline/reproduction command, cwd, and result.
- Change and preserved boundary.
- Checks with command, cwd, exit status, and evidence.
- Status: `VERIFIED`, `UNVERIFIED`, or `BLOCKED`.
- Next action.
