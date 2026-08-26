# Cambium architecture

**Status:** current-versus-target contract. Source and tests establish current
behavior. This document names targets but does not turn them into features.
See [`agents.md`](../../agents.md) for the operating contract and the focused
architecture documents beside this file for subsystem contracts.

## 1. Current runtime

Cambium runs directly from source; no wheel is built and no install is
required or supported. The CLI routes `auth`, `supervisor`, `doctor`, `bench`,
`module-test`, `version`, `run`, `repl`, `tui`, `monitor`, `optimize`,
`session`, and `architectus`.
The session surface includes `list`, `latest`, `show`, `status`, `resume`, and
`usage`. Task-tree inspection remains available as `python -m cambium.tasktree`.
`cambium.__init__` exports only `__version__`; there is no public session API.

### Plan and publication

`cambium.supervisor.run_plan` accepts a mapping with `tasks` or a task list. It
validates supplied task records, rejects duplicate IDs and unsafe worktree
paths, then supervises the supplied tasks concurrently in one
`asyncio.TaskGroup`. A task worker runs in a Git worktree and process group. A
clean worker whose envelope reports `succeeded` publishes; there is no
task-command pre-merge gate. A succeeded envelope proceeds to merge only after
the repository-integrity checks pass (worker success integrity, fencing,
expected-old ref publication, session admission, worktree confinement,
protocol/request correlation, and quarantine). Successful publication uses an
expected-old atomic update of `refs/heads/main`. It is ref-only and never
refreshes the caller's checkout or index. A child publication records its
accepted integration head and advances a clean suspended parent before the
join barrier; the invariant is
`post_join_parent_HEAD == accepted_integration_HEAD`. A failed join emits a
bounded `join_invariant_failed` record and cannot resume the parent.

A session-wide parallel-worker cap defaults to one worker per CPU:
`run_plan` rewrites `max_concurrent_tasks=None` to the CPU count before
building `_Runtime`, which creates an `asyncio.Semaphore` when the cap is
nonzero (`0` disables the cap, meaning unlimited). The semaphore is acquired
for the worker phase (spawn through worker exit) on both the flat and the
hierarchy paths, and is released before merge, prune, and observer
notification. A flat plan (no `depends_on`) fans out under one
`asyncio.TaskGroup`; the flat canary
(`test_flat_plan_ignores_max_width_and_preserves_canary`, four tasks) shows
`max_width` is ignored on that path while the default CPU-count cap still
bounds concurrent workers. A plan that supplies `depends_on` is built into
one validated rooted `TaskTree` and dispatched in static ready-node waves:
only nodes whose dependencies finished are admitted per wave, and each
wave's concurrency is bounded by `max_width` (parameter, then the plan
field, then `tasktree.MAX_WIDTH`). A failed node cascades so its descendants
are never spawned. `resource_thresholds` remains the only host-health
pre-flight.

The plan runtime creates `store.EventStore` at `.cambium/events.db`, emits
records through it, and writes `.cambium/result.json` after shutdown. The
one-task `run_session` adapter remains for compatibility. `EventStore` is
the current event boundary; there is no current `events.py` or dead-letter
queue module.

Interactive TUI turns keep those stores isolated under
`turn-NNNN/.cambium/events.db`. `supervisor.read_events` aggregates the turn
stores into one session-level timeline with stable sequence numbers for the
TUI and monitor; it does not rewrite archived turn history. The canonical root
result keeps its `event_log_ref` as the owning root's
`sqlite:<root>/.cambium/events.db` URI, with aggregation preserved as a
read-time supervisor behavior.

The canonical root result written to `.cambium/result.json` is
`results.Result`. Its JSON record includes the serving `provider` and the
optional `fell_back_from` origin, so a terminal-death provider substitution is
visible in the durable result rather than only in the event stream.

