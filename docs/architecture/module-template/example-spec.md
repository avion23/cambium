# Example Module Spec — `should_decompose`

**Status:** Reference example. The first module implemented in Cambium. This spec is **build-ready against the existing scaffold** at `src/cambium/modules/example/` in `main` (read there for the authoritative implementation). This document is the spec; the code is the implementation. Where the two disagree, the code wins — file an issue and update this spec.

---

## 0. Why this module first

`should_decompose` is the right reference example for four reasons:

1. **It closes a critical flaw.** It directly resolves LLM-C6 ("no do-not-decompose path"). Every task in Cambium passes through it. Without it, the v2 architecture is incomplete.

2. **It is small and well-bounded.** A single `Decision` classification, one decision per call. No subprocess management, no git, no IPC. The v2 implementation is a **rule engine** — a DSPy program is a *future seam*, not the v2 primary.

3. **It has a clean metric.** Exact-match scoring on a labeled dataset, computable without human-in-the-loop scoring on the dataset. No coupled dependencies on worker competence (LLM-C4 does not bite here — `siblings-stub.yaml` is empty).

4. **It exercises the full per-module pattern.** `Module` ABC + `decide()` + `metric()` + `DatasetLoader` + `Example` + dataset file + canary entries + DSPy seam. Building it first validates the pattern every future module follows.

The merge sequencer was considered as an alternative. It is **not** the right pick: it is not LLM-driven (no DSPy seam to speak of), has no dataset, and lives in the Deterministic Layer rather than the Orchestrator Layer.

---

## 1. Module Identity

| Field | Value |
|---|---|
| Code | M6.A (submodule of Architectus, M6) |
| Name | `should_decompose` |
| Layer | Orchestrator |
| Owner | TBD (assigned at build time) |
| Status | Spec'd; scaffold merged into `main` |
| Reference code path | `src/cambium/modules/example/` |

---

## 2. Purpose

Decide whether a task spec should be dispatched atomically (one worker) or decomposed into parallel subtasks (multiple workers + merge). The decision is a single boolean; the module also emits a human-readable reason and a confidence value for audit and future calibration.

**Failure mode of the system if this module did not exist:** every task — including trivially atomic ones like "rename this function" — pays the full cost of decomposition (one LLM call) + parallel dispatch (N worktrees, N process spawns, N ReAct loops) + serial merge (N rebases + N test runs, see §7.8). Over-decomposition of coherent tasks also produces workers that each see only a fragment of design intent, yielding inconsistent APIs and integration conflicts at merge time (LLM review C6).

---

## 3. Interfaces

The interfaces in this section are **authoritative for v2** because the scaffold at `src/cambium/modules/example/` already implements them. They are not negotiable in v2; changes require a v2.1 spec bump.

### 3.1 Inputs

```python
# src/cambium/modules/example/decide.py
@dataclass(frozen=True, slots=True)
class TaskInput:
    """Input schema for the should_decompose module."""
    task: str
    context: str = ""
```

| Field | Type | Validation |
|---|---|---|
| `task` | `str` | non-empty after `strip()`; the dataset loader rejects empty strings as invalid records (the rule engine itself tolerates arbitrary strings, but a real caller must pass a real spec) |
| `context` | `str` | optional; defaults to `""`. If non-empty, the rule engine searches it for `"subtask"` / `"decompos"` and short-circuits to `Decision.DO_NOT_DECOMPOSE` (an explicit prior decomposition in the context wins). |

> **Note on the absent `task_kind_hint` field.** An earlier draft of this spec proposed `task_kind_hint: str` (an allowlist: feature/bugfix/refactor/test/docs). The scaffold does not carry that field — `TaskInput` has only `task, context`. To comply with the enum-not-string rule in `agents.md` §7 (no allowlist-with-string disguised as a domain type) **and** to stay aligned with the scaffold, the field is **dropped** in v2 rather than re-typed. If a future caller needs task-kind hints, that is a v2.1 extension: add `task_kind: TaskKind` (enum) to `TaskInput`, gated by a schema-version bump in the dataset.

