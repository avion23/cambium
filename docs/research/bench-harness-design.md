# Benchmark Harness — objective measurement for Cambium scenario tests

**Status: HISTORICAL RESEARCH SNAPSHOT (2026-08-09).** This document records
the proposed Ascensus measurement layer. It is not a claim that every proposed
surface is implemented. Current implementation evidence belongs to
`src/cambium/bench.py`, `src/cambium/module_conformance.py`, the example CLI,
and the colocated tests. No current-main test count or commit SHA is asserted
here.

## 1. Purpose and scope

The proposed harness stores four repeatable measurements for each module:

1. test wall time (p50/p90/max);
2. dataset health (schema, duplicates, cross-split leaks, balance, canaries);
3. module metric baselines on train/eval/canaries; and
4. canary hit rates, the anti-reward-hacking brake.

The intended deliverable is a stdlib-plus-pytest plugin in `src/cambium/bench.py`
with an optional `python -m cambium.bench` CLI. A baseline is comparable only
when command, worktree, Python, pytest, dataset version, and split digests are
recorded.

## 2. Commands (snapshot)

```sh
uv run --python 3.14.7 --extra test pytest --collect-only -q
uv run --python 3.14.7 --extra test pytest -q
uv run --python 3.14.7 --extra test pytest -q -p cambium.bench --bench=report
uv run --python 3.14.7 --extra test python -m cambium.bench report
```

The first two are repository checks. The latter two are proposed harness
forms. A gate exits 1 on metric drift, any new canary failure, dataset error,
duplicate ID, or cross-split leak.

| Section | Reported values |
|---|---|
| Test times | per-test wall time, p50, p90, max |
| Dataset integrity | record count, schema, duplicate IDs, leaks, class balance, canary kinds |
| Metric baseline | mean/std/count for train, eval, canaries |
| Canary coverage | taxonomy fraction and pass results |
| Drift | comparison with same-version baseline and thresholds |

The snapshot's earlier scaffold artifact used metric counts train 4, eval 3,
canaries 2 and dataset records 9; those values are historical examples, not
current-main measurements. The reference module's live split counts are
200/50/10 and are recorded in its baseline and dataset research snapshot.

## 3. Baseline artifact

Committed reference baselines live at
`src/cambium/modules/<name>/tests/baselines/baseline.json`; ephemeral reports
belong in gitignored `.cambium/baselines/`.

### The schema (abbreviated structural excerpt; not copyable)

The following excerpt shows the required shape and live values, but it is
intentionally abbreviated: `tests.count` is 57 while `tests.by_nodeid` shows
one representative node only. It must not be copied as a committed baseline
and does not by itself satisfy `module_conformance`. The canonical complete
artifact is the tracked
`src/cambium/modules/example/tests/baselines/baseline.json`; in every complete
artifact, `tests.count` must equal the number of entries in `tests.by_nodeid`.

```jsonc
{
  "schema_version": 1,
  "module": "should_decompose",
  "dataset_version": "1.1.0",
  "git_sha": "17dfcd362817fd173cf13d61585752d6d74b18e4",
  "date": "2026-08-10T13:52:09Z",
  "python": "3.14.7",
  "pytest": "9.1.1",
  "split_digests": {
    "train": "e41f1f4ca9e1905122e1faa0955cd2833bf032635ea721d33d36d1b3b7caf136",
    "eval": "f43cb1501ba4ba10fc27e2333a3794db04d6f5afa95ebfe586f66cf9d486d7ca",
    "canaries": "54bf2e41663b29d1382fe965cacb553009567287dc722a7710533bfe3e92ff3e"
  },
  "metric": {
    "train": {"mean": 1.0, "std": 0.0, "count": 200},
    "eval": {"mean": 1.0, "std": 0.0, "count": 50},
    "canaries": {"mean": 1.0, "std": 0.0, "count": 10}
  },
  "canaries": {
    "total": 10,
    "kinds_present": [
      "ambiguous_calibration", "context_suppression", "format_only_hack",
      "keyword_hack", "must_decompose", "near_duplicate_contradiction",
      "trivially_atomic"
    ],
    "taxonomy_coverage": 1.0,
    "failed": 0
  },
  "dataset": {"records": 260, "duplicate_ids": 0, "cross_split_leaks": 0,
               "decompose_true": 128, "decompose_false": 132, "canaries": 10},
  "tests": {
    "count": 57,
    "wall_seconds": {"p50": 0.001504, "p90": 0.123111, "max": 0.162092},
    "by_nodeid": {
      "src/cambium/modules/example/tests/test_dataset_splits.py::test_all_260_records_score_perfectly": 0.01338
    }
  },
  "drift_thresholds": {"metric_mean_delta": 0.05, "wall_p90_ratio": 1.5,
                        "canary_failed_delta": 0,
                        "dataset": {"duplicate_ids": 0, "cross_split_leaks": 0}}
}
```

