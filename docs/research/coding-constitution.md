# Cambium Coding Constitution — Rust/HFT → Python 3.14 Translation

**Author:** wt-constitution worktree
**Date:** 2026-08-09
**Status:** Historical research; its proposed `agents.md` wording is not a live specification.
**Source:** the orchestrator's Rust/HFT constitution (12 principles, (a)–(l)).
**Scope:** retain the intent that survives Python 3.14, reject Rust-only mechanics, and cite the
architecture/research/source evidence that was available at the snapshot.

**Historical snapshot / current pointer (2026-08-09):** `worker-coldstart.md` and the cited
research are now in the tree; the branch-local provenance and commit `108c83d` below remain
historical. For current behavior, use `docs/architecture/architecture.md`, `src/cambium/`, and
`docs/research/v2-1-status.md`. Current notes: provider loop, Diffundo, EventStore, and root
`Result` exist; DLQ, eval cache, ResourceBudget, `worker_pool`, and `events` are absent; there is
no per-worker sandbox or production shell approval, and no dynamic hierarchy.

## 1. Translation boundary

| Rust/HFT concern | Verdict | Python form retained |
|---|---|---|
| Zero-cost abstractions; ownership/borrowing; manual layout; SIMD; `unsafe`; monomorphization | **Rejected as literals** — Python has no equivalent compile-time/runtime contract | Measure first; isolate mutable state with processes/queues; use flat records; keep typed interfaces |
| Vtables/static dispatch | **Rejected as a literal rule** | Small `Protocol`s and plain functions over deep hierarchies |
| `perf`/`valgrind` discipline | **Translated** | cProfile/time-based measurement; cold start is the first measured floor |

## 2. Principle record

The rows retain the original verdict and the evidence pointer. “New” means the proposed
`agents.md` patch added a norm; it is not evidence that the behavior already exists.

| ID | Principle translated for Cambium | Snapshot verdict and evidence |
|---|---|---|
| (a) | **Mechanical sympathy:** measure before optimizing; worker cold start (interpreter + `import dspy`, about 2.1 s) dominates; only then tune allocations/hot loops. | **Partial / new standing norm.** `ready_timeout` and off-loop event logging are architectural (`docs/architecture.md` §7.2, §6.2). Benchmark provenance: `docs/research/worker-coldstart.md` @ `108c83d`; supporting `docs/reviews/review-implementation.md` §M2 and `docs/research/dspy-python-314.md` §Recommendation. |
| (b) | **Data-oriented design:** frozen `slots=True` dataclasses, lists, and flat JSON event payloads; no pointer graphs. | **Yes.** `src/cambium/events.py`, `modules/base.py:Example`, `modules/example/decide.py:TaskInput/DecomposeOutput`; `docs/research/event-schema-draft.md` §2. |
| (c) | **CSP/actor:** never share mutable state across threads; single writer, bounded/drop-on-full queues, workers as processes over stdio. | **Yes structurally.** `docs/architecture.md` §6.2, `docs/research/custos-asyncio-design.md` §2.4/§3.3, `docs/research/logging-design.md` §2.9, and architecture §5.1. SQLite single-writer evidence: `sqlite-wal-durability.md` §6. |
| (d) | **Functional core / imperative shell:** pure module rules; state and I/O at supervisor, worker, and store edges. | **Yes.** `docs/module-template/architecture.md` §5.1; `src/cambium/modules/base.py` `Module.decide`; `modules/example/decide.py` `should_decompose`. |
| (e) | **Concurrency rules:** loop-affine state, queues over locks, bounded waits, no shared mutex state. | **Yes in the target design.** `custos-asyncio-design.md` §3.1–§4 and `docs/architecture.md` §6.2/§7.8; Unio's merge lock is the sole intended `asyncio.Lock`. |
| (f) | **Memory layout:** prefer `Protocol`s/plain functions; allocation-conscious only after (a). | **Partial / new standing norm.** `modules/base.py` `Output`/`Metric` and `decide.py` provide the precedent; Python dynamic dispatch is accepted. |
| (g) | **Architecture:** small JSON-schema-shaped interfaces; composition over inheritance; modules removable without sibling coupling. | **Yes.** `docs/module-template/architecture.md` §§3–5, §9.5; `docs/architecture.md` §17.2; `modules/example/__init__.py`. |
| (h) | **Control flow:** early returns, guards, exhaustive enum matching. | **Yes.** `agents.md` §7 and `modules/example/decide.py` guard clauses. |
| (i) | **Types:** enums for domain alternatives; booleans only predicates/API compatibility. | **Yes as a norm.** `agents.md` §7 and architecture §3.4. Reviewed exception: `DecomposeOutput(decompose: bool)` in `decide.py:63`, authoritative in `docs/module-template/example-spec.md` §3.2; a schema-versioned `Decision` migration is v2.1 work. |
| (j) | **Libraries:** stdlib + git + uv + pytest; DSPy optional; do not hand-roll logging, IPC framing, or persistence. | **Yes.** `agents.md` §7, `docs/architecture.md` §1 non-goal 5, `pyproject.toml`, `sqlite3` WAL, stdlib logging/subprocess, and `dspy-python-314.md` §Recommendation. |
| (k) | **No globals/hidden state/singletons:** config through frozen records; state under `.cambium`; only explicitly owned caches. | **Yes.** `agents.md` §7, `docs/architecture.md` §§8.1, 16.2, 19 item 6. No `functools.cache`/`lru_cache` was found in `src/` or `tests/`; Diffundo ownership is prospective. |
| (l) | **Delete over add:** remove, compose, or reuse before adding infrastructure. | **No / new norm.** No equivalent rule was found in `agents.md` §7 or `docs/architecture.md` §19; the proposed patch adds it. |

