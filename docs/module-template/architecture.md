# Module Architecture — Template

**Status:** Normative template. Every DSPy module in Cambium (`ShouldDecompose`, `TaskDecomposer`, `TaskRouter`, `ResultEvaluator`, `Opifex` ReAct, future modules) ships a `src/cambium/modules/<name>/architecture.md` filled out against this template.

> Copy this file to `src/cambium/modules/<name>/architecture.md` and fill in every section. Empty sections are not acceptable; write "N/A — <reason>" if a section genuinely does not apply.

---

## 1. Module Identity

| Field | Value |
|---|---|
| Code | M? (assign from `docs/architecture.md` §4 catalog, or "new" if not listed) |
| Name (Latin) | e.g., `Architectus.should_decompose` |
| Layer | Deterministic \| Orchestrator \| Worker \| View \| Tooling (offline) |
| Owner | Initials or agent name; transferred on handoff |
| Status | Draft \| In review \| Build-ready \| Done |
| Version | semver of this module's `program.py` (e.g., `0.1.0`) |

---

## 2. Purpose

One paragraph. State the decision the module makes or the transformation it performs. State the **failure mode of the system if this module did not exist**.

Example (`ShouldDecompose`): "Decides whether a task spec should be decomposed into parallel subtasks or dispatched as a single atomic unit. Without this module, every task — including trivially atomic ones — pays the cost of decomposition + parallel dispatch + serial merge, and the orchestrator over-decomposes coherent tasks into inconsistent fragments."

---

## 3. Interfaces

### 3.1 Inputs

A typed list. Every input is a field on a frozen dataclass or a typed parameter to `forward()`. Untyped `dict` inputs are not permitted.

```python
@dataclass(frozen=True)
class <Module>Input:
    field_a: str
    field_b: int
    field_c: dict[str, float]   # justified here, not just convenient
```

For each input: source (which module/caller produces it), validation rules (length, range, charset), and what happens on invalid input (raise, default, fallback).

### 3.2 Outputs

```python
@dataclass(frozen=True)
class <Module>Output:
    decision: Literal["yes", "no"]      # use enums for domain alternatives
    confidence: float                    # [0.0, 1.0]
    rationale: str
```

For each output: consumer (which module reads it), invariants the consumer relies on, serialization rules (must be JSON-serializable for the event log).

### 3.3 Errors

Typed exceptions raised by this module. Each must be caught at a named boundary; never let module exceptions escape to the supervisor's event loop.

```python
class <Module>Error(Exception): ...
class InvalidInput(<Module>Error): ...
class ModelUnavailable(<Module>Error): ...   # raised only after Diffundo cascade exhausted
```

---

## 4. State

| State | Scope (per-call / per-instance / per-process) | Mutation path | Persistence |
|---|---|---|---|
| (e.g., cached DSPy program) | per-instance | lazy init in `__init__` | none |

If the module owns **no state** beyond the DSPy program, say so explicitly: "This module is stateless across calls; only the DSPy program (read-only at runtime) is held on the instance."

Modules in the **Deterministic Layer** of Cambium must not own LLM-derived state. Modules in the **Orchestrator Layer** may own per-session state but must not mutate it from a worker process.

---

## 5. DSPy Program

### 5.1 Signature

The DSPy signature, as a string and as a Python class. Reference: DSPy docs for `dspy.Signature`, `dspy.ChainOfThought`, `dspy.ReAct`, `dspy.Predict`.

```python
class ShouldDecomposeSignature(dspy.Signature):
    """Classify whether a task spec should be decomposed."""
    spec: str = dspy.InputField(desc="The task specification.")
    repo_context: str = dspy.InputField(desc="Repository context summary, ≤2k chars.")
    should_decompose: bool = dspy.OutputField(desc="True if decomposition is worth the cost.")
    rationale: str = dspy.OutputField(desc="One-sentence justification.")
    confidence: float = dspy.OutputField(desc="Calibrated probability in [0, 1].")
```

### 5.2 Module class

```python
class <Module>(dspy.Module):
    def __init__(self):
        self.classifier = dspy.ChainOfThought(ShouldDecomposeSignature)

    def forward(self, *, spec: str, repo_context: str = "") -> <Module>Output:
        pred = self.classifier(spec=spec, repo_context=repo_context)
        # Post-processing / validation / type coercion here.
        return <Module>Output(
            decision=pred.should_decompose,
            confidence=float(pred.confidence),
            rationale=pred.rationale.strip(),
        )
```

### 5.3 LLM access

All LLM calls route through `Diffundo` (see `docs/architecture.md` §9). The module receives a `Diffundo`-backed `CambiumLM` from its caller; it never constructs `dspy.LM` directly.

