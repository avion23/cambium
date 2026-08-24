# Overhead profiling baseline

The harness is [`scripts/profile_overhead.py`](../../scripts/profile_overhead.py).
It measures existing code only; neither recorded run includes an optimization
or a behavior change. The latest refresh is recorded first, with the original
numbers retained in the historical section below.

## Reproduction

```text
PYTHONPATH=src python3 scripts/profile_overhead.py
```

The recorded run used 12 timed samples and 2 warmups per operation. It ran on
Python 3.12.3, Linux `aarch64`, with `/tmp` on a btrfs filesystem. Values are
`time.perf_counter()` wall-clock samples unless noted. `p95` is the nearest-rank
95th percentile. Import timing runs a fresh interpreter for each sample using
`python -X importtime`.

## Refresh — 2026-08-24 UTC

The refresh was captured from commit `a89c4f1` on the isolated worktree. The
machine had 4 CPUs. Load was already elevated before the run (13.46, 15.20,
14.52 for 1/5/15 minutes) and was 16.06, 15.61, 14.70 at the end of the
profiler, so scheduler, filesystem, and fresh-process timings are
load-confounded. The harness still used its established 12 timed samples and
2 warmups per operation.

### Current results

| Measurement | Representative load | Median | P95 | Mean | Unit |
| --- | --- | ---: | ---: | ---: | --- |
| Provider request construction (chat) | 8 messages, 4 tools; opener stops before network | 0.056 | 2.363 | 0.250 | ms/op |
| Provider request construction (Codex Responses) | 8 messages, 4 tools; opener stops before network | 0.050 | 4.432 | 0.422 | ms/op |
| Event persistence (`EventStore` critical write) | SQLite WAL result row; insert, checkpoint, fsync | 15.727 | 18.152 | 14.629 | ms/op |
| Checkpoint serialization (canonical JSON) | 6,925 canonical bytes; 8 messages | 0.069 | 3.400 | 0.350 | ms/op |
| Checkpoint epoch serialization + fsync | Immutable epoch file; 6,925 canonical bytes | 46.170 | 261.425 | 69.426 | ms/op |
| Git merge pipeline | Real git repo; stage, rebase, publish, cleanup | 487.264 | 1,063.247 | 551.936 | ms/op |
| TUI render (prepared event batch) | 32-event transcript; 120x40 cockpit; color disabled | 3.060 | 10.023 | 3.249 | ms/op |
| TUI render (derived per event) | Prepared 32-event batch | 0.096 | 0.313 | 0.102 | ms/event |
| Module startup (fresh process wall) | Fresh `/usr/bin/python3` process importing `cambium.supervisor` | 922.451 | 2,088.008 | 1,036.476 | ms/op |
| Module import (`-X importtime` cumulative) | Same fresh-process import | 644.311 | 1,279.219 | 688.130 | ms/op |
| Schema validation (`read_batch`) | Real tool schema; 16 paths | 0.026 | 0.027 | 0.026 | ms/op |
| Mailbox wait | Empty `asyncio.Queue`; producer publishes after one scheduler yield | 0.016 | 0.026 | 0.018 | ms/op |

The 100-round cProfile cross-check took 0.695 seconds. Its largest current
cumulative paths were `tui_screen.py:1847 render_cockpit` (0.589 seconds),
`_side_sections` (0.338 seconds), and `_sanitize` (0.169 seconds). As before,
cProfile instrumentation changes absolute timings; it identifies CPU shape,
not the wall-clock values in the table.

### Median comparison with the 2026-08-23 history

The comparison uses medians because the refresh ran under elevated host load.
The final column flags increases greater than 20%; it is an observation, not a
claim that the code caused the change.