## 3. Translation summary and verification

- All 12 principles are covered. **Already implemented:** (b), (c), (d), (e), (g), (h), (i),
  (j), (k). **Partial:** (a), (f). **New:** (l). Rust-only mechanics are rejected or translated
  as listed in §1.
- The brief's cold-start premise was corrected: the benchmark is now in current main, but the
  original branch provenance is historical. Delete-over-add was not previously an `agents.md`
  norm.
- Spot checks retained from the original record: frozen event dataclasses (`events.py:14,22,31,41`),
  `Output`/`Metric` Protocols and `Example` (`base.py:17,21–22,40`), `DecomposeOutput` and its
  loader/metric (`decide.py:63`, `metric.py:18–19`, `dataset.py:49`), bounded logging queue
  (`logging-design.md` §§2.2, 2.9), and module-template ports (`architecture.md` §§3–5).

The original §6 `agents.md` patch text is intentionally not repeated here: `agents.md` and the
canonical architecture are the live sources; this file remains the dated decision record.

## 4. Evidence notes retained from the snapshot

These notes preserve the decision-making detail that led to the compact table above.

### (a) Measurement floor

The cold-start claim was not a generic optimization slogan. The cited `worker-coldstart.md`
measurement found interpreter startup plus `import dspy` (about 57 transitive packages) at a
median of 2166.8 ms and p90 2188.6 ms. The architecture's `ready_timeout` default is 60 seconds
and its event log is off the supervisor loop. `review-implementation.md` §M2 independently
describes a 1–3 second import floor; `dspy-python-314.md` recommends an optional extra and lazy
imports. Therefore allocation micro-optimizations were explicitly lower priority than measuring
the cold floor. The benchmark was branch-local when drafted and merged at commit `108c83d`;
that provenance is retained even though the file is now present in main.

### (b), (c), and (e) isolation details

The event schema keeps each event one flat record; the draft catalog has 21 kinds and the
`payload` is a flat dict. The intended CSP boundary is not a metaphor: one event-log writer
thread owns the SQLite connection, the queue is bounded in the target architecture, and only
immutable/redacted event dataclasses cross a thread boundary. `DropQueueHandler`'s
drop-on-full behavior is documented in `logging-design.md` §2.2 footgun 1 and §2.9. The
single-writer experiment in `sqlite-wal-durability.md` §6 creates the writer connection inside
the writer thread and uses per-thread connections (`threadsafety=3`). Loop-affine `WorkerHandle`
transitions have no `await` between check and set; shutdown awaits are timeout-bounded in
`custos-asyncio-design.md` §4. Unio's `asyncio.Lock` covers verify and publish only.

### (d), (g), and (h) boundaries

The functional seam is deliberately narrow: `Module.decide()` and `metric()` are the only
surfaces a replacement engine must implement. `should_decompose(task, context)` is pure, and
the reference module is stateless across calls. The module template's typed interfaces and
state table make a module independently deletable; pinned siblings in architecture §17.2 mean
`rm -rf src/cambium/modules/<name>/` should not alter another module. The reference uses guard
clauses for pre-decomposition context and evidence thresholds; future enum dispatch should be
exhaustive rather than a string/boolean cascade.

### (i), (j), (k), and (l) boundary cases

