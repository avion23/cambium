# Diffundo Provider Cascade — Design

**Historical snapshot — 2026-08-09.** Docs-only proposal from worktree
`/tmp/opencode/cambium-cascade` (branch `wt-cascade`). It extends architecture §9 and
records fixes for `LLM-C2` and `LLM-M6`; pricing/context data was not re-run against the
later `provider-landscape.md` and remains **UNVERIFIED**. Current behavior is owned by
[`docs/architecture/architecture.md`](../architecture/architecture.md), source/tests,
and [`v2-1-status.md`](v2-1-status.md).

**Current note (not retroactive):** active `supervisor.run_plan` is flat;
`task_decomposed` remains unsupported; the provider cascade is source-defined and now
honors `Retry-After`; worker stdout/event admission is bounded; there is no per-worker
OS sandbox or approval; DLQ and eval cache are absent.

## 0. Findings and disposition

| Finding | Historical resolution |
|---|---|
| **LLM-C2** — v0.1 `_cascade` compared a resolved first model and became a one-provider no-op (`[rev-llm C2]`, `[sysd §M2]`). | Use request `tier` (`[arch §9.2]`) and an ordered fallback list within that tier. |
| **LLM-M6** — `_race` let the first task that finished by raising win, cancelled superior work, and favored the fastest provider (`[rev-llm M6]`). | Race is opt-in; exceptions are values, a quality gate may defer a winner, and best completed score is retained. |

Claims about existing documents use `[arch §N]`, `[sysd §M2]`, or `[rev-llm C2/M6]`;
extensions are marked **design**, unavailable data **UNVERIFIED**.

## 1. Cascade semantics

For request tier (default `fast`), filter providers by tier, required tools and minimum
context (`[arch §9.1, §9.2]`), remove cooldown/circuit-`OPEN`, then sort ascending
`priority`. An explicit `model=` pins optimization; it is not the default. Optional
`fallback_order` is advisory after priority (**design**, **UNVERIFIED** registry data).

Fall through on timeout, transport/5xx/malformed error, quota/HTTP 429, or refusal.
Timeout, error, and quota retry with backoff; refusal does not retry but still falls
through and is recorded separately. `Retry-After` is honored by the current source
(the historical proposal predates that implementation). If all candidates fail, raise
`AllProvidersFailed(providers_tried, last_error)`; never hang beyond the shared
`call_budget_s` and per-attempt timeout. Worker patience (`provider_patience_s`, default
180 s) handles an outage without killing the worker (DS-M7); Architectus parks dispatch.

### 1.1 Opt-in race (LLM-M6 repair)

The following rules are the proposal's unique contract: fixed deadline
`min(call_budget_s, race_timeout_s)`, first `n` eligible providers, and a provider→task
map. `asyncio.wait(..., FIRST_COMPLETED, return_exceptions=True)` treats exceptions as
values; crashed providers are recorded and never win. A result wins immediately only if
the deterministic quality gate passes. Otherwise continue; when the deadline/exhaustion
arrives return the highest `score` completed result, not the first-completed result. Cancel
pending tasks best-effort and emit `race_cancelled` cost events because a metered request
may still bill. Cooldown/cache mutation is per-instance or lock-protected (`DS-M4`).

Quality gate: non-empty output, valid expected JSON, no refusal, required fields;
LLM-judge is optional/default-off. `score()` is a deterministic monotone proxy. Both are
**design, UNVERIFIED** because architecture defines only task metrics.

### 1.2 Call state and retries

`cache lookup → tier select → cascade (default) or race → health/cost event → cache
write`. Per-provider retries use `max_retries`, exponential backoff and full jitter;
each attempt has `timeout_s`; a fixed call deadline stops retries. Circuit breaker is a
sliding window (`window_size`, `failure_threshold`, `open_backoff_base`) with
`HEALTHY → OPEN → HALF_OPEN → HEALTHY|OPEN`; explicit `enabled=false` is `DISABLED`.
Budget caps (`per_task_max_usd`, optional session cap) stop further calls with
`CostBudgetExceeded` (**design, UNVERIFIED**). Cost events retain provider/model/tier,
tokens, estimated cost, latency, cache hit, and failure class; no keys.

## 2. Cache and failure policy

