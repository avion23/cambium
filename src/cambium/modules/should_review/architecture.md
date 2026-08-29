# Module Architecture — should_review

**Status: NORMATIVE TARGET, implemented.** Copy of the module template with
every section completed for the `should_review` decision module.

## 1. Module identity

| Field | Required value |
|---|---|
| Code | `new` (no catalog code in `docs/architecture/architecture.md` §4 covers review routing) |
| Name (Latin) | `Architectus.should_review` |
| Logical module name | `should_review` |
| Python package name | `cambium.modules.should_review` |
| Layer | Deterministic |
| Owner | agent:module-two (data builder `agent:data-builder-v1` for records) |
| Status | Build-ready |
| Version | 1.0.0 (`decide.py` rule engine) |

Logical and package names are separate contract values. The logical name is
`should_review`; the package is `cambium.modules.should_review`; the
module-test selector is `should_review`.

## 2. Purpose

Decide whether a worker result needs an adversarial review pass before it is
accepted. The module is a pure rule engine over evidence signals in the
result text: refusal markers, leftover TODO/FIXME/HACK markers, high-stakes
keywords, file references, a missing test signal on large diffs, and a terse
result for a complex change. The system failure without it: results that
cannot be completed, are unfinished, or touch sensitive/destructive paths are
accepted without review, so defects and policy violations reach the main
branch. `Architectus` is a future caller; the current consumer is the neutral
JSON CLI invoked by the bench harness, the conformance gate, and the colocated
tests.

## 3. Interfaces

### 3.1 Inputs

```python
@dataclass(frozen=True)
class TaskInput:
    task: str
    context: str = ""
```

| Field | Producer | Validation | Invalid-input behavior |
|---|---|---|---|
| `task` | JSON CLI `input.task`; dataset `input.task` | non-empty `str`; whitespace is not significant (empty after strip rejected); Unicode accepted; no length cap | `InputValidationError` at the CLI boundary; `DatasetError` at the loader boundary |
| `context` | JSON CLI `input.context`; dataset `input.context` | optional `str`, default `""` | `InputValidationError` / `DatasetError` when not a string |

The JSON adapter reads one JSON object, requires non-empty `input.task`,
allows optional string `input.context`, and rejects unknown fields, duplicate
keys, malformed JSON, and bad input with exit 1.

### 3.2 Outputs

```python
@dataclass(frozen=True)
class ReviewOutput:
    decision: Decision
    reason: str
    confidence: float  # [0.0, 1.0]

    @property
    def review(self) -> bool: ...
```

Closed domain alternatives use the `Decision` enum (`REVIEW` /
`DO_NOT_REVIEW`); the enum stays in the domain model and the wire boolean at
the boundary is `review`, never `decompose`. The JSON wire object is
`{"confidence": float, "review": bool, "reason": str}`. Consumers are the
JSON CLI probe, the bench harness (`operation: evaluate` reads the
prediction), and the colocated tests.

The v1 class-balance contract stays generic. The bench harness and the
committed baseline schema carry `label_true` / `label_false` as the
generic v1 class-balance field names (a bench contract, not a domain claim).
`module.json` therefore declares `label_field: "review"`; the conformance gate
counts class balance from `expected.review` and checks it against the
baseline's `dataset.label_true` / `dataset.label_false`. The bench harness
reads the manifest's `label_field` (`review`) for class balance;
`expected.decompose` remains a v1-compat mirror in each record, and the loader
still enforces `expected.decompose == expected.review`.
The same `label_field` mechanism is applied to `scripts/check_dataset_v1.py`,
the one remaining harness consumer that hardcodes the generic name.

### 3.3 Errors

```python
class InputValidationError(ValueError): ...   # JSON CLI boundary, __main__.py
class SchemaInvalidError(ValueError): ...     # dataset-record schema, __main__.py
class DatasetError(ValueError): ...           # cambium.modules.base, loader boundary
```

`InputValidationError` is raised and caught inside `__main__.main()` (the
process boundary; it never escapes to the supervisor event loop).  A
schema-invalid dataset record (bad `review`/`reason`/`canary`, malformed
`input`) raises `SchemaInvalidError`; `__main__._write_error` marks those
errors with the explicit `"code": "SCHEMA_INVALID"` split marker so the bench
harness can fall back to the combined file without guessing from the error
type.
`DatasetError` is raised by `ExampleDatasetLoader` for unreadable files,
malformed JSON, schema-invalid records, version drift, duplicate ids, and
cross-split collisions.

### 3.4 Tool surface

`N/A — this module adds no tools.` `TOOL_SCHEMAS` in `src/cambium/schemas.py`
and dispatch in `src/cambium/tools.py` are untouched.

