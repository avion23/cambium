# Handoff: typing-cleanup wave (2026-08-21)

Goal: drive Pyright and Mypy diagnostics to zero in every owned file without
changing runtime behavior. This wave follows the completed hardening wave
recorded in `plan.md` §9. The root tree is intentionally uncommitted; this
wave adds typing-only edits on top of it.

## Environment

- Repo: `/home/ubuntu/cambium`. Python: `/home/ubuntu/.local/bin/python3.14`.
  Always set `PYTHONPATH=src`.
- Tools: `ruff`, `pyright`, `mypy` (on PATH). There is NO project
  type-checker configuration and none may be added (no `mypy.ini`,
  `pyrightconfig.json`, `[tool.mypy]`, `py.typed`). Fix diagnostics in code.
- Baseline (pre-wave): Pyright 664 diagnostics across 64 files (`src tests`);
  Mypy 176 error lines (`mypy src`).

## Isolated worktree recipe (exact)

```sh
git -C /home/ubuntu/cambium worktree add --detach /tmp/opencode/cambium-types-NN
rsync -a --exclude .git --exclude __pycache__ --exclude optimized \
  /home/ubuntu/cambium/ /tmp/opencode/cambium-types-NN/
```

The rsync copies the current dirty hardening state into the worktree. Do all
editing there. Do NOT commit. Do NOT edit anything under `/home/ubuntu/cambium`.

## Rules

1. Own only your assigned files. Never edit another agent's files,
   `conftest.py`, `pyproject.toml`, `plan.md`, `handsoff.md`, docs, or
   `optimized/`. If a correct fix needs an unowned file, skip it and report.
2. Typing-only changes: annotations, `cast`, typed aliases, `TypeIs`/overloads,
   small parameterized generics. No behavior changes, no API changes, no new
   dependencies, no refactors, no comment prose beyond what a targeted ignore
   requires.
3. Prefer real annotations over ignores. A scoped `# type: ignore[code]` /
   `# pyright: ignore[rule]` is allowed ONLY for missing third-party stubs
   (known: `dspy`, `tree_sitter_python`) or analyzer false positives, each with
   a one-line reason in your final report.
4. Runtime guards stay: never delete an `isinstance` check, validation, or
   fail-closed branch just to silence a checker. Narrow with `narrowing`,
   `cast`, or typed helpers instead.
5. Verify inside your worktree before reporting:
   - `PYTHONPATH=src /home/ubuntu/.local/bin/python3.14 -m pytest -q <your test files>`
   - `ruff check <your changed files>`
   - `pyright <your owned src files>` and `mypy <your owned src files>`
   - Target: zero diagnostics per owned file. Residuals need a stated blocker.
6. Report exactly: (a) files changed, (b) per-owned-file Pyright/Mypy counts
   before -> after, (c) commands run with exit status, (d) residuals and why,
   (e) any behavior-risk observation. Root integration diffs each owned file
   between your worktree and root, so keep edits strictly inside ownership.

## Ownership map (20 disjoint owners)

| #  | Owner surface | Files |
|----|---------------|-------|
| 01 | ipc | `src/cambium/ipc.py`; `tests/scenarios/test_ipc.py`, `test_ipc_partial_write.py`, `test_ipc_fuzz.py` |
| 02 | lm | `src/cambium/lm.py`; `tests/scenarios/test_lm.py` |
| 03 | diffundo src | `src/cambium/diffundo.py`; `tests/scenarios/test_diffundo.py`, `test_diffundo_ordering.py` |
| 04 | routing | `src/cambium/routing.py`; `tests/scenarios/test_routing_balance.py`, `test_routing_scored.py`, `test_routing_lanes.py`, `test_routing_edges.py` |
| 05 | worker | `src/cambium/worker.py`; `tests/scenarios/test_worker_provider.py`, `test_worker_pool.py`, `test_worker_agent_loop.py` |
| 06 | optimize | `src/cambium/optimize.py`; `tests/scenarios/test_optimize.py`, `test_dspy_program.py` |
| 07 | supervisor | `src/cambium/supervisor.py`; `tests/scenarios/test_supervisor_fanout.py`, `test_supervisor_hardening.py`, `test_supervisor_exception_hygiene.py` |
| 08 | provider config | `src/cambium/provider_config.py`; `tests/scenarios/test_provider_config.py` |
| 09 | repl/user cli | `src/cambium/repl.py`; `tests/scenarios/test_user_cli.py` |
| 10 | oauth | `src/cambium/oauth.py`; `tests/scenarios/test_oauth.py`, `test_oauth_wiring.py`, `test_oauth_edges.py` |
| 11 | analysis | `src/cambium/stats.py`, `selection.py`, `render.py` |
| 12 | store/usage | `src/cambium/store.py`; `tests/scenarios/test_store.py`, `test_usage_events.py` |
| 13 | cli/auth | `src/cambium/cli.py`, `auth.py`; `tests/scenarios/test_auth.py` |
| 14 | merge | `src/cambium/merge.py`; `tests/scenarios/test_merge.py`, `test_m6_staging.py` |
| 15 | modules | `src/cambium/module_conformance.py`, `modules/example/dataset.py`, `modules/example/dspy_program.py`, `modules/should_review/dataset.py`; `tests/scenarios/test_warm_pool_contract.py` |
| 16 | tools/ast | `src/cambium/tools.py`, `ast_tools.py`; `tests/scenarios/test_tools.py`, `test_marker_required_fields.py` |
| 17 | redact | `src/cambium/redact.py`; `tests/scenarios/test_redact.py`, `test_redact_drift.py` |
| 18 | misc src | `src/cambium/architectus.py`, `bench.py`, `orchestrator.py`, `doctor.py`, `jlens.py`, `schemas.py`, `fencing.py` |
| 19 | fixtures/misc tests | `tests/fixtures/env_worker.py`, `tests/fixtures/hierarchy_worker.py`; `tests/scenarios/test_context_epochs.py`, `test_system_health_boundaries.py` |
| 20 | diffundo tests b | `tests/scenarios/test_diffundo_codex.py`, `test_diffundo_weighted.py` |

Baseline per-file counts are recorded in `plan.md` §10.

## Integration protocol (root orchestrator only)

For each worktree: `diff -ru` every owned file against root, apply the delta
to root, then rerun full verification (fast tier, slow tier, Ruff, compileall,
`git diff --check`, full Pyright/Mypy inventories). Conflicts are impossible
by construction unless an agent edited out of scope; out-of-scope edits are
discarded and reported.
