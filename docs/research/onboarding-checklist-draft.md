# Module-Onboarding Checklist — DRAFT

**Status: HISTORICAL/SNAPSHOT (2026-08-09).** This is a compressed process
draft, not a report of a completed module or current-main test count/SHA. The
normative targets are `agents.md`,
`docs/architecture/module-template/architecture.md`, and
`docs/architecture/module-template/dataset-format.md`. The live reference is
`src/cambium/modules/example/`; it has no production `Architectus` caller.

## Source map

| Source | Use |
|---|---|
| `agents.md` | worktrees, verification states, coding rules, definition of done |
| `docs/architecture/architecture.md` | module catalog, sibling pins, optimization |
| `docs/architecture/module-template/architecture.md` | normative module sections |
| `docs/architecture/module-template/dataset-format.md` | JSONL envelope, splits, versions, canaries |
| `docs/architecture/module-template/example-spec.md` | `should_decompose` reference contract |
| `src/cambium/modules/example/**` | live `decide`, loader, metric, CLI, datasets, tests |
| `src/cambium/module_conformance.py` | live conformance gate |
| `scripts/check_dataset_v1.py` | live example-specific dataset checker |

## Pre-flight

1. Read `agents.md` completely. Verify the assigned isolated worktree and
   branch before editing.
2. Read architecture §17–§18, then both normative module-template files.
3. Read the reference in order: `architecture.md`, `__init__.py`, `decide.py`,
   `metric.py`, `dataset.py`, datasets, and colocated tests. Also read
   `src/cambium/modules/base.py`.
4. Copy the template to `src/cambium/modules/<name>/architecture.md`, fill all
   sections, and assign a catalog code or `new`.

Pre-flight is complete only when the architecture document exists, has no
empty section, and states authority, caller, state, interfaces, data, metric,
failure modes, tests, and optimization.

## Steps 1–8: implementation gates

### 1. Interface and state

- Inputs are typed frozen dataclasses/parameters; no untyped `dict`.
- Record producer, validation, and invalid-input behavior for every field.
- Outputs are typed frozen dataclasses with consumer, invariants, and wire form.
- Use enums for domain alternatives; keep booleans only for predicates or wire
  compatibility.
- Define typed errors and the boundary that catches each. Do not use a catch-all
  fallback.
- Implement `Module.name`, `async decide(input) -> Output`, and
  `metric(example) -> float`. State explicitly whether the module is stateless.
- A decision package has `__main__.py`: one JSON object in, one JSON object plus
  newline out; reject unknown/duplicate fields, malformed JSON, and invalid
  input with exit 1; no provider/network access.

**Gate:** `uv run --python 3.14.7 python -c "import cambium"` exits 0.

### 2. Dataset v1

Use UTF-8 JSONL, no BOM/comments, one record per line, trailing newline, no
trailing whitespace, and sorted IDs. The envelope has `id`, integer
`schema_version`, semver `dataset_version`, `split`, `added_at`, `added_by`,
`source`, `license`, `redacted`, module fields, and optional `notes` ≤500 chars.
The implemented v1 reference keeps `input`/`expected` at top level; the
normative v2.1 `data` wrapper is a target, not something to infer in a v1
loader.

Choose and document one layout:

| Layout | Required files/minimum | Freeze |
|---|---|---|
| Split target | `train.jsonl` ≥200, `eval.jsonl` ≥50, `canaries.jsonl` ≥15, `meta.json` | eval/canaries frozen; exact digests |
| v2 interim | `<name>_pairs.jsonl`, ≥8 records and ≥1 canary | canaries asserted by tests |

No duplicate IDs or canonical cross-split inputs. Use deterministic, documented
partitioning. Canaries carry a boolean marker at the current wire boundary and
describe their `kind`, anti-expected behavior, and pass condition. Keep
`trivially_atomic`, `must_decompose`, `ambiguous_calibration`,
`format_only_hack`, `keyword_hack`, and justified module-specific kinds.
No secrets or unnecessary PII; preserve license and provenance.

