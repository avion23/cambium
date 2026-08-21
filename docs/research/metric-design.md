# Metric Design — Automatic Coding Metric

**Status: HISTORICAL RESEARCH SNAPSHOT (2026-08-09).** This design addresses
LLM-C5 for diff-producing modules. It is not the metric contract for the live
`should_decompose` module: that module keeps the exact-match
`src/cambium/modules/example/metric.py` function and the implemented example
CLI `operation: evaluate`/split evaluation helpers. This document remains the
target for `Opifex`-style coding tasks and future Ascensus work.

Companions: normative `docs/architecture/module-template/dataset-format.md`,
its architecture metric/test sections, and the example exact-match metric.

## 1. Requirements and cost

The metric must be automatic per example (human input only for dataset
construction, calibration, and promotion), resist reward hacking, be cheap for
SIMBA/GEPA hill-climbing, and track behavior rather than tokens/tool calls.
The design uses a floor (failed tests score zero), a binary canary brake, and
independent deterministic signals.

Measured/target costs from the snapshot:

| Path | Cost | Evidence |
|---|---|---|
| git diff checks | 9.3 ms average (20 runs) | measured in scratch repo |
| spec greps | <1 ms | measured in same pipeline |
| scenario suite | 10–30 s | target only; no reference suite existed |
| coverage run | 10–30 s | mechanics verified; inherits suite cost |
| LLM judge | one call/task | unverified; offline batch only |

Cheap checks run first. A candidate may get one retry after a failed test, never
an unbounded run-until-green loop. The LLM judge runs only on eval/promotion
after floor and canaries pass.

## 2. Signals

All signals normalize to `[0, 1]`.

### 2.1 Scenario-test pass rate (`tests`)

Each task pre-registers explicit argv, weighted tests, critical tests, and
`locked_files`:

```json
"scenario_tests": {
  "cmd": ["pytest", "-q", "tests/"],
  "tests": [{"id": "t01", "weight": 2, "critical": true}],
  "locked_files": ["tests/"]
}
```

Run in the candidate worktree with default hard timeout 120 s and captured
output. Never pipe through `tail` or `set -o pipefail`; preserve the test
process exit code. Parse pytest (`N passed, M failed`) or cargo summary and
compute:

```text
test_pass_rate = weighted_passed / weighted_total
critical failure => test_pass_rate = 0
no registered suite/tests_required:false => None (skip; floor moves to spec_coverage)
```

Locked-file changes/deletions are hard failures (`git diff --name-only` and
content hashes), as is `assert True` inflation. Flaky failures get one retry;
environment-sensitive tests are excluded at dataset review.

### 2.2 Diff-quality heuristics (`diff_quality`)

Measure `base_ref..candidate_head`; never trust a worker's commit narrative.
The verified scratch commands and outputs were:

```console
$ git diff --stat HEAD~1..HEAD
 mod.py | 5 ++++-
 1 file changed, 4 insertions(+), 1 deletion(-)
$ git diff --numstat HEAD~1..HEAD
4  1  mod.py
$ git diff --name-only HEAD~1..HEAD
mod.py
$ git diff --name-only --diff-filter=D HEAD~1..HEAD
test_mod.py
$ git diff --name-status HEAD~1..HEAD
M  mod.py
```

All five commands were verified against a real `/tmp` repo in the original
snapshot. Size uses `churn = Σ(added + deleted)`:

```text
score_size(churn) = 0.0 if empty/zero
                    1.0 if min_churn <= churn <= max_churn
                    linear falloff outside the interval (0 at .5×min, 2×max)
                    0.0 beyond hard bounds
```

The interval makes churn inflation score lower. `files` declares
`must_touch`, `must_not_touch`, and `max_files`; missing required files subtract,
forbidden files or too many files score 0, and `locked_files` are implicitly
forbidden.

Coverage has two tiers. Git proxies are test-file churn and added test
functions (`git diff -U0 | grep -E '^\\+.*def test_'`); source-only churn is
coverage-neutral. Optional tool coverage is:

```console
coverage run --source=. -m pytest
coverage report                         # delta = candidate TOTAL% - base TOTAL%
```

The snapshot verified that `--source=.` and importing the module are required;
otherwise coverage silently omits it.

Red flags inspect added lines only:

