# agents.md — Orientation for Agents Working in Cambium

> This file is **orientation**, not rules. It exists so that any agent (human, LLM, or hybrid) landing in this repository for the first time can become useful in minutes rather than hours. Read it once; refer back as needed.

---

## 1. What Cambium is, in one paragraph

Cambium is a Python 3.14 multi-agent coding-agent harness. It runs as an embeddable library (headless-first) with an optional TUI. A deterministic supervisor (`Custos`) manages N isolated worker processes (`Opifex`) over JSON-Lines-on-stdio IPC with `request_id` RPC framing. Workers run DSPy ReAct loops in private git worktrees. An LLM-driven orchestrator (`Architectus`) decomposes, routes, and evaluates. A serialized merge sequencer (`Unio`) fuses worker branches back. Read `docs/architecture.md` before changing anything non-trivial.

---

## 2. Repository layout

```
cambium/
├── agents.md                      ← you are here
├── docs/
│   ├── architecture.md            ← authoritative design (v2)
│   ├── system-design.md           ← v0.1 draft (superseded; read for history)
│   ├── reviews/                   ← adversarial reviews (cited from architecture.md)
│   └── module-template/           ← per-module template + reference example spec
├── src/cambium/                   ← implementation
│   └── modules/<name>/            ← one subdir per decision module
│       ├── architecture.md        ← per-module design
│       ├── decide.py              ← rule engine (primary) + the DSPy seam
│       ├── metric.py
│       ├── dataset.py
│       └── datasets/<name>_pairs.jsonl   ← v2 combined; train/eval/canaries split is v2.1
└── pyproject.toml
```

If a directory or file referenced here does not exist yet, it is planned (see `docs/architecture.md` §4) but not built. Do not invent it; ask.

---

## 3. Search before editing

Trace from entry points, not from filenames. Concrete starting points:

- **Public API surface:** `src/cambium/__init__.py` — `Cambium`, `Session`, `Result`, `Instance`, `Event`, `Config`.
- **IPC protocol:** `src/cambium/nuntius/` — message types and framing. Schema is normative in `docs/architecture.md` §5.2.
- **Supervisor:** `src/cambium/custos/` — lifecycle, restart, watchdog. Semantics normative in `docs/architecture.md` §7.
- **Worker entry:** `src/cambium/opifex/__main__.py` — read-init → ready → loop → result/exit.
- **Decision modules:** `src/cambium/modules/<name>/` — each module is self-contained (rule engine primary + DSPy seam in `decide.py`).

`nuntius`, `custos`, and `opifex` are **planned** directories — they do not exist in `src/cambium/` yet (only `supervisor.py`, `orchestrator.py`, `events.py`, and `modules/` are present). Read the architecture sections above for their intended shape; see the §2 note for what a missing path means.

When a `grep`/`rg` search fails to find what you expect, follow the execution path: read the import graph, the route registration, the message dispatcher. Don't conclude "doesn't exist" from a single miss.

---

## 4. Worktree workflow

- Every non-trivial change happens in an **isolated git worktree** off the relevant branch. The orchestrator (root agent) owns the integration worktree; child agents work in disjoint worktrees.
- Work in **disjoint file scopes** when running in parallel. Same-file concurrent edits require isolated worktrees and explicit merge sequencing.
- Commit **frequently** in your worktree. Small, well-described commits are easier to review and revert than large ones.
- **Worktree discipline guard:** before *any* commit, `git rev-parse --show-toplevel` must equal your worktree's path — verify with `git worktree list`. Never commit to `main`; the python314 incident did exactly that and its changes had to be untangled by hand.
- **No destructive git.** No `push --force`, no `rebase` of shared branches, no `reset --hard` of other agents' work. Amend only your own unpushed commit if asked.
- Clean up your own worktree when finished. The supervisor's `Surculus.prune()` is for runtime worktrees, not for your development worktrees.

---

## 5. Verification standards

**Run the narrowest check that catches your change.** Cite the exact command, working directory, and exit status when you report completion. A claim of "done" without verification is **UNVERIFIED**; mark it as such.

Standard checks (run from repo root unless noted):

- **Per-module unit tests:**
  ```
  python -m pytest src/cambium/modules/<name>/ -v
  ```
- **Module eval harness** (against frozen held-out set):
  ```
  python -m cambium.modules.<name>.eval
  ```
