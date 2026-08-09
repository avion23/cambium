# Cambium — Fourth External Critique: Assessment

**Version:** 1.0.0
**Date:** 2026-08-09
**Branch:** `wt-doc-fb4` (`/tmp/opencode/cambium-doc-fb4`)
**Status:** Assessment of the fourth external critique against the current state
(architecture v2.0.0 + design-deltas D1–D7 + feedback-2 deltas D8a–D8g + merged research +
current `src/cambium/` modules). The orchestrator has already decided the disposition of
each claim; this document records the disposition, the reason, and the citation that
substantiates it.

**Sources read (read-only from `/home/ubuntu/cambium`):** `docs/architecture/architecture.md`
(v2.0.0), `agents.md`, `implementation-plan.md` (decisions 1–10), the research docs under
`docs/research/`, and the current modules under `src/cambium/`.

### 0.1 Verification convention

- **Every citation is a real repository-relative file/section.** Anything that could not be
  verified from the corpus is marked **UNVERIFIED** (§3) and attributed as
  orchestrator-provided context, not corpus evidence.
- The critique text itself is not yet in the corpus; claims are reproduced here from the
  orchestrator's disposition record. Claim numbers below match that record.
- Independent checks run for this assessment are cited with the exact command and outcome
  in §3.

---

## 1. Verdict table