Cache is before the cascade (`[arch §8.1]`), keyed by prompt plus `context_hash`; a hit
returns `cache_hit=true` with generation timestamp and skips providers. A winner writes
provider/model/tier/cost provenance when `cache=true` and `context_hash` is present.
Negative caching is an optional, bounded short-TTL `(cache_key, provider)` memo for
permanent refusal/auth failures; transient failures remain cooldown-only (**design,
UNVERIFIED**). This proposal's cache does not imply an eval cache; the current note above
records that eval cache is absent.

When all providers fail, `AllProvidersFailed` is converted to a recoverable worker error
only after provider patience; no supervisor restart is consumed for an outage. Proposed
`provider_health_change` and `all_providers_down` events are observability/dispatch
signals (non-critical/critical respectively), requiring schema reconciliation.

## 3. Configuration (historical example)

Architecture fields are `name`, `model`, `tier` (`fast|balanced|strong|reasoning`),
`api_key_env` (name only), `base_url`, `priority`, `context_window`, `supports_tools`,
`cooldown_s`, and `max_retries` (`[arch §9.1]`). Design additions are marked below:

```jsonc
{"diffundo":{"default_mode":"cascade","default_tier":"fast","call_budget_s":60,
 "race":{"enabled":false,"redundancy":2,"timeout_s":30,
         "quality_gate":{"mode":"deterministic","judge_tier":null}},
 "circuit_breaker":{"window_size":20,"failure_threshold":0.5,"open_backoff_base":2.0},
 "cost":{"per_task_max_usd":1.0,"per_session_max_usd":null},
 "negative_cache":{"enabled":true,"ttl_s":30},
 "providers":[
  {"name":"deepcode","model":"deepseek-v4-flash","tier":"fast",
   "api_key_env":"DEEPCODE_API_KEY","base_url":null,"priority":0,"enabled":true,
   "fallback_order":["gemini","openai","claude"],"timeout_s":30,"max_retries":2,
   "retry_backoff_base":2.0,"retry_jitter":1.0,"cooldown_s":60.0,
   "context_window":200000,"supports_tools":true,"price_per_1m_in":0.0,"price_per_1m_out":0.0},
  {"name":"gemini","model":"gemini-flash","tier":"fast","api_key_env":"GEMINI_API_KEY","priority":1,"context_window":1000000,"supports_tools":true,"timeout_s":30,"max_retries":2,"cooldown_s":60.0},
  {"name":"openai","model":"openai-mini","tier":"fast","api_key_env":"OPENAI_API_KEY","priority":2,"context_window":200000,"supports_tools":true,"timeout_s":30,"max_retries":2,"cooldown_s":60.0},
  {"name":"claude","model":"claude-haiku","tier":"fast","api_key_env":"ANTHROPIC_API_KEY","priority":3,"context_window":200000,"supports_tools":true,"timeout_s":30,"max_retries":2,"cooldown_s":60.0}]}}
```

Model IDs, windows, and prices are illustrative and **UNVERIFIED**. Config is not
serialized to logs (`[arch §3.2]`); only env-var names are retained (`[arch §12]`).

## 4. Open questions and references

Q1 tier taxonomy and registry data; Q2 race quality-gate cost; Q3 downgrade versus fail
on `CostBudgetExceeded`; Q4 event schema additions/replay; Q5 breaker calibration; Q6
negative-cache scope; Q7 `call_budget_s` versus patience sizing. These remain proposals.

References: `docs/architecture/architecture.md` §§2, 3.2–3.6, 5.2, 6, 7.4, 8.1, 9,
10, 12, 18 (DS-M4, DS-M7, IMPL-M5); superseded `docs/architecture/system-design.md`
M2; `docs/architecture/reviews/review-llm-design.md` C1/C2/C3/M6; distributed-systems
review C3; `docs/research/provider-landscape.md` was not used. Historical alternatives
are explicit: default cascade is adopted; default race is rejected; negative cache is
optional/defer; all-provider outage parks dispatch rather than restarts workers.

## Appendix A — state transitions and provider accounting

The call state machine recorded in the original proposal was:

```text
call(tier, request)
  → CACHE_LOOKUP --hit--> envelope(cache_hit=true)
  → TIER_SELECT (tier + capability + cooldown/OPEN filters)
  → CASCADE (priority order) or RACE (explicit opt-in)
  → HEALTH_UPDATE + COST_EVENT
  → CACHE_WRITE (winner provenance) or AllProvidersFailed
```

