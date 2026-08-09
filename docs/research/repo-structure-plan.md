# Repo Structure Audit + Final Layout Plan

**Status:** Plan for reorg AFTER all `wt-*` branches merge. No files were moved in this audit.
**Date:** 2026-08-09
**Worktree audited:** `/tmp/opencode/cambium-hygiene` (branch `wt-hygiene`, == `main` tip `96da568`)
**Verified against:** all 22 `wt-*` branch trees via `git ls-tree` (read-only).

---

## 1. Verification of preliminary findings

Every orchestrator claim was re-run with commands against the audit worktree.

| Claim | Command | Result |
|---|---|---|
| No `cambium/cambium` nesting | `git ls-files \| grep -c '^src/cambium/cambium/'` | 0 matches. Only dir named `cambium` is `./src/cambium` (confirmed with `find . -type d -name cambium`). **Confirmed.** |
| Standard src-layout | `git ls-files` | All packages under `src/cambium/` (`src/cambium/{__init__.py,events.py,orchestrator.py,modules/}`). `pyproject.toml` sets `[tool.hatch.build.targets.wheel] packages = ["src/cambium"]`. **Confirmed.** |
| No TRACKED duplicate files | `git ls-files \| xargs -I{} basename {} \| sort \| uniq -d` | Only duplicate basename among the 30 tracked files: `__init__.py` (intentional — Python packages). **Confirmed.** |
| Dups only inside gitignored dirs | `git status --porcelain --ignored` | No ignored files present on disk in this worktree (no `.venv/`, no `__pycache__/`). Nothing tracked under them. **Confirmed (empty).** |
| ~20 research docs | `git ls-tree -r --name-only <branch> -- docs/research/ \| sort -u` | **Corrected: 28.** `main` has 9; 19 single-commit branches each add one. The orchestrator's "~20" was an estimate of the partially-merged state. |
| Three architecture-ish docs | `git ls-tree -r --name-only wt-arch`, `main` | Exactly three: `docs/architecture.md` (canonical, wt-arch unmerged), `docs/module-template/architecture.md` (template, wt-arch), `src/cambium/modules/example/architecture.md` (per-module, merged on main). **Confirmed.** |
| `implementation-plan.md` transient at root | `head implementation-plan.md` | Line 1: `# Implementation Plan (TRANSIENT — delete when implementation is done)`. **Confirmed.** |

Additional verified facts:

- **Tracked file count:** `git ls-files | wc -l` → **30** (main / wt-hygiene). Post-merge union of all 22 branches → **63** unique tracked paths (this is the final tree, §3).
- **`.gitignore` coverage** (`git check-ignore -q` on simulated paths):
  - `.venv/x.py` → IGNORED; `__pycache__/y.py` → IGNORED; `.pytest_cache/z.txt` → IGNORED.
  - Also covered: `*.py[cod]`, `*.egg-info/`, `*.egg`, `build/`, `dist/`, `.ruff_cache/`, `.mypy_cache/`, `.coverage`, `htmlcov/`, `.cambium/`.
  - **No tracked junk:** zero tracked files under `.pytest_cache/`, `.venv/`, `__pycache__/`, or any cache dir.
- **`uv.lock` is intentional:** tracked, consistent with `pyproject.toml` (uv-based project; README documents `uv run`). Not junk.
- **Post-merge duplicate basenames:** across the union of all branch trees, only `architecture.md` (×3) and `__init__.py` (×5) share basenames. All intentional:
  - `docs/architecture.md` — canonical v2 architecture (authoritative; supersedes `system-design.md`).
  - `docs/module-template/architecture.md` — normative template.
  - `src/cambium/modules/example/architecture.md` — filled-in per-module instance, co-located with code.
  - `__init__.py` — Python package markers.

---

## 2. Audit of current state

### What is tracked (30 files on main)

