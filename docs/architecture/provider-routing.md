# Provider routing and resource control

**Status:** current routing ownership contract plus target agent-visible resource
projection. Source and tests remain authoritative.

## 1. Problem

A provider decision is not merely “pick the best model.” It is admission under
hard constraints followed by optimization under uncertain, changing evidence:

```text
credentials and authorization
context/output/tool capability
provider/model enablement
task quality constraints
request rate and in-flight capacity
token/quota windows
prepaid cash or subscription scarcity
provider health and Retry-After
prompt-cache affinity and switching cost
wall deadline
```

Parent continuation and independent child work have different affinity needs.
A root semantic branch should remain stable while feasible; a separable child
may use another lane when that improves capability, capacity, cost, or
independent review value.

## 2. One ownership path

Routing is split by time scale, not duplicated:

```text
provider configuration
  capabilities, auth/protocol mode, tariffs, cache/quota declarations
            |
            v
supervisor admission / routing.py
  hard feasibility, authorization, credential readiness,
  model constraints, lane/quota availability, ranking, lease
            |
            v
worker pinned to provider/model lease
            |
            v
Diffundo call-time execution
  direct health evidence, deadlines, retry, cooldown, bounded cascade
            |
            v
transport/provider response
  usage, cache, rate, quota, error evidence
            |
            v
usage debt / quota reconciliation / next admission
```

`provider_scheduler.py` owns shared immutable lease/cache/quota values and
transactional reservations. It is not another scheduler. `routing.py` owns
admission. `diffundo.py` owns attempts after admission. `provider_config.py`
owns declared capabilities. The supervisor owns the branch lease.

## 3. Hard feasibility

Admission first constructs a hard-feasible set. A candidate is excluded when:

- provider or model is disabled;
- required credentials are unavailable;
- the provider lies outside inherited authorization;
- context/output or declared capability cannot satisfy the task;
- task quality, paid/free, or model constraints reject it;
- quota/cash reservation or lane capacity blocks dispatch;
- an exact context request is incompatible with provider/model/protocol/prompt/
  tool/checkpoint identity;
- a permanent configuration/authentication quarantine applies.

Prompt prose cannot override these facts. Unknown capability does not satisfy a
positive hard requirement.

Only after this filter may Cambium rank candidates.

## 4. Ranking objective

Within the feasible set, the target policy minimizes regret using measured or
configured evidence:

```text
utility =
    expected quality
  + expected throughput
  + useful idle-capacity value
  - cash cost
  - quota shadow price
  - expected verification/rework cost
  - provider/cache switching cost
  - uncertainty penalty for weak evidence
```

The exact formula is versioned and auditable. A hard requirement is never given
a finite penalty that allows a sufficiently attractive score to bypass it.

Current source already records requests, tokens, failures, cost, cache-hit
count, latency, throughput evidence, Retry-After pressure, and durable
configuration/auth quarantine. A placeholder token-window allowance remains
where real account contracts are unavailable. Target work replaces persuasive
placeholders with measured/configured windows or `unknown`.

## 5. Leases

A lease is branch state:

```text
provider
model
protocol/reasoning identity
credential authority fingerprint (never secret value)
context/checkpoint affinity
acquired_at / source evidence
migration status
```

### Root continuation

The root keeps its provider/model lease through normal turns while feasible.
Transient slowness or one failed request does not silently create a new semantic
branch.

### Exact child

`trunk + inherit` pins the compatible parent provider/model and exact checkpoint
prefix. If compatibility fails, the explicit request is rejected rather than
downgraded.

### Semantic/fresh child with inherit

The child keeps the parent provider/model but uses semantic-only or fresh
context as requested. Context representation and placement are orthogonal.

### Semantic/fresh child with spread

Inherited hard pinning is removed. Admission first prefers another feasible
lane and then falls back to the complete feasible set. Spread is a throughput
preference, not permission to violate capability, authorization, quota, or cash
constraints.

### Migration

A permanently infeasible root lease uses an explicit transition:

```text
latest safe checkpoint
    -> classify infeasibility
    -> select hard-feasible migration target
    -> construct exact/semantic/fresh continuation honestly
    -> reserve target resources
    -> emit provider_lease_migrated
    -> continue under new lease
```

Migration loss of cache affinity is visible. It is never hidden inside a retry.

## 6. Failure classification

Provider outcomes require different state transitions:

