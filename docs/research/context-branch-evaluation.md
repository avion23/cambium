# Context-branch evaluation

**Status:** experimental protocol. Architecture is in
[`../architecture/context-branches.md`](../architecture/context-branches.md).

The implementation separates hard correctness from empirical policy. The
runtime enforces explicit branch modes and deterministic history reads. Whether
a model chooses those modes well—and whether provider spreading improves total
throughput—must be measured.

## 1. Questions

### Q1 — context sufficiency

Does a complete cached trunk reduce task failure and unnecessary history reads
relative to semantic-only child prompts?

### Q2 — cache economics

For same-provider children, is `trunk+inherit` cheaper/faster than constructing
and serving a separately compressed child prompt?

### Q3 — provider throughput

Does `semantic+spread` or `fresh+spread` increase completed tasks per wall-clock
hour under real subscription limits without reducing task quality?

### Q4 — history recovery

Can a parent recover a missing detail through a specific tool reference more
efficiently than replaying a whole child transcript?

### Q5 — branch-decision quality

Does the model choose continue/trunk/semantic/fresh policies that minimize total
cost subject to successful completion?

## 2. Compared policies

Use paired tasks with frozen repository state and provider configuration.

| Policy | Description |
| --- | --- |
| P0 | Single branch; no delegation |
| P1 | Every child receives full trunk on inherited provider |
| P2 | Every child receives semantic trunk and normal routing |
| P3 | Explicit LM decision using `BRANCH_DECISION_POLICY` |
| P4 | Oracle-labelled branch policy for the same tasks |
| P5 | P3 plus on-demand `branch_history` |

Do not compare different code revisions or provider settings in the same pair.

## 3. Workload strata

```text
small cohesive edit
large cohesive edit
independent read-only audit
parallel non-overlapping edits
blind reproduction
cross-provider review
child-summary omission requiring recall
suspected incorrect child conclusion
recursive child decomposition
```

Split by repository and time so near-duplicate tasks cannot cross train/eval
boundaries.

## 4. Metrics

### Correctness

- task success and exact acceptance criteria;
- focused and full test results;
- invalid child-policy proposals;
- semantic result / Git artifact join disagreement;
- wrong conclusions caused by omitted context;
- wrong conclusions retained after branch-history inspection.

### Context

- prompt tokens by call;
- cached input tokens reported by provider;
- summary-call input/output tokens;
- trunk size and raw-tail size;
- branch-history calls;
- rows/bytes returned;
- history returned but not used in a later action or conclusion.

### Scheduling

- provider/model selected per branch;
- parent and child overlap;
- queue time;
- task completion latency;
- tasks completed per hour;
- provider quota utilization and idle capacity;
- switches away from an otherwise reusable provider cache.

### Cost

Keep dimensions separate:

```text
uncached input
cached input
cache write
output
summary calls
history reads
verification calls
cash cost
subscription-window consumption
```

Cached tokens are not automatically “free” or “not consumed.” Use provider
telemetry and the actual account limit being optimized.

## 5. DSPy prompt components

Optimize named components independently:

```text
BRANCH_DECISION_POLICY
BRANCH_HISTORY_POLICY
SEMANTIC_SUMMARIZER
```

Keep fixed during one optimization run:

```text
tool schemas
runtime validators
provider configuration
repository snapshot
budgets
model family
```

### Branch decision labels

Each training/evaluation example should contain:

```json
{
  "task_features": {
    "estimated_scope": "small|medium|large",
    "ownership_overlap": false,
    "needs_live_tail": false,
    "independent_review": true,
    "semantic_state_sufficient": true,
    "provider_capacity_available": true
  },
  "decision": {
    "action": "continue|delegate",
    "context_mode": "trunk|semantic|fresh|null",
    "placement": "inherit|spread|null"
  },
  "outcome": {
    "success": true,
    "wall_s": 123.4,
    "new_tokens": 4200,
    "cached_tokens": 18000,
    "history_reads": 1
  }
}
```

Labels should be derived from successful paired outcomes, not from prose
preference alone.

### History recall labels

Evaluate whether the model:

1. first uses the bounded child capsule;
2. lists tools before requesting a whole transcript;
3. reopens the relevant tool reference;
4. stops retrieving when enough evidence is present;
5. corrects or preserves the prior conclusion appropriately.

## 6. Hypotheses

- H1: `trunk+inherit` wins for most children that require parent state because
  the full trunk is small and cached.
- H2: `semantic+spread` wins on separable workloads when another subscription
  would otherwise be idle.
- H3: `fresh+spread` improves independent review/reproduction but loses on tasks
  requiring project-specific decisions.
- H4: stable tool references reduce recalled tokens relative to transcript
  replay while preserving correction rate.
- H5: unbounded automatic delegation loses to explicit branch decisions because
  orchestration overhead dominates small tasks.

These are hypotheses, not implementation claims.

## 7. Paired experiment

For each task:

```text
freeze repository commit
freeze task and acceptance tests
randomize policy order
run repeated warm and cold trials
record provider-reported usage
score outcome blind to policy
report distributions and confidence intervals
```

Warm and cold cache trials must be reported separately. The first sibling may
populate a provider cache while later siblings hit it.

## 8. Promotion gate

A prompt or routing-policy change is promoted only when:

```text
[ ] held-out success is non-inferior
[ ] severe correctness failures do not increase
[ ] median completion latency improves or remains bounded
[ ] token/cost claims use provider-reported measurements
[ ] improvement survives more than one repository and task stratum
[ ] canary tasks pass
[ ] no runtime invariant or schema changed during prompt optimization
```

## 9. Related systems

Relevant comparisons include context compression, recursive retrieval trees,
external-context inspection, shared-context agents, isolated actor-style agents,
and provider prompt caching. These systems motivate individual components, but
Cambium must evaluate the complete combination: recursive execution branches,
cache-aligned trunks, cross-provider placement, stable raw-session recall, and
transactional code integration.