Source: produced by `Architectus.execute` from the host's task spec.

### 3.2 Outputs

```python
# src/cambium/modules/example/decide.py
@dataclass(frozen=True, slots=True)
class DecomposeOutput:
    """Prediction: whether the task should be decomposed into subtasks."""
    decision: Decision
    reason: str
    confidence: float = 1.0

    @property
    def decompose(self) -> bool:
        """Read-only wire-compatibility view; domain code uses ``decision``."""
        return self.decision is Decision.DECOMPOSE
```

| Field | Type | Notes |
|---|---|---|
| `decision` | `Decision` | The domain decision. `Decision.DECOMPOSE` → orchestrator dispatches `TaskDecomposer`; `Decision.DO_NOT_DECOMPOSE` → one worker atomically. |
| `decompose` | `bool` | Read-only compatibility view. The JSON wire field `expected.decompose` remains a boolean; it is not the domain model. |
| `reason` | `str` | Human-readable justification produced by the rule engine (e.g., `"three or more distinct requirement clauses; long task description"`). Recorded in the canonical event store for audit. Not scored by the metric. |
| `confidence` | `float` | In `[0.0, 1.0]`. The v2 rule engine emits one of three fixed values (`0.7`, `0.8`, `0.9`) corresponding to evidence tiers; a future DSPy replacement may emit calibrated probabilities. **Not scored by the v2 metric** (exact-match only); v2.1 may add a calibration signal. |

Consumers:
- `Architectus.execute` reads `decision` to choose between the atomic fast path and the decomposition path.
- The canonical event store records the decision (redacted via §12.3 of `architecture.md`) as a `should_decompose_decision` event for offline analysis. M1 removes the slice-only event-log path.

### 3.3 Module class — the v2 contract

```python
# src/cambium/modules/base.py (excerpt)
class Module(ABC):
    """A Cambium decision module. Seed of the per-module pattern."""
    name: str

    @abstractmethod
    async def decide(self, input: Any) -> Output: ...

    @abstractmethod
    def metric(self, example: Example) -> float: ...

# src/cambium/modules/example/decide.py (excerpt)
class ShouldDecomposeModule(Module):
    """Reference decision module: should a task be decomposed?

    Pure rule engine today; a DSPy program may replace the engine behind
    this interface later.
    """
    name = "should_decompose"

    async def decide(self, input: TaskInput) -> DecomposeOutput:
        return should_decompose(input.task, input.context)

    def metric(self, example: Example) -> float:
        return should_decompose_metric(example)
```

This is the **DSPy seam**: `decide` is the only surface a replacement program must implement. Callers, the dataset, the metric, and the loader all stay unchanged when a DSPy classification program replaces the rule engine in a future version.

### 3.4 Errors

The v2 rule engine is a pure function and does not raise under normal operation — `should_decompose(task, context)` returns a `DecomposeOutput` for any `str` input (including empty/garbage, which yield `Decision.DO_NOT_DECOMPOSE` with a low-confidence reason). The dataset loader (`ExampleDatasetLoader`) is the only error source:

```python
class DatasetError(ValueError):
    """Raised when a dataset file is unreadable or schema-invalid."""
```

Raised by the loader for: unreadable file, invalid JSON per line, non-object records, missing `input`/`expected` keys, `input.task` not a string, `expected.decompose` not a boolean, `expected.reason` not a string, `canary` not a boolean. A `DatasetError` aborts the eval harness — broken datasets are a hard gate.

A future DSPy-backed `decide` may raise:
- `ModelUnavailable` — all `Diffundo` providers exhausted. Caller (`Architectus`) falls back to `DecomposeOutput(decision=Decision.DO_NOT_DECOMPOSE, reason="model unavailable; atomic dispatch is the safe default", confidence=0.0)`. Atomic is the cheaper error.
- `MalformedLLMResponse` — unparseable output after `MAX_RETRIES`. Same fallback.

These are **v2.1**; the v2 rule engine does not raise them.

---

## 4. State