```
.gitignore  README.md  implementation-plan.md  pyproject.toml  uv.lock
docs/research/                      9 historical evidence docs
docs/reviews/                       3 adversarial reviews
docs/system-design.md               v0.1 design draft
src/cambium/                        events.py, orchestrator.py, __init__.py
src/cambium/modules/                base.py, __init__.py
src/cambium/modules/example/        architecture.md, dataset.py, decide.py, metric.py, __init__.py
src/cambium/modules/example/datasets/example_pairs.jsonl
tests/scenarios/test_example_module.py
```

### What is junk

- **Tracked junk: none.** No caches, build artifacts, editor files, or secrets are tracked.
- **`implementation-plan.md` (root): transient, not junk.** Self-declared delete-on-completion (§1). Must be removed in the final cleanup step (§5, step 2). Until then it is the orchestrator's live tracker.
- **`uv.lock`:** intentional (§1). Keep tracked.

### Doc taxonomy

| Category | Location | Role | Post-merge count | Pruning? |
|---|---|---|---|---|
| Research (evidence) | `docs/research/` | Historical findings on competitors, tools, drafts of designs (`*-design.md`, `*-draft.md`, `*-report.md`) | 28 | **No — historical evidence, never pruned** |
| Reviews | `docs/reviews/` | Adversarial reviews of v0.1 design (evidence of the flaw→fix cycle) | 3 | No |
| Canonical architecture | `docs/architecture.md` | v2 authoritative spec (wt-arch; supersedes system-design.md) | 1 | No |
| Templates | `docs/module-template/` | Normative: architecture template, dataset-format, example-spec | 3 | No |
| Design draft | `docs/system-design.md` | v0.1 draft; superseded by architecture.md but referenced by per-module docs | 1 | No |
| Per-module | `src/cambium/modules/<name>/architecture.md` | Instance filled from the template, next to code | grows with modules | No |
| Agent orientation | `agents.md` (root, wt-arch) | Onboarding for agents landing in the repo | 1 | No |
| Transient | `implementation-plan.md` (root) | Orchestrator tracker | 1 | **Remove at end** |

**Pending merge inventory (verified):** 19 branches add exactly one research doc each; `wt-datasets` also adds `scripts/{check_dataset_v1,generate_should_decompose_v1}.py` and `datasets/{canaries,eval,train}.jsonl` + `meta.json`; `wt-slice` also adds `scripts/fake_worker.py`, `src/cambium/supervisor.py`, `tests/scenarios/test_vertical_slice.py`; `wt-arch` adds `agents.md`, `docs/architecture.md`, `docs/module-template/{architecture,dataset-format,example-spec}.md`. `wt-deltas2` and `wt-fb2` have no commits beyond `main`.

---

## 3. Proposed final layout (after all merges)

63 tracked files. The tree needs **no structural moves** — every merged file already lands in its rule-compliant location. The reorg is verification + transient removal + README/.gitignore polish (§5).