**Gate:** the loader and scenario test load every record; malformed records
raise `DatasetError`.

### 3. Metric and baseline

Implement `(example-with-prediction) -> float in [0, 1]`, returning `0.0` when
unprocessed. It must be automatic; an LLM judge requires human-graded
calibration. Document signal weights, gameability, canaries, threshold, and
baseline command. The reference exact-match metric is
`should_decompose_metric`; it compares `Decision` and scores canaries too.

**Gate:** every loaded record, including canaries, returns a finite score and
the aggregate reaches the declared threshold (reference: 1.0).

### 4. Pure engine and DSPy seam

Build the deterministic pure function first. Keep business logic pure,
stateless, and free of hidden globals or `print()`. Document a future DSPy
signature, `CambiumLM`/`Diffundo` routing, and deterministic settings; no DSPy
runtime dependency is required for v2. The seam replaces the engine behind
`decide` without changing callers, loader, dataset, or metric.

**Gate:** import succeeds and the engine meets the full-dataset metric gate.

### 5. Colocated scenarios

At minimum:

1. load the real dataset and validate schema, with a negative `DatasetError`;
2. run `decide` over every record and score the aggregate;
3. assert canaries are present and processed; and
4. cover a happy path, every failure mode, empty/max/unicode input, and
   determinism.

Shared runtime behavior belongs in `tests/scenarios/`; module tests belong in
`src/cambium/modules/<name>/tests/` and have no network or mocks.

### 6. Verification commands

Run from the repository root on Python 3.14.7 (commands are the recorded
workflow; this research snapshot does not claim they were run for a new
module):

```console
uv run --python 3.14.7 --extra test pytest src/cambium/modules/<name>/tests/test_<name>_module.py -v
uv run --python 3.14.7 --extra test pytest -q
uv run --python 3.14.7 python -m compileall src/cambium
uv run --python 3.14.7 python -c "import cambium"
```

For the reference, `python -m cambium.modules.example` supports direct,
`operation: decide`, and `operation: evaluate` requests. A standalone
`python -m cambium.modules.<name>.eval` and `--suite canaries` are v2.1 targets,
not current reference commands.

### 7. Review and commit

Before commit, run `git diff --check`, inspect the diff, stage only module
files, and leave the worktree clean. Record command, cwd, exit status, and
evidence as VERIFIED, UNVERIFIED, or BLOCKED. Obtain adversarial review of
interfaces, dataset integrity, metric gameability, state, and every unverified
claim. The root agent owns merge; do not merge another branch in a child
worktree.

### 8. Conformance and distribution

Run the live gate:

```console
PYTHONPATH=src python3.14 -m cambium.cli module-test NAME
```

`module_conformance` validates tracked layout/manifest, dataset and baseline
schemas/digests, imports, JSON CLI, offline subprocess behavior, and colocated
tests. The wheel must include code, `__main__.py`, `architecture.md`, datasets,
`meta.json`, tests, and baseline. A module is removable by deleting the whole
package directory; shared scenarios remain.

## Dataset versioning and optimization

`schema_version` (integer) bumps only for incompatible shape changes and uses a
tested pure migration. `dataset_version` is semver: patch for metadata/typos,
minor for additions/canaries, major for labels, re-splits, schema, or frozen
set changes. Frozen eval/canaries require re-running pinned modules; train is
grow-only. The reference generator is
`scripts/generate_should_decompose_v1.py`; the live checker is
`scripts/check_dataset_v1.py` and reports 200/50/10 split counts, no leaks, and
engine metric 1.0. The current split records, metadata, and baseline all use
`1.1.0`; retain an older `1.0.0` label only as historical provenance and never
silently re-anchor live records.