This module is **stateless across calls**. The `ShouldDecomposeModule` instance holds only its `name` (a class attribute). No caches, no counters, no mutable state. The rule engine (`should_decompose`) is a pure function of `(task, context)`.

This makes the module trivially parallelizable (multiple `decide()` calls can run in any thread or process) and gives a future DSPy replacement a clean stateless target — the `decide` contract is synchronous with respect to module state.

---

## 5. Decision Rules (v2 rule engine)

The primary v2 implementation is a rule engine in `src/cambium/modules/example/decide.py`. The engine accumulates evidence from six signals; `decompose == True` iff total evidence ≥ 2.

| Signal | Condition | Evidence |
|---|---|---|
| Requirement clauses | 3+ sentences (split on `.`/`;`) | +1 |
| Length | `task` > 220 chars | +1 |
| Parallel-work keywords | 2+ of `HIGH_SIGNAL` (`multiple`, `several`, `both`, `subtasks`, `components`, `services`, `independently`, `in parallel`, `separately`, `decompose`) | +1 |
| Per-item phrasing | word `each` | +1 |
| File references | 3+ file paths (matched by extension) in `task` | +1 |
| Itemized list | 3+ numbered/bulleted items | +2 |
| Verb-led workstreams | 3+ clauses starting with an action verb (`add`, `update`, `refactor`, `implement`, `migrate`, `build`, `fix`, `create`, `remove`, `rewrite`, `backfill`, `introduce`, `restructure`, `split`, `port`) | +2 |
| Verb-led workstreams | exactly 2 such clauses | +1 |

**Short-circuit:** if `context` already mentions `subtask` or `decompos*`, the engine returns `Decision.DO_NOT_DECOMPOSE` with `reason="context already provides a decomposition"`, `confidence=0.9`. An explicit prior decomposition in the context wins outright.

**Output:** `evidence >= 2` → `DecomposeOutput(decision=Decision.DECOMPOSE, reason="; ".join(reasons) or "evidence threshold met", confidence=0.8)`. Otherwise → `DecomposeOutput(decision=Decision.DO_NOT_DECOMPOSE, reason="task is atomic or already scoped", confidence=0.7)`. The wire mapping remains `true`/`false` in `expected.decompose`.

### 5.1 The DSPy seam (v2.1+)

`decide()` is the only surface a future DSPy program must implement. When the seam is exercised, the replacement will:

- Implement `async def decide(self, input: TaskInput) -> DecomposeOutput` on a `ShouldDecomposeModuleDSPy(Module)` subclass.
- Use DSPy idioms correctly. Concretely, configure the LM via `dspy.configure(lm=CambiumLM(diffundo, tier="fast", temperature=0.0))` (see `architecture.md` §9.3) — **not** by mutating `dspy.settings.context` — and read prediction fields via attribute access (`pred.decompose`, not `pred.dict()`):
  ```python
  class ShouldDecomposeSignature(dspy.Signature):
      """Classify whether a task should be decomposed into parallel subtasks."""
      task: str = dspy.InputField()
      context: str = dspy.InputField()
      decompose: bool = dspy.OutputField(desc="True if decomposition is worth the cost.")
      reason: str = dspy.OutputField(desc="One-sentence justification.")

  class ShouldDecomposeModuleDSPy(ShouldDecomposeModule):
      def __init__(self, diffundo: Diffundo):
          dspy.configure(lm=CambiumLM(diffundo, tier="fast", temperature=0.0))
          self._clf = dspy.ChainOfThought(ShouldDecomposeSignature)

      async def decide(self, input: TaskInput) -> DecomposeOutput:
          pred = self._clf(task=input.task, context=input.context)
          # attribute access on dspy.Prediction (NOT pred.dict(), which does not exist)
          return DecomposeOutput(
              decision=(Decision.DECOMPOSE
                        if bool(pred.decompose)
                        else Decision.DO_NOT_DECOMPOSE),
              reason=str(pred.reason),
              confidence=0.9,   # placeholder until calibration is added
          )
  ```