The counts, digests, timings, and SHA above are copied from the committed
example snapshot for orientation only. The SHA is run provenance, not a claim
about the current tree; a new run must record its own SHA and date. Use the
tracked artifact when a complete baseline is required.

`dataset_version` selects the anchor. Compare only with the last baseline of
the same version. A version change creates a new anchor; a digest change with
the same version is a hard regression. Every baseline must carry
`split_digests.{train,eval,canaries}` equal to `datasets/meta.json` and the
SHA-256 of exact split bytes.

## 4. Gates and schedule

| Gate | Command | Contents | Fail on |
|---|---|---|---|
| Pre-merge | `uv run --python 3.14.7 --extra test pytest -q -p cambium.bench --bench=gate` | tests, integrity, canary presence | test/error/duplicate/leak/missing canary |
| Nightly | `uv run --python 3.14.7 --extra test python -m cambium.bench report --full` | gate + metric and canary baselines | drift over threshold or canary failure |
| Release | `uv run --python 3.14.7 --extra test python -m cambium.bench report --full --drift-report` | nightly + `.cambium/baselines/` report | threshold breach |

The snapshot proposed a DSPy hill-climb report of `train_gain - canary_gain`;
that is a reward-hacking diagnostic, not an implemented optimizer. The live
`module_conformance` gate and `scripts/check_dataset_v1.py` remain the concrete
module/dataset checks. There is no `eval_cache.py`, DLQ-backed benchmark cache,
or `ResourceBudget` symbol to cite.

## 5. Implementation sketch

The plugin would use `pytest_addoption` (`--bench=gate|report`),
`pytest_configure`, `pytest_collection_finish`,
`pytest_runtest_makereport` (timing/outcome), `pytest_sessionfinish` (integrity,
metric, drift, artifact, exit status), and `pytest_terminal_summary`. The
timing hook was checked against pytest 9.1.1 hookspec/runner in the original
snapshot; re-check before implementation.

`build_module_report()` should read `meta.json` fail-closed, require
`schema_version == 1`, semver `dataset_version`, both freeze dates, and three
lowercase SHA-256 digests; hash exact `train.jsonl`, `eval.jsonl`, and
`canaries.jsonl`; and never fall back to a combined file when split metadata
exists but a required file is unreadable. `_assemble_baseline()` copies the
digests. `compare_against_anchor()` checks version and all digests before
metric drift. Plugin and CLI paths apply the same checks.

## 6. Proposed self-tests

The harness test module should verify:

- per-test timing via `pytest_runtest_makereport`;
- dataset integrity, duplicate/cross-split rejection, and canary taxonomy;
- a zero-canary report (`total == 0`);
- a `metric.train.mean` 1.0 baseline and a 0.9 comparison that returns
  regression, while equal means do not; and
- exit-code failure for any canary regression.

## 7. DRAFT: mock git and AST asserts (v2.1/M8)

This section is a historical design, **not implemented**. The optimizer would
run in `.cambium/mock-envs/<module>/<run_id>/`, a deterministic scratch git
repo containing fixture code, train records without canaries, and a
`base_ref`. It would score `git diff base_ref..HEAD`, frozen split metrics, and
AST fingerprints; only human-approved promotion would touch the real tree.
Fixtures derive from `(module, dataset_version, candidate_hash)` so a version
bump invalidates the baseline. The reference split files are
`src/cambium/modules/example/datasets/{train,eval,canaries}.jsonl`.

AST asserts would preserve `ShouldDecomposeModule.decide(self, input:
TaskInput) -> DecomposeOutput`, `TaskInput` fields `task: str` and
`context: str = ""`, `DecomposeOutput` fields `decompose: bool`, `reason:
str`, `confidence: float = 1.0`, and the `should_decompose_metric` seam. A
candidate failing the fingerprint is scored 0 before the expensive test suite.

Before trusting mock metrics, run paired mock/real tasks with the same
candidate and compare train/eval deltas and canary results. A sign flip or
configured mean-gap means fixtures are not predictive; deterministic baseline
stays in production. No M8 run, tolerance, or DSPy optimization result was
measured in this snapshot.

## 8. Open questions

- Should taxonomy coverage become a hard gate after more modules migrate?
- Should nightly experiments use `deepseek-v4-flash` or another pinned model?
- Should `--bench` remain the option name if a third-party plugin adds one?

