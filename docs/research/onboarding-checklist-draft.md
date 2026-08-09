# Module-Onboarding Checklist — DRAFT

**Status:** DRAFT — 2026-08-09. Produced against `agents.md`, `docs/architecture/architecture.md` (v2.0.0),
and `docs/architecture/module-template/*`. The normative docs were originally read on branch `wt-arch`
(pending review) and are now **merged into `main`** (commit `18128a6`, "merge:
docs(architecture) v2"). They changed during review; the citations below were re-verified
against the merged `main` versions (see the template caveat).

**Purpose:** the exact steps an implementer (human or new AI agent) follows to add a new
Cambium decision module, from spec to merged and optimized.

**Normative sources** (all read at the time of writing):

| Source | Path | Says |
|---|---|---|
| Orientation + verification standards + "done" definition | `agents.md` | §4 worktrees, §5 VERIFIED/UNVERIFIED/BLOCKED, §7 coding norms, §9 what "done" means |
| Architecture, resolution matrix, optimization strategy | `docs/architecture/architecture.md` | §4 module catalog, §17 DSPy-per-module (pinned siblings), §18 resolution matrix (every CRITICAL flaw + mechanism) |
| Module template | `docs/architecture/module-template/architecture.md` | the 12-section per-module template |
| Dataset schema/versioning/canaries | `docs/architecture/module-template/dataset-format.md` | JSONL envelope, splits, versions, canary taxonomy |
| Reference example spec | `docs/architecture/module-template/example-spec.md` | the `should_decompose` module spec |
| Reference implementation | `src/cambium/modules/example/**` | `__init__.py`, `decide.py`, `dataset.py`, `metric.py`, `architecture.md`, `datasets/example_pairs.jsonl` |
| Reference scenario test | `src/cambium/modules/example/tests/test_example_module.py` | the per-module test gate |
| Python 3.14 verification | `docs/research/python-3.14.md` | CPython 3.14.7 regular build is the target; GIL present by default |

> **Template caveat.** `agents.md`, `docs/architecture/architecture.md` (v2.0.0), and
> `docs/architecture/module-template/*` are now in `main` (merged as commit `18128a6`); the reference
> module's split dataset and the dataset scripts were added by the `wt-datasets` merge
> (`fe160fd`). The templates changed during arch review — e.g. `program.py` → `decide.py`,
> `forward()` → `decide()`, and the `{train,eval,canaries}.jsonl` split re-labeled as the v2.1
> target with a single combined `<name>_pairs.jsonl` for v2. The citations below were
> re-checked against the merged `main` versions.

---

## Pre-flight (do this before touching any code)

1. Read `agents.md` in full. Note §5 (verification standards), §7 (coding norms: no hidden
   globals, no `print()`, enums not bools, no `shell=True` with user input, secrets env-only),
   and §9 (what "done" means for a module — your definition of done below maps to it).
2. Read `docs/architecture/architecture.md` §18 (the resolution matrix). Every module must not reintroduce
   a CRITICAL flaw already resolved there (LLM-C4 pinned-sibling optimization, LLM-C6 no
   do-not-decompose path, IMPL-M8 test strategy, DS-C6 hidden-state/fencing, etc.).
3. Read `docs/architecture/architecture.md` §17 (DSPy-per-module strategy): the §17.2 sibling-pinning table,
   §17.3 per-module artifact layout, and §17.4 the optimization loop. Your module's artifact
   layout and `architecture.md` §10 must match these.
4. Read `docs/architecture/module-template/architecture.md` (the normative template) and
   `docs/architecture/module-template/dataset-format.md` (normative dataset schema).
5. Read the reference module end-to-end, in this order:
   `src/cambium/modules/example/architecture.md` → `__init__.py` →
   `decide.py` → `metric.py` → `dataset.py` → `datasets/example_pairs.jsonl` →
   the colocated scenario test `src/cambium/modules/example/tests/test_example_module.py`. Also read
   `src/cambium/modules/base.py` (`Module`, `Example`, `DatasetLoader`, `DatasetError`).
6. Copy `docs/architecture/module-template/architecture.md` to `src/cambium/modules/<name>/architecture.md`
   and fill in every section. **Empty sections are not acceptable** — write "N/A — <reason>"
   if a section genuinely does not apply (template §1 note).
7. Assign a module code from the `docs/architecture/architecture.md` §4 catalog (or "new" if not listed)
   into the Module Identity table. If "new", you must also update the catalog in step 15.

Pre-flight is done when `src/cambium/modules/<name>/architecture.md` exists with every
section filled and the module code assigned.

---

## Steps 1–15

### Step 1 — Module spec (interfaces in/out, per template)

**What:** the interface contract, written into `src/cambium/modules/<name>/architecture.md` §3.

1. **Inputs** (§3.1): a typed frozen dataclass (or typed parameters to `decide()`). Untyped
   `dict` inputs are not permitted. For each field: source (which module/caller produces it),
   validation rules, and what happens on invalid input (raise / default / fallback — prefer
   raise). Reference: `TaskInput(task: str, context: str = "")`.
2. **Outputs** (§3.2): a typed frozen dataclass. For each field: consumer, invariants the
   consumer relies on, and JSON-serializability (the event log records it). Reference:
   `DecomposeOutput(decompose: bool, reason: str, confidence: float)`.
3. **Errors** (§3.3): typed exceptions, each caught at a named boundary. Module exceptions
   must **never** escape to the supervisor's event loop. Use enums for domain alternatives,
   not bools/strings (agents.md §7). `bool` is for genuine predicates only.
4. Declare the `Module` subclass: `name: str`, `async decide(input) -> Output`,
   `metric(example) -> float`. This is the DSPy seam — `decide` is the only surface a future
   program must implement (reference: `ShouldDecomposeModule`).
5. Write §4 State explicitly: if stateless, say so in one sentence (the reference does).

**Gate:** `uv run --python 3.14.7 python -c "import cambium"` still succeeds after scaffolding
`src/cambium/modules/<name>/` (exit 0 — VERIFIED in this repo on 2026-08-09).

### Step 2 — Dataset v1 (per `dataset-format.md`, stated minimums)

**What:** `src/cambium/modules/<name>/datasets/`, conforming to
`docs/architecture/module-template/dataset-format.md`.

1. **Container** (§1): JSONL, UTF-8, no BOM, one record per line, trailing newline, no
   trailing whitespace, no comments, records sorted lexicographically by `id`.
2. **Envelope** (§2): every record carries `id`, `schema_version`, `dataset_version`, `split`,
   `added_at`, `added_by`, `source`, `license`, `redacted`, and module-specific fields under
   `data`. `id` unique within the module's dataset dir; duplicates are a hard loader error.
3. **Layout and minimums** — two permitted shapes; declare which in `architecture.md` §7:

   | Layout | Files | Minimum sizes (state these in §7) | Freeze |
   |---|---|---|---|
   | **v2.1 split (normative target, `dataset-format.md` §4)** | `datasets/train.jsonl`, `datasets/eval.jsonl`, `datasets/canaries.jsonl` + sidecar `datasets/meta.json` | train **≥ 200**, eval **≥ 50** (held-out), canaries **≥ 15** (defaults; the reference `should_decompose` split ships 10 canaries as a justified override — see `scripts/check_dataset_v1.py`) | eval + canaries frozen (`meta.json` `*_frozen_at`) |
   | **v2 single-file interim (reference shape, `example-spec.md` §7.1)** | `datasets/<name>_pairs.jsonl`, canaries inline with `"canary": true` | **≥ 8 records; ≥ 1 canary (test gate)** — `test_<name>_module.py` asserts `len(canaries) >= 1` — and **≥ 2 canaries recommended** (the reference ships 9 records / 2 canaries) | canaries frozen by test assertion, not sidecar |

   The v2.1 split targets are defaults and may be overridden only with justification in
   `architecture.md` §7. Migration from single-file to split is a `dataset_version` major bump.
4. **Splits** (§4): no record exists in two splits (cross-split leak is a hard loader error);
   partitions are deterministic (seeded shuffle, documented per module).
5. **Canaries** (§6): use the taxonomy table (`trivially_atomic`, `must_decompose`,
   `ambiguous_calibration`, `format_only_hack`, `keyword_hack`, + module-specific kinds). Each
   canary carries a `canary` object with `kind`, `anti_expected`, and a `description` of the
   gaming behavior it traps. **Canaries must be deliberately misaligned with your engine's
   surface heuristics** (the reference's two canaries are the model).
6. **Hygiene** (§7): no secrets (loader refuses `sk-...`, `AIza...`, `ghp_...`), no PII,
   `redacted: true` + `redaction_notes` on scrubbed records, `license` on every record,
   mixed licenses not permitted in one file.

**Gate:** dataset loads through your `DatasetLoader` subclass with no `DatasetError`; the
scenario test's load-and-validate case passes (see Step 5).

### Step 3 — Metric + baseline

**What:** `metric.py` — a function `(example-with-prediction) -> float in [0, 1]`, plus the
baseline number it implies. Reference: `should_decompose_metric` (exact match on `decompose`).

1. Must be computable **without human-in-the-loop scoring** for the automatic optimization
   path (template §6). An LLM-as-judge signal is allowed only if the judge is itself evaluated
   against a human-graded held-out subset.
2. Document in `architecture.md` §6: each signal's weight and why, a **gameability analysis**
   (what does this metric reward that we do not want?), and which canaries detect each
   gameable mode.
3. State the module's eval **threshold** (template §9.2 default 0.75; the reference asserts
   1.0 exact-match over its own dataset).
4. **Baseline:** run the pure-python engine (Step 4) over the frozen held-out set and record
   the mean metric + per-signal breakdown. This is the bar a future DSPy variant must beat
   (`example-spec.md` §10 "Baseline"): if the DSPy variant does not beat the rule engine on
   the held-out set, the rule engine stays in production.

**Gate:** metric returns a float in [0,1] for every loaded example (including canaries) and
0.0 for unprocessed examples.

### Step 4 — Pure-python engine first; DSPy seam later

**What:** `decide.py` — a deterministic, pure-function rule engine as the v2 primary. The
reference engine is ~140 LOC with no LLM; **no `dspy` runtime dependency in v2** (recorded
decision in `implementation-plan.md`: "No dspy runtime dep in scaffold (heavy; seam
documented)").

1. Implement `decide(input) -> Output` as a pure function of the input (reference:
   `should_decompose(task, context)` accumulates evidence signals; stateless across calls).
2. Document the **DSPy seam** in `architecture.md` §5: the signature, the module class shape,
   LLM access routed through `CambiumLM`/`Diffundo` (never a bare `dspy.LM`), and the
   determinism policy (temperature/seed). The seam means a future program replaces the engine
   behind `decide` without touching callers, the dataset, or the metric.
3. Respect agents.md §7: stateless across calls (no module-level mutables), business logic in
   pure functions, no `print()` (use `logging`), no hidden global state, config via frozen
   dataclasses.

**Gate:** `python -c "import cambium"` succeeds and the rule engine scores 1.0 (or ≥ the
declared threshold) on the full dataset including canaries.

### Step 5 — Scenario tests (minimum set)

**What:** `src/cambium/modules/<name>/tests/test_<name>_module.py`, no mocking, no network
(reference: `src/cambium/modules/example/tests/test_example_module.py`). Minimum test set:

1. **Load and validate** the real dataset; assert schema validity; include a negative case
   (malformed record raises `DatasetError`).
2. **Aggregate metric**: run `decide()` over every record, attach predictions, score with
   `metric()`, assert the aggregate meets the threshold (reference asserts **1.0** — the
   engine perfectly fits its own dataset).
3. **Canary coverage**: assert canaries are present **and** were processed (a prediction was
   attached to each). This catches the "drop canaries to inflate the metric" reward-hacking
   path.
4. **Boundary conditions**: empty input, max-length input, unicode (template §9.1).
5. **Determinism**: same input → same output (trivially true for the rule engine; asserted
   anyway so the DSPy replacement keeps the contract).

**Gate:**

```
uv run --python 3.14.7 --extra test pytest src/cambium/modules/<name>/tests/test_<name>_module.py -v
```

Exit 0. (VERIFIED for the reference: `src/cambium/modules/example/tests/test_example_module.py` — 6 passed on
2026-08-09.)

### Step 6 — Verify on Python 3.14.7 (exact commands)

Run from the repo root, on CPython 3.14.7 (regular GIL build; see `docs/research/python-3.14.md`):

```
uv run --python 3.14.7 --extra test pytest src/cambium/modules/<name>/tests/test_<name>_module.py -v   # per-module gate
uv run --python 3.14.7 --extra test pytest -q                                          # whole suite
uv run --python 3.14.7 python -m compileall src/cambium                                # syntax gate
uv run --python 3.14.7 python -c "import cambium"                                      # import gate
```

Planned-but-not-yet-built (v2.1 per `example-spec.md` §9.2–9.4; **UNVERIFIED** until they
exist on `main`):

```
uv run --python 3.14.7 python -m cambium.modules.<name>.eval                  # frozen held-out eval
uv run --python 3.14.7 python -m cambium.modules.<name>.eval --suite canaries # canary suite; any failure → non-zero
uv run --python 3.14.7 python -m cambium.tests.smoke                          # e2e smoke, once the module is wired in
```

Record the exact command, working directory, and exit status for every check you actually run.

### Step 7 — Self-review against `agents.md` verification standards

1. Mark every claim in your report with **VERIFIED** (command run, exit 0, output cited),
   **UNVERIFIED** (claim made, check not run — state why), or **BLOCKED** (external
   dependency) per agents.md §5.
2. Do not say "done" when you mean UNVERIFIED; do not say "tests pass" without citing the
   command.
3. Re-check the coding norms (agents.md §7): no module-level mutables, no `print()` in
   library/worker code, no `shell=True` with user input, no secrets in logs or protocol.
4. Check your module's §8 failure-modes table against reality: every mode listed must have a
   detection path that your tests exercise.

### Step 8 — Commit in the worktree

1. Work in an **isolated git worktree** (agents.md §4). You are already on `wt-<name>`.
2. Commit **frequently**; small well-described commits are easier to review and revert.
3. One commit must contain the whole module at a reviewable boundary: `architecture.md`,
   `decide.py`, `metric.py`, `dataset.py`, `datasets/*`, the scenario test, and the changelog.
4. No destructive git: no force-push, no rebase of shared branches, no `reset --hard` of
   other agents' work. Amend only your own unpushed commit if asked.
5. Match repo commit-message style (`docs(plan): …`, `merge: …`, `research(…): …`); a module
   lands as e.g. `feat(module): <name> v0.1.0 with dataset, metric, scenario test`.

**Gate:** `git status` shows no uncommitted module files; `git log --oneline` shows the
module commit on `wt-<name>`.

### Step 9 — Adversarial review gate

1. Have the module adversarially reviewed before merge (agents.md §9 item 6; arch §19.5
   "adversarial review gates"). A new module gets a review committed under `docs/architecture/reviews/`
   (shape of the three v0.1 reviews: `docs/architecture/reviews/review-{distributed-systems,llm-design,implementation}.md`),
   or an existing review is updated and re-run if this is an interface change to a live module.
2. Review scope must cover: interface contract, dataset integrity (canaries present, frozen
   splits, version fields), metric gameability, hidden state, and the verification claims in
   Step 7 (any UNVERIFIED item is a review finding).
3. A canary failure or an interface change sends the module back to the owning step; re-review
   after the fix.

### Step 10 — Merge

1. The orchestrator (root agent) owns merging. The implementer's job ends with a green,
   reviewed branch on `wt-<name>`.
2. Merges are sequential and serialized (Unio / single-writer `refs/heads/main`, arch §7.8);
   never merge past a red gate.
3. After merge, verify on `main`: `uv run --python 3.14.7 --extra test pytest -q` exit 0.

### Step 11 — Baseline metrics recorded

1. Record the baseline (Step 3.4) in `src/cambium/modules/<name>/architecture.md` §10
   (Optimization Plan): rule-engine mean metric + per-signal breakdown on the frozen held-out
   set, the eval threshold, and the exact eval command that produced it. Store the committed
   benchmark artifact at `src/cambium/modules/<name>/tests/baselines/baseline.json`.
2. In v2 (no `eval.py` yet) the baseline is the scenario-test aggregate — the reference
   records "1.0 exact-match over the full dataset" as its baseline.
3. A future DSPy variant must beat this baseline on the held-out set, or the rule engine
   stays in production (`example-spec.md` §10).

### Step 12 — DSPy optimization loop per module (hill-climb with pinned siblings)

Only once the module is merged and has a frozen dataset. Follow `docs/architecture/architecture.md` §17.4:

1. Pick module **M**; load its current production version **M_v**.
2. Load the **pinned siblings** declared in `siblings-stub.yaml` (frozen references, NOT live
   co-adapted siblings — §17.2; LLM-C4). A module with no siblings ships an empty stub (the
   reference's is absent).
3. Load `train.jsonl`; run SIMBA (or GEPA) against `metric.py` → **M_v+1**.
4. Score M_v+1 on the **frozen** `eval.jsonl` (held-out).
5. Score M_v+1 on `canaries.jsonl` — **any canary regression → REJECT** (§17.4 steps 8–9 are
   the brakes the v0.1 flywheel lacked).
6. **Human gate:** promote to production only after a human (or human-authorized agent)
   reviews the prompt delta and signs off (template §10).
7. **Rollback:** previous production prompt retained under `optimized/<name>/v<N-1>/`;
   promotion is a symlink swap (template §10).
8. If M's interface changed, update `siblings-stub.yaml` / `sibling_pins` in the other
   modules (§17.4 step 10).
9. **Model pinning:** optimize against a single named model at `temperature=0.0`, not the
   cascade — avoids cross-model prompt transfer (`example-spec.md` §10; LLM-C3).
10. Modules are **per-module optimizable**, not jointly optimized (§17.5).

### Step 13 — Dataset versioning bump rules

Per `docs/architecture/module-template/dataset-format.md` §5, two orthogonal versions, both recorded in
`datasets/meta.json`:

- **`schema_version`** (integer): bump only on a backwards-incompatible change to the `data`
  shape. Migrations are pure functions (`migrate(record, from_v, to_v)`), tested, and
  committed alongside the bump.
- **`dataset_version`** (semver):
  - **Patch** (`1.0.0 → 1.0.1`): typo fixes, metadata-only changes, no label changes.
  - **Minor** (`1.0.0 → 1.1.0`): added records, added canaries. Frozen splits stay frozen.
  - **Major** (`1.0.0 → 2.0.0`): label changes, re-splits, schema bumps, frozen-set changes.
- `eval.jsonl` and `canaries.jsonl` are **immutable once frozen**; changing them requires a
  `dataset_version` bump and **re-running every module that pins this one**.
- `train.jsonl` may grow; deletions are not permitted (deprecate via `deprecated: true`).
- No record exists in two splits; duplicate ids are a hard error (loader contract, §9).
- The reference dataset is validated by **`scripts/check_dataset_v1.py`** (real, in `main`):
  asserts the three split files' counts (200/50/10), id sorting, envelope fields, secrets scan,
  class balance, no cross-split leaks or duplicate payloads, and runs every record through the
  real `ExampleDatasetLoader` + `ShouldDecomposeModule` asserting metric == 1.0. It does **not**
  write `meta.json`. Run (VERIFIED, exit 0, on `main` 2026-08-09):
  `uv run --python 3.14.7 python scripts/check_dataset_v1.py`
- The reference dataset generator is **`scripts/generate_should_decompose_v1.py`** — rule-based
  enumeration over `decide.py`'s evidence rules; it asserts engine-consistency before writing.
- The v1 scripts are `should_decompose`-specific. A **generic per-module** dataset checker
  (CI gate for any module's splits, including `meta.json` regeneration) is a **v2.1 target** —
  there is no `scripts/check_dataset.py` on `main`.
- **Two-reviewer rule** for frozen `eval.jsonl` changes; canary additions require sign-off from
  a reviewer who did not author the canary (§8).

### Step 14 — Changelog

1. Per-module changelog in `src/cambium/modules/<name>/architecture.md` §12:
   | Version | Date | Change | (semver of the module's primary implementation file `decide.py`, per template §1; a future DSPy replacement is versioned separately. The reference starts at 0.1.0/1.0.0).
2. Dataset changes are tracked by `dataset_version` bumps (§13) with the reason recorded in
   `architecture.md` §7.
3. Cross-cutting changes (new module code, new catalog row) are recorded in the repo-wide
   changelog / commit history, not just the module doc.

### Step 15 — Update `architecture.md` if interfaces changed

1. If the implemented interfaces differ from the spec written in Step 1, fix
   `src/cambium/modules/<name>/architecture.md` **before** merge — the template's §3
   interfaces are normative and the module is not done with a stale contract.
2. If a **cross-module contract** changed (a sibling consumes your new output shape), update
   the sibling's `architecture.md`, re-run its pinned eval, and re-review (arch §19.5).
3. If you assigned a **new module code**, add the row to the catalog in `docs/architecture/architecture.md`
   §4. The catalog is the normative interface contract; a module without a catalog row (or a
   "new" marker) is incomplete.
4. Re-check `siblings-stub.yaml` / `meta.json` `sibling_pins` in affected modules.

---

## Definition of done

A module is **done** only when **all** of the following checkboxes hold (mapping to
agents.md §9). Each item's verification command is given; run it and record
VERIFIED/UNVERIFIED/BLOCKED next to it.

- [ ] `src/cambium/modules/<name>/architecture.md` committed, filled per template (no empty
      sections). *Verify:* `ls src/cambium/modules/<name>/architecture.md`; grep the file for
      empty section bodies.
- [ ] Datasets committed with explicit version fields (`schema_version`, `dataset_version`,
      `split`) and — for the split layout — `datasets/meta.json`. *Verify:*
      `uv run --python 3.14.7 python -m cambium.modules.<name>.eval` (or, in v2, the scenario
      test's load-and-validate case) exits 0.
- [ ] Metric + eval harness run green on the **frozen held-out set** at ≥ the declared
      threshold. *Verify:* `uv run --python 3.14.7 python -m cambium.modules.<name>.eval` exit 0
      (v2.1; in v2 the scenario aggregate asserts 1.0).
- [ ] Canary suite passes **100%**. *Verify:*
      `uv run --python 3.14.7 python -m cambium.modules.<name>.eval --suite canaries` exit 0
      (v2.1), or the scenario test's canary-coverage assertion (v2).
- [ ] Colocated module tests pass. *Verify:* `uv run --python 3.14.7 --extra test pytest
      src/cambium/modules/<name>/tests/test_<name>_module.py -v` exit 0.
- [ ] Whole suite passes. *Verify:* `uv run --python 3.14.7 --extra test pytest -q` exit 0.
- [ ] Syntax + import gates pass. *Verify:*
      `uv run --python 3.14.7 python -m compileall src/cambium` and
      `uv run --python 3.14.7 python -c "import cambium"` exit 0.
- [ ] End-to-end smoke test passes with the module wired in (or a justified deferral is
      recorded). *Verify:* `uv run --python 3.14.7 python -m cambium.tests.smoke` exit 0.
- [ ] Adversarial review committed under `docs/architecture/reviews/` (or an existing review updated and
      re-run). *Verify:* `ls docs/architecture/reviews/*<name>*`.
- [ ] Baseline metrics recorded in `architecture.md` §10 with the exact eval command.
- [ ] Every claim in the report is marked VERIFIED (not UNVERIFIED) per agents.md §5, with
      command + working directory + exit status.
- [ ] Module committed in the worktree (`git status` clean for module files) and merged by the
      orchestrator. *Verify:* `git log --oneline -5` shows the module commit.

If any box is unchecked, the module is **not done** — it is "in progress." State which step
is missing and why (agents.md §9).

---

## Anti-patterns (what makes modules fail)

1. **Silent fallbacks / catch-all paths.** `except Exception: return default` hides the
   failure and the causal chain. Module errors are typed and caught at a named boundary
   (template §3.3); they never escape to the supervisor's event loop. A workaround is
   reported as a workaround, never as a solution.
2. **Hidden global state.** Module-level mutables, counters, or process-global caches break
   parallel `decide()` calls and reproducible eval. The module is stateless across calls; all
   configuration flows through frozen dataclasses; runtime state lives under the session dir
   (agents.md §7, arch §19.6).
3. **Tests without canaries — or that drop them.** A test that filters canaries out, or that
   asserts only a train aggregate, re-enables reward hacking. The scenario test must assert
   canaries are present **and** processed; canary pass rate is the promotion gate (§17.4).
4. **Unverified "done" claims.** "Tests pass" without the command, working directory, and exit
   status is UNVERIFIED (agents.md §5). Mark every claim; a report full of UNVERIFIED items
   is a review finding, not a handoff.
5. **Surface-memorizing datasets.** A metric/prompt that memorizes the engine's keyword
   heuristics scores 1.0 on train but fails canaries. Canaries must be deliberately
   misaligned with the surface heuristics (the reference's keyword-dense-but-atomic and
   keyword-free-but-parallel canaries are the model).
6. **Moving-target optimization (no sibling pins).** Optimizing a module against live
   co-adapted siblings is LLM-C4; the metric moves underneath the optimizer. Always optimize
   against **pinned stub siblings** (`siblings-stub.yaml`, §17.2); never jointly optimize
   (§17.5).
7. **Unfrozen or quietly-version-bumped eval sets.** Mutating `eval.jsonl`/`canaries.jsonl`
   without a `dataset_version` major bump and re-running pinned modules silently corrupts the
   held-out gate. Frozen means frozen; versioning rules in §13 are mandatory.
8. **Interface drift without catalog/review update.** Changing a module's contract (input
   shape, output shape, error types) without updating `architecture.md` §3, the `docs/architecture/architecture.md`
   §4 catalog, and re-reviewing cross-module consumers breaks the normative interface
   contract. A stale `architecture.md` is an incomplete module.
