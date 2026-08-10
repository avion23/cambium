# agents.md — Cambium operating contract

Read this file before work. Use source, tests, and recorded commands for
current behavior. Do not infer implementation from a role name or a draft.

## Development mode

This repository is under active development. KISS is the default. Implement
only what the task asks for. Unless the task explicitly requests it:

- Do not add gates, approval systems, admission controls, containment,
  sandboxes, retries, fallbacks, readiness checks, or production hardening.
- Do not add hashes, checksums, signatures, provenance records, attestations,
  evidence artifacts, accounting, or observability systems.
- Do not add environment, dependency, credential, platform, or configuration
  validation beyond what the requested path needs to run.
- Do not add speculative input validation. Validate only what the existing
  public boundary already requires.
- Do not add tests for unrequested behavior. Prefer one scenario test for the
  requested path when a change affects behavior.
- Do not add abstractions, configuration options, compatibility layers, or new
  modules when direct code is sufficient.
- Do not treat security, deployment, packaging, observability, performance, or
  production-readiness findings as implementation scope. They are notes.
- Reviewers may report findings, but findings do not become tasks unless the
  request asks for them.
- A successful direct source run is sufficient acceptance unless the task
  defines additional criteria.
- Preserve existing repository-integrity checks. Do not introduce new policy
  checks.

## Authority and workflow

Authority order is: task request; this contract; source and tests. Start at
route registration, command tables, and imports. Trace callers and tests. A
failed name search is not proof of absence.

- Keep the requested file scope. Report any required expansion before editing.
- Work in an isolated worktree. Children do not merge branches; the root
  integrates, verifies, and cleans up.
- Reproduce before changing code. Remove the cause; do not mask it with a
  fallback, retry, default, or catch-all. Preserve protocol, schema, worktree,
  and module boundaries.
- Use adversarial review for consequential changes. Report exact commands,
  working directories, exit statuses, and observed evidence.
- Do not force-push, rewrite shared history, reset another worktree, or delete
  work to hide a failure. Secrets stay in the environment and never in task
  specs, events, logs, or commits.

## Run

Cambium is a Python-native multi-agent coding-agent harness run directly from
source. No wheel or install is required. Use `uv` for environments and
commands; use the system interpreter for direct runs.

```sh
cd /home/ubuntu/cambium
PYTHONPATH=src python3.14 -m cambium.cli supervisor --session-dir demo
PYTHONPATH=src python3.14 -m cambium.cli --help
```

The `cambium` CLI exposes `auth`, `supervisor`, `doctor`, `bench`, `tasktree`,
`module-test`, and `version`. Worker subprocesses receive an absolute
`PYTHONPATH` to the source tree, so child imports resolve without an install.

## Current entry points and behavior

- `supervisor.run_plan` validates a flat task list, starts one runtime, and
  fans tasks out under an `asyncio.TaskGroup`. It creates `store.EventStore`,
  writes `.cambium/result.json`, and publishes a clean worker whose envelope
  reports `succeeded` by an expected-old update of `refs/heads/main`. There is
  no pre-merge gate: the worker verdict alone decides merge eligibility.
  Publication is ref-only; it does not refresh a checkout.
- Each worker is a process group in a Git worktree. Its stdout is NDJSON only;
  diagnostics use stderr/logging. The supervisor bounds each worker's decoded
  stdout queue and routes emitted records through `EventStore`.
- `worker.do_work` selects deterministic marker mode unless `fanout_config` is
  present. Provider mode runs the bounded `Diffundo` loop: one provider call
  per turn, strict `tool_call`/`finish` parsing, schema and permission checks,
  tool events, checkpoints, and one fenced commit.
- `run_shell` and `git_op` execute without an approval gate. `approval.py` and
  `resources.py` remain standalone reusable primitives with their own tests.
- `tasktree.build_tree` validates dependency specs; `run_plan` does not
  schedule a DAG. Architectus and the conversation store are not wired into
  `run_plan`.
- `doctor` reports runtime, worktree, provider/auth, optional stores, dataset
  integrity, and advisory host health.

## Boundary invariants

- IPC framing is bounded and request/generation-correlated. Malformed advisory
  lines are logged or skipped; fatal framing, missing correlated results,
  non-zero exits, and deadline failures fail or restart the task according to
  the boundary policy.
- A merge conflict, non-fast-forward, or stale expected-old ref never
  publishes `main`.
- Provider keys are allowlisted environment values. Never put credentials or
  sensitive content in task specs, event payloads, or durable artifacts.
- Keep blocking disk and subprocess I/O at existing thread/process boundaries.

## Checks and handoff

Tests are example data-in/data-out pairs: deterministic module input produces
the expected module output. Run the narrowest real check, then the affected
package check when a boundary changes. Useful system commands from the
repository root:

```sh
PYTHONPATH=src python3.14 -m cambium.cli supervisor --session-dir demo
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
