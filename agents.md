# Cambium contributor notes

Read the relevant source and follow one live path before editing. Start with
[the runtime map](docs/architecture/architecture.md), then the owning subsystem
document. Source describes what runs; a proposal or a green fixture is not
proof that a feature is wired or correct.

## Work style

Keep the harness small: ordinary functions, explicit data and ownership, local
control flow, existing code and the standard library before new dependencies.
Fix the cause at its owner. Do not add a planner, reviewer, approval gate,
compatibility layer, fallback, memory database or scheduler merely to conceal
a failed call or incomplete integration.

Use a short plan when the work needs one, not a mandatory template before every
read. Make the smallest complete change and explain remaining uncertainty.

## Parallel work and publication

Work in an isolated worktree based on freshly fetched `origin/main`. Reuse it
for continued work. Preserve other checkouts, uncommitted changes and active
parallel branches. Do not reset, clean or force-push shared work.

Inspect the diff, commit the actual changes, integrate with current `main`, and
verify the remote ref after pushing. Do not claim a merge, publication or test
run without observing it. A task handoff states the commit, checks performed
and remaining limitations; it need not create another status ledger.

## Runtime owners

```text
CLI / interactive session -> supervisor -> isolated worker -> provider/tools
                                   |              |
                                   |              +-> context checkpoints
                                   +-> child lifetime, joins and publication

events + checkpoints + Git + provider usage -> inspection and TUI
```

The root and children use one worker implementation. A model proposes actions;
the harness executes them. A model saying a check passed or a child merged is
not evidence that it happened. Keep task ancestry, context lineage, provider
identity and accepted Git state separate.

The active tools are defined once in `schemas.py` and dispatched in `tools.py`.
Navigation and history are live read-only tools; do not duplicate their roster
in a source-pinning test. A direct name/arguments tool request is unambiguous
without an extra type tag. Actual arguments and tool ownership are validated
at their execution boundary.

## Context and resources

[CAST](docs/architecture/context-engine.md) owns the context model: stable head,
append-only semantic entries, bounded raw tail, and a separate K0 rollover.
Earlier evidence remains in history. Cache hits come from provider usage, not
matching hashes or warm worker processes.

[Delegation](docs/architecture/context-branches.md) explains model-directed
work splitting and automatic context/placement defaults. The supervisor owns
child workspaces and provider selection. Parents join or cancel their children
before releasing their integration workspace. Preserve unrelated local work.

Provider admission belongs to routing/supervisor; call-time attempts belong to
Diffundo; account windows and cache capabilities have their existing owners.
Unknown quota or tariff stays unknown. Prompt tokens are not output throughput.
Keep credentials out of source, prompts, tool output and durable logs. Preserve
redaction, terminal sanitization and Git ownership boundaries.

DSPy optimizes policy text offline. [Prompt replacement](docs/architecture/optimization.md)
applies to new sessions; it does not rewrite an active prefix or add an online
classifier call. Do not make the coding loop depend on DSPy imports.

## Verification

Run the smallest check that reproduces the changed behavior, then the affected
suite. Use real PTYs for input/resize claims and real providers for model-loop
claims. Check accepted Git artifacts, not only the model's summary. A successful
shell command alone is not general verification.

Useful commands, in the repository's installed environment:

```sh
ruff check src tests
python -m pytest -o addopts='' tests/scenarios/<affected-file>.py -q
python -m pytest -o addopts='' -n 2 -m 'not acceptance' -q
python -m pytest -o addopts='' -m acceptance tests/acceptance/test_live_frontends.py -q
git diff --check
```

Live tests use configured providers and consume quota. Report failed trials as
well as passing reruns. Do not manufacture success when a budget ends, and do
not delete a meaningful regression simply to get a green run. Consolidate
repeated fixtures and tests that pin implementation trivia; retain checks for
actual effects, data loss, cancellation, replay and publication.

## Documentation

Give each contract one owner: architecture for rationale/ownership, reference
for exact shapes, how-to for sequences, research for measurements. Link rather
than copy. Mark proposals explicitly. Keep only open work in
[implementation-plan.md](implementation-plan.md). Update the owning document
when behavior changes; do not require a documentation or certification ritual
before a normal tool call.