| Measurement | Historical median (ms) | Refresh median (ms) | Change | >20% |
| --- | ---: | ---: | ---: | :---: |
| Provider request construction (chat) | 0.052 | 0.056 | +7.7% |  |
| Provider request construction (Codex Responses) | 0.047 | 0.050 | +6.4% |  |
| Event persistence | 14.923 | 15.727 | +5.4% |  |
| Checkpoint serialization | 0.066 | 0.069 | +4.5% |  |
| Checkpoint epoch serialization + fsync | 20.325 | 46.170 | **+127.2%** | **yes** |
| Git merge pipeline | 126.967 | 487.264 | **+283.8%** | **yes** |
| TUI render (prepared event batch) | 0.472 | 3.060 | **+548.3%** | **yes** |
| TUI render (derived per event) | 0.015 | 0.096 | **+540.0%** | **yes** |
| Module startup (fresh process wall) | 247.411 | 922.451 | **+272.8%** | **yes** |
| Module import (importtime cumulative) | 158.868 | 644.311 | **+305.6%** | **yes** |
| Schema validation | 0.026 | 0.026 | +0.0% |  |
| Mailbox wait | 0.017 | 0.016 | -5.9% |  |

**Regression flags:** checkpoint epoch serialization + fsync, the git merge
pipeline, both TUI rows, fresh-process startup, and cumulative module import.
Every flagged wall-clock path is especially sensitive to the observed load
(the TUI rows also show scheduler contention); repeat on a quiet host before
assigning these increases to quarantine boot, terminal-death fallback, GEPA,
eval, or lease changes. The request-construction, EventStore, schema, and
mailbox medians did not exceed the threshold.

### New hot-path microbenchmarks

These additional probes used `time.perf_counter()` for 100 timed iterations and
10 warmups, separate from the harness's 12-sample table. The probe machine
load was 23.30, 19.34, 16.54 at start and 22.96, 19.34, 16.55 at end (same
4-CPU host, therefore also elevated). Values below are milliseconds per
operation; the p95 and mean are included to expose contention-sensitive tails.

| Hot path | Fixture | Median | P95 | Mean |
| --- | --- | ---: | ---: | ---: |
| Provider-config load, sidecar absent | 6 entries: 5 valid + 1 invalid; sidecar removed before each load, so quarantine JSON is written and fsynced | 8.974 | 101.620 | 38.392 |
| Provider-config load, sidecar present | Same 6 entries; matching quarantine record pre-existing, so sidecar is read/deduplicated without rewrite | 0.502 | 1.638 | 0.664 |
| `Diffundo` construction | 7 `strong` providers | 0.0217 | 0.1428 | 0.0711 |
| `Diffundo._candidates` | Same 7 providers; tier selection with no model pin | 0.0308 | 0.3756 | 0.0845 |
| `Diffundo` construction + `_candidates` | Same 7-provider fixture, constructed per iteration | 0.0540 | 0.2038 | 0.1323 |
| `_bounded_items` coercion | 32 nested objects, depth 16, each canonical JSON payload exactly 2,000 bytes (the text cap) | 2.090 | 6.311 | 2.801 |

The absent/present provider-config rows intentionally exercise the new
quarantine sidecar path; a six-valid-entry file would not read the sidecar and
would not measure that distinction. The large absent-sidecar tail and all
hot-path p95 values should be treated as load-sensitive measurements.

## Historical results — 2026-08-23

