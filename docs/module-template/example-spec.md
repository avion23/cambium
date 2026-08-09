# Example Module Spec — `ShouldDecompose`

**Status:** Reference example. The first module to implement after the smoke-test harness lands. Demonstrates the template in `architecture.md` and the dataset format in `dataset-format.md`. The reference implementation will live at `src/cambium/modules/should_decompose/`, built by a parallel agent. This document is the spec; the code is the implementation.

---

## 0. Why this module first

`ShouldDecompose` is the right reference example for four reasons:

1. **It closes a critical flaw.** It directly resolves LLM-C6 ("no do-not-decompose path"). Every task in Cambium passes through it. Without it, the v2 architecture is incomplete.

2. **It is small and well-bounded.** A single binary classification, one LLM call per invocation. No subprocess management, no git, no IPC. The implementation surface is roughly 150 lines of Python.

3. **It has a clean metric.** Accuracy + calibration + format-validity, all computable without human-in-the-loop scoring on a hand-labeled dataset. No coupled dependencies on worker competence (LLM-C4 does not bite here).

4. **It exercises the full template.** Per-module architecture, dataset format, splits, canaries, sibling pinning (none needed), eval harness, optimization plan. Building it first validates the template itself before the harder modules (`TaskDecomposer`, worker ReAct) consume it.

The merge sequencer was considered as an alternative. It is **not** the right pick: it is not LLM-driven (no DSPy program), has no dataset, and exercises a different part of the template. `ShouldDecompose` exercises every part.

---

## 1. Module Identity

| Field | Value |
|---|---|
| Code | M6.A (submodule of Architectus, M6) |
| Name | `Architectus.should_decompose` |
| Layer | Orchestrator |
| Owner | TBD (assigned at build time) |
| Status | Spec'd (build-ready) |
| Version | 0.1.0 |

---

## 2. Purpose

Decide whether a task spec should be decomposed into parallel subtasks or dispatched as a single atomic unit to one worker. The decision is binary; the module also emits a calibrated confidence and a one-sentence rationale for audit.

**Failure mode of the system if this module did not exist:** every task — including trivially atomic ones like "rename this function" or "fix this typo" — pays the full cost of decomposition (one LLM call) + parallel dispatch (N worktrees, N process spawns, N ReAct loops) + serial merge (N rebases + N test runs). Over-decomposition of coherent tasks also produces workers that each see only a fragment of design intent, yielding inconsistent APIs and integration conflicts at merge time (LLM review C6).

---

## 3. Interfaces

### 3.1 Inputs

```python
@dataclass(frozen=True)
class ShouldDecomposeInput:
    spec: str                # the task specification; non-empty; ≤ 16_000 chars
    repo_context: str = ""   # short repository context summary; ≤ 4_000 chars
    task_kind_hint: str = "" # optional hint: "feature" | "bugfix" | "refactor" | "test" | "docs"
```

Validation:
- `spec` is non-empty after `strip()`. Invalid → `InvalidInput`.
- `len(spec) <= 16_000`. Invalid → `InvalidInput` (caller should chunk first).
- `repo_context` length ≤ 4_000. Invalid → truncated by caller before invocation; module raises on violation.
- `task_kind_hint` if non-empty must be in the allowlist. Invalid → ignored with a warning event.

Source: produced by `Architectus.execute` from the host's task spec.

### 3.2 Outputs

```python
class ShouldDecomposeDecision(enum.Enum):
    DISPATCH_ATOMIC = "dispatch_atomic"
    DECOMPOSE = "decompose"

@dataclass(frozen=True)
class ShouldDecomposeOutput:
    decision: ShouldDecomposeDecision
    confidence: float                 # calibrated probability in [0.0, 1.0]
    rationale: str                    # one sentence; ≤ 500 chars
    raw_llm_response: dict            # for trajectory recording; redacted
```

Invariants the consumer (`Architectus`) relies on:
- `0.0 <= confidence <= 1.0`. Enforced by post-processing; out-of-range values are clipped and the discrepancy is logged as a warning event.
- `len(rationale) <= 500`. Truncated with ellipsis if the model rambles.
- `decision` is one of the two enum values; never a string.
- `raw_llm_response` is JSON-serializable and has been through the redaction filter (`docs/architecture.md` §12.3).

