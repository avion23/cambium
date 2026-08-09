# Diffundo Provider Cascade — Design

**Date:** 2026-08-09
**Worktree:** `/tmp/opencode/cambium-cascade` (branch `wt-cascade`)
**Inputs (read-only):** `docs/architecture.md` (v2, at `/tmp/opencode/cambium-arch/docs/architecture.md`), `docs/system-design.md` (v0.1, superseded), `docs/reviews/review-llm-design.md` (LLM-C2, LLM-M6).
**Status:** design (docs only). Normative extension of architecture §9; defers only where explicitly flagged.
**Verification rule:** every claim about existing documents cites a section (`[arch §N]`, `[sysd §M2]`, `[rev-llm C2]`). Claims the architecture does not make are marked **design**; claims that depend on data no available source provides are marked **UNVERIFIED**.

**Provenance note:** `docs/research/provider-landscape.md` **does not exist** in main as of 2026-08-09 (verified by glob; the research directory contains `cloud-code.md`, `codex.md`, `omp.md`, `opencode.md`, `pi.md`, `prime-agent.md`, `pydev.md`, `python-3.14.md`, `tui-best-practices.md`). Per-provider pricing, real context windows, and tool-calling support cannot be validated from a landscape doc. All such numbers in this document are **UNVERIFIED design defaults**, not facts.

---

## 0. What this document resolves

| Review finding | Disposition |
|---|---|
| **LLM-C2** — "Provider cascade does not actually cascade across models." The v0.1 `if provider.model != model: continue` guard, combined with `model` always being resolved to `providers[0].model`, made the cascade a single-provider no-op `[rev-llm C2]`, `[sysd §M2]`. | Resolved by architecture v2 via a **`tier` primary key** `[arch §9.2]` and pinned here into an ordered per-tier fallback list with explicit fall-through triggers (§1.1–§1.2). |
| **LLM-M6** — "Race mode can silently discard a superior result and has no exception hygiene." v0.1 `_race` used `FIRST_COMPLETED` + `winner.result()` (re-raises if the first *finished* task raised), cancelled pending tasks, and biased results to the fastest — typically weakest — provider `[rev-llm M6]`, `[sysd §M2]`. | Architecture removes race from the default config `[arch §9.2]`. This document additionally defines **exact opt-in race semantics** that fix the discard and hygiene bugs (§1.3–§1.4), because the architecture keeps same-priority providers available and race is still reachable by explicit configuration. |

The cascade design below is the normative reading of `[arch §9]` plus the extensions needed to make §1–§7 of this document implementable. Extensions are labeled **design** inline.

---

## 1. Cascade semantics

### 1.1 Ordered fallback list per tier

The cascade operates on a **candidate list per tier**, not on the global provider set:

1. Filter `providers` to those whose `tier` equals the request tier `[arch §9.2]` (the request default is `tier="fast"` `[arch §9.2]`).
2. Apply capability filters — `supports_tools=False` is skipped when `require_tools=True`; `context_window < min_context_window` is skipped `[arch §9.1, §9.2]`. These are explicit, documented tradeoffs `[arch §9.2]`.
3. Drop providers in `cooldown` `[arch §9.2 step 2]` or circuit-`OPEN` (§2.3).
4. Sort survivors by `priority`, lower first `[arch §9.1, §9.2 step 3]`.

The **fallback order is `priority` ascending within the tier** `[arch §9.1]`. The review example "DeepCode Flash → Gemini Flash → OpenAI Mini → Claude Haiku" `[rev-llm C2]` is exactly a priority-ordered fast tier; a request for `"fast"` matches any of them interchangeably `[arch §9.2]`. The only time an exact model string is a constraint is when the caller passes an explicit `model=` (used by optimization to pin a model) `[arch §9.2]` — never by default.

**(design)** An optional explicit `fallback_order: [name, ...]` per provider (§6) is supported as a tie-breaker *after* `priority`. Where both are present, `priority` wins and `fallback_order` is advisory. UNVERIFIED: no landscape data exists to validate whether priority-by-model ranking should come from config or from a provider registry.

### 1.2 When to fall through

A sequential cascade attempt yields control to the next provider in the tier list when the current attempt terminates in one of these four classes. The class also decides retry eligibility (§2.1):

| Class | Examples | Retryable? | Fall through |
|---|---|---|---|
| **Timeout** | attempt exceeds per-provider `timeout_s` (attempt-level), or the shared `call_budget_s` deadline expires (§2.2) | yes | next provider; budget exhaustion stops the loop |
| **Error** | transport failure, 5xx, malformed response | yes | next provider |
| **Quota** | HTTP 429, `insufficient_quota`, rate-limit error | yes (with backoff) | next provider |
| **Refusal** | content-policy refusal, refusal marker in completion, 4xx content rejection | no | next provider |

