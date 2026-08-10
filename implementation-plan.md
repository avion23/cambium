# Implementation plan

Ordered work only. Source and tests decide when a step is complete; this file
is not a branch ledger or merge log.

## 1. Live-run prerequisites

- Bound worker admission in `run_plan`. Its flat `TaskGroup` starts one
  lifecycle per task; an 11-task canary observed 11 concurrent supervisions.
  Keep host-health `resource_thresholds` separate from heavy-command
  `CompileGate` limits.
- Fix free-form redaction so mixed raw/Unicode-escaped stderr cannot retain a
  credential escape; the bench canary must pass. Keep live use blocked until
  admission, redaction, credentials/configuration, and OS containment are
  verified.

## 2. Smallest production hierarchy slice: static waves

- Make the harness own one explicit validated `TaskTree`. Integrate
  `build_tree`, `ready_tasks`, and `topological_order` with `run_plan` so static
  ready-node waves, dependency order, and width limits control admission.
- Give every admitted child a fresh bounded context derived from its own task
  and allowed parent envelope. Permit upward data only through the strict
  envelope key set; do not expose sibling context or an unbounded transcript.
- Acceptance measures: a fixed fixture proves exact ready waves, no unready
  dispatch, width enforcement, bounded child context, and exact envelope keys;
  failed children stop dependent admission.

## 3. Validated dynamic child admission

- After the static slice is reproducible, let a parent propose a child only as a
  typed tree revision. The harness validates and durably records each revision
  before admission; a provider response cannot mutate the live tree directly.
- Connect the injected Architectus decision port and conversation persistence
  only at this boundary, with explicit schemas and failure paths.
- Acceptance measures: duplicate, cyclic, multi-parent, over-depth, and
  over-width revisions spawn nothing; a valid child is admitted only at a ready
  wave and its envelope is visible only to its parent.

## 4. Per-worker OS containment and approval

- Select and implement the host boundary for each worker's process, filesystem,
  CPU/memory/task limits, network policy, and teardown. Worktree/process-group
  isolation alone is not sufficient.
- Pass an `approval.py:ApprovalGate` policy and callback into the worker tool
  context (consumed by `tools.py`). Keep denied commands fail-closed; treat
  `fail_open` as development configuration only.
- Add focused checks for containment setup/teardown, resource exhaustion,
  denied and unavailable approval, and no publication after control failure.
- Acceptance measures: a denied or unavailable approval cannot run the command;
  a containment setup or teardown failure cannot publish a worker result.

## 5. Provider usage, prompt stability, and quota contract

- Specify redacted durable usage events: provider, model, request/turn,
  token fields, cost, latency, Retry-After, request-rate status, account-quota
  owner, and failure reason.
- Measure prompt-prefix stability and provider-reported cache-hit metrics for
  the same fixed prompt fixtures. These metrics are requirements for routing
  decisions, not evidence that a local response cache exists.
- Connect accounting at the supervisor/event boundary and define behavior when
  rate-limit, token, cost, or account-quota state is unavailable. Preserve
  environment-only secrets.
- Test 429 `Retry-After`, same-provider retry, `RATE_LIMITED` buckets, and
  provider fallback against the contract. Do not introduce weighted routing
  until the usage and quota evidence is stable; configured priority remains the
  current policy.
- Acceptance measures: fixed prompts report stable prefix and cache-hit fields;
  rate-limit and accounting failures are visible without exposing credentials.

## 6. External-provider smoke

- After steps 1, 4, and 5 are verified and credentials exist, run one disposable
  provider configuration through the custom worker loop, tool/checkpoint
  events, deterministic gate, and ref-only merge under the selected
  containment boundary.
- Keep the run opt-in and networked only by explicit command. Record request
  count, usage events, commit, gate result, merge ref, and the failure case that
  leaves `main` unchanged without recording secrets.
- Acceptance measures: the credentialed run has a recorded provider response,
  usage record, passing gate, one expected ref update, and an unchanged `main`
  on the failure fixture.
- Local fake-provider fixtures can support regression tests, but they do not
  substitute for an external-provider run or prove per-worker OS isolation.

## 7. Follow-on evaluation

After steps 1–6 are reproducible, measure worker reuse, provider routing,
context compression, and the example module's DSPy seam with fixed datasets,
baselines, and failure criteria. Adopt, defer, or reject each experiment from
its evidence; do not change the runtime contract silently.
