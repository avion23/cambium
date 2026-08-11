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
- Do not add environment, dependency, credential, platform, configuration,
  input, or schema validation unless the task explicitly requests it. Preserve
  existing boundary validation.
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
source. No wheel is built and no install is required or supported. Use `uv`
only for environment setup; direct runs and the commands below use the system
interpreter.

```sh
cd /home/ubuntu/cambium
PYTHONPATH=src python3.14 -m cambium.cli supervisor --session-dir demo
PYTHONPATH=src python3.14 -m cambium.cli --help
```

The `cambium` CLI exposes `auth`, `supervisor`, `doctor`, `bench`, `tasktree`,
`module-test`, `version`, `run`, `repl`, `tui`, and `session` (`session
list/latest/show` reads completed session results); prefer it over the internal
supervisor module.
Worker subprocesses receive an absolute `PYTHONPATH` to the source tree, so
child imports resolve without an install.
Root `conftest.py` exports `src` on `PYTHONPATH` so scenario subprocesses
import `cambium` without a manual export.

### Provider auth modes

Provider entries carry a tagged `auth`/`protocol` mode in `providers.json`
(`src/cambium/provider_config.py`). The legacy `api_key` + `chat_completions`
pair is unchanged and still requires `base_url`/`api_key_env`; `codex_chatgpt`
is pinned to the `CODEX_CHATGPT_PROFILE` module constants, requires protocol
`codex_responses`, and rejects `base_url`/`api_key_env` in the file so a
modified provider file can never redirect the bearer token.

The `codex_responses` transport (`src/cambium/diffundo.py`) posts the
Responses-API shape to the profile endpoint, streams SSE `output_text` deltas,
and maps errors to retryable / CONFIG-quarantine / refusal classes. The bearer
token and ChatGPT account id come from an injected `CredentialSource` only
(absent -> AUTH_ERROR fail-closed); optional `reasoning_effort` (codex entry
sets "max") rides the request body.

### Usage evidence

`supervisor.run_plan` resolves un-pinned provider tasks (`model_candidates`)
at admission from the usage-debt ledger (`DebtStore`,
`~/.config/cambium/routing-state.json`) and presets the worker's Diffundo
primary to the assigned provider.

Provider lanes (H1): one concurrency lane per provider
(`routing.LaneState`); `run_plan` pre-assigns each wave's un-pinned tasks in
one batch pass, and 429 pressure decays a lane's in-flight cap.

Capability/quality-constrained selection (H2): a task may declare
`requirements` (`quality` high/normal, optional `min_context_window`); the
supervisor then filters providers strictly by capability and picks the lowest
`routing.score_providers` score (utilization, cache-hit rate, latency, shadow
price) instead of `select_lane`, and the `task_assigned` event carries the
requirements. Unknown requirement keys fail closed.

`scripts/usage_evidence.py` aggregates durable per-call usage events
across session stores (positional session dirs and/or `--repo <path>`,
which globs `.cambium/sessions/*`) into per-provider routing evidence:
request counts, tokens, latency, cost, provider-reported cache-hit
rate, prompt-prefix stability, Retry-After, request-rate status,
failure reasons, and quota owners (`--json` for machine-readable
output). Sessions without usage events are skipped; missing event DBs
warn and exit 0.

## Eval isolation

`scripts/isolated_eval.sh --repo <path> [--eval module-test|bench|pytest|all]
[--worktree <path>] [--bench-root <path>]` snapshots a repository's committed
state with `git clone --shared`, makes the copy read-only (ro bind mount when
permitted, `chmod -R a-w` fallback), and runs the eval from that copy with the
cambium venv interpreter, so the suite never executes inside a mutable agent
worktree and working-tree tampering cannot reach the eval. Results go to
stdout and to a file under /tmp; the source repo is never written.

## Current entry points and behavior

