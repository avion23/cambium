# Cambium Coding Constitution — Rust/HFT → Python 3.14 Translation

**Author:** wt-constitution worktree
**Date:** 2026-08-09
**Status:** Research. Input to the agents.md update task. The ready-to-apply patch is §6.
**Source:** the orchestrator's Rust/HFT coding-preference constitution (12 principles, (a)–(l)).
**Scope:** translate each principle for Cambium (Python 3.14, standard GIL CPython, asyncio
supervisor + subprocess-per-worker), keep what survives, reject what does not, and map each
survivor to where the design **already** implements it — citing `docs/architecture.md`,
`docs/research/*`, and `src/cambium/*`.

**Current-main note (2026-08-09):** this research preserves its original
verification snapshot. `worker-coldstart.md` is now tracked in current main;
the branch-local provenance below is historical. The canonical architecture
path in the current tree is `docs/architecture/architecture.md`.

Verification rule: every citation below was spot-checked against the cited file in this
worktree (evidence in §5). Anything that could not be verified is flagged.

---

## 1. What was rejected in translation (Rust-only parts)

The Rust/HFT constitution optimizes for a memory-managed, allocation-exact, statically
dispatched native runtime. Most of that does not survive to Python; only the *intent*
survives, restated as Python norms.

| Rust/HFT concern | Verdict | What survives |
|---|---|---|
| Zero-cost abstractions; no runtime overhead | **Rejected** — Python is interpreted; "zero cost" is meaningless | Measurement-first rule (a): profile, then optimize the measured floor |
| Ownership / borrowing / move semantics | **Rejected** — no compile-time borrow checker in Python; enforcement is architectural (processes, queues, single-writer), not syntactic | "Never share mutable state across threads" survives as (c)/(e) |
| Cache-line control, manual memory layout (`repr(C)`, `offset_of`, alignment), stack-vs-heap placement | **Rejected** — Python allocates freely; the object layout is the interpreter's | "Flat `slots` dataclasses + lists over deep object graphs" survives as (b); "allocation-conscious only after measurement" as (f) |
| No vtables; static dispatch; `dyn trait` | **Rejected as a literal rule** — Python dispatch is dynamic by design | Interface minimalism survives: small `Protocol`s and plain functions over deep class hierarchies (f) |
| SIMD / intrinsics / vectorization | **Rejected** — not applicable to harness code; numpy-style vectorization is out of scope | Only the measure-before-optimizing rule (a) |
| Manual memory management; `unsafe` | **Rejected** — GC'd language | — |
| Generics monomorphization / no dynamic polymorphism | **Rejected** — Python generics are type hints only | Typing discipline survives as (i): enums and typed frozen dataclasses |
| `perf`/`valgrind`/`cachegrind` profiling discipline | **Translated** | cProfile/time-based measurement; the design's first measured floor is worker cold start (§2 (a)) |

---

## 2. Principle-by-principle translation

Columns: **PRINCIPLE** (verbatim, condensed) → **PYTHON TRANSLATION** (concrete, actionable)
→ **ALREADY-IMPLEMENTED?** (cite, or NEW) → **NOTES**.