```console
git diff -U0 | grep -E '^\\+' | grep -vE '^\\+\\+\\+'
```

Patterns are `# noqa`, `# type: ignore`, `assert True`/`assert 1`, `print(`,
and `^\\+.*#` (added comments). The robust filter catches column-zero lines;
the naive `^\\+[^+].*` form misses them. `allowed_red_flags` is empty by
default and must be pre-authorized per task.

### 2.3 Canary-consistency score (`canaries`)

Dataset canaries and diff patch canaries are pre-registered and invisible to
the optimizer. The diagnostics pass rate is:

```text
canaries_pass_rate = passed / total
canaries_ok = 1 iff canaries_pass_rate == 1.0, else 0
```

One failed canary zeroes the complete score. Patch checks include
`tests_not_deleted`, `tests_not_trivialized`, `no_trivial_assert`, `no_silencer`,
`scope_respected`, and `no_harness_write`; the registry also includes
`no_gold_diff_drift` and `stub_detector`. Additions require a reviewer who did
not author the canary. The worker never receives canary records; cross-split
leaks are hard `DatasetError`s.

### 2.4 Spec-coverage heuristic (`spec_coverage`)

Task records pre-register requirements:

```yaml
requirements:
  - {id: R1, type: symbol_present, value: "def mul"}
  - {id: R2, type: line_present, value: "rate_limit.add(tenant_id)"}
  - {id: R3, type: behavior, value: "test_spec_r3_id"}
  - {id: R4, type: semantic, value: "multiplies per-tenant rates and retries on failure"}
```

`symbol_present` and `line_present` grep added lines; symbol credit is not
enough without its behavior test. `behavior` credits only when its registered
test passes. `semantic` is deferred when the judge is off. Thus
`spec_coverage = credited cheap requirements / non-deferred requirements` and
a no-op scores 0.

### 2.5 LLM judge (`spec_adherence`, optional/offline)

The architecture target names `spec_adherence` as a 1–5 judge signal. Use it
only for eval/promotion semantic requirements after deterministic gates. The
judge sees `(spec, diff, test_output)`, not a worker summary; calibrate it on a
human-graded held-out subset and disable it below its configured agreement
floor. It is the weakest signal and never replaces deterministic brakes.

## 3. Aggregation

The design is a gated weighted sum:

```text
if canaries_ok == 0:                         score = 0.0
elif tests is not None and not tests_ok:     score = 0.0
elif tests is None and spec_coverage == 0:   score = 0.0
else: score = w_test·test_pass_rate
           + w_diff·diff_quality
           + w_spec·spec_coverage
```

Default weights are `test_pass_rate 0.40`, `diff_quality 0.35`, and
`spec_coverage 0.25`; architecture's earlier reference was 0.30/0.20/0.30
with 0.15 behavioral folded into diff/canaries. If the judge is enabled, cheap
spec coverage is 0.10 and judge 0.15. Disabled signals are removed and weights
renormalized. Emit confidence as the minimum per-signal confidence; promotion
requires `score ≥ threshold` and `confidence ≥ 0.8`.

## 4. Dataset coupling and canary taxonomy

Coding records contain spec, `base_ref`, pre-registered tests, gold diff,
requirements, bounds, and canaries. `train.jsonl` (≥200) is optimization;
frozen `eval.jsonl` (≥50) is promotion and must beat baseline; frozen,
additive `canaries.jsonl` (target ≥15; reference ships 10) is loaded only at
promotion. `meta.json` carries freeze dates and sibling pins; loaders reject
cross-split leaks.

Decision canary kinds and v2 enforceability:

| Kind | Pass condition | v2 |
|---|---|---|
| `trivially_atomic` | decision false | active exact-match |
| `must_decompose` | decision true | active exact-match |
| `keyword_hack` | gold decision | active exact-match |
| `ambiguous_calibration` | confidence ≤ 0.6 | v2.1 only |
| `format_only_hack` | rationale length ≥50 | v2.1 only |

The shipped example uses top-level boolean `canary: true` and `canary_info`; it
has 10 records with `trivially_atomic ×2`, `must_decompose ×2`, `keyword_hack`,
`ambiguous_calibration`, `format_only_hack`, `context_suppression`, and
`near_duplicate_contradiction ×2`. The latter two are module-specific.

