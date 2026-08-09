# Constitution Compliance Audit — Merged Implementation

**Author:** wt-audit-constitution worktree
**Date:** 2026-08-09
**Status:** Research. Read-only audit of the merged implementation code against the
translated coding constitution (`docs/research/coding-constitution.md`, principles
(a)–(l)) and the norms in `agents.md` §7 (coding norms) / §8 (design norms).
No code changed. The only output of this task is this document.

## 0. Scope and evidence base

| Audited surface | Revision | Notes |
|---|---|---|
| `docs/research/coding-constitution.md` | main @ `6109a6a` | The 12 translated principles (a)–(l) + Python norms |
| `agents.md` §7 (coding norms + "Coding principles (translated constitution)" subsection), §8 (design norms) | main @ `6109a6a` | The constitution patch (§6 of the constitution doc) is applied to agents.md |
| `src/cambium/store.py`, `merge.py`, `supervisor.py`, `events.py`, `orchestrator.py`, `doctor.py`, `__init__.py` | main @ `6109a6a` | Merged implementation |
| `src/cambium/modules/base.py`, `modules/example/{__init__,decide,dataset,metric}.py`, `tasktree.py` | main @ `6109a6a` | Reference module and deterministic task tree |
| `src/cambium/ipc.py`, `src/cambium/worker.py` | main via `38e1d43` | Nuntius framing + Opifex seed; re-auditable in the merged tree |
| `tests/scenarios/*` | main @ `6109a6a` | 108 scenario tests drive the real components |

Verification note: line citations below are repo-relative. Every citation was read
directly in this session. Checks that require measurement (cold-start profile) or a
running system (§8 design norms) are marked **UNVERIFIED** where I could not run them.

**Current-main status (2026-08-09):** this audit was performed at `main@3d27ba3`.
Current main is `6109a6a`, with 108 tests collected and passed. `ipc.py` and
`worker.py` are merged by `38e1d43`; `tasktree.py` is merged by `06ce0dc`;
`diffundo.py` is not present in current main. The original findings and line numbers
are retained as point-in-time evidence. The `_merge_lock`/canonical-supervisor
finding remains pending on `wt-impl-super`.

**Stale-pattern note:** the remaining broad-grep match in this document is the
current source symbol `_UNMERGED_PAIRS`, not a claim about branch or file state.

**Verdict key:** COMPLIANT / PARTIAL / VIOLATION + severity (INFO / LOW / MEDIUM /
HIGH). Severity is *norm divergence impact on the merged code*, not hypothetical.

## 1. Summary table

| # | Norm | Verdict | Severity | Primary evidence | Headline finding |
|---|---|---|---|---|---|
| (a) | Measure before optimizing | COMPLIANT | — | `ipc.py:69-88`; constitution §2 (a) | No premature micro-opt; byte-accurate read is correctness-motivated |
| (b) | Flat records (frozen+slots) | COMPLIANT | — | `events.py:14,22,31,41`; `decide.py:51-65`; `doctor.py:47`; `supervisor.py:53` | All new records flat; no deep object graphs |
| (c) | No shared mutable state across threads; single writer | COMPLIANT | — | `store.py:112-115,172`; `worker.py:211` | Single writer thread; only thread-safe primitives cross boundaries; 2 documented low deviations |
| (d) | Functional core / imperative shell | COMPLIANT | — | `decide.py:68-143,155-157` | `should_decompose` is pure; `decide()` is the seam |
| (e) | Loop-affine state; queues over locks; tiny bounds | PARTIAL | LOW | `store.py:107,106` | `threading.Lock` + unbounded queue are the two documented deviations from the strict norm |
| (f) | Protocols / plain functions over deep hierarchies | COMPLIANT | — | `base.py:17,40` | `Output`/`Metric` are Protocols; no deep hierarchy |
| (g) | Deletable modules; small interfaces | COMPLIANT | — | `modules/__init__.py:1-7`; `example/metric.py:5` | `rm -rf modules/example/` breaks only its own tests |
| (h) | Flat control flow | COMPLIANT | — | `merge.py:432-480`; `store.py:123-149` | Guards + early raises throughout; one 6-level block noted in supervisor |
| (i) | Enums over booleans/strings | PARTIAL | MEDIUM | `decide.py:63` (KNOWN); `worker.py:54,78`; `supervisor.py:57,64` | KNOWN v2.1 `decompose: bool`; new str-allowlists for status |
| (j) | Battle-tested libs over custom infra | COMPLIANT | — | `pyproject.toml`; `store.py:35`; `merge.py:187-193` | stdlib + git only; no reinvented wheels; tenacity correctly *not* added |
| (k) | No globals / hidden state / singletons | COMPLIANT | — | grep (no `functools.cache`/`lru_cache`); `merge.py:44-53` | No module-level mutables that are mutated; two constant-lookup nits |
| (l) | Delete over add | PARTIAL | LOW | `events.py:41`; `orchestrator.py:1-59`; `ipc.py:43-45` | `LogEvent` and `orchestrator.py` have no consumer; two event representations coexist |
| §7 | Module shape: JSON-in/JSON-out + CLI entry | PARTIAL | MEDIUM | `modules/example/` (no `__main__`); `docs/architecture/module-template/architecture.md:217,223` | Only existing module lacks the §7 CLI entry; module-template defers it to v2.1 |
| §7 | Protocol deps / engine-swap strategy | COMPLIANT | — | `example/decide.py:146-157` | Module depends only on `base.py` ports; DSPy seam present |
| §8.3 | Let it crash | PARTIAL | LOW | `worker.py:172-174` | Catch-all masks task crashes into in-band "failed" outcomes |

