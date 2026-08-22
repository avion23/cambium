# Provider and model routing contract

**Status:** target contract plus implementation audit. Source and tests remain
authoritative for current behavior.

## 1. Decision

Routing is a constrained scheduling problem. Cambium must first decide which
provider/model lanes are **semantically legal** for a request. Only then may it
optimize quality, cost, latency, load, or prompt-cache locality.

Cache affinity is a switching cost, not a permission to violate provider
priority, exact model selection, context capacity, authorization, or tool
compatibility.

## 2. Formal ordering

For task `t` at time `k`, construct the feasible set:

```text
F(t, k) = {
  p |
  authorized(p)
  ∧ enabled(p)
  ∧ exact_model_match(p, t)
  ∧ protocol_compatible(p, t)
  ∧ tools_supported(p, t)
  ∧ context_window(p) >= required_context(t)
  ∧ output_limit(p) >= required_output(t)
  ∧ budget_available(p, t, k)
  ∧ health_allows(p, k)
}
```

An empty set fails before a billable request. It does not call an incompatible
fallback and wait for a downstream model-mismatch rejection.

Selection is lexicographic only across hard policy classes:

1. feasible set membership;
2. configured priority tier;
3. risk-adjusted soft objective inside that tier;
4. deterministic tie-break/load distribution.

Inside one priority tier, cache affinity is a measured switching cost in the
soft objective, not an unconditional override. Retain the incumbent while its
risk-adjusted score remains within a hysteresis margin equal to the expected
cache rebuild/warm-up cost; switch when the alternative's expected gain exceeds
that margin.

A useful soft objective inside one feasible priority class is:

```text
score(p) =
    E[task_loss | evidence]
  + λ * E[cost]
  + μ * E[latency + queue_delay]
  + ν * cache_switch_cost(current_affinity, p)
  + ξ * exploration_bonus_or_penalty
```

The weights are policy. Hard constraints are not weights.

## 3. State separation

Keep four state machines separate:

- **eligibility:** static provider/model/protocol/tool/context facts;
- **health/circuit breaker:** failures, Retry-After, quarantine, recovery;
- **capacity:** request/token buckets, in-flight work, observed service time;
- **selection evidence:** quality, latency, cost, cache affinity, uncertainty.

Combining them into one scalar too early makes invalid states comparable. A
provider with no credential cannot become eligible because its latency score is
excellent. A model mismatch cannot be offset by a cache hit.

## 4. Cache affinity identity

Affinity is valid only for the exact cache identity described in
[`context-engine.md`](context-engine.md): provider, model, endpoint/protocol,
reasoning settings, tool schemas, ordered prefix, assets, serialization, and
cache namespace/policy.

A provider-level incumbent string is therefore an approximation. The target
routing key is:

```text
CacheAffinity = {
  provider,
  model,
  protocol,
  request_prefix_digest,
  namespace,
  retention_mode,
  observed_at,
  expires_at_or_unknown
}
```

If any required field changes, the old affinity cannot justify stickiness.
Unknown cache evidence is not a miss. Provider-reported positive cache-read
tokens are direct evidence; documented zero is a miss; omitted fields remain
unknown.

## 5. Capacity and queueing

Requests per minute and concurrent requests have different units. A safe
in-flight estimate follows Little's Law:

```text
concurrency ≈ arrival_rate_per_second * observed_service_time_seconds
```

Rate limits should use token/request buckets. In-flight caps should use
configured provider limits, observed service time, memory/socket constraints,
and a safety factor. A 429 may reduce the relevant bucket or concurrency cap,
but `rpm` must not be copied directly into a concurrency count.

When service time or capacity is unknown, use a conservative configured cap and
learn from measured completions. Do not use a placeholder context/token limit
as proof of provider capability.

## 6. Evidence and uncertainty

Raw empirical means are unstable at small sample sizes. In particular, a
provider with one success should not automatically outrank one with hundreds of
well-measured calls. Use at least one of:

- a minimum evidence threshold before reordering;
- Beta-Binomial posteriors or Wilson intervals for failure probability;
- shrinkage priors for latency/cost;
- UCB/Thompson-style exploration bounded to feasible candidates;
- exponential decay or change-point handling for non-stationarity.

The debt ledger must record the configuration version and observation window.
Provider, model, protocol, and cache identity should be separable dimensions;
aggregating all models under one provider can create Simpson's-paradox-style
misrouting.

## 7. Stable distribution

For truly equivalent candidates with no decisive evidence, use deterministic
weighted rendezvous (highest-random-weight) hashing over a stable task/branch
identity. It provides stable assignment and limited churn when candidates join
or leave. Weight by measured capacity only after that capacity is trustworthy.

List rotation is acceptable only for an evidence-free equal-priority run. It
must not rotate a lower-quality measured candidate ahead of a better measured
candidate, and it must never cross a configured priority boundary.

## 8. Implementation audit at `main@877e4a7`

### Fixed in this change set

1. `selection.order_candidates` hoisted an incumbent ahead of every candidate,
   including higher-priority providers. It now moves the incumbent only to the
   front of its equal-priority run.
2. Deterministic rotation ran after quality sorting and could reverse the
   measured order. Rotation now applies only when the whole priority run lacks
   current quality evidence.
Scenario tests pin both properties.

### Open correctness gaps

