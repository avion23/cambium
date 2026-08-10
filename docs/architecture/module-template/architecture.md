# Module Architecture — Template

**Status: NORMATIVE TARGET.** Copy this document to
`src/cambium/modules/<name>/architecture.md` and complete every section. Empty
sections are not acceptable; use `N/A — <reason>` when a section does not
apply. The target is a removable, colocated module, not a claim about every
module currently present in the checkout.

## 1. Module identity

| Field | Required value |
|---|---|
| Code | Catalog code from `docs/architecture/architecture.md` §4, or `new` |
| Name (Latin) | For example, `Architectus.should_decompose` |
| Logical module name | Stable dataset, metric, baseline, and report identity |
| Python package name | Import path under `cambium.modules` |
| Layer | Deterministic \| Orchestrator \| Worker \| View \| Tooling (offline) |
| Owner | Initials or agent name; update on handoff |
| Status | Draft \| In review \| Build-ready \| Done |
| Version | Semver of the primary implementation (for example, `decide.py`) |

Logical and package names are separate contract values. Do not derive one from
the other. The reference logical name is `should_decompose`, while its package
is `cambium.modules.example` and its module-test selector is `example`.

## 2. Purpose

Write one paragraph describing the decision or transformation and the system
failure if it does not exist.

## 3. Interfaces

### 3.1 Inputs

Inputs are fields on a frozen dataclass or typed `decide()` parameters; untyped
`dict` input is prohibited. For every field record its producer, validation
(length, range, and charset), and invalid-input behavior.

```python
@dataclass(frozen=True)
class <Module>Input:
    field_a: str
    field_b: int
```

### 3.2 Outputs

Outputs are a frozen typed dataclass. State each consumer, invariant, and JSON
wire representation.

```python
@dataclass(frozen=True)
class <Module>Output:
    decision: <DecisionEnum>
    confidence: float  # [0.0, 1.0]
    rationale: str
```

Closed domain alternatives use enums. The `Decision` pattern in
`src/cambium/modules/example/decide.py` is normative: keep the enum in the
domain model and preserve the existing wire representation at the boundary.

### 3.3 Errors

List typed exceptions and the named boundary that catches each one. Do not let
module errors escape into the supervisor event loop.

```python
class <Module>Error(Exception): ...
class InvalidInput(<Module>Error): ...
```

### 3.4 Tool surface

`TOOL_SCHEMAS` in `src/cambium/schemas.py` and dispatch in
`src/cambium/tools.py` are the canonical tool surfaces. A module adding a tool
extends those registries; it does not create a parallel contract.

### 3.5 JSON CLI

Every decision module ships `__main__.py` as its wire adapter. It must read one
JSON object, require non-empty `input.task`, allow optional string
`input.context`, reject unknown fields, duplicate keys, malformed JSON, and bad
input with exit 1, and write exactly one JSON object plus newline to stdout.
Diagnostics go to stderr. The adapter must preserve stable wire fields, avoid
providers and network access, and work without the checkout on `sys.path`.

```console
$ printf '%s\n' '{"task":"Fix the typo.","context":""}' \
    | python -m cambium.modules.<package_name>
{"confidence":0.7,"decompose":false,"reason":"task is atomic or already scoped"}
```

## 4. State

Declare scope, mutation path, and persistence in a table. Deterministic-layer
modules must not own LLM-derived state; orchestrator state is per-session and
is never mutated from a worker process. If there is no state, say:
"This module is stateless across calls; only the primary implementation is
held on the instance."

## 5. DSPy program

### 5.1 Primary and seam

Every v2 module subclasses `cambium.modules.base.Module` and implements
`async decide(input) -> Output` and `metric(example: Example) -> float`. The
primary may be a pure function or deterministic rule engine; DSPy is not
required for v2. `decide` is the replacement seam, so callers, loader, dataset,
and metric remain stable.

```python
class Module(ABC):
    name: str
    async def decide(self, input: Any) -> Output: ...
    def metric(self, example: Example) -> float: ...
```

### 5.2 Signature and replacement (when applicable)

Document a future `dspy.Signature` or write `N/A — no DSPy seam in v2`.
Replacement modules use the same interface and read `dspy.Prediction`
attributes directly. Configure a `CambiumLM` through `dspy.configure(lm=...)`;
do not construct `dspy.LM` directly or mutate `dspy.settings.context`.

### 5.3 LLM and determinism

