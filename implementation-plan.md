# Implementation plan

Ordered work only. Source and tests decide when a step is complete; this file
is not a branch ledger or merge log.

## 1. Production hierarchy and dynamic admission

- Integrate `tasktree.build_tree`, `ready_tasks`, and `topological_order` with
  `supervisor.run_plan` so only validated, dependency-ready nodes are admitted.
- Define the production hierarchy boundary: root ownership, depth/width and
  session admission, envelope-only child results, durable revision records, and
  failure propagation.
- Connect the injected Architectus decision port and conversation persistence
  only after their callers, schemas, and failure paths are explicit. A provider
  response may propose a revision, but cannot mutate the live tree directly.
- Add deterministic checks for unready dispatch, duplicate/cyclic revisions,
  width limits, parent isolation, and cancellation.

## 2. Per-worker OS containment and approval

- Select and implement the host boundary for each worker's process, filesystem,
  CPU/memory/task limits, network policy, and teardown. Worktree/process-group
  isolation alone is not sufficient.
- Pass a production `ApprovalGate` policy and callback into the worker tool
  context. Keep denied commands fail-closed; treat `fail_open` as development
  configuration only.
- Add focused checks for containment setup/teardown, resource exhaustion,
  denied and unavailable approval, and no publication after control failure.

## 3. Provider usage and quota contract

- Specify redacted durable usage events: provider, model, request/turn,
  token fields, cost, latency, Retry-After, quota owner, and failure reason.
- Connect accounting at the supervisor/event boundary and define behavior when
  accounting or quota state is unavailable. Preserve environment-only secrets.
- Test 429 `Retry-After`, same-provider retry, quota exhaustion, and provider
  fallback against the contract. Do not introduce weighted routing until the
  usage and quota evidence is stable; configured priority remains the current
  policy.

## 4. External-provider smoke

- When credentials exist, run one disposable provider configuration through the
  custom worker loop, tool/checkpoint events, deterministic gate, and ref-only
  merge under the selected containment boundary.
- Keep the run opt-in and networked only by explicit command. Record request
  count, usage events, commit, gate result, merge ref, and the failure case that
  leaves `main` unchanged without recording secrets.
- A loopback smoke is useful regression evidence, but it does not substitute
  for an external-provider run or prove per-worker OS isolation.

## 5. Follow-on evaluation

After steps 1–4 are reproducible, measure worker reuse, provider routing,
context compression, and the example module's DSPy seam with fixed datasets,
baselines, and failure criteria. Adopt, defer, or reject each experiment from
its evidence; do not change the runtime contract silently.