- **Integration smoke test** (fake LLM + 1 worker + 1 merge):
  ```
  python -m cambium.tests.smoke
  ```
- **Type / syntax gate:**
  ```
  python -m compileall src/cambium
  python -c "import cambium"
  ```

**Test hygiene** (on top of the checks above):

- Scenario/integration tests are the primary module tests (`tests/scenarios/test_<module>.py`); no TDD ceremony — write a test when it earns its place.
- Supervision tests use **fake workers**, not real ones.
- **No network in tests.** Anything that dials a provider is a manual or gated run.
- Harness code is **stdlib + git only**. `dspy` is an optional extra, lazy-imported, never a hard dependency.

Mark your report with one of:
- **VERIFIED** — command run, exit status 0, output cited.
- **UNVERIFIED** — claim made, check not run (state why: no interpreter, no fake LLM, out of scope, etc.).
- **BLOCKED** — check could not run due to external dependency; describe the blocker.

Do not say "done" when you mean UNVERIFIED. Do not say "tests pass" without citing the command.

---

## 6. Reporting norms

- **State what you observed**, with repository-relative paths and stable symbols. Cite line numbers when relevant.
- **Separate facts from inferences.** "The supervisor emits `worker_exit` on EOF" is a fact (cite the line). "The supervisor is therefore robust to zombie grandchildren" is an inference (justify it or test it).
- **A defect fix is done only with before/after verification.** "Unverified" if not run; "workaround" if the cause still exists.
- **Three-failure rule.** If three attempts at a fix fail, stop and report all three with evidence. Do not keep guessing. Each attempt must test a distinct hypothesis.
- **Empty reports are failures.** Every task ends with a substantial report: files changed, exact commands with their outputs, and the commit hash. Silent completion — and returning early without the deliverable — are failures, not results.
- **Snapshots are point-in-time.** A dump of a live system (DB, log, session state) needs an explicit as-of timestamp and the command that produced it; never present it as stable truth.
- **Use existing vocabulary.** Cambium, Custos, Opifex, Diffundo, worktree, generation, request_id, etc. Do not invent synonyms or new jargon. Module names match `docs/architecture.md` §4.
- **No new doc/report/summary files unless asked.** Say it in chat. `agents.md`, `docs/architecture.md`, and `docs/module-template/*` are the normative documents; do not proliferate.

---

## 7. Coding norms specific to Cambium

- **Stdlib + DSPy + git.** No new frameworks. Structured logging via stdlib `logging`. No `structlog`, no `loguru`, no `aiofiles` (use `asyncio.to_thread` or a writer thread).
- **No hidden global state.** Configuration flows through `Config` (frozen dataclass). Runtime state lives under `${session_dir}/.cambium/`. No module-level mutables, no process-global caches outside explicitly-owned ones (`Diffundo` cache is owned).
- **Flat over nested.** Early returns, guard clauses, exhaustive match/switch. Business logic in pure functions; state and I/O at the edges.
- **Concrete over abstract.** Inline unless a boundary is independently meaningful.
- **Real enums for domain alternatives.** `WorkerState`, `ResultStatus`, `EventKind` are enums, not strings or booleans.
- **Booleans are for predicates and API compatibility only.** Use enums for domain alternatives (`WorkerState.Running` vs `WorkerState.Crashed`, not `is_running=True`).
- **No `print()` in worker code or library code.** Use `logging`. The worker's stdout is reserved for the protocol.
- **No shell=True with user input.** Use list-form `subprocess.run`. `git_op` and `grep_code` enforce this.
- **API keys are env-only.** Never log them. Never put them in protocol messages. See `docs/architecture.md` §12.
- **Every disk write off the event loop.** Use `asyncio.to_thread` or a writer thread. See `docs/architecture.md` §6.2.
- **Module shape** (per `docs/module-template/*`): modules are pure JSON-in/JSON-out functions with strict JSON schemas, each with a CLI entry — `python -m cambium.modules.<name>` reads JSON from stdin, writes JSON to stdout. Modules depend on `Protocol`s (ports/adapters), never concrete providers; dependency injection happens at the root.
- **Engine swap is a strategy pattern.** The rule engine is the primary `decide` implementation today; a DSPy program implementing the same interface can replace it behind the seam without touching callers (v2.1 — `docs/research/dspy-python-314.md`; see `docs/module-template/architecture.md` §5.1/§5.3).
- **Durable state layout.** Event log and conversation store live in SQLite (WAL mode); low-level IPC is JSON-Lines. All session state sits under the dotted `.cambium/` dir — `docs/architecture.md` §16.2 is canonical on that naming.