```
cambium/
├── .gitignore
├── README.md                        # pointers to docs/ (see §5 step 5)
├── agents.md                        # (wt-arch) agent orientation
├── pyproject.toml
├── uv.lock                          # intentional, tracked
├── implementation-plan.md           # TRANSIENT — removed at end (rule d)
├── docs/
│   ├── architecture.md              # canonical v2 (wt-arch) — rule (a)
│   ├── system-design.md             # v0.1 draft, superseded; kept as origin record
│   ├── module-template/             # normative templates (wt-arch) — rule (a)
│   │   ├── architecture.md
│   │   ├── dataset-format.md
│   │   └── example-spec.md
│   ├── research/                    # 28 historical docs, no pruning — rule (b)
│   │   ├── bench-harness-design.md
│   │   ├── cascade-design.md
│   │   ├── cloud-code.md
│   │   ├── codex.md
│   │   ├── custos-asyncio-design.md
│   │   ├── dspy-python-314.md
│   │   ├── event-schema-draft.md
│   │   ├── example-datasets-v1.md
│   │   ├── ipc-protocol-draft.md
│   │   ├── logging-design.md
│   │   ├── metric-design.md
│   │   ├── omp.md
│   │   ├── onboarding-checklist-draft.md
│   │   ├── opencode.md
│   │   ├── pi.md
│   │   ├── prime-agent.md
│   │   ├── provider-landscape.md
│   │   ├── pydev.md
│   │   ├── python-3.14.md
│   │   ├── replay-restart-design.md
│   │   ├── sandbox-options.md
│   │   ├── sqlite-wal-durability.md
│   │   ├── test-strategy.md
│   │   ├── threat-model.md
│   │   ├── tui-best-practices.md
│   │   ├── vertical-slice-report.md
│   │   ├── worker-coldstart.md
│   │   └── worktree-concurrency.md
│   └── reviews/                     # evidence, kept
│       ├── review-distributed-systems.md
│       ├── review-implementation.md
│       └── review-llm-design.md
├── scripts/                         # repo tooling (wt-datasets, wt-slice)
│   ├── check_dataset_v1.py
│   ├── fake_worker.py
│   └── generate_should_decompose_v1.py
├── src/cambium/
│   ├── __init__.py
│   ├── events.py
│   ├── orchestrator.py
│   ├── supervisor.py                # (wt-slice)
│   └── modules/
│       ├── __init__.py
│       ├── base.py
│       └── example/
│           ├── __init__.py
│           ├── architecture.md      # per-module doc next to code — rule (c)
│           ├── dataset.py
│           ├── datasets/
│           │   ├── canaries.jsonl   # (wt-datasets)
│           │   ├── eval.jsonl       # (wt-datasets)
│           │   ├── example_pairs.jsonl
│           │   ├── meta.json        # (wt-datasets)
│           │   └── train.jsonl      # (wt-datasets)
│           ├── decide.py
│           └── metric.py
└── tests/
    └── scenarios/
        ├── test_example_module.py
        └── test_vertical_slice.py   # (wt-slice)
```

**Rules enforced (from brief):**
- (a) Canonical architecture: `docs/architecture.md` + `docs/module-template/*`.
- (b) Research docs stay in `docs/research/` — historical evidence, no pruning.
- (c) Per-module docs live next to code: `src/cambium/modules/<name>/architecture.md`.
- (d) Transient `implementation-plan.md` marked TRANSIENT now, removed at end of reorg.
- (e) `.gitignore` completeness: see §5 step 4.
- (f) README pointers: see §5 step 5.

**Open layout questions (resolved):**
- `docs/system-design.md` **stays in `docs/`** (not moved to `docs/research/`). It is an internal design draft, not external-tool research; it is referenced by `src/cambium/modules/example/architecture.md` (§M9) and by the README. It is superseded by `docs/architecture.md` (which states "Where it conflicts, this document wins") but kept as the v0.1 origin record. Update the README to mark it superseded (§5 step 5).
- `agents.md` stays at **repo root** (matches the `AGENTS.md`-at-root convention; it is orientation, not architecture).

---

## 4. Naming conventions

| Kind | Convention | Current state |
|---|---|---|
| Doc files | kebab-case, `.md` | Compliant everywhere: `cloud-code.md`, `system-design.md`, `tui-best-practices.md`, `event-schema-draft.md`, `example-datasets-v1.md`. Reviews use prefix `review-<topic>.md`. Research docs use `<topic>.md` with `-design`/`-draft`/`-report` suffixes describing doc type — **keep all 28 as merged** (historical; renaming adds noise). |
| Python modules | snake_case | Compliant: `events.py`, `orchestrator.py`, `supervisor.py`, `base.py`, `dataset.py`, `decide.py`, `metric.py`. |
| Packages | snake_case, no `__` prefix | `src/cambium/`, `modules/`, `modules/example/`. |
| Test files | `test_<target>.py` | `test_example_module.py`, `test_vertical_slice.py`. |
| Test dirs | `tests/scenarios/` for end-to-end; future unit dirs `tests/<area>/` | Compliant. |
| Datasets | snake_case jsonl under the owning module's `datasets/`: `train.jsonl`, `eval.jsonl`, `canaries.jsonl`, `meta.json`, `example_pairs.jsonl` | Compliant. |
| Scripts | snake_case under `scripts/` | Compliant: `fake_worker.py`, `check_dataset_v1.py`. |
| Branches / worktrees | `wt-<name>` / `/tmp/opencode/cambium-<name>` | Compliant (23 `wt-*` branches, incl. this audit worktree). |