These are design questions, not current implementation claims.

## Appendix A. Baseline schema and drift details

The committed baseline is a schema-bearing artifact, not an opaque report. In
addition to the summary fields in §3, an implementation should preserve
per-nodeid timings and exact metric count/std values:

```jsonc
{
  "tests": {
    "count": 57,
    "wall_seconds": {"p50": 0.001504, "p90": 0.123111, "max": 0.162092},
    "by_nodeid": {"path/to/test.py::test_name": 0.004}
  },
  "canaries": {
    "total": 10,
    "kinds_present": ["trivially_atomic", "must_decompose"],
    "taxonomy_coverage": 1.0,
    "failed": 0
  }
}
```

`schema_version` identifies this JSON format; migration is a pure function.
`dataset_version` and `split_digests` identify the evaluation input. `python`,
`pytest`, `date`, and the run SHA make the measurement attributable, while
`drift_thresholds` makes the decision reproducible. The reference snapshot
uses `metric_mean_delta: 0.05`, `wall_p90_ratio: 1.5`,
`canary_failed_delta: 0`, and zero duplicate/leak limits. A report with a
missing required split, a non-finite mean/std, a stale record count, or a
digest mismatch is invalid before drift comparison.

Drift comparison is ordered: validate schema; load metadata; compare dataset
version; compare each exact split digest; then compare metric means, canary
failures, and timing. A new dataset version may record a fresh anchor. A digest
change at the same version is always a regression, even when score and tests
improve. This prevents a benchmark owner from re-anchoring a changed eval set
under an unchanged version.

## Appendix B. Plugin and CLI behavior

The plugin stores state on a per-session object, not a process-global cache.
`pytest_addoption` registers `--bench=gate|report` and threshold overrides;
`pytest_configure` records configuration and runtime versions;
`pytest_collection_finish` enumerates module tests;
`pytest_runtest_makereport` records call-phase outcome and wall time;
`pytest_sessionfinish` computes integrity, metrics, drift, and exit status; and
`pytest_terminal_summary` prints the report. The CLI calls the same report and
comparison functions so `pytest -p cambium.bench` and `python -m cambium.bench`
cannot disagree about freeze/version rules.

The harness must capture a child command's actual exit code and full output.
Piping `cargo test 2>&1 | tail -5` or an equivalent shell pipeline can mask a
failure and is prohibited. Commands are explicit argv with a bounded timeout;
credentials and task content are never written to the artifact. Dataset
records are loaded through the module loader, so a malformed record is a hard
gate rather than a best-effort report.

## Appendix C. Dataset report contents

For every discovered module, the report carries records by split, expected
class counts, duplicate IDs, canonical cross-split collision count, canary
total, kinds present, taxonomy coverage, per-split metric mean/std/count, and
the exact version/digests used. The reference split report therefore expects
train 200, eval 50, and canaries 10; the committed baseline records 260 total,
128 true and 132 false, and zero duplicate/leak counts. These values come from
the checked-in baseline, not a current-main collection claim. The historic
scaffold example in §2 (4/3/2 metric counts and nine records) remains only as a
format example.

Canary diagnostics distinguish pass rate from gate state. A zero-canary module
may report `total: 0` for diagnostics, but a configured module with required
canaries fails on missing canaries. A failed canary never gets averaged away.
The benchmark should also report whether a canary was absent because of a
loader error, filtered into the wrong split, or genuinely scored zero.

## Appendix D. Mock environment and AST target

The historical M8 proposal isolates optimization from the real checkout. A
run would create `.cambium/mock-envs/<module>/<run_id>/`, initialize a scratch
git repository, add deterministic fixture stubs for `decide.py`, `metric.py`,
`dataset.py`, tests, and train records without canaries, and commit `base_ref`.
The candidate receives no real-repository path. The score reads
`git diff base_ref..HEAD`, fixture tests, frozen metrics, and AST assertions;
only human-approved promotion touches the real tree.

The pre-registered example fingerprint requires `TaskInput(task: str,
context: str = "")`, `DecomposeOutput(decompose: bool, reason: str,
confidence: float = 1.0)`, `ShouldDecomposeModule.decide(self, input:
TaskInput) -> DecomposeOutput` as async, `metric(self, example: Example) ->
float`, and the `should_decompose_metric` reference. A failed fingerprint
scores zero before the 10–30-second suite. This is structural protection, not
proof of behavior.

Before trusting the mock environment, run paired known-good and known-bad
tasks in mock and real environments with the same candidate and metric. Compare
train/eval deltas and canary outcomes. A sign flip or mean gap beyond the
configured tolerance proves fixture drift; keep the deterministic baseline and
do not promote. No M8 run, tolerance, or optimizer result was measured in this
snapshot.