**(design)** Attempt-level timeout `timeout_s` per provider is added to `ProviderConfig` (§6). The architecture specifies cooldown on exception `[arch §9.2 step 4]` but does not specify a per-attempt timeout field; v0.1 had a single `FanOutConfig.timeout = 30.0` `[sysd §M2]`, which this design replaces with per-provider values.

A provider is marked for cooldown on exception `[arch §9.2 step 4]`. Refusals count as a provider-side failure of the *request* (fall through), but are recorded separately from outages so an operator can distinguish "model refused" from "model down" (§5).

**(design)** If **every** tier provider refuses the same request, the call fails with `AllProvidersFailed` carrying per-provider outcomes (§5.1). This is distinct from "all providers down": the envelope must include the refusal outcome so the caller can distinguish content-policy failure from infrastructure failure. UNVERIFIED: whether the orchestrator should treat all-refused as a task-level failure (`recoverable: false`) rather than retryable.

### 1.3 Race mode — exact winner-selection semantics (fixes LLM-M6)

Race is **not** the default: architecture removes race mode from the default config because of LLM-M6 (fastest-typically-weakest bias, cancelled metered requests) and notes that same-priority providers in cascade already give "first of N" latency behavior `[arch §9.2]`. Race is opt-in per call or per config. When enabled, it must not reintroduce the two LLM-M6 pathologies. The semantics below are **design**, written to fix them.

```
race(prompt, tier, n):                                   # n = race_redundancy (§6)
  start = time.monotonic()
  deadline = min(start + call_budget_s, start + race_timeout_s)   # fixed at entry
  candidates = first n of tier_list(...)                  # §1.1 filters apply
  tasks = {p: create_task(attempt(p, timeout=timeout_s[p]))
           for p in candidates}                           # explicit provider -> task map
  results = {}; best = None; gated_winner = None
  while tasks:
      now = time.monotonic()               # refresh each iteration; deadline is fixed
      if now >= deadline: break
      done, pending = await asyncio.wait(
          list(tasks.values()), FIRST_COMPLETED, return_exceptions=True,
          timeout=deadline - now)          # remaining budget shrinks each pass
      done_by_provider = {p: t for p, t in tasks.items() if t in done}
      for p, task in done_by_provider.items():
          r = task.result()                # safe: exceptions returned as values
          results[p] = r
          if isinstance(r, Exception):     # ★ exception hygiene: a crashed provider
              record_failure(p, r)         #   never becomes the winner; it is recorded
              continue                     #   and the race keeps waiting (§1.4)
          if quality_gate(r):              # ★ first-complete wins ONLY if gate passes
              gated_winner = (p, r); break #   else we keep waiting (§1.3.2)
          if best is None or score(r) > score(best):
              best = (p, r)                # ★ superior result is never discarded (§1.3.3)
      if gated_winner: break
      tasks = {p: t for p, t in tasks.items() if t in pending}   # keep survivors
  for t in tasks.values():
      t.cancel()                           # best-effort; may still be billed (§3.4)
  if gated_winner:   return envelope(gated_winner)
  if best is not None: return envelope(best) # ★ no gated pass → best-by-score wins
  raise AllProvidersFailed(sorted(results.items()))   # every attempt ended in exception
```

Three rules in the loop are the LLM-M6 fixes:

1. **First-*completed* is not first-*succeeded*.** `asyncio.wait(..., FIRST_COMPLETED)` returns the first task that *finished*, which may have finished by raising `[rev-llm M6]`. With `return_exceptions=True`, `task.result()` returns the exception as a value instead of re-raising `[rev-llm M6]`, so a crashed provider cannot terminate the race (`§1.4`).
2. **First-complete wins only if the quality gate passes.** If the first completed result fails the gate, the race keeps waiting for later completions — it does not settle for a fast-but-failed gate.
3. **Best-by-score fallback, not discard.** If the deadline or task exhaustion arrives with no gated success, the race returns the highest-`score` completed result instead of raising. The v0.1 code discarded all losing providers' work and took whichever task finished first regardless of quality `[rev-llm M6]`; here, a superior result already in hand is never thrown away.

**(design)** `quality_gate` is a deterministic checker first (non-empty completion, valid JSON when a schema is expected, no refusal marker, required fields present). An LLM-judge gate for `strong`/`reasoning` tiers is optional and default-off because it costs an extra call (§3.4). **(design)** `score()` is a deterministic monotone proxy (gate-passing result > gated-failure; among gated-failures, a rubric heuristic such as length/format constraints). Both gate and scorer are **UNVERIFIED** — the architecture defines no quality gate (it defines the multi-signal coding metric `[arch §10]`, which is a *task* metric, not a per-completion gate), and no per-completion scoring exists in the sources.

