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

## 3. Provider usage, prompt stability, and quota contract (LANDED)

- Specify redacted durable usage events: provider, model, request/turn,
  token fields, cost, latency, Retry-After, request-rate status, account-quota
  owner, and failure reason. [Landed: one redacted `usage_event` per router
  call through the EventStore; `tests/scenarios/test_usage_events.py` proves
  the contract including the 429/`Retry-After`/quota-owner retry case and the
  un-reported-field omission rule.]
- Measure prompt-prefix stability and provider-reported cache-hit metrics for
  the same fixed prompt fixtures. These metrics are requirements for routing
  decisions, not evidence that a local response cache exists. [Landed:
  `prompt_prefix_bytes` and `provider_cache_hit` fields;
  `scripts/usage_evidence.py` aggregates both across sessions.]
- Connect accounting at the supervisor/event boundary and define behavior when
  rate-limit, token, cost, or account-quota state is unavailable. Preserve
  environment-only secrets. [Landed: `DebtStore` usage-debt ledger at
  `routing-state.json`, fed live from `usage_event` rows and persisted at
  session end; un-reported fields are omitted, never an error.]
- Admission-time model selection (solution C): tasks may declare
  `model_candidates`; the supervisor balances (model, provider) from a durable
  usage-debt ledger (`~/.config/cambium/routing-state.json`) before the model
  filter partitions the pool, and binds each task to the assigned provider.
  [Landed: `routing.select_primary` / `select_lane`; `cambium run --auto`
  routes through the ledger.]
- Provider lanes (H1): one concurrency lane per provider; `run_plan`
  pre-assigns each wave's un-pinned tasks in one pass, and 429 pressure
  decays a lane's in-flight cap (`routing.select_lane`). The existing `rpm`
  provider field is the lane allowance. [Landed.]
- Capability/quality-constrained selection (H2): tasks may declare
  `requirements` (quality high/normal, optional `min_context_window`); the
  supervisor filters providers strictly by capability (fail-closed on unknown
  keys) and picks the lowest `routing.score_providers` score — utilization,
  cache-hit rate, latency, and a shadow price — instead of `select_lane`, with
  placeholder module-constant weights until measured quality/latency data
  exists (step 5). [Landed: `routing.score_providers` with placeholder
  weights; fail-closed on unknown requirement keys.]
- Test 429 `Retry-After`, same-provider retry, `RATE_LIMITED` buckets, and
  provider fallback against the contract. Do not introduce weighted routing
  until the usage and quota evidence is stable; configured priority remains the
  current policy. [Landed: `test_usage_events.py` + `test_worker_provider.py`
  cover the contract; weighted routing stays off pending stable evidence.]
- Acceptance measures: fixed prompts report stable prefix and cache-hit fields;
  rate-limit and accounting failures are visible without exposing credentials.
  [Met: durable redacted events + `usage_evidence.py` aggregate report.]

## 4. External-provider smoke (VERIFIED; ran against live codex OAuth, 2026-08-16)

- After step 3 is verified and credentials exist, run one disposable provider
  configuration through the custom worker loop, tool/checkpoint events, and
  ref-only merge. [Committed as `scripts/external-provider-smoke.sh`; it runs
  only when `CAMBIUM_SMOKE_PROVIDER_CONFIG` names a real, non-loopback
  provider config whose credential env keys are set.]
- Keep the run opt-in and networked only by explicit command. Record request
  count, usage events, commit, merge ref, and the failure case that leaves
  `main` unchanged without recording secrets. [The script verifies the durable
  `usage_event` rows, exactly one expected ref update touching only the smoke
  fixture, and an unchanged `main` on the failure fixture.]
- Acceptance measures: the credentialed run has a recorded provider response,
  usage record, one expected ref update, and an unchanged `main` on the
  failure fixture.
- Local fake-provider fixtures can support regression tests, but they do not
  substitute for an external-provider run. [The script refuses loopback
  configs for exactly this reason.]