| # | PRINCIPLE | PYTHON TRANSLATION | ALREADY-IMPLEMENTED? | NOTES |
|---|---|---|---|---|
| (a) | **Mechanical sympathy** — hardware-aware code; know where time actually goes; no needless churn in hot paths | Measure before optimizing. The dominant cost in Cambium's hot path is **worker cold start**: interpreter startup + `import dspy` and its ~57 transitive packages. Allocation micro-opts are noise until that floor is addressed. Keep hot loops allocation-conscious only after measurement; prefer flat `slots` dataclasses + lists over deep object graphs. | **Partial.** Cold start is already a first-class design concern: `ready_timeout` default 60 s (`docs/architecture.md` §7.2, §14 IMPL-M2); the event log is off the loop so the supervisor loop never does disk I/O (§6.2). "Measure before optimizing" as a *standing norm*: **NEW** (patch adds it). | The brief cited `docs/research/worker-coldstart.md` ("dspy import 2.1s dominates") — that benchmark was branch-local at drafting time and is now in current main (commit 108c83d provenance), measuring `import dspy` ≈ 2.1 s (median 2166.8 ms, p90 2188.6 ms). The current supporting evidence also includes `docs/reviews/review-implementation.md` §M2 ("`import dspy` and transitive imports: potentially 1–3 seconds") and `docs/research/dspy-python-314.md` §Recommendation (57 packages; optional-extra + lazy-import policy). The architectural mitigation is documented at `docs/research/event-schema-draft.md` §3.2 (`ready_timeout_s`). |
| (b) | **Data-oriented design** — flat records, contiguous iteration, no pointer-graph modeling | Model data as flat frozen `slots=True` dataclasses and lists; iterate contiguous sequences of flat records; represent events as flat payloads (JSON-serializable dicts), never object graphs. | **Yes.** `src/cambium/events.py` — all four event types are `@dataclass(frozen=True, slots=True, kw_only=True)` (lines 14/22/31/41). `base.py` `Example` is frozen+slots (line 21). `decide.py` `TaskInput`/`DecomposeOutput` are frozen+slots (lines 51/59). `docs/research/event-schema-draft.md` §2 defines a single flat canonical envelope with `payload` as a flat dict. | The event-schema draft (§3, 21 kinds) keeps every event one flat record — the "no pointer-graph modeling" rule. New payloads must stay flat; nest only where the schema draft already does. |
| (c) | **CSP/actor** — never share mutable state across threads; bounded queues + single-writer discipline; workers as processes with pipes | Cambium's architecture **is** this rule. Norms: exactly one writer per store; bounded queues with drop-on-full backpressure; mutable state never crosses a thread boundary; workers are separate processes over stdio pipes. | **Yes, structurally.** Single-writer event-log thread + bounded 10 000-entry queue: `docs/architecture.md` §6.2, `docs/research/custos-asyncio-design.md` §2.4. Bounded drop-on-full logging queue (`DropQueueHandler`): `docs/research/logging-design.md` §2.2 footgun 1, §2.9. Workers as processes over pipes: `docs/architecture.md` §2 (layering), §5.1 (channel invariants). Only immutable, redacted `Event` dataclasses cross any thread boundary: `custos-asyncio-design.md` §3.3. | `docs/research/sqlite-wal-durability.md` §6 empirically validates the single-writer-thread pattern (per-thread connections, `threadsafety=3`, writer connection created inside the writer thread). The NEW framing to add: "add nothing that shares mutable state across threads — queue-based isolation is the enforcement." |
| (d) | **Functional core / imperative shell** — business logic = pure functions on flat structs; state + I/O at the edges | Module rule engines are pure functions; `Module.decide()` is the pure seam; all state and I/O live at module/supervisor edges (worker spawn, IPC, event log). | **Yes.** `docs/module-template/architecture.md` §5.1 (rule engine primary, DSPy seam; `decide()` is the only surface a replacement must implement). `src/cambium/modules/base.py` `Module` ABC (`decide()`/`metric()`, lines 46–57). The reference module is explicitly stateless across calls: `src/cambium/modules/example/architecture.md` §"State". In the target base (`agents.md` @ 2b3bf93) the norm is already restated twice in §7: the "Flat over nested" tail ("Business logic in pure functions; state and I/O at the edges") and the "Engine swap is a strategy pattern" bullet — the patch merges into them (§6 preamble). | Reference instance: `src/cambium/modules/example/decide.py` — `should_decompose(task, context)` is a pure function; the `Module` subclass delegates to it (lines 155–157). A future DSPy program preserves the same pure interface. |
| (e) | **Concurrency rules** — no mutex-sharing, queues over locks, tiny bounds, deadlock immunity | Queue-based isolation is already the architecture. Python norms: asyncio **loop-affine state** (one loop task mutates a handle, no `await` between check and set); single-writer stores; bounded queues; the only `asyncio.Lock` in the deterministic layer is Unio's merge lock. | **Yes.** Loop-affine `WorkerHandle` with atomic check-and-set transitions: `custos-asyncio-design.md` §3.1–3.2. Single-writer stores: §2.4. Bounded 10k queue, critical events never dropped (block ≤100 ms), non-critical drop-on-full: `docs/architecture.md` §6.2 inv. 2–3. Unio's `asyncio.Lock` is the sole lock: §7.8, `custos-asyncio-design.md` §3.3. "Bounded everything": `docs/architecture.md` §1 goal 5, §19 item 14. | Deadlock immunity in Python terms: no thread locks on shared state at all (only bounded queues + `call_soon_threadsafe` handoffs), and every await in the shutdown sequence is timeout-bounded (`custos-asyncio-design.md` §4 steps 3–5). |
| (f) | **Memory layout** — zero pointer chasing, no vtables, static dispatch | Python allocates freely, so the meaningful norms are: prefer `Protocol`s / plain functions over deep class hierarchies; no dynamic machinery where a plain function suffices; keep hot loops allocation-conscious **only after measurement** (cold start dominates — see (a)). | **Partial.** `base.py` already defines `Output` and `Metric` as `Protocol`s (lines 17/40) and the rule engine is a plain function (`decide.py`). But there is no *standing norm*; interface minimalism as a stated rule is **NEW**. | "No vtables / static dispatch" is rejected as a literal rule — Python dispatch is dynamic by design; the translated form is interface minimalism, not dispatch strategy. Allocation tuning is governed by the measurement-first rule (a), not by speculation. |
| (g) | **Architecture** — easy-to-delete modules, small interfaces (message structs), composition over inheritance, minimal state/scope | A module's contract is a small, JSON-schema-shaped set of frozen-dataclass inputs/outputs; modules are deletable without breaking siblings; a module composes engine + dataset + metric + loader rather than inheriting deep hierarchies. | **Yes.** Typed inputs/outputs/errors: `docs/module-template/architecture.md` §3. Minimal explicit state table: §4. Sibling pinning keeps each module independently optimizable and removable: `docs/module-template/architecture.md` §9.5, `docs/architecture.md` §17.2. Reference module composition: `src/cambium/modules/example/__init__.py` (exports engine + loader + metric). Interface shape already stated by the §7 "Module shape" bullet in the target base. The onboarding checklist (`agents.md` §10) makes every module self-contained. | The deletion test: `rm -rf src/cambium/modules/<name>/` must break nothing else — guaranteed by §3 interface isolation + §17.2 pinned siblings + `example-spec.md` §12 (extensions are separate files, not woven into siblings). |
| (h) | **Control flow** — pure functions, flat control flow (early returns/guards), exhaustive matching | Early returns and guard clauses; exhaustive `match` over enums for domain alternatives. | **Yes.** `agents.md` §7: "Flat over nested. Early returns, guard clauses, exhaustive match/switch." | Reference implementation: `decide.py` uses guard-clause short-circuits (context pre-decomposition check at line 80, evidence threshold at line 133). Python 3.14 `match` is available for enum dispatch; the norm is exhaustive matching. |
| (i) | **Types** — enums over bools/ints, no boolean traps | Real enums for domain alternatives (`WorkerState`, `ResultStatus`, `EventKind`, `SandboxKind`, …); booleans only for genuine predicates and API compatibility. | **Yes** as a norm — `agents.md` §7 (two bullets: "Real enums for domain alternatives", "Booleans are for predicates and API compatibility only"). The v2 architecture uses `Literal` for status fields (§3.4). | **Scaffold exception (do NOT change now):** `DecomposeOutput(decompose: bool)` (`src/cambium/modules/example/decide.py` line 63) is a **reviewed v2 contract** — authoritative in `docs/module-template/example-spec.md` §3.2, enforced by the metric and dataset loader. It is a genuine boolean-trap candidate, but v2 keeps it. **Recommend v2.1 migration:** replace `decompose: bool` with a `Decision { DECOMPOSE, DO_NOT_DECOMPOSE }` enum across `DecomposeOutput`, dataset `expected.decompose`, and the metric, **gated behind a dataset schema-version bump** — exactly the precedent `example-spec.md` §3.1 already set for the dropped `task_kind_hint` field (v2.1 `TaskKind` enum, schema-version gated). |
| (j) | **Ecosystem** — prefer battle-tested libraries over custom infra | Already project policy: stdlib + git + uv + pytest; dspy as an optional extra; no hand-rolled logging, IPC framing, or persistence. | **Yes.** `agents.md` §7: "Stdlib + DSPy + git. No new frameworks." `docs/architecture.md` §1 non-goal 5. `pyproject.toml`: `dependencies = []`, optional `test = ["pytest>=8.0"]`. dspy-optional-extra recommendation: `docs/research/dspy-python-314.md` §Recommendation items 1–2. | Instances of the principle already in the design: SQLite WAL via stdlib `sqlite3` (`architecture.md` §6.1), stdlib `logging` `QueueHandler`/`QueueListener` instead of a logging framework (§13), list-form `subprocess.run` instead of hand-rolled exec (§11). Note: the dspy optional extra is **recommended** but not yet in `pyproject.toml`. |
| (k) | **No globals, no hidden state, no singletons, no function-level static state** | Config flows through the frozen `Config` dataclass; runtime state lives under `${session_dir}/.cambium/`; no module-level mutables; no process-global caches outside explicitly-owned ones (the `Diffundo` cache is owned). Python nuance: `functools.cache` and mutable default args are function/class-level statics — permitted only at owned boundaries. | **Yes.** `agents.md` §7: "No hidden global state. … No module-level mutables, no process-global caches outside explicitly-owned ones (`Diffundo` cache is owned)." `docs/architecture.md` §19 item 6 ("No hidden global state"), §16.2 invariant 5 ("No implicit global state"), §8.1 (cache ownership). | Verified: no `functools`/`@cache`/`lru_cache` usage anywhere in `src/` or `tests/` today — the cache-discipline nuance is prospective (guards future code), not a fix for existing code. |
| (l) | **Delete over add** | Prefer deleting, composing, or reusing an existing library over adding new code. A smaller interface is easier to reason about and delete later. | **NO — NEW.** Verified by search: no delete-over-add norm exists in `agents.md` §7 or `docs/architecture.md` §19. Nearest existing proxy: `agents.md` §7 "Concrete over abstract. Inline unless a boundary is independently meaningful." | The task brief stated this was "already an agents.md norm"; it is not. The patch (§6) adds it as a new normative bullet. |