Consumers:
- `Architectus.execute` reads `decision` to choose between the atomic fast path and the decomposition path.
- The event log records the full `ShouldDecomposeOutput` as a `should_decompose_decision` event for offline analysis.

### 3.3 Errors

```python
class ShouldDecomposeError(Exception): ...
class InvalidInput(ShouldDecomposeError): ...
class MalformedLLMResponse(ShouldDecomposeError):
    """LLM returned unparseable output after max_retries attempts."""
class ModelUnavailable(ShouldDecomposeError):
    """All Diffundo providers exhausted; cannot classify."""
```

`Architectus.execute` catches `ModelUnavailable` and **falls back to `DISPATCH_ATOMIC`** with `confidence=0.0` and rationale `"model unavailable; atomic dispatch is the safe default"`. Rationale for the fallback: an atomic task costs one worker; a wrongly-decomposed task costs N workers and a merge — atomic is the cheaper error.

---

## 4. State

This module is **stateless across calls**. Only the DSPy program (read-only at runtime) is held on the instance.

```python
class ShouldDecompose(dspy.Module):
    def __init__(self, diffundo: Diffundo):
        self._diffundo = diffundo
        self._classifier = dspy.ChainOfThought(ShouldDecomposeSignature)
        self._lm = CambiumLM(diffundo, tier="fast", temperature=0.0)
```

No caches, no counters, no mutable state. The DSPy program is loaded from `optimized/should_decompose/v<N>/` at construction time and never replaced during a session.

---

## 5. DSPy Program

### 5.1 Signature

```python
class ShouldDecomposeSignature(dspy.Signature):
    """Classify whether a task spec should be decomposed into parallel subtasks.

    A task SHOULD be decomposed (should_decompose=True) when ALL of:
      - it contains >=3 distinct, independently-implementable units of work, AND
      - those units touch disjoint files or modules, AND
      - the cost of merge (rebase + test per branch) is less than the latency
        saving of parallel implementation.

    A task should NOT be decomposed (should_decompose=False) when ANY of:
      - it is a single-file, single-function change,
      - it requires whole-design coherence to be correct (e.g., introducing a
        new abstraction),
      - the spec is short and unambiguous,
      - decomposition would create inter-worker API conflicts.
    """
    spec: str = dspy.InputField(desc="The task specification, <=16k chars.")
    repo_context: str = dspy.InputField(
        desc="Repository context summary, <=4k chars. May be empty.")
    task_kind_hint: str = dspy.InputField(
        desc="Optional hint: feature|bugfix|refactor|test|docs. May be empty.")

    should_decompose: bool = dspy.OutputField(
        desc="True if decomposition is worth the cost; False otherwise.")
    rationale: str = dspy.OutputField(
        desc="One sentence justifying the decision. <=500 chars.")
    confidence: float = dspy.OutputField(
        desc="Calibrated probability the decision is correct. In [0.0, 1.0].")
```

### 5.2 Module class

```python
class ShouldDecompose(dspy.Module):
    MAX_RETRIES = 3

    def __init__(self, diffundo: Diffundo):
        self._diffundo = diffundo
        self._classifier = dspy.ChainOfThought(ShouldDecomposeSignature)
        self._lm = CambiumLM(diffundo, tier="fast", temperature=0.0)

    def forward(self, inp: ShouldDecomposeInput) -> ShouldDecomposeOutput:
        self._validate(inp)
        dspy.settings.context["lm"] = self._lm

        last_err: Exception | None = None
        for attempt in range(self.MAX_RETRIES):
            try:
                pred = self._classifier(
                    spec=inp.spec,
                    repo_context=inp.repo_context,
                    task_kind_hint=inp.task_kind_hint,
                )
                return self._coerce(pred, inp)
            except (ModelUnavailable, InvalidInput):
                raise
            except Exception as e:               # malformed response, parse error, etc.
                last_err = e
                continue
        raise MalformedLLMResponse(
            f"Failed to parse LLM response after {self.MAX_RETRIES} attempts: {last_err}")

    def _validate(self, inp: ShouldDecomposeInput) -> None:
        if not inp.spec or not inp.spec.strip():
            raise InvalidInput("spec is empty")
        if len(inp.spec) > 16_000:
            raise InvalidInput(f"spec too long: {len(inp.spec)} > 16_000")

    def _coerce(self, pred, inp: ShouldDecomposeInput) -> ShouldDecomposeOutput:
        decision = ShouldDecomposeDecision.DECOMPOSE if bool(pred.should_decompose) \
                   else ShouldDecomposeDecision.DISPATCH_ATOMIC
        raw_conf = float(pred.confidence)
        if not (0.0 <= raw_conf <= 1.0):
            # Log the discrepancy, clip, and continue. Calibration metric will catch this.
            logger.warning("should_decompose: confidence out of range",
                           extra={"task_id": ..., "raw": raw_conf})
            raw_conf = max(0.0, min(1.0, raw_conf))
        rationale = pred.rationale.strip()[:500]
        return ShouldDecomposeOutput(
            decision=decision,
            confidence=raw_conf,
            rationale=rationale,
            raw_llm_response=redact(pred.dict()),
        )
```