**Counts:** COMPLIANT 11 · PARTIAL 5 · VIOLATION 0 · UNVERIFIED-flagged items listed
in §6.

---

## 2. Per-norm findings

### (a) Measure before optimizing — COMPLIANT

No premature micro-optimization exists in the new code.

- `src/cambium/ipc.py:69-88` (`_read_line`, byte-at-a-time reads) is the one place a
  reader might suspect tuning. It is **correctness-motivated**, not an optimization:
  a single `reader.read(n)` can consume bytes past the first newline, and the framing
  contract (ipc-protocol-draft §1.4) requires a message boundary at the newline with
  no over-consumption. Its per-byte cost is against `asyncio.StreamReader`'s internal
  buffer (no syscall per byte) and processes small heartbeat/result messages inside an
  already cold-started worker. Relative to the documented cold-start floor
  (`import dspy` ≈ 2.1 s — `docs/reviews/review-implementation.md` §M2; the
  `wt-coldstart` measurement is cited in constitution §2 (a)), it is noise.
- `store.py` fsync cadence (`store.py:205-206`), `busy_timeout` (69), and
  `synchronous=NORMAL` are measured design decisions carried from
  `docs/research/sqlite-wal-durability.md`, not speculation.
- `_discard_to_newline` (`ipc.py:57-66`) is O(n) byte-at-a-time on an oversized
  (>1 MiB) line. Bounded and only on the error path. INFO only.

### (b) Flat records — COMPLIANT

The constitution's (b) precedent is the code:

- `events.py:14,22,31,41` — all four event types are `@dataclass(frozen=True, slots=True, kw_only=True)`.
- `modules/base.py:21-22` — `Example` is `frozen=True, slots=True`.
- `modules/example/decide.py:51-58` (`TaskInput`), `:59-65` (`DecomposeOutput`) — frozen+slots.
- `doctor.py:47-54` (`Check`), `supervisor.py:53-64` (`SliceResult`) — frozen+slots.
- All event/payload data flows as flat JSON dicts: `store.py:127-135` (row assembly),
  `supervisor.py:81-87` (`EventLog` records), `worker.py:89-96` (outcome dict). No deep
  object graphs anywhere; no object-graph serialization.

One note: `merge.py:56-133` exception types are plain classes without `__slots__`
(carrying `stdout`/`stderr` strings). Exceptions are transient, not hot-path records;
acceptable under the norm.

### (c) No shared mutable state across threads; single-writer discipline — COMPLIANT

Structural audit of every thread in the codebase:

