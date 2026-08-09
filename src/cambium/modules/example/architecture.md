# should_decompose module — architecture

Reference example of the Cambium per-module pattern. Template for future
modules; the DSPy-per-module design keeps each module independently
hill-climbable (see `docs/architecture/system-design.md` §M9, Ascensus).

## Purpose

Decide whether a task should be decomposed into parallel subtasks before
it is dispatched to a worker. The orchestrator (Architectus, M6) is the
future caller: decompose before dispatch, evaluate results after merge.

## Interface

- `ShouldDecomposeModule` implements `cambium.modules.base.Module`
  - `name: str` — `"should_decompose"`
  - `async decide(input: TaskInput) -> DecomposeOutput`
  - `metric(example: Example) -> float` — delegates to
    `should_decompose_metric`
- `TaskInput` — `{task: str, context: str}` (context optional)
- `DecomposeOutput` — `{decompose: bool, reason: str, confidence: float}`
- `ExampleDatasetLoader` — JSONL dataset loader; schema in the module
  docstring, validated line by line.

The `Module` base is the DSPy seam: `decide` is the only surface a
replacement program must implement.

## State

None. The rule engine is a pure function of `(task, context)`; the
module holds no mutable state. This keeps it trivially parallelizable
and gives DSPy optimization a clean stateless target.

## Decision rules

Evidence accumulates from six signals; `decompose == true` iff
`evidence >= 2`:

| Signal | Condition | Evidence |
|---|---|---|
| Requirement clauses | 3+ sentences (split on `.`/`;`) | +1 |
| Length | task > 220 chars | +1 |
| Parallel-work keywords | 2+ of `HIGH_SIGNAL` | +1 |
| Per-item phrasing | word `each` | +1 |
| File references | 3+ file paths in task | +1 |
| Itemized list | 3+ numbered/bulleted items | +2 |
| Verb-led workstreams | 3+ clauses starting with an action verb | +2 |
| Verb-led workstreams | exactly 2 such clauses | +1 |

A context that already mentions subtasks/decomposition suppresses
decomposition outright (an explicit prior decomposition wins).

## Failure modes

- **Over-decomposition** — a keyword-heavy but atomic task is split into
  meaningless subtasks. Guarded by the `canary` decoy entry, which is
  keyword-dense but atomic (`decompose: false`).
- **Under-decomposition** — a genuinely parallel task with no surface
  keywords stays whole. Guarded by the `canary` entry with four
  verb-led workstreams and no keywords (`decompose: true`).
- **Reward hacking** (future DSPy eval) — an optimizer that memorizes
  surface heuristics scores 1.0 on the training split but fails the
  canaries, which are deliberately misaligned with those heuristics.
  Canaries are scored like any other entry; dropping them is a dataset
  integrity failure caught by the scenario test.
- **Garbage input** — structurally invalid records (malformed JSON,
  non-object records, non-string `task`, non-string `expected.reason`,
  non-boolean `expected.decompose`/`canary`) are rejected by the loader
  with `DatasetError`. String *content* is not validated: the engine is
  a pure function that must tolerate arbitrary strings (clause
  splitting is guarded), and a DSPy replacement inherits the same
  requirement.

## Test strategy

One scenario test (`tests/scenarios/test_example_module.py`), no
mocking, no network:

1. Load the real dataset; assert it loads and every record is
   schema-valid, plus a negative case: a malformed record raises
   `DatasetError`.
2. Run the module over every pair; assert the aggregate metric is
   perfect (1.0 on every example).
3. Assert the canary entries are present in the loaded dataset and were
   processed (a prediction was produced).

## DSPy seam

`decide` is the only surface a future DSPy program must implement. A
classification signature (`task, context -> decompose, reason`) compiled
with SIMBA/GEPA against this dataset, scored by `should_decompose_metric`
(holding the rule engine as baseline), can replace the engine without
touching the loader, metric, dataset, or callers.
