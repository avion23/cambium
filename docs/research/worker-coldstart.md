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
| `uv run --with dspy python -c "import dspy"` | 5 | **2260.7** | **2579.9** | 2214.6 | 2736.0 |
| `import dspy` only (dspy ephemeral env python, no uv) | 5 | **2188.7** | **2285.6** | 2108.3 | 2304.4 |
| subprocess worker: `python -c "import dspy; from cambium.modules.example.decide import ShouldDecomposeModule"` | 5 | **2221.2** | **2349.2** | 2201.2 | 2404.3 |
| fork from warmed parent (cambium pre-imported, RSS 22.8 MB) | 30 | **1.83** | **2.31** | 1.38 | 2.98 |
| fork from warmed parent (cambium + dspy pre-imported, RSS 89 MB) | 10 | **5.60** | **6.87** | 3.80 | 7.42 |

The four dspy rows and both fork-dspy rows were re-measured on 2026-08-09 and
replaced with the new medians/p90s (raw data in the appendix); `loadavg` at
measurement time: `uv run --with dspy` 6.93; `import dspy` only 7.39;
subprocess worker dspy 6.81; fork (cambium+dspy) 5.59. The first batch's
figures for these rows (2166.8 / 2126.1 / 2087.4 / 5.33 ms) had no captured raw
data and are superseded.

Representative raw samples (median run of each), ms:

- `uv run --python 3.14.7 python -c "pass"`: `[35.4, 39.0, 35.7, 38.0, 44.0, 32.4, 37.4, 37.2, 41.3, 33.1, 34.6, 42.6, 35.3, 45.0, 46.6, 41.5, 34.3, 43.0, 37.3, 37.8]`
- `uv run` + Orchestrator: `[148.5, 125.5, 137.2, 126.2, 113.3, 122.3, 122.8, 118.2, 112.9, 128.1, 138.2, 114.4, 114.7, 118.6, 115.6, 147.0, 126.9, 113.3, 115.1, 116.1]`
- `venv/bin/python` + ShouldDecomposeModule: `[53.6, 54.8, 57.1, 65.8, 63.4, 54.6, 53.7, 52.3, 51.7, 52.5, 57.4, 59.0, 59.3, 83.1, 60.8, 62.6, 56.5, 61.5, 58.6, 58.9]`
- `import dspy` only (re-measured): `[2108.3, 2142.6, 2257.3, 2304.4, 2188.7]`
- `uv run --with dspy python -c "import dspy"` (re-measured): `[2736.0, 2345.8, 2248.3, 2260.7, 2214.6]`
- subprocess worker, dspy + cambium.decide (re-measured): `[2221.2, 2404.3, 2266.5, 2210.7, 2201.2]`
- fork, warmed (cambium): `[2.18, 2.98, 1.72, 2.95, 1.84, 1.52, 1.78, 2.17, 1.86, 2.31, 1.63, 1.82, 1.92, 1.73, 1.89, 1.83, 1.85, 1.82, 1.63, 1.38, 2.58, 2.12, 1.69, 1.88, 1.71, 1.76, 1.74, 1.78, 1.43, 1.88]`
- fork, warmed (cambium + dspy), re-measured: `[6.81, 5.94, 5.66, 5.86, 5.54, 3.80, 7.42, 4.03, 3.92, 3.80]`

### Derived costs (venv-based, no dspy)

- Pure python 3.14.7 interpreter floor: **16.9 ms** median.
- venv site-packages processing adds ~7.7 ms over the raw interpreter.
- `uv run` wrapper adds **~20 ms** over invoking the same interpreter directly
  (120.4 vs 100.2 on Orchestrator; uv runs no per-call sync with the pre-synced
  `UV_PROJECT_ENVIRONMENT` and a `dependencies = []` project).
- `import cambium.orchestrator` ≈ **75.6 ms** (100.2 − 24.6).
- `import cambium.modules.example.decide` ≈ **33.4 ms** (58.0 − 24.6).

### dspy

