# DSPy hill-climbing in Cambium — implementation plan

**Status: SPIKE PLAN (2026-08-18).** The rule-engine, dataset, metric, and
`CambiumLM` contracts below are grounded in the current source. The DSPy
program, optimizer, manifest extension, artifact schema, and optimizer runs are
**UNVERIFIED** until the spike completes.

## Bottom line

This spike is about proving the DSPy seam, not improving classification
accuracy. The current `should_decompose` rule engine scores `1.0` on all three
live splits: train 200, eval 50, and canaries 10, dataset version `1.1.0`.
There is no error signal for SIMBA or GEPA to climb. A harder, non-saturated
dataset slice must exist before an optimizer result can be treated as an
accuracy result.

DSPy `3.3.0` import, configuration, `Predict` construction, and one fake-LM
forward are verified on Python `3.14.7` GIL. DSPy optimizer execution on that
interpreter is **UNVERIFIED**.

## 1. Goal and boundary

The end-to-end path to prove is:

1. Read a module manifest and resolve its DSPy program class.
2. Load the versioned example splits and build DSPy examples without leaking
   canaries.
3. Inject a `CambiumLM`, invoke the program through DSPy, and parse its output
   into Cambium's domain model.
4. Adapt `should_decompose_metric` to the DSPy optimizer callback shape.
5. Run zero-shot and BootstrapFewShot stages under a USD budget.
6. Score the frozen gate splits, write program/LM/report state, and round-trip
   the saved LM state.

This demonstrates plumbing, failure handling, cost accounting, and artifact
recovery. It does not demonstrate that an LM is more accurate than the rule
engine. With a `1.0` baseline on every split, a `1.0` candidate is a tie, not a
hill-climbing gain.

## 2. Interfaces

### 2.1 Existing Cambium boundary

The example module defines:

```python
TaskInput(task: str, context: str = "")
Decision.DECOMPOSE = "decompose"
Decision.DO_NOT_DECOMPOSE = "do_not_decompose"
DecomposeOutput(decision: Decision, reason: str, confidence: float = 1.0)
```

`ExampleDatasetLoader` exposes `Split.TRAIN`, `Split.EVAL`, and
`Split.CANARIES`. Split-aware loading excludes `canary: true` records from
train and eval and returns them only through the canaries split. Its current
counts are 200/50/10. `should_decompose_metric` scores only the `Decision`
enum: an exact decision match is `1.0`; a missing, malformed, or unprocessed
prediction is `0.0`. `reason` is not scored.

### 2.2 DSPy program

The planned file is `src/cambium/modules/example/dspy_program.py`.
**UNVERIFIED:** its concrete lazy-class construction must still be implemented.
The public class shape is:

```python
class ShouldDecomposeModuleDSPy(dspy.Module):
    # LM is injected by the caller; the program does not construct a provider.
    ...
```

Its DSPy signature is exactly:

```text
(task, context) -> decision: Literal["decompose", "do_not_decompose"], reason: str
```

Each call must use `dspy.context(lm=lm)` with the injected LM. It must not rely
on process-global `dspy.configure` state. The adapter maps the two allowed
decision strings to `Decision` and preserves the returned reason. An
unparseable output takes the conservative path:

```text
decision = DO_NOT_DECOMPOSE
confidence = 0.0
```

The program module must not import `dspy` at top level. A bare top-level DSPy
import pulls `openai` into `sys.modules` and trips Cambium's module-conformance
gate. The exact mechanism that keeps the concrete `dspy.Module` subclass lazy
is **UNVERIFIED**; the conformance-safe rule is not.

### 2.3 Manifest seam

`ModuleManifest` gains an optional `dspy_program` field. The example manifest
will name `ShouldDecomposeModuleDSPy`; the planned import-path form is:

```json
"dspy_program":
  "cambium.modules.example.dspy_program:ShouldDecomposeModuleDSPy"
```

The field is optional so ordinary module discovery remains import-free. The
optimizer requires it and must fail explicitly when it is absent; it must not
silently substitute the deterministic rule engine. The field's final parser
and validation rules are **UNVERIFIED**.

### 2.4 Optimizer CLI and public functions

The first CLI surface is:

```console
python3.14 -m cambium.optimize <module_name> \
  --optimizer zero|bootstrap --budget-usd N \
  [--seed N] [--tier NAME] [--dry-run]
```

`--optimizer zero` selects stage 0; `--optimizer bootstrap` selects stage 1.
`--tier` selects the `ProviderTier` used by `CambiumLM`. `--dry-run` must
validate the manifest, dataset, split digests, and planned call path without
making provider calls. Its final exit and report behavior is **UNVERIFIED**.