All LLM calls route through `Diffundo` and its `CambiumLM`. State temperature,
top-p, and seed policy; defaults are `temperature=0.0` for classifiers and
evaluators and `0.2` for generative modules.

## 6. Metric

Define a deterministic function `(example: Example) -> float in [0, 1]`.
`Example` carries `input`, `expected`, and a prediction attached by the caller.
It must work without human scoring for optimization. Document signal weights,
gameability, and the canaries that detect each unwanted behavior.

The reference exact-match floor is:

```python
def metric(example: Example) -> float:
    if example.prediction is None:
        return 0.0
    expected = example.expected.get("decision")
    prediction = example.prediction
    if not isinstance(expected, <DecisionEnum>) or not isinstance(
        prediction.decision, <DecisionEnum>
    ):
        return 0.0
    return 1.0 if prediction.decision is expected else 0.0
```

## 7. Dataset

Refer to `dataset-format.md` and record:

- paths and layout (`{train,eval,canaries}.jsonl`; a legacy
  `<name>_pairs.jsonl` is an explicit fallback only);
- train, frozen eval, and canary counts;
- hand-authored/mined provenance and schema version;
- deterministic split procedure and canary markers;
- sibling versions pinned in `siblings-stub.yaml`/`meta.json`; and
- who may add records, review policy, and refresh cadence.

## 8. Failure modes

List at least five rows with trigger, symptom, detection, and recovery. Include
invalid input, malformed model output where relevant, metric failure, and
canary/reward-hacking behavior. Recovery must name the boundary; do not hide a
cause behind a catch-all default.

## 9. Test strategy

Tests are colocated in `src/cambium/modules/<name>/tests/`, baselines in
`tests/baselines/`, and shared runtime scenarios in `tests/scenarios/`. A
module is removable by deleting its complete directory; shared scenarios stay.

### 9.1 Unit and integration tests

Cover at least three happy paths, every failure mode, empty/max/unicode input,
and deterministic output. The colocated scenario loads the real dataset,
checks schema plus a negative `DatasetError`, runs every record (including
canaries), and asserts the declared aggregate threshold.

### 9.2 Eval and canaries

In v2, colocated tests are the eval-harness substitute. A v2.1 target may add
`python -m cambium.modules.<name>.eval` and `--suite canaries`; any canary
failure exits non-zero and canary pass rate gates promotion.

### 9.3 Module conformance

Run:

```console
uv run --extra test cambium module-test <package_name>
# reference:
uv run --extra test cambium module-test example
```

The live `module_conformance` gate validates tracked layout, datasets,
baselines, imports, JSON CLI, subprocess isolation, and module-scoped tests.
This offline guard is a **BEST-EFFORT, deterministic lint-style check for common forms of
accidental network use; it is not a security boundary. It CANNOT prevent a hostile
same-UID module from bypassing the check with os.system, posix_spawn, raw sockets,
subprocess monkey-patching, or by killing a same-UID tracer. The harness does not start
such a tracer or provide an in-harness sandbox. Real containment is the deployment-layer
boundary.**
Sibling imports and reverse imports from `bench.py`, `scripts/`, and `tools/` are static failures.

### 9.4 Baseline, wheel, and removal

Each baseline JSON must contain `schema_version`, logical `module`,
`dataset_version`, `split_digests`, `git_sha`, `date`, `python`, `pytest`,
`metric`, `canaries`, `dataset`, `tests`, and `drift_thresholds`. Its digests
and version must match `datasets/meta.json` and exact split bytes. The wheel
includes package code, `__main__.py`, `architecture.md`, datasets, metadata,
colocated tests, and baseline; the wheel acceptance probe runs
`cambium module-test <package_name>` outside the checkout. The tool is
developed and run directly from source; the Hatch wheel target and wheel
acceptance tests remain for packaging, which is not the primary delivery path.

## 10. Optimization plan

State optimizer (`dspy.SIMBA`, `dspy.GEPA`, or custom), train size, max steps,
max demos, held-out threshold, and 100% canary gate. Human approval is
required for promotion; retain `optimized/<name>/v<N-1>/` and promote by
symlink swap for rollback. Optimize against pinned siblings and a single
named model at the declared deterministic settings.

## 11. Open questions

List concrete questions, owner, and decision needed from the orchestrator,
sibling owner, or infrastructure.

## 12. Changelog