- `supervisor.run_plan` validates a flat task list, starts one runtime, and
  fans tasks out under an `asyncio.TaskGroup`. It creates `store.EventStore`
  and writes `.cambium/result.json`. A clean worker whose envelope reports
  `succeeded` with a commit publishes that commit by an expected-old update of
  `refs/heads/main`. A provider-backed task that changed no files completes
  successfully with a conversational/read-only answer: no commit is made and
  nothing is merged or published — no empty commit or merge occurs. There is
  no task-command pre-merge gate: a succeeded envelope with a commit proceeds
  to merge only after the repository-integrity checks pass (worker success
  integrity, fencing, expected-old ref publication, session admission,
  worktree confinement, protocol/request correlation, and quarantine).
  Publication is ref-only; it does not refresh a checkout.
- Each worker is a process group in a Git worktree. Its stdout is NDJSON only;
  diagnostics use stderr/logging. The supervisor bounds each worker's decoded
  stdout queue and routes emitted records through `EventStore`.
- Warm worker pool (eval-3 ADOPT): the supervisor keeps a bounded
  session-scoped pool (`CAMBIUM_WARM_POOL_SIZE`, default 1; 0 disables) of
  idle reuse-ready workers and rebinds them to new worktrees via a full
  second init instead of spawning a fresh interpreter per task. Only the
  first generation of a task may pop the pool; restarts always spawn fresh;
  pooled workers are killed at session end. A pooled worker only serves a
  task whose env matches (session, provider config, credentials) and rebuilds
  all per-task state (agent loop, transcripts, tool state, LM clients) from
  the rebind init.
- `worker.do_work` selects deterministic marker mode unless `fanout_config` is
  present. Provider mode runs the bounded `Diffundo` loop: one provider call
  per turn, strict `tool_call`/`finish` parsing, schema and permission checks,
  tool events, checkpoints, and one fenced commit when the agent changed
  files. A provider task that changed no files completes successfully with a
  conversational/read-only answer and no commit; its summary is carried in the
  result and the rendered output. The deterministic marker worker always
  writes its marker and commits.
- Worker-exposed `run_shell` and inspection-only `git_op` run without an
  `ApprovalGate` or `CompileGate`; mutating Git operations are not
  worker-exposed. `approval.py` and `resources.py` are deleted.
- `tasktree.build_tree` validates dependency specs; `run_plan` does not
  schedule a DAG. Architectus and the conversation store are not wired into
  `run_plan`.
- `doctor` reports runtime, worktree, provider/auth, optional stores, dataset
  integrity, and advisory host health.

## Boundary invariants

- IPC framing is bounded and correlated by `request_id` (generation is not
  enforced for message correlation). Malformed lines that fail JSON parsing are
  counted and skipped up to a bound; a valid JSON line that is not an object
  currently fails supervision (open defect). Fatal framing, missing correlated
  results, non-zero exits, and deadline failures fail or restart the task
  according to the boundary policy.
- A merge conflict, non-fast-forward, stale expected-old ref, or quarantine
  violation never publishes `main`.
- A provider-backed task may complete successfully with a conversational/
  read-only answer and no file/commit; no empty commit or merge occurs. An
  edit task still commits once and merges normally; the marker worker always
  writes its marker and commits.
- Provider keys are allowlisted environment values. Never put credentials or
  sensitive content in task specs, event payloads, or durable artifacts.
- Keep blocking disk and subprocess I/O at existing thread/process boundaries.

## Checks and handoff

Module tests are example data-in/data-out pairs: deterministic module input
produces the expected module output. The scenario suite also covers process,
git, persistence, and concurrency behavior. Run the narrowest real check, then
the affected package check when a boundary changes. Useful system commands from
the repository root:

```sh
PYTHONPATH=src python3.14 -m cambium.cli supervisor --session-dir demo
python3.14 -m pytest -q src/cambium/modules/example/tests/
python3.14 -m compileall src tests
PYTHONPATH=src python3.14 -m cambium.cli --help
git diff --check
```

Before commit, inspect status and diff and stage only intended files. Before
handoff, verify the worktree is clean. Every handoff states:

- Scope and files in scope.
- Authority and entry points read.
- Baseline/reproduction command, cwd, and result.
- Change and preserved boundary.
- Checks with command, cwd, exit status, and evidence.
- Status: `VERIFIED`, `UNVERIFIED`, or `BLOCKED`.
- Next action.