Coding patch kinds and pass conditions are `tests_not_deleted` (no locked/test
deletion), `tests_not_trivialized` (no locked change), `no_trivial_assert`,
`no_silencer` (outside `allowed_red_flags`), `scope_respected`,
`no_harness_write`, `no_gold_diff_drift` (file-set tolerance), and
`stub_detector` (no constant/no-op body).

## 5. Calibration and config

Each eval task has known-good and known-bad patches (`noop.patch`,
`deleted_test.patch`, `trivial_assert.patch`, `noqa_silencer.patch`,
`stub_symbol.patch`, `wrong_file.patch`). Gold must score `≥ 0.8`, each bad
patch `≤ 0.2`, and all bad canaries must fail. Re-run
`tests/scenarios/test_metric_antigaming.py` after every config change.

Tunables live in `src/cambium/modules/<name>/metric_config.yaml`: `weights`,
`size_bounds`, `red_flags`, `allowed_red_flags`, `canary_floor`,
`promotion_min_score`, and `promotion_min_confidence`. `config_version` pins
scores and prompts. A change requires known-good/bad separation, frozen eval,
and the applicable dataset-version bump. Production prompt promotion replaces
the module's artifact set `optimized/<name>/{program.json,lm.json,report.json}`
in place.

## 6. Anti-gaming matrix

| ID | Attempt | Countermeasure |
|---|---|---|
| G1 | delete failing test | locked files, deletion diff, `tests_not_deleted` |
| G2 | weaken test | locked hash and `tests_not_trivialized` |
| G3 | add `assert True` | not pre-registered; red flag and canary |
| G4 | no-op patch | zero size and zero spec coverage |
| G5 | `# noqa`/`# type: ignore` | added-line red flags and `no_silencer` |
| G6 | stub required symbol | behavior gate, semantic judge, `stub_detector` |
| G7 | memorize canaries | optimizer never sees them; additive/versioned set |
| G8 | game git state/out-of-scope writes | `base_ref..HEAD`, scope and git-op allowlist |
| G9 | inflate churn | interval size score and comment red flag |
| G10 | generic patch | task-specific requirements and mutated eval tasks |

## 7. Scenario tests proposed by this snapshot

`tests/scenarios/test_metric_antigaming.py` should use real git in a scratch
repo and assert: failed tests floor to 0; deleted locked test is fatal; added
`assert True` is fatal; no-op scores `< 0.1`; `# noqa` is fatal; and
known-good/bad separation holds. These are future coding-metric checks and do
not replace the live example module tests or its `operation: evaluate` CLI.

## Appendix A — measured verification log

The `/tmp/opencode/metric-verify` scratch repo (`mod.py`, `test_mod.py`, 15
commits) measured:

| Claim | Result |
|---|---|
| diff stat | `1 file changed, 4 insertions(+), 1 deletion(-)` |
| numstat | `4 1 mod.py` |
| touched/deleted paths | `mod.py` / `test_mod.py` |
| size sum | `adds=2 dels=3 files=1` |
| cheap path | 20 iterations, **9.3 ms** average |
| coverage mechanics | base **40%** → candidate **86%** with `coverage run --source=. -m pytest` |
| robust added-line filter | caught column-zero and indented `# noqa`, `assert True`, `def test_…` |

Unverified in the snapshot: a 10–30 s reference suite, judge cost/agreement,
and git-only coverage measurement (coverage requires the tool).

## Appendix B. Detailed signal contracts

### B.1 Test registration and floor

The task record owns the command, test IDs, weights, critical flags, and locked
paths. The candidate cannot choose a smaller suite or replace its output. A
test command runs with `cwd` set to the candidate worktree, a bounded timeout,
and full capture; a shell pipeline is not an acceptable adapter because it can
return the pipe consumer's success. Summary parsing accepts pytest's
`N passed, M failed` and cargo's `test result: ok. N passed` forms. Weighted
passes divide by weighted total. A critical failure makes the result zero,
even if every non-critical test passes. If no suite is registered, `tests` is
`None`, not `1.0`; the no-tests floor requires nonzero `spec_coverage`.

The one-retry rule is only for a first failure and is recorded in confidence.
Two failures remain a failed result. Reference datasets exclude tests involving
ports, clocks, or other environment-sensitive behavior. Locked test paths are
checked both by `git diff --name-only` and pre-registered content hashes, so a
candidate cannot delete or weaken a test to improve its floor.

