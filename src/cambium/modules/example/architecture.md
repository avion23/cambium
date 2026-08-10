# `should_decompose` module — architecture

**Status: NORMATIVE IN-TREE REFERENCE.** This is the compact module document
for the live scaffold. The template and example spec are the broader target;
the code and tests under this package are authoritative. A production
`Architectus` caller is not present yet.

## Purpose

Decide whether a task should be decomposed into parallel subtasks before
worker dispatch. `Architectus` is a future caller; the current module is
invoked by its CLI and colocated evaluation tests.

## Interface

- `ShouldDecomposeModule` implements `cambium.modules.base.Module` with
  `name == "should_decompose"`, async `decide(TaskInput)`, and `metric(Example)`
  delegated to `should_decompose_metric`.
- `TaskInput` is `{task: str, context: str = ""}`.
- `DecomposeOutput` is `{decision: Decision, reason: str, confidence: float}`
  with read-only boolean `decompose` for wire compatibility.
- `ExampleDatasetLoader` validates JSONL line by line and maps wire boolean
  `expected.decompose` to `Decision`.

`decide` is the only future DSPy replacement seam.

## State

None. The rule engine is pure `(task, context) -> DecomposeOutput`; the module
has no mutable state or cache.

## Decision rules

Evidence threshold: `decompose == true` iff evidence is at least 2.

| Signal | Condition | Evidence |
|---|---|---|
| Requirement clauses | 3+ sentences (`.`/`;`) | +1 |
| Length | task > 220 chars | +1 |
| Parallel keywords | 2+ `HIGH_SIGNAL` terms | +1 |
| Per-item phrasing | word `each` | +1 |
| File references | 3+ file paths | +1 |
| Itemized list | 3+ numbered/bulleted items | +2 |
| Verb-led workstreams | 3+ clauses / exactly 2 clauses | +2 / +1 |

Context mentioning `subtask` or `decompos` suppresses decomposition with
`Decision.DO_NOT_DECOMPOSE` (an explicit prior decomposition wins).

## Failure modes

- **Over-decomposition:** keyword-dense atomic canaries (`trivially_atomic`,
  `keyword_hack`) must remain false.
- **Under-decomposition:** keyword-free parallel canaries (`must_decompose`)
  must be true.
- **Reward hacking:** canaries are scored with all records; dropping them is a
  dataset-integrity failure.
- **Garbage/schema input:** malformed JSON, non-object records, invalid
  `task`, `reason`, `decompose`, or `canary` types raise `DatasetError`.
- **Context suppression:** intentional short-circuit; no recovery is needed.

## Test strategy

Tests in `src/cambium/modules/example/tests/` load both dataset layouts, reject
malformed records, verify enum/wire mapping, run every record, and assert a
perfect metric including canaries. `test_dataset_splits.py` covers the 200
train, 50 eval, and 10 canaries counts, record versions, filtering, duplicate
IDs, and cross-split checks; it does not own exact-byte digest enforcement.
The shared `module_conformance` gate validates metadata/baseline/content
digests, while `scripts/check_dataset_v1.py` validates schema/version/count,
leak, secret, and engine consistency. CLI tests cover
direct, `decide`, and `evaluate` operations and strict JSON errors. There is no
production orchestrator integration test.

## DSPy seam

A future classifier may implement `async decide` with signature
`task, context -> decompose, reason`, configure `CambiumLM` through
`dspy.configure`, and keep the `Decision` mapping and
`should_decompose_metric`. SIMBA/GEPA optimization is a target, not a current
dependency or implementation.