The supervisor gives each worker a bounded decoded-stdout `asyncio.Queue` and
routes worker and runtime records through `EventStore`'s bounded writer queue.
Worker stdout remains protocol-only NDJSON; diagnostics use stderr/logging.
When a worker generation fails, the failure envelope/result reason includes
the latest non-empty worker stderr tail after session redaction, bounded to
512 characters. Non-critical store records can be dropped under the store
overflow policy.

### Worker and providers

`provider_config.load_providers` keeps the provider document boundary strict but
quarantines an invalid entry instead of discarding the whole file. Each dropped
entry is recorded as `{"entry", "reason", "quarantined_at"}` in the
`<config>.quarantine` JSON sidecar; writes merge-deduplicate by canonical entry
and reason, and the loader emits a warning (the one-shot path prints it to
stderr). Before an entry is persisted, secret-key or secret-shaped string
values in its `entry` copy become `<redacted:N>` markers. The serialized entry
copy is capped at 64 KiB (`MAX_QUARANTINE_RECORD_BYTES`), with an oversized
copy replaced by an `<oversized: ... bytes>` marker, and new sidecar payloads
are capped at 1 MiB (`MAX_QUARANTINE_SIDECAR_BYTES`); records that do not fit
are not appended. An existing sidecar over the limit is left unchanged. The
loader refuses a symlink at the final sidecar path rather than following it.
Accepted appends are written to a same-directory temporary file, flushed and
`fsync`ed, then published with `os.replace`, so the sidecar replacement is
atomic. Valid entries continue to load.
Malformed JSON, invalid root structure, unknown root fields, duplicate provider
names, and provider-environment collisions remain fatal structural errors. If
every entry is quarantined, the loader returns a list-compatible empty provider
set; `select_provider` and the one-shot resolver fail with the sidecar path so
the operator gets a repairable error rather than an unexplained empty cascade.

`worker.do_work` selects one of two explicit modes:

1. Without `fanout_config`, the deterministic marker worker edits and fences
   one commit.
2. With `fanout_config`, the worker loads the configured providers and runs a
   custom bounded loop. Each turn calls `Diffundo`, requires exactly one strict
   JSON action (`plan`, `tool_call`, or `finish`), validates permissions and
   tool arguments, dispatches the tool, emits a `tool_event` and checkpoint,
   and then, when the agent changed files, creates one fenced result commit.
   A provider loop that finished cleanly with no non-`.cambium` changes is a
   successful no-op: it owns zero commits and no empty commit is made; the
    summary is carried in the result and rendered output. The agent is instructed to emit a
    short `plan` action before any `tool_call`, and the plan is retained in the
    transcript. Context reuse and rolling deterministic compaction are default-on.
    A fold rewrites only the active continuation projection, persists it as the
    next immutable content-addressed epoch, and updates supervisor fork metadata;
    the stable head and older epoch files are not mutated. The transcript remains
    bounded within `max_turns`. A
   `read_batch` tool reads related files in one bounded call, and lint feedback
   from `write_file` reaches the agent as a tool observation. Summary-only
   checkpoints can be rolled into one CAST K0 semantic projection under the
   configured cache/CAST policy, producing a new immutable epoch.

The loop bounds turns, tokens, wall time, transcript size, and summaries. It
returns cumulative provider usage and latency as redacted metadata. `lm.py`
contains optional DSPy-compatible `CambiumLM` and `ArchitectusLM` adapters;
they are not a supervisor planner.

`Diffundo` is a tiered provider router with health, independent request-rate
and in-flight capacity buckets, cooldown, circuit-breaker, and evidence-based
candidate ordering. The ordering key uses success confidence, latency-SLO
compliance, expected cost per successful turn, measured output throughput,
normalized latency/cache evidence, incumbent stickiness, rotation, debt, and
configured order when evidence is absent. A depleted bucket reports
`RATE_LIMITED`. It has no local response cache. HTTP 429 responses carry a
parsed `Retry-After` delay into the same-provider retry path. One-shot runs
store routing debt in `<repo>/.cambium/routing-state.json`; `DebtStore` itself
defaults to `~/.config/cambium/routing-state.json` when no path is supplied.
Interactive wall budgets use explicit configuration when supplied and can
otherwise scale from provider throughput hints and measured branch usage with a
safety factor.