| Class | Treatment |
| --- | --- |
| invalid/revoked credential | quarantine until credential identity changes or explicit recovery |
| missing entitlement/configuration | disable affected provider/model configuration |
| quota/rate/billing | cooldown/block until reset or top-up evidence |
| policy/content refusal | fail/fall through this request without damaging provider health |
| WAF/network/overload/stall | bounded retry/cooldown health transition |
| malformed provider output | request/model evidence; do not infer credential failure |
| success | update usage/throughput/cache evidence and clear eligible quarantine |

Health changes only from direct evidence about the attempted lane. A sibling
provider result cannot rehabilitate or damage another lane.

## 7. Accounting dimensions

Keep separate ledgers or fields for:

```text
request-rate tokens
in-flight slots
input tokens
cached input tokens
cache write tokens/cost
output tokens
time-window quotas
prepaid cash
subscription scarcity
wall time
retry/backoff
verification and rework
```

Cached tokens are not automatically free and may still count against provider
limits. Unknown tariffs/limits remain unknown. Each reservation has an owner,
expiry/reconciliation path, and durable result.

## 8. Cache capability

Provider configuration may describe:

```text
minimum cacheable tokens
cache TTL
cache block granularity
cache-read price
cache-write price
```

CAST may use this to decide breakpoint batching and K0 rollover economics. A
configured TTL supports only a pre-call warm estimate. `provider_cache_hit`
comes from provider evidence after the call.

Exact fork compatibility additionally requires provider/model/protocol,
reasoning mode, stable system prompt, tool-schema hash, checkpoint hashes, and
authorization identity. Cross-provider semantic reuse is always cold on the new
provider even when summary text matches.

## 9. ResourceEnvelope for the agent

The target model surface is a bounded policy projection, not raw scheduler
state:

```text
remaining turns and wall
context pressure
uncached-token pressure
provider/model lease
cache affinity and warm estimate
quota pressure
cash pressure
delegation overhead
alternative feasible lane availability
```

Common values use `low`, `medium`, `high`, `critical/blocked`, or `unknown`.
Exact underlying values remain available through `inspect_state(resources)`.
Thresholds are versioned.

The ResourceEnvelope lets the agent decide whether to continue, retrieve,
delegate, verify, or finish. It does not let the model choose a credential or
bypass admission.

Examples:

- high context pressure favors exact evidence retrieval over transcript replay;
- low wall time makes non-critical child creation unattractive;
- a warm exact root lease favors continuation for coupled work;
- idle alternative capacity favors `semantic + spread` only for separable work;
- high quota pressure discourages speculative calls but cannot justify skipping
  required verification silently.

## 10. Observability and explanation

A provider decision should be reconstructible from durable bounded evidence:

```text
request/task constraints
excluded candidates and hard reasons
candidate scores and evidence age/sample count
selected lease
reservation result
attempt outcomes
usage/cache/quota evidence
migration or release
```

The TUI and model SituationFrame share lease and pressure semantics through the
target BranchState. Secrets, bearer tokens, and raw credential material never
enter these records.

## 11. Invariants

- One scheduler ownership path.
- Hard feasibility before ranking.
- Credentials never enter task specs, prompts, events, or commits.
- Health changes only on direct evidence.
- Content/policy refusal does not poison provider health.
- Pinned-model/root-lease behavior is explicit.
- Every legal migration starts from a safe checkpoint.
- Request rate, concurrency, tokens, windows, cash, and cache remain separate.
- A child cannot widen parent provider authority.
- Cross-provider semantic reuse is never labelled a cache hit.
- Unknown economics remain unknown.
- The model expresses intent; the supervisor resolves the actual lease.

## 12. Current versus target

Current:

- admission from credential/capability/authorization constraints;
- usage-debt and lane-aware selection;
- provider/model pinning;
- cache/quota value objects and transactional reservations;
- typed call outcomes, deadlines, retry/cooldown, and bounded cascade;
- declared child inherit/spread materialization;
- operator usage/quota projection.

Target:

- real account-window/cash models replacing placeholders;
- explicit root lease migration event/protocol;
- uncertainty-aware ranking and recorded explanation;
- model-visible ResourceEnvelope from the canonical BranchState;
- measured switching/delegation/verification costs;
- held-out policy evaluation before promotion.

The target work is ordered in `../../implementation-plan.md` Phase 6.
