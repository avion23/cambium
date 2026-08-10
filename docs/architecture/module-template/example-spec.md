# Example Module Spec — `should_decompose`

**Status: NORMATIVE REFERENCE TARGET.** The code under
`src/cambium/modules/example/` is authoritative for the current scaffold; this
spec records its stable contract. There is no production `Architectus` caller
yet, so caller references below are target integration points.

## 0. Why this module

`should_decompose` closes the missing do-not-decompose path, is a small
deterministic classification, has an exact-match metric, and exercises the
module pattern (ABC, loader, dataset, canaries, CLI, and DSPy seam). It has no
subprocess, git, or IPC ownership. The v2 implementation is a rule engine;
DSPy is a v2.1 replacement seam. The merge sequencer is not a comparable
reference because it is deterministic and has no dataset or DSPy seam.

## 1. Module identity

| Field | Value |
|---|---|
| Code | M6.A (submodule of Architectus, M6) |
| Logical name | `should_decompose` |
| Python package | `cambium.modules.example` |
| Directory / selector | `src/cambium/modules/example/` / `example` |
| Layer | Orchestrator target |
| Owner | TBD |
| Status | Spec'd; scaffold present |
| Reference path | `src/cambium/modules/example/` |

These names are intentionally separate and must not be normalized by a caller.

## 2. Purpose

Choose atomic dispatch (one worker) or decomposition (parallel workers plus
merge), and emit a reason and confidence for audit. Without the module,
trivial tasks pay decomposition, process, worktree, and merge costs while
coherent tasks risk fragmented intent and merge conflicts. `Architectus.execute`
is a future caller, not a production caller in this checkout.

## 3. Interfaces

### 3.1 Input

```python
@dataclass(frozen=True, slots=True)
class TaskInput:
    task: str
    context: str = ""
```

`task` is non-empty after `strip()` at the CLI/loader boundary. `context` is
optional; if it contains `subtask` or `decompos`, the engine suppresses
decomposition. A prior `task_kind_hint` field was removed: a future hint must
be a `TaskKind` enum and requires a schema-version bump. The target producer is
`Architectus.execute` from the host task spec.

### 3.2 Output

```python
@dataclass(frozen=True, slots=True)
class DecomposeOutput:
    decision: Decision
    reason: str
    confidence: float = 1.0

    @property
    def decompose(self) -> bool: ...  # read-only wire view
```

`Decision.DECOMPOSE` selects parallel dispatch; `DO_NOT_DECOMPOSE` selects one
worker. `reason` is audit text and is not scored by v2. `confidence` is in
`[0.0, 1.0]`; the rule engine emits `0.7`, `0.8`, or `0.9` and v2 does not
score calibration. JSON keeps `expected.decompose` as a boolean while domain
code uses `Decision`.

### 3.3 Module class

`ShouldDecomposeModule` implements `cambium.modules.base.Module`:

```python
name = "should_decompose"
async def decide(self, input: TaskInput) -> DecomposeOutput: ...
def metric(self, example: Example) -> float: ...
```

`decide` is the only DSPy replacement seam; callers, loader, dataset, and
metric remain unchanged.

### 3.4 Errors

The v2 rule engine accepts any string and does not raise. `ExampleDatasetLoader`
raises `DatasetError` for unreadable/invalid JSONL, non-object records, missing
`input`/`expected`, non-string `input.task` or `expected.reason`, non-boolean
`expected.decompose` or `canary`, duplicate IDs, version drift, and
cross-split collisions. A bad dataset aborts evaluation. `ModelUnavailable`
and `MalformedLLMResponse` are v2.1 DSPy errors; the current code does not
raise or implement their atomic fallback.

### 3.5 JSON CLI

`python -m cambium.modules.example` is the implemented wire adapter. It reads
one object, rejects unknown/duplicate fields and malformed input, and emits
one JSON object plus newline. A direct request is `{"task": str,
"context": str}`; successful output is:

```json
{"confidence":0.7,"decompose":false,"reason":"task is atomic or already scoped"}
```

It also implements `{"operation":"decide","inputs":[...]}` and
`{"operation":"evaluate","records":[...]}`. `evaluate` runs the module
metric over supplied records and returns prediction/score pairs. Errors return
one JSON `error` object on stdout, a one-line stderr diagnostic, and exit 1.
The CLI does not contact providers or the network and is intended to work
outside the checkout import path.