| # | Claim | Disposition | Reason | Citation |
|---|---|---|---|---|
| 1 | Delete the Latin module names (Architectus/Custos/Opifex/Diffundo/Unio) | **REJECT** | The names are short (4–10 chars: Unio 4, Custos/Opifex 6, Diffundo 8, Architectus 10) and are the established vocabulary the corpus maps and reuses; `agents.md` makes the vocabulary normative ("Do not invent synonyms or new jargon"). Renaming churns 35 tracked files that reference them (Unio 20, Architectus/Diffundo 18 each, Opifex 15, Custos 16 — `rg -l` across the repo), including tests and docs, for a token saving the critique never measured. Per `agents.md` "Measure before optimizing", an unmeasured token-burn claim does not justify a cross-cutting rename. The architecture also explicitly decouples code from the names: "no module requires the Latin name to be used in code." | `agents.md` §6 (vocabulary norm), §7 "Measure before optimizing"; `docs/architecture/architecture.md` §4 (module catalog; "Latin names retained for continuity"); `rg -l 'Unio\|Custos\|Opifex\|Diffundo\|Architectus'` → 35 files |
| 2 | Delete `doctor.py` | **REJECT** | `cambium doctor` is a tested diagnostics command that catches real drift: worktree-hygiene checks (missing worktree directories — the Codex-doctor failure class) and event-store integrity (`PRAGMA integrity_check`) are exercised by `tests/scenarios/test_tooling.py`, and the secrets WARN fired truthfully on this host when run for this assessment (5 pass · 1 warn · 1 skip · 0 fail; `/home/ubuntu/.omp/agent/models.yml is git-tracked`). It is one file, P2 tooling (not on the P0 hot path, per the §19.4 build-phase vocabulary), with a stable exit-code contract (0 = no failure) and three scenario tests. Deleting it removes a real health surface for an unmeasured reason. | `src/cambium/doctor.py`; `tests/scenarios/test_tooling.py`; `docs/architecture/architecture.md` §13 (diagnostics command), §19.4 (P0/P1/P2 build phases); `docs/research/codex.md` (`codex doctor` model) |
| 3 | Replace SQLite with pure JSONL | **REJECT** | The SQLite event/conversation store is an earlier adopted directive and delta, not an open choice: the second critique's "SQLite WAL conversation store" was adopted as D8g and folded into `architecture.md` §6.6, and the conversation store is queried for composed context (D2 I2.4), not grepped. The durability experiment verifies 0-loss crash recovery: 8 crash trials / 44,992 committed events, zero lost, `integrity_check` `ok` in every scenario including SIGKILL mid-transaction. A JSONL mirror already exists for grep-ability (`events.jsonl`, optional per §6.1, written by the slice supervisor), so the critique's stated benefit is already present without dropping atomicity and queryability. | `docs/research/sqlite-wal-durability.md` §2 (0-loss crash recovery); `docs/architecture/architecture.md` §6.1 (primary store + JSONL mirror), §6.6 (conversation store, D8g); `docs/research/feedback-2-deltas.md` D8g; `src/cambium/store.py` |
| 4 | Use `ProcessPoolExecutor` instead of subprocess pipes | **REJECT** | `ProcessPoolExecutor` cannot provide streamed JSON-Lines IPC with `request_id` RPC framing, `killpg` process-group supervision (`start_new_session=True`), restart policies, or worktree-bound cwd/env per worker. Pipes are the verified design — the vertical slice proves the full pipe path (init→ready→run→result→exit) end to end — and they are the original Erlang/OTP one-for-one transient supervision vision the architecture retains. The critique demonstrates no failing behavior of the pipe design that a process pool would fix. | `docs/architecture/architecture.md` §5.1 (channel invariants), §7.2 (spawn: `start_new_session`, `pass_fds=()`), §0 (Erlang/OTP supervision kept); `src/cambium/supervisor.py`; `docs/research/vertical-slice-report.md` |
| 5 | Replace IPC with Redis/ZeroMQ (Proposal 1) | **REJECT** | A message broker is new infrastructure for a single-host, embeddable, headless-first library whose non-goals are "not distributed" and "no new frameworks." Pipes are zero-config and verified (JSON-Lines framing is implemented and tested in `ipc.py`). The critique does not demonstrate a falsified failure mode that Redis/ZeroMQ resolves; the "stdout EOF ≠ death" and backpressure failure classes are already addressed by the four-layer liveness model (§5.3) and the single-writer queue (§6.2). | `docs/architecture/architecture.md` §5 (transport), §1 non-goals 2 and 5, §5.3, §6.2; `src/cambium/ipc.py`; `agents.md` §7 (stdlib + git + DSPy; no new frameworks) |
| 6 | Remove DAG cycle detection (Proposal 3) | **REJECT** | 29 scenario tests in `tests/scenarios/test_tasktree.py` cover cycle detection, topological ordering, self-loops, depth/width bounds, and envelope shape (`rg -c '^def test_'` = 29). The proposal's own fallback — throw and ask the LLM to fix the plan — still requires the detection to name the cycle before the LLM can fix it, and `topological_order` raises `CycleError` naming the cycle path at dispatch. Kahn is ~20 lines (`_find_cycle` + `topological_order`). Removing it would reopen review DS-M6 (cyclic graphs leave tasks `pending` forever) and I2.2. | `tests/scenarios/test_tasktree.py` (29 tests); `src/cambium/tasktree.py` (`_find_cycle`, `topological_order`); `docs/architecture/architecture.md` §3.7 I2.2, §18.1 DS-M6 |
| 7 | Eliminate the diff from the upward payload (Proposal 4) | **ADOPT-LITE** (partial) | Keep the capped 64 KiB `unified_diff` in the upward envelope — it is the merge-conflict context the parent and evaluator need, it is the I2.7/D8b envelope rule, and `diff_truncated` already bounds it. Adopt the `include_diff` config flag (see claim 21) so higher tiers can disable the diff payload; it stays default-on for the evaluator tier. | `docs/architecture/architecture.md` §3.4 (`unified_diff` ≤ 64 KiB, `diff_truncated`), §5.2 (`result.diff`), §3.7 I2.7; `docs/research/feedback-2-deltas.md` D8b; `src/cambium/tasktree.py` `_ENVELOPE_KEYS` |
| 8 | Blackboard pattern for cross-cutting changes | **ADOPT-LITE** | A shared context substrate that siblings can query for schema/cross-cutting tasks is a sound augmentation of the I2.4 context-composition rule, and the conversation store (§6.6, D8g) is the natural home for it. Scope the adoption: it is a queryable store, not a free-for-all scratchpad — sibling access remains parent-mediated and context composition stays bounded (I2.4). Amendment to `architectus-design.md` is a separate task (branch `wt-doc-architectus` already carries that design doc). | `docs/architecture/architecture.md` §6.6 (conversation store), §3.7 I2.4; `docs/research/architectus-design.md` (in flight) |
| 9 | Flat task list with `requires:[...]` | **ALREADY-IMPLEMENTED** | `tasktree.build_tree` consumes exactly that: a flat plan `{"tasks": [{"task_id", "kind", "depends_on", "spec"}]}` (the critique's `requires` is the code's `depends_on`), with unique IDs, exactly one root, no multi-parent, and no cycles validated in Python before dispatch. The DAG is built and topologically ordered in Python (Kahn via `topological_order`); the LLM never outputs a nested tree — the decomposition payload is the flat list. | `src/cambium/tasktree.py` (`build_tree`:233–347, `topological_order`:350–377); `docs/architecture/architecture.md` §3.7; `docs/research/design-deltas.md` D2 |
| 10 | Data → Function → Data; only the supervisor has side effects | **ALREADY-IMPLEMENTED** | The module contract is pure JSON-in/JSON-out: `Module.decide()` is the pure function, and the D8a module CLI reads one JSON object on stdin and writes one JSON object on stdout; state and I/O live at the edges (dedicated writer thread for the event store, worker subprocesses for execution). The layering invariant makes the Deterministic Layer (Custos) the only place lifecycle side effects occur, and it never calls an LLM. | `src/cambium/modules/base.py` (`Module.decide`, `Metric`); `docs/architecture/architecture.md` §4 (module CLI, D8a), §2 (layering invariants); `agents.md` §7 (module shape) |
| 11 | Only lock on the final merge | **ALREADY-IMPLEMENTED** | The merge path is serialized by Unio's `asyncio.Lock`, held across verify-in-throwaway-worktree and publish to `refs/heads/main` — the only lock in the merge pipeline (event-schema-draft §3.11: "merge_started — Unio acquires the lock"). The concurrency semantics it protects were verified empirically in `worktree-concurrency.md` (concurrent merges: exactly one winner, 0/40 lost updates, no corruption). The `threading.Lock` in `store.py` is the event-writer's queue protection, not a second merge guard. | `docs/architecture/architecture.md` §7.8 (Unio `asyncio.Lock`, single writer); `docs/research/worktree-concurrency.md`; `docs/research/event-schema-draft.md` §3.11; `docs/research/design-deltas.md` D6 (already-resolved table) |
| 12 | Mailboxes/queues (CSP) | **ALREADY-IMPLEMENTED** | Every cross-thread/cross-process boundary is already a single-writer mailbox: the event-log single-writer queue discipline (`queue.Queue` in `store.py` on a dedicated writer thread), the logging `DropQueueHandler` bounded drop-on-full queues (`logging-design.md` §2.9), and the worker stdio pipes with per-worker message framing (§5.1, `ipc.py`). Note the store queue itself is **unbounded by design**: `store.py` documents that events are the source of truth and dropping one loses state, so "bounded-with-backpressure is a v2.1 option" (`store.py:20–22`); the v2.1 review's P0 gap 10 flags the unbounded store queue plus unbounded critical waits as the hardening item. The CSP disposition stands on the single-writer discipline, not on a bound. | `src/cambium/store.py` (:20–22 unbounded-by-design note, :106 `queue.Queue`); `docs/research/v2-1-review.md` §1.3 P0 gap 10; `docs/architecture/architecture.md` §6.2 (single-writer thread, bounded-queue proposal), §5.1 (pipes); `docs/research/logging-design.md` §2.9; `agents.md` §7 (no shared mutable state) |
| 13 | `check_system_health` before heavy ops | **ADOPT** | A pre-heavy-op health check (memory pressure/CPU load) protects the host from oversubscribed compile-heavy gates, which the v2.1 review lists as an open P0 resource gap. The helper must be stdlib-only — `/proc/meminfo` + `os.sysconf` — because the project norm forbids psutil and new dependencies. Implemented as `src/cambium/system_health.py` on branch `wt-luna-health` (d4db2ff) but **not yet merged into main** at assessment time; module task still in flight. | `docs/research/v2-1-review.md` §1.3 P0 gap 9; `agents.md` §7 (stdlib + git + DSPy; no new frameworks); `docs/architecture/architecture.md` §1 non-goal 5; branch `wt-luna-health` d4db2ff (`src/cambium/system_health.py`, unmerged) |
| 14 | Token bucket rate limiter | **ALREADY-IMPLEMENTED** | Diffundo's token-bucket rate limiting (per-provider `rpm` refill, empty bucket → `RATE_LIMITED` → skipped by the same selection-filter as cooldown), tier pause on total exhaustion, and fallback were adopted as D8f and folded into `architecture.md` §9.1/§9.2 and §7.4 (queue-level pause + recovery monitor). The branch implementation (`wt-impl-diffundo`) realizes the buckets; the folded architecture is the normative contract. | `docs/architecture/architecture.md` §9.1 (`rpm`), §9.2 (bucket check, `AllProvidersFailed` → pause), §7.4; `docs/research/feedback-2-deltas.md` D8f; `docs/research/v2-1-review.md` §1.1 item 7 |
| 15 | Local cache for DSPy evals | **ADOPT** (scoped) | An eval-harness-only cache keyed on full-prompt+model, production-disabled, does not violate D1: D1's no-cache is a *production* correctness choice (repo-state coherence for the live diffundo router), while eval inputs are frozen (`eval_frozen_at`/`canary_frozen_at` in the dataset `meta.json`), so a deterministic prompt→response cache in Ascensus/bench is coherence-safe. Scope: eval-harness only, never on the worker/orchestrator hot path. Implemented as `src/cambium/eval_cache.py` on branch `wt-luna-evalcache` (d8f9408) but **not yet merged into main** at assessment time; task still in flight. | `docs/research/design-deltas.md` D1; `docs/architecture/architecture.md` §8.1 (production no-cache), §4 (M9 Ascensus offline); `src/cambium/modules/example/datasets/meta.json`; `docs/research/bench-harness-design.md`; branch `wt-luna-evalcache` d8f9408 (`src/cambium/eval_cache.py`, unmerged) |
| 16 | Mock git env + AST asserts for evals | **ALREADY-IMPLEMENTED in part** | The frozen dataset splits, canaries, and metric already exist: `train.jsonl`/`eval.jsonl`/`canaries.jsonl` with frozen markers and `should_decompose_metric`, exercised by `test_dataset_splits.py` and the module scenario test. The mock-git eval environment and AST-assert scoring are a v2.1 eval enhancement designed in `bench-harness-design.md` §8 (merged 97ef7d6), not delivered in v2. | `src/cambium/modules/example/datasets/*` + `meta.json`; `src/cambium/modules/example/metric.py`; `docs/research/bench-harness-design.md` §8 (mock git env §8.1, AST-assert §8.2, falsification §8.3); `docs/research/example-datasets-v1.md`; `tests/scenarios/test_dataset_splits.py` |
| 17 | AST code search tool | **ADOPT** | A tree-sitter-based AST/symbol search tool gives workers a structured code-lookup primitive that the v2 tool set deliberately lacks ("No AST/symbol search. Planned for v2.1", §11). The v2.1 review's Proposal 1 (tree-sitter context compression, M9) already scopes this class of work. **Merged in main** as `src/cambium/ast_tools.py` (1ed155b, via `merge: luna-ast`): definitions/references/signature search with a tree-sitter backend and a stdlib `ast` fallback (`backend()` reports the selected backend). The "tree-sitter 0.26.0 verified on CPython 3.14" claim is orchestrator-provided — **UNVERIFIED in this corpus** (§3); the module's tree-sitter optional path still needs that version check recorded. | `src/cambium/ast_tools.py` (main, 1ed155b); `docs/architecture/architecture.md` §11 (tool set; AST search deferred to v2.1); `docs/research/v2-1-review.md` §M9 |
| 18 | LSP/lint diagnostics | **ADOPT-LITE** | Ruff-based lint diagnostics after edits reuse the already-present ruff dev dependency (`dev = ["pytest>=9", "ruff>=0.12"]` in `pyproject.toml`, `[tool.ruff]` configured), and ruff cleanliness over `src/` is already a scenario test. No pyright: it is too heavy for the stdlib + git + DSPy norm. Implemented as `src/cambium/lint_diag.py` on branch `wt-luna-lint` (2d26e5f) but **not yet merged into main** at assessment time; module task still in flight. | `pyproject.toml` (dev extra, `[tool.ruff]`); `tests/scenarios/test_tooling.py` (`test_ruff_check_clean_on_src`); `agents.md` §7; branch `wt-luna-lint` 2d26e5f (`src/cambium/lint_diag.py`, unmerged) |
| 19 | Strict tool schemas | **ADOPT-LITE** | Strict, machine-checked tool schemas are implemented with a stdlib dataclass→JSON-Schema converter module — no pydantic hard dependency (`dependencies = []` in `pyproject.toml`; pydantic is not in `uv.lock`); pydantic stays optional via the dspy extra. This matches the module shape norm (typed dataclasses are the CLI schema, D8a). **Merged in main** as `src/cambium/schemas.py` (4e2c2ea, `wt-luna-schemas`): stdlib-only dataclass→JSON-Schema conversion plus `validate_tool_call` for deterministic rejection of invalid tool calls. | `src/cambium/schemas.py` (main, 4e2c2ea); `pyproject.toml` (`dependencies = []`); `agents.md` §7 (module shape, strict JSON schemas); `docs/architecture/architecture.md` §4 (module CLI: typed dataclasses are the schema) |
| 20 | Speculative batched tool calls (Proposal 2) | **ADOPT-LITE** | Speculative batching of worker tool calls is a worker-loop efficiency idea, not a correctness change; v2 keeps the sequential per-tool heartbeat loop (§7.6). Record it as a v2.1 worker-loop design note in the architectus amendment rather than building it now. | `docs/architecture/architecture.md` §7.6 (per-tool heartbeat loop), §11 (tool set); `docs/research/architectus-design.md` (in flight); `docs/research/v2-1-review.md` §1.1 item 3 (Opifex seed) |
| 21 | `include_diff` config flag | **ADOPT-LITE** | Adopt a config flag that turns the diff off the upward payload for higher tiers, while keeping the diff capped (64 KiB, `diff_truncated`) and default-on for the evaluator tier that scores on it. Add as an `architecture.md` §3.4 note; the envelope key set and `Nuntius`/`Custos` schema validation stay the enforcement point. | `docs/architecture/architecture.md` §3.4 (`unified_diff`, `diff_truncated`), §3.7 I2.7; `src/cambium/tasktree.py` `_ENVELOPE_KEYS` |