### 5.4 Determinism

State the temperature, top-p, and seed policy. Default: `temperature=0.0` for classifier/evaluator modules; `temperature=0.2` for generative ones. Document any module where determinism is required and how it is enforced.

---

## 6. Metric

A function from `(module_output, reference) -> float in [0, 1]`. Must be **computable without human-in-the-loop scoring** for the automatic optimization path; can use LLM-as-judge as one signal, but the LLM judge must itself be evaluated against a human-graded held-out subset.

```python
def metric(output: <Module>Output, reference: <Module>Reference) -> float:
    """Composite: accuracy (0.7) + calibration (0.2) + format-validity (0.1)."""
    accuracy = 1.0 if output.decision == reference.expected_decision else 0.0
    calibration = 1.0 - abs(output.confidence - reference.expected_confidence)
    format_ok = 1.0 if (0.0 <= output.confidence <= 1.0
                       and len(output.rationale) <= 500) else 0.0
    return 0.7 * accuracy + 0.2 * max(0.0, calibration) + 0.1 * format_ok
```

State:

- Each signal's weight and why.
- Gameability analysis: what does this metric reward that we do not want?
- Canaries (see `dataset-format.md`): which dataset entries are designed to detect each gameable failure mode?

---

## 7. Dataset

Reference: `dataset-format.md` for schema and versioning. In this section, state:

- Dataset path: `src/cambium/modules/<name>/datasets/{train,eval,canaries}.jsonl`
- Train size, eval size (frozen, held-out), canary count.
- Provenance: how were the examples collected? Hand-authored? Mined from production? Both?
- Schema version (`schema_version` field, integer, monotonic).
- Splits: how train/eval/canary were partitioned (deterministic seed).
- Sibling-pinning manifest: which sibling-module versions this dataset was last validated against (`siblings-stub.yaml`).
- Refresh policy: who can add examples, who reviews, how often.

---

## 8. Failure Modes

A table. For each mode: trigger, symptom, detection, recovery.

| Mode | Trigger | Symptom | Detection | Recovery |
|---|---|---|---|---|
| LLM returns malformed JSON | provider drift; weak model | `dspy` parse error | `try/except` in `forward()` | Re-prompt with stricter instruction; if 3× fail, raise `ModelUnavailable` |
| Spec is empty | upstream bug | `InvalidInput` | input validation | Caller catches, logs, escalates |
| Confidence always ~0.5 | under-specified prompt | eval calibration <0.3 | metric | Re-optimize against eval set |

List at least five. If you cannot think of five, the module is under-specified.

---

## 9. Test Strategy

### 9.1 Unit tests

`tests/unit/test_<module>.py`. Cover:

- Happy path (≥3 inputs).
- Each failure mode in §8.
- Boundary conditions (empty input, max-length input, unicode).
- Determinism (same input → same output under `temperature=0`).

### 9.2 Eval harness

`python -m cambium.modules.<name>.eval` runs the metric over the frozen eval set and prints per-signal breakdown. Exit code 0 if mean metric ≥ module's threshold (state the threshold here; default 0.75).

### 9.3 Canary suite

`python -m cambium.modules.<name>.eval --suite canaries` runs the metric over `canaries.jsonl`. **Any canary failure exits non-zero.** Canary pass rate is the gate for promoting an optimized prompt to production.

### 9.4 Integration

Where this module is exercised by the end-to-end smoke test (`cambium.tests.smoke`). If not exercised, justify.

### 9.5 Sibling pinning

How this module is tested against **stub** siblings (frozen references) rather than live co-adapted siblings, per `docs/architecture.md` §17.2.

---

## 10. Optimization Plan

- Optimizer: `dspy.SIMBA` \| `dspy.GEPA` \| custom. State which.
- Train set size, max steps, max demos.
- Eval gate: mean metric on held-out ≥ threshold; canary pass rate 100%.
- Human gate: an optimized prompt is promoted to production only after a human (or human-authorized agent) reviews the eval delta and signs off.
- Rollback: previous production prompt is retained under `optimized/<name>/v<N-1>/`; promotion is a symlink swap.

---

## 11. Open Questions

Concrete, answerable questions that this module cannot resolve in isolation. Each one should be tagged with who can answer it (orchestrator, sibling-module owner, infrastructure).

Example:
- Q: Does `ShouldDecompose` see the worker tier mix before deciding? Currently no. (Owner: `Architectus` author.)

---

## 12. Changelog

| Version | Date | Change |
|---|---|---|
| 0.1.0 | YYYY-MM-DD | Initial draft. |
