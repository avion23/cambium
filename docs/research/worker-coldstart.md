# Worker cold-start cost: fork-per-task vs persistent pool

Research date: 2026-08-09. Worktree: `/tmp/opencode/cambium-coldstart` (branch
`wt-coldstart`). Benchmark directory: `/tmp/opencode/exp-coldstart` (outside
the worktree, per task). Purpose: decide the worker-spawn architecture open
question — persistent worker pool (v2.1) vs fork-per-task — with measured
numbers. Verification rule: every number below is a real measurement taken on
this host; anything that could not be measured is marked **UNVERIFIED**.

## Host and environment

- Linux aarch64, 4 cores, shared dev box (other agents' builds/processes ran
  concurrently; `loadavg` captured per batch, see below).
- `uv 0.12.2 (aarch64-unknown-linux-gnu)`; CPython `3.14.7` managed by uv
  (already downloaded, `~/.local/share/uv/python/cpython-3.14.7-linux-aarch64-gnu`).
- Project env: `UV_PROJECT_ENVIRONMENT=/tmp/opencode/exp-coldstart-venv`
  created once from `/home/ubuntu/cambium` via
  `uv sync --extra test --python 3.14.7` (cambium editable + `pytest`; project
  `dependencies = []`, `test = ["pytest>=8.0"]`).
- dspy: installed via `uv run --python 3.14.7 --with dspy python -c "import dspy"`
  → installs and imports on 3.14.7 (`dspy 3.3.0`). Its ephemeral env lives at
  `~/.cache/uv/archive-v0/z2NVN2upFNYcb8-P/`.

Timing method: each sample = wall time of one full subprocess invocation of the
given command (Python `subprocess.run`, `time.perf_counter`), excluding the
driver itself. Median/p90 computed on sorted samples. Primary batch ran at
`loadavg 2.6`; earlier exploratory runs at `loadavg 7–17` are not used in the
table.

## Per-operation measurements (median / p90, ms)

| Operation | N | median | p90 | min | max |
|---|---:|---:|---:|---:|---:|
| `uv run --python 3.14.7 python -c "pass"` (no project, empty dir) | 20 | **37.6** | **44.1** | 32.4 | 46.6 |
| `python3.14 -c "pass"` (managed interpreter, no venv) | 10 | **16.9** | **19.8** | 15.1 | 19.9 |
| `exp-coldstart-venv/bin/python -c "pass"` (venv) | 20 | **24.6** | **36.8** | 20.7 | 46.9 |
| `uv run` + `from cambium.orchestrator import Orchestrator` | 20 | **120.4** | **139.1** | 112.9 | 148.5 |
| `uv run` + `from cambium.modules.example.decide import ShouldDecomposeModule` | 20 | **78.3** | **96.4** | 69.1 | 103.5 |
| `venv/bin/python` + `import Orchestrator` | 20 | **100.2** | **118.9** | 94.0 | 121.9 |
| `venv/bin/python` + `import ShouldDecomposeModule` | 20 | **58.0** | **63.7** | 51.7 | 83.1 |
| `uv run --with dspy python -c "import dspy"` | 5 | **2166.8** | **2188.6** | 2069.0 | 2199.8 |
| `import dspy` only (dspy ephemeral env python, no uv) | 5 | **2126.1** | **2160.5** | 2039.9 | 2165.2 |
| subprocess worker: `python -c "import dspy; from cambium.modules.example.decide import ShouldDecomposeModule"` | 5 | **2087.4** | **2118.9** | 2041.2 | 2126.4 |
| fork from warmed parent (cambium pre-imported, RSS 22.8 MB) | 30 | **1.83** | **2.31** | 1.38 | 2.98 |
| fork from warmed parent (cambium + dspy pre-imported, RSS 89 MB) | 15 | **5.33** | **5.82** | 4.66 | 6.43 |

Representative raw samples (median run of each), ms:

- `uv run --python 3.14.7 python -c "pass"`: `[35.4, 39.0, 35.7, 38.0, 44.0, 32.4, 37.4, 37.2, 41.3, 33.1, 34.6, 42.6, 35.3, 45.0, 46.6, 41.5, 34.3, 43.0, 37.3, 37.8]`
- `uv run` + Orchestrator: `[148.5, 125.5, 137.2, 126.2, 113.3, 122.3, 122.8, 118.2, 112.9, 128.1, 138.2, 114.4, 114.7, 118.6, 115.6, 147.0, 126.9, 113.3, 115.1, 116.1]`
- `venv/bin/python` + ShouldDecomposeModule: `[53.6, 54.8, 57.1, 65.8, 63.4, 54.6, 53.7, 52.3, 51.7, 52.5, 57.4, 59.0, 59.3, 83.1, 60.8, 62.6, 56.5, 61.5, 58.6, 58.9]`
- `import dspy` only: `[2165.2, 2126.1, 2039.9, 2066.2, 2153.5]`
- fork, warmed (cambium): `[2.18, 2.98, 1.72, 2.95, 1.84, 1.52, 1.78, 2.17, 1.86, 2.31, 1.63, 1.82, 1.92, 1.73, 1.89, 1.83, 1.85, 1.82, 1.63, 1.38, 2.58, 2.12, 1.69, 1.88, 1.71, 1.76, 1.74, 1.78, 1.43, 1.88]`
- fork, warmed (cambium + dspy): `[6.43, 5.42, 5.12, 5.82, 5.96, 5.53, 5.27, 5.37, 5.28, 5.04, 5.33, 5.11, 4.66, 5.30, 5.38]`