```console
printf '%s\n' '{"task":"Fix the typo.","context":""}' \
  | python -m cambium.modules.example
```

## 4. State

Stateless across calls: the instance has only class attribute `name`; the rule
engine is pure `(task, context) -> DecomposeOutput`. No cache, counter, or
mutable process state exists.

## 5. Decision rules (v2)

The engine returns `DECOMPOSE` when total evidence is at least 2:

| Signal | Condition | Evidence |
|---|---|---|
| Requirement clauses | 3+ sentences split on `.`/`;` | +1 |
| Length | `task` > 220 chars | +1 |
| Parallel keywords | 2+ `HIGH_SIGNAL`: `multiple`, `several`, `both`, `subtasks`, `components`, `services`, `independently`, `in parallel`, `separately`, `decompose` | +1 |
| Per-item phrasing | word `each` | +1 |
| File references | 3+ recognised paths | +1 |
| Itemized list | 3+ numbered/bulleted items | +2 |
| Verb-led workstreams | 3+ action-led clauses | +2 |
| Verb-led workstreams | exactly 2 action-led clauses | +1 |

Context mentioning `subtask` or `decompos*` wins outright with
`DO_NOT_DECOMPOSE`, reason `context already provides a decomposition`,
confidence `0.9`. Otherwise a positive result has confidence `0.8` and joined
evidence reasons; a negative result has reason `task is atomic or already
scoped` and confidence `0.7`. Wire output remains boolean.

### 5.1 DSPy seam (v2.1 target)

A future `ShouldDecomposeModuleDSPy` implements the same async `decide`, uses
`dspy.Signature(task, context -> decompose, reason)`, configures
`dspy.configure(lm=CambiumLM(diffundo, tier="fast", temperature=0.0))`, and
reads prediction attributes (`pred.decompose`, not `pred.dict()`). It preserves
the enum/wire mapping and `should_decompose_metric`; it is not implemented in
the current scaffold.

## 6. Metric

`should_decompose_metric` is exact match on the domain enum:

```python
def should_decompose_metric(example: Example) -> float:
    if example.prediction is None:
        return 0.0
    expected = example.expected.get("decompose")
    prediction = example.prediction
    if not isinstance(expected, Decision) or not isinstance(prediction.decision, Decision):
        return 0.0
    return 1.0 if prediction.decision == expected else 0.0
```

Only attached predictions with valid `Decision` values score; `reason` and
confidence are not scored in v2. Canaries are included, so keyword memorizing
is exposed by deliberately misaligned records. A v2.1 composite may add
calibration/rationale signals only with the schema and dataset-version bumps
required by `dataset-format.md`.

## 7. Dataset

| Item | Current value |
|---|---|
| Split files | `datasets/train.jsonl`, `eval.jsonl`, `canaries.jsonl` |
| Counts | 200 train, 50 eval, 10 canaries |
| Metadata | `datasets/meta.json`, `schema_version: 1`, `dataset_version: 1.1.0` |
| Legacy fallback | `datasets/example_pairs.jsonl` (9 records, 2 canaries) |
| Loader | `ExampleDatasetLoader` in `dataset.py` |
| Provenance | hand-authored; canaries misalign surface heuristics |
| Sibling pins | none; first module |

Split records use the implemented envelope and loader-validated payload:

```json
{
  "input": {"task": "...", "context": ""},
  "expected": {"decompose": false, "reason": "..."},
  "canary": false
}
```

The split files are curated and disjoint. `meta.json` freezes eval and canary
dates at `2026-08-09` and records exact split digests; the baseline must match
that version and digest map. Train/eval/canary additions and review follow the
normative dataset format. The current live loader also provides
`load_split()`, `load_all()`, and `evaluate_split_async()`/`evaluate_split()`
metric evaluation.

## 8. Failure modes

| Mode | Trigger | Detection/recovery |
|---|---|---|
| Over-decomposition | keyword-heavy atomic task | `trivially_atomic`/`keyword_hack` canary; reject candidate |
| Under-decomposition | parallel work with few keywords | `must_decompose` canary; reject candidate |
| Reward hacking | train heuristic memorization | all canaries score in aggregate; no promotion on a miss |
| Invalid record | malformed JSON, missing/type-invalid field | loader raises `DatasetError`; fix data, do not catch |
| Context already decomposed | context contains `subtask`/`decompos` | intentional short-circuit; no recovery |
| Empty task | caller/CLI passes empty string | CLI rejects; direct rule function remains tolerant |