- `import dspy` ≈ **2.2 s**, re-verified with `-X importtime` (dspy cumulative
  1,764,228 µs = **1.76 s CPU**, `loadavg` 6.24; full log
  `/tmp/opencode/exp-coldstart/importtime.dspy.log`). dspy 3.3.0 installs on 3.14.7.
  The reviewer-suggested command `uv run --python 3.14.7 -X importtime python -c "import dspy"`
  does not run: uv parses `-X` itself and prints usage (no `--with dspy` and no
  dspy in the project env), so the equivalent is
  `$DSPY_ENV/bin/python -X importtime -c "import dspy" 2>&1 | tail -1` →
  `import time:       462 |    1764228 | dspy`.
- The `uv run --with dspy` figure (2260.7 ms) is ~identical to the bare import
  (2188.7 ms): uv's `--with` ephemeral-env handling is negligible on cache hit;
  **dspy's own import is the entire ~2.2 s**.

## 10-worker fan-out (wall time until all workers ready)

| Architecture | payload | samples (ms) | median (ms) | p90 (ms) |
|---|---:|---|---:|---:|
| subprocess per task (fork-per-task) | cambium only | `168.96, 177.61, 186.61, 204.23, 166.89` | **177.6** | **197.2** |
| subprocess per task (fork-per-task) | cambium + dspy | `8784.27, 8039.85, 6713.12, 7034.26, 6993.54` | **7034.3** | **8486.5** |
| warm-fork from pre-imported parent (pool) | cambium only | `8.01, 6.87, 7.18, 7.96, 7.05` | **7.2** | **8.0** |
| warm-fork from pre-imported parent (pool) | cambium + dspy | `35.44, 36.36, 38.91, 44.50, 46.01` | **38.9** | **45.4** |

Fan-out measurement: 10 concurrent `os.fork` children that immediately exit
(pool) vs 10 concurrent `subprocess.Popen` that import the payload then exit
(fork-per-task), wall time from first spawn to last exit. All rows re-measured
N=5 on 2026-08-09; `loadavg` at measurement time: no-dspy rows 5.2–5.6, dspy
subprocess row 8.69, dspy fork row 5.46. The first batch's figures for these
rows (164.1 / 6706.1 / 12.1 / 23.7 ms) had no captured raw data and are
superseded.

## Conclusion

**Per-task cold-start budget for a 10-worker fan-out:** with the realistic
worker payload (cambium + dspy, per architecture v2.0 §2 the Opifex worker runs
a DSPy ReAct loop), fork-per-task costs **~2.22 s per worker** and
**~7.0 s wall for the 10-worker fan-out**. A pre-warmed pool forks a worker in
**5.6 ms** (89 MB parent) and brings up all 10 in **~39 ms**. For the
deterministic-only path (no dspy) the gap is smaller but still real: **~100 ms
per worker / ~178 ms fan-out** vs **~1.8 ms per fork / ~7.2 ms fan-out**.

**Recommendation: persistent pool, not fork-per-task — by ~180x on the
10-worker fan-out with dspy (7.0 s vs 39 ms), ~400x per worker (2.22 s vs
5.6 ms).** dspy's import dominates everything else in the worker path, so the
decision hinges on whether that 2.2 s is paid once or per task.

Caveat — pooling mechanism is a separate choice. The numbers support *not*
paying 2.2 s per task, but:
- `os.fork` from the supervisor contradicts architecture v2.0 invariants: the
  deterministic layer (Custos) must never import a DSPy module, workers must
  stay isolated (sandbox, own FDs), and fork-after-threads (asyncio supervisor,
  provider clients) is unsafe.
- The same win is available while keeping the existing subprocess + stdio IPC
  (`Nuntius` JSON-Lines/`request_id`) design: **pre-spawn a pool of subprocesses
  once (~2.2 s each, amortized over their lifetime) and reuse them across
  tasks**, so the marginal per-task cost is an IPC round-trip, not a process
  spawn. This is the v2.1 persistent-worker-pool direction.

Numbers not independently re-measured and therefore not used: none; every figure
above is a measured sample on this host. All measurements were taken under
concurrent third-party load (`loadavg` ranged 2.6–8.7 across batches); the fork
figures are the most robust (min 1.38 ms, tight distribution), the dspy fan-out
figures the noisiest (±~1 s), and the dspy fork figures are COW/load sensitive
(first-touch page faults spike the p90: 6.87–34.5 ms across batches).