| Module | Threads | Cross-thread state | Discipline |
|---|---|---|---|
| `store.py` | 1 daemon writer thread (`:112-115`, `_writer_loop` `:172`) | `_queue` (bounded—see note), `_next_seq`/`_dead`/`_closed` under `threading.Lock` (`:107`), `threading.Event` handoffs (`:89,111`) | The writer owns the write connection and the DB/WAL fds (`:185-187`); readers use short-lived connections (`:151-158`); only immutable event rows cross the queue |
| `worker.py` | `asyncio.to_thread` per task (`:211`) | `stop: threading.Event` (set by the loop on steer/cancel `:357,363`, polled by the thread `:134,152`) | `outcome` dict is thread-local, returned to the loop; the stdout writer is touched only by the loop |
| `supervisor.py` | none (pure asyncio) | — | — |
| `merge.py` | none (synchronous subprocess) | — | — |
| `doctor.py` | none | — | — |

`asyncio.to_thread(do_work, run, stop)` (`worker.py:211`) is the one loop→thread
handoff and it is exactly the norm shape: only a thread-safe primitive (`threading.Event`)
crosses, no mutable data. COMPLIANT.

**Documented deviation (LOW):** `store.py:20-22,106` — the enqueue queue is **unbounded**,
versus the constitution's "bounded queues with drop-on-full backpressure" (constitution
§2 (c)). Intentional and stated in the module docstring: events are the source of truth
and dropping one loses state; bounded-with-backpressure is a v2.1 option. Accepted
deviation, not an accident.

**Documented deviation (LOW):** `supervisor.py:81-87` — `EventLog.emit` writes JSON-Lines
to disk **on the event loop**, violating agents.md §7 "Every disk write off the event
loop" (`agents.md:128`). The slice docstring flags this explicitly
(`supervisor.py:69-74`) as superseded by the real store; `store.py` is the compliant
implementation. Accepted slice artifact; re-check when the supervisor is rewired onto
`EventStore`.

### (d) Functional core / imperative shell — COMPLIANT

- `modules/example/decide.py:68-143` — `should_decompose(task, context)` is a pure
  function (guards + evidence accumulation, no I/O).
- `ShouldDecomposeModule.decide` (`:155-157`) delegates to it; `metric` (`:159-161`)
  delegates to the pure metric.
- State and I/O live at the edges: `supervisor.py` (`run_session`), `store.py`
  (writer thread), `worker.py` (`do_work` at the process edge).

### (e) Concurrency rules / loop-affine state — PARTIAL (LOW)

- Loop-affine state is respected: `run_session` accumulates outcome state in coroutine
  locals (`supervisor.py:260-371`); the worker's `run` loop mutates `current`/`stop`
  with no `await` between check and set in the dispatch section (`worker.py:301-365`).
- No `asyncio.Lock` anywhere (grep: zero hits). The constitution's "the only
  `asyncio.Lock` is Unio's" holds vacuously — Unio's merge lock is in the design;
  `merge.py` is synchronous subprocess plumbing and needs no lock.
- **Deviation (LOW):** `store.py:107` — a `threading.Lock` guards cross-thread scalars
  (`_next_seq`, `_dead`, `_closed`). The strict reading of the norm ("no thread locks on
  shared state at all; only bounded queues + `call_soon_threadsafe` handoffs",
  constitution §2 (e)) is not met. It is minimal and deadlock-immunity is preserved
  (single lock, no nesting, no I/O under the lock). Moving seq reservation to the writer
  thread would eliminate the lock but breaks the documented "seq reserved at enqueue,
  no gaps by construction" invariant (`store.py:13-15`). Verdict: accepted trade-off;
  recommend the store docstring state this explicitly as a (e) deviation.
- **Deviation (LOW):** the unbounded queue (see (c)) also touches the "tiny bounds"
  clause of (e).

### (f) Protocols / plain functions over deep class hierarchies — COMPLIANT

- `base.py:17` (`Output` Protocol), `:40` (`Metric` Protocol).
- The only inheritance is one level deep: `Module` (ABC) → `ShouldDecomposeModule`;
  `DatasetLoader` (ABC) → `ExampleDatasetLoader`. `worker.py:368` `_WriterProtocol`
  extends stdlib `FlowControlMixin` for pipe-close semantics — required plumbing, not a
  hierarchy.
- No dynamic machinery where a plain function suffices.

### (g) Deletable modules; small interfaces — COMPLIANT

