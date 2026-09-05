# Providers are resources

**Status:** current routing/accounting map, followed by explicitly unimplemented
optimization work. Exact configuration is owned by
[provider_config.py](../../src/cambium/provider_config.py).

## Objective

Optimize **correct, useful work per unit of time and available quota**, not raw
token production. Track generated output per second and tokens consumed per
provider window, but do not reward verbosity, repeated reads, speculative
children, or failed retries merely because they increase throughput.

A subscription has finite capacity even when incremental cash cost is zero.
A free provider can be slow or congested. A faster model can waste more tokens
than a slower one. Cache reads, uncached input, generated output, time, requests,
and cash are different dimensions; none is a universal substitute for the rest.

## One owner at each boundary

| Owner | Responsibility |
| --- | --- |
| `provider_config.py` | Provider capabilities, billing/tariff declarations, quota-window configuration |
| `routing.py` | Task admission candidates, debt balancing, provider lanes, resolved assignment |
| `selection.py` | Pure capability/quality/cost/latency scoring within the applicable candidate set |
| `diffundo.py` | Actual provider attempts, retry/fallback, protocol translation, provider usage |
| `provider_scheduler.py` | Shared lease values and durable quota reservations; not a second scheduler |
| `observability.py` | Read-only session usage and quota projection from recorded events |

The model chooses task/context/placement intent. The harness resolves an actual
provider/model with available credentials and the required capabilities.
Configuration and credentials are not inferred from a model's prose.

## Admission versus call-time selection

Admission first removes unavailable or incompatible candidates: disabled
providers, unavailable credentials, explicit provider/model restrictions,
required capabilities, and incompatible context leases. Provider request rate
and in-flight capacity determine whether a lane can start work now.

The simple routing path balances normalized token debt, current lane load, and
request counts with stable configuration-order tie breaks. Requirement-aware
selection additionally uses the existing capability/quality and measured
cost/latency/throughput scoring. These paths are heuristics, not a single global
optimizer with a proven optimum.

Diffundo owns the subsequent provider call and its fallback behavior. Keep task
assignment and call-time lease evidence distinct: an initial assignment does
not prove which provider ultimately served every request. Summaries and child
calls must pass through the same accounting rather than becoming invisible
side traffic.

Child placement is described once in [context branches](context-branches.md).
`spread` prefers another feasible provider; it is not permission to ignore
quota or to migrate an exact same-provider prefix to an incompatible backend.

## What the numbers mean

**Generation throughput:** measured `output_tokens` (Responses) or
`completion_tokens` (Chat Completions), divided by the provider call's wall time.
This includes call overhead; it is not decoder-only speed. `total_tokens`
contains prompt tokens and must never be used as generated output. Missing
output counts mean unknown throughput.

**Routing debt:** usage history normalized by `token_window_allowance`, with
existing decay. The fallback allowance of 20 million tokens and 24-hour debt
decay are balancing defaults, **not evidence of a provider's weekly allowance or
reset**. Do not display or reason about them as an account quota.

**Quota windows:** `QuotaLedger` reserves and reconciles declared token/request
windows across processes. A provider can have several windows, such as a short
request window and a weekly token window. Observations include provider, window,
allowance, use, and reset time. Unknown allowance does not mean unlimited quota.
The accounting must follow that provider's actual rules; cached input is not
assumed exempt from token limits.

**Cash:** reported cost is an estimate under configured tariffs. Numeric zero
alone does not prove a free service. Explicit free/subscription billing labels
are separate from the estimate and from tokens already consumed.

**Cache:** matching request prefixes indicate compatibility, not a hit. Only
provider usage is hit evidence. Cache capability/TTL can inform a prediction;
they cannot replace observed usage.

## Inspection must not consume or mutate capacity

The TUI's full rail shows output rate, input/output/cache counts, call counts,
and known quota windows. Session replay uses the quota snapshots carried by
`usage_event`, retaining the latest window for each provider. It must not mix
historical session state with today's global ledger.

`/quota` and `cambium quota status` explicitly read account-wide observations.
They open existing SQLite storage read-only and do not initialize a ledger,
change directory permissions, reserve capacity, or run write retries during
screen redraws. `cambium quota observe` is the explicit mutation command.

The rail may omit details at small terminal sizes; `/usage`, `/agents`, and
`/quota` provide deeper inspection. A missing observation stays unavailable.

## Remaining optimization work

Use traces to determine whether normalized debt should additionally account
for **known remaining weekly capacity and time to reset**, rather than adding
another scheduler. Compare decisions on held-out task mixes before changing
ranking. Preserve exact-context affinity only when it pays for itself, and
measure both critical-path completion time and quota consumed per accepted
outcome.

Important measurements are accepted tasks/hour, end-to-end output tokens/s,
uncached/cached input and output per task, retries and summaries, child overhead,
and consumption against each provider's actual window. Latency distributions
and errors matter more than an isolated fast sample.

A bounded model-facing resource projection remains part of the
[operating-model design](agent-operating-model.md). It should expose useful
facts and unknowns, not raw scheduler internals or a mandatory policy decision
on every turn.

## Regression evidence

[Resource projection tests](../../tests/scenarios/test_resource_projection.py)
cover output-only rates, deterministic multi-provider quota replay, read-only
inspection, and a height-bounded resource rail.
[Routing throughput tests](../../tests/scenarios/test_routing_throughput.py)
cover lane capacity and measured provider scoring. Real coding/TUI tests cover
accepted artifacts rather than self-reported success.