**Counts:** 21 rows — **REJECT 6** (1, 2, 3, 4, 5, 6) · **ADOPT 3** (13, 15, 17) ·
**ADOPT-LITE 6** (7, 8, 18, 19, 20, 21) · **ALREADY-IMPLEMENTED 6** (9, 10, 11, 12, 14, 16).

---

## 2. What this means for the plan

The dispositions above create concrete follow-ups. Module-state is as of main `6d80a05`:

1. **Blackboard amendment to `architectus-design.md`** (claim 8): shared conversation-store
   context queryable by siblings for cross-cutting/schema tasks, scoped to parent-mediated,
   bounded I2.4 access. Separate task — branch `wt-doc-architectus` already holds the
   `architectus-design.md` draft.
2. **Stdlib `check_system_health` helper module** (claim 13): `/proc/meminfo` + `os.sysconf`
   based, no psutil, run before compile-heavy gates/merges. Committed on `wt-luna-health`
   (d4db2ff) as `src/cambium/system_health.py`; **merge to main pending**.
3. **Eval-harness-only LLM cache** (claim 15): keyed on full-prompt+model, production-
   disabled, confined to `Ascensus`/bench where eval inputs are frozen. Committed on
   `wt-luna-evalcache` (d8f9408) as `src/cambium/eval_cache.py`; **merge to main pending**.