- `modules/__init__.py:1-7` is docstring-only (no import coupling to `example`).
- The only dependency edge in the module tree is example → `base.py`
  (`example/metric.py:5`, `example/decide.py:13`, `example/dataset.py:5`); nothing
  imports example. `rm -rf src/cambium/modules/example/` breaks only
  `tests/scenarios/test_example_module.py` — the §(g) deletion test passes.
- The module composes engine + metric + loader (`modules/example/__init__.py:24-26`).

### (h) Flat control flow — COMPLIANT

- Guard clauses and early raises are the dominant style: `merge.py:432-480`
  (`publish_merge` is a sequence of early `NonFastForwardError` guards), `store.py:123-149`
  (`append` early value-check then proceed), `decide.py:80-143` (short-circuit guards),
  `worker.py:105-153` (`do_work` guard-returns).
- Maximum compound-statement nesting (AST-measured): `store.py` = 3, `merge.py` = 2,
  `ipc.py` = 1, `worker.py` = 3. The codebase maximum is `supervisor.py:332-363` (6
  levels) — the failure-classification block in `run_session`. It is a linear guard
  chain (`if/elif/elif/else`) with two nested `if worktree.exists()`/`if not timed_out`
  sub-blocks. Not a violation; extractable to a pure `_classify(...) -> SliceResult`
  function if it grows further.

### (i) Enums over booleans/strings — PARTIAL (MEDIUM)

**The KNOWN exception, flagged per the task:** `modules/example/decide.py:63`
`decompose: bool` in `DecomposeOutput`. Per constitution §2 (i) note this is the
**reviewed v2 contract** (`docs/architecture/module-template/example-spec.md` §3.2),
authoritative and enforced by the metric (`example/metric.py:18-19`) and loader
(`example/dataset.py:49`). **Do not change now.** v2.1 migration: replace with a
`Decision { DECOMPOSE, DO_NOT_DECOMPOSE }` enum across `DecomposeOutput`, dataset
`expected.decompose`, and the metric, gated behind a dataset schema-version bump (the
`TaskKind` precedent, `example-spec.md` §3.1).

**New-code string allowlists (the PARTIAL):**

| Location | Field | Norm gap |
|---|---|---|
| `worker.py:54` | `EXIT_CODES = {"succeeded": 0, "failed": 1, "cancelled": 4}` — status as str keys | `ResultStatus` is named in constitution §2 (i) / `agents.md:123` as an enum; here status is a string allowlist with a `.get` fallback (`worker.py:235,245`) |
| `worker.py:78,89-96` | `outcome["status"]` — str | same |
| `supervisor.py:57` | `SliceResult.status: str  # "succeeded" \| "failed"` | internal result, LOW |
| `supervisor.py:64` | `timeout_phase: str  # "ready" \| "run" \| "gate" \| "wall"` | LOW |
| `events.py:36` | `WorkerFinished.status: str = "finished"` | seed contract, LOW |

Context: the constitution notes v2 uses `Literal` for status fields (§2 (i)), and the
supervisor is explicitly scoped as "the slice, not Custos" (`supervisor.py:20-25`). The
worker status strings also cross the wire (`result_envelope.status`, `worker.py:234`),
where JSON forces strings — the enum would live at the Python boundary. This is the
single most consistent norm gap in the new code; severity MEDIUM only for the worker
boundary, LOW elsewhere.

**Genuine booleans (OK):** `write_marker` (predicate + API compat with
`scripts/fake_worker.py`), `canary` (`dataset.py:34,57`), `timed_out`, `saw_ready` —
all predicates.

**Good example:** `doctor.py:38-44` `Status(enum.StrEnum)` is exactly the norm.

### (j) Battle-tested libraries over custom infra — COMPLIANT

- `pyproject.toml` `dependencies = []`; no new frameworks anywhere.
- Persistence = stdlib `sqlite3` WAL (`store.py:35,177-181`); subprocesses = list-form
  `subprocess.run`/`create_subprocess_exec` (no `shell=True`; `merge.py:187-193`,
  `supervisor.py:145-148`, `worker.py:69`); logging = stdlib (`worker.py:56`,
  `ipc.py:26`); JSON = stdlib `json` (`store.py:128`, `worker.py:113`, `ipc.py:113`).