A pinned one-shot provider remains the task's primary association until
`Diffundo.ProviderError.is_real_death` reports terminal evidence: key-level
HTTP 401/403 authentication failure, HTTP 5xx endpoint failure, or a terminal
connection, DNS, or TLS/SSL transport failure. 429/quota pressure and other
transient outcomes retain their retry, cooldown, disable, or refusal semantics
and do not trigger this pinned-provider fallback. When terminal death is
established, authorized fallback candidates are tried in the same tier before
other tiers; once one serves, the router records the original provider and
stays with the serving association rather than bouncing back.

`provider_scheduler.py` owns the provider-neutral `CacheHorizonConfig` and
`CastConfig` values. `summary_trunk.py` compiles immutable semantic history into
CAST K0; interactive `/compact` and configured rollover paths write a successor
content-addressed epoch and preserve the source segments.

The escaped-secret bench canary was deleted by product decision; it is no
longer a live blocker.

### Trees, diagnostics, and modules

`tasktree.build_tree` validates one rooted dependency tree, cycle and
depth/width bounds, and deep-copies each input `spec` into frozen node records.
`topological_order` and `ready_tasks` are pure inspection/scheduling inputs.
`run_plan` integrates them on the hierarchy path: a plan whose tasks carry
`depends_on` is built into a `TaskTree` and dispatched in static ready-node
waves; a flat plan keeps the one-`TaskGroup` fan-out under the default
CPU-count cap.

`architectus.ArchitectusCore` is the injected decision port: `run_plan`
accepts an optional `architectus` argument (an `ArchitectusCore` or an
`aggregate`/`step` adapter) and an optional `conversations` flag that opens
`ConversationStore` at `<session_dir>/.cambium/conversations.db`. When the
port is configured, each admitted parent's terminal envelope feeds
`aggregate`/`step` and the resulting typed proposals are routed through the
existing `_admit_child` revision validation (never the live tree directly);
every admitted/rejected revision is appended to the conversation store.
`orchestrator.py` forwards both options from its stabilized public `run`
surface, and `cambium supervisor --conversations` exposes the flag on the
CLI. The unified supervisor accepts the same `--plan`, `--task-spec`, and
`--demo` inputs as the module entry point. With neither backend configured,
`run_plan` keeps the normal execution path. The session-scoped warm pool is
opt-in via `--warm-pool-size` (default 0, disabled); the
`CAMBIUM_WARM_POOL_SIZE` environment variable is not read.

`doctor` checks Python/Git and `uv`, worktree hygiene, provider environment and
auth coverage, optional event and conversation databases, module datasets, and
advisory host health. Its provider-environment check reads the file named by
`CAMBIUM_PROVIDERS` or `<cwd>/.cambium/providers.json` (falling back to the
shipped default sample), not the trusted user config
`~/.config/cambium/providers.json` that `run` selects providers from.
`resources.py` is deleted; there is no `CompileGate` and
no `ResourceBudget` class. `module_conformance` provides an
isolated module-test gate. `modules/example` and `modules/should_review` have
deterministic decision logic,
train/eval/canary data, split metrics, and a JSON CLI with `decide` and
`evaluate` operations. There is no `eval_cache.py`.

The tracked source does not contain `worker_pool.py`, `events.py`, or `dlq.py`.
Do not use those names as current architecture components.

### Optimization

The direct `python -m cambium.optimize` driver offers the three optimizer
choices `zero`, `bootstrap`, and `gepa`; the top-level `cambium optimize`
wrapper exposes the same choices. GEPA calls `build_trainsets` with a
seeded shuffle and `_GEPA_VAL_FRACTION = 0.3`, producing a deterministic
approximately 70/30 train/held-out validation split while leaving at least one
training record. It requires at least four reviewed, non-canary records and
both resulting splits; otherwise `run_stage_gepa` raises the clear
`OptimizeError` data error before compilation.