---

## 3. Translation summary

- **12 principles** (a)–(l), all covered in §2.
- **Already implemented:** 9 — (b), (c), (d), (e), (g), (h), (i), (j), (k).
- **Partially implemented (norm missing, design present):** 2 — (a) measurement-first as a standing rule; (f) interface-minimalism as a standing rule.
- **New (must be added by the agents.md patch):** 1 — (l) delete over add.
- **Rejected/translated away:** the Rust-only mechanics — zero-cost abstractions, ownership/borrowing, manual memory layout, cache-line control, static dispatch/vtables, SIMD, `unsafe`. Each survived only as a Python-norm intent (measurement-first, no shared mutable state, flat `slots` records, small `Protocol`s/plain functions). See §1.
- **One uncited premise corrected:** the brief's `worker-coldstart.md` was branch-local when this record was drafted and is now in current main (commit `108c83d` provenance), measuring `import dspy` ≈ 2.1 s; the in-main evidence also includes `review-implementation.md` §M2 (1–3 s `import dspy`) and `dspy-python-314.md` (§Recommendation: 57 packages, lazy-import policy).
- **One norm misattributed in the brief:** delete-over-add is **not** currently in `agents.md`; the patch introduces it.

---

## 4. Cross-checks against `agents.md` structure (for the patch)

