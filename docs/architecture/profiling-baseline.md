# Overhead profiling baseline

Baseline captured **2026-08-23 UTC** from commit `5d4448e` on the isolated
worktree. The harness is [`scripts/profile_overhead.py`](../../scripts/profile_overhead.py).
It measures existing code only; this baseline does not include an optimization
or a behavior change.

## Reproduction

```text
PYTHONPATH=src python3 scripts/profile_overhead.py
```

The recorded run used 12 timed samples and 2 warmups per operation. It ran on
Python 3.12.3, Linux `aarch64`, with `/tmp` on a btrfs filesystem. Values are
`time.perf_counter()` wall-clock samples unless noted. `p95` is the nearest-rank
95th percentile. Import timing runs a fresh interpreter for each sample using
`python -X importtime`.

## Results

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

## Top three measured bottlenecks

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