- No custom retry logic: `busy_timeout` is sqlite's own mechanism. **tenacity is
  correctly not added** — stdlib-only is the project rule (`agents.md:119`) and the
  constitution (j) names stdlib + git + uv + pytest.
- `ipc.py` NDJSON framing is the project's own wire spec
  (`docs/research/ipc-protocol-draft.md` §1), not a reinvention of an existing
  battle-tested library (there is no stdlib asyncio-NDJSON framer with the 1 MiB cap /
  resync / non-object-skip semantics the spec demands), and adding one would violate
  the stdlib-only rule. It is the single "custom infra" surface and it is justified.
- **Awareness note (not a violation):** `supervisor.py:146` runs the gate via
  `sh -c <task_spec["gate"]>`. The `gate` field is a documented supervisor-side shell
  command (`supervisor.py:191-192`), same trust model as a CI command — not
  worker-controlled input. Flagged because it is technically `shell` with a string;
  the norm's `shell=True` prohibition targets untrusted user input in `git_op`/
  `grep_code`-style helpers, which this is not.

### (k) No globals / hidden state / singletons — COMPLIANT

- No `functools.cache`/`@cache`/`lru_cache` anywhere in `src/` or `tests/` (grep:
  zero hits) — the constitution §2 (k) prospective guard holds.
- Module-level objects are all immutable or constant: `store.py:42-45`
  (`CRITICAL_KINDS` frozenset), `merge.py:44-53` (str constants + `_UNMERGED_PAIRS` set),
  `decide.py:17-35` (frozenset/tuple), `supervisor.py:42-43` (int constants).
- No singletons. `EventStore` (`store.py:93`) and `MergeSequencer`
  (`merge.py:158`, docstring "Holds no global or cross-session state") are ordinary
  classes; instance state is confined to the object.
- Runtime state lives under `<session_dir>/.cambium/` (`supervisor.py:195`),
  per agents.md §7.
- **INFO nits (LOW, non-mutating):** `merge.py:52` `_UNMERGED_PAIRS` is a module-level
  *set* literal and `worker.py:54` `EXIT_CODES` is a module-level *dict* literal. Both
  are never mutated (read-only lookup tables). Strictly "module-level mutables"; convert
  to `frozenset` / `types.MappingProxyType` or accept. Also `logger =
  logging.getLogger(__name__)` (`ipc.py:26`, `worker.py:56`) is standard logging
  practice, not state.

### (l) Delete over add — PARTIAL (LOW)

- **`events.py:41` `LogEvent`** is imported by nothing (grep: zero consumers outside
  the module). Genuinely dead per the (l) test.
- **`orchestrator.py:1-59`** — the whole module has zero importers in `src/` or
  `tests/` and no tests. It is the *only* consumer of `events.py` (`orchestrator.py:15`),
  so `events.py` is one deletion away from orphaned.
- **Two event representations coexist with no bridge:** the frozen-dataclass seed
  (`events.py`, `type`-keyed, consumed by the unused orchestrator) versus the production
  dict pipeline (`store.py` `kind`-keyed rows, `supervisor.py` `EventLog` records). The
  constitution itself cites `events.py` as the (b) precedent (§2 (b), §5 row 1) — so
  deletion must be coordinated with that citation. Honest options: (1) wire
  `Orchestrator.run` onto `EventStore`/`run_session` and add a scenario test, keeping the
  seed live; or (2) delete `events.py` + `orchestrator.py` and update the constitution's
  (b) citation to the dict pipeline. Middle ground (keep seed, mark intentional) is
  acceptable but currently unstated.
- `ipc.py:43-45` `make_request_id` has no production caller (the worker uses the
  supervisor-provided `request_id`; only `tests/scenarios/test_ipc.py:240-244` uses it).
  Keep as a public framing helper or drop.
- No unused functions found in `store.py`, `merge.py`, `worker.py`, `supervisor.py`,
  `doctor.py` (each public/private helper has a caller or test).

### §7 Module shape (agents.md:129) — PARTIAL (MEDIUM)

agents.md §7: "modules are pure JSON-in/JSON-out functions with strict JSON schemas,
each with a CLI entry — `python -m cambium.modules.<name>` reads JSON from stdin,
writes JSON to stdout."

