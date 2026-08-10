# Repo Structure Audit + Final Layout Plan

**Status:** Historical snapshot of the reorg plan. No files were moved in this
audit; keep the layout decisions and provenance, but verify the tree before any
future cleanup.
**Date:** 2026-08-09
**Worktree audited:** `/tmp/opencode/cambium-hygiene` (branch `wt-hygiene`,
audit anchor `96da568`)
**Evidence source:** read-only `git ls-tree` checks over all 22 `wt-*` branch
trees. The later `main` anchor `6109a6a` is historical provenance, not a current
count or baseline.

---

## 1. Snapshot evidence

The audit established these facts in its snapshot:

- `git ls-files` showed a standard `src/cambium/` layout, with no nested
  `cambium/cambium`; `pyproject.toml` used Hatch's `src/cambium` package.
- The 30-file audit tree had no tracked cache, build, editor, or secret files;
  `uv.lock` was intentional. The union of the 22 branch trees contained 63
  unique paths.
- `git ls-files | xargs -I{} basename {} | sort | uniq -d` found only the
  intentional package `__init__.py` duplicate. The post-merge union also has
  the intentional `architecture.md` duplicates (canonical, template, and
  per-module instance).
- The branch-tree scan corrected the research-doc estimate to 28. The
  `implementation-plan.md` header marked it transient and the final cleanup
  step removes it.
- The snapshot also checked simulated ignore paths for `.venv/`,
  `__pycache__/`, and `.pytest_cache/`, plus `*.py[cod]`, `*.egg-info/`,
  `*.egg`, `build/`, `dist/`, `.ruff_cache/`, `.mypy_cache/`, `.coverage`,
  `htmlcov/`, and `.cambium/`. No tracked path was found under those
  directories. This is why `uv.lock` remains tracked and why ignore hardening
  is a small follow-up, not a repository move.

These are audit observations, not current-main inventory. Re-run the checks on
the checkout being changed.

---

## 2. Historical layout decisions

| Category | Location | Role | Decision |
|---|---|---|---|
| Research evidence | `docs/research/` | Historical findings and design drafts | Keep; do not prune evidence. |
| Reviews | `docs/reviews/` | Adversarial v0.1 design reviews | Keep. |
| Canonical architecture | `docs/architecture.md` | Authoritative v2 spec | Keep; supersedes `docs/system-design.md`. |
| Templates | `docs/module-template/` | Normative architecture, dataset, and module templates | Keep. |
| Design draft | `docs/system-design.md` | v0.1 origin record | Keep; mark superseded. |
| Per-module docs | `src/cambium/modules/<name>/architecture.md` | Template-filled module docs next to code | Keep and extend per module. |
| Agent orientation | `agents.md` | Root onboarding and vocabulary | Keep at repository root. |
| Transient tracker | `implementation-plan.md` | Orchestrator work tracker | Remove only after implementation is done. |

**Branch provenance:** `wt-arch` adds the architecture, templates, and
`agents.md`; `wt-datasets` adds dataset/scripts paths; `wt-slice` adds the
vertical-slice paths; `wt-deltas2` and `wt-fb2` add no commits beyond `main`.
The merge risk is recorded in step 0 below.

### Proposed historical tree

The tree below records the no-move layout decision from the audit. Filenames are
kept here even when later work changes their status.

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
│   ├── research/                    # historical docs, no pruning — rule (b)
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

**Rules retained from the brief:**

- (a) Canonical architecture is `docs/architecture.md` plus
  `docs/module-template/*`.
- (b) Research remains in `docs/research/` as historical evidence.
- (c) Per-module docs stay next to code.
- (d) `implementation-plan.md` is transient and is removed at the end.
- (e) `.gitignore` hardening is step 4; (f) README pointers are step 5.

**Resolved layout questions:** `docs/system-design.md` stays in `docs/` as an
internal v0.1 draft referenced by the per-module docs and README; the canonical
architecture supersedes it. `agents.md` stays at the repository root because it
is orientation, not architecture.

---

## 3. Naming and reorg checklist

Use kebab-case Markdown names, snake_case Python names, `test_<target>.py` test
names, and `src/cambium/modules/<name>/architecture.md` for new module docs.
Branches and worktrees follow the recorded `wt-<name>` convention.

