# Persistent interactive terminal cockpit

**Status:** implemented operator contract; source and tests remain authoritative.

## Interactive session

`cambium tui` keeps one persistent CAST branch across operator turns. Each turn
gets a fresh worker leaf under `turn-000N`, and a successful checkpoint seeds the
next turn. (`src/cambium/interactive.py:368-405,1216-1274`)

Use `-c`/`--continue` to reopen the latest or a named durable interactive
session; without it, Cambium allocates a new root.
(`src/cambium/cli.py:459-475,1133-1164`; `src/cambium/interactive.py:408-485`)

## Turn lifecycle

- Each operator turn starts a fresh worker in an isolated supervisor leaf.
  (`src/cambium/interactive.py:1221-1235,1276-1309`; `src/cambium/tui.py:935-1006`)
- Checkpoint resume/reuse requires matching `workspace_hash` and cache identity:
  the supervisor gates turn resume by workspace hash, and the worker validates
  context-fork identity. (`src/cambium/supervisor.py:2860-2877`;
  `src/cambium/worker.py:2628-2713`)
- On mismatch, the runtime uses a semantic summary fork when possible; an exact
  fork skipped for `model mismatch` starts a fresh provider-specific turn, or a
  fresh prompt when no summary is usable. (`src/cambium/worker.py:2688-2713,4983-5044`;
  `src/cambium/observability.py:285-294`)
- Turn finalization completes before its result/checkpoint can publish the next
  branch head. (`src/cambium/worker.py:5776-5820`;
  `src/cambium/interactive.py:1313-1382`)
- Abnormal-exit recovery salvages dirty worktree state before cleanup or reset.
  (`src/cambium/supervisor.py:2758-2816,2940-2977`)

## Pointers

- Rendering contract: see [`terminal-interface.md`](terminal-interface.md).
- Operations: see [`operations.md`](operations.md).
