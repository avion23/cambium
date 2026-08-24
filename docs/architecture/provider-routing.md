# Provider routing

**Status:** current production contract.

Cambium has one admission policy, one attempt-ordering primitive, and one
quota/lease state boundary. There is no second scheduler actor and no alternate
provider-policy stack.

## Ownership

| Concern | Owner | Mutability |
| --- | --- | --- |
| Task admission and provider/model assignment | `cambium.routing` | Pure selectors over injected debt and lane snapshots; `DebtStore` owns durable usage debt |
| Equal-priority quality ordering | `cambium.selection` | Pure, clock-injected ordering function shared by admission and Diffundo |
| Per-call health, retry, cooldown, and cascade execution | `cambium.diffundo` | One router instance owns transport health and request-rate buckets |
| Provider/model trunk affinity | `cambium.provider_scheduler.ProviderLease` | Immutable value bound once per semantic trunk |
| Token/request quota reservations | `cambium.provider_scheduler.QuotaLedger` | SQLite transactions shared safely across processes |
| Provider configuration | `cambium.provider_config` | Validated records with per-entry schema quarantine; document structure stays fatal; secrets remain environment references |
| Cache/CAST policy | `cambium.provider_scheduler.CacheHorizonConfig` / `CastConfig` | Provider-neutral breakpoint and K0 rollover thresholds |
| Supervisor lane reservations | `cambium.supervisor` | Session-owned counters around admitted tasks |

`provider_scheduler.py` keeps its historical import path because
`provider_config`, `Diffundo`, and the quota CLI already consume its domain
values. It intentionally contains no `ProviderScheduler` class, ranking API,
mailbox, or concurrency ownership. Admission belongs only to `routing.py`.

## Admission flow

1. The supervisor loads and authorizes provider configuration.
2. A task with `model_candidates` reaches `routing.resolve_assignment` only
   after it owns an admission slot.
3. The selector applies hard filters first: authorization, enabled state,
   requested model, optional tier, declared task requirements, and current lane
   capacity. Request-rate tokens and `max_in_flight` are independent lane
   dimensions; legacy `rpm`/`max_concurrency` aliases remain accepted.
4. Without explicit quality requirements, `select_lane` balances normalized
   usage debt and in-flight lane load. With requirements, `score_providers`
   applies the capability boundary and delegates equal-priority quality order
   to `selection.order_candidates`.
5. The supervisor writes the chosen provider, model, and tier into the task
   specification and increments exactly that provider's lane.
6. The worker receives a pinned assignment. Diffundo keeps that primary
   association while it is live. Only terminal provider death can open a
   fallback, and then only inside the authorized provider set; same-tier
   candidates precede other tiers and the serving provider becomes the new
   sticky association.
7. Every provider call emits redacted usage evidence. The supervisor folds that
   evidence into `DebtStore`, and later admissions see the updated snapshot.
8. The lane is released when the task leaves the worker phase, including
   failure and cancellation paths.

This split is intentional. Admission chooses ownership; Diffundo executes the
owned call while enforcing health and retry policy. They share evidence and the
same pure quality-ordering primitive, but they do not compete for scheduling
ownership.

## Hard constraints

The following are feasibility checks, not weighted preferences:

- provider is enabled and authorized;
- configured model is one of the task's candidates;
- a pinned tier matches;
- `quality: high` requires a strong provider tier;
- `min_context_window` requires a declared capacity at least that large;
- the provider lane has spare capacity;
- a strict `ProviderLease` matches provider and model exactly;
- a quota reservation fits every configured token/request window;
- malformed or unknown requirement fields fail closed.

No score may reintroduce a provider removed by those checks.

## Pinned-provider fallback

One-shot resolution in `oneshot._resolve_provider` carries the selected
provider as the pinned primary and hands the worker only the enabled,
credential-ready authorization set. `Diffundo.call` may leave that pin only
after the attempt has terminal endpoint-death evidence. The decision is the
narrow `diffundo.ProviderError.is_real_death` predicate, not the broad
"request failed" outcome:

- `AUTH_ERROR` with HTTP 401, or with HTTP 403 after the classifier identifies
  a key-level credential failure, is terminal for the pinned endpoint;
- any HTTP 5xx response is terminal endpoint-unavailable evidence;
- connection refusal/unreachable errors, DNS resolution failures, and TLS/SSL
  handshake or certificate failures are terminal transport evidence.

After terminal death, `_real_death_fallback_candidates` searches enabled,
capability-compatible providers in the original tier first and then the other
tiers, excluding providers already tried. A successful substitution records
the serving provider and `fell_back_from` in the call/result provenance and
keeps the new association for later calls; it does not bounce back to the
dead pin or preserve the old model as an implicit requirement.

429/quota pressure, including `Retry-After`, does not qualify. Neither do
WAF/network-block 403s, model-entitlement/configuration 403s, policy/content
refusals, malformed responses, or an ordinary timeout. Those paths retain the
existing same-provider retry/cooldown, disable, or untouched-health behavior;
they cannot cause a pinned provider to be replaced merely because a request
failed.