- In the target base (`/tmp/opencode/cambium-agentsmd/agents.md` @ 2b3bf93): §7 "Coding norms specific to Cambium" is a flat bullet list ending with "Durable state layout" (line 129); §8 = "Design norms"; §9 = "Where to look for what"; §10 = "What 'done' means for a module". The patch inserts a `###` subsection after the "Durable state layout" bullet (end of §7), before §8 — structurally minimal, no renumbering needed.
- The patch keeps every existing §7 bullet untouched; the two bullets it overlaps ("Module shape", "Engine swap is a strategy pattern") are reconciled as one-line pointers in the patch (§6 preamble), the fuller existing bullet winning.
- The patch references `docs/research/coding-constitution.md` for detail rather than duplicating it (§6).

---

## 5. Verification appendix (spot-checked citations)

Each row: citation → checked fact → evidence location.

| # | Citation | Checked | Where |
|---|---|---|---|
| 1 | `events.py` frozen+slots dataclasses | `@dataclass(frozen=True, slots=True, kw_only=True)` on all four types | `src/cambium/events.py:14,22,31,41` |
| 2 | `base.py` `Output`/`Metric` Protocols + frozen `Example` | `class Output(Protocol)`, `class Metric(Protocol)`, `@dataclass(frozen=True, slots=True) class Example` | `src/cambium/modules/base.py:17,21-22,40` |
| 3 | `custos-asyncio-design.md` §2.4 single-writer event store | section titled "Single-writer discipline for the event store"; queue inventory table | `docs/research/custos-asyncio-design.md` §2.4 (lines 105–124) |
| 4 | `logging-design.md` bounded drop-on-full backpressure | §2.2 footgun 1 (`DropQueueHandler`, verified 203 ms/5000 records vs 2148 ms vanilla); §2.9 "Queue bounds and drop policy (backpressure)" | `docs/research/logging-design.md:121-129, 311-323` |
| 5 | `DecomposeOutput(decompose: bool)` reviewed contract | `decompose: bool` in `DecomposeOutput`; metric + loader assert booleans; `example-spec.md` §3.2 authoritative, §3.1 enum-migration precedent | `src/cambium/modules/example/decide.py:63`; `metric.py:18-19`; `dataset.py:49`; `docs/module-template/example-spec.md` §3.1 (lines 64), §3.2 |
| 6 | agents.md §7 norms (flat control flow, enums, no globals, no frameworks; target base) | the §7 bullets quoted in rows (d),(h),(i),(j),(k) are present verbatim in the target base, incl. "Module shape" and "Engine swap is a strategy pattern" | `/tmp/opencode/cambium-agentsmd/agents.md` §7 (lines 117–129) |
| 7 | architecture.md §19 item 6 / §16.2 inv. 5 (no hidden state); §6.2 bounded queue; §7.8 Unio lock | item 6 "No hidden global state"; invariant 5 "No implicit global state"; 10 000-bounded queue; Unio `asyncio.Lock` scope; "the only `asyncio.Lock` in the deterministic layer is Unio's" | `docs/architecture.md:1123,966,405,672`; `docs/research/custos-asyncio-design.md:157` |
| 8 | cold-start evidence (review-implementation §M2; dspy-python-314 §Recommendation) | §M2 "`import dspy` … 1–3 seconds"; dspy extra "57 packages", lazy-import recommendation | `docs/reviews/review-implementation.md:183`; `docs/research/dspy-python-314.md:195-223` |
| 9 | delete-over-add absent from agents.md/architecture | no "delete/remove/unused" norm found in either | `rg -i "delete" agents.md docs/architecture.md` (no normative hit) |
| 10 | module template §3/§4/§5.1 (§(d),(g) cites) | typed interfaces, state table, "rule engine primary, DSPy seam" | `docs/module-template/architecture.md:30-66,70-87` |