- Preserve the dataset, loader, and metric unchanged. `should_decompose_metric` keeps scoring exact match on the domain `Decision`; the wire boolean remains unchanged.
- Be optimized via `Ascensus` against the v2.1 split (`train.jsonl` / `eval.jsonl` / `canaries.jsonl`).

This is **not implemented in v2**. The seam is documented here so the rule-engine choice is explicit and the future swap is mechanical.

---

## 6. Metric (v2)

```python
# src/cambium/modules/example/metric.py
def should_decompose_metric(example: Example) -> float:
    """Score one example in [0, 1]; exact match on the decision wins.

    Returns 0.0 for unprocessed examples (no prediction) and for records
    whose expected value is not a `Decision`. The `reason` field is not
    scored; the domain decision is what matters.
    """
    prediction = example.prediction
    if prediction is None:
        return 0.0
    from .decide import Decision

    expected = example.expected.get("decompose")
    if not isinstance(expected, Decision) or not isinstance(prediction.decision, Decision):
        return 0.0
    return 1.0 if prediction.decision == expected else 0.0
```

The v2 metric is **exact match on the domain `Decision`**, full stop. This is deliberately simple:

- It is **computable without an LLM judge**, which is the right floor for a v2 reference module.
- It is **not gameable by a keyword-greedy replacement**: the canaries are deliberately misaligned with the rule engine's surface heuristics (see §7), so a program that memorizes the rules' keyword set still gets the canaries wrong.
- It is **non-zero only when a prediction is attached**. The eval harness attaches predictions by running `decide()` over each `Example`, then scores the `Decision` with `metric()`.

**v2.1 extension (labeled, opt-in).** Once a DSPy replacement exists, the metric may be extended to a multi-signal composite (accuracy + calibration + reason-keyword coverage), matching the shape of `architecture.md` §10. The exact-match floor stays; additional signals layer on top. Changes to the JSON record shape require a `schema_version` bump; changes that affect evaluation require a `dataset_version` bump and a re-eval against the frozen held-out set.

---

## 7. Dataset

| Item | Value |
|---|---|
| Current files | `src/cambium/modules/example/datasets/train.jsonl`, `src/cambium/modules/example/datasets/eval.jsonl`, and `src/cambium/modules/example/datasets/canaries.jsonl` |
| Current records | 200 train, 50 eval, 10 canaries; `meta.json` records dataset version `1.1.0` |
| Legacy file | `src/cambium/modules/example/datasets/example_pairs.jsonl` (9 records, 2 canaries; loader fallback) |
| Loader | `ExampleDatasetLoader` (`src/cambium/modules/example/dataset.py`) |
| Format | JSONL, one record per line, UTF-8, no BOM, trailing newline |
| Provenance | Hand-authored; split canaries deliberately misaligned with surface heuristics |
| Sibling pinning | None (`siblings-stub.yaml` is empty / absent — `should_decompose` is the first module) |