## 9. Test strategy and acceptance

Colocated tests under `src/cambium/modules/example/tests/` load the legacy and
split datasets, validate `Decision` mapping and read-only boolean view, reject
malformed records, process canaries, and score the engine over all 260 split
records. The CLI tests cover direct, `decide`, and `evaluate` operations,
duplicate keys, typed input errors, and one-object stdout behavior. Shared
runtime scenarios remain in `tests/scenarios/`; there is no production
Architectus integration test yet.

The module conformance command is:

```console
uv run --extra test cambium module-test example
```

It validates tracked layout, manifest, dataset and baseline schema/digests,
imports, CLI, offline subprocess behavior, and module-scoped tests. The gate
rejects provider/network use and sibling/reverse imports. The probe runs from the
source layout; there is no wheel delivery. The module is removable by deleting its package,
including tests, datasets, baselines, CLI, architecture, and freeze metadata.

### 9.1 Verification commands and recorded state

The historical verification command for the colocated suite is:

```console
uv run --python 3.14.7 --extra test pytest src/cambium/modules/example/tests -v
```

The committed baseline records Python `3.14.7`, pytest `9.1.1`, metric means
1.0 (`count`: 200/50/10), canaries total 10 with `failed: 0`, and dataset total
260 (`duplicate_ids`: 0, `cross_split_leaks`: 0, `decompose_true`: 128,
`decompose_false`: 132). Its drift thresholds are `metric_mean_delta: 0.05`,
`wall_p90_ratio: 1.5`, `canary_failed_delta: 0`, and zero duplicate/leak
counts. The baseline's recorded `git_sha` is data provenance, not a current
tree claim.

## 10. Optimization plan (v2.1 target)

Use SIMBA or GEPA on `train.jsonl` (200), score frozen `eval.jsonl` (threshold
`≥ 0.85`), and require 100% canaries. Optimize one named model at
`temperature=0.0`, with pinned siblings (none today), and keep the rule-engine
baseline. Human approval promotes a prompt; retain
`optimized/should_decompose/v<N-1>/` for rollback via symlink swap. No DSPy
optimizer or standalone `eval.py` is claimed as current; the implemented CLI
`operation: evaluate` and split metric functions are the available evaluation
surfaces.

## 11. Open questions

- Does a future caller provide worker-pool size or tier mix? (Architectus owner.)
- Should confidence become a gate? It is currently unused and unscored.
- Does the future DSPy seam coexist with or replace the rule engine? A config
  selector is the current v2.1 assumption.

## 12. Changelog

| Version | Date | Change |
|---|---|---|
| 0.1.0 | 2026-08-09 | Initial spec |
| 1.0.0 | 2026-08-09 | Aligned scaffold, exact-match metric, combined dataset, and rule engine |
| 1.1.0 | 2026-08-10 | Domain enum, stable wire boolean, split dataset |
| 1.2.0 | 2026-08-10 | CLI, module gate, isolation, baseline/digests, wheel, and removal |

## Appendix A. Reference implementation evidence

### A.1 Rule-engine details

`decide.py` defines `ACTION_VERBS` as `add`, `update`, `refactor`, `implement`,
`migrate`, `build`, `fix`, `create`, `remove`, `rewrite`, `backfill`,
`introduce`, `restructure`, `split`, and `port`. `HIGH_SIGNAL` is
`multiple`, `several`, `both`, `subtasks`, `components`, `services`,
`independently`, `in parallel`, `separately`, and `decompose`. Sentence
splitting uses `[.;]` followed by whitespace; action clauses split on comma or
semicolon. File references recognize `py`, `rs`, `ts`, `js`, `go`, `toml`,
`json`, `yaml`, `md`, and `sh`/`sql` extensions. Itemized lists match a leading
dash/star or `N)`/`N.`. These details matter for reproducibility and are part of
the current rule-engine evidence profile.