## Appendix: re-measured evidence (dspy rows, fork-dspy, 10-worker fan-outs)

Raw data for the re-measured figures lives outside the worktree at
`/tmp/opencode/exp-coldstart/measurements2.jsonl` (newline-delimited JSON, one
record per run; each record carries `loadavg`, `raw_ms`, `median_ms`, `p90_ms`).

Generation driver: `/tmp/opencode/exp-coldstart/bench2.py`, invoked as
`<py> bench2.py <mode> <N> <name> [dspy|nodspy] [-- cmd...]` with modes
`cmd` (subprocess wall time), `fork` (single fork of warmed parent),
`fansub` (10-worker subprocess fan-out), `fanfork` (10-worker fork fan-out).
The dspy scenarios used the dspy ephemeral env
`/home/ubuntu/.cache/uv/archive-v0/z2NVN2upFNYcb8-P/bin/python` with
`PYTHONPATH=/home/ubuntu/cambium/src`; no-dspy scenarios used
`/tmp/opencode/exp-coldstart-venv/bin/python`.

The records backing the updated table rows (verbatim from `measurements2.jsonl`,
rounded to 3 decimals):

```
{"mode": "cmd", "name": "uv-run-with-dspy", "n": 5, "loadavg": [6.93, 6.28, 6.25], "raw_ms": [2735.953, 2345.846, 2248.33, 2260.676, 2214.642], "median_ms": 2260.676, "p90_ms": 2579.91}
{"mode": "cmd", "name": "import-dspy-only", "n": 5, "loadavg": [7.39, 6.29, 6.25], "raw_ms": [2108.284, 2142.599, 2257.314, 2304.44, 2188.701], "median_ms": 2188.701, "p90_ms": 2285.589}
{"mode": "cmd", "name": "worker-subprocess-dspy-cambium", "n": 5, "loadavg": [6.81, 6.21, 6.22], "raw_ms": [2221.198, 2404.341, 2266.549, 2210.725, 2201.206], "median_ms": 2221.198, "p90_ms": 2349.224}
{"mode": "fork", "name": "fork-warmed-cambium-dspy", "n": 10, "loadavg": [5.59, 5.74, 6.08], "raw_ms": [6.806, 5.942, 5.655, 5.859, 5.541, 3.798, 7.419, 4.033, 3.92, 3.8], "median_ms": 5.598, "p90_ms": 6.868}
{"mode": "fansub", "name": "fanout-subprocess-10-nodspy", "n": 5, "loadavg": [5.19, 5.65, 6.04], "raw_ms": [168.961, 177.612, 186.613, 204.233, 166.892], "median_ms": 177.612, "p90_ms": 197.185}
{"mode": "fansub", "name": "fanout-subprocess-10-dspy", "n": 5, "loadavg": [8.69, 6.48, 6.31], "raw_ms": [8784.265, 8039.852, 6713.122, 7034.259, 6993.54], "median_ms": 7034.259, "p90_ms": 8486.5}
{"mode": "fanfork", "name": "fanout-fork-10-nodspy", "n": 5, "loadavg": [5.59, 5.74, 6.08], "raw_ms": [8.01, 6.873, 7.184, 7.956, 7.052], "median_ms": 7.184, "p90_ms": 7.989}
{"mode": "fanfork", "name": "fanout-fork-10-dspy", "n": 5, "loadavg": [5.46, 5.71, 6.06], "raw_ms": [35.438, 36.358, 38.908, 44.504, 46.008], "median_ms": 38.908, "p90_ms": 45.406}
```

The `-X importtime` verification:
`/home/ubuntu/.cache/uv/archive-v0/z2NVN2upFNYcb8-P/bin/python -X importtime -c "import dspy" 2>&1 | tail -1`
→ `import time:       462 |    1764228 | dspy` (loadavg 6.24), full log
`/tmp/opencode/exp-coldstart/importtime.dspy.log`. The literal
`uv run --python 3.14.7 -X importtime python -c "import dspy"` prints uv usage
(uv consumes `-X importtime`) and is **UNVERIFIED as stated**; the adapted
command above is the real measurement.
