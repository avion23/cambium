# Worker cold-start cost: fork-per-task vs persistent pool

**Snapshot (2026-08-09):** historical benchmark from
`/tmp/opencode/cambium-coldstart` (branch `wt-coldstart`); artifacts are in
`/tmp/opencode/exp-coldstart` (outside the worktree). It compares the v2.1
persistent pool with fork-per-task. Check current dspy metadata at the [dspy
PyPI JSON](https://pypi.org/pypi/dspy/json). Every number is a host
measurement; unchecked items are **UNVERIFIED**.

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

The dspy and fork-dspy rows were re-measured on 2026-08-09; the table keeps
the resulting medians/p90s. Earlier figures without raw data are superseded.

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

## Appendix: re-measured evidence (dspy rows, fork-dspy, fan-outs)

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

The `-X importtime` verification:
`/home/ubuntu/.cache/uv/archive-v0/z2NVN2upFNYcb8-P/bin/python -X importtime -c "import dspy" 2>&1 | tail -1`
→ `import time:       462 |    1764228 | dspy` (loadavg 6.24), full log
`/tmp/opencode/exp-coldstart/importtime.dspy.log`. The literal
`uv run --python 3.14.7 -X importtime python -c "import dspy"` prints uv usage
(uv consumes `-X importtime`) and is **UNVERIFIED as stated**; the adapted
command above is the real measurement.
