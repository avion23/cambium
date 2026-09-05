# Agent-system evaluation

**Status:** proposed experiments, not mandatory runtime gates. Choose a
comparison that answers a concrete question; implementing every variant is not
a prerequisite for a useful harness. Hard runtime invariants remain fixed.

Architecture is in
[`../architecture/agent-operating-model.md`](../architecture/agent-operating-model.md).
Exact target state contracts are in
[`../reference/agent-state.md`](../reference/agent-state.md).

## 1. Central question

Does the system help an agent reach more correct accepted outcomes with less
wasted context, fewer unnecessary actions, lower coordination cost, and better
retention of expensive knowledge?

The unit of evaluation is an accepted branch outcome, not a persuasive final
message. Source state, tests, events, context, provider usage, child joins, and
artifact publication must all be scored.

## 2. Hypotheses

### H1 — deterministic orientation

A bounded SituationFrame reduces stale-state actions, redundant status reads,
and time-to-first-relevant-tool without lowering correctness.

### H2 — progressive disclosure

`SituationFrame -> capsule -> exact evidence ref -> transcript window` returns
less historical text than broad transcript replay while preserving correction
rate.

### H3 — typed repository navigation

`repo_query` reduces shell searches, guessed file reads, and context bytes per
located relevant symbol relative to `run_shell` plus serial `read_batch`.

### H4 — accretive state

Evidence-linked claims, decisions, obligations, and verification retain more
critical facts across compaction, restart, provider migration, and child joins
than untyped summary prose.

### H5 — resource-visible control

A ResourceEnvelope improves delegation, verification, and provider-placement
decisions under wall/context/quota pressure without causing premature stopping.

### H6 — shared projections

When the TUI and model derive from one BranchState, operator interventions are
more accurate and disagreements about current head, child state, or blockers
decrease.

### H7 — bounded delegation

An explicit child-benefit policy improves critical-path completion or
independent information gain, while reducing children whose spawn/join cost
exceeds their contribution.

## 3. Compared systems

Run paired or randomized trials on the same repository commit, task, provider
configuration, budgets, and acceptance checks.

| Variant | Description |
| --- | --- |
| A0 | Current short prompt, active `branch_history` and `repo_query` |
| A1 | A0 plus the proposed bounded SituationFrame |
| A2 | A1 plus model `inspect_state` |
| A3 | A0 with historical recall disabled, to measure its contribution |
| A4 | A0 with `repo_query` disabled, to compare ordinary reads/searches |
| A5 | A0 plus a tested obligation/result-state extension |
| A6 | A0 plus measured resource-pressure guidance |
| AO | Oracle state/policy labels for diagnostic upper bound, not production |

Do not change the model, repository, runtime validators, or provider settings
inside one pair. Warm and cold provider-cache trials are separate strata.

## 4. Workload strata

```text
new-repository orientation
small local edit
large cohesive edit
cross-module protocol change
read-only architecture audit
bug with misleading first hypothesis
failure requiring exact old tool evidence
long session crossing multiple CAST folds
worker restart with a valid checkpoint
worker restart with a stale workspace
provider migration after lease failure
parallel non-overlapping children
child merge conflict and resolver
blind independent review
operator steering during active work
reconnect after frontend exit
```

Split evaluation by repository, session, and time. Adjacent tasks from one
project history must not leak across training and held-out sets.

## 5. Ground truth and acceptance

Each task fixture defines:

```text
starting commit and environment
objective and authority boundary
observable done criteria
required checks
forbidden changes
expected artifact relationship
known relevant and irrelevant source regions
known critical facts/obligations to retain
resource budget
```

Scoring should be blind to the variant when practical. A final answer is not a
success if required artifacts are absent, publication is stale, verification
applies to an earlier head, or forbidden files changed.

## 6. Orientation metrics

Measure from model-call start until the first action that can materially reduce
the dominant uncertainty:

- turns to first relevant tool;
- wall time to first relevant tool;
- prompt tokens before first relevant tool;
- irrelevant files read before relevant source;
- redundant status, branch, or Git queries;
- actions based on a stale generation, epoch, child state, or artifact head;
- incorrect assumptions about writable scope or done criteria;
- number of frame facts the model contradicts without new evidence.

A faster first action is not a gain if it increases severe errors.

## 7. Tool ergonomics metrics

### Repository navigation

- searches before locating the relevant symbol;
- files and bytes scanned;
- shell invocations used only for navigation;
- guessed path failures;
- precision and recall of returned source locations;
- source bytes read before the first correct edit or conclusion.

### State and history inspection

- `inspect_state` calls and returned bytes;
- branch capsule reads;
- exact tool/evidence refs reopened;
- transcript windows requested;
- historical bytes returned but never used in a later action, claim, or
  correction;
- missing detail that forced complete rediscovery;
- incorrect conclusions corrected after evidence inspection.

### Mutation and verification

- failed edits caused by stale or non-unique text;
- mutation retries;
- checks run before any plausible repair;
- focused checks omitted;
- full-suite runs that could not affect the decision;
- verification later made stale by overlapping changes;
- false `objective_met=true` verdicts.

## 8. Accretion metrics

Create a frozen list of critical semantic items for each long trajectory:

decisions, direct observations, failed approaches, constraints, artifact
changes, verification, and open obligations.

At every compaction, restart, child join, provider migration, and delayed
session resume, score:

```text
retention recall
  critical items available in active state / critical items required

retention precision
  current valid items / items presented as current

status accuracy
  correct active/superseded/invalidated/open/satisfied/stale labels

evidence recoverability
  retained items whose exact source can be reopened

re-derivation cost
  tool calls, wall time, and tokens spent rediscovering a previously known item

obligation loss rate
  required unfinished items absent from the resumed state

stale verification rate
  checks presented as current after relevant artifact changes
```

Do not reward verbatim retention of every transcript token. The objective is
current decision sufficiency plus exact recoverability.

## 9. Delegation metrics

For every proposed and admitted child, record:

```text
predicted purpose
ownership overlap
context_mode and placement
spawn/queue/runtime/join wall time
parent-child overlap
provider capacity used
context and token cost
conflict or rework cost
verification cost
critical-path contribution
novel accepted claims
accepted artifacts
history reads needed to understand the capsule
```

Derived measures:

- delegation acceptance and rejection rates;
- child contribution per call/token/wall second;
- children with zero accepted contribution;
- conflict rate by ownership overlap;
- speedup versus sequential execution;
- parent idle time waiting for children;
- independent-review disagreement and correction yield;
- context-policy regret versus the best paired policy;
- placement regret versus feasible alternatives.

A child that runs in parallel but creates more join and verification work than
it saves is a loss.

## 10. Resource-control metrics

Keep dimensions separate:

```text
uncached input
cached input
cache write
output
summary calls
history reads
navigation calls
verification calls
cash cost
subscription/quota-window consumption
wall time
provider queue time
```

Measure:

- task success under fixed budgets;
- budget exhaustion before done criteria;
- unused budget at successful finish;
- provider/cache switches and their measured cost;
- use of idle feasible provider capacity;
- speculative calls under high quota pressure;
- premature finish caused by resource warnings;
- cache-warmth estimate calibration against provider-reported hits.

Unknown tariffs or quotas remain unknown; do not score them as zero.

## 11. Human-agent agreement

For selected trials, capture the operator snapshot and SituationFrame at the
same source watermark. Score whether they agree on:

- branch lifecycle and generation;
- context epoch/lineage;
- accepted artifact head;
- active and critical children;
- blockers and open obligations;
- verification state;
- provider lease and resource pressure.

Also measure:

- operator time to diagnose a wrong/stalled action;
- interventions based on misleading UI state;
- steering acknowledged by the next frame;
- successful reconnect without state reconstruction by hand.

Any disagreement on a shared field is a system defect, not a presentation
preference.