**Forward rule:** new docs use kebab-case with a type suffix (`-design`, `-draft`, `-spec`); new per-module docs always use the exact filename `architecture.md` filled from `docs/module-template/architecture.md`. Never prefix a doc with `v0.1`/`v1` style — version lives in the doc front-matter, not the filename.

---

## 5. Reorg checklist (run AFTER all `wt-*` branches merge; verify from `main`)

Size: **10 steps, 16 git commands** (excluding the post-merge integration run). Ordered so no step conflicts with a pending merge — the only step that could ever conflict (`wt-arch` resolution) is explicitly a merge-phase concern, flagged in step 0, not part of the moves.

**Step 0 — Precondition (merge phase, not reorg).**
Merge all `wt-*` branches into `main`. **Flagged risk:** `wt-arch` branched from the initial commit `a0fc528` (which contained only `docs/reviews/` + `docs/system-design.md`). Relative to `main`, `wt-arch` looks like it deletes `.gitignore`, `README.md`, `pyproject.toml`, `src/`, `uv.lock`, `implementation-plan.md`, `docs/research/*`. The merge needs explicit conflict resolution: **keep `main`'s versions** of `.gitignore`, `README.md`, `pyproject.toml`, `src/`, `uv.lock`, `implementation-plan.md`, `docs/research/*`; **take** `wt-arch`'s `agents.md`, `docs/architecture.md`, `docs/module-template/*`. Merge `wt-arch` before running step 1.

```sh
# only if any research doc landed at docs/ root instead of docs/research/ (check first):
git ls-files docs/ | sort
git mv docs/foo.md docs/research/foo.md        # if misplaced (none expected)
```

**Step 1 — Clean-tree + baseline check.**
```sh
git status --porcelain                          # expect empty
git ls-files | wc -l                            # expect 63
git branch | grep '^wt-'                        # expect none left (all merged+deleted)
```

**Step 2 — Remove transient tracker (rule d).** Record its final status first (orchestrator): update it to "implementation done" and commit, then delete.
```sh
git rm implementation-plan.md
git commit -m "chore: remove transient implementation-plan.md (implementation done)"
```

**Step 3 — Confirm doc placement; no moves required.** All 28 research docs in `docs/research/`, 3 reviews in `docs/reviews/`, canonical + templates in place, per-module doc co-located.
```sh
git ls-files docs/ src/cambium/modules/ | sort
```
Expected: every `*.md` is under `docs/{research,reviews,module-template}/`, `docs/architecture.md`, `docs/system-design.md`, or `src/cambium/modules/example/architecture.md`. Fix any straggler with `git mv` (none expected).

**Step 4 — `.gitignore` hardening (rule e).** Append (idempotent — verify with `grep` before adding):
```sh
printf '\n# Secrets\n.env\n.env.*\n\n# Editor / OS\n.DS_Store\n*.swp\n*~\n\n# Logs\n*.log\n' >> .gitignore
git add .gitignore && git commit -m "chore: harden .gitignore (.env, editor/OS, logs)"
```
Existing coverage is already complete for Python + uv + pytest (`__pycache__/`, `*.py[cod]`, `.venv/`, `.pytest_cache/`, `.ruff_cache/`, `.mypy_cache/`, `.coverage`, `htmlcov/`, `build/`, `dist/`, `.egg-info/`, `.cambium/`). The additions cover secrets (architecture mandates env-only secrets) and cross-platform noise.

**Step 5 — README pointer update (rule f).** Current README already cites `docs/system-design.md` and `docs/architecture.md` (both exist post-merge). Edit to:
- keep `docs/architecture.md` as the authoritative pointer;
- mark `docs/system-design.md` as the superseded v0.1 draft;
- add pointers to `docs/module-template/`, `docs/research/` (briefly — historical evidence), `docs/reviews/`, `agents.md`, and the per-module pattern `src/cambium/modules/example/architecture.md`.
```sh
git add README.md && git commit -m "docs: point README at canonical architecture and docs tree"
```

