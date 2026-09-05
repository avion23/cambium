# Harness audit — 2026-09-04

## Scope and evidence

Started in an isolated worktree from fetched `origin/dev` (`fad5687`). Integrated
committed parallel navigation/prompt work (`cb42036`, `ecf8b1d`) and the later
architecture cleanup (`0509552`). Other checkouts' uncommitted work was not used
as an implementation or overwritten.

This is a test record, not a claim of a measured global performance improvement.
Real-provider results below used the configured `zai / glm-5.3` lane. Local PTY,
process, and scripted-provider cases are identified separately.

## Reproductions and changes

| Observed problem | Change and regression evidence |
| --- | --- |
| A total-token count could report 50,000 generated tokens/s for a call with only 20 output tokens in two seconds | Output-only rates across routing, rendering, and observability; missing output counts stay unknown. `test_resource_projection.py` |
| Quota rendering opened writable account storage and replay could show unrelated current state | Reduce per-provider/window event snapshots; explicit read-only account inspection. `test_resource_projection.py` |
| Decaying sample counts and totals separately changed measured provider speed | Decay evidence weight while preserving its mean. `test_routing_throughput.py` |
| Repeated task/generation/model-turn counters could resolve history from the wrong interactive turn | Scoped `@turn-NNNN` references; reject ambiguous unscoped references. `test_branch_history.py` |
| Navigation/history libraries were not active tools | Wire `repo_query` and `branch_history` through schema, dispatch, batching, prompt and real frontend paths. `test_navigation_tools.py`, `test_live_frontends.py` |
| The full rail lacked resource information and retained empty live-detail rows after completion | Compact resource/quota rows; collapse terminal-state detail. `test_resource_projection.py`, TUI row tests |
| Resize during typing caused a native libedit segfault | Deliver resize signals to the native input owner. PTY resize/typing stress |
| Rendering read libedit's mutable buffer from another thread and could abort in string conversion | No cross-thread native buffer access; live cells update without destructive input repaint. PTY stress and owner-access regression |
| Invalid model responses after the last valid tool call were missing from durable history | Checkpoint invalid responses and repair feedback on the existing path. `test_worker_agent_loop.py` |
| Generic action-repair feedback repeated a long list of action shapes | Short feedback names the parse error and JSON escaping; parser and retry bound unchanged |
| DSPy classifier construction mutated class inheritance at runtime | Ordinary optional DSPy base, lazy stable class exports, independent predictors and save/load tests |
| A process-cleanup fixture observed a newly created but empty PID file | Publish the fixture's PID file atomically; retain actual process cleanup assertions |

The native-input fix has an explicit tradeoff: a complete geometry repaint can
wait until the current edit finishes. Conversation/result/status cells continue
to update, and active cancellation/inspection work. Do not describe this as a
fully rewritten terminal editor or immediate full repaint in every condition.

## Executed checks

Using the checkout's source and the installed Python 3.14 environment:

```sh
PYTHONPATH=src python -m pytest -o addopts='' -n 2 -m 'not acceptance' -q
PYTHONPATH=src python -m pytest -o addopts='' -m acceptance -q -rs tests/acceptance/test_live_coding_gate.py tests/acceptance/test_live_frontends.py tests/acceptance/test_live_tui_coding.py
ruff check src tests
git diff --check
```

The final non-acceptance run passed **1,870 tests**, with **one skipped**. This
includes slow process and PTY tests, not only string rendering. The final live
run passed **all five** selected real-provider tests: coding publication,
impossible-task non-publication, CLI navigation/code change, TUI coding/history
retrieval, and a two-turn coding/read-only continuation without an empty commit.
Lint and whitespace checks passed.

Earlier runs were not uniformly successful. They exposed both native terminal
crashes, an import-boundary regression during the DSPy refactor, a stale prompt
assertion, a fixture publication race, and one real CLI task that exhausted its
invalid-action allowance. Those observations drove the changes above. The
subsequent passing live run does not establish a population-wide prompt success
rate or prove that a small feedback change alone caused its success.

The module tests passed 52 decomposition and 58 review cases. An actual DSPy
classifier call through Cambium's adapter returned `do_not_decompose` for a
single-comment correction, using one request and 422 tokens. This verifies the
adapter/inference path, not an optimization gain.

## What is deliberately not claimed

The coding worker does not load DSPy optimizer artifacts. No held-out coding
prompt improvement or automatic deployment was demonstrated. Finite token/call
budgets matter even where estimated incremental cash cost is zero.

The complete model-facing SituationFrame, unified operator/model reducer,
evidence-linked WorkLedger, richer ResultCapsule, and weekly-capacity-aware
useful-work ranking remain open integration work. Existing BranchState/CLI
inspection, summaries, checkpoints, joins, provider accounting, and navigation
are their starting points, not evidence that every proposed layer is complete.

The single [implementation plan](../../implementation-plan.md) lists the remaining
work. Current [runtime architecture](../architecture/architecture.md),
[provider accounting](../architecture/provider-routing.md),
[terminal contract](../architecture/terminal-interface.md), and
[offline optimization](../architecture/optimization.md) own the implementation
claims. This record does not introduce a new runtime gate or policy layer.