- The only existing module (`modules/example/`) has **no CLI entry**: no `__main__.py`,
  no `if __name__ == "__main__"` (grep: the only `src` mains are
  `supervisor.py:450`, `doctor.py:266`, and `worker.py:423` in main).
- **Mitigating fact:** the module template defers the standalone entry point to v2.1 —
  `docs/architecture/module-template/architecture.md:217,223` (`python -m
  cambium.modules.<name>.eval` is a v2.1 target) and `example-spec.md:330,402` ("In v2
  the scenario test subsumes the role of the eval harness"). So agents.md §7 states a
  norm the template explicitly schedules for v2.1 — a doc-vs-doc inconsistency that the
  code exposes. Recommend either adding a minimal JSON-stdin/JSON-stdout `__main__` to
  the example module (the module is JSON-shaped already: `decide.py` takes/returns flat
  dataclasses) or amending the §7 bullet to note the v2.1 deferral.
- `tasktree.py` exists in main via `06ce0dc` and is a deterministic library
  module; its scenario coverage is included in the current 108-test suite.
  `diffundo.py` is not present in current main, so its provider/CLI contract is still
  spec-future and not checkable here.
- `store.py`, `merge.py`, `ipc.py` are library modules, not decision modules — the §7
  CLI norm does not apply to them. `supervisor.py`, `doctor.py`, `worker.py` have CLI
  mains.

### §7 Protocol deps / engine-swap — COMPLIANT

- `example/` depends only on `base.py` (`example/decide.py:13`, `dataset.py:5`,
  `metric.py:5`) — ports, not concrete providers. `Output`/`Metric` are Protocols
  (`base.py:17,40`).
- Engine swap is a strategy pattern: `decide.py` is the rule engine behind the `Module`
  seam with an explicit DSPy-replacement docstring (`decide.py:1-6,146-151`); matches
  `agents.md:130` and constitution (d).

### §8.3 Let it crash (agents.md:189) — PARTIAL (LOW)

- `worker.py:172-174`: `do_work` wraps the whole task body in `except Exception` and
  converts any crash into an in-band `status="failed"` / `failure_reason` outcome.
  §8.3 says workers should *not* catch-and-wrap; crash and let the supervisor restart
  from the last durable checkpoint. The current seed worker is the deterministic slice
  (not the DSPy worker), and `run`/`_fatal` (`worker.py:256-274`) do implement the
  crash-and-report path for protocol errors — but the broad `do_work` catch masks task
  crashes so the supervisor's restart path is never exercised. Watch that this pattern
  does not carry into the DSPy worker. Severity LOW for the slice, MEDIUM as a precedent.
- Other §8 norms (task tree/DAG, determinism split, provider-side caching, canary gate,
  no sandboxing in harness) are architectural; the orchestration/meta layers are not
  built. Not mechanically checkable — see §6.

---

## 3. Cross-cutting observations

1. **Two event-log implementations coexist.** `EventLog` (JSON-Lines on the event loop,
   `supervisor.py:67-87`) and `EventStore` (SQLite WAL on a writer thread, `store.py:93`).
   The former is a documented slice artifact; the latter is the norm-compliant target.
   `doctor.py:153-165` checks the *store's* DB (`events.db`), while the slice writes
   `events.jsonl` — no wiring yet. Expected at this milestone; keep on the integration
   plan.
2. **Historical audit surface correction.** `ipc.py`/`worker.py` are now in main
   via `38e1d43` (the original branch was `wt-impl-ipc` @ `ad372ae`). The
   (c)/(e)/(i)/(l)/§8.3 findings that cite them are re-auditable against the
   merged line shifts; the canonical-supervisor lock finding remains pending
   on `wt-impl-super`.
3. **The constitution's own citations were re-verified and hold** (`events.py:14,22,31,41`,
   `base.py:17,21-22,40`, `decide.py:63`, `metric.py:18-19`, `dataset.py:49`) — no stale
   citation found in constitution §5.

---

## 4. Top-5 fix priority list