**Schema** (authoritative; matches the loader's `_validate`):

```jsonc
{
  "input": {"task": str, "context": str},
  "expected": {"decompose": bool, "reason": str},
  "canary": bool              // optional, default false
}
```

The loader (in `dataset.py`) enforces:
- Top-level keys `{"input", "expected"}` present; both must be JSON objects.
- `input.task` must be a string.
- `expected.decompose` must be a boolean.
- `expected.reason` must be a string.
- `canary` (if present) must be a boolean.

After validation, the loader maps the wire boolean in `expected.decompose` to
`Decision.DECOMPOSE` or `Decision.DO_NOT_DECOMPOSE` in the domain `Example`.
The `schema_version` remains `1`; the current dataset release is
`dataset_version` `1.1.0`. Anything else raises `DatasetError` at load time.

**Reference record (non-canary):**

```json
{"input": {"task": "Update the batch scheduler so it retries failed jobs with exponential backoff. Add per-tenant rate limiting to the public API. Write a migration to backfill timestamps for existing rows. Run the migration on staging and verify throughput.", "context": ""}, "expected": {"decompose": true, "reason": "four independent workstreams"}}
```

**Reference canary (keyword-greedy trap):**

```json
{"input": {"task": "Run the full test suite and commit the fix. This covers the flaky integration tests for multiple services, several providers, both the API and CLI paths, and a few database migrations.", "context": ""}, "expected": {"decompose": false, "reason": "keyword-heavy but atomic: run tests, commit"}, "canary": true}
```

This canary hits four `HIGH_SIGNAL` keywords (`multiple`, `several`, `both`, `services`) and would naïvely score `decompose=True` under a keyword-counting baseline. The gold label is `False` — the task is atomic (run tests, commit). A reward-hacking replacement that learns "keyword count → decompose" fails this canary.

**Reference canary (no-surface-keyword trap):**

```json
{"input": {"task": "Update the payment service to support refunds, add a retry queue for failed webhooks, migrate the billing schema, and backfill historical invoices for the last two years.", "context": ""}, "expected": {"decompose": true, "reason": "four verb-led workstreams with no surface keywords"}, "canary": true}
```

This canary has zero `HIGH_SIGNAL` keyword hits — a keyword-greedy replacement would say `decompose=False`. The gold label is `True` because of four verb-led workstreams. The rule engine's verb-clause signal catches it; a DSPy replacement that drops verb-clause analysis fails this canary.

### 7.1 Current split-aware dataset policy

The current reference module uses the frozen `train.jsonl`, `eval.jsonl`, and
`canaries.jsonl` files under `src/cambium/modules/example/datasets/`, with
`meta.json` carrying the schema and dataset versions. The loader excludes
canaries from train/eval and rejects duplicate IDs and cross-split collisions.

`example_pairs.jsonl` remains a legacy combined-file fallback for compatibility
with the original v2 records. It is not the current source of the train/eval
metrics. All formats retain the same JSON wire field:
`expected.decompose: true|false`; the in-memory domain value is `Decision`.

**Dataset-format compliance.** The split files use the full envelope (`id`,
`schema_version`, `dataset_version`, `split`, `added_at`, `added_by`, `source`,
`license`, `redacted`, `input`, `expected`, and optional canary metadata). The
legacy combined file uses the loader's minimal `{input, expected, canary?}`
shape and remains covered by the compatibility tests.

### 7.2 Refresh policy

- The dataset is loaded by every test and eval run; adding records is safe (the loader is order-independent).
- Removing records is not permitted in v2; deprecate via gold-label flip + a `notes` field instead.
- Adding canaries is encouraged and requires review by someone other than the canary author (two-reviewer rule, `dataset-format.md` §8).
- A schema change (e.g., adding `task_kind`) requires a loader update + a `DatasetError`-free re-load + a scenario-test update, all in one commit.

---

## 8. Failure Modes

| Mode | Trigger | Symptom | Detection | Recovery |
|---|---|---|---|---|
| Over-decomposition (keyword greed) | A rule/DSPy program over-weights `HIGH_SIGNAL` keywords | Atomic-but-keyword-dense task is split | Canary `keyword-heavy but atomic: run tests, commit` (decompose=false) | If rule engine: tune evidence weights; if DSPy: reject optimized variant at promotion gate |
| Under-decomposition (surface-blind) | A rule/DSPy program under-weights verb-clauses | Multi-workstream task with no surface keywords stays whole | Canary `four verb-led workstreams with no surface keywords` (decompose=true) | Same |
| Garbage input (structurally invalid record) | Hand-edit error, bad mining | Loader fails mid-record | `DatasetError` from `ExampleDatasetLoader._validate` | Hard gate — fix the record, do not catch |
| Garbage input (string content) | Caller passes weird/garbage `task` string | Rule engine returns `Decision.DO_NOT_DECOMPOSE, confidence=0.7` for nonsense | Caller-side validation (rule engine tolerates any string by design) | Caller decides whether to escalate |
| Reward hacking (future DSPy eval) | Optimizer memorizes surface heuristics | High train metric, canary failures | Canary suite (the two `canary: true` records) | Reject optimized prompt; promote previous version |
| Context already decomposed | Caller passes `context` with "subtask"/"decompos*" | Engine short-circuits to `Decision.DO_NOT_DECOMPOSE, confidence=0.9` | Intentional; no recovery needed | None — by design |
| Empty task | `TaskInput(task="")` | Engine returns `Decision.DO_NOT_DECOMPOSE` | Caller-side validation | Caller should reject empty tasks upstream |

A future DSPy `decide` adds two more (see §3.4): `ModelUnavailable` and `MalformedLLMResponse`, both with the atomic-dispatch fallback.

---

## 9. Test Strategy

### 9.1 Colocated module tests

The module suite is colocated under `src/cambium/modules/example/tests/` and
has no mocking or network access:

- `test_example_module.py` loads the legacy combined dataset, verifies the
  `Decision` mapping and compatibility view, runs `decide()` over every pair,
  and asserts a perfect metric including canaries.
- `test_dataset_splits.py` loads the current 200/50/10 split files, checks the
  `meta.json` dataset version, rejects duplicate and cross-split records, and
  verifies the full 260-record metric.

Malformed records raise `DatasetError`; the colocated suite is the module's
scenario and integration gate.

### 9.2 Eval harness (v2.1)

A standalone eval entry point remains a future optimization-harness surface.
The current split-aware loader and colocated tests already separate train,
eval, and canary data and score each split through the module metric.

### 9.3 Canary suite (v2.1)

Canaries are loaded from `src/cambium/modules/example/datasets/canaries.jsonl`
and scored inline with the module metric. A canary miss drops the aggregate
score below 1.0 and fails the colocated gate.

### 9.4 Integration

Shared supervisor, store, merge, IPC, worker, and CLI scenarios live in
`tests/scenarios/`. The M1 deletion set removes the slice-only event-log and fallback paths;
those scenarios must target the canonical runtime surfaces. Until
`Architectus.execute` is wired, the colocated module suite (§9.1) is the
integration gate for `should_decompose`.

### 9.5 Sibling pinning

N/A. `should_decompose` is the first module; no siblings to pin. The `siblings-stub.yaml` is absent; the optimization harness (when it lands in v2.1) skips sibling-stub loading for this module.

### 9.6 Verification commands (per `agents.md` §5)

```
# Run from repo root, on a Python 3.14 interpreter:
uv run --python 3.14.7 --extra test pytest src/cambium/modules/example/tests -v
uv run --python 3.14.7 --extra test pytest -q          # whole suite
```

A passing run is the gate. The reference scaffold on `main` runs green against these commands.

---

## 10. Optimization Plan (v2.1)

Not in v2 scope. Documented here as the v2.1 target so the seam is clear:

- **Optimizer:** `dspy.SIMBA(metric=should_decompose_metric, max_steps=12, max_demos=8, num_threads=4)`.
- **Train set:** `src/cambium/modules/example/datasets/train.jsonl` (200 records).
- **Eval gate:** mean metric on `src/cambium/modules/example/datasets/eval.jsonl` ≥ 0.85 (exact match on the domain `Decision`).
- **Canary gate:** 100% pass on `src/cambium/modules/example/datasets/canaries.jsonl`.
- **Human gate:** an optimized prompt is promoted only after a diff of the prompt change is reviewed in the optimization PR.
- **Rollback:** promotion is a symlink swap under `optimized/should_decompose/`; the previous version is retained at `optimized/should_decompose/v<N-1>/` and the production pointer can be reverted atomically.
- **Model pinning:** optimization runs against a single named model (declared in the optimization run manifest) at `temperature=0.0` — not against the cascade. This avoids the cross-model prompt-transfer problem (LLM-C3) during optimization. Production serves via cascade as usual.
- **Baseline:** the rule engine's exact-match score on the train split is the baseline a DSPy replacement must beat. If the DSPy variant does not beat the rule engine on the held-out eval set, the rule engine stays in production.

---

## 11. Open Questions

- Q: Does `should_decompose` see the available worker pool (size, tiers) before deciding? Currently no; it decides on `(task, context)` alone. (Owner: `Architectus` author. Resolution deferred to v2.1.)
- Q: Should `confidence` ever be scored or used to gate the decision (e.g., "ambiguous → ask upstream")? Currently unscored and unused. (Owner: orchestrator owner.)
- Q: Should we add a "soft decompose" path that dispatches 2 subtasks instead of N? Not in v2; would require a different downstream contract. (Owner: future.)
- Q: When the v2.1 DSPy seam is implemented, does it live alongside the rule engine (selector flag) or replace it? Current assumption: alongside, with a config flag picking which `ShouldDecomposeModule` subclass to instantiate. (Owner: v2.1.)

---

## 12. Implementation Notes (for the parallel agent extending this module)

The reference implementation already exists at `src/cambium/modules/example/`:

```
src/cambium/modules/example/
├── __init__.py                # exports: ShouldDecomposeModule, TaskInput,
│                              #          Decision, DecomposeOutput,
│                              #          should_decompose,
│                              #          should_decompose_metric,
│                              #          ExampleDatasetLoader
├── architecture.md            # the in-tree per-module architecture doc
│                              #   (this spec is the canonical version; the
│                              #   in-tree doc is a shorter reference)
├── decide.py                  # Decision enum, rule engine, module + dataclasses
├── metric.py                  # should_decompose_metric (exact match)
├── dataset.py                 # ExampleDatasetLoader and split handling
├── tests/
│   ├── test_example_module.py
│   ├── test_dataset_splits.py
│   └── baselines/baseline.json
└── datasets/
    ├── train.jsonl            # 200 records
    ├── eval.jsonl              # 50 frozen records
    ├── canaries.jsonl          # 10 canaries
    ├── meta.json               # schema_version 1, dataset_version 1.1.0
    └── example_pairs.jsonl     # legacy 9-record fallback
```

**Extensions that align with this spec but are NOT yet in the scaffold** (label as v2.1, do not implement in v2):

- A standalone eval entry point — split loading and metric scoring already exist; only the dedicated CLI/report surface is future work.
- `decide.py` DSPy subclass — `ShouldDecomposeModuleDSPy`, behind a config flag.
- `siblings-stub.yaml` — empty placeholder; added when this module becomes siblings with another.
- `optimized/should_decompose/v<N>/` — promoted-prompt artifacts, written by `Ascensus`.

**Acceptance gate (per `agents.md` §9) — already met by the merged scaffold:**

1. ✅ `architecture.md` committed (in-tree).
2. ✅ Split dataset committed (`datasets/train.jsonl`, `datasets/eval.jsonl`, `datasets/canaries.jsonl`, and `datasets/meta.json`); legacy combined data remains as a fallback.
3. ✅ Loader maps the stable wire boolean to `Decision`; `DatasetError` is raised on malformed records.
4. ✅ Rule-engine `decide()` scores 1.0 over all 260 split records, including canaries.
5. ✅ Colocated suite passes: `uv run --python 3.14.7 --extra test pytest src/cambium/modules/example/tests -v` exits 0.
6. ⏳ End-to-end orchestrator exercise — pending `Architectus.execute` wiring.
7. ⏳ Adversarial review of this module — this spec is the reviewed artifact; re-review on any contract change.
8. ✅ All verifiable items above marked VERIFIED with the cited command from §9.6.

---

## 13. Changelog

| Version | Date | Change |
|---|---|---|
| 0.1.0 | 2026-08-09 | Initial spec; described a custom `ShouldDecompose` DSPy module. |
| 1.0.0 | 2026-08-09 | Aligned to the merged scaffold: `ShouldDecomposeModule(Module)` ABC, `TaskInput`/`DecomposeOutput` dataclasses, `should_decompose_metric` exact-match, single-file dataset with inline canaries, rule-engine primary with DSPy as a documented v2.1 seam. |
| 1.1.0 | 2026-08-10 | Aligned to the colocated split-aware tests, `Decision` domain enum, stable boolean wire format, and current module dataset. |