`run_stage_gepa` passes the same Diffundo-backed reflection LM used by the
program to `dspy.GEPA`, and `_CostLedger` bounds provider spending. The
remaining ledger dollars become GEPA's `max_metric_calls`; every LM call is
recorded by `_TrackingDiffundo`, and budget exhaustion writes the partial
report with `budget_exhausted`. Completed reports include a `stage_gepa`
section with train/evaluation means and any GEPA call, iteration, and full
validation-evaluation counters exposed by the installed DSPy version. The
The direct and wrapper commands share the module manifest and optimizer
label-resolution logic.

The `eval` subcommand is `cambium optimize eval MODULE --dataset PATH`, with
optional `--program-dir PATH`, `--budget-usd N`, `--tier TIER`, and `--json`.
Without `--program-dir`, it loads `optimized/<MODULE>/program.json` when that
state is present and otherwise evaluates a fresh program. Its JSON report has
`module`, `program` (`fresh` or `optimized`), `dataset`, and `splits`; each
split has `mean`, `std`, `count`, and per-record `records` with `index` and
`score`.

An optimizable module declares its package-local DSPy module in the manifest's
`dspy_program` field. `should_review` sets that field to
`cambium.modules.should_review.dspy_program` and its `label_field` to
`review`; `_label_field` carries that name through optimizer metric lookup,
prediction parsing, fallback decisions, gold labels, and DSPy training-example
conversion. This keeps the second module's `review` labels distinct from the
default `decompose` label.

Training data has two read-only extraction paths. `cambium.opencode` accepts
explicit OpenCode SQLite databases or storage directories, discovers only
databases with the `session`/`project`/`message`/`part` schema, and extracts
explicit visible decision/rationale pairs after redaction and canonical-pair
deduplication. `scripts/extract_pi.py` recursively scans pi session `*.jsonl`
files and emits redacted, inferred candidates with count-only outcome evidence;
the inferred label is always review-required and the script does not copy
assistant or tool output into the candidate record.

The OpenCode `--review-gate` output and every pi candidate file are queues, not
training data: their records carry `candidate: true`, `redacted: true`, and
`review_status: "needs_review"`. `_reviewed_transcript_records` admits only
explicitly approved records, ignores `rejected`/`excluded` records, and fails
closed on pending or unknown statuses; transcript candidates are opt-in via
`--include-transcript-candidates` or `--transcript-candidates PATH`, and a
candidate file may not contain a canary. The committed reviewed snapshot
`artifacts/optimization/first-real-extraction/train_queue_v2.jsonl` contains
34 approved records: 24 marked `train` and 10 marked `val`. It is pipeline
state, not an implicit module dataset; the optimizer consumes a candidate file
only when one of those opt-in flags is supplied.

### 1.1 Bounded CAST context policy and transactional fork joins

`src/cambium/context_policy.py` defines `CastPolicy`, a bounded CAST/K0
context policy: token ceilings for prompt, transcript, and cache trunk plus
epoch rollover thresholds. Interactive sessions consult it when deciding
whether the next turn fits the bounded window or must roll over into a new
K0 epoch (see §CAST in `cast.md`).

Fork joins are transactional: a worker's integration is accepted only if
the parent session head still matches the head observed at fork time, and
`supervisor._assert_parent_join_invariant` enforces
`post_join_parent_HEAD == accepted_integration_HEAD` durably. A failed
invariant produces a structured conflict envelope instead of silent
overwrite; conflicting work lands on a side branch for manual reconciliation.
Context epochs are published per rollover so resumed sessions rebuild from
the correct epoch boundary (`tests/scenarios/test_context_epochs.py`,
`test_join_invariant.py`).

## 2. Ownership and invariants

1. The caller owns the session directory and supplies plan records.
2. The supervisor owns validation, worker handles, generations, event
   admission, restart decisions, and publication order.
3. A worker owns its worktree edits, provider calls, tool context, and at most
   one fenced commit (zero for a clean no-change finish); it cannot publish
   `main` directly.