### B.2 Diff quality and added-line filtering

The size interval has task-specific `min_churn`, `max_churn`, and `ideal`; an
empty diff scores zero, an in-band diff scores one, and out-of-band churn falls
off linearly until the hard bounds. File scope uses `must_touch`,
`must_not_touch`, and `max_files`. Every absent must-touch path subtracts;
forbidden paths and excess files score zero. `locked_files` are automatically
must-not-touch.

The canonical added-line filter is:

```console
git diff -U0 | grep -E '^\\+' | grep -vE '^\\+\\+\\+'
```

It must be applied before fixed-string checks for `# noqa`, `# type: ignore`,
`assert True`, `assert 1`, `print(`, and added comments (`^\\+.*#`). The
naive `^\\+[^+].*` filter loses the first content character and misses
column-zero matches. Legitimate suppressions require `allowed_red_flags` in
the task record. Coverage delta is not derivable from git: run
`coverage run --source=. -m pytest` on base and candidate and subtract their
`TOTAL` percentages. The snapshot measured base 40% and candidate 86% on an
untested-function patch and confirmed that omitted `--source=.` or an unimported
module produces no useful report.

### B.3 Canary predicates

Dataset canaries are ordinary module examples with trap metadata. Coding-task
patch canaries inspect `(diff, test_output, fs)` and use these stable IDs:

| ID | Predicate |
|---|---|
| `tests_not_deleted` | no `test_glob` path in `git diff --diff-filter=D` |
| `tests_not_trivialized` | no locked path in `git diff --name-only` |
| `no_trivial_assert` | no added `assert True`/`assert 1` |
| `no_silencer` | no added `# noqa`/`# type: ignore` outside allowlist |
| `scope_respected` | no `must_not_touch` path |
| `no_harness_write` | no new `.cambium/`, session, or metadata path |
| `no_gold_diff_drift` | diff file set within gold-file tolerance |
| `stub_detector` | no all-constant/no-op body below task statement floor |

The diagnostic pass rate is `(passed canaries)/(total canaries)`; the gate is
binary `canaries_ok`, and one failure multiplies the entire score by zero.
Canaries are not supplied to SIMBA/GEPA training and may be added with a minor
dataset-version bump, so memorizing a frozen set is not a solution. Every
canary description names the gaming move it catches, and a second reviewer
checks each addition.

### B.4 Requirements and semantic coverage

Requirements are authored with `type`, `id`, and `value`. `symbol_present`
uses the robust added-line filter and gives only partial credit until an
associated behavior test passes. `line_present` is an exact added-line match.
`behavior` delegates credit to the pre-registered test. `semantic` delegates
to the calibrated offline judge; when the judge is disabled, it is marked
deferred and excluded from the denominator. Therefore:

```text
spec_coverage = credited non-deferred requirements /
                non-deferred requirement count
```

This deliberately makes a dead `def mul` insufficient while keeping the cheap
path deterministic. Requirement aliases or semantic classification handle a
legitimate rename/refactor without making exact-line matching brittle.

## Appendix C. Aggregation, confidence, and calibration

The floor/brake ordering is not an implementation detail:

```text
canaries_ok == 0                         -> score 0.0
tests is not None and tests_pass == false -> score 0.0
tests is None and spec_coverage == 0      -> score 0.0
otherwise                                 -> weighted enabled signals
```

With all signals enabled the proposed weights are 0.40 tests, 0.35 diff
quality, and 0.25 spec coverage. The older architecture reference used 0.30,
0.20, and 0.30 plus 0.15 behavioral checks; this snapshot folds behavioral
checks into diff/canaries. Promotion-path judge use splits spec weight to 0.10
cheap and 0.15 judge. Disabled signals are removed and remaining weights
renormalized, so scores remain in `[0,1]`.

Confidence is the minimum signal confidence. Tests use `1 - flake_rate`, or
`0.5` when fewer than three tests exist or a retry differed. Diff/spec use 1.0
when an in-band diff and at least one requirement were checked, 0.5 for near
empty evidence. Deterministic canaries use 1.0, reduced for a one-entry suite;
the judge uses calibrated agreement. Promotion requires confidence `≥ 0.8`
in addition to the configured score threshold. Low-confidence scores are
reported but never promoted.