After merge and a frozen dataset, optimization follows pinned siblings in
`siblings-stub.yaml`, runs SIMBA/GEPA on `train.jsonl`, scores frozen eval, and
rejects any canary regression. A human reviews promotion. Optimize one named
model at `temperature=0.0`; retain `optimized/<name>/v<N-1>/` and swap a
versioned pointer for rollback. Modules are optimized independently.

## Definition of done (acceptance gates)

All boxes must be checked with command, cwd, exit status, and evidence:

- [ ] `src/cambium/modules/<name>/architecture.md` exists, is complete, and is committed.
- [ ] Dataset records carry schema/version/split fields; split layout has `meta.json`.
- [ ] Metric/evaluation meets the frozen held-out threshold; v2 uses the colocated aggregate.
- [ ] Canary suite is 100%; v2 asserts presence and processing in the scenario.
- [ ] Colocated module tests pass (`pytest .../tests/test_<name>_module.py -v`).
- [ ] Whole suite passes (`uv run --python 3.14.7 --extra test pytest -q`).
- [ ] Syntax and import gates pass (`compileall` and `import cambium`).
- [ ] End-to-end smoke passes when wired, or the deferral is recorded.
- [ ] Adversarial review is committed under `docs/architecture/reviews/` or rerun.
- [ ] Baseline metrics, exact command, dataset version, and split digests are recorded.
- [ ] Every claim is VERIFIED or explicitly UNVERIFIED/BLOCKED under `agents.md`.
- [ ] Module files are clean and committed; the root agent merges the branch.

## Anti-patterns

Do not hide failures with defaults/catch-all exceptions, add hidden mutable
state, drop canaries, claim unverified completion, use surface-memorizing
datasets, optimize against live co-adapted siblings, mutate frozen sets without
version/review, or change interfaces without updating the architecture,
catalog, consumers, and review.

## Appendix A. Expanded implementation checklist

### A.1 Module document review

Before implementation review the Module Identity table, then check each
interface against its source entry point. For inputs, cite the exact caller and
state whether empty strings, maximum length, Unicode, and invalid ranges are
rejected. For outputs, name the consumer and preserve wire compatibility only
at the serialization boundary. For errors, name the catching boundary and
show the failure event or return code. A module document that merely repeats a
template example without these facts is incomplete.

The state section must name scope (`per-call`, `per-instance`, or
`per-process`), mutation path, and persistence. A deterministic module may not
hold provider-derived state. A future DSPy seam documents signature, model
route, temperature/top-p/seed, and replacement behavior but does not add a
runtime dependency before the seam is needed.

### A.2 Dataset review

Review `meta.json` before running metrics. Check `schema_version`, semver
`dataset_version`, both freeze dates, all three exact split digests, and
`sibling_pins`. Check each record for a unique sorted ID, matching schema and
dataset versions, fixed split label, source/license/provenance, redaction
status, and bounded notes. Hash canonical input/expected content to reject
cross-split collisions. Record the deterministic seed or curated partition
rule in the module architecture.

Canaries must include at least one over-decomposition and one
under-decomposition trap, plus module-specific traps where useful. Record the
expected output, anti-expected output, confidence range if scored, and the
surface heuristic a weak model would exploit. A canary addition needs a
non-author reviewer. Never filter canaries out before aggregate scoring.

### A.3 Metric and benchmark review

Run the pure engine on train, frozen eval, and canaries. Record per-split mean,
std, count, canary total/pass/failure, dataset counts, test timings, runtime
versions, dataset version, and split digests in `tests/baselines/baseline.json`.
The baseline is invalid when records, metadata, and split bytes disagree. Keep
the engine baseline if a future DSPy prompt does not beat it on held-out eval
or if any canary regresses. Human approval is required for promotion; scoring
itself remains automatic.

For diff-producing modules, use the historical coding metric only when its
task record provides pre-registered tests, locked files, requirements, bounds,
gold diff, and patch canaries. Apply the tests floor and binary canary brake;
do not substitute a weighted average that can trade a failed test for a large
diff. The example decision module uses exact enum match instead.