4. **Tree-sitter AST code search tool** (claim 17): **merged in main** as `src/cambium/
   ast_tools.py` (1ed155b). Remaining: record the tree-sitter 0.26.0-on-3.14 verification
   (currently **UNVERIFIED**, §3) and wire the tool into the worker tool set (§11).
5. **Ruff-based lint diagnostics after edits** (claim 18): reuses the existing ruff dev
   dependency. Committed on `wt-luna-lint` (2d26e5f) as `src/cambium/lint_diag.py`;
   **merge to main pending**.
6. **Stdlib dataclass→JSON-Schema converter module** (claim 19): **merged in main** as
   `src/cambium/schemas.py` (4e2c2ea) — strict tool schemas without a pydantic hard
   dependency; remaining work is consumer wiring.
7. **v2.1 worker-loop design note: speculative batched tool calls** (claim 20): recorded in
   the architectus amendment; not built in v2.
8. **`include_diff` config flag** (claims 7, 21): `architecture.md` §3.4 note; diff stays
   capped and default-on for the evaluator tier, configurable off for higher tiers.

No disposition requires reverting an existing adopted delta or the SQLite/pipe/cycle-detection
decisions — the six REJECTs preserve the event-store durability contract, the pipe IPC, the
Kahn cycle detection, and the stable vocabulary.