Known-good and known-bad separation calibrates thresholds. Each eval task has
gold plus `noop.patch`, `deleted_test.patch`, `trivial_assert.patch`,
`noqa_silencer.patch`, `stub_symbol.patch`, and `wrong_file.patch`. Gold must
score `≥ 0.8`, every bad patch `≤ 0.2`, and all bad canaries must fail. A
configuration is trusted only after this separation across the full eval set;
`tests/scenarios/test_metric_antigaming.py` reruns after every config change.

## Appendix D. Dataset and promotion protocol

Coding records contain a task spec, starting `base_ref`, explicit scenario
test argv/IDs/locked files, gold diff, requirements, size/file bounds, and
canaries. Train has at least 200 tasks and is the SIMBA/GEPA path. Eval has at
least 50 frozen tasks and is the promotion gate. Canaries target at least 15;
the shipped example has 10 and keeps them frozen but additive. `meta.json`
records `eval_frozen_at`, `canary_frozen_at`, `dataset_version`, split digests,
and sibling pins. A cross-split leak is a hard `DatasetError` because it both
poisons training and reveals traps.

Decision-module canary kinds remain `trivially_atomic`, `must_decompose`,
`keyword_hack`, `ambiguous_calibration`, and `format_only_hack`; only the first
three are active in the v2 example exact-match metric. The reference's
`context_suppression` and `near_duplicate_contradiction` are module-specific
extensions. Coding-task kinds use the patch predicates in Appendix B.

Promotion stores model, temperature, metric-config version, dataset version,
split digests, sibling pins, score/confidence, canary results, and human signoff.
The optimizer never receives canaries or the metric source. Production uses
the single artifact set under `optimized/<name>/`; each run replaces it in
place. If a candidate fails any floor or canary, keep the deterministic
baseline and report the exact failing predicate.

## Appendix E. Anti-gaming matrix and test anchors

| ID | Attempt | Signal targeted | Required brake |
|---|---|---|---|
| G1 | delete failing test | tests | locked deletion + `tests_not_deleted` |
| G2 | relax assertion | tests | locked hash + `tests_not_trivialized` |
| G3 | add empty/assert-true tests | tests | not pre-registered + red flag |
| G4 | no-op | tests/spec | size 0 and spec 0 |
| G5 | silence lint/type errors | diff | red flags + `no_silencer` |
| G6 | stub required function | spec/tests | behavior/semantic checks + `stub_detector` |
| G7 | memorize canaries | canaries | invisible/additive split |
| G8 | rewrite git or write out of scope | all | `base_ref..HEAD`, allowlist, scope canary |
| G9 | pad churn | diff | interval size and comment red flag |
| G10 | generic patch | spec/eval | per-task requirements and mutations |

The proposed real-git scenario file asserts failed tests score zero, deleting
a locked test is fatal, `assert True` and `# noqa` are fatal, no-op scores
below 0.1, and known-good/bad separation holds. It complements, but does not
replace, the live example's colocated module tests or `operation: evaluate`
CLI.

## Appendix F. Verification command record

The scratch verification directory was `/tmp/opencode/metric-verify`, with
`mod.py`, `test_mod.py`, and 15 commits. The following outputs were recorded:

| Check | Command/result |
|---|---|
| stat shape | `git diff --stat HEAD~1..HEAD` → `1 file changed, 4 insertions(+), 1 deletion(-)` |
| numstat shape | `git diff --numstat HEAD~1..HEAD` → `4 1 mod.py` |
| touched files | `git diff --name-only HEAD~1..HEAD` → `mod.py` |
| deleted files | `git diff --name-only --diff-filter=D HEAD~1..HEAD` → `test_mod.py` after `git rm` |
| deletion-only numstat | `0 2 test_mod.py` |
| `# noqa` filter | robust filter matched indented and column-zero additions |
| `assert True` filter | robust filter matched indented and column-zero additions |
| `print(` filter | matched `+    print("debug")` |
| added comment filter | `^\\+.*#` matched `+# def add(a, b):`; `+++` header did not |
| naive filter pitfall | `^\\+[^+].*` produced false negatives for column-zero content |
| symbol check | added-line filter matched `def mul` |
| size sum | `adds=2 dels=3 files=1` |
| cheap path | 20 iterations of numstat/name-only/`-U0`: **9.3 ms** average |
| coverage | `coverage run --source=. -m pytest`; base **40%**, candidate **86%** |

