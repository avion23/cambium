# agents.md — Cambium operating contract

Read this before changing the repository. Use the task request, current source,
executable tests, durable records, and accepted Git state as authority. A role
name, README claim, design target, old review, branch name, or green test count
is not proof that a runtime path is wired.

## 1. Orient before acting

Start every task with a small situation note:

```text
objective       what observable outcome is requested
done_when       exact completion and verification criteria
authority       files/modules/branches you may change
current_truth   current branch/head and relevant live entry points
unknowns        facts that still require source or executable evidence
next_evidence   cheapest action that can resolve the largest uncertainty
```

Then trace one complete live path before editing:

```text
public command/API/schema
    -> dispatcher/caller
    -> owning module
    -> effect boundary
    -> durable result/event
    -> scenario tests
```

A failed name search is not proof of absence. Check imports, registries, schemas,
entry points, and tests. When docs and source disagree, state the disagreement;
do not silently choose the more attractive story.

For system-level work, read in this order:

1. [`docs/architecture/agent-operating-model.md`](docs/architecture/agent-operating-model.md)
2. [`docs/architecture/architecture.md`](docs/architecture/architecture.md)
3. [`implementation-plan.md`](implementation-plan.md)
4. the focused subsystem document and source/tests

## 2. Authority order

```text
1. explicit user task and constraints
2. this operating contract
3. accepted current source and tests
4. current architecture/reference documents
5. implementation plan for open work
6. research and historical reviews
```

Source is not automatically correct, but it is the current executable fact.
Target documents explain where it should go. Do not report target behavior as
landed.

## 3. System model

Cambium is one linked control system, not a bag of utilities:

```text
repository/providers/operator intent
        -> tools/workers/transports/merge
        -> events/checkpoints/Git/quota
        -> CAST/artifact/provider views
        -> canonical BranchState          target integration layer
        -> model SituationFrame + TUI
        -> agent/supervisor decisions
        -> evaluation and policy promotion
```

Keep these identities separate:

```text
task tree
conversation branch
Git artifact graph
provider-cache lineage
semantic/epistemic projection
```

The root and every child use the same branch abstraction. The model proposes
intent. The supervisor owns admission, provider lease, process lifecycle,
children, budgets, join, publication, and recovery. Git owns artifact identity;
provider responses own cache-hit evidence; tests own only the checks they
actually ran at a particular artifact state.

## 4. Development mode

KISS is the default. Implement only the requested behavior and the minimum
support needed to make it correct and reviewable.

Unless the task explicitly requests it:

- do not add gates, approvals, sandboxes, retries, fallbacks, readiness checks,
  or production-hardening systems;
- do not add hashes, attestations, evidence stores, accounting, or observability
  merely because they might be useful;
- do not add environment, dependency, credential, platform, configuration,
  input, or schema validation beyond preserving an existing boundary;
- do not add abstractions, compatibility layers, options, or modules when a
  direct change is sufficient;
- do not turn review findings into implementation scope;
- do not add broad interface tests; prefer one scenario that proves the changed
  behavior through its live path;
- do not micro-optimize Python before measuring a local bottleneck;
- do not refactor unrelated code while repairing a concrete defect.

When the task explicitly changes an architectural boundary, add the smallest
value object/reducer/interface that gives that boundary one owner. Avoid a
second scheduler, memory database, frontend state machine, or worker hierarchy.

## 5. Change workflow

### 5.1 Pull and establish the baseline

```sh
git status --short --branch
git fetch --all --prune
git rebase origin/main          # or the explicitly requested target branch
git rev-parse HEAD
git log -1 --oneline
```

Do not overwrite unrelated local work. Use an isolated branch/worktree for
changes unless the task explicitly authorizes direct target-branch work.

### 5.2 Reproduce or prove the gap

For a bug, run the smallest real reproduction before editing. For a missing
integration, trace the symbol from schema/entry point through dispatch and show
where it stops. For documentation work, compare each implementation claim with
the current source path.

Record:

```text
command or source path
working directory / branch / commit
observed output or absence
expected invariant
```

### 5.3 Change one owner

Remove the cause at its owning boundary. Do not mask it with a default, catch-all,
retry, duplicate state, or wrapper. Preserve protocol, schema, task-tree,
worktree, provider, context, and publication ownership.

### 5.4 Verify in layers

```text
syntax/static check
    -> focused reproduction
    -> affected scenario/module suite
    -> combined integration check
    -> full fast/slow gates when required
```

Inspect the diff before broad tests. A passing check is evidence only for the
artifact/configuration it tested. Rerun overlapping checks after later edits.

### 5.5 Commit and publish

```sh
git diff --check
git status --short
git diff --stat
git diff --cached
git commit -m "<focused message>"
git push <remote> <requested-branch>
git ls-remote <remote> refs/heads/<requested-branch>
```

Never claim a push, branch, tag, merge, or CI result without fetching the remote
state. Do not force-push or rewrite shared history unless the user explicitly
requires it and the expected old remote SHA is verified.

## 6. Runtime entry points

Prefer the public CLI over internal module entry points:

```text
auth
supervisor
doctor
module-test
version
run
repl
tui
monitor
quota
optimize
session
architectus
```

Typical development commands:

```sh
PYTHONPATH=src python3.14 -m cambium.cli --help
PYTHONPATH=src python3.14 -m cambium.cli supervisor --session-dir demo --demo
uv run cambium tui --repo . --auto
uv run cambium doctor
```