### 3.5 JSON CLI

`__main__.py` is the wire adapter. It reads one JSON object, requires
non-empty `input.task`, allows optional string `input.context`, rejects
unknown fields, duplicate keys, malformed JSON, and bad input with exit 1, and
writes exactly one JSON object plus newline to stdout (`json.dumps(..., sort_keys=True) + "\n"`).
Diagnostics go to stderr. The adapter preserves stable wire fields (`review`,
`reason`, `confidence`), avoids providers and network access, and works without
the checkout on `sys.path`.

```console
$ printf '%s\n' '{"task":"I cannot complete the payment migration.","context":""}' \
    | python -m cambium.modules.should_review
{"confidence":0.9,"reason":"worker refusal marker","review":true}
```

## 4. State

| Scope | Mutation path | Persistence |
|---|---|---|
| Decision calls | none | none |

This module is stateless across calls; only the primary implementation is held
on the instance. No LLM-derived state is owned; no state is mutated from a
worker process.

## 5. DSPy program

### 5.1 Primary and seam

`ShouldReviewModule` subclasses `cambium.modules.base.Module` and implements
`async decide(input: TaskInput) -> ReviewOutput` and
`metric(example: Example) -> float`. The primary is a deterministic rule
engine; DSPy is not required for v2. `decide` is the replacement seam, so
callers, loader, dataset, and metric remain stable.

### 5.2 Signature and replacement

`ShouldReviewSignature` is the shipped optimization seam, used only by
`cambium optimize` and conformance tests; the rule engine decides in the
default run path. A classifier may implement `async decide` with signature
`task, context -> review, reason`; replacement modules use the same interface
and read `dspy.Prediction` attributes directly.
Configure a `CambiumLM` through `dspy.configure(lm=...)`; do not construct
`dspy.LM` directly or mutate `dspy.settings.context`.

### 5.3 LLM and determinism

`N/A — no LLM calls in the v2 rule engine.` A future LLM seam must route all
calls through `Diffundo` and its `CambiumLM` at `temperature=0.0` (classifier).

## 6. Metric

`should_review_metric(example: Example) -> float` is deterministic exact match
in `[0, 1]`: 1.0 when the predicted `ReviewOutput.decision` equals the
expected `Decision`; 0.0 for an unprocessed example (no prediction) or when
either side is not a `Decision`. The `reason` and `confidence` fields are not
scored. Signal weights and gameability: the rules weight refusal (+3) and
markers (+2) above keyword density (+1/+2), so a keyword-greedy reviewer that
ignores completion markers under-reviews large untested diffs. The canaries
detect each unwanted behavior: `trivially_atomic` and `keyword_hack` detect
over-review from surface keywords, `must_review` and `format_only_hack` detect
under-review of keyword-free or well-formatted-but-untested results, and
`ambiguous_calibration` guards over-confidence on a single low-risk signal.

## 7. Dataset

Paths and layout: `datasets/{train,eval,canaries}.jsonl` plus `datasets/meta.json`;
no legacy `<name>_pairs.jsonl` is shipped (the loader keeps the explicit
fallback for backward compat and it is exercised by tests).

- train: 40 records; frozen eval: 10 records; canaries: 5 records.
- Hand-authored provenance, schema_version 1, dataset_version `1.0.0`,
  `eval_frozen_at`/`canary_frozen_at` `2026-08-11`, license `internal`.
- Deterministic split: fixed hand-assigned ids (`should_review-0001..0040`,
  `should_review-0201..0210`, `should_review-canary-01..05`); records sorted by
  `id` within each split; no canonical `(task, context)` occurs in two splits.
- Canary markers: `canary: true` plus `canary_info` with `kind`,
  `anti_expected`, `anti_expected_confidence_range`, `failure_mode`, and
  description. Kinds: `trivially_atomic`, `must_review` (the must-decompose
  analogue renamed for review semantics), `keyword_hack`, `format_only_hack`,
  `ambiguous_calibration`; four of the five are taxonomy kinds, so
  `bench.canary_stats` reports non-zero coverage (0.8).
- Sibling versions: `sibling_pins` is empty; no sibling stubs exist.
- Who may add records: the module owner hand-authors additions; a second
  reviewer approves frozen eval changes and canary additions; refresh cadence
  is per review, each bump requiring a `dataset_version` bump and a new
  baseline anchor.

## 8. Failure modes