The optimizer module exposes these public seams (**UNVERIFIED** until
implemented):

| Function | Contract |
|---|---|
| `load_program_class` | Resolve and validate the class named by manifest `dspy_program`; load it only in the optimizer path. |
| `make_dspy_metric` | Return the six-argument DSPy metric adapter. Convert a prediction to the Cambium `Example`/`DecomposeOutput` shape, delegate to `should_decompose_metric`, return a float in `[0, 1]`, and return `0.0` on parse failure. |
| `build_trainsets` | Convert only loader train records to DSPy examples; use a seeded carve of 40 records from the 200-record train split as validation, leaving 160 records for the trainset. |
| `score_split` | Run one explicit split through the program and exact-match metric, returning its aggregate score and per-record evidence needed by the report. |
| `run_stage_zero` | Run the uncompiled, zero-shot program and collect scores, calls, and cost. |
| `run_stage_bootstrap` | Compile a fresh student with the planned BootstrapFewShot limits, then return the compiled program and measurements. |
| `write_artifact` | Write program state, LM state, and the JSON report into the module's single artifact directory, replacing the previous set in place. |

The adapter must keep `cambium.modules.base.Example` and `dspy.Example`
distinct. It must also read DSPy prediction attributes rather than depending on
an unstable dictionary representation.

### 2.5 Artifacts

The target layout is:

```text
optimized/<module>/
├── program.json
├── lm.json
└── report.json
```

`program.json` stores the DSPy program state. `lm.json` stores the trusted
`CambiumLM.dump_state()` result and must contain no credential values.
`report.json` records at least module/version, optimizer/stage, seed, tier,
dataset version, split digests, baseline and candidate scores, canary gate
results, call/cost totals, budget status, promotion decision, and the
`train_gain - canary_gain` diagnostic. Exact report fields are **UNVERIFIED**.

Each run replaces the artifact set in place. A rejected candidate's report
remains auditable with its failed-gate verdict.

## 3. Optimizer ladder

| Stage | Optimizer | Planned limits | Use |
|---|---|---|---|
| 0 | Zero-shot | No demonstrations; approximately 260 record evaluations for the initial full measurement | Prove LM injection, parsing, scoring, cost capture, and artifact writing. |
| 1 | `BootstrapFewShot` | `max_bootstrapped_demos=4`, `max_labeled_demos=8`, `max_rounds=1` | Prove train-only demo construction and program serialization. |
| 2 | `SIMBA` | `max_steps=4`, `num_candidates=4`, `bsize=16`, `max_demos=4` | **UNVERIFIED;** run only after a non-saturated dataset slice exists. |

The approximately 260-call estimate corresponds to the current 200 train, 50
eval, and 10 canary records. It is a planning estimate for a full measurement,
not a provider-call guarantee: retries, gate canary double-scoring, and
provider behavior can change the count. The optimizer's selection data remains
train-only.

SIMBA is not exposed by the first CLI contract until the hard slice and the
3.14.7 compatibility probe pass. GEPA and MIPROv2 are deferred. No optimizer
can produce a meaningful improvement signal from the current all-`1.0` slice.

## 4. Anti-reward-hacking controls

The optimizer boundary is deliberately narrower than the gate boundary:

1. **Train only.** `build_trainsets` receives the loader's train records. The
   loader already filters canaries out of train and eval; add a defensive
   assertion that no train or carved-val example has `canary=True`.
2. **Seeded validation only.** Carve exactly 40 of the 200 train records with
   the supplied seed. The remaining 160 are the optimizer trainset. The carve
   is not a substitute for frozen eval.
3. **Eval is gate-only.** `eval.jsonl` is never passed to an optimizer, demo
   selector, or candidate-selection loop. It is scored only after a candidate
   exists and is used by the promotion gate.
4. **Canaries are gate-only.** Score `canaries.jsonl` on the gate path, twice,
   at `temperature=0.0`. Both passes must be complete and score 100%. A
   temperature of zero is not proof of deterministic output; the second pass
   is an explicit drift check.
5. **Gate errors fail closed.** Gate evaluation uses `max_errors=0` on
   `dspy.Evaluate`. A thrown evaluation error is not converted into a passing
   split. The adapter's required parse-failure score remains `0.0` and must be
   visible in per-record diagnostics.