## Ordering

Configured `priority` is the first policy class and is never crossed by
measured quality. Inside one equal-priority run, `selection.order_candidates`
uses current evidence in this order:

1. failure probability;
2. latency-SLO compliance;
3. expected cost per successful turn;
4. normalized throughput/latency/cache tie-break. Measured output
   `tokens_per_s` comes from redacted usage events; configured
   `throughput_hint_tps` is only a fallback when fresh evidence is absent.

Missing or stale evidence is a barrier that keeps configuration order. Root
incumbency moves a provider only to the front of its own equal-priority run.
Deterministic rotation applies only where no current evidence exists.

The simpler max-min admission path ranks by normalized token debt, then current
lane load, request count, and configuration order. A provider under 429
pressure receives a smaller effective request-rate allowance before ranking;
its in-flight capacity is not silently conflated with RPM.

## Quota and debt state

Two stores represent different facts and must not be merged:

- `DebtStore` is a compact redacted usage history used for balancing and
  quality evidence. It contains counters, cost, latency, cache hits, retry
  pressure, and quarantine metadata; never credentials or prompt content.
- `QuotaLedger` is transactional reservation state for declared or observed
  token/request windows. One reservation succeeds for every window or for none
  of them, and reconciliation replaces the estimate with actual usage exactly
  once.
- `CacheHorizonConfig` batches immutable cache breakpoints by pending tokens or
  elapsed horizon without claiming a provider retained a cache. `CastConfig`
  adds `max_segments` and `max_active_trunk_tokens`; when an interactive
  summary-only trunk is due, the semantic history is materialized as one CAST
  K0 entry in a new immutable epoch.

`cambium quota observe` records trusted provider/header/dashboard evidence;
`cambium quota status` displays the content-free snapshot. Providers without a
configured quota window continue to use the debt-balancing allowance rather
than inventing a provider contract.

## HTTP failure classification

Provider HTTP failures pass through one classifier in `cambium.diffundo`.
`429` responses are quota pressure: bounded `Retry-After` evidence, lane
cooldown, and full-jitter retry within the call deadline. Plain `403` responses
are not auth errors by default. Body markers distinguish five operational
classes:

- invalid credentials -> `AUTH_ERROR`; disable the provider and quarantine the
  credential fingerprint. It is re-admitted only after the credential identity
  changes, and a pinned run may use a fallback because 401/key-level 403 is
  terminal endpoint evidence;
- missing model entitlement -> `CONFIG_ERROR`; disable that provider/model
  configuration without treating it as bad credentials;
- quota or billing exhaustion -> `QUOTA`; honor bounded reset/`Retry-After`
  evidence and hold the provider in cooldown until it is eligible again;
- policy/content refusal -> `REFUSAL`; fail the request and leave provider
  health untouched;
- WAF/browser/network block -> transient `ERROR` with bounded retry/cooldown;
  it does not quarantine the credential or trigger pinned fallback;
- HTTP 5xx -> `ERROR` with the response status retained as terminal
  endpoint-unavailable evidence after the provider attempt's retry sequence;
  this is the other HTTP class that can open pinned fallback.

An unlabelled `403` remains fail-closed as authentication and therefore is
terminal for pinned fallback. Network connection/DNS/TLS errors remain
retryable `ERROR` attempts until their configured retry sequence is exhausted,
then qualify as terminal transport evidence; ordinary timeouts instead retain
the timeout/cooldown path. Malformed responses fail closed but are not terminal
death. The classification is retained in redacted usage/failure evidence,
never in prompt or credential material.

## Removed alternatives

The following parallel implementations are intentionally gone:

- the unused asyncio `ProviderScheduler` mailbox and its separate
  `ProviderPolicy`/`RoutingRequest` rank;
- `provider_policy.py` and its second billing/quota/affinity model;
- `dispatch_policy.py`, which adapted configuration into the unused actor;
- `provider_resources.py`, whose budget/header layer was reachable only through
  the alternate stack;
- materialization/fix scripts that generated those files.

`selection.py` remains because it is a single shared pure primitive, not a
second admission engine.

## Validation

This project does not use GitHub Actions or hosted continuous integration.
Changes are validated from source with local commands, for example:

```bash
PYTHONPATH=src python -m pytest -q
PYTHONPATH=src python -m ruff check src tests
PYTHONPATH=src python -m cambium doctor
```

A helper's existence is never integration evidence. The authoritative path is
`supervisor -> routing -> pinned worker -> Diffundo`, with usage flowing back to
`DebtStore` and quota state flowing through `QuotaLedger`. Interactive wall
budgets use an explicit operator value when supplied; otherwise they can scale
from the selected provider's throughput hint and the current branch's measured
output rate, with a safety margin, while ordinary one-shot runs retain their
fixed default.
