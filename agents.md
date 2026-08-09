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
- **Supervisor:** `src/cambium/custos/` — lifecycle, restart, watchdog. Semantics normative in §7.
- **Worker entry:** `src/cambium/opifex/__main__.py` — read-init → ready → loop → result/exit.
- **Decision modules:** `src/cambium/modules/<name>/` — each module is self-contained (rule engine primary + DSPy seam in `decide.py`).

When a `grep`/`rg` search fails to find what you expect, follow the execution path: read the import graph, the route registration, the message dispatcher. Don't conclude "doesn't exist" from a single miss.

---

## 4. Worktree workflow

- Every non-trivial change happens in an **isolated git worktree** off the relevant branch. The orchestrator (root agent) owns the integration worktree; child agents work in disjoint worktrees.
- Work in **disjoint file scopes** when running in parallel. Same-file concurrent edits require isolated worktrees and explicit merge sequencing.
- Commit **frequently** in your worktree. Small, well-described commits are easier to review and revert than large ones.
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
- **Use existing vocabulary.** Cambium, Custos, Opifex, Diffundo, worktree, generation, request_id, etc. Do not invent synonyms or new jargon. Module names match `docs/architecture.md` §4.
- **No new doc/report/summary files unless asked.** Say it in chat. `agents.md`, `docs/architecture.md`, and `docs/module-template/*` are the normative documents; do not proliferate.

---

## 7. Coding norms specific to Cambium

- **Stdlib + DSPy + git.** No new frameworks. Structured logging via stdlib `logging`. No `structlog`, no `loguru`, no `aiofiles` (use `asyncio.to_thread` or a writer thread).
- **No hidden global state.** Configuration flows through `Config` (frozen dataclass). Runtime state lives under `${session_dir}/.cambium/`. No module-level mutables, no process-global caches outside explicitly-owned ones (`Diffundo` cache is owned).
- **Flat over nested.** Early returns, guard clauses, exhaustive match/switch. Business logic in pure functions; state and I/O at the edges.
- **Concrete over abstract.** Inline unless a boundary is independently meaningful.
- **Real enums for domain alternatives.** `WorkerState`, `ResultStatus`, `EventKind` are enums, not strings or booleans.
- **Booleans are for predicates and API compatibility only.** Use enums for domain alternatives (`SandboxKind.Bwrap` vs `SandboxKind.SandboxExec` vs `SandboxKind.Noop`, not `is_linux=True`).
- **No `print()` in worker code or library code.** Use `logging`. The worker's stdout is reserved for the protocol.
- **No shell=True with user input.** Use list-form `subprocess.run`. `git_op` and `grep_code` enforce this.
- **API keys are env-only.** Never log them. Never put them in protocol messages. See `docs/architecture.md` §12.
- **Every disk write off the event loop.** Use `asyncio.to_thread` or a writer thread. See §6.2.

---

## 8. Where to look for what

| If you need to... | Read this |
|---|---|
| Understand the system end-to-end | `docs/architecture.md` §0–§7 |
| Add or change a decision module | `docs/module-template/architecture.md`, then `docs/module-template/example-spec.md` |
| Add or change a dataset | `docs/module-template/dataset-format.md` |
| Add a new protocol message | `docs/architecture.md` §5.2, then `src/cambium/nuntius/` |
| Debug a worker crash / restart loop | `docs/architecture.md` §7 (Lifecycle), esp. §7.4–7.6 |
| Debug a merge failure | `docs/architecture.md` §4 (Unio), §7.7 |
| Understand an old design choice | `docs/system-design.md` (v0.1) + the three `docs/reviews/` |
| Find what to copy for a new sandbox backend | `src/cambium/septum/` + §4 (Septum) in architecture.md |

---

## 9. What "done" means for a module

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

## 10. Asking for help

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