The context short-circuit executes before all signals. A positive threshold
returns `reason="; ".join(reasons) or "evidence threshold met"` and confidence
`0.8`. The negative default is exactly
`reason="task is atomic or already scoped"`, confidence `0.7`. Empty or
garbage strings are tolerated by the pure function, but the CLI rejects an
empty task. No provider, retry, cache, or model fallback occurs in v2.

### A.2 Dataset loader contract

`ExampleDatasetLoader` exposes `load()`, `load_split(Split)`, `load_all()`, and
`dataset_version`. `Split` has `TRAIN`, `EVAL`, and `CANARIES`. Split-aware
loads read `datasets/<split>.jsonl`; the explicit fallback is
`datasets/example_pairs.jsonl`. Train/eval filter out canaries, while the
canaries split returns only records marked `canary: true`. `load_all()` checks
canonical `(task, context)` collisions across all three tuples and returns a
`DatasetBundle` with `dataset_version` from `meta.json`.

The loader rejects unsupported metadata schema versions, invalid JSON
constants, non-object records, missing IDs in split files, duplicate IDs,
record/schema/dataset version drift, invalid `split`, and wrong input/expected
types. A metadata read failure is a `DatasetError`; absent metadata is allowed
only for legacy unversioned files. A future version mismatch must be reported
as an owner reconciliation failure instead of repaired in documentation.

### A.3 Current metadata and baseline anchor

`datasets/meta.json` records:

```json
{
  "schema_version": 1,
  "dataset_version": "1.1.0",
  "eval_frozen_at": "2026-08-09",
  "canary_frozen_at": "2026-08-09",
  "sibling_pins": {}
}
```

Its split digests are the exact maps committed with the dataset; the baseline
must copy them. The baseline summary currently records mean/std `1.0/0.0` for
each split, counts 200/50/10, total 260 records, `decompose_true: 128`,
`decompose_false: 132`, duplicate and cross-split counts of zero, 10 canaries,
taxonomy coverage `1.0`, and `failed: 0`. Runtime fields are Python `3.14.7`
and pytest `9.1.1`; drift thresholds are metric delta `0.05`, p90 ratio `1.5`,
canary failure delta `0`, and zero duplicates/leaks. The baseline's recorded
SHA is provenance only; it must not be presented as the current branch SHA.

### A.4 CLI wire and evaluation operations

The direct request is an input object. `operation: decide` takes an `inputs`
array and returns `results` with one stable output per input. `operation:
evaluate` takes a `records` array, validates each `input`, `expected`, and
boolean `canary`, runs `ShouldDecomposeModule.decide`, maps expected booleans
to `Decision`, and returns `prediction` plus numeric `score` per record. It
rejects missing arrays and unknown operations. JSON is parsed with a duplicate
key hook at every nesting level. Success emits no stderr; errors emit a JSON
error object and one diagnostic line on stderr. These are the implemented
evaluation surfaces; no separate `eval.py` is claimed.

### A.5 Colocated acceptance evidence

`test_example_module.py` checks decision members, the read-only `decompose`
view, leading-separator tolerance, enum mapping, exact-match metric, malformed
records, missing reasons, perfect legacy-dataset scoring, canary processing,
and denied subprocess network clients. `test_dataset_splits.py` checks split
counts, canary filtering, duplicate/cross-split rejection, metadata defaults
and errors, record-version drift, event-loop-safe `evaluate_split` behavior,
and perfect scoring over all 260 records. `test_example_cli.py` checks direct,
decide, and evaluate requests, optional context default, duplicate keys,
unknown/typed inputs, malformed documents, and strict stdout shape.

Exact-byte split digest ownership is outside `test_dataset_splits.py`: the
shared `module_conformance` gate anchors metadata, baseline, and current split
content, while `scripts/check_dataset_v1.py` covers schema/version/count,
cross-split, secret, and engine-consistency checks.

The conformance gate runs before the module tests and also scans sibling and
reverse imports. It must be invoked with the package-directory selector
`example`; arbitrary pytest arguments are rejected. The offline environment
removes credentials and plugin injection and denies ordinary socket access.
This offline guard is a **BEST-EFFORT, deterministic lint-style check for
common forms of accidental network use; it is not a security boundary. It
CANNOT prevent a hostile same-UID module from bypassing the check with
os.system, posix_spawn, raw sockets, subprocess monkey-patching, or by killing
a same-UID tracer. The harness does not start such a tracer or provide an
in-harness sandbox. Real containment is the deployment-layer boundary.**
`module-test` verification runs from the source checkout; no installed-wheel
probe exists. Complete removal includes
the package code, CLI, architecture, datasets, tests, baseline, and metadata.

