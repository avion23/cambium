# Implementation plan

Ordered work only. Source and tests decide when a step is complete; this file
is not a branch ledger or merge log.

## 1. Smallest production hierarchy slice: static waves

- Make the harness own one explicit validated `TaskTree`. Integrate
  `build_tree`, `ready_tasks`, and `topological_order` with `run_plan` so static
  ready-node waves, dependency order, and width limits control admission.
- Give every admitted child a fresh bounded context derived from its own task
  and allowed parent envelope. Permit upward data only through the strict
  envelope key set; do not expose sibling context or an unbounded transcript.
- Acceptance measures: a fixed fixture proves exact ready waves, no unready
  dispatch, width enforcement, bounded child context, and exact envelope keys;
  failed children stop dependent admission.

## 2. Validated dynamic child admission

- After the static slice is reproducible, let a parent propose a child only as a
  typed tree revision. The harness validates and durably records each revision
  before admission; a provider response cannot mutate the live tree directly.
- Connect the injected Architectus decision port and conversation persistence
  only at this boundary, with explicit schemas and failure paths.
- Acceptance measures: duplicate, cyclic, multi-parent, over-depth, and
  over-width revisions spawn nothing; a valid child is admitted only at a ready
  wave and its envelope is visible only to its parent.

## 3. Provider usage, prompt stability, and quota contract

- Specify redacted durable usage events: provider, model, request/turn,
  token fields, cost, latency, Retry-After, request-rate status, account-quota
  owner, and failure reason.
- Measure prompt-prefix stability and provider-reported cache-hit metrics for
  the same fixed prompt fixtures. These metrics are requirements for routing
  decisions, not evidence that a local response cache exists.
- Connect accounting at the supervisor/event boundary and define behavior when
  rate-limit, token, cost, or account-quota state is unavailable. Preserve
  environment-only secrets.
- Admission-time model selection (solution C): tasks may declare
  `model_candidates`; the supervisor balances (model, provider) from a durable
  usage-debt ledger (`~/.config/cambium/routing-state.json`) before the model
  filter partitions the pool, and binds each task to the assigned provider.
- Provider lanes (H1): one concurrency lane per provider; `run_plan`
  pre-assigns each wave's un-pinned tasks in one pass, and 429 pressure
  decays a lane's in-flight cap (`routing.select_lane`). The existing `rpm`
  provider field is the lane allowance.
- Capability/quality-constrained selection (H2): tasks may declare
  `requirements` (quality high/normal, optional `min_context_window`); the
  supervisor filters providers strictly by capability (fail-closed on unknown
  keys) and picks the lowest `routing.score_providers` score — utilization,
  cache-hit rate, latency, and a shadow price — instead of `select_lane`, with
  placeholder module-constant weights until measured quality/latency data
  exists (step 5).
- Test 429 `Retry-After`, same-provider retry, `RATE_LIMITED` buckets, and
  provider fallback against the contract. Do not introduce weighted routing
  until the usage and quota evidence is stable; configured priority remains the
  current policy.
- Acceptance measures: fixed prompts report stable prefix and cache-hit fields;
  rate-limit and accounting failures are visible without exposing credentials.

## 4. External-provider smoke

- After step 3 is verified and credentials exist, run one disposable provider
  configuration through the custom worker loop, tool/checkpoint events, and
  ref-only merge.
- Keep the run opt-in and networked only by explicit command. Record request
  count, usage events, commit, merge ref, and the failure case that leaves
  `main` unchanged without recording secrets.
- Acceptance measures: the credentialed run has a recorded provider response,
  usage record, one expected ref update, and an unchanged `main` on the
  failure fixture.
- Local fake-provider fixtures can support regression tests, but they do not
  substitute for an external-provider run.

## 5. Follow-on evaluation

After steps 1–4 are reproducible, measure worker reuse, provider routing,
context compression, and the example module's DSPy seam with fixed datasets,
baselines, and failure criteria. Adopt, defer, or reject each experiment from
its evidence; do not change the runtime contract silently.

### Worker reuse (ADOPT, implemented)

`docs/research/worker-coldstart.md` measured spawn-to-ready at ~130–158 ms
for the marker worker and ~2.2 s with dspy imports; `scripts/measure_worker_coldstart.py`
projects a 96–99.8 % per-task saving from a persistent-worker pool. ADOPT is
implemented as a bounded session-scoped warm pool:

- worker.py: an init may opt in with `worker_reuse: true`; after the task the
  worker emits `reuse_ready` and waits for a full rebind init on stdin
  instead of exiting (clears all per-task state, chdir to the new worktree,
  rebuilds clients from the new init's config; exits cleanly on EOF).
  Without the flag the single-init exit behavior is unchanged.
- supervisor.py: `_Runtime` holds the pool (`CAMBIUM_WARM_POOL_SIZE`, default
  1, 0 disables). The first generation of a task pops a matching idle worker
  (same command, env modulo per-task overrides) and emits `worker_reused`;
  a clean reuse-ready generation returns its live process to the pool, all
  other exits kill as before. Restart generations always spawn fresh;
  pooled workers are killed at session end (run_plan finally / shutdown).

### Provider auth/protocol tagging (implemented, W1 config slice)

`ProviderConfig` carries tagged `auth` (`api_key` | `codex_chatgpt`) and
`protocol` (`chat_completions` | `codex_responses`) modes; legacy files without
the tags load unchanged. `codex_chatgpt` entries are pinned to the
`CODEX_CHATGPT_PROFILE` constants in `provider_config.py` — they require
protocol `codex_responses`, reject `base_url`/`api_key_env` in the file, and
the transport later derives the endpoint from the profile, not from config.
The codex OAuth transport/entitlement flow is the follow-on (plan v2 W1).

### Codex responses transport adapter (implemented, W1 adapter slice)

`diffundo.py` speaks `codex_responses` when `ProviderConfig.protocol` says so:
chat prompts convert to the Responses-API body (system->developer,
`input_text` parts, flattened function tools, `store:false`/`stream:true`, no
`max_output_tokens`), SSE output is assembled from `output_text.delta`, and
errors classify as retryable outage / CONFIG-quarantine (`model_not_found`,
machine-readable model/parameter 400) / refusal. Bearer credentials are
injected via `CredentialSource` (absent -> AUTH_ERROR) and `reasoning_effort`
is a normal provider-config field.