1. **Strict model pin is not strict before dispatch.** `Diffundo._candidates`
   includes different-model providers after exact matches. The worker later
   rejects a returned model mismatch. This can spend latency/quota/money on a
   response that was ineligible from the start. Model substitution needs an
   explicit policy and must be reflected in the task/result contract; otherwise
   candidate construction must filter it out.
2. **RPM is used as an in-flight cap.** `routing.LaneState` mixes rate and
   concurrency dimensions. Split token/request buckets from in-flight capacity
   and derive the latter from measured service time or configuration.
3. **Cache evidence is too coarse.** A provider-neutral boolean and leading
   system-message byte count do not identify an exact provider request prefix.
4. **Missing cache usage can become a false miss.** Omitted provider cache
   fields should remain unknown rather than update failure evidence.
5. **Cost does not model cache read/write tariffs.** Input, cache read, cache
   write, and output prices need separate fields; subscriptions may have zero
   marginal API cost but non-zero quota opportunity cost.
6. **Quality confidence is absent.** The current score uses raw fractions and
   averages without sample-size uncertainty.
7. **Evidence aggregation is too broad.** Provider-level debt can conflate
   models/protocols and cache identities with different performance.
8. **Fallback contract is split across layers.** Admission, Diffundo cascade,
   and worker result validation can disagree on what substitution is legal.
9. **Incumbency is provider-name stickiness, not measured affinity.** Within an
   equal-priority run the incumbent currently leads even when its measured
   quality is materially worse. The target is cache-identity-aware hysteresis
   with an explicit switching-cost estimate and expiry.
10. **Zero cost conflates free with unknown pricing.** Provider price fields and
    estimated cost default to zero, while quality scoring treats zero as
    unavailable evidence. Introduce an explicit pricing-known state before
    ranking a genuinely zero-marginal-cost lane ahead of a priced lane.

The strict-model and rate/concurrency gaps should be corrected before making
cache locality a stronger routing weight.

## 9. Required properties and tests

### Eligibility properties

- An unauthorized/disabled/incompatible provider is never called.
- An exact model pin never produces a request to a different model unless the
  task explicitly authorizes substitution.
- A provider with insufficient context/output capacity fails before dispatch.
- Tool/schema/protocol changes invalidate cache affinity.

### Ordering properties

- Priority dominates every soft score.
- An incumbent never crosses priority or capability boundaries.
- Rotation never changes a measured quality ordering.
- Free, unknown, and unreported price states remain distinguishable.
- Stale evidence is neutral and cannot silently become current.

### Capacity properties

- Request rate and concurrency are independently bounded.
- Randomized service times satisfy the configured in-flight invariant.
- Retry-After blocks new dispatch without corrupting long-run quality evidence.
- Concurrent assignment never exceeds the effective lane cap.

### Determinism properties

- Given frozen state, time, task identity, and candidates, selection is pure.
- Candidate input order cannot alter results except where configured order is
  the declared tie-break.
- Adding/removing an unrelated equivalent provider causes only bounded
  rendezvous-hash remapping.

### Measurement properties

- Cache hits are attributed to the exact provider/model/prefix identity.
- Unknown cache fields do not count as hits or misses.
- Cost reconstruction equals provider invoices on a frozen sample within a
  declared tolerance.
- Confidence-aware reordering is tested on adversarial small samples.

## 10. Computer-science mapping

- **Constrained optimization:** legality first, soft objective second.
- **Multi-armed bandits with switching costs:** quality exploration plus cache
  affinity, under hard feasibility constraints.
- **Queueing theory / Little's Law:** translate measured service time and rate
  into capacity instead of equating RPM with concurrency.
- **Circuit breakers:** health state controls admission, not quality ranking.
- **Rendezvous hashing:** stable deterministic distribution with minimal churn.
- **Bayesian estimation / confidence bounds:** avoid overreacting to tiny
  samples and non-stationary observations.
- **Dimensional analysis:** tokens/minute, requests/minute, seconds, dollars,
  and concurrent calls cannot be combined without explicit conversion.

## 11. References

- Little's Law: <https://pubsonline.informs.org/doi/10.1287/opre.9.3.383>
- Bandits with switching costs: <https://arxiv.org/abs/1310.2997>
- Highest-random-weight hashing: <https://www.cs.ucsb.edu/sites/default/files/documents/tech_reports/1996-03.pdf>
- Weighted rendezvous hashing: <https://datatracker.ietf.org/doc/draft-ietf-bess-weighted-hrw/>


## Implemented production policy

Cambium treats the root agent's first successful provider/model as a strict
`ProviderLease`. Every later action and summary call on that recursive trunk is
filtered to the lease; an unavailable incumbent fails the branch rather than
silently moving it and destroying cache/context continuity. Exact
cache-compatible children inherit the lease. Provider-neutral semantic-summary
children and other cold parallel branches choose independently.

Provider configuration separates `rpm` from `max_concurrency`, supports known
free, metered, local, and subscription billing modes, and accepts multiple
independent quota windows. A five-hour, weekly, and monthly allowance are three
constraints, not one blended budget. `QuotaLedger` reserves and reconciles them
with SQLite `BEGIN IMMEDIATE`, while `ProviderScheduler` owns in-process lane
state through an asyncio mailbox.

Selection remains hard-feasibility first. Within a configured priority class it
uses shrinkage success evidence, measured/hinted output throughput, utilization,
known marginal price, cache-switch cost, and a deterministic rendezvous tie
break. Free models are useful for bounded independent work, review, search,
classification, and redundant verification, but cannot win tasks whose model,
context, quality, or tool requirements they do not satisfy.