---

## 3. UNVERIFIED flags

- **Tree-sitter 0.26.0 on CPython 3.14 (claim 17).** Orchestrator-provided; no research doc in
  this corpus records a tree-sitter version verification (`rg "tree-sitter|0\.26" docs/research/`
  hits only `v2-1-review.md` §M9, `tui-best-practices.md`, `opencode.md` — none claim a version
  verified on 3.14). `ast_tools.py` is merged with a tree-sitter optional path and a stdlib
  fallback, but the 0.26.0-on-3.14 check must still be recorded before that path is relied on.
- **The critique's token-burn claim (claim 1).** Unmeasured; the rejection rests on the
  absence of a benchmark, not on a counter-benchmark. If the critique produces a measured
  saving, the rename question reopens.
- **Unmerged module tasks (claims 13, 15, 18).** `system_health.py` (wt-luna-health
  d4db2ff), `eval_cache.py` (wt-luna-evalcache d8f9408), and `lint_diag.py` (wt-luna-lint
  2d26e5f) exist on their branches but were **not in main at assessment time (6d80a05)** —
  verified by `git merge-base --is-ancestor <commit> HEAD` failing for each. Their
  dispositions are plan-level until the merges land; claims 17 and 19 are merged (1ed155b,
  4e2c2ea — `--is-ancestor` true). Claim 20 is a design note, not a module.