## 12. Failure and recovery experiments

Inject failures at deterministic boundaries:

```text
worker exit before result
worker exit after checkpoint before result
provider timeout before/after usage metadata
quota storm with Retry-After
credential quarantine
summary rejection and K0 rollover failure
SQLite busy and disk-full simulation
parent cancellation while children run
child completion in randomized order
merge conflict and resolver failure
frontend disconnect and reconnect
stale artifact head after external ref advance
truncated tool output with retained spill reference
```

For each, score:

- last safe artifact and checkpoint preserved;
- correct failure classification;
- BranchState rebuilt deterministically;
- affected claims/verification marked stale;
- no lost open obligation;
- bounded retry or explicit stop;
- no duplicate publication or orphaned authority.

## 13. Dataset records

A useful trajectory record contains only visible, reproducible information:

```json
{
  "task_id": "fixture-17",
  "repository_commit": "...",
  "variant": "A4",
  "frame": {
    "version": 1,
    "source_watermark": 481,
    "sha256": "...",
    "bytes": 6142
  },
  "decision": {
    "type": "tool_call",
    "tool": "repo_query",
    "arguments_class": "symbol"
  },
  "outcome": {
    "accepted": true,
    "objective_met": true,
    "artifact_head": "...",
    "verification": ["..."]
  },
  "resources": {
    "wall_s": 151.2,
    "calls": 8,
    "input_tokens": 34000,
    "cached_tokens": 21000,
    "output_tokens": 4300
  },
  "errors": [],
  "review_status": "approved"
}
```

Do not store hidden reasoning. Labels come from accepted outcomes, explicit
human review, and executable evidence—not from the model praising its own
choice.

## 14. Statistical discipline

- Randomize variant order.
- Report warm and cold cache trials separately.
- Repeat stochastic tasks enough to estimate distributions.
- Use repository/session grouped bootstrap intervals rather than treating
  adjacent turns as independent samples.
- Report severe failures individually even when aggregate means improve.
- Freeze evaluation and canary sets before optimization.
- Preserve all failed variants and negative results.
- Compare accepted outcome per resource unit, not tokens in isolation.

## 15. Evidence to examine before adoption

### SituationFrame

```text
[ ] no increase in severe correctness failures
[ ] stale-state actions decrease
[ ] redundant orientation calls decrease
[ ] frame remains within its bound
[ ] model/operator shared fields agree exactly
[ ] provider-prefix stability is not materially degraded
```

### inspect_state / branch_history / repo_query

```text
[ ] relevant evidence/source is located with fewer bytes or calls
[ ] exact references remain stable and reopen correctly
[ ] broad transcript and shell-navigation use decrease
[ ] no new authority or secret exposure
```

### WorkLedger / ResultCapsule

```text
[ ] critical obligation retention improves
[ ] stale facts and verification do not increase
[ ] evidence recoverability improves
[ ] active-context growth remains bounded
[ ] child capsules remain smaller than replaying child transcripts
```

### ResourceEnvelope and delegation policy

```text
[ ] held-out completion is non-inferior
[ ] resource use or critical-path latency improves
[ ] child zero-contribution and overlap conflicts do not increase
[ ] unknown evidence is never treated as favorable certainty
[ ] gains survive multiple repositories and provider conditions
```

### Overall system

```text
[ ] all hard runtime tests pass unchanged
[ ] all canaries pass
[ ] long-session soak has no obligation loss
[ ] fault injection preserves deterministic recovery
[ ] documentation and source use one vocabulary
[ ] a human can reconstruct why an action was legal from durable records
```

## 16. Optimization targets

Once the state and measurement contracts are stable, optimize named components
independently:

```text
orientation policy
repository-location policy
delegation-benefit policy
context-mode/placement policy
history-recall stopping policy
verification-depth policy
finish policy
semantic summarizer
```

The runtime schemas, authority rules, and projection semantics remain frozen
during one optimization run. A prompt gain that depends on weakening validation
is rejected.