- PASSED end-to-end: a `codex_responses` run against a live ChatGPT `pro`
  OAuth session drove the provider-backed worker loop with real `usage_event`
  rows, produced exactly one ref-only commit touching only the fixture, and
  left `main` unchanged on the failure fixture. En route, the driver's plan
  was fixed to derive tier/model from the supplied provider config (a direct
  supervisor plan must declare both; `oneshot.py` resolves them before
  dispatch), the success fixture was reworded so the model leaves the change
  uncommitted (the worker owns the fenced commit), and
  `_codex_oauth_provider_names` now treats an empty `authorized_providers`
  set as unrestricted so the codex OAuth token is injected for such tasks.
  The failure fixture is deterministic and model-independent: its
  fanout_config references a non-codex provider, so the supervisor injects no
  codex OAuth token and the worker's provider routing fails closed with a
  failed verdict before any model call.

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

### OAuth dynamic secret redaction (implemented, W1 redact slice)

`Redactor.register_secret(value)` registers an exact value at any time
(thread-safe, idempotent), so a session redactor that snapshots provider
values at session start can still redact OAuth tokens rotated mid-session.
`sanitize_oauth_document(doc)` returns a copy with token fields replaced by
`<redacted>` and `account_id` reduced to the first 8 hex of SHA-256; the OAuth
field names (`access_token`, `refresh_token`, `id_token`,
`authorization_code`, `code_verifier`, `device_auth_id`, `user_code`) are
structured secret names in JSON-looking text. Covered by fast scenario tests.

### Codex-subscription OAuth module (W2, implemented)

`src/cambium/oauth.py` lands the wave-2 credential layer ahead of the
provider_config/diffundo/CLI wiring: a hardened per-provider `OAuthStore`
(fail-closed corruption, explicit `repair()`), a flock'd `TokenManager`
refresh transaction with a persistent per-provider lock file and
last-good-on-429/5xx/timeout policy, the `DeviceFlow` against the pinned
issuer contract, and `import_codex_cli_session` for the existing
`~/.codex/auth.json` session. The codex CLI's own client id is passed in
(`--client-id`), never hardcoded; `cambium auth oauth` (W4) consumes it.

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
### Supervisor/CLI OAuth wiring (implemented, W3/W4)

`run_plan` preflights every task that references a `codex_chatgpt` provider:
the local `OAuthStore` document must be present and unexpired-or-refreshable
(fail-closed, no network probe; the transport stays authoritative). At spawn
`_worker_environment` ensures a fresh access token once and injects
`CAMBIUM_OAUTH_ACCESS_<PROVIDER>` + `CAMBIUM_OAUTH_ACCOUNT_<PROVIDER>` — never
the refresh token — registering the token with the session `Redactor` via
`register_secret`. `cambium auth oauth <provider> [--client-id ID]` runs the
device flow (verification URL + user code printed only to the controlling
TTY), plus `--status` (local expiry + account fingerprint, no refresh, no
secrets), `--logout` (locked local removal, no remote revoke claim), and
`--import-codex-cli` (imports `~/.codex/auth.json` as provider `codex`).
`cambium doctor --oauth-live` is an opt-in live probe of issuer reachability
and refreshability that consumes quota and never makes a model call.

### Codex OAuth transport/entitlement flow (plan v2 W1, partially landed)

The codex transport now matches the CLI's wire identity: `_codex_post_sync`
sends `originator: codex_cli_rs`, a codex-shaped `User-Agent`
(`codex_cli_rs/<version> (cambium; cambium)`), and a stable per-instance
`session-id` (one worker process runs one task, so per-instance is
per-session and never rotates per request). `_codex_request_body` is
unchanged: `prompt_cache_key` is live-probed useless on this endpoint and
`include`/`instructions` add nothing.

Entitlement evidence (openai/codex sources + live probe): there is no
client-side model-name translation — the configured slug goes on the wire
verbatim — and no client header unlocks a model; the gate is server-side
via the authenticated `/models` catalog filtered by account and
`client_version`. A live `GET
https://chatgpt.com/backend-api/codex/models?client_version=<codex-cli-ver>`
with the pro-session token returns 200 and lists `gpt-5.6-luna`, confirming
server-side entitlement to the pinned slug. `doctor`'s provider row now
shows the configured model per provider. Surfacing the CONFIG_ERROR
quarantine reason verbatim is deferred: the disable state is in-memory only
and nothing persists it; that needs a persisted provider-disable record
first.