Run this sequence only after all `wt-*` branches merge; the commands are the
historical plan, not evidence that they ran.

**Step 0 — Merge precondition.** Merge all `wt-*` branches into `main`. `wt-arch`
branched from `a0fc528`; keep `main`'s `.gitignore`, `README.md`, `pyproject.toml`,
`src/`, `uv.lock`, `implementation-plan.md`, and `docs/research/*`, while taking
`wt-arch`'s `agents.md`, `docs/architecture.md`, and
`docs/module-template/*`. Check misplaced research files with:

```sh
git ls-files docs/ | sort
git mv docs/foo.md docs/research/foo.md        # only if a file is misplaced
```

**Step 1 — Clean-tree and baseline.** On the merged checkout, expect a clean
status, no `wt-*` branches, and re-measure the tracked-file count; the audit's
63-path union is only a historical comparison.

```sh
git status --porcelain
git ls-files | wc -l
git branch | grep '^wt-'
```

**Step 2 — Remove the transient tracker.** Record final status, then remove and
commit `implementation-plan.md`.

```sh
git rm implementation-plan.md
git commit -m "chore: remove transient implementation-plan.md (implementation done)"
```

**Step 3 — Confirm placement.** Verify research, reviews, canonical architecture,
templates, and per-module docs with `git ls-files docs/ src/cambium/modules/`;
move only a genuine straggler.

**Step 4 — Harden `.gitignore`.** After checking existing entries, add env,
editor/OS, and log patterns, then commit the change.

```sh
printf '\n# Secrets\n.env\n.env.*\n\n# Editor / OS\n.DS_Store\n*.swp\n*~\n\n# Logs\n*.log\n' >> .gitignore
git add .gitignore && git commit -m "chore: harden .gitignore (.env, editor/OS, logs)"
```

**Step 5 — Update README pointers.** Keep `docs/architecture.md` authoritative;
mark `docs/system-design.md` superseded; point to `docs/module-template/`,
`docs/research/`, `docs/reviews/`, `agents.md`, and the per-module architecture
doc, then commit.

```sh
git add README.md && git commit -m "docs: point README at canonical architecture and docs tree"
```

**Step 6 — Final sweep.** Check status, duplicate basenames, tracked junk, and
`.gitignore` coverage (`.venv/x.py`, `__pycache__/y.py`, `.pytest_cache/z.txt`).
The expected duplicate-basename set is only package `__init__.py` and the
three intentional `architecture.md` paths.

**Step 7 — Integration check.** Run the full suite on the merged `main`:

```sh
uv run --python 3.14.7 --extra test pytest -q
```

**Step 8 — Cleanup refs.** Delete only verified merged `wt-*` branches and run
`git worktree prune`.

**Step 9 — Report.** Record the final `git log --oneline -5`.

**Step 10 — Deferred dataset check.** After the `wt-datasets` merge, verify
whether `src/cambium/modules/example/datasets/example_pairs.jsonl` is still
referenced; remove it only in a separate hygiene change if proven dead.

---

## 4. Unverified historical flags

1. `wt-arch` was not merged in this audit; verify conflict resolution and the
   resulting tree before step 1.
2. The post-merge fate of `example_pairs.jsonl` was not checked; use step 10.
3. The orchestrator's `~20` research-doc estimate was corrected to 28 in the
   branch-tree union.
4. The 63-path union assumes all branches merge unchanged; re-measure after
   merge.
5. Root placement of `agents.md` follows the `AGENTS.md` convention; moving it
   would be a separate one-line decision.
6. README wording was specified but not written during this audit.
7. Tests were not run during this docs-only audit; step 7 is the post-merge
   check.

---

## Appendix — recorded commands

```sh
git ls-files | sort
git ls-files | wc -l
git ls-files | xargs -I{} basename {} | sort | uniq -d
git status --porcelain --ignored
git check-ignore -q .venv/x.py __pycache__/y.py .pytest_cache/z.txt && echo IGNORED
git ls-tree -r --name-only wt-arch -- docs/
git diff --name-status main wt-arch
git merge-base wt-arch main                   # a0fc528
```