### 5.3 LLM access

All LLM calls go through `self._lm`, a `CambiumLM` wrapping the injected `Diffundo` instance. The module never calls `dspy.LM(...)` or imports provider SDKs. Tier `"fast"` is used: this is a classification call where latency matters and capability floor is low.

### 5.4 Determinism

`temperature=0.0`. The classifier must give the same output for the same input across runs (modulo provider non-determinism, which is recorded as a `calibration_drift` event). The eval harness asserts determinism on a fixed 10-record subset and fails on any divergence.

---

## 6. Metric

```python
def metric(output: ShouldDecomposeOutput,
           reference: ShouldDecomposeReference) -> float:
    """Composite: accuracy (0.6) + calibration (0.2) + format (0.1) + rationale (0.1)."""
    accuracy = 1.0 if output.decision == reference.expected_decision else 0.0
    calibration = 1.0 - abs(output.confidence - reference.expected_confidence)
    calibration = max(0.0, calibration)
    format_ok = 1.0 if (0.0 <= output.confidence <= 1.0
                       and len(output.rationale) <= 500) else 0.0
    rationale_quality = _rationale_quality(output.rationale, reference.rationale_keywords)
    return 0.6 * accuracy + 0.2 * calibration + 0.1 * format_ok + 0.1 * rationale_quality


def _rationale_quality(rationale: str, keywords: tuple[str, ...]) -> float:
    if not keywords:
        return 1.0 if 50 <= len(rationale) <= 500 else 0.0
    hits = sum(1 for k in keywords if k.lower() in rationale.lower())
    return hits / len(keywords)
```

| Signal | Weight | Why |
|---|---|---|
| Accuracy | 0.6 | The decision is the whole point. |
| Calibration | 0.2 | An uncertain decision routed atomically is safe; an over-confident wrong decision is dangerous. |
| Format | 0.1 | Out-of-range confidence pollutes downstream logic; long rationales pollute logs. |
| Rationale quality | 0.1 | Forces the model to articulate reasoning, which improves auditability and resists pure-pattern-matching. |

**Gameability analysis.** A pure-accuracy metric rewards a model that always says "decompose" or always says "atomic," whichever is more common in train. Calibration weight resists this: a model that is right 70% of the time but always outputs `confidence=0.99` scores worse than one that outputs calibrated confidences. The rationale-quality weight resists a model that emits empty strings.

**Canaries** (see §7 and `dataset-format.md` §6): every canary exists to detect a specific gaming pattern.

---

## 7. Dataset

| File | Records | Notes |
|---|---|---|
| `datasets/train.jsonl` | 200 | Hand-authored (100) + mined from synthetic event logs (50) + paraphrased real specs (50, redacted). |
| `datasets/eval.jsonl` | 50 | Frozen at first commit; never used for training. |
| `datasets/canaries.jsonl` | 15 | Reward-hacking traps. |

**Provenance:**
- Hand-authored: written by the module owner, covering the obvious cases (single-function refactor, multi-feature epic, ambiguous one-liner, etc.).
- Mined: specs are generated by a synthetic-spec generator (deterministic seed), then hand-labeled.
- Paraphrased real specs: derived from internal Cambium event logs; all PII, repo URLs, and author identifiers scrubbed.

**Schema version:** `1`.

**Dataset version:** `1.0.0` at first commit.

**Splits:** produced by `scripts/split_dataset.py --module should_decompose --seed 1337`. The seed is documented; the split is reproducible.