| Version | Date | Change |
|---|---|---|
| 0.1.0 | YYYY-MM-DD | Initial draft |
| 0.2.0 | 2026-08-10 | Normative colocated tests, enum/wire boundary, and current runtime boundaries |
| 0.3.0 | 2026-08-10 | JSON CLI, conformance, isolation, baseline, wheel, removal, and package naming |

## Appendix A. Required evidence and boundary notes

The architecture document is a contract, not a catalogue of planned names.
Trace a module from its route/command/import entry point and cite the live
caller or state that the caller is still a target. Matching role names in the
architecture are not proof that a caller exists. A reference module may state
that it is first or has no siblings, but it must not claim a production
orchestrator path without a source symbol and test.

The current shared surfaces that a module may use are:

| Surface | Live boundary |
|---|---|
| module ABC and examples | `src/cambium/modules/base.py` and `src/cambium/modules/example/` |
| JSON schemas and tools | `src/cambium/schemas.py` and `src/cambium/tools.py` (`TOOL_DISPATCH`) |
| benchmark/conformance | `src/cambium/bench.py` and `src/cambium/module_conformance.py` |
| CLI | `src/cambium/cli.py:main` |
| runtime | `src/cambium/ipc.py`, `worker.py`, `supervisor.py`, `tasktree.py` |
| state/control | `store.py`, `conversations.py`, `approval.py`, `provider_config.py` |

Do not add a module-specific schema registry, dispatch path, cache, or
resource-budget abstraction when the shared boundary already owns it. The
checkout has `resources.py` with `CompileGate`; it has no `ResourceBudget`
class. It has no `eval_cache.py` and no separate DLQ module. These absences are
current facts, not invitations to document dead capability claims.

### A.1 Interface and wire checklist

The author must answer, in the module document, all of the following: who
constructs each input; whether whitespace, length, Unicode, and numeric bounds
are checked; which exception is raised and where it is caught; which consumer
reads each output; and which fields are retained at the wire boundary. A
domain enum must not be replaced with a string allowlist or integer merely to
make JSON easier. A compatibility boolean is allowed only at that boundary.

The JSON adapter is a process boundary. It must reject duplicate object keys
instead of keeping the last value, reject unknown fields, emit no logs on
stdout, and avoid importing providers. A direct probe and a malformed-input
probe belong in the colocated test. If a module has an `evaluate` operation,
document its input/output and scoring path; do not invent a standalone eval
entry point unless `__main__.py` implements it.

### A.2 Dataset and baseline evidence

Section §7 must state exact train/eval/canary counts, source and license,
schema/dataset versions, split seed or curation rule, freeze dates, digest
source, sibling pins, and refresh authority. Section §9.4 must name the
baseline file and all required fields. A baseline `git_sha` identifies the run
that produced it; it is not evidence that the current checkout is at that SHA.
When records, metadata, and baseline disagree, state the mismatch and the
owner action. Never quietly rewrite a frozen record from a documentation edit.

### A.3 Failure and test evidence

Failure tables must distinguish a malformed boundary record, a deterministic
domain result, a provider/model failure in a future seam, and a metric/canary
gate failure. Recovery names the owner and boundary; it is not a default that
turns a broken input into a successful result. Tests must cover both the
module's removable directory and shared contracts: module tests may be deleted
with the module, while shared scenario tests remain in `tests/scenarios/`.

The conformance gate is a layout and isolation check before it is a pytest
check. It validates tracked files, manifest, dataset versions/digests, imports,
CLI subprocess behavior, and the loaded module set. Offline checks are
best-effort lint-style checks, not same-UID containment. Sibling imports and
reverse imports are static failures and must be reported by file, line, and
symbol. The wheel acceptance probe must work outside the repository; a
repository-relative fallback is not a packaging solution. The tool is developed
and run directly from source; the Hatch wheel target and wheel acceptance tests
remain for packaging, which is not the primary delivery path.

### A.4 Target-state labels

Use **normative target** for this template and requirements that every new
module must meet. Use **implemented** only when a source symbol and focused
test prove the behavior. Use **v2.1 target** for standalone eval commands,
DSPy replacements, optimizer artifacts, or sibling stubs that are not in the
current module. This vocabulary keeps a copied architecture document from
turning planned capability into a false current-state claim.

The same label discipline applies to benchmark results: a measured count is
evidence only for the command, worktree, Python, pytest, dataset version, and
split bytes recorded with it. A historical research note may preserve a
result, but it must not present that result as a current-main run after the
baseline or source tree changes.
