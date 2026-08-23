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
| Provider configuration | `cambium.provider_config` | Immutable validated records; secrets remain environment references |
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
   capacity.
4. Without explicit quality requirements, `select_lane` balances normalized
   usage debt and in-flight lane load. With requirements, `score_providers`
   applies the capability boundary and delegates equal-priority quality order
   to `selection.order_candidates`.
5. The supervisor writes the chosen provider, model, and tier into the task
   specification and increments exactly that provider's lane.
6. The worker receives a pinned assignment. Diffundo may order attempts only
   inside that assignment's allowed provider set; a live root lease cannot move
   to another provider or model.
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

## Ordering

Configured `priority` is the first policy class and is never crossed by
measured quality. Inside one equal-priority run, `selection.order_candidates`
uses current evidence in this order:

1. failure probability;
2. latency-SLO compliance;
3. expected cost per successful turn;
4. normalized latency/cache tie-break.

Missing or stale evidence is a barrier that keeps configuration order. Root
incumbency moves a provider only to the front of its own equal-priority run.
Deterministic rotation applies only where no current evidence exists.

The simpler max-min admission path ranks by normalized token debt, then current
lane load, request count, and configuration order. A provider under 429
pressure receives a smaller effective lane cap before ranking.

## Quota and debt state

Two stores represent different facts and must not be merged:

- `DebtStore` is a compact redacted usage history used for balancing and
  quality evidence. It contains counters, cost, latency, cache hits, retry
  pressure, and quarantine metadata; never credentials or prompt content.
- `QuotaLedger` is transactional reservation state for declared or observed
  token/request windows. One reservation succeeds for every window or for none
  of them, and reconciliation replaces the estimate with actual usage exactly
  once.

`cambium quota observe` records trusted provider/header/dashboard evidence;
`cambium quota status` displays the content-free snapshot. Providers without a
configured quota window continue to use the debt-balancing allowance rather
than inventing a provider contract.

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
`DebtStore` and quota state flowing through `QuotaLedger`.