**Step 6 — Final verification sweep.**
```sh
git status --porcelain                          # clean
git ls-files | xargs -I{} basename {} | sort | uniq -d   # only __init__.py + architecture.md
git ls-files | grep -iE '\.(pyc|cache|venv|DS_Store|env)$|/\.venv/'   # empty
git check-ignore -q .venv/x.py __pycache__/y.py .pytest_cache/z.txt && echo IGNORED
```

**Step 7 — Integration verification.** Run after all merges + reorg, on `main`:
```sh
uv run --python 3.14.7 --extra test pytest -q
```

**Step 8 — Delete merged branch refs and worktrees** (only after all merges verified):
```sh
for b in $(git branch --format='%(refname:short)' | grep '^wt-'); do
  git branch -d "$b"   # -D if merges were squash/rebase, after confirming content in main
done
git worktree prune
```

**Step 9 — Final commit + report.**
```sh
git log --oneline -5
```

**Step 10 — Post-reorg follow-up (deferred decisions, not part of this plan):** re-check whether `src/cambium/modules/example/datasets/example_pairs.jsonl` is still referenced by code/tests after the `wt-datasets` merge (see UNVERIFIED 5); if dead, remove it in a separate hygiene commit.

---

## 6. UNVERIFIED flags

1. **`wt-arch` merge outcome** — the conflict-resolution recommendations in step 0 are inferred from `git ls-tree`/`git diff` (branched from `a0fc528`, its tree lacks all files main added). The actual merge was **not run** (this audit is docs-only and the merge is pending). Verify the resolution produces the 63-file tree before step 1.
2. **`example_pairs.jsonl` post-merge fate** — `wt-datasets` adds `{train,eval,canaries}.jsonl` + `meta.json` without deleting `example_pairs.jsonl`. Whether code/tests still consume `example_pairs.jsonl` after merge was **not checked** (needs the merged tree; `wt-datasets` test/code state on a branch). Flag for step 10.
3. **Orchestrator's "~20 research docs"** — corrected to 28 (verified via branch-tree union). The discrepancy is the partially-merged snapshot the orchestrator saw; no action needed.
4. **Post-merge file count (63)** — computed as the `sort -u` union of all 22 branch trees. Assumes every branch merges unchanged and `wt-arch`'s deletes resolve to "keep main's files". If any branch is amended before merge, the count shifts. Re-run step 1's `git ls-files | wc -l` on the real merged `main` and trust that number.
5. **`agents.md` placement** — kept at root per `AGENTS.md`-at-root convention. Not an explicit rule in the brief; if the orchestrator prefers `docs/`, it is a one-line `git mv` addition to step 5.
6. **README wording** — the exact edits in step 5 were not written into the README (docs-only audit; README edit runs post-merge). Step 5 states the required content, not the final prose.
7. **Tests not run** — no `pytest` was executed during this audit (no code changed). Test state on `main` is unknown; step 7 covers it post-merge.

---

## Appendix — commands used (for reproducibility)

```sh
git ls-files | sort                          # tracked files
git ls-files | wc -l                         # 30
git ls-files | xargs -I{} basename {} | sort | uniq -d      # __init__.py only
git status --porcelain --ignored             # no untracked/ignored files on disk
git check-ignore -q .venv/x.py __pycache__/y.py .pytest_cache/z.txt && echo IGNORED
for b in $(git branch --format='%(refname:short)' | grep '^wt-'); do
  git ls-tree -r --name-only $b -- docs/; done | sort -u    # doc placement per branch
for b in ...; do git ls-tree -r --name-only $b; done | sort -u | wc -l   # 63 post-merge
git diff --name-status main wt-arch           # wt-arch add/delete inventory
git merge-base wt-arch main                   # a0fc528 (init commit)
```