---

## 8. Design norms

- **Task tree, not flat lists.** Decomposition produces a tree (DAG): nodes are sub-LLM sessions. A node's only contract is its `Result` envelope — a unified diff, summary, and metrics. A parent **never reads a child's scratchpad or reasoning**; steering goes downward by `session_id`, results flow upward as envelopes (design-deltas D2/D3).
- **Determinism split.** The LLM plans — it emits JSON arrays of sub-tasks. Deterministic supervisor code manages spawning, queues, and merges. The LLM never manages parallelism.
- **Let it crash.** Worker crashes are normal; the supervisor restarts from the last durable checkpoint. Do **not** write defensive spaghetti in workers: no `try/except` around LLM-output parsing — crash, and let the supervisor handle it.
- **Prompt structure for provider caching.** Static prefix (system prompt, `AGENTS.md`, guidelines) at the top; dynamic content (conversation history, repo state) at the bottom. Never put timestamps or request IDs at the top. There is **no local LLM cache** — provider-side caching only (supersedes `architecture.md` §8.1 cache design per design-deltas D1 — arch amendment pending); prompt structure exists to make provider caches hit, not as a correctness mechanism.
- **No sandboxing in the harness.** Containment = git worktree isolation + permission allowlists + approval gates (design-deltas D7). Workers are stdio processes — local today, a disposable container at deployment, and that is out of harness scope.
- **Canary gate.** Any metric or refinement change that degrades the canary score is **rejected** — the canary suite is the gate, not a suggestion (`docs/module-template/dataset-format.md` §6; design-deltas D5).

---

## 9. Where to look for what

| If you need to... | Read this |
|---|---|
| Understand the system end-to-end | `docs/architecture.md` §0–§7 |
| Understand an adopted design decision (delta over architecture v2) | `docs/research/design-deltas.md` (D1–D7) |
| Add or change a decision module | `docs/module-template/architecture.md`, then `docs/module-template/example-spec.md` |
| Add or change a dataset | `docs/module-template/dataset-format.md` |
| Add a new protocol message | `docs/architecture.md` §5.2, then `src/cambium/nuntius/` |
| Debug a worker crash / restart loop | `docs/architecture.md` §7 (Lifecycle), esp. §7.4–7.6 |
| Debug a merge failure | `docs/architecture.md` §4 (Unio), §7.8 |
| Understand an old design choice | `docs/system-design.md` (v0.1) + the three `docs/reviews/` |
| Find what to copy for a sandbox backend (out of v2 scope) | `src/cambium/septum/` + §4 (Septum) in architecture.md; removal rationale in design-deltas D7 |

---

## 10. What "done" means for a module

A module is **done** when **all** of the following hold:

1. Its `architecture.md` (per template) is committed.
2. Its datasets are committed with explicit schema/version markers. **v2:** a single `<name>_pairs.jsonl` with inline `canary: true` records (see `src/cambium/modules/example/`); **v2.1:** the `train.jsonl` / `eval.jsonl` / `canaries.jsonl` split per `docs/module-template/dataset-format.md`.
3. Its metric and eval harness run green over the full dataset (including canaries) — in v2, via the scenario test (§9 of `docs/module-template/architecture.md`).
4. Its unit tests pass.
5. The end-to-end smoke test passes with the module wired in.
6. An adversarial review has been committed under `docs/reviews/` (or an existing one updated and re-run).
7. The change has been verified (VERIFIED, not UNVERIFIED) per §5.

If any of these is missing, the module is **not done** — it is "in progress." State which step is missing and why.

---

## 11. Asking for help

Ask the orchestrator (root agent) when:
- Two equal-priority requirements conflict and evidence cannot decide.
- A choice is irreversible and you are uncertain (e.g., changing the IPC schema, removing a public API).
- You have failed three times on distinct hypotheses.
- You need access outside your assigned file scope.

Do **not** ask when:
- The answer is in `docs/architecture.md` or in this file.
- A test or grep would answer it.
- You are hedging out of caution rather than uncertainty.

Act, record the assumption, and continue.
