# M1 Canonicalization Plan (corrected) — one runtime, one store, one sequencer

**Historical snapshot — 2026-08-10.** Design-only record from branch `wt-m1-plan`
off `main@b709375`; it supersedes the earlier M1 plan and is not a status report.
Current behavior belongs to [`docs/architecture/architecture.md`](../architecture/architecture.md),
the [`src/cambium`](../../src/cambium) implementation and tests, and
[`v2-1-status.md`](v2-1-status.md).

**Current note (not retroactive):** active `supervisor.run_plan` is flat;
`task_decomposed` remains unsupported; the provider cascade is source-defined and
honors `Retry-After`; worker stdout/event admission is bounded; there is no
per-worker OS sandbox or production approval service. `ToolContext` accepts an
optional `ApprovalGate`, but the run-plan worker path does not inject one. DLQ and
eval cache are absent.

**Scope:** M1 in `docs/research/v2-1-review.md` §4 (v2.1 roadmap and acceptance gates): integrate one Custos runtime,
remove slice/fallback paths, and rerun conformance, security, and constitution audits
against one frozen SHA. Phases (a), (b), and (d) all touch `supervisor.py` and are one
serialized effort. This plan is a proposal, not evidence that those changes landed.

## 1. Corrected sequence

### (a) Integrate redaction first

Credential isolation (declared provider keys only) must land before the redaction
registry. At the snapshot, `_worker_environment` (`supervisor.py:824-826`) admitted
canonical `CAMBIUM_PROVIDER_*` variables through `auth.py:191-193`; the canary is
`tests/scenarios/test_supervisor_fanout.py:739-764`. Build one session `Redactor`
from the F-02 default patterns plus every value that `_worker_environment` can pass.
Redact complete event records in `_Runtime.emit` (`supervisor.py:1291-1319`) before
critical append, non-critical queueing, and observers. Add defense-in-depth redaction
inside `EventStore.append` (`store.py:368-380`, `783-815`) before serialization.
`_redacted_provider_metadata` remains a field allowlist, not the general boundary.
Proof: inspect raw SQLite rows and observer records for absence of worker/gate secrets.

### (b) Canonicalize `supervisor.py`

1. Make `run_session` a one-task adapter over `run_plan` (map `scratch_repo→repo`,
   `spec→task`, `wall_budget_s→max_wall_s`, preserve worker/provider/gate/path/timeouts/
   fanout, and set `max_restarts=0`); map `TaskResult` to `SliceResult`.
2. Route `--task-spec` and the demo through `_amain_plan`; default worker is
   `python -m cambium.worker`, never `scripts/fake_worker.py`; fold repo init into
   `_ensure_repo_initialized`.
3. Import `CRITICAL_KINDS`/`EventStore` from `cambium.store` and merge types from
   `cambium.merge`; fail at import if these canonical boundaries are missing.
4. Rewrite the stale `supervisor.py:1-24` docstring. Delete the local `EventLog`,
   `_FallbackEventStore`, `_FallbackSequencer`, resolver/fallback branches, duplicate
   merge exceptions and `CRITICAL_KINDS`, slice path/message/gate/merge helpers,
   `_default_spec`, `_load_task_spec`, `_bootstrap_scratch`, and slice CLI body
   (inventory below). Keep `EventSink`, request IDs, result types, `_Runtime`,
   `run_plan`, merge/reconcile helpers, environment stripping, quarantine, and stdin
   deadlines, including all post-`0867572` hardening.

### (c) Remove the duplicate event/orchestrator scaffold

Delete `src/cambium/events.py` and `Orchestrator.submit`/`_queue`/`_next_task_id`
(`orchestrator.py:28-46`, no-work drain `:71-75`); retain the thin
`Orchestrator.run(session_dir, plan)` forwarder and canonical redacted `EventHandler`.

### (d) Wire the canonical result

The terminal worker envelope is transient (`_GenOutcome.envelope`,
`supervisor.py:1245-1255`, `2183-2188`); `_supervise` and the persisted event retain
less (`:1194-1204`, `:1731-1745`, `:2082-2089`). Add a redacted retention boundary
until after shutdown. Build `cambium.results.Result` from the **supervisor verdict
first**, then sanitized envelope commits/files/diff/summary; never invent a root for
flat multi-task plans. Gate/merge failures override a worker `succeeded` envelope;
cancelled writes use status `cancelled`, exit code `4`; a failing gate uses status
`failed`, exit code `1`. Set times around startup/shutdown, session id to
`str(session_dir.resolve())`, call `write_result` through `asyncio.to_thread` before
return, and propagate write failures. `CancelledError` is re-raised after writing.