Provider health transitions were `HEALTHY → OPEN` after the sliding failure threshold,
`OPEN → HALF_OPEN` after `open_backoff_base`, and `HALF_OPEN → HEALTHY|OPEN` from the
probe. `DISABLED` was an explicit config state, not a circuit inference. A cooldown
started on transport exception, quota, malformed response, and timeout; a refusal was
tracked separately so an operator could distinguish model policy from infrastructure.
`Retry-After` now supplies the server-directed delay when present; the proposal's full
jitter remains the fallback when it is absent.

Per-call accounting was designed to include provider name, model, tier, request hash,
attempt number, latency, input/output token estimates, estimated cost, cache hit, and
failure class. It deliberately excluded API-key values and raw prompt text. Budget
enforcement was session-owned: a task cap stopped new attempts while a session cap
stopped all calls; the proposal left downgrade-to-cheap-tier versus hard failure to Q3.

### A.1 Sequential cascade pseudocode

```python
for provider in eligible(tier, require_tools, min_context_window):
    if provider.cooldown or breaker_open(provider):
        continue
    for attempt in range(provider.max_retries + 1):
        try:
            result = await call(provider, timeout=provider.timeout_s,
                                deadline=call_deadline)
        except TimeoutError:
            record(provider, "timeout")
        except QuotaError as exc:
            record(provider, "quota", retry_after=exc.retry_after)
        except ProviderError as exc:
            record(provider, "error", detail=type(exc).__name__)
        else:
            if refusal_marker(result):
                record(provider, "refusal")
            else:
                return result
        await backoff(attempt, retry_after=last_retry_after)
        if deadline_expired():
            break
    mark_cooldown(provider)
raise AllProvidersFailed(tried, last_error)
```

The loop never retries a refusal by default, never sleeps beyond the shared deadline,
and never lets a provider exception escape into an unbounded worker restart loop. If a
provider returns an HTTP 429 with `Retry-After`, the delay is clamped to remaining budget;
the current source-defined behavior honors it even though this historical draft did not.

### A.2 Exact race behavior retained

Race candidates were the first `n` eligible providers. The deadline was fixed at entry,
not recomputed from each completion. `asyncio.wait(FIRST_COMPLETED,
return_exceptions=True)` returned exceptions as values. The algorithm recorded each
provider, skipped exception results, stopped on the first quality-gate pass, otherwise
kept the best score, then cancelled survivors. It emitted one cost record per cancelled
request because cancellation may still incur provider billing. A crashed provider could
never become `winner`; this was the direct LLM-M6 repair over the v0.1 `winner.result()`
path.

The deterministic gate accepted non-empty completions, valid expected JSON, required
fields, and no refusal marker. An LLM judge was optional and default-off because it adds
another provider call. `score()` ranked gated failures only as a fallback. Both remained
UNVERIFIED, as did the 60% race-quality claim.

### A.3 Cache key and safety rules

Positive cache keys included namespace, model/tier, temperature, prompt, and mandatory
`context_hash`; a hit preserved original generation timestamp and winner metadata.
Without `context_hash`, a caching request was rejected. Negative cache entries were
short-lived and per-instance, keyed by `(cache_key, provider.name)` for permanent refusal,
auth, or other deterministic failures; transient timeout/error stayed cooldown-only.
No cache content entered the durable event log, and this proposal did not create an eval
cache. This distinction matters because the current runtime has no eval cache.

### A.4 Open-question record

The original Q1–Q7 remain: validate tier taxonomy against provider landscape; decide
race judge cost; downgrade versus fail on `CostBudgetExceeded`; add provider-health event
kinds and schema/replay bump; calibrate breaker window/threshold; retain or defer negative
cache; and size 60-second call budget against 180-second provider patience. Each provider
example remains illustrative; no price, context window, or model ID was verified.

## Appendix B — cost, health, and failure event shapes

The proposed `llm_call` cost event was separate from the provider response:

```json
{"kind":"llm_call","task_id":"task-1","payload":
 {"provider":"openai","model":"openai-mini","tier":"fast","attempt":1,
  "latency_ms":840,"input_tokens":1200,"output_tokens":420,
  "estimated_cost_usd":0.0012,"cache_hit":false,"outcome":"quota"}}
```