Supervisor plans require a session directory and `--plan`, `--task-spec`, or
`--demo`. Operator-facing context reuse is on by default.

Current main flow:

```text
CLI/interactive/plan
    -> supervisor.run_plan
    -> task/provider admission
    -> isolated worktree + worker process
    -> worker provider/tool loop
    -> checkpoint/result/exit events
    -> integrity and join/publication
    -> canonical result and cleanup/recovery
```

Current active model tools are:

```text
write_file
edit_file
git_op
run_shell
read_batch
delegate
```

`branch_history.py`, `code_index.py`, and `lsp_query.py` are implemented library
boundaries but are not yet in that active roster. Do not describe them as model
capabilities until the schema, dispatch, prompt, provider-tool hash, and live
scenario path are wired.

## 7. Context and branch rules

- The active context is a stable system/tool head, immutable CAST summary
  entries, and a bounded raw tail.
- A summary covers one disjoint raw range. Existing entries are immutable.
- K0 is a bounded current-state materialization, not a second summary tier.
- Exact cache reuse requires byte/identity compatibility; provider cache hits
  come only from provider evidence.
- Current supervisor source consumes declared child `context_mode` and
  `placement`. The model schema still permits omission and automatic
  exact/semantic resolution; treat this as a current compatibility gap, not an
  implicit normative default.
- A child cannot widen parent filesystem, tool, credential, or provider
  authority.
- Admission is durable before spawn. Child completion order does not determine
  join order.
- Semantic child result and Git artifact acceptance are separate. A parent may
  resume with write authority only when its worktree matches the accepted
  integration head.

Use [`docs/architecture/context-engine.md`](docs/architecture/context-engine.md),
[`docs/architecture/context-branches.md`](docs/architecture/context-branches.md),
and [`docs/architecture/subagents.md`](docs/architecture/subagents.md) for the
focused contracts.

## 8. Provider and credential rules

- Provider admission belongs to `routing.py`/supervisor; call-time attempts
  belong to `diffundo.py`; quota/cache/lease values belong to
  `provider_scheduler.py`; configuration belongs to `provider_config.py`.
- Do not create another scheduler or let prompt prose select credentials.
- Hard feasibility precedes ranking.
- Missing credentials fail before worker spawn where possible.
- Request rate, in-flight capacity, token windows, cash, wall time, and cache
  state are separate dimensions.
- OAuth refresh tokens remain in the supervisor-side store. Workers receive
  only the access token/account identity needed for the assigned provider.
- Secrets stay in approved environment/store boundaries and never enter task
  specs, prompts, events, logs, tests, fixtures, or commits.
- Redaction and terminal sanitization are boundary contracts; preserve them.

Focused references:

- [`docs/architecture/provider-routing.md`](docs/architecture/provider-routing.md)
- [`docs/research/codex-activation.md`](docs/research/codex-activation.md)
- `src/cambium/provider_config.py`
- `src/cambium/oauth.py`
- `src/cambium/auth.py`
- `src/cambium/diffundo.py`

## 9. Process, Git, and persistence invariants

- Worker stdout is bounded NDJSON; diagnostics use stderr/log events.
- Request IDs correlate protocol messages; generation tokens own effects.
- Blocking disk/subprocess work stays at existing thread/process boundaries.
- One worker generation owns at most one fenced commit.
- A provider-backed read-only task may succeed without a commit; no empty commit
  or merge is created.
- A dirty, detached, wrong-branch, stale-base, conflicted, quarantined, or
  envelope-inconsistent worker result is not published.
- Publication is expected-old and ref-only. Do not reset a caller-owned primary
  checkout to make it visually match.
- Recovery preserves salvage and the last safe checkpoint; resume requires a
  matching workspace identity.
- Frontends and monitors derive state from durable records and do not mutate the
  runtime directly.

## 10. Testing guidance

Run the narrowest real check first. Useful commands from the repository root:

```sh
uv run ruff check src tests
uv run pytest -q tests/scenarios/<focused-file>.py
uv run pytest -m "not slow" -q
uv run pytest -m slow -q
python3.14 -m compileall -q src tests
git diff --check
```

Use the repository's locked/dev environment when available. Credential-gated
acceptance tests may issue live calls and are separate from hermetic CI.

Scenario tests prove process, Git, persistence, concurrency, context, and
provider boundaries. Module tests prove deterministic data-in/data-out logic.
Do not test an interface merely for existing; test an externally meaningful
state transition or invariant.

## 11. Documentation work

Documentation categories have distinct purposes:

```text
architecture     rationale, ownership, invariants, current/target boundary
reference        exact public or target values
how-to           recommended workflow
research         hypotheses and evaluation
implementation-plan ordered open work only
```

For an implemented claim, name the live source/test path. For target behavior,
label it target. Remove stale source line numbers and branch-ledger prose rather
than preserving a misleading history in active docs.

When changing an enum, tool, prompt component, event, public state, or command,
update all executable and documentary consumers in the same change.

## 12. Handoff

Every handoff states:

```text
scope and ownership
base branch and starting SHA
entry points/source/tests inspected
baseline or reproduction and observed result
changes and preserved invariants
verification commands, cwd, exit status, and evidence
commit SHA and remote branch
independent remote verification
status: VERIFIED | UNVERIFIED | BLOCKED
remaining exact next action
```

Do not say “all tests pass,” “merged,” “pushed,” or “implemented” when the
corresponding command or remote state was not observed.