4. The merge sequencer owns staging, expected-old checks, quarantine, and
   cleanup. A conflict, non-fast-forward, or cleanup violation does not
   advance `main`; conflicts surface as a structured `merge_failed` envelope
   with `status=merge_conflict`, conflicted files, bounded diff evidence, and
   the integration head.
5. The event store owns durable rows and its writer thread. Observer copies
   cannot mutate persisted records.

IPC is bounded and correlated by request ID (generation is not enforced for
   message correlation). Fatal framing, oversized lines, missing correlated
   results, non-zero exits, and deadline failures follow the boundary-specific
   supervisor policy; malformed lines that fail JSON parsing and valid JSON
   lines that are not objects are counted as parse errors and skipped up to the
   same bound, never failing supervision. Tool schemas reject malformed calls.

Provider credentials are allowlisted environment values. They must not enter
task specs, prompts persisted as events, logs, or result
artifacts. Worktree and process-group isolation is not an OS sandbox.
`approval.py` and `resources.py` are deleted; `tools.py` exposes the six
dispatch tools (`delegate`, `read_batch`, `write_file`, `edit_file`, `git_op`,
and `run_shell`) without `ApprovalGate` or `CompileGate`. The separately
permission-gated `run_python` compatibility path is not part of that dispatch.

Live-use blockers were removed by product decision; this is a local development
tool run directly from source.

## 3. Target contracts and delivery order

These are open contracts, not current interfaces. Production hierarchy and
dynamic admission are current behavior (see §1); the open contracts below are
the delivery order for what remains.

### Production hierarchy and admission

Landed (see §1 and [`implementation-plan.md`](../../implementation-plan.md)
steps 1–2): `run_plan` builds one validated `TaskTree` for plans with
`depends_on`, computes static ready-node waves bounded by `max_width`, admits
only nodes whose dependencies and width limits are satisfied, and gives each
child a fresh bounded context (own spec + strict parent envelope). A parent may
propose a typed tree revision (`propose_child`), but the supervisor validates
and durably admits it before dispatch; a provider response cannot mutate the
live tree in place. The Architectus decision port and conversation persistence
are wired at that boundary.

### Per-worker containment and approval

Per-worker OS containment and production approval were removed by product
decision; worktree/process-group isolation is the only worker boundary.

### Provider accounting before routing policy

Landed (see §1 and [`implementation-plan.md`](../../implementation-plan.md)
step 3): durable redacted usage events, provider and model identity, token/cost
fields, request-rate status, account-quota ownership, privacy/redaction rules,
Retry-After and `RATE_LIMITED` behavior, prompt-prefix/cache-hit metrics, and
measured output throughput are in place. Priority remains the first policy
class; measured quality and throughput refine ordering only within an
equal-priority run.

### External-provider acceptance

Run a disposable credentialed smoke through the worker loop, tool event,
checkpoint, and ref-only merge. Keep credentials in the environment and
network opt-in. The driver is committed (`scripts/external-provider-smoke.sh`)
and passed against a live codex OAuth session (ChatGPT `pro`): a real
`codex_responses` run produced usage events, exactly one ref-only commit
touching only the fixture, and an unchanged main on the failure fixture. The
smoke's fanout plan derives tier/model from the supplied provider config, and
the supervisor injects the codex OAuth token for tasks whose
`authorized_providers` set is empty (unrestricted). Deployment
credentials/configuration are external and ephemeral.

## 4. Failure policy by boundary

