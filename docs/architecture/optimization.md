# Offline prompt optimization

**Status:** DSPy experiments and artifact evaluation are implemented for two
small decision modules. **The coding worker does not load these optimized
artifacts, and its system prompt is not automatically improved by running
`optimize`.**

## What exists

The `should_decompose` and `should_review` modules have rule implementations,
module-owned train/eval/canary datasets, metrics, and optional DSPy predictors.
The decomposition implementation lives in the physical `example` package;
`should_decompose` is its logical optimizer name.

The optimizer supports zero-shot evaluation and the configured bootstrap/GEPA
paths through Cambium's LM adapter. It records usage and estimated spend,
evaluates dataset splits, and saves program/report artifacts. Saved state can
be loaded by the optimizer's evaluation path. It is not silently promoted into
the worker, supervisor, or default rule engine.

The optional [DSPy base](../../src/cambium/modules/dspy_module.py) is an ordinary
`dspy.Module` with one predictor. Only DSPy program modules import it. The
shared [rule-module base](../../src/cambium/modules/base.py) remains importable
without DSPy; constructing a program does not mutate class inheritance.

## Commands and what they prove

Run from the checkout with the appropriate environment installed:

```sh
python -m cambium module-test example
python -m cambium module-test should_review
python -m cambium optimize should_decompose --dry-run
python -m cambium optimize should_review --dry-run
python -m cambium optimize --help
python -m cambium optimize eval --help
```

`module-test` uses the physical package name. It checks the isolated module and
its fixtures; it is not a provider-backed coding benchmark. `--dry-run` resolves
the experiment without constructing an LM, so a passing dry run proves neither
provider connectivity nor useful optimization.

A real predictor call through `CambiumLM` verifies the adapter and structured
output path. A successful compile additionally verifies optimizer execution.
Only a held-out comparison establishes whether the resulting program improves
the chosen metric. None of these alone proves that the complete coding agent
gets better.

## Does DSPy make sense here?

Yes, as an **offline experiment tool** for a bounded decision with reproducible
inputs, executable outcomes, and a baseline. It is not a reason to insert another
model call or classification gate before each small edit, delegation, or finish.
For a decision a small rule already gets right cheaply, keep the rule unless
held-out results justify the added request, latency, and maintenance cost.

For coding-prompt experiments, use whole tasks in disposable repositories:
check the resulting code, relevant tests, and publication. Labels should come
from those outcomes, not the agent saying it succeeded. Record the prompt
version, repository state, tool schema, provider/model, budgets, and task case
so the comparison can be repeated.

Train only on the training split. Select candidates using held-out evidence
without repeatedly tuning against the final test set. Retain difficult cases
and negative results; a smaller prompt that fixes one sample can still lose
important behavior elsewhere.

## Optimize the resource objective, not only classification accuracy

Correct accepted outcome comes first. Then compare wall time, provider calls,
generated output, uncached/cached input, summary/retrieval overhead, and actual
quota-window consumption. A subscription's zero incremental cash price does
not make an unlimited optimization run free of opportunity cost.

The current monetary budget and usage ledger do not by themselves bound a
zero-tariff experiment's weekly token consumption. Choose finite case counts
and optimizer settings; include a token/call budget in any expansion of the
experiment runner. Do not introduce an online gate to solve an offline budget
problem.

A useful experiment can compare a short hand-written prompt with an optimized
candidate under the same tool/runtime contract. Freeze that contract during the
comparison. Do not let an optimizer “win” by weakening checks, broadening tool
access, or replacing executable labels with self-evaluation.

## Promotion is a separate engineering decision

The current coding prompt is a versioned static value in
[prompts.py](../../src/cambium/prompts.py). Changes must be checked on real coding
and continuation cases as well as parsing tests. Do not add automatic artifact
loading to the worker merely because an experiment saved `program.json`.

Before adding an optimized component to the runtime, establish a measured
benefit, define where it is called and what it replaces, and preserve a simple
failure path. Prefer replacing one demonstrated weak decision over layering an
additional planner, reviewer, and policy model around the existing loop.

The larger experiments in
[agent-system evaluation](../research/agent-system-evaluation.md) are proposals,
not claims that learning or deployment already runs in production.

## Executable anchors

- [Optimizer and cost/usage ledger](../../src/cambium/optimize.py),
  [LM adapter](../../src/cambium/lm.py)
- [Decomposition predictor](../../src/cambium/modules/example/dspy_program.py),
  [review predictor](../../src/cambium/modules/should_review/dspy_program.py)
- [Optimizer scenarios](../../tests/scenarios/test_optimize.py),
  [stable program identity/save-load](../../tests/scenarios/test_dspy_module_identity.py)
- [Real coding publication](../../tests/acceptance/test_live_coding_gate.py),
  [real frontend navigation](../../tests/acceptance/test_live_frontends.py)