`provider_health_change` carried provider name, old/new state, window rate, and reason;
`all_providers_down` carried requested tier and last failure. The architecture catalog
did not yet include these kinds, so both were schema proposals. They were
non-critical/critical respectively: a health transition is reconstructible from call
events, while a complete outage explains a dispatch pause across restart/replay.

`AllProvidersFailed` carried ordered provider outcomes, not only the last exception. The
worker boundary converted it to a recoverable error after patience, and the supervisor
stayed healthy. An all-refused request remained distinct from all-down: each refusal was
retained so Architectus could decide whether to fail or ask for changed content. That
policy was Q2 and was not hidden in a retry/default.

## Appendix C — exact capability and ordering rules

Capability filtering happened before circuit state and priority ordering:

1. Match request `tier` (`fast`, `balanced`, `strong`, or `reasoning`).
2. If `require_tools`, drop `supports_tools=false`; if `min_context_window`, drop a
   smaller window.
3. Drop cooldown and breaker `OPEN` providers.
4. Sort by numeric `priority`; only equal priorities consult advisory `fallback_order`.

An explicit model pin bypassed interchangeability for optimization but did not change its
tier. The proposal did not infer real ordering from names, prices, or windows.
`provider-landscape.md` appeared later but was not a source for this snapshot, so each
example datum remains **UNVERIFIED**.

Retries used per-provider `timeout_s` inside fixed `call_budget_s`; deadline expiry
stopped retry and fall-through. Backoff was full jitter around an exponential base,
except a server `Retry-After`, which current source honors and clamps to remaining
budget. Circuit health used sliding window/half-open probe; cooldown started on exception
and quota, while refusal was recorded without marking the model offline. Disabled stayed
disabled until configuration changed.

## Appendix D — alternatives record

Rejected alternatives were: global model list with an exact-model guard (the LLM-C2
single-provider bug); default race mode (fastest/weakest bias and metered cancellations);
blind `winner.result()` (LLM-M6 exception poisoning); cache keys without repository
`context_hash` (LLM-C1 stale edits); shared cache/cooldown state across workers (DS-M4
mutation race); and treating provider outage as worker crash (DS-M7 restart storm).
Retained but optional were negative caching, LLM-judge quality gates, and budget-
pressured downgrade to a cheaper tier. None became an implicit compatibility path.

## Appendix E — provider response classes

The proposed adapter normalized responses before cascade policy saw them. A successful
response carried text/structured content, usage metadata, model identity, and an
optional refusal marker. Transport failures, malformed JSON, 5xx, quota, and timeout
were classified without exposing provider-specific exception types to Architectus. A
refusal carried its policy code and bounded message; it was not converted to outage.
The normalized envelope made retry/fall-through deterministic and gave fake-provider
tests one contract.

| Class | State update | Retry / next candidate |
|---|---|---|
| Timeout | failure window + cooldown | retry while deadline, then fall through. |
| Error | failure window + cooldown | retry transport/5xx/malformed, then fall through. |
| Quota | rate window + `Retry-After` | retry with server delay, then next provider. |
| Refusal | refusal counter, not outage | no retry; next provider receives request. |

The proposal rejected treating every exception as outage: breaker state would open for
content-policy refusals and hide a healthy provider. It also rejected retrying until
wall-budget exhaustion when the next tier candidate was healthy; fall-through happened
after each provider's bounded attempts. Current source-defined `Retry-After` refines the
table without changing its classes.

## Appendix F — provider configuration boundary

Configuration was intentionally split between routing metadata and secrets. The
serialized `ProviderConfig` held name, model, tier, endpoint, priority, capability
flags, context window, timeout, retries, cooldown, and prices (where known); it held
`api_key_env` names only. Environment lookup occurred at the provider boundary and was
never copied to events, cache entries, prompts, or cost records. A caller requesting
tools or a minimum window could receive `AllProvidersFailed` even when a lower-capability
provider was healthy; silently dropping the capability requirement was forbidden.

The cascade was per call and per tier. A balanced/strong request never fell down to fast
merely because fast had a lower priority; downgrade required an explicit orchestrator
decision (Q3). A pinned `model=` similarly bypassed fallback by caller choice. This kept
the historical alternatives auditable: “cheap-first” was the default only within a
configured tier, not a universal quality claim.

