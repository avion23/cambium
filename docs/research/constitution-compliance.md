# Constitution Compliance Audit — Merged Implementation

**Author:** wt-audit-constitution worktree
**Date:** 2026-08-09
**Status:** Historical, read-only audit against `docs/research/coding-constitution.md` (a)–(l),
`agents.md` §§7–8, and the merged implementation. No code changed.

**Snapshot:** `main@6109a6a`; constitution patch was recorded at the same SHA. IPC/worker came
from `38e1d43`, task tree from `06ce0dc`; the audit's earlier baseline was `main@3d27ba3`.
The original branch-local `_merge_lock`/Custos finding remains historical. Current readers
should use `docs/architecture/architecture.md`, `src/cambium/`, and
`docs/research/v2-1-status.md`. Current notes: provider loop, Diffundo, EventStore, and root
`Result` exist; DLQ, eval cache, ResourceBudget, `worker_pool`, and `events` are absent; there is
no per-worker sandbox or production shell approval, and no dynamic hierarchy.

## 0. Scope and verdict key

Audited surfaces were `store.py`, `merge.py`, `supervisor.py`, `events.py`, `orchestrator.py`,
`doctor.py`, `__init__.py`, `modules/base.py`, `modules/example/{__init__,decide,dataset,metric}.py`,
`tasktree.py`, IPC/worker (`38e1d43`), and `tests/scenarios/*`. Repository-relative citations
were read directly; cold-start and unbuilt design norms are **UNVERIFIED**.

`COMPLIANT` means the snapshot satisfies the norm; `PARTIAL` records a concrete deviation;
severity is impact on merged code, not a hypothetical future risk.

## 1. Verdict table

| ID | Norm | Verdict | Severity | Evidence / finding |
|---|---|---|---|---|
| (a) | Measure before optimizing | COMPLIANT | — | Byte-accurate `ipc._read_line` is correctness-driven (`ipc.py:69–88`); cold-start evidence is documented, not re-profiled here. |
| (b) | Flat frozen/slots records | COMPLIANT | — | `events.py:14,22,31,41`; `base.py:21–22`; `decide.py:51–65`; `doctor.py:47`; `supervisor.py:53`. |
| (c) | No shared mutable state; single writer | COMPLIANT | — | Store writer thread and immutable handoffs (`store.py:112–115,172`); worker `to_thread` crosses only `threading.Event` (`worker.py:211`). Deviations: unbounded store queue and slice EventLog on the loop. |
| (d) | Functional core / imperative shell | COMPLIANT | — | Pure `should_decompose` and `Module.decide` seam (`decide.py:68–161`). |
| (e) | Loop-affine state; queues over locks; bounds | PARTIAL | LOW | `store.py:107` uses a minimal `threading.Lock`; queue is unbounded. No `asyncio.Lock` appears in the snapshot; Unio's intended lock is architectural. |
| (f) | Protocols/plain functions over hierarchies | COMPLIANT | — | `base.py:17,40`; one-level module inheritance; no dynamic machinery. |
| (g) | Deletable modules; small interfaces | COMPLIANT | — | `modules/__init__.py` has no example coupling; example depends only on `base.py`; deletion-isolation check passed by import-graph inspection. |
| (h) | Flat control flow | COMPLIANT | — | Guard/early-return style in `merge.py:432–480`, `store.py:123–149`, `decide.py:80–143`; supervisor's six-level classification block is noted but not a violation. |
| (i) | Enums over bools/strings | PARTIAL | MEDIUM | Reviewed v2 `decompose: bool` (`decide.py:63`, `example-spec.md` §3.2) is retained; worker/supervisor status allowlists are strings (`worker.py:54,78,89–96`; `supervisor.py:57,64`); `doctor.Status` is the good example. |
| (j) | Battle-tested libraries | COMPLIANT | — | stdlib `sqlite3`, subprocess, logging, JSON; no pydantic/tenacity/new framework (`pyproject.toml`, `merge.py:187–193`). Shell gate is host-authored and documented. |
| (k) | No globals/hidden state/singletons | COMPLIANT | — | No cache decorators; state is instance/session-owned. Read-only module tables `_UNMERGED_PAIRS` and `EXIT_CODES` are low-severity mutability nits. |
| (l) | Delete over add | PARTIAL | LOW | `events.py:41` `LogEvent`, `orchestrator.py:1–59`, and `ipc.make_request_id` lack production consumers; seed dataclasses and store dicts are duplicate event models. |
| §7 | Module shape / JSON CLI | PARTIAL | MEDIUM | `modules/example/` has no `__main__`; template defers the standalone entry to v2.1 (`module-template/architecture.md:217,223`; `example-spec.md:330,402`). |
| §7 | Protocol dependencies / engine swap | COMPLIANT | — | Example imports only `base.py`; rule engine sits behind `Module.decide` (`example/decide.py:13,146–157`). |
| §8.3 | Let it crash | PARTIAL | LOW | `worker.py:172–174` catches all task exceptions and emits in-band `failed`, masking the supervisor restart path. |

**Counts:** COMPLIANT 11 · PARTIAL 5 · VIOLATION 0. The unverified design flags are listed in
§5, not counted as violations.