### 1.4 Exception hygiene — a crashed provider must not poison the race

- **No blind `winner.result()`.** Every task result is read with `return_exceptions=True` and dispatched on `isinstance(r, Exception)` before any winner logic runs. This removes the v0.1 re-raise path `[rev-llm M6]`.
- **Failures are recorded, not propagated.** A crashed provider contributes a `record_failure` (health state §2.4, cost event §3.4) and drops out of the running; the race continues with the remaining tasks.
- **Cancellation is best-effort and accounted.** `cancel_all(pending)` mirrors v0.1's cancellation `[rev-llm M6]`, but this design records a `race_cancelled` cost event for each cancelled attempt, because a cancelled in-flight HTTP request to a metered provider may still count against quota `[rev-llm M6]` (§3.4).
- **Concurrency guard.** All shared mutation (cooldown map, cache, breaker state) is protected per the DS-M4 resolution: cache is per-instance and mutated only from the owning process; cooldown tracked in a `threading.Lock`-protected structure when needed `[arch §18.1 DS-M4]`.

### 1.5 Call-level state machine

```
                    ┌──────────────┐
   call(tier,...) ─►│  CACHE LOOKUP│──hit──► return envelope(cache_hit=true)  [arch §8.1]
                    └──────┬───────┘
                           │ miss (or cache=False)
                           ▼
                    ┌──────────────┐
                    │  TIER SELECT │  filter tier + capabilities + cooldown/OPEN
                    │  + SORT      │  [arch §9.1, §9.2]
                    └──────┬───────┘
                           ▼
            mode=cascade (default)     mode=race (opt-in)
            ┌──────────────────┐       ┌──────────────────┐
            │ try p0; fall      │       │ fire n; wait      │
            │ through p1..pk    │       │ per §1.3 rules    │
            │ (timeout/error/   │       │ (gate + best-by-  │
            │  quota/refusal)   │       │  score + hygiene) │
            └─────────┬────────┘       └─────────┬────────┘
                      │ success                  │ success
                      └───────────┬──────────────┘
                                  ▼
                    ┌──────────────────────────┐
                    │  CACHE WRITE (if cache=T │  write winning envelope [arch §8.1, §9.2]
                    │  && context_hash present)│
                    └──────────────────────────┘
                                  ▼
                    ┌──────────────────────────┐
                    │  RETURN envelope         │  or, all failed:
                    │  (provider/model/cost)   │  AllProvidersFailed(providers_tried,
                    └──────────────────────────┘  last_error) [arch §9.2]
```

---

## 2. Retry policy

### 2.1 Per-provider retries (backoff, jitter)

- **Scope.** Retries are per-attempt on the *same provider* for **retryable** classes only (§1.2): timeout, error, quota. Refusals and non-retryable 4xx do not retry (re-running against the same model is unlikely to change a refusal; it wastes budget and may trip provider rate limits).
- **Count.** `max_retries` is per provider in `ProviderConfig` `[arch §9.1]`, default 2.
- **Backoff + full jitter.** Between retries on the same provider, wait `delay = random.uniform(0, retry_backoff_base ** retry_n * base_delay_s)` with full jitter. This mirrors the worker restart policy's full-jitter backoff `[arch §7.4]`; the parameters `retry_backoff_base` and `base_delay_s` are **design** additions to the schema (§6), **UNVERIFIED** (no data on provider throttle curves exists).
- **Quota special case.** A 429 should be retried only if the provider exposes retry-after semantics; otherwise it is treated as a fall-through trigger (§1.2) so the cascade moves to a healthy provider instead of burning budget on the throttled one. **(design; UNVERIFIED)**

### 2.2 Total deadline budget per call

- **`call_budget_s`** bounds the entire `call()` — all cascade fall-throughs plus all retries plus a race's wait (§1.3) — from entry to raise-or-return. When the deadline is reached, in-flight attempts are cancelled and the call either returns the best completed result (race) or raises `AllProvidersFailed` (§5.1).
- The worker side already treats a long LLM outage as bounded: `Diffundo.AllProvidersFailed` raised inside a worker is caught at the tool boundary and retried internally for up to `provider_patience_s` (default 180 s) before the worker emits `error` `[arch §7.4]`. The per-call budget must therefore be smaller than `provider_patience_s`; **(design, UNVERIFIED)** default `call_budget_s = 60` so that at most three budget-consuming calls can occur inside one patience window.
- Relationship to v0.1: v0.1's single `FanOutConfig.timeout = 30.0` per provider `[sysd §M2]` meant a sequential cascade over the review's 4 fast-tier providers could wait up to 4 × 30 = 120 s worst-case `[rev-ds C3]` (the DS review computed the same product). This design replaces that unbounded product with a hard deadline: sum of (provider timeouts, retries, fall-throughs) ≤ `call_budget_s`.