| Trigger | Symptom | Detection | Recovery |
|---|---|---|---|
| Malformed JSON / non-object / duplicate keys at the CLI | exit 1, error object on stdout, diagnostic on stderr | `InputValidationError` / `json.JSONDecodeError` caught in `__main__.main` | caller resubmits a valid object; the boundary never defaults |
| Invalid `input.task`/`input.context` (missing, empty, wrong type) | exit 1 with typed error | `_parse_input` at the CLI boundary | caller fixes the record |
| Schema-invalid dataset record at the CLI (`evaluate` on a bad record) | exit 1, error object with `"code": "SCHEMA_INVALID"` | `SchemaInvalidError` in `__main__._evaluate`/`_write_error` | record owner fixes the JSONL and bumps `dataset_version`; the bench harness falls back to the combined file |
| Schema-invalid dataset record at the loader (bad `review`/`reason`, mirror drift) | `DatasetError` with file:line | `ExampleDatasetLoader._validate` | record owner fixes the JSONL and bumps `dataset_version` |
| Version drift (record vs meta.json) | `DatasetError` | `_validate_record_versions` | bump `dataset_version` deliberately; never silently re-anchor |
| Duplicate id / cross-split `(task, context)` | `DatasetError` | loader + conformance gate | deduplicate and re-run digests |
| Over-review: keyword-dense atomic canaries | metric < 1.0 on `trivially_atomic`/`keyword_hack` | canary gate (`bench.canary_stats.failed`) | tune evidence weights; do not drop the canary |
| Under-review: keyword-free/well-formatted untested canaries | metric < 1.0 on `must_review`/`format_only_hack` | canary gate | restore file/test signals; do not drop the canary |
| Reward hacking: dropping canaries | `dataset.canaries` count mismatch | baseline + gate integrity checks | restore the frozen records |

## 9. Test strategy

Tests are colocated in `src/cambium/modules/should_review/tests/`, the baseline
in `src/cambium/modules/should_review/tests/baselines/baseline.json`, and shared
runtime scenarios in `tests/scenarios/`. The module is removable by deleting its
complete directory; shared scenarios stay.

### 9.1 Unit and integration tests

`test_review_module.py` and `test_dataset_splits.py` load the real dataset,
check the schema plus negative `DatasetError` paths, run every record
(including canaries), assert the declared aggregate threshold
(`eval` mean ≥ 0.95), and anchor the committed baseline to `meta.json` and the
exact split bytes. `test_review_cli.py` covers the direct, `decide`, and
`evaluate` operations, strict JSON errors, duplicate keys, unknown fields,
empty/max/unicode input, and deterministic output. More than three happy paths
and every failure mode in §8 are covered.

### 9.2 Eval and canaries

In v2, colocated tests are the eval-harness substitute. A v2.1 target may add
`python -m cambium.modules.should_review.eval` and `--suite canaries`; any
canary failure exits non-zero and canary pass rate gates promotion. The
committed baseline records `canaries.failed == 0` and `taxonomy_coverage` > 0.

### 9.3 Module conformance

Run:

```console
PYTHONPATH=src uv run --python 3.14 python -m cambium.cli module-test should_review
```

The live `module_conformance` gate validates tracked layout, datasets,
baselines, imports, JSON CLI, subprocess isolation, and module-scoped tests.
This offline guard is a **BEST-EFFORT, deterministic lint-style check for
common forms of accidental network use; it is not a security boundary.** The
module imports only `cambium.modules.base` plus its own package; sibling
imports and reverse imports are static failures.

### 9.4 Baseline and removal

Baseline: `src/cambium/modules/should_review/tests/baselines/baseline.json`,
generated by `pytest -p cambium.bench --bench=report` (never hand-written).
It contains `schema_version`, logical `module` (`should_review`),
`dataset_version`, `split_digests`, `git_sha`, `date`, `python`, `pytest`,
`metric`, `canaries`, `dataset`, `tests`, and `drift_thresholds`. Its digests
and version match `datasets/meta.json` and the exact split bytes. The module
package in the source layout includes package code, `__main__.py`,
`architecture.md`, datasets, metadata, colocated tests, and baseline. Removal
means deleting `src/cambium/modules/should_review/`; shared scenarios remain.

## 10. Optimization plan

The v2 rule engine is deterministic and needs no state optimizer. A v2.1
DSPy replacement (state optimizer `dspy.SIMBA` or `dspy.GEPA`, train 40, max
steps as configured) would require human approval for promotion, replace
`optimized/<name>/{program.json,lm.json,report.json}` in place, hold out the
10-record eval set, gate at ≥ 0.95 aggregate, and pass 100% of canaries at
`temperature=0.0`.

## 11. Open questions

| Question | Owner | Decision needed |
|---|---|---|
| Should the refusal/high-stakes keyword sets be registry-driven (like `CANARY_TAXONOMY`) instead of module constants? | module owner | orchestrator: keep module-local sets or centralize |
| Is an `Architectus.should_review` caller planned in the orchestrator? | orchestrator owner | route decision after worker completion |