**Sibling pinning:** `siblings-stub.yaml` is empty (`{}`). `ShouldDecompose` is the first module in the pipeline; it has no upstream sibling. (This is one of the reasons it is the right reference example — no sibling-coupling to worry about.)

**Refresh policy:** the module owner may add records to `train.jsonl` at any time (minor version bump). `eval.jsonl` changes require sign-off from the orchestrator owner. Canary additions require a second reviewer.

**Reference record (train):**

```json
{
  "id": "should_decompose-0001",
  "schema_version": 1,
  "dataset_version": "1.0.0",
  "split": "train",
  "added_at": "2026-08-09",
  "added_by": "human:architect",
  "source": "hand-authored",
  "license": "internal",
  "redacted": false,
  "data": {
    "spec": "Rename the function `calc_dry_run` to `compute_dry_run` in src/dry_run.rs. Update the one call site in main.rs.",
    "repo_context": "Single crate, Rust.",
    "task_kind_hint": "refactor",
    "expected_decision": false,
    "expected_confidence": 0.95,
    "rationale_keywords": ["single-function", "rename", "one-call-site"]
  },
  "notes": "Trivially atomic; the canonical 'no' example."
}
```

**Reference canary:**

```json
{
  "id": "should_decompose-canary-01",
  "schema_version": 1,
  "dataset_version": "1.0.0",
  "split": "canary",
  "added_at": "2026-08-09",
  "added_by": "human:architect",
  "source": "hand-authored",
  "license": "internal",
  "redacted": false,
  "data": {
    "spec": "Refactor function `foo` to use a list comprehension. Single file, single function, no other call sites.",
    "repo_context": "",
    "task_kind_hint": "refactor",
    "expected_decision": false,
    "expected_confidence": 0.9,
    "rationale_keywords": ["single-function", "atomic"],
    "canary": {
      "kind": "trivially_atomic",
      "anti_expected": true,
      "anti_expected_confidence_range": [0.5, 1.0],
      "description": "A prompt optimized for high decomposition rate will say 'yes' here. We trap that."
    }
  }
}
```

---

## 8. Failure Modes

| Mode | Trigger | Symptom | Detection | Recovery |
|---|---|---|---|---|
| LLM returns malformed JSON | provider drift; weak model in tier | `dspy` parse error | `try/except` in `forward()` | Re-prompt up to `MAX_RETRIES=3`; then `MalformedLLMResponse` |
| Provider outage | rate limit, network | `Diffundo.AllProvidersFailed` | propagates as `ModelUnavailable` | Caller (`Architectus`) falls back to `DISPATCH_ATOMIC` |
| Empty `spec` | upstream bug | `InvalidInput` | input validation | Caller catches, logs, escalates |
| Confidence always ~0.5 | under-specified prompt | eval calibration signal < 0.3 | metric on eval set | Re-optimize; if persists, escalate to module owner |
| Confidence always ~0.99 | over-confident prompt | calibration signal < 0.3 | metric on eval set | Re-optimize; canary `ambiguous_calibration` should fail |
| Always-True prompt | gamed train set | canary `trivially_atomic` fails | canary suite | Reject optimized prompt at promotion gate |
| Always-False prompt | gamed train set | canary `must_decompose` fails | canary suite | Reject optimized prompt at promotion gate |
| Rationale keyword-stuffed | gamed metric | rationale has gold keywords but wrong decision | canary `keyword_hack` | Reject; metric weight on rationale drops to 0 for that variant |
| Spec exceeds 16k chars | upstream chunking bug | `InvalidInput` | validation | Caller chunks and retries |

---

## 9. Test Strategy

### 9.1 Unit tests (`tests/unit/test_should_decompose.py`)

- Happy path: 5 inputs covering `decision=true` and `decision=false`.
- Empty spec → `InvalidInput`.
- Oversized spec → `InvalidInput`.
- Confidence out of range → clipped, warning logged, output still produced.
- Malformed LLM response 3× → `MalformedLLMResponse`.
- `ModelUnavailable` propagates from `Diffundo`.
- Determinism: same input → same output under `temperature=0` on a 10-record subset.
- Rationale truncation at 500 chars.

All LLM calls in unit tests use a **fake LLM** (`cambium.tests.fake_llm`) returning canned responses — no real API calls in CI.

### 9.2 Eval harness

```
python -m cambium.modules.should_decompose.eval
```