Acceptance: gate passes; `refs/heads/main` advances through the canonical sequencer;
`events.db` contains fsynced `merge_committed`; `.cambium/result.json` has exactly
`ROOT_RESULT_KEYS`; success/failing-gate cases produce `done`/`failed`; envelope
commits/files match; no temp file remains; worker PID and task branch are gone; only
the primary worktree remains.

### (e) Freeze and audit

Freeze the post-commit SHA. Run the full scenario suite, `ruff`, structural greps for
one `EventStore`/`MergeSequencer`, no `EventLog`/`_Fallback*`/`events.py`, no
`os.environ` assignment in `src/`, and `doctor`. Run conformance, security, and
constitution audits against that SHA, adding the three M1 audit documents. Mark M1
done only in a final docs commit.

## 2. Deletion inventory (line numbers from `main@b709375`, advisory)

| File | Delete | Keep / outcome |
|---|---|---|
| `src/cambium/supervisor.py` | `EventLog :105-125`; `_validate_paths :135-150`; `_next_message :284-291`; module `_run_gate :294-324`; `_merge_branch :327-383`; local `CRITICAL_KINDS :770-775`; merge exception duplicates `:902-927`; `_FallbackEventStore :930-1013`; `_FallbackSequencer :1016-1158`; resolvers `:1161-1174`; `_open_store` fallback `:1177-1182`; `_make_sequencer` fallback `:2254-2256`; `_default_spec :2577-2590`; `_load_task_spec :2593-2599`; `_bootstrap_scratch :2614-2630`; slice CLI `:2661-2704` | Rewrite module docstring; retain `EventSink`, `make_request_id`, `SliceResult`, `_cfg_float`, `_write_json`, kill/communicate helpers, env/redaction helpers, `_ensure_repo_initialized`, `_amain_plan`, `read_events`, `_Runtime`, `run_plan`, `TaskResult`/`PlanResult`, merge/reconcile/flush helpers. |
| `orchestrator.py` / `events.py` | `submit`/queue/drain; whole `events.py` seed | Keep thin `run`; handlers receive canonical records. |

## 3. Test migration (same merge as phase b)

Migrate exactly `test_vertical_slice.py`, `test_supervisor_hardening.py`,
`test_worker_provider.py`, and `test_conformance.py`. Preserve the focused ranges:
oversized stdout (`818-833`), wrong-ready request id (`836-892`), missing proto
(`895-924`), provider bridge (`312-345`), `events.jsonl` absence (`348-360`), and
`supervisor.CRITICAL_KINDS` (`203-210`). Assert `events.db`, canonical exit codes,
cleanup, and the adapter path. Leave `test_store`, `test_supervisor_fanout`,
`test_merge`, `test_m6_staging`, `test_pipeline`, non-slice provider tests,
`test_ipc:790`, tooling, results, and bench unchanged.

## 4. Deferred work and provenance

| Item | Decision |
|---|---|
| `_Runtime._writer_loop` error-swallow (`supervisor.py:1337-1338`) | Defer to M4 bounded-backpressure/fatal-store work; flag in the security audit. |
| Multi-task root aggregation | Emit an aggregate status only; Architectus (M5) owns a full root policy. |

Normative order is redaction, supervisor canonicalization (including test migration),
event/orchestrator removal, result wiring, then audits. Line numbers may drift as
consolidation merges land. Phases (a), (b), and (d) remain serialized; phase (c) is a
separate change; phase (e) is three audits plus one docs commit on the frozen SHA.

## Appendix — retained acceptance checks

The redaction proof was intentionally stronger than checking a formatted log: a fake
worker and a failing gate each emitted a secret, then tests inspected raw SQLite
`payload` bytes and the observer callback record. The expected result was no cleartext
secret in either surface. The environment-isolation prerequisite mattered because a
redactor built only from declared keys cannot cover a value that `_worker_environment`
silently admits through `is_provider_env_name`.

The canonical-result proof used two worker outcomes: worker `succeeded` followed by a
nonzero gate (write `failed`, code 1), and worker `succeeded` plus a passing gate (write
`done`, code 0). A cancelled run wrote `cancelled`, code 4, before re-raising
`CancelledError`. Tests checked `ROOT_RESULT_KEYS`, no temporary result file, retained
sanitized commits/files, worker PID reaping, and only the primary worktree plus
`refs/heads/main` publication. These checks distinguish the supervisor's verdict from a
worker self-report.