6. **Demo hashes.** Hash selected demo `(task, context)` payloads with the
   same canonical form used by `ExampleDatasetLoader`, then check those hashes
   against the eval and canary record-digest sets. Any overlap is a hard
   failure. Keep the aggregate eval/canary split SHA-256 digests in the report
   as separate provenance fields.
7. **No response cache.** Use `cache=False` throughout the LM boundary.
   `CambiumLM` forces this on construction, calls, and copies. Enforce the
   CLI's USD budget with each `CallResult.estimated_cost_usd` and stop before
   the run exceeds `--budget-usd N`.
8. **Report the gap.** For each candidate, report:

   ```text
   train_gain       = candidate_train - baseline_train
   canary_gain      = candidate_canaries - baseline_canaries
   anti_reward_gap  = train_gain - canary_gain
   ```

   A positive gap is evidence that training improvement did not transfer to
   canaries. It is a diagnostic, not a replacement for the hard canary gate.

## 5. Promotion gate

A candidate satisfies the metric gate only when all three conditions hold:

```text
eval >= 0.85
AND eval >= baseline_eval - 0.05
AND canaries == 1.0
```

With the current baseline, `baseline_eval == 1.0`, so the drift floor is
`0.95`. A candidate that ties the rule engine at `1.0` but costs more is a
regression, not an improvement. The spike may retain its artifact for
inspection, but it must not promote that costlier tie.

Promotion is an artifact-pointer operation only. Runtime module selection is
outside this spike; no production caller is changed.

## 6. Known pitfalls and compatibility checks

- **SIMBA exception handling.** DSPy optimizer paths can swallow an exception
  and turn it into `0.0`. This can look like a bad candidate rather than an
  infrastructure failure. Record exception counts and the first bounded error
  separately. SIMBA behavior on this exact Cambium path is **UNVERIFIED**.
- **Temperature is not determinism.** `temperature=0.0` does not prove two
  calls produce the same response. Keep the double canary score and report any
  mismatch.
- **Cache-directory import order.** `CambiumLM._load_dspy` redirects
  `DSPY_CACHEDIR` before importing DSPy. Import `cambium.lm` and use that lazy
  path before any bare `import dspy`; otherwise DSPy may create `~/.dspy_cache`
  before Cambium can redirect it.
- **Two `Example` classes.** Alias or qualify
  `cambium.modules.base.Example` and `dspy.Example`; never pass one as the
  other by name alone.
- **Running event loops.** `CambiumLM.forward` refuses a running asyncio event
  loop. Optimizers must run synchronously in the CLI process; do not call the
  synchronous optimizer from an async loop.
- **ChatAdapter fake-LM format.** DSPy `3.3.0` expects field markers in fake
  completions, including `[[ ## decision ## ]]`, `[[ ## reason ## ]]`, and the
  terminating `[[ ## completed ## ]]` marker. A plain JSON string that worked
  for the earlier `Predict` smoke test is not sufficient for this signature.
- **Lazy DSPy import.** A top-level `import dspy` loads `openai` into
  `sys.modules`, which violates the module-conformance import boundary. Keep
  all DSPy loading in the optimizer/program execution path.
- **Python build.** Use regular GIL-enabled Python `3.14.7`; `CambiumLM` rejects
  free-threaded CPython. The optimizer probe must record the exact line
  `SIMBA runs to completion on 3.14.7 GIL` only after that result is observed.

## 7. First-spike scope and exit criteria

The first spike covers only `should_decompose` and its 200/50/10 dataset. It
does not generalize the optimizer to sibling modules.

Exit requires all of the following (**UNVERIFIED** until run):

1. Stage 0 and stage 1 complete under the supplied USD cap without an
   unreported provider or parser error.
2. Both gate canary passes score 100%, with no split or demo-hash violation.
3. Eval is reported at or above `0.85` and within the baseline drift floor;
   any costlier `1.0` tie is rejected as a regression.
4. `CambiumLM.dump_state()` and `CambiumLM.load_state()` round-trip the LM
   state, including tier and budget policy, without writing credentials.
5. `program.json`, `lm.json`, and `report.json` render as valid artifacts, and
   the human-readable run report identifies the stage, scores, gate, cost, and
   artifact path.
6. The compatibility record states whether the exact SIMBA completion line was
   observed. Until a stage-2 probe passes, that item remains **UNVERIFIED** and
   is not an accuracy claim.

Explicit non-goals are:

- no accuracy claim from this saturated dataset;
- no `should_review` DSPy program;
- no GEPA or MIPROv2 implementation or run;
- no composite metric for rationale, confidence, or calibration; and
- no runtime promotion wiring or production caller integration.