The coverage check also showed two traps: without `--source=.` only imported
modules report, and a test that never imports the candidate module produces no
module row. These measurements support the cheap-first ordering but do not
prove a production coding-task suite.

## Appendix G. Judge and semantic-signal boundary

`spec_adherence` is deliberately optional because an LLM judge is itself a
reward-hacking surface. On a promotion candidate that already passes tests,
spec coverage, and canaries, the judge reads only the pre-registered spec,
diff, and test output. It scores the semantic requirements with a fixed 1–5
rubric and is normalized to `[0,1]`. It never reads the worker's self-reported
summary, tool count, or hidden metric source. A human-graded held-out subset
sets the judge agreement floor; below that floor the judge is disabled and
semantic requirements remain deferred.

The judge is cached by diff hash only in an implementation that explicitly
records model/config version; this snapshot does not provide a cache module.
The cache must not become a hidden global or a way to skip a changed dataset
version. A config or judge-model change invalidates stored scores, reruns the
known-good/bad separation, and re-evaluates frozen eval before promotion.

## Appendix H. Why the floor and brake are non-negotiable

A pure weighted sum permits a candidate to trade a broken test for a large
diff or a polished rationale. The gated shape encodes the review judgment that
a task test failure and a canary failure are fatal. The test floor catches
behavioral regressions, diff quality catches out-of-scope or padded changes,
spec coverage catches no-op/general patches, and canaries catch known gaming
moves. They use different mechanisms, so one exploit does not raise all
signals. The v2 decision metric intentionally retains a simpler exact-match
floor because its outputs are labels, not diffs; this coding metric must not be
copied into `should_decompose` as a hidden complexity requirement.

## Appendix I. Requirement provenance and maintenance rules

Requirements, size bounds, locked paths, test IDs, and canary predicates are
authored with the reference task. They are not inferred from a candidate's
working tree, test collection, commit message, or tool transcript. A task
author may mark a requirement `semantic` when a rename or formatting choice
makes a cheap exact match brittle, but must supply a behavior test or calibrated
judge rubric. A task cannot mark all requirements deferred simply to avoid
checks. Changes to a task record are dataset changes: frozen eval/canary edits
need a version bump, second review, fresh split digest, and rerun of every
pinned module.

The metric implementation is itself outside the optimizer's write scope. The
worker receives task data and tool interfaces, not `metric.py`, regex lists,
baseline anchors, or sibling pins. Harness code reads candidate output as data
and performs the structural checks in its own process. This keeps G8 (game the
git state) and G7 (memorize canaries) structural failures rather than prompt
instructions. Calibration reruns after every metric-config change; a known-bad
patch that rises above `0.2` or a gold patch that falls below `0.8` blocks the
configuration from promotion.

The proposed anti-gaming scenario names are stable integration anchors:
`test_failed_tests_floor_to_zero`, `test_deleted_test_file_is_fatal`,
`test_assert_true_inflation_is_fatal`, `test_noop_patch_scores_near_zero`,
`test_silencer_noqa_is_fatal`, and
`test_known_good_known_bad_separation`. Their scratch repo uses real git and no
mocked subprocess. They are historical target tests; their names do not imply
that the current checkout has a coding-task metric suite.

### I.1 Review decisions recorded by the snapshot

The design rejects tool-call count, self-reported completion, and unlabeled
ground-truth F1 as primary signals because each can rise without better code.
It also rejects a pure LLM judge on the training path: a judge that sees the
worker summary can reward persuasive prose, while a judge that is not
human-calibrated can drift with the prompt. Deterministic git checks, fixed
tests, pre-registered requirements, and hidden canaries therefore run first;
semantic judging is a bounded promotion signal only.

The metric config is versioned separately from module and dataset versions.
Scores and optimized prompts carry `config_version`, dataset version, split
digests, model/temperature, and sibling pins. A config edit reruns separation
on all known-good/bad patches and the frozen eval before any pointer swap. If
the separation fails or a canary regresses, retain the previous prompt and the
deterministic baseline; do not lower thresholds to make the candidate pass.