Cache provenance was part of the result envelope: `provider`, `model`, `tier`, generation
timestamp, and estimated cost survived a hit. `cache_hit=true` let Ascensus filter cached
trajectories from optimization data. Negative cache was bounded/private and never a
durable event, so replay could not mistake a transient provider refusal for a permanent
task failure. These semantics remained proposal details; current source/tests decide
which fields exist.

## Appendix G — deterministic test fixtures

The fake-provider fixture returned one scripted response per `(provider, request_hash,
attempt)`. Tests delayed the first provider, raised an exception in the second, returned
429 with `Retry-After` in the third, and supplied a valid response in the fourth. The
sequential cascade had to preserve order and bounded deadline; the race fixture had to
ignore the exception, wait past a quality-gate failure, and return the best completed
score. A provider that refused all candidates produced an all-refused envelope, distinct
from all-down health events.

Cache tests changed repository state while retaining the prompt and asserted that a
different `context_hash` missed. A same-hash hit preserved provider provenance and
generation timestamp. Cooldown tests ran concurrent calls against one provider and
asserted per-instance state did not race; a separate process had an independent breaker.
Cost tests exhausted the task cap and asserted no fallback call happened after the cap.
These fixtures were proposed acceptance evidence, not a claim that Diffundo or an eval
cache exists in the current tree.

The provider list was ordered within a tier, never across tiers by a hidden “quality”
score. A strong-tier call that exhausted strong providers returned a typed failure unless
Architectus explicitly changed tier. This made quality/cost tradeoffs visible to the
orchestrator and avoided silently routing a sensitive task to a cheaper model. Capability
filters were similarly explicit: dropping a provider that lacked tools or context was
recorded in selection evidence, not treated as a provider crash.

## Appendix H — outage and budget sequencing

The call budget was checked before provider selection, before every retry, and after a
server-directed delay. A task cap stopped new attempts for that task; a session cap
stopped new calls while allowing already admitted calls to finish. The proposal left
downgrade-to-cheap-tier versus hard failure as Q3, so no hidden downgrade path was
allowed. `AllProvidersFailed` carried the tried provider names and final typed error;
the worker's patience loop treated that value as a recoverable provider outage and did
not consume process restart budget.

Health events were observations, not policy. `provider_health_change` recorded the
from/to state, failure class, and bounded window rate. `all_providers_down` was proposed
as a critical signal for a requested tier; it did not mean every configured tier was
offline. API-key values, prompts, and full completions were excluded from both events
and cost records. This kept the cascade draft compatible with the explicit current note:
source-defined retry timing is authoritative, while eval-cache and DLQ behavior are not
implied by provider caching or bounded queues.

## Appendix I — retained open questions

Q1 left tier names and registry data to an authority with current provider evidence. Q2
left race quality-gate cost and Q3 left downgrade versus hard failure. Q4 required event
schema additions and replay tests before health/cost events could become durable. Q5 left
circuit thresholds, Q6 negative-cache scope, and Q7 the relationship between call budget
and worker patience. These questions were not silently answered by illustrative model
IDs, prices, context windows, or “cheap-first” wording. Any implementation must pin its
provider/model, dataset, and source commit before converting a proposal field to a
current behavior claim.

The cascade did not own task decomposition, retry budgets for workers, or event-store
durability. It returned a typed response or typed provider failure to its caller; the
worker patience loop and supervisor decided whether to wait, park dispatch, or surface a
terminal result. This boundary prevented a provider exception from becoming a generic
process restart and kept source-defined `Retry-After` handling in one implementation.

Provider names and prices in the JSON example were placeholders. They were not a
recommendation or a live registry snapshot.

The adapter normalized provider response, refusal, usage, and retry metadata before
policy. Architectus received a stable envelope and never provider-specific exceptions.

Retry timing remained bounded by the shared call deadline.

Circuit state was per provider instance. A disabled provider stayed disabled until config
changed, and a refusal did not masquerade as infrastructure outage.

Current source owns retry timing.

The proposal stays historical.

Provider claims require measurement.

Illustrative prices are stale.

Current registry data is required.

Do not infer pricing.

Historical only.

Historical review identifier retained: `C4`.
