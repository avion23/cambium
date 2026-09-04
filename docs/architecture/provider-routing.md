# Providers, capacity and routing

**Status:** current implementation and its limits. The desired outcome is more
correct completed work per second and per account budget, not more token burn.

## Owners and call path

```text
provider_config -> supervisor task admission -> worker assignment
    -> Diffundo request attempts -> usage / quota evidence -> later admission
```

`routing.resolve_assignment` chooses a task's provider/model from configured,
authorized candidates. `LaneState` models request admission and in-flight slots.
`Diffundo` owns actual provider calls, deadline handling, retries and fallback.
`ProviderLease` preserves useful affinity; `QuotaLedger` reserves and reconciles
configured account windows. These are different responsibilities, not parallel
task schedulers.

Provider intent comes from the task's requirements and child placement. The
model does not select credentials. Exact child context constrains the provider
boundary; changing providers is not an exact-cache continuation.

## Keep the quantities separate

| Quantity | Current meaning |
| --- | --- |
| Request rate | Replenishing request-token bucket, configured requests/minute |
| In-flight capacity | Concurrent request slots, independently configured |
| Output tokens/second | Mean of available per-call output-token/latency samples |
| Account quota | Explicit token/request windows, reservations and reconciliation |
| Usage debt | Decayed historical usage used for load spreading |
| Cash | Provider usage valued using available tariff information |
| Cache affinity | Exact-compatible request identity; not proof of a cache hit |
| Context capacity | A request must fit the selected model's declared window |

Effective output rate includes the recorded call latency, not just decoder time.
A report containing only total tokens does not provide an output-rate sample.
Large prompt/cache counts must not make a provider look faster. When evidence
ages, its weight can decrease without changing its measured speed or latency.

Use `requests_per_minute` and `max_in_flight` for new provider configurations.
The retained legacy lane construction derives a concurrency cap from RPM; it is
not the independent-capacity model and should not guide new configuration.

## Current selection policy

Without task requirements, `select_primary` minimizes normalized usage debt;
`select_lane` also filters capacity and breaks ties by in-flight calls, request
count and configuration order.

With requirements, `score_providers` applies capability and lane constraints,
then calls `selection.order_candidates`. Configured priority comes first. Within
compatible measured runs, the ordering uses failure fraction, latency-SLO
compliance, cost per successful call and a latency/cache/throughput tie-break.
An incumbent has affinity inside its priority group. Missing evidence preserves
configuration position rather than fabricating a quality score.

**These paths do not implement one coherent tokens/second-versus-tokens/week
optimizer.** The requirement-free path is mostly load balancing; the scored
path uses a different objective. Measured throughput is not the primary
criterion of every admission. Raw failure fractions are not calibrated success
confidence. Neither path accounts for all task-level repair and integration
costs.

Keep hard feasibility separate from preference: credentials, declared model and
context capabilities, explicit billing choices, live capacity and actual quota
blocks cannot be fixed by a better score.

## Weekly allowances are not usage debt

`token_window_allowance` normalizes the load-balancing debt. Its fallback,
`DEFAULT_TOKEN_WINDOW_ALLOWANCE = 20_000_000`, is a heuristic scale. It is **not**
a measured entitlement, a hard account cap, or a weekly reset contract.
`DebtStore`'s 24-hour half-life is also not a weekly reset. Do not display either
as remaining provider quota.

Real limits belong in the provider's `quota_windows`, whose entries contain:

```text
name, duration_s, token_allowance and/or request_allowance, reserve_fraction
```

`QuotaWindowSpec` requires a positive duration and at least one allowance.
`QuotaLedger` stores window usage, reservations and reset times transactionally;
actual usage reconciles the estimate. Provider observations can update the
window state. A seven-day local window can be declared with duration 604800,
but its limit and reset alignment must come from the actual account contract.
Do not guess them from the word "subscription".

Unknown quota means there is no known window to enforce, not that the provider
has infinite entitlement or a known low-pressure state. Subscriptions may have
no marginal cash charge while still consuming scarce capacity.

## Accounting caveat

Provider usage events and quota accounting must retain real reported usage,
including repeated prompts. Separately, the worker's `_usage_budget_charge`
counter charges marginal uncached prompt growth plus completions. This is a
context-growth guard, **not a measurement of total billable tokens**: repeated
uncached input still costs provider resources. The current naming/help around
this budget needs to be reconciled before treating it as a spending limit.

Do not optimize a benchmark using that marginal counter while reporting its
result as physical account consumption. Record input, cache reads/writes,
output, summaries, retries, wall time and accepted completion independently.

## Context placement

For coupled work, preserving the root lease can avoid cold input and retain
useful affinity. For independent children, `spread` prefers another feasible
lane and may use more of the available subscription capacity. Spreading onto an
incapable or exhausted provider does not help.

Explicit root migration from an accepted checkpoint remains distinct from
call-time retry/fallback. The complete durable migration and model-visible
resource-pressure proposal is not implemented by the presence of a lease value
object. See the [open plan](../../implementation-plan.md).

## Next useful improvements

Use the existing quota ledger and observed throughput in a single admission
policy, with explicit behavior for missing evidence. Compare it on real task
outcomes, not only synthetic score order. Preserve the operator's priority and
capability choices. Account for cold-input and join cost before moving work.

Add a bounded resource summary to the existing state projection only when it
helps an actual continuation/delegation decision. Do not add another ledger,
optimizer service or approval layer to expose fields already owned elsewhere.

Relevant sources are `routing.py`, `selection.py`, `provider_scheduler.py`,
`provider_config.py` and `diffundo.py`. Tests include `test_routing_throughput.py`,
`test_routing_balance.py`, `test_routing_scored.py`, `test_routing_lanes.py` and
`test_provider_scheduler.py` under `tests/scenarios/`.