Runs the metric over `eval.jsonl`. Prints per-signal breakdown (accuracy, calibration, format, rationale). Exit code 0 iff mean metric ≥ `0.80` and per-signal accuracy ≥ `0.70`.

### 9.3 Canary suite

```
python -m cambium.modules.should_decompose.eval --suite canaries
```

Runs the metric over `canaries.jsonl`. **Any canary failure exits non-zero.** No aggregate score is computed; canaries are pass/fail.

### 9.4 Integration

The smoke test (`cambium.tests.smoke`) exercises `ShouldDecompose` end-to-end: a fake task spec is submitted, `Architectus.execute` calls `ShouldDecompose`, the chosen path (atomic or decomposed) is taken, the result is asserted. The fake LLM returns `should_decompose=false` deterministically for the smoke-test spec.

### 9.5 Sibling pinning

N/A. `ShouldDecompose` is the first module; no siblings to pin. The `siblings-stub.yaml` is `{}` and the optimization harness skips sibling-stub loading for this module.

---

## 10. Optimization Plan

- **Optimizer:** `dspy.SIMBA(metric=metric, max_steps=12, max_demos=8, num_threads=4)`.
- **Train set:** `datasets/train.jsonl` (200 records).
- **Eval gate:** mean metric on `eval.jsonl` ≥ `0.80`; per-signal accuracy ≥ `0.70`; calibration ≥ `0.50`.
- **Canary gate:** 100% pass on `canaries.jsonl`.
- **Human gate:** an optimized prompt is promoted only after a diff of the prompt change is reviewed and signed off in the optimization PR.
- **Rollback:** promotion is a symlink swap under `optimized/should_decompose/`; the previous version is retained at `optimized/should_decompose/v<N-1>/` and the production pointer can be reverted atomically.
- **Model pinning:** optimization runs against `tier="fast"` with `temperature=0.0` on a single named model (declared in the optimization run manifest) — not against the cascade. This avoids the cross-model prompt-transfer problem (LLM-C3) during optimization. Production serves via the cascade as usual.

---

## 11. Open Questions

- Q: Should `ShouldDecompose` see the available worker pool (size, tiers) before deciding? Currently no; it decides on the spec alone. (Owner: `Architectus` author. Resolution deferred to v2.1 — the current design is correct for the common case.)
- Q: Should the confidence threshold for "definitely decompose" vs "ask human" be configurable per session? Currently hardcoded at 0.5 (the decision boundary). (Owner: orchestrator owner.)
- Q: Should we add a "soft decompose" path that dispatches 2 subtasks instead of N? Not in v2; would require a different downstream contract. (Owner: future.)

---

## 12. Implementation Notes (for the parallel agent building this)

The reference implementation will live at `src/cambium/modules/should_decompose/`:

```
src/cambium/modules/should_decompose/
├── architecture.md            ← copy of docs/module-template/example-spec.md,
│                                adapted as the module's own architecture.md
├── __init__.py                ← exports ShouldDecompose, Input/Output, errors
├── program.py                 ← ShouldDecomposeSignature, ShouldDecompose class
├── metric.py                  ← metric(), _rationale_quality()
├── eval.py                    ← `python -m cambium.modules.should_decompose.eval`
├── datasets/
│   ├── train.jsonl            ← 200 records, schema v1, dataset v1.0.0
│   ├── eval.jsonl             ← 50 records, frozen
│   ├── canaries.jsonl         ← 15 records
│   └── meta.json              ← schema/dataset versions, sibling_pins={}
├── tests/
│   └── test_should_decompose.py
└── siblings-stub.yaml         ← {}
```

**Acceptance gate (per `agents.md` §9):**

1. `architecture.md` committed (copy of this spec).
2. All three datasets committed with explicit versions in `meta.json`.
3. `python -m cambium.modules.should_decompose.eval` exits 0 on the frozen eval set.
4. `python -m cambium.modules.should_decompose.eval --suite canaries` exits 0.
5. `python -m pytest src/cambium/modules/should_decompose/tests/ -v` exits 0.
6. `python -m cambium.tests.smoke` exits 0 with `ShouldDecompose` wired in.
7. Adversarial review committed under `docs/reviews/` (or this spec updated to reflect findings).
8. All of the above marked VERIFIED with cited commands.

---

## 13. Changelog

| Version | Date | Change |
|---|---|---|
| 0.1.0 | 2026-08-09 | Initial spec. |