### A.6 Acceptance status boundaries

The deterministic engine, loader, metric, split files, baseline schema, JSON
CLI, offline checks, import prohibitions, and removability checks (wheel delivery
removed) are
implemented surfaces. A production `Architectus.execute` caller, DSPy class,
standalone module eval command, sibling stubs, optimized prompt artifacts,
and end-to-end orchestrator exercise remain future work. The live checker
confirms that split records, metadata, and baseline use `dataset_version:
"1.1.0"`; a future version or digest mismatch must fail and report the owner,
not be silently re-anchored.

## Appendix B. Failure and optimization review

### B.1 Failure classification

The module's failures are deliberately separated by layer. A malformed JSONL
line, missing expected field, invalid boolean, duplicate ID, or stale split
digest is a dataset boundary failure and stops evaluation. An empty task is a
caller/CLI validation failure; the pure engine's tolerance of arbitrary string
content is not a substitute for caller validation. A context containing an
explicit prior decomposition is a successful intentional short-circuit, not a
failure. Over- and under-decomposition are domain errors detected by canaries.
Only a future DSPy implementation can produce `ModelUnavailable` or
`MalformedLLMResponse`; the current rule engine has no such path. This avoids
documenting an unimplemented atomic fallback as if it were live behavior.

### B.2 Metric and canary review

The v2 exact-match floor is intentionally not a reason-quality or confidence
metric. A replacement that emits a polished rationale but the wrong
`Decision` scores zero. The 10 split canaries include keyword decoys,
keyword-free workstreams, context suppression, near-duplicate contradictory
labels, long atomic prose, and an itemized migration. A candidate is not
promoted merely because train/eval mean is high: every canary remains in the
aggregate and the conformance/test gate checks that the canary records were
loaded and processed. A v2.1 composite may score confidence or rationale only
after a schema/dataset version decision and held-out re-evaluation.

### B.3 Future optimization evidence

The target optimization record names module version, optimizer, model, seed,
temperature, dataset version, split digests, sibling pins, train/eval means,
canary pass rate, and human approval. SIMBA/GEPA may read `train.jsonl`, but
`eval.jsonl` remains held out and `canaries.jsonl` is loaded at promotion. The
candidate must beat the rule-engine baseline on frozen eval (default target
`0.85`) and pass all canaries. Optimize against a single named model at
`temperature=0.0`, not a provider cascade, to avoid cross-model prompt
transfer. A failed gate retains the existing deterministic engine. Promotion
is a versioned pointer swap under `optimized/should_decompose/`; the previous
pointer remains available for rollback. No sibling is currently pinned because
this is the first module; later interface changes require re-evaluating every
module that pins it.

The reference package is intentionally independently removable. Its imports
may use `cambium.modules.base` and its own package, but not sibling decision
packages. Harness production code, `bench.py`, `scripts/`, and `tools/` must
not import it directly; neutral CLI/data boundaries are used instead. This
keeps the example from becoming an implicit dependency for future modules.

### B.4 Stable wire and event boundaries

The stable response fields are `confidence`, `decompose`, and `reason`, with
JSON numbers/booleans/strings only. The in-process `Decision` enum never leaks
as an enum name or integer. A caller that needs the domain value uses
`output.decision`; a serializer uses `output.decompose`. The `reason` is
recorded for audit but is not a hidden metric input. If a future event store
records the result, it must use the repository's existing redaction and store
boundary; this module does not create an event log, DLQ, or persistence layer.

### B.5 Reproduction and change policy

A rule change that changes any of the 260 labels, split scores, canary result,
or loader interpretation is an evaluation change. The author records the
old/new rule, bumps the dataset version as required, regenerates exact split
digests, reruns the colocated suite and conformance gate, and updates the
baseline only after review. A documentation-only wording change must not alter
dataset bytes or imply a new score. A package-name change is an interface
change: update the `module-test` selector and every neutral CLI
boundary before merge.