## 12. Changelog

| Version | Date | Change |
|---|---|---|
| 1.0.0 | 2026-08-11 | Initial module: rule engine, JSONL dataset (40/10/5), metric, CLI, conformance, baseline |

## Appendix A. Required evidence and boundary notes

The module is traced from its live entry point: the neutral JSON CLI
(`cambium.modules.should_review.__main__`) is invoked by the bench harness
(`src/cambium/bench.py::build_module_report` through `run_module_cli`), the
conformance probe (`src/cambium/module_conformance.py::probe_module_cli`), and
`scripts/check_dataset_v1.py`. No production orchestrator caller exists yet;
`Architectus` is a future target, stated as a target, not a live caller.

Shared surfaces used: the module ABC and examples (`cambium.modules.base`,
`cambium.modules.example`), the benchmark/conformance harness (`bench.py`,
`module_conformance.py`), and the CLI (`cli.py`). No module-specific schema
registry, dispatch path, cache, or resource-budget abstraction is added.

### A.1 Interface and wire checklist

- Who constructs each input: the JSON CLI (`input.task`, `input.context`) and
  the dataset loader (`input` envelope). Whitespace: `task` rejected when empty
  after `strip`; `context` defaulted to `""`. Length: no cap; the engine is
  bounded by linear scans. Unicode: accepted and passed through
  `json.dumps(..., ensure_ascii=False)`. Numeric bounds: `confidence` validated
  as finite in `[0.0, 1.0]` at the wire.
- Which exception is raised and where it is caught: `InputValidationError` in
  `__main__.py` (caught in `main()`), `DatasetError` in `dataset.py` (raised to
  callers; never hidden behind a catch-all).
- Which consumer reads each output: the bench harness reads `evaluate`
  predictions and scores; the CLI probe reads the direct decision; tests read
  `ReviewOutput` directly.
- Which fields are retained at the wire boundary: `review` (boolean label),
  `reason`, `confidence`. The domain enum is never replaced with a string
  allowlist; the compatibility boolean `review` exists only at the boundary.
- `decompose` is not reused as the wire label. `module.json` declares
  `label_field: "review"`, the conformance gate counts class balance from
  `expected.review`, and the baseline carries `label_true`/`label_false`
  as generic v1 class-balance names. `expected.decompose` in each dataset record
  is a v1-compat mirror of `expected.review` enforced by the loader; the bench
  harness reads `manifest.label_field` for class balance. `scripts/check_dataset_v1.py`
  reads the same `label_field` instead of hardcoding the generic name.
- The JSON adapter is a process boundary: it rejects duplicate object keys,
  rejects unknown fields, emits no logs on stdout, and avoids importing
  providers. Direct and malformed-input probes are colocated in
  `test_review_cli.py`. The `evaluate` operation reads `records[].input`,
  `records[].expected.review`, and `records[].expected.reason` and returns
  `{prediction, score}` per record.

### A.2 Dataset and baseline evidence

Counts: 40 train / 10 eval / 5 canaries = 55 records. Source and license:
hand-authored, `internal`. Schema/dataset versions: 1 / `1.0.0`. Split rule:
hand-assigned ids, sorted within each split, unique `(task, context)` across
splits. Freeze dates: `eval_frozen_at` and `canary_frozen_at` `2026-08-11`.
Digest source: exact split bytes (SHA-256), recorded in `meta.json` and the
baseline. Sibling pins: none. Refresh authority: module owner; a second
reviewer for frozen changes. The baseline `git_sha` identifies the worktree
commit that produced it; it is not evidence that the checkout is at that SHA.

### A.3 Failure and test evidence

Failure tables distinguish malformed boundary records (`InputValidationError`),
deterministic domain results (evidence thresholds), and metric/canary gate
failures. Recovery names the owner and boundary; no default turns a broken
input into a successful result. Tests cover both the removable directory and
shared contracts: module tests may be deleted with the module, while
`tests/scenarios/` stays. The conformance gate validates tracked files,
manifest, dataset versions/digests, imports, CLI subprocess behavior, and the
loaded module set. The deletion canary deletes `example/` and re-runs the
shared scenarios; the two `example`-coupled scenario tests skip when the
reference module is absent (guards in `tests/scenarios/test_module_conformance.py`).

### A.4 Target-state labels

**Implemented:** the rule engine, dataset, metric, CLI, conformance gate
`label_field` (Option A), `check_dataset_v1.py` `label_field`, the deletion
canary skip guards, and the committed baseline. **Normative target:** the
module template requirements. **v2.1 target:** the standalone eval command,
DSPy replacement, optimizer artifacts, and the `Architectus` caller.