### 2.3 Circuit breaker (per-provider error tracking — sliding window)

**(design — the architecture does not specify a circuit breaker.)** The architecture tracks failures only as a per-provider `cooldown_s` timer `[arch §9.1]` and marks cooldown on exception `[arch §9.2 step 4]`. This design layers a sliding-window circuit breaker over that timer to stop *repeated* attempts against a persistently failing provider:

- **Window:** the last `N` attempts for the provider (default `N = 20`), maintained per `Diffundo` instance, protected by the DS-M4 locking rule `[arch §18.1 DS-M4]`.
- **Trip:** if retryable failure rate over the window ≥ `failure_threshold` (default 0.5), the provider enters `OPEN` and is excluded from candidate selection `[arch §9.2 step 2 filter]` for `cooldown_s` × backoff.
- **Probe:** after the open interval, the provider enters `HALF_OPEN` and is admitted as exactly one probe candidate; success returns it to `HEALTHY`, failure re-opens it.
- **Parameters `N`, `failure_threshold` are UNVERIFIED design defaults** — no provider-outage data exists to calibrate them.

### 2.4 Provider health state machine

States and transitions (design, but built on the architecture's cooldown concept `[arch §9.1]` and the "not in cooldown" selection filter `[arch §9.2 step 2]`). Triggers are the attempt-outcome classes of §1.2 (timeout, error, quota, refusal) plus the §2.3 circuit metrics. **Refusals do not drive health transitions** — they are request-level fall-throughs recorded separately (§1.2), so "model refused" never marks a provider down.

**Circuit spine** (forward flow; the recovery loop — every label is a trigger):

```
  UNKNOWN ──1st success──► HEALTHY ──retryable failure──► COOLDOWN ──probe fails──► OPEN ──open interval──► HALF_OPEN ──probe succeeds──► HEALTHY
                                                                (retryable or         (cooldown_s ×
                                                                 timeout_s)           backoff elapses)
```

**Back edges and exits** (each also appears in the transition table below):

```
  COOLDOWN ──probe succeeds (cooldown_s elapsed)────────────────► HEALTHY
  COOLDOWN ──sliding-window failure rate ≥ failure_threshold────► OPEN          (escalation, §2.3)
  HALF_OPEN ──probe fails (retryable or timeout_s)──────────────► OPEN          (probe timeout explicit)
  {UNKNOWN, HEALTHY, COOLDOWN, HALF_OPEN} ──non-retryable error
      (auth/config)─────────────────────────────────────────────► DISABLED      (terminal; first call included)
```

**Exit to DISABLED:** every attempt state — `UNKNOWN`, `HEALTHY`, `COOLDOWN`, `HALF_OPEN` — exits to `DISABLED` on a non-retryable error (auth/config), **first call included**. `DISABLED` is terminal for the session (requires operator action) and has no outgoing transitions. `OPEN → DISABLED` is unreachable: while `OPEN` the provider is excluded from candidate selection (only the `HALF_OPEN` probe is admitted), so no non-retryable error can be observed in `OPEN`.

**Transition table (authoritative — diagram, text, and triggers agree):**

| From | To | Trigger | Notes |
|---|---|---|---|
| `UNKNOWN` | `HEALTHY` | first attempt succeeds | |
| `UNKNOWN` | `COOLDOWN` | first attempt fails with a retryable error (timeout/error/quota) | |
| `UNKNOWN` | `DISABLED` | first attempt hits a non-retryable error (auth/config) | first-call included |
| `HEALTHY` | `COOLDOWN` | retryable failure (timeout/error/quota) | |
| `HEALTHY` | `DISABLED` | non-retryable error (auth/config) | |
| `COOLDOWN` | `HEALTHY` | `cooldown_s` elapsed → probe succeeds | provider re-admitted as one probe candidate |
| `COOLDOWN` | `OPEN` | `cooldown_s` elapsed → probe fails (retryable or `timeout_s`) | probe-failure path explicit; a failure after a full cooldown window indicates persistence → escalate |
| `COOLDOWN` | `OPEN` | sliding-window failure rate ≥ `failure_threshold` (§2.3) | escalation can fire before the probe |
| `COOLDOWN` | `DISABLED` | probe hits a non-retryable error (auth/config) | |
| `OPEN` | `HALF_OPEN` | open interval (`cooldown_s` × `open_backoff_base`) elapses | |
| `HALF_OPEN` | `HEALTHY` | probe succeeds | |
| `HALF_OPEN` | `OPEN` | probe fails — retryable error **or timeout under the per-attempt `timeout_s` (§1.2)** | probe timeout explicit |
| `HALF_OPEN` | `DISABLED` | probe hits a non-retryable error (auth/config) | |
| `DISABLED` | — | terminal | no outgoing transitions |

- `UNKNOWN` — never called. `HEALTHY` — recent success. `COOLDOWN` — a transient failure; the provider is skipped for `cooldown_s` `[arch §9.1]`; on expiry it is re-admitted as one probe candidate. `OPEN` — circuit tripped (§2.3); excluded from candidate selection `[arch §9.2 step 2]` until the open interval elapses. `HALF_OPEN` — exactly one probe candidate is admitted; the probe runs under the per-attempt `timeout_s` (§1.2). `DISABLED` — a non-retryable error (invalid auth, malformed config) removed the provider for the session; reachable from any attempt state, **including the first call**; terminal.
- **(design)** Every transition emits a `provider_health_change` event (§5.2). This is **design** — architecture defines no such event kind; event kinds are extensible `[arch §3.6]` (`kind` is a string with an explicit ellipsis) but the enumeration and event-tier classification are a schema change (§7).
- Every state is per-`Diffundo`-instance (workers each construct their own client from `fanout_config` `[arch §9.3]`), so health is **not** shared across worker processes — consistent with the no-cross-worker cache rule `[arch §8.1]`; the durable, cross-process signal is the event log (§5.2).

---

## 3. Cost control

### 3.1 Budget caps per task / session

- The `init` message already carries a `budget` block (`max_wall_s` default 1800, `max_restarts` default 10) `[arch §5.2]`. **(design)** Add `max_cost_usd` to that block (task cap) and a session-level cap in `Config` (`Config` holds `fanout`, `supervisor`, `worker`, `sandbox`, `providers` `[arch §3.2]`; the cost fields are additions). Both are **UNVERIFIED** (no pricing data exists to justify defaults; proposed task default $1.00).
- **Enforcement point.** `Diffundo.call` checks the running task/session spend against the cap before every attempt and after every cache write. When the task cap is exceeded: **(design)** `Diffundo` raises `CostBudgetExceeded`; the orchestrator parks dispatch (same posture as `AllProvidersFailed` `[arch §9.2], [arch §18.3 IMPL-M5]`). When only the session cap is exceeded, new tasks are refused but in-flight work continues. UNVERIFIED: whether tier *downgrade* (re-route to `fast`) is preferable to failing is an open orchestrator policy question (§7).

### 3.2 Tier routing rules (which calls use which tier)

The architecture names the orchestrator modules `ShouldDecompose → TaskDecomposer → TaskRouter → ResultEvaluator` `[arch §2]` and routes every DSPy call through `Diffundo` via `CambiumLM(tier=...)` `[arch §9.3]`, but it does **not** prescribe per-module tiers. **(design, UNVERIFIED)** proposed routing:

| Caller | Tier | Rationale |
|---|---|---|
| Worker ReAct steps (workers configure `CambiumLM(diffundo, tier=...)`) `[arch §9.3]` | `fast` | highest call volume; cheapest model on the hot path; the "fastest-typically-weakest" bias race mode would introduce `[rev-llm C3]`, `[arch §9.2]` is acceptable here because cascade (not race) is the default |
| `ShouldDecompose` classifier | `fast` | cheap, repetitive, cache-friendly `[arch §8.1]` (genuinely stateless for a fixed spec) |
| `TaskDecomposer` | `balanced` or `strong` | decomposition quality gates everything downstream (review C4 coupling) |
| `ResultEvaluator` (LLM-judge) | `reasoning` | judgment quality matters most; lowest volume |
| Explicit `model=` (optimization pinning) | any | caller pins exactly `[arch §9.2]`; bypasses tier routing |

The tier taxonomy itself (`fast`/`balanced`/`strong`/`reasoning`) is from `[arch §9.1]`; the *mapping of real models to tiers* is **UNVERIFIED** (no provider-landscape; architecture names only fast-tier examples `[arch §9.2]`).

### 3.3 Cheap-first default

- `call()` defaults to `tier="fast"` `[arch §9.2]` and to `mode="cascade"` `[arch §9.2]`.
- Within a tier, `priority` ascending (lower first) `[arch §9.1]` — i.e., the cheapest/least-busy subscription is tried first, matching the v0.1 intent "tries cheap subscriptions first" `[sysd §M2]` and the review's example ordering `[rev-llm C2]`.
- Cheap-first is therefore the *composition* of default-tier + priority ordering, not a separate mechanism.

### 3.4 Cost accounting events

**(design — the architecture defines the event log `[arch §6]` and its schema `[arch §6.3]` but no cost events.)**

- **`llm_call` event** per attempt: provider, model, tier, prompt/response token counts, est. cost, mode (cascade/race/cache), outcome (success / class from §1.2 / cancelled), duration. Estimated cost derives from per-provider `price_per_1m_in/out` (§6); token counts come from the provider response where available, else estimated. UNVERIFIED: provider response shapes for token counts are not in any source.
- **`race_cancelled` accounting:** a cancelled attempt is billed at whatever the provider reports or, absent that, at an estimate using the tokens the provider returned before cancellation. This is the accounting half of LLM-M6's "cancelled in-flight HTTP request may still count against quota" `[rev-llm M6]`.
- **Event tiering.** `llm_call` and `provider_health_change` are **non-critical** (the critical set is fixed as `result`, `checkpoint`, `worker_exit`, `task_failed`, `merge_progress`, `task_assigned`, `merge_committed` `[arch §6.5]`; cost events are advisory telemetry). **(design)** `all_providers_down` should be added as **critical** so a subscriber can rely on it surviving a supervisor crash; this is a schema change (§7).
- **Redaction.** Events are redacted at enqueue time `[arch §6.2], [arch §12]`; cost/health events must never include API keys or prompt text. Workers receive `api_key_env` *names*, never values, over the protocol `[arch §5.2], [arch §12.2]`.

---

## 4. Cache interaction

### 4.1 The cache sits BEFORE the cascade

- `Diffundo.call` does the cache check first, before provider selection or any attempt `[arch §9.2 step 1]`.
- The cache lives in `Diffundo`, upstream of workers `[arch §8.1]`, and is **opt-in per call** — default `cache=False`; enabled by passing `cache=True` + `cache_namespace` + `context_hash` `[arch §8.1]`. Workers do not cache codegen by default; the orchestrator caches genuinely stateless calls `[arch §8.1]`.
- Key = `sha256(namespace || model || temperature || prompt || context_hash)` `[arch §8.1]`. The task's "task+context+model" maps to: namespace/task via `cache_namespace`, world state via `context_hash` (must include `git rev-parse HEAD` + a hash of relevant file contents for code-aware calls `[arch §8.1]`), and the model string. TTL default **300 s** `[arch §8.1]` (the v0.1 3600 s TTL is superseded `[arch §8.1]`).
- Cache is per-instance LRU (default 10 000 entries), **never shared across worker processes** `[arch §8.1]`.

### 4.2 Cache hit skips the cascade

A hit returns the stored envelope directly, skipping provider selection, cascade, race, retries, and cost of a new attempt `[arch §9.2 step 1]`. Every hit is tagged `"cache_hit": true` with the original generation timestamp `[arch §8.1]` — so optimization harnesses can filter cache hits out of trajectory datasets `[arch §8.1]`.

### 4.3 Cache write comes from whichever provider won

On success (cascade winner, or race winner under §1.3), the response is written to the cache **only if** `cache=True` and `context_hash` is present (calls omitting `context_hash` are rejected when caching is requested `[arch §8.1]`). **(design)** the cached envelope stores provenance: winning `provider`, `model`, `tier`, `estimated_cost`, and the original generation timestamp (already required by `[arch §8.1]`). A later hit therefore returns a result that records *which provider produced it*, so cost attribution and provenance survive the cache. This extends `[arch §8.1]`'s `cache_hit` tagging with winner metadata (the architecture stores the response; it does not specify storing provenance).

### 4.4 Negative caching (provider failure memoization)

**(design, UNVERIFIED — not present in any source.)** The architecture's only failure memoization is the per-provider cooldown timer `[arch §9.1]`. This design adds a *per-request* negative cache as an optional refinement:

- **What:** a short-TTL store keyed by `(cache_key, provider.name)` recording "this exact call failed on this provider" for deterministic failure classes (refusal, auth, permanent error) — not for transient errors, which are already covered by cooldown.
- **Why:** byte-identical prompts recur in the harness — the review notes the cache is hit by highly repetitive decomposition, evaluation, and routing prompts across tasks `[rev-llm C1]`; re-running the full retry + fall-through loop against a provider that already refused the same request wastes budget and latency. The negative entry makes the cascade skip that provider for that exact call.
- **Safety:** safe only because the key includes `context_hash` `[arch §8.1]` — a moved world state changes the key and re-enables the provider. Negative entries live only in the per-instance cache (LRU, default TTL 30 s, bounded), never in the durable event log.
- **Scope:** populated only for calls with `cache=True` (the same opt-in discipline as positive caching `[arch §8.1]`). Recommendation: keep this out of v2 scope if implementation budget is tight; cooldown already bounds repeated hammering at the provider level (§2.4).

---

## 5. Failure semantics

### 5.1 All providers down → error envelope, not a hang

- `Diffundo` raises **`AllProvidersFailed(providers_tried, last_error)`** when every tier candidate fails `[arch §9.2 step 5]`; it is a real exception class in `cambium.diffundo.errors` carrying the tried providers and last error `[arch §9.2]`.
- **No hang by construction:** the wait is bounded by `call_budget_s` (§2.2) plus per-attempt timeouts, so the call returns or raises within a deadline, and the worker-side patience loop is bounded by `provider_patience_s` (default 180 s) `[arch §7.4]`.
- **Worker view:** `AllProvidersFailed` is caught at the worker's tool boundary, logged, and converted to a backoff retry inside the worker for up to `provider_patience_s`; only after that does the worker emit `{"type":"error", ..., "recoverable": true}` `[arch §7.4]`. The supervisor then treats it as a recoverable worker error — it consumes restart budget only after patience expires, and a *provider outage is not a worker failure* (resolves DS-M7) `[arch §7.4], [arch §18.1 DS-M7]`.
- **Supervisor view:** the deterministic layer never calls an LLM and never imports a DSPy module; a total LLM/provider outage leaves existing workers running and the supervisor healthy `[arch §2]`. The orchestrator catches `AllProvidersFailed` and parks dispatch `[arch §9.2], [arch §18.3 IMPL-M5]` — i.e., no new tasks are spawned until a provider recovers.

### 5.2 Supervisor-visible health event

**(design — the architecture's event kinds `[arch §6.3]`, `[arch §3.6]` do not include provider-health events.)** `Diffundo` emits:

- `provider_health_change` on every §2.4 transition (provider, from-state, to-state, window rate, reason).
- `all_providers_down` when the last non-`DISABLED` provider in any requested tier leaves `HEALTHY`.

The supervisor and host observe these via `Session.events()` `[arch §3.3]`, stored in the durable event log `[arch §6.1]`. Because the deterministic layer cannot act on LLM state directly (it never calls the LLM `[arch §2]`), the supervisor's role is **observability + dispatch gating**: the `all_providers_down` event is the reason the host sees no task progress, and the orchestrator's park-on-`AllProvidersFailed` is the enforcement `[arch §9.2]`. Event durability tiering (§3.4): `provider_health_change` non-critical, `all_providers_down` critical (schema change, §7).

---

## 6. Config schema (JSON example)

Architecture `ProviderConfig` fields: `name`, `model`, `tier` (`fast|balanced|strong|reasoning`), `api_key_env`, `base_url`, `priority`, `context_window`, `supports_tools`, `cooldown_s`, `max_retries` `[arch §9.1]`. The example below shows those fields plus the **design** additions of §1–§3 (`timeout_s`, retry params, cost, `enabled`, `fallback_order`). Fields marked **(design)** are additions this document proposes; the rest are `[arch §9.1]`.

```jsonc
{
  "diffundo": {
    "default_mode": "cascade",              // "cascade" | "race"; race opt-in [arch §9.2]
    "default_tier": "fast",                 // [arch §9.2]
    "call_budget_s": 60,                    // (design) §2.2 hard deadline per call()
    "race": {                               // (design) §1.3; opt-in
      "enabled": false,
      "redundancy": 2,                      // n candidates; matches v0.1 race_redundancy [sysd §M2]
      "timeout_s": 30,
      "quality_gate": { "mode": "deterministic", "judge_tier": null }   // (design, UNVERIFIED)
    },
    "circuit_breaker": {                    // (design, UNVERIFIED) §2.3
      "window_size": 20,
      "failure_threshold": 0.5,
      "open_backoff_base": 2.0
    },
    "cost": {                               // (design, UNVERIFIED) §3.1
      "per_task_max_usd": 1.00,
      "per_session_max_usd": null
    },
    "negative_cache": { "enabled": true, "ttl_s": 30 },   // (design, UNVERIFIED) §4.4
    "providers": [
      {
        "name": "deepcode",
        "model": "deepseek-v4-flash",       // provider-specific model id [arch §9.1]
        "tier": "fast",
        "api_key_env": "DEEPCODE_API_KEY",  // name only; never the key [arch §9.1, §12]
        "base_url": null,
        "priority": 0,                      // lower tried first, within tier [arch §9.1]
        "enabled": true,                    // (design) §2.4 DISABLED state
        "fallback_order": ["gemini", "openai", "claude"],   // (design) advisory after priority (§1.1)
        "timeout_s": 30,                    // (design) §1.2 per-attempt timeout
        "max_retries": 2,                   // [arch §9.1]
        "retry_backoff_base": 2.0,          // (design, UNVERIFIED) §2.1
        "retry_jitter": 1.0,                // (design, UNVERIFIED) full-jitter, mirrors [arch §7.4]
        "cooldown_s": 60.0,                 // [arch §9.1]
        "context_window": 200000,           // [arch §9.1] for routing decisions
        "supports_tools": true,             // [arch §9.1] native function calling
        "price_per_1m_in": 0.00,            // (design, UNVERIFIED) §3.4
        "price_per_1m_out": 0.00            // (design, UNVERIFIED) §3.4
      },
      {
        "name": "gemini", "model": "gemini-flash", "tier": "fast",
        "api_key_env": "GEMINI_API_KEY", "priority": 1,
        "context_window": 1000000, "supports_tools": true,
        "timeout_s": 30, "max_retries": 2, "cooldown_s": 60.0
      },
      {
        "name": "openai", "model": "openai-mini", "tier": "fast",
        "api_key_env": "OPENAI_API_KEY", "priority": 2,
        "context_window": 200000, "supports_tools": true,
        "timeout_s": 30, "max_retries": 2, "cooldown_s": 60.0
      },
      {
        "name": "claude", "model": "claude-haiku", "tier": "fast",
        "api_key_env": "ANTHROPIC_API_KEY", "priority": 3,
        "context_window": 200000, "supports_tools": true,
        "timeout_s": 30, "max_retries": 2, "cooldown_s": 60.0
      }
    ]
  }
}
```

The example's fast-tier list is the review's example order `[rev-llm C2]`, which architecture §9.2 cites as the interchangeable fast tier `[arch §9.2]`. **No actual model id, context window, or price in this example is verified** (no provider-landscape; model ids in `[arch §9.1]`/`[rev-llm C2]` are illustrative). Provider config is part of `Config` as `providers: tuple[ProviderConfig, ...]`, never serialized to logs `[arch §3.2]`; API keys come from `api_key_env` names resolved from the environment `[arch §9.1], [arch §12]`.

---

## 7. Open questions for the orchestrator

1. **Tier taxonomy validation.** Architecture defines `tier: Literal["fast","balanced","strong","reasoning"]` `[arch §9.1]` but only names fast-tier models `[arch §9.2]`. **`provider-landscape.md` does not exist in main** (verified absent, 2026-08-09), so there is no data answering: which providers/models occupy `balanced`/`strong`/`reasoning`; what are their real `context_window`, `supports_tools`, prices; and should tier membership live in config or in a registry. All UNVERIFIED.
2. **Race quality gate cost.** `quality_gate`/`score()` (§1.3) are deterministic by default; an LLM-judge gate on `strong`/`reasoning` costs an extra call (§3.4). Does the orchestrator accept that cost for latency-critical evaluation steps, or is race only ever used with deterministic gates?
3. **Budget-pressured downgrade vs fail.** On `CostBudgetExceeded` (§3.1), should `Architectus` re-route remaining work to `fast`, or fail the task? The architecture's posture for `AllProvidersFailed` is park-dispatch `[arch §9.2]`; cost exhaustion has no analogous mechanism.
4. **Event schema extension.** `provider_health_change`, `llm_call`, `all_providers_down` (§3.4, §5.2) extend the `kind` enumeration of `[arch §6.3]` and add `all_providers_down` to the critical tier of `[arch §6.5]`. Needs a schema-version bump and replay-compat decision (`snapshots` compaction `[arch §6.1]`).
5. **Circuit-breaker calibration.** `window_size`/`failure_threshold` (§2.3) are UNVERIFIED defaults; requires either landscape data or an operational tuning pass.
6. **Negative-cache scope.** Keep in v2 (an optimizer for ReAct repetition) or defer to v2.1 (cooldown already bounds provider-level hammering, §4.4)?
7. **Per-call budget vs patience sizing.** `call_budget_s = 60` < `provider_patience_s = 180` `[arch §7.4]` is a design guess; needs measurement of real provider p95 latencies to set the ratio.

---

## References

- `docs/architecture.md` (v2) — §2 (layering invariants), §3.2–3.6 (Config/Session/Result/Event), §5.2 (init/error message schema), §6 (event log: schema §6.3, durability §6.5), §7.4 (restart policy, provider_patience_s, DS-M7), §8.1 (cache policy), §9 (provider cascade: §9.1 ProviderConfig, §9.2 cascade semantics, §9.3 worker integration), §10 (coding metric), §12 (secrets), §18 (resolution matrix; DS-M4, IMPL-M5).
- `docs/system-design.md` (v0.1, superseded) — M2 Diffundo (the buggy `_cascade`/`_race`, `FanOutConfig`, cache key `[sysd §M2]`).
- `docs/reviews/review-llm-design.md` — C1 (cache staleness), C2 (cascade no-op), C3 (transparency), M6 (race discard + hygiene).
- `docs/reviews/review-distributed-systems.md` — C3 (worst-case cascade latency product).
- **Absent:** `docs/research/provider-landscape.md` — verified not in main on 2026-08-09; any per-provider datum derived from it is flagged **UNVERIFIED** throughout.
