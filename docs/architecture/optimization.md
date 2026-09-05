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
providers. They also distinguish summary requests, malformed actions, failed
provider attempts, tool failures, output/cache tokens and accepted heads after
each interactive turn. Execution artifacts remain in each rollout directory.

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

`--max-wall-s` covers one complete harness rollout across all of its operator
turns, child joins and reconnects. It is not restarted for every follow-up.
Timeout cancels owned work and retains a failed report; the checker still examines
only accepted code. Fixture setup and the independent artifact check are outside
that harness deadline, and cleanup can extend the reported elapsed time.

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

## Historical corpus

`src/cambium/benchmarks/prompts.jsonl` contains eight frozen cases adapted from
recorded Cambium sessions. Each records its source session and a task family.
The loader rejects a family split across training and final evaluation. Derived
variants are labelled as variants, not claimed to be transcripts that occurred.

| Split | Cases | Historical basis |
| --- | --- | --- |
| Train | `arithmetic`, `parallel-modules` | Small repair and failed independent-module runs |
| Train | `parallel-utilities` | The CSV/configuration join failure, with explicit semantic children |
| Validation | `cambium-self-fix` | Repair `render_tokens_per_s(None)` at frozen Cambium commit `63034cc` |
| Validation | `blocking-child` | One read-only child sharing the current trunk/provider |
| Test | `navigation-history` | Actual edit, check, and exact tool-evidence retrieval |
| Test | `history-reconnect` | Derived continuation across frontend reconnect |
| Test | `corrected-history` | Derived requirement change, semantic review, K0, reconnect and stale-check recall |

The interactive runner uses the same `InteractiveSession` as the frontends.
Its check runs outside the agent's editable worktree against accepted code.
Read-only follow-ups must leave the accepted head unchanged. Required tool,
child or rollover evidence is checked only in cases specifically testing those
behaviors; ordinary coding cases do not reward extra children or extra calls.
The parallel-utilities case also requires two overlapping sibling lifetimes
(`required_parallel_children`): sequential one-child suspensions cannot pass as
parallel work. `peak_pending_children` counts admitted, unfinished siblings,
including queued work; it does not claim simultaneous provider calls or speedup.
Provider lists report successful serving calls, not failed attempts alone.

The raw source sessions are development artifacts under the earlier worktree's
`.cambium` directories. The fixtures contain the minimum task/input/check,
not credentials or entire private transcripts. Source provenance and negative
runs are described in [the audit](../research/harness-audit-2026-09-04.md).

## Metrics and limitations

Correct accepted output is primary. A passing case receives a small bounded
efficiency contribution from elapsed time, token usage and calls; failed cases
score zero. This is an explicit heuristic, not a calibrated economic model.

Keep train, validation and test cases disjoint. Do not repeatedly revise prompts
against the final test cases and still call them held out. Repeat close
comparisons and enlarge the corpus before treating small gains as general.
The eight cases now exercise continuation, corrections, a semantic fold and K0,
but remain a small development corpus, not a representative coding benchmark.
Once a test case is used to repair the harness, it is a regression case, not
independent evidence of generalization. Reserve fresh cases before making that
claim after a GEPA search.

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