### Derived costs (venv-based, no dspy)

- Pure python 3.14.7 interpreter floor: **16.9 ms** median.
- venv site-packages processing adds ~7.7 ms over the raw interpreter.
- `uv run` wrapper adds **~20 ms** over invoking the same interpreter directly
  (120.4 vs 100.2 on Orchestrator; uv runs no per-call sync with the pre-synced
  `UV_PROJECT_ENVIRONMENT` and a `dependencies = []` project).
- `import cambium.orchestrator` ≈ **75.6 ms** (100.2 − 24.6).
- `import cambium.modules.example.decide` ≈ **33.4 ms** (58.0 − 24.6).

### dspy

- `import dspy` ≈ **2.1 s**, verified with `-X importtime` (dspy cumulative
  1.71 s CPU; wall 2.24 s at `loadavg ~6`). dspy 3.3.0 installs on 3.14.7.
- The `uv run --with dspy` figure (2166.8 ms) is ~identical to the bare import
  (2126.1 ms): uv's `--with` ephemeral-env handling is negligible on cache hit;
  **dspy's own import is the entire 2.1 s**.

## 10-worker fan-out (wall time until all workers ready)

| Architecture | payload | samples (ms) | median (ms) |
|---|---:|---|---:|
| subprocess per task (fork-per-task) | cambium only | `194.4, 148.9, 176.8, 151.5` | **164.1** |
| subprocess per task (fork-per-task) | cambium + dspy | `5584.7, 7293.0, 6940.4, 6471.7` | **6706.1** |
| warm-fork from pre-imported parent (pool) | cambium only | `7.7, 8.3, 15.9, 26.7` | **12.1** |
| warm-fork from pre-imported parent (pool) | cambium + dspy | `23.97, 23.37, 23.12, 33.89` | **23.7** |

Fan-out measurement: 10 concurrent `os.fork` children that immediately exit
(pool) vs 10 concurrent `subprocess.Popen` that import the payload then exit
(fork-per-task), wall time from first spawn to last exit. N=4 (dspy subprocess
fan-out N=4, ~6.7 s each run).

## Conclusion

**Per-task cold-start budget for a 10-worker fan-out:** with the realistic
worker payload (cambium + dspy, per architecture v2.0 §2 the Opifex worker runs
a DSPy ReAct loop), fork-per-task costs **~2.09 s per worker** and
**~6.7 s wall for the 10-worker fan-out**. A pre-warmed pool forks a worker in
**5.3 ms** (89 MB parent) and brings up all 10 in **~24 ms**. For the
deterministic-only path (no dspy) the gap is smaller but still real: **~120 ms
per worker / ~164 ms fan-out** vs **~1.8 ms per fork / ~12 ms fan-out**.

**Recommendation: persistent pool, not fork-per-task — by ~280x on the
10-worker fan-out with dspy (6.7 s vs 24 ms), ~390x per worker (2.09 s vs
5.3 ms).** dspy's import dominates everything else in the worker path, so the
decision hinges on whether that 2.1 s is paid once or per task.

Caveat — pooling mechanism is a separate choice. The numbers support *not*
paying 2.1 s per task, but:
- `os.fork` from the supervisor contradicts architecture v2.0 invariants: the
  deterministic layer (Custos) must never import a DSPy module, workers must
  stay isolated (sandbox, own FDs), and fork-after-threads (asyncio supervisor,
  provider clients) is unsafe.
- The same win is available while keeping the existing subprocess + stdio IPC
  (`Nuntius` JSON-Lines/`request_id`) design: **pre-spawn a pool of subprocesses
  once (~2.1 s each, amortized over their lifetime) and reuse them across
  tasks**, so the marginal per-task cost is an IPC round-trip, not a process
  spawn. This is the v2.1 persistent-worker-pool direction.

Numbers not independently re-measured and therefore not used: none; every figure
above is a measured sample on this host. All measurements were taken under
concurrent third-party load (`loadavg` ranged 2.6–7 across batches); the fork
figures are the most robust (min 1.38 ms, tight distribution), the dspy fan-out
figures the noisiest (±~1 s).