## 2. Evidence by deviation

### Concurrency and boundaries (c/e)

The store owns its connection and WAL on one daemon writer thread; readers use short-lived
connections (`store.py:151–158,185–187`). Worker `asyncio.to_thread(do_work, run, stop)` passes a
thread-safe event only. The two accepted snapshot deviations are (1) `queue.Queue()` is
unbounded because dropping a source-of-truth event loses state (`store.py:20–22,106`), and
(2) slice `EventLog.emit` writes JSONL on the event loop (`supervisor.py:69–87`), explicitly
marked a superseded artifact. The strict no-lock reading of (e) is not met because seq/dead/
closed scalars use `threading.Lock`; it is single, non-nesting, and performs no I/O.

### Enums (i)

The boolean `DecomposeOutput.decompose` is a reviewed v2 contract enforced by `metric.py:18–19`
and `dataset.py:49`; migrate to `Decision {DECOMPOSE, DO_NOT_DECOMPOSE}` only with a dataset
schema-version bump. Worker wire statuses must remain JSON strings, but a Python `ResultStatus`
boundary would satisfy the norm. Genuine booleans (`write_marker`, `canary`, `timed_out`,
`saw_ready`) are predicates/API compatibility and are acceptable.

### Delete-over-add and module shape (l/§7)

`events.py`/unused `orchestrator.py` form a disconnected `type/timestamp` model beside the
production `kind/ts` store and supervisor JSONL. Either wire the seed to `EventStore` or delete
both and update constitution citations; `make_request_id` is test/public-helper-only. The
example module has typed JSON-shaped inputs/outputs but no module CLI, a doc-vs-template mismatch.

### Crash policy (§8.3)

`do_work` catches broad `Exception` and returns `status="failed"`, while `run`/`_fatal`
(`worker.py:256–274`) handle protocol crashes. Narrowing the catch would make actual task
crashes exercise restart/recovery; the current deterministic slice is low severity, but the
pattern must not reach the DSPy worker.

## 3. Cross-cutting observations and priorities

1. `EventLog` and `EventStore` coexist without wiring; `doctor.py:153–165` checks the SQLite
   DB while the slice writes `events.jsonl`.
2. IPC/worker are now in main via `38e1d43`; the historical branch-only N-A is not current.
3. Constitution citations (`events.py:14,22,31,41`; `base.py:17,21–22,40`; `decide.py:63`;
   `metric.py:18–19`; `dataset.py:49`) were re-verified at the snapshot.

Priority order retained: reconcile module CLI with the template; introduce enum boundaries at
the worker; resolve the events/orchestrator dead code; document the store queue/lock tradeoff;
and narrow the worker catch. These are recommendations, not changes made by this audit.

## 4. Verification appendix

| Check | Result |
|---|---|
| Frozen event types | Verified at `events.py:14,22,31,41`. |
| `functools.cache`/`lru_cache` | 0 hits in `src/`/`tests/`. |
| `asyncio.Lock` | 0 hits in snapshot source. |
| `threading` users | `store.py`, `worker.py` only. |
| Example module CLI | Absent (`rg "if __name__"`; no `__main__.py`). |
| `LogEvent` / `Orchestrator` consumers | None outside defining modules. |
| AST nesting | store 3, merge 2, ipc 1, worker 3, supervisor 6. |
| Cold-start and §8 design norms | **UNVERIFIED** here; relied on `review-implementation.md` §M2, `dspy-python-314.md` §Recommendation, and architecture contracts. |

The original audit is immutable evidence. Current implementation and milestone status belong
in the architecture, source, and `v2-1-status` pointer above.

## 5. Detailed audit notes

### (a) measurement and (b) records

The byte-at-a-time `_read_line` implementation was checked as a framing choice, not an
optimization: a larger `StreamReader.read(n)` can consume bytes after the first newline, which
would violate the IPC boundary. It runs against asyncio's in-memory buffer, and the documented
`import dspy` floor makes its per-byte cost secondary. `_discard_to_newline` is O(n) only on the
oversized-line error path. Store fsync cadence, `busy_timeout`, and `synchronous=NORMAL` came from
`sqlite-wal-durability.md`, not speculative tuning.

All four event types (`events.py:14,22,31,41`) and module records are frozen/slots; merge
exceptions are transient classes and do not carry hot-path records. `store.py:127–135`,
`supervisor.py:81–87`, and `worker.py:89–96` move flat JSON dicts across boundaries. The note
matters because the constitution's “no object-graph serialization” rule is satisfied even where
exception objects use ordinary Python layout.

### (c)/(e) thread inventory

The store has one daemon writer thread and short-lived reader connections; `_queue`,
`_next_seq`, `_dead`, and `_closed` are the only shared scalars. The worker's `asyncio.to_thread`
handoff passes a `threading.Event`; the outcome dict remains thread-local. Supervisor and merge
are pure asyncio/synchronous subprocess paths, and doctor has no thread. The queue is unbounded
by design and EventLog performs loop-thread disk I/O, both explicitly documented slice
deviations. The lock protects seq reservation and lifecycle flags with no I/O or nesting; moving
reservation into the writer would remove it but change the no-gap invariant.

