# Provider routing

## Problems

1. Providers fail heterogeneously: authentication, quota, configuration,
   stalls, overload, and content policy require different responses. Blind
   retries burn budget or damage healthy lanes.

2. Every provider eventually has an outage, so single-provider operation is
   unacceptable for work that must complete.

3. Switching providers invalidates prompt-cache affinity and may fork context
   unless task identity and progress move with the switch.

4. Large code contexts can trigger moderation false positives on benign tasks,
   turning a provider-specific interpretation into an avoidable task failure.

5. Deep-reasoning calls legitimately exceed naive timeouts, so a fixed short
   deadline mistakes slow useful work for failure.

6. Credential-less lanes waste attempts and pollute health statistics when
   missing credentials are discovered only during a call.

7. Parent and child tasks have different affinity needs. A continuation of one
   semantic branch should remain sticky, while an independent child may be
   admitted on another provider when that improves capability, capacity, or
   cost.

## Ownership

Routing is split by time scale, not duplicated:

```text
supervisor admission
    |
    | hard feasibility, credentials, task constraints,
    | lane capacity, debt, cache affinity
    v
assigned provider/model lease
    |
    v
Diffundo call-time execution
    |
    | direct health evidence, retry, cooldown, bounded cascade
    v
provider transport
```

The supervisor owns admission and the provider/model lease. Diffundo owns
attempt execution after admission. Provider configuration describes
capabilities. The quota ledger owns observed windows. None of these modules is a
second scheduler.

## Admission

Admission constructs a hard-feasible set before ranking:

- provider and model are enabled;
- credentials are available;
- task capability requirements are satisfied;
- the provider is inside the inherited authorization boundary;
- lane capacity and quota reservations permit dispatch;
- the requested model constraints are satisfied.

A task can constrain admission with `requirements`, `model_candidates`,
`authorized_providers`, and `authorized_providers_explicit`. Prompt prose does
not override these fields.

Within the feasible set, routing may consider quality, measured throughput,
cash cost, quota scarcity, verification cost, and cache switching cost. The
policy must not turn a hard constraint into a soft score.

## Leases and failures

- Each lane uses an evidence-based health state machine: open admits work,
  cooldown withholds transiently, half-open permits a bounded probe, and
  disabled quarantines proven authentication or configuration failures.
- Direct success, quota, stall, overload, transport, endpoint, and policy
  evidence drive distinct transitions. Health is never inferred from another
  lane’s result.
- Content-policy flags cascade without damaging provider health.
- Each attempt receives an effort-aware deadline inside the hard task wall.
  Backoff and retries cannot overrun the wall.
- Budget charging is uncached-only where provider evidence permits it: count
  fresh input and newly generated output, not reused cached work.
- Safe checkpoints preserve progress across a legal failover.

## Root and child affinity

A Cambium child is a supervised task, not a provider-native subagent. See
[`subagents.md`](subagents.md).

| Task relationship | Admission rule | Context construction |
| --- | --- | --- |
| Same semantic branch | Keep provider/model lease while feasible | Resume exact checkpoint |
| Exact compatible child | Pin to parent provider/model | Byte-identical prefix plus child task |
| Independent child | Admit from its own feasible set | Fresh head; semantic summaries when available |
| Incompatible child | Do not claim cache reuse | `summary_trunk_ref` or fresh prompt |
| Permanently infeasible lease | Explicit migration/failure path | Latest safe checkpoint, never silent substitution |

An exact child is cache-affine only when provider, model, protocol, reasoning
effort, tool schema, system prompt, checkpoint hashes, and authorization
boundary are compatible. Otherwise the supervisor clears inherited assignment
and may give the child a provider-neutral semantic trunk.

This is an affinity-scheduling problem with switching costs. The root branch has
a hard lease until infeasible. Independent children may use spare or
better-matched capacity because their execution does not require the root’s
provider-local cache.

## Invariants

- Provider health changes only on direct evidence.
- Content flags never damage health.
- Pinned-model tasks never silently switch model on transient failure.
- Every legal failover preserves task progress through a safe checkpoint.
- Admission never spawns a lane that cannot authenticate.
- A child cannot widen the parent provider authorization boundary.
- A cache hit is claimed only from provider evidence.
- Cross-provider semantic reuse is never labelled a cache hit.
- The prompt cannot directly select a provider or bypass admission.

## Violations this design prevents

- Typed outcomes and evidence-only health replace blind retry.
- Feasibility filtering removes credential-less and capability-incompatible
  lanes before dispatch.
- Leases and checkpoint identity keep a continuation on one semantic branch.
- Explicit cache-affine and semantic-reuse modes prevent cross-provider cache
  claims.
- Effort-aware deadlines distinguish useful slow reasoning from an overrun.
- Separate admission and call-time ownership prevent competing schedulers.