| Boundary | Current check | Required outcome |
| --- | --- | --- |
| Plan | `run_plan` rejects malformed tasks, duplicate IDs, and unsafe paths before worker setup. | No worker side effect before structural validation. |
| Task tree | `build_tree` rejects missing dependencies, multiple roots/parents, cycles, and bounds. | A future scheduler dispatches only a validated graph with snapshotted specs. |
| IPC | Framing limits, request IDs, heartbeat deadlines, and request-correlated result checks are enforced in `_Runtime._drive_generation`. | Stale or missing worker messages cannot complete a task. |
| Worker | Provider/tool failures, missing results, non-zero exits, and wall/token limits fail the generation; recoverable failures may restart it. | A worker verdict is accepted only for its active generation. |
| Merge | Conflict, non-fast-forward, unsafe quarantine, cleanup failure, or a failed parent join stops publication/resume. | `main` advances only through the expected-old ref contract; accepted child publication must satisfy `post_join_parent_HEAD == accepted_integration_HEAD`. Conflicts use a structured `merge_conflict` envelope. |
| Store | Critical event admission waits for the writer; writer death raises; non-critical overflow follows the bounded queue policy. | Durable failure is visible; no silent success after store failure. |

The table describes checks on paths that call these modules. A helper's
existence is not proof of integration: Redaction, resource admission, and
hierarchy remain targets; approval and containment were removed by decision.

## 5. Source map

| Concern | Current source | State |
| --- | --- | --- |
| CLI/version | `pyproject.toml`, `src/cambium/cli.py`, `__init__.py` | Direct-source CLI; version-only package export |
| Plan runtime | `src/cambium/supervisor.py` | Flat concurrent `run_plan` for plans without `depends_on`; static ready-node waves with width-bounded admission for plans with `depends_on`; one-task adapter retained |
| Worker/IPC | `src/cambium/worker.py`, `src/cambium/ipc.py`, `src/cambium/prompts.py` | Marker mode, custom provider loop, bounded NDJSON; `CODING_AGENT` and `SEMANTIC_SUMMARIZER` prompt text is centralized in versioned constants (`PROMPTS_VERSION`) with a drift test against worker embeds |
| Provider/LM | `src/cambium/diffundo.py`, `src/cambium/routing.py`, `src/cambium/provider_config.py`, `src/cambium/lm.py` | Independent request-rate/in-flight lanes, measured `tokens_per_s` plus configured throughput hints, priority router, and optional adapters |
| Context/CAST | `src/cambium/summary_trunk.py`, `src/cambium/provider_scheduler.py`, `src/cambium/interactive.py` | Immutable summary entries, K0 projection/rollover, cache-horizon and CAST thresholds, and interactive epoch publication |
| Tree/planner | `tasktree.py`, `architectus.py`, `orchestrator.py` | Pure tree/core; `build_tree`/`ready_tasks`/`topological_order` wired into `run_plan` for static waves; dynamic child admission wired through the injected decision port (`ArchitectusCore` or `aggregate`/`step` adapter) with conversation persistence, exposed by `Orchestrator.run` and `cambium supervisor --conversations` |
| Store/merge | `store.py`, `merge.py`, `results.py`, `fencing.py` | Current event, result, and ref-publication boundaries |
| Controls | `src/cambium/tools.py`, `src/cambium/schemas.py`, `redact.py` | Six tools are wired through `TOOL_SCHEMAS` and `TOOL_DISPATCH`; `run_shell`/`git_op` run without `ApprovalGate`/`CompileGate`; `run_python` remains separately permission-gated and outside `TOOL_DISPATCH`; `approval.py` and `resources.py` are deleted |
| Diagnostics/evaluation | `doctor.py`, `module_conformance.py`, `bench.py`, `modules/example/`, `modules/should_review/` | CLI diagnostics and module evaluation exist |
| Optimization/training data | `src/cambium/optimize.py`, `src/cambium/cli.py`, `src/cambium/modules/base.py`, `src/cambium/modules/example/`, `src/cambium/modules/should_review/`, `src/cambium/opencode.py`, `scripts/extract_pi.py`, `artifacts/optimization/first-real-extraction/train_queue_v2.jsonl` | Zero-shot/bootstrap/GEPA and `eval` driver, manifest-selected DSPy programs with label-aware evaluation, read-only OpenCode/pi extraction, review-gated candidate admission, and the 34-record reviewed snapshot |

Any target moves to current only after a caller and focused failure test
demonstrate it. Keep public names and status mappings stable once a host API is
introduced; a worker envelope is not a substitute for a typed root result.
