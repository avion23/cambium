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

The simple routing path uses current quota observations when available: the
most constrained token/request window determines utilization. Expired windows
do not constrain a new allowance. At comparable utilization and lane load, a
sooner reset is preferred. With no observed window it uses normalized token debt
as a balancing heuristic, not as a claim about weekly entitlement.

Configured request rate and concurrent capacity are separate. When eligible
lanes are busy, the supervisor queues the task without occupying a worker-process
slot. It wakes on a lane release, new usage, or an observed reset/Retry-After time;
there is no periodic admission polling. An exhausted wall budget remains a
reported timeout, not indefinite waiting or fabricated completion.

Requirement-aware selection applies the same quota/cooldown exclusions and
resource ordering after capability filtering. With a live quota observation for
an eligible provider, quality breaks resource ties. Without one, measured
quality/cost/latency/throughput ordering remains in force. Expired windows and
windows belonging only to other providers do not switch the ranking strategy.
Declaring a capability must not disable resource steering. These are heuristics,
not a proven global optimum, and need no provider preflight request.

Diffundo owns the subsequent provider call and its fallback behavior. Keep task
assignment and call-time lease evidence distinct: an initial assignment does
not prove which provider ultimately served every request. An explicit provider
selection is sticky: normal turns and soft unavailability such as cooldown,
Retry-After, or rate limiting do not proactively move the task to a sibling
provider/model merely because substitution is allowed. An actual attempted
provider timeout or hard endpoint-death failure may trigger the bounded fallback
path; once fallback serves, the new provider remains the task's incumbent.
Persisted cooldown evidence is excluded during admission as well as call-time
selection.

The configured logical
call budget is capped by the worker task's remaining wall time; summary headroom
can lengthen the configured summary budget relative to ordinary calls, but never
past that remaining task wall. A timed-out blocking transport may still finish
inside its executor thread, so the worker subprocess remains the hard kill
boundary; the logical call and fallback do not wait for that late transport.

Summaries and child calls pass through the same accounting rather than becoming
invisible side traffic. A successful fallback moves the task's lane reservation
to the provider that actually served it; repeated calls do not reserve it again.
Exact and `inherit` children also reserve the selected provider lane before a
worker starts: context compatibility is not permission to exceed provider
capacity. A failed attempt does not move that reservation. Persisted Retry-After
timestamps expire naturally; a past 429 is not a permanent ban.

OpenAI-compatible Chat Completions use SSE streaming when the endpoint returns
`text/event-stream`; Codex Responses uses the same bounded SSE reader. Reasoning
and visible text deltas update worker progress without changing the final
provider result or accounting. Private reasoning text is not forwarded to the
operator. A provider that ignores streaming and returns ordinary JSON still
uses the same completed-response path. Observability callbacks are read-only:
a broken renderer/progress callback cannot turn a valid provider response into
a failed attempt.

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
request window and a weekly token window. Reservation busy retries are capped by
the provider call's logical deadline and can be woken by cancellation. Once a
reservation or provider request succeeds, reconciliation remains mandatory even
if the logical deadline has expired; dropping that write would corrupt shared
quota accounting. Snapshot projection is observability-only and may be omitted
when its read cannot fit the remaining deadline. Observations include provider,
window, allowance, use, and reset time. Unknown allowance does not mean unlimited
quota. The accounting must follow that provider's actual rules; cached input is
not assumed exempt from token limits.

**Cash:** reported cost is an estimate under configured tariffs. Numeric zero
alone does not prove a free service. Explicit free/subscription billing labels
are separate from the estimate and from tokens already consumed.

**Cache:** matching request prefixes indicate compatibility, not a hit. Only
provider usage is hit evidence. Cache capability/TTL can inform a prediction;
they cannot replace observed usage.

## Inspection must not consume or mutate capacity

The TUI's single status row shows the current provider/model owner and, when
known, the active tool or command/path, elapsed time, output rate, and compact
usage/cache fields. `/detail` expands those fields in the same row; `/usage`,
`/agents`, and `/quota` append deeper snapshots to the timeline. Session replay
uses the quota snapshots carried by `usage_event`, retaining the latest window
for each provider. It must not mix historical session state with today's global
ledger.

`/quota` and `cambium quota status` explicitly read account-wide observations.
They open existing SQLite storage read-only and do not initialize a ledger,
change directory permissions, reserve capacity, or run write retries during
screen redraws. `cambium quota observe` is the explicit mutation command.

At narrow widths the status row clips optional fields rather than creating a
second pane. A missing observation stays unavailable.

## Remaining optimization work

Observed remaining capacity and reset times now affect admission; a long-run
improvement in accepted tasks/hour has not yet been established. Compare the
ranking on repeated task mixes and real weekly windows, including fallback,
summary cost and integration time. Context migration is not free cache transfer.
Do not add another scheduler to compensate for missing measurements.

Important measurements are accepted tasks/hour, end-to-end output tokens/s,
uncached/cached input and output per task, retries and summaries, child overhead,
and consumption against each provider's actual window. Latency distributions
and errors matter more than an isolated fast sample.

`inspect_state` and TUI `/inspect` expose the same recorded branch resource
facts through `state_view.py`; unavailable facts remain unknown. This is an
on-demand read, not a mandatory policy decision on every turn.

## Regression evidence

[Resource projection tests](../../tests/scenarios/test_resource_projection.py)
cover output-only rates, deterministic multi-provider quota replay, read-only
inspection, and the one-line status projection.
[Routing throughput tests](../../tests/scenarios/test_routing_throughput.py)
cover lane capacity and measured provider scoring.
[Observed-resource scenarios](../../tests/scenarios/test_routing_resources.py)
check quota expiry, persisted cooldown, fallback reservation ownership, and a
queued task waking on release. Real coding/TUI tests check accepted artifacts;
these deterministic scenarios alone do not establish a throughput improvement.

For renderer comparisons, run the same interpreter and sample counts at each
revision:

```sh
PYTHONPATH=src python3 scripts/profile_overhead.py \
  --iterations 12 --warmups 2 --no-cprofile
```

The profile reports median/p95 wall time for a long-session event draw,
status-only draw, and resize, plus retained timeline bookkeeping bytes for one
long-session replay. The fixtures use a discard-only TTY and fixed 120/100-column
sizes, so the numbers measure renderer and timeline work rather than provider or
terminal I/O.