Structural audit greps were fail-loud: one `EventStore` and `MergeSequencer`; no
`EventLog`, `_Fallback*`, `events.py`, or `os.environ` assignment in `src/`; and
`doctor` on the frozen SHA. Line numbers in the deletion table are advisory snapshots
from `main@b709375`, not stable source identifiers.

## Appendix B — phase ownership and no-coexistence rule

Phases (a), (b), and (d) were intentionally serialized because each changed
`supervisor.py` and the result of one phase changed the acceptance surface of the next.
The redactor had to be present before canonical `EventStore` writes; canonicalization
had to remove fallback stores before result retention could be trusted; result wiring had
to run after shutdown so gate/merge verdicts were authoritative. A parallel branch that
reintroduced a local `EventLog`, fake worker default, or stale `SliceResult` mapping was
not an acceptable merge conflict resolution. Phase (c) touched separate scaffold files;
phase (e) consumed only a frozen SHA.

The deletion inventory preserved source symbols because each had a specific failure
mode: fallback stores hid import errors; local merge exceptions split type identity;
slice CLI bootstrap wrote a second event format; `_default_spec` selected
`scripts/fake_worker.py`; and the old module docstring claimed `events.jsonl`/ff-only
merge after the runtime had moved to `events.db`/MergeSequencer. The plan did not authorize
deleting unrelated helpers or generated files.

## Appendix C — test and audit handoff

The four migrating test files landed with the supervisor rewrite so no coexistence period
could pass both event formats. Vertical-slice tests asserted adapter argument mapping;
hardening tests covered oversized lines, wrong ready IDs, and missing proto; provider
tests covered the bridge and `events.db`; conformance imported canonical
`cambium.store.CRITICAL_KINDS`. The unaffected test list was retained to ensure no
unrelated behavior was silently re-anchored.

Audit reports were required to carry the frozen SHA, command/cwd/status, and exact
structural grep results. A failed audit blocked the final docs commit; a skipped audit
was **UNVERIFIED**, not a pass. This record contains no post-hoc “M1 complete” claim.

The plan also required preserving dates/branch refs and the `main@b709375` line anchor
while labeling all line numbers advisory. No result was accepted from a moving baseline:
an audit rerun after a source merge needed a new SHA and command record.

## Appendix D — canonical boundary checklist

The corrected plan used import and path checks to prevent two runtimes from coexisting.
`supervisor.py` had to import the one `EventStore`, `CRITICAL_KINDS`, and merge
sequencer; a missing canonical import was an immediate failure, not permission to use a
local fallback. The event writer remained the sole sequencer, and observers received
already-redacted records. The plan kept request-ID generation and stdin deadlines at
the supervisor edge because they are transport concerns, while task policy stayed in
Architectus.

The result boundary was similarly explicit. A transient `_GenOutcome.envelope` could
inform sanitised fields, but status came from gate, merge, cancellation, and process
evidence. A flat multi-task plan did not receive an invented synthetic root. The audit
checked `ROOT_RESULT_KEYS`, `events.db`, `merge_committed`, ref advancement, process
reaping, and worktree cleanup together; one passing assertion could not mask a failed
publication or a leaked worker.

The plan's deletion list was a failure map, not a general cleanup list. Removing
`_FallbackEventStore`, `_FallbackSequencer`, local `EventLog`, and duplicate merge
exceptions eliminated split-brain type identity. Removing the fake-worker default and
slice CLI eliminated a second bootstrap path. Removing `events.py` prevented a stale
event catalog from diverging from the canonical store. Unrelated helpers and generated
files were explicitly outside scope.

## Appendix E — frozen-SHA rule

The final audit had to record one post-change SHA, working directory, command, exit
status, and structural grep output. A moving `main` or a later source merge invalidated
the result and required a new audit. “M1 complete” was not to be written from a partial
test run, a skipped security check, or a green fallback path. This snapshot therefore
preserves the plan and its acceptance gates without converting them into post-hoc status.

The corrected plan also required a no-coexistence review: after canonicalization, imports,
CLI routes, event writes, result publication, and tests had to point to the same runtime.
A green legacy slice or fallback import invalidated the audit even if the new path passed.
That rule is why the deletion inventory and migration list remain part of this historical
snapshot.

The plan's line references are advisory and tied to `main@b709375`.

The plan did not authorize broad dependency upgrades or generated-file edits. Its scope
was the runtime/store/result convergence and the named test migration.

The frozen SHA governed all audit claims.

The final docs commit was separate from implementation changes and recorded only the
audited source revision.

No implementation landing was inferred.

The plan is not a status report.

Audit evidence must be rerun.

Line anchors can drift.

Record the frozen SHA.

Audit commands stay reproducible.

Historical only.

Historical plan identifier retained: `M3`.