### A.4 Scenario and CLI review

The colocated test must load real files, exercise a malformed record, process
all canaries, and verify deterministic output. Add tests for duplicate JSON
keys, unknown fields, missing fields, wrong types, malformed JSON, and strict
stdout/stderr behavior when a JSON CLI exists. The reference CLI has direct,
`decide`, and `evaluate` operations; a future standalone `eval` command must
not be documented until its package entry point and tests exist.

### A.5 Conformance and wheel review

Run `cambium module-test NAME` from the checkout and from an
installed wheel. Confirm the gate discovers only the package's colocated tests,
rejects arbitrary pytest arguments, validates manifest/dataset/baseline files,
probes the JSON CLI, and scans sibling/reverse imports. Confirm the offline
environment strips credentials and plugin injection. Document that it is
best-effort isolation, not a hostile same-UID sandbox. Confirm the wheel
contains code, CLI, architecture, datasets, metadata, tests, and baseline;
delete the package in a scratch copy to verify removability without deleting
shared scenario tests.

### A.6 Review, merge, and post-merge gates

Adversarial review checks interface drift, enum/wire mapping, loader error
paths, split freeze/version rules, canary coverage, metric gameability, hidden
state, and every UNVERIFIED claim. The implementing child commits only its
scoped files; root verifies and merges sequentially. After merge, rerun the
whole suite, module conformance, and the exact baseline command on `main`.

If the module introduces a new catalog code, update the catalog. If its output
shape changes, update consumers and every sibling's pinned evaluation. If a
dataset changes, bump the proper version and regenerate exact digests. If a
gate is blocked by external state, state the blocker and do not mark the module
done. A workaround is never a successful acceptance gate.

## Appendix B. Acceptance gate evidence template

Use this block for the root handoff:

```text
Scope: one module package and its colocated docs/tests/datasets.
Authority and target: module template + dataset format; code/tests are live truth.
Entry points read: package __main__, Module.decide, metric, loader, conformance CLI.
Baseline and reproduction: command, cwd, exit status, measured result.
Files in scope: explicit paths; no generated or unrelated files.
Change and preserved boundary: schema, split/version, canary, wire, and import boundaries.
Checks: exact command, cwd, exit status, evidence for each gate.
Status: VERIFIED | UNVERIFIED | BLOCKED.
Next action: root merge, owner reconciliation, or named blocker.
```

Do not report a test count, commit SHA, or successful command from memory. If a
command was not run, mark it UNVERIFIED; if it timed out or was unavailable,
report that result. A clean worktree proves scope, not correctness.

### B.1 Reference-module command mapping

For `should_decompose`, the focused command targets
`src/cambium/modules/example/tests/`; the split test covers 200 train, 50 eval,
and 10 canaries, while CLI tests cover direct, `decide`, and `evaluate`
operations. `scripts/check_dataset_v1.py` is a historical example-specific
checker, not a generic replacement for `module_conformance`. The live baseline
records Python `3.14.7`, pytest `9.1.1`, split means `1.0`, total 260 records,
10 canaries with no failures, and zero duplicate/leak counts. Treat its run SHA
as provenance only. A future standalone `eval.py` or DSPy optimizer must be
marked target until a source entry point and focused test exist.

### B.2 Change-scope guard

The eight documentation files in this task are independent of source and test
implementation. A module onboarding change normally owns the module package,
its colocated tests/datasets/baseline, and its architecture file; it does not
rewrite shared runtime code, generated files, or another module's docs without
first reporting the cross-module contract change. This guard keeps review and
rollback bounded.

### B.3 Handoff wording

Separate observed facts from inferences in the handoff. Cite repository-relative
paths and stable symbols, not filenames alone. State the before/after check
that distinguishes the root cause from a workaround. If the source does not
contain a caller, cache, class, or command named by the architecture, say so;
do not infer implementation from a matching document heading.