---

## 6. agents.md patch

Ready to apply verbatim. Target base: `/tmp/opencode/cambium-agentsmd/agents.md` @ 2b3bf93.
Insert after the §7 "Durable state layout" bullet (end of §7), before §8 "Design norms".
~53 lines (the fenced block below).

```
## (patch) INSERT into agents.md — after the §7 "Durable state layout" bullet (end of §7),
##                      before §8 "Design norms"

### Coding principles (translated constitution)

> The Rust/HFT coding-preference constitution, translated for Cambium's Python 3.14 stack.
> Detail and citations per principle: `docs/research/coding-constitution.md` (a)–(l).
> Bullets marked **new** become normative on merge; the rest restate or sharpen existing
> §7 / `docs/architecture.md` §19 norms.
> Overlaps with existing §7 bullets are merged — ONE bullet each, the fuller existing bullet
> wins: "Module shape" absorbs the patch's "Small, JSON-schema-shaped interfaces"; "Engine
> swap is a strategy pattern" (with the "Flat over nested" tail) absorbs the patch's
> "Business logic = pure functions on flat structs" — those two patch bullets are one-line
> pointers below. The "Prefer Protocols" bullet shares the Protocols point with "Module shape"
> but keeps its new no-deep-hierarchy/no-dynamic-machinery norm.

- **Measure before optimizing.** *New.* Time goes where measurement says it goes: worker cold
  start is dominated by interpreter startup + `import dspy` (~1–3 s, `docs/reviews/
  review-implementation.md` §M2), so allocation micro-opts are noise until that floor is
  addressed. Profile first; do not churn hot paths on speculation. See (a).
- **Flat records over deep object graphs.** Data lives in frozen `slots=True` dataclasses and
  lists — `events.py`, `base.py.Example`, `decide.py.TaskInput` are the precedent. Events are
  flat payloads, not pointer graphs. See (b).
- **No shared mutable state across threads.** Cambium's architecture is the enforcement:
  single-writer event-log thread with a bounded queue (`docs/architecture.md` §6.2;
  `docs/research/custos-asyncio-design.md` §2.4), bounded drop-on-full logging queues
  (`docs/research/logging-design.md` §2.9), workers as separate processes over stdio pipes
  (§5.1). Add nothing that shares mutable state across threads. See (c).
- **asyncio loop-affine state.** Mutable handles are mutated by exactly one loop task per
  transition, with no `await` between check and set (`docs/research/custos-asyncio-design.md`
  §3.1). Anything crossing into a thread is an immutable, already-redacted value. See (e).
- **Business logic = pure functions on flat structs; state and I/O at the edges.** Covered by the existing §7 "Engine swap is a strategy pattern" bullet (`Module.decide()` is the seam) and the "Flat over nested" tail; see `docs/research/coding-constitution.md` (d).
- **Enums over booleans/ints for domain alternatives.** `WorkerState`, `ResultStatus`,
  `EventKind`, `SandboxKind` are enums, not strings; booleans are predicates and API
  compatibility only (existing §7 bullets). New domain alternatives are enum members — the
  v2.1 `Decision` migration for `should_decompose` is documented in `docs/research/
  coding-constitution.md` (i); do not change the reviewed v2 contract now.
- **Prefer Protocols and plain functions over deep class hierarchies.** `base.py`
  `Output`/`Metric` are the precedent. No dynamic machinery where a plain function suffices.
  Composition over inheritance; a module is a small interface + a pure core. See (f), (g).
- **Small, JSON-schema-shaped interfaces; modules deletable without breaking siblings.** Interface shape = the existing §7 "Module shape" bullet; new here: pinned siblings (`docs/architecture.md` §17.2) keep a module removable without breaking siblings — §10 "done" is the deletion checklist; see `docs/research/coding-constitution.md` (g).
- **Flat control flow.** Early returns, guard clauses, exhaustive `match` over enums (existing
  §7 "Flat over nested"). See (h).
- **No globals, no hidden state, no singletons.** Existing §7 "No hidden global state";
  `docs/architecture.md` §19 item 6, §16.2 invariant 5. New nuance: `functools.cache` and
  class-level mutable defaults are static state — use them only at explicitly-owned boundaries
  (the `Diffundo` cache is owned). See (k).
- **Battle-tested libraries over custom infra.** Stdlib + git + uv + pytest; dspy is an
  optional extra. No hand-rolled logging, IPC framing, or persistence (`pyproject.toml`;
  `docs/architecture.md` §1 non-goal 5). See (j).
- **Delete over add.** *New.* Prefer deleting, composing, or using an existing library over
  adding new code. A smaller interface is easier to reason about and delete later. See (l).
```

End of patch. Post-apply checks: `agents.md` keeps every existing §7 bullet unchanged; the
subsection adds 12 bullets (2 are one-line pointers to existing §7 bullets); the subsection
sits under §7, so no section renumbering is required.