- **`doctor` on other hosts (claim 2).** The WARN fired truthfully on this host; behavior on
  other hosts (no `.omp` install, different git/worktree layouts) is exercised only by the
  healthy-repo and corrupt-store scenario tests, not by this assessment.

### 3.1 Checks run for this assessment

- `python3.14 -c` unavailable for `cambium` on bare PATH; `PYTHONPATH=src python3.14 -m
  cambium.doctor` → **5 pass · 1 warn · 1 skip · 0 fail, exit 0** (secrets WARN:
  `/home/ubuntu/.omp/agent/models.yml is git-tracked`). Supports claim 2.
- `rg -c '^def test_' tests/scenarios/test_tasktree.py` → **29**. Supports claim 6.
- `rg -l 'Architectus|Custos|Opifex|Diffundo|Unio'` (excluding `uv.lock`) → **35 files**
  (Unio 20, Architectus 18, Diffundo 18, Custos 16, Opifex 15). Supports claim 1.
- `pyproject.toml` `dependencies = []`; no `pydantic` entry in `uv.lock`. Supports claim 19.
- `src/cambium/modules/example/datasets/meta.json` carries `eval_frozen_at` and
  `canary_frozen_at`. Supports claims 15/16.
- `src/cambium/store.py:20–22` documents the event queue as "unbounded by design… dropping
  one would lose state; bounded-with-backpressure is a v2.1 option"; `queue.Queue()` at
  `store.py:106`. Supports claim 12.
- `rg -n "^## 8" docs/research/bench-harness-design.md` → §8 "DRAFT (v2.1, M8 scope) — Mock
  git eval environment and AST-assert evaluation" (merged 97ef7d6). Supports claim 16.
- Module merge state at main `6d80a05`: `git merge-base --is-ancestor` → **IN MAIN**:
  `ast_tools.py` 1ed155b, `schemas.py` 4e2c2ea; **NOT IN MAIN**: `system_health.py`
  d4db2ff, `eval_cache.py` d8f9408, `lint_diag.py` 2d26e5f. `ls src/cambium/` confirms
  `ast_tools.py` and `schemas.py` present in the main working tree. Supports claims 13, 15,
  17, 18, 19.
