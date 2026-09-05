# Prompt experiments and automatic replacement

**Status:** `cambium optimize prompts` runs real repository-task benchmarks and
GEPA over coding or summary policy text. A better evaluated candidate replaces
the prompt artifact automatically. Ordinary worker turns do not import DSPy.

## One runtime, an offline optimizer

`prompts.py` separates fixed action/summary protocol from two tunable strings:
`coding` and `summary`. `prompt_optimize.py` presents an actual Cambium rollout
as a DSPy predictor. GEPA changes the selected policy string; `benchmark.py`
runs the existing supervisor and worker against an isolated repository.

The checker runs against the accepted Git head, not an uncommitted worker tree
or a model's claim of success. Reports retain changed paths, outcomes, elapsed
time, calls, reported tokens, estimated cost, child policies and actual serving
providers. Execution artifacts remain in each rollout directory.

DSPy format is useful at this optimization boundary, not everywhere. Tools,
Git effects, provider configuration and the normal action protocol stay ordinary
code. There is no additional classification or approval request per action.
The separate `should_decompose` and `should_review` optimizers still exist for
small decision experiments; they are not the coding worker's runtime policy.

## Run it

From a checkout with the optional DSPy environment installed:

```sh
# Inspect cases and budgets without constructing an LM.
python -m cambium optimize prompts --optimizer gepa --dry-run

# Baseline only; no prompt replacement.
python -m cambium optimize prompts --optimizer zero --provider zai \
  --output .cambium/prompt-baseline

# Hill climb coding/delegation policy and automatically install an improvement.
python -m cambium optimize prompts --optimizer gepa --component coding \
  --provider zai --reflection-provider zai \
  --max-evals 12 --max-calls 200 --max-tokens 500000 --budget-usd 2 \
  --max-wall-s 300 --output .cambium/gepa-coding
```

Provider names above refer to the operator's configuration. `--component
summary` runs the same experiment over summary policy. Use `--dataset PATH`
for your own task distribution and `--case ID` for a baseline reproduction.
`--no-deploy` keeps a GEPA candidate as an experiment rather than replacing the
runtime artifact. These are experiment controls, not prerequisites for normal
coding or delegation.

GEPA uses `current_best` candidate selection, no candidate merging, and one
rollout at a time. The experiment budget includes reflection requests and
reported task usage. Concurrent in-flight requests can finish after a limit is
observed; these limits are not a provider-enforced account quota. A zero cash
estimate does not make subscription tokens unlimited.

## Replacement semantics

The normal artifact is `~/.config/cambium/prompts.json` (or the corresponding
`XDG_CONFIG_HOME` path). `CAMBIUM_PROMPTS` selects another artifact. It contains
versioned JSON with plain `coding` and `summary` text, not an executable pickle.

A changed candidate is installed when validation completion count does not
regress, average validation score improves, and the held-out checks pass.
Replacement is atomic. This is experiment selection, not an online agent gate.
A failed or interrupted experiment does not replace the current policy.

New sessions load the artifact automatically; a missing default artifact uses
the built-in policy. An interactive session pins its policy in its durable
manifest. Reconnect and child work retain that text. `/new` loads the current
policy for a fresh branch. Replacing a file never rewrites an active CAST
prefix. See [CAST](context-engine.md).

The output directory contains `candidate.json` when produced, `report.json`,
and individual rollout repositories, events and checkpoints. Copy a previously
retained policy artifact back to the configured prompt path to revert it.

## Metrics and limitations

Correct accepted output is primary. A passing case receives a small bounded
efficiency contribution from elapsed time, token usage and calls; failed cases
score zero. This is an explicit heuristic, not a calibrated economic model.

Keep train, validation and test cases disjoint. Do not repeatedly revise prompts
against the final test cases and still call them held out. Repeat close
comparisons and enlarge the corpus before treating small gains as general.
The packaged cases are starter fixtures, not a representative coding benchmark.
In particular, use long continuation/fold/recovery cases to evaluate summary
quality rather than relying on tiny functions.

Freeze repository revision, provider pool and runtime behavior during a prompt
comparison. Provider failure and malformed model output remain failures in the
report. Distinguish assigned provider from actual serving provider after
fallback. Inspect traces before adding another prompt rule: often the defect
is a missing integration or redundant protocol requirement instead.

## Focused checks

`test_prompt_replacement.py` exercises policy pinning, artifact loading and the
GEPA deployment path with controlled outcomes. It does not establish a real
prompt quality gain. Live frontend tests exercise actual provider calls, tools,
publication and continuation. `cambium optimize prompts --optimizer zero`
provides the repeatable real-task path; the larger GEPA search is operator-run.

No successful dry run, saved artifact or green classifier suite proves that
Cambium's coding prompt became better. Use the report's actual accepted results.