### (f)/(g)/(h) module boundaries

`Output` and `Metric` are Protocols; only `Module`→`ShouldDecomposeModule` and
`DatasetLoader`→`ExampleDatasetLoader` are one level deep. `modules/__init__.py` is docstring-only,
and example imports only `base.py`; its deletion affects its own tests. `publish_merge` and
`append` use early raises; the only six-level block is supervisor failure classification with a
linear if/elif chain. The architecture's “deletable sibling” guarantee is therefore an import
graph property, not a claim that every future module has already been built.

### (i) exact string/boolean boundary

The v2 boolean exception is enforced in `metric.py:18–19` and `dataset.py:49`, so changing it
without a dataset schema bump would invalidate frozen records. New worker statuses are JSON wire
strings (`EXIT_CODES = {succeeded, failed, cancelled}`); a Python enum can sit inside the module
while serialization remains explicit. `SliceResult.status` and `timeout_phase` are internal
strings with LOW impact. `doctor.Status(enum.StrEnum)` demonstrates the preferred pattern.

### (j)/(k) libraries and globals

Persistence uses stdlib SQLite WAL; process launch uses list-form subprocess calls except the
host-authored gate `sh -c`; IPC is a project-specific bounded NDJSON contract, not a general
framework replacement. No pydantic, tenacity, or new logging framework was introduced. Module
tables `_UNMERGED_PAIRS` and `EXIT_CODES` are immutable by convention but technically set/dict
literals; `logger = logging.getLogger` is standard logging practice. Runtime state is under
`<session_dir>/.cambium/`, while EventStore and MergeSequencer are ordinary instances, not
singletons.

### (l) cleanup candidates and §8.3

`LogEvent` has no consumer; `orchestrator.py` has no importer and is the only consumer of the
seed event types. `ipc.make_request_id` is used by a scenario but not production. The safe choice
is either wire the seed to the real store and add a scenario, or delete both and update the
constitution's (b) citations. Broad `do_work` exception handling is a separate boundary: task
crashes become in-band failures instead of `fatal_error`/nonzero exits, so restart policy is not
exercised. This is low for the deterministic slice but a dangerous precedent for DSPy.

### §7 CLI and unverified design norms

The module template defers `python -m cambium.modules.<name>.eval` to v2.1 while `agents.md`
states a CLI entry as a current norm. The example module is JSON-shaped but has no `__main__.py`;
the issue is documentation/code alignment, not a missing data schema. Task tree/DAG execution,
LLM-never-manages-parallelism, provider cache structure, canary gates, and no-sandbox containment
are architectural contracts with no implementing code at this snapshot. Cold-start claims rely
on `review-implementation.md` §M2 and `dspy-python-314.md` §Recommendation; no profile was run.

## 6. Proposed fixes recorded by the audit

These proposals were not applied in this historical document, but preserve the accepted/rejected
rationale for future implementation:

1. **Module CLI:** add a minimal JSON-stdin/JSON-stdout `__main__.py`, or amend the `agents.md`
   bullet to state that the entry point is a v2.1 target. The template's deferral and the
   constitution's current norm cannot both be silently true.
2. **Status enum:** introduce a Python `ResultStatus`/`Literal` at the worker boundary while
   serializing explicit wire strings; keep the reviewed `decompose: bool` until a dataset schema
   bump. This is the highest-value (i) fix because it preserves JSON compatibility.
3. **Event seed:** either wire `Orchestrator.run` to the real `EventStore` and add a scenario,
   or delete `events.py`/`orchestrator.py` and update the constitution's (b) evidence. Keeping
   both unconnected models is the opposite of (l).
4. **Store deviations:** document the unbounded queue and seq-reservation lock as an explicit
   tradeoff, then implement v2.1 backpressure and bounded critical waits. Do not silently drop
   source-of-truth events.
5. **Crash path:** narrow `do_work`'s broad catch or re-raise unexpected failures so real task
   crashes reach `fatal_error`/nonzero exit and supervisor recovery. Add a scenario that proves
   crash versus in-band failed result.

Cold-start profile, task-tree scheduling, provider cache layout, canary gate, no-sandbox
containment, sibling pinning for future modules, and the full §8 design contract were not
mechanically checkable here. Their absence from the implementation is a finding boundary, not
permission to label the architecture implemented.

The compliance audit did not treat a matching role name as proof of a role: `EventStore`,
`MergeSequencer`, `Custos`, and `Module` were traced to importers and tests before a verdict.
Likewise, the absent example CLI was established from entry-point search and package contents,
not inferred from its directory name. This is the orientation rule's causal check for the
constitution: source paths, callers, and tests distinguish a real implementation from a planned
architecture symbol.

The audit also distinguishes a source symbol from a proof of a role: matching names such as
`EventStore`, `MergeSequencer`, `Custos`, or `Module` do not establish that the canonical runtime
imports them. Conversely, the absence of a `__main__.py` was checked from entry points, not
inferred from the package name. This follows the repository orientation rule to trace imports,
callers, and tests before calling a feature present or absent.