### D.1 Report fields and invariants

The report's module block carries logical module name, package selector,
dataset version, split digests, and the selected test node IDs. The metric
block carries `mean`, population `std`, and `count` for `train`, `eval`, and
`canaries`; an empty split is represented explicitly rather than silently
dropped. The canary block carries `total`, `kinds_present`,
`taxonomy_coverage`, and `failed`. The dataset block carries `records`,
`duplicate_ids`, `cross_split_leaks`, `decompose_true`, `decompose_false`,
and `canaries`. The timing block carries p50/p90/max and optional node IDs.

All numbers are finite and non-negative; rates are within `[0, 1]`, counts are
integers, and SHA-256 values are 64 lowercase hexadecimal characters. A
baseline's schema version, runtime versions, date, and run SHA are required
even when the metric is unavailable. If an optional metric cannot run, report
an explicit null with its reason and fail a gate that requires it; do not turn a
missing measurement into a passing zero.

### D.2 Proposed self-test matrix

The harness tests should cover a valid split module, a legacy combined module,
missing metadata, malformed metadata, stale record versions, changed split
bytes under the same version, new version anchoring, duplicate IDs,
cross-split canonical collisions, zero canaries, failed canaries, and a metric
mean regression. They should assert that plugin and CLI paths return the same
decision and that a failed gate preserves the previous anchor. A test should
also verify exact-byte hashing by changing only a trailing newline and
expecting a digest mismatch.

### D.3 Security and reproducibility boundary

The benchmark receives task records and candidate outputs as data. It must not
log credentials, prompts, private source, or full test output when those
contain sensitive content; retain only bounded diagnostics and stable counts.
The worker's commit message is not a measurement input. The harness hashes the
candidate worktree relative to a recorded base and rejects writes outside the
task scope. Offline subprocess checks are deterministic lint, not a sandbox;
deployment containment remains the security boundary. These constraints keep
the baseline useful for drift detection without granting the optimizer a way
to edit its own metric, dataset, sibling pins, or benchmark anchor.

## Appendix E. Failure policy and implementation checklist

The proposed implementation is fail-closed at the benchmark boundary. A
missing `meta.json`, unsupported schema, malformed split, invalid digest, or
version conflict prevents an anchor from being written. A provider import,
network attempt, or reverse module import belongs to the module-conformance
finding path, not a benchmark retry. A test timeout is a failed test result;
re-running it is allowed only once under the metric design's flake policy.
No fallback to a combined dataset is allowed after valid split metadata has
been found. No baseline is silently rewritten when a check fails.

The implementation checklist is:

1. discover module packages from tracked files and verify manifest/layout;
2. read and validate metadata and exact split bytes;
3. load each split through its module loader and reject duplicates/leaks;
4. collect pytest timings and outcomes without changing the test command;
5. evaluate train/eval/canaries through the module metric (the example uses
   `evaluate_split_async` and `evaluate_split`);
6. assemble the baseline with version, digests, counts, canaries, timings, and
   thresholds;
7. compare against a same-version anchor and set exit status; and
8. print a concise report while keeping artifacts free of secrets or prompts.

The live example has these pieces split across `bench.py`,
`module_conformance.py`, `dataset.py`, `metric.py`, and `__main__.py`; this
research document proposes composition and stricter freeze checks, not a claim
that every hook or report field has shipped.

### E.4 Reference-module adapter

For the example module, the benchmark adapter should call the package-neutral
JSON boundary or the existing metric helpers rather than importing the
decision package from harness production code. The split loader provides
`load_split()` and `load_all()`; `metric.py` provides
`evaluate_split_async()` for async callers and `evaluate_split()` for sync
callers, with an explicit error when the sync wrapper is called inside a
running event loop. The CLI's `operation: evaluate` is useful for a subprocess
probe because it returns prediction/score pairs without writing a report.

The adapter must distinguish the legacy nine-record combined file from the
current 200/50/10 split layout. If a split file is present but version checks
fail, report the error; a combined-file fallback is allowed only when the split
file is genuinely absent and the loader's explicit compatibility path selects
it. This prevents a run from silently scoring a smaller or older dataset after
a split edit.

### E.5 Historical limits

The snapshot did not measure a real coding-task suite, LLM-judge agreement,
mock-to-real transfer, or a production nightly run. It did measure local git
command shapes, red-flag filters, coverage mechanics, and the example baseline
fields listed above. Any implementation handoff must rerun the affected check
in its assigned worktree and report its own cwd and exit status; these recorded
numbers are context, not evidence of a later run.
