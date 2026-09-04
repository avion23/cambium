# Prompt experiments and DSPy

**Status:** offline module optimization is implemented. Automatic optimization or
loading of the live coding prompt is not.

## What is connected today

```text
module manifest + dataset loader + DSPy program + metric
    -> optimize / evaluate
    -> saved program under optimized/<module>
    -> optimize eval can load that program
```

The registered examples are `should_decompose` (under `modules/example`) and
`should_review`. Their `dspy_program.py` implementations are not selected by the
normal worker/supervisor coding loop. `worker.py` builds its instructions from
`prompts.CODING_AGENT`; summary calls use `SEMANTIC_SUMMARIZER`. Saving an
optimized classifier does not change either prompt.

The implementation supports zero-stage evaluation, BootstrapFewShot and GEPA.
It loads examples, applies a metric, accounts for optimizer calls and can save
program state for later evaluation. DSPy remains outside the ordinary runtime's
import path. This is an experiment facility, not a background learning process.

## Commands and scope

These validate registration without making a model call:

```sh
cambium optimize should_decompose --dry-run
cambium optimize should_review --dry-run
```

A real experiment is explicit and budgeted:

```sh
cambium optimize should_review --optimizer bootstrap --budget-usd 2 \
  --dataset /path/to/reviewed.jsonl
cambium optimize eval should_review --dataset /path/to/reviewed.jsonl \
  --budget-usd 2 --json
```

`optimize eval` requires `--dataset`. With no `--program-dir`, it loads
`optimized/<module>` when available, otherwise a fresh program. An explicitly
requested program directory must contain usable state. This auto-loading is
for **evaluation**, not production worker requests.

`optimize extract` and `optimize stats` handle trajectory candidates and dataset
reports. Their options are documented by `cambium optimize --help`. Extraction
is not label validation: a successful parse or redaction pass says nothing
about whether an expected action is useful.

## What the current data can and cannot establish

The module datasets mix task text with observed session outcomes. For example,
`modules/should_review/datasets/eval.jsonl` contains failed/cancelled status,
final tool counts and labels recommending review because a session failed.
Other labels prescribe review at a security boundary. Those are labeling
policies, not measured reductions in total task cost.

Post-task outcome information can be legitimate input to a post-task review
decision. It leaks future information if used to claim a pre-task decomposition
policy. A provider outage also does not establish that more agents or an extra
review call would have helped. Keep those decision timings and objectives
separate.

The actual coding question is not "can a classifier reproduce these labels?"
It is "does this prompt or delegation choice complete the task correctly with
less time and scarce account usage?" Current classifier scores do not answer
that question.

## A useful coding-prompt experiment

Use small repositories with executable outcomes: locate and explain a symbol,
make a bounded edit and verify it, recall exact prior evidence, resume after a
checkpoint, or integrate an independent child. Keep repository state, tools,
provider/model configuration and budgets fixed between candidates.

Measure accepted correctness first, then whole-task wall time and actual
provider usage. Include malformed actions, retries, cached/uncached input,
output, summary calls, duplicate reads, verification and repair work. Report
sample counts and unsuccessful trials. The worker's marginal context-growth
budget is not physical provider consumption.

Keep training tasks separate from evaluation tasks and split related
trajectories together. Examples from the same session should not appear on
both sides merely because their prompts differ. Compare against the current
short static prompt, not only another generated candidate.

Only instructions should vary in a prompt experiment. Do not simultaneously
change tool behavior, loosen acceptance criteria or relabel failures as success.
Optimization may add demonstrations and make a prompt longer. Include serialized
request size and actual input usage in the comparison; do not assume DSPy means
shorter prompts.

## Adoption

There is no reason to add a review/decomposition model call to every task just
because a module can be optimized. Keep a direct coding path. An offline winner
can become a reviewed change to `prompts.py`, with a prompt version change and
real task regressions. Exact context/cache identity must reflect the changed
instructions and tools.

The live frontend tests provide concrete tasks and traces for such experiments,
but one successful run is not a statistical prompt-quality result. Current
mechanical prompt reductions are likewise not a measured tokens/week gain.

Implementation: `optimize.py`, the module manifests, `modules/*/dspy_program.py`
and the corresponding dataset/metric modules. Regression coverage includes
`tests/scenarios/test_dspy_program.py` and module-local tests. The missing coding
prompt experiment/consumer belongs in the [open plan](../../implementation-plan.md),
not in a new runtime approval framework.