1. **MEDIUM — Reconcile agents.md §7 "Module shape" with the code and the template
   (`agents.md:129`; `modules/example/`, no `__main__`).** Either add a JSON-stdin →
   JSON-stdout `__main__` to the example module (or a `cli()` function in `decide.py`)
   or amend the §7 bullet to state the CLI entry is a v2.1 target per
   `module-template/architecture.md:217,223`. Currently the norm is unfulfilled and the
   template contradicts it.
2. **MEDIUM — Enums/Literal for status across the worker boundary
   (`worker.py:54,78,234-245`).** Introduce a `ResultStatus` enum (or `Literal[
   "succeeded","failed","cancelled"]`) at the worker's Python boundary, converting
   `EXIT_CODES` and the outcome status to it; keep wire strings. This is the highest-value
   (i) fix because it also de-risks the `decompose: bool` v2.1 migration pattern.
3. **LOW — Resolve the `events.py`/`orchestrator.py` dead-code drift (`events.py:41`,
   `orchestrator.py`).** Pick one: wire `Orchestrator` onto the real store/supervisor and
   add a scenario test, or delete the seed + update constitution §2 (b)/§5 citations to
   the dict pipeline. Also delete or justify `LogEvent` and `ipc.make_request_id`.
4. **LOW — Document the two (c)/(e) store deviations as norm deviations
   (`store.py:20-22,106-107`).** The unbounded queue and the `threading.Lock` are
   intentional; state the (e) trade-off (lock vs writer-owned seq reservation) in the
   store docstring so a future reviewer does not rediscover it. Bounded queue is the v2.1
   plan — keep it tracked.
5. **LOW — Keep the worker's crash path honest (`worker.py:172-174`).** Narrow
   `do_work`'s catch to expected task failures (or re-raise unexpected exceptions) so
   real crashes reach the `fatal_error`/nonzero-exit path and exercise the supervisor's
   §8.3 restart contract; add a scenario asserting a task crash yields a worker crash,
   not an in-band "failed" envelope.

---

## 5. Verification appendix

| # | Claim | Method | Result |
|---|---|---|---|
| 1 | All four `events.py` types frozen+slots+kw_only | read `events.py:14,22,31,41` | verified |
| 2 | No `functools.cache`/`lru_cache` in src/tests | `rg` whole repo | verified (0 hits) |
| 3 | No `asyncio.Lock` anywhere | `rg` src (main + wt-impl-ipc) | verified (0 hits) |
| 4 | `threading` users = store.py, worker.py only | `rg` src | verified |
| 5 | No module CLI entry for `modules/example` | `rg "if __name__"` src; `ls modules/example/` | verified (absent) |
| 6 | `LogEvent` has no consumer | `rg "LogEvent"` src+tests | verified (0 hits outside module) |
| 7 | `Orchestrator` has no consumer | `rg "Orchestrator|orchestrator"` src+tests | verified (definition only) |
| 8 | Max nesting per file | AST walk | store=3, merge=2, ipc=1, worker=3, supervisor=6 |
| 9 | Example module deletion-isolation | import graph read | verified (example ← nothing) |
| 10 | ipc/worker provenance | `git log` on main | `38e1d43`, merged; original implementation `ad372ae` |
| 11 | Cold-start floor | cited `review-implementation.md` §M2, `dspy-python-314.md` §Recommendation | **UNVERIFIED-by-measurement** (relied on documented evidence; no profile run) |
| 12 | §8 design norms (task tree, determinism split, canary gate, no-sandbox) | code inspection | **UNVERIFIED** (orchestration/meta layers not built) |

---

## 6. UNVERIFIED flags

- **(a)** The "byte-accurate read is noise" judgment rests on the documented cold-start
  evidence (review-implementation §M2 / dspy-python-314 §Recommendation), not on a
  profile run in this session. If a worker loop is ever measured hot, re-profile
  `ipc._read_line`/`_discard_to_newline`.
- **§8 design norms** (DAG task tree, LLM-never-manages-parallelism, provider-cache
  prompt structure, canary gate, no-sandbox containment) are architectural contracts with
  no implementing code yet (orchestrator is a skeleton, workers are the deterministic
  slice). Not mechanically checkable; the audit flags only the one code-visible §8 item
  (let-it-crash, §2 above).
- **(g) "deletable without breaking siblings"** is verified for the only existing module;
  sibling-pinning for future tasktree/diffundo is a design contract, not code.