| Measurement | Representative load | Median | P95 | Mean | Unit |
| --- | --- | ---: | ---: | ---: | --- |
| Provider request construction (chat) | 8 messages, 4 tools; opener stops before network | 0.052 | 0.092 | 0.056 | ms/op |
| Provider request construction (Codex Responses) | 8 messages, 4 tools; opener stops before network | 0.047 | 0.073 | 0.049 | ms/op |
| Event persistence (`EventStore` critical write) | SQLite WAL result row; insert, checkpoint, fsync | 14.923 | 17.577 | 15.403 | ms/op |
| Checkpoint serialization (canonical JSON) | 6,925 canonical bytes; 8 messages | 0.066 | 0.145 | 0.073 | ms/op |
| Checkpoint epoch serialization + fsync | Immutable epoch file; 6,925 canonical bytes | 20.325 | 57.270 | 22.886 | ms/op |
| Git merge pipeline | Real git repo; stage, rebase, publish, cleanup | 126.967 | 288.401 | 139.378 | ms/op |
| TUI render (prepared event batch) | 32-event transcript; 120x40 cockpit; color disabled | 0.472 | 0.542 | 0.478 | ms/batch |
| TUI render (derived per event) | Prepared 32-event batch | 0.015 | 0.017 | 0.015 | ms/event |
| Module startup (fresh process wall) | Fresh `/usr/bin/python3` process importing `cambium.supervisor` | 247.411 | 604.497 | 299.436 | ms/op |
| Module import (`-X importtime` cumulative) | Same fresh-process import | 158.868 | 321.337 | 169.213 | ms/op |
| Schema validation (`read_batch`) | Real tool schema; 16 paths | 0.026 | 0.267 | 0.047 | ms/op |
| Mailbox wait | Empty `asyncio.Queue`; producer publishes after one scheduler yield | 0.017 | 0.037 | 0.019 | ms/op |

The event-store row is a critical append, so it waits for the writer's
durability barrier. A non-critical append intentionally has a different
contract and is not comparable. The checkpoint epoch row includes the actual
content-addressed checkpoint path, validation/hashing, exclusive file create,
and file/directory fsync; the canonical-JSON row isolates serialization alone.
The merge row is the full `MergeSequencer` stage/rebase/ref-publish/cleanup
path, not a single `git` subprocess. The mailbox row measures a blocked,
supervisor-style `Queue.get` rather than a prefilled queue hit.

### Historical top three measured bottlenecks

Ranked by median wall time per operation (startup/import is one subsystem;
the importtime row is its independent evidence, not a second copy of the
ranking):

1. **Fresh-process startup/import — 247.411 ms median, 604.497 ms P95.**
   `python -X importtime` attributes **158.868 ms median** cumulatively to
   importing `cambium.supervisor`, or about 64% of the median startup wall
   time. This is a process-boundary cost, not provider/network latency.
2. **Git merge pipeline — 126.967 ms median, 288.401 ms P95.** Every sample
   used a real temporary repository and one clean worker commit, including
   staging worktree creation, rebase, atomic publish, and cleanup.
3. **Checkpoint epoch serialization + fsync — 20.325 ms median, 57.270 ms
   P95.** The operation writes the same immutable checkpoint shape used by the
   worker. The next measured persistence cost was the critical EventStore
   write at **14.923 ms median** (17.577 ms P95).

As a CPU-only cross-check, the harness also runs 100 rounds under `cProfile`.
In that pass the top cumulative functions were `tui_screen.py:353
render_cockpit` (90 ms total), `_transcript_lines` (35 ms), and
`_wrap_markdown` (34 ms). cProfile instrumentation changes absolute timings;
these values identify the render call's CPU shape, while the table is the
source for the wall-clock bottleneck ranking.

## Method and scope

- Provider requests use the real chat and Codex request-building methods with
  an opener that raises immediately after request construction. No credentials
  or network are used.
- Event persistence uses a temporary `EventStore` and a representative result
  event based on `tests/scenarios/test_store.py`.
- Prompt/tool and TUI data use the worker prompt builder and fixture shapes from
  `tests/scenarios/test_diffundo_codex.py` and `tests/scenarios/test_tui_screen.py`.
- Git uses temporary repositories and plumbing-created worker commits before
  timing, so branch fixture creation is excluded from the merge samples.
- Schema validation calls the production `validate_tool_call` implementation;
  mailbox timing uses the production asyncio primitives' shape without a
  worker process.

These are local baseline measurements, not performance guarantees. Disk,
scheduler, interpreter, and filesystem-cache state can move the tail values;
rerun the harness on the target host before making a performance decision.