`DecomposeOutput(decompose: bool)` is intentionally not changed in v2: its dataset and metric
assert booleans, and `example-spec.md` §3.1 already established schema-version gating for a
future `TaskKind` enum. The library norm is concrete: stdlib `sqlite3` WAL, stdlib logging and
list-form subprocesses, git, uv, pytest, and DSPy only as an optional extra. No `functools`/
`lru_cache` calls exist in `src/` or `tests/`; Diffundo's cache ownership rule is a guard for
future code, not a claim that a cache exists. Finally, delete-over-add was not found by the
original search (`rg -i "delete" agents.md docs/architecture.md`), which is why it is recorded
as a **new** norm rather than misattributed history.

## 5. Citation map

| Principle | Snapshot references retained |
|---|---|
| (a) | `docs/architecture.md` §§7.2, 6.2; `worker-coldstart.md`; `review-implementation.md` §M2; `dspy-python-314.md` §Recommendation; `event-schema-draft.md` §3.2. |
| (b) | `src/cambium/events.py`; `src/cambium/modules/base.py`; `src/cambium/modules/example/decide.py`; `event-schema-draft.md` §2. |
| (c)/(e) | `architecture.md` §§5.1, 6.2, 7.8; `custos-asyncio-design.md` §§2.4, 3.1–3.3, 4; `logging-design.md` §§2.2, 2.9; `sqlite-wal-durability.md` §6. |
| (d)/(g) | `docs/module-template/architecture.md` §§3–5, §9.5; `architecture.md` §§2, 17.2; `modules/example/__init__.py`. |
| (f)/(h) | `modules/base.py` Protocols; `modules/example/decide.py` guards; `agents.md` §7 flat-control-flow bullet. |
| (i) | `decide.py:63`; `example-spec.md` §§3.1–3.2; `metric.py:18–19`; `dataset.py:49`; architecture §3.4. |
| (j)/(k) | `pyproject.toml`; `architecture.md` §§1, 8.1, 16.2, 19; stdlib `sqlite3`/logging/subprocess; `dspy-python-314.md`. |
| (l) | `agents.md` §7 and `architecture.md` §19 search; nearest proxy is “Concrete over abstract.” |

The target-base research anchor for the proposed agents patch was `/tmp/opencode/
cambium-agentsmd/agents.md@2b3bf93`. The patch itself is intentionally absent from this record;
the current root `agents.md` is authoritative. This avoids a second copied constitution while
retaining the exact source location and the distinction between translated intent and verified
implementation.

This is a historical evidence boundary.

Not current certification.

## 6. Historical translation examples

The rejected Rust mechanics were not discarded without replacements. Ownership and borrowing
became process/queue isolation; cache-line and manual layout rules became flat slots records
after measurement; static dispatch became a small Protocol/plain-function seam; SIMD and
intrinsics became “measure first” because this harness has no numeric kernel; `unsafe` became
ordinary GC-managed Python; and monomorphization became typed frozen dataclasses plus explicit
JSON schema. The translation keeps intent while refusing a false claim that Python can provide
the same machine-level guarantee.

The `(a)` row's cold-start example explains why “allocation-conscious” is conditional. The `(b)`
row's event rule explains why nested payloads are not a general modeling tool, although a schema-
defined nested payload remains allowed. The `(c)` row's bounded queue is a target invariant; the
historical store intentionally chose an unbounded source-of-truth queue and records that
deviation in conformance F-16. The `(d)` row keeps side effects at worker/supervisor/store edges,
so a future DSPy program can be swapped without moving persistence or process policy.

The `(g)` deletion test is stronger than “small files”: a module's imports, dataset, metric,
engine, and CLI boundary must be independently removable. The `(h)` exhaustive-match rule is a
future guard for domain enums, not proof that every current string status has become an enum.
The `(i)` boolean exception is the reviewed v2 data contract; changing it in a historical doc
would invalidate the frozen dataset. The `(j)` list names actual dependencies rather than a
generic “use libraries” slogan. The `(k)` owned-cache wording prevents a future cache from
becoming a hidden process global. Finally, `(l)` is explicitly new so later audits can ask what
was deleted, not merely what was added.

The status labels are historical: “Yes” means architecture or source precedent, “Partial” means
the norm or implementation is incomplete, and “New” means the proposed agents patch would have
made it normative. They do not certify future `WorkerState`, `ResultStatus`, `EventKind`, or
`SandboxKind` enums, nor do they claim a DSPy loop or dynamic task hierarchy exists.
