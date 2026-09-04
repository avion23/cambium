# Open implementation work

This file contains unfinished work, not another architecture specification.
Current behavior and module ownership are in
[the runtime map](docs/architecture/architecture.md). Proposed data shapes remain
in [agent-state reference](docs/reference/agent-state.md).

Work on one observable failure or complete runtime path at a time. Preserve
parallel work and avoid creating an abstraction merely to complete a diagram.

## 1. Converge the existing state readers

`BranchState` and CLI `inspect-state` already exist. The TUI still derives common
semantics through `observability.py`; the complete model SituationFrame and
`inspect_state` proposal are not integrated in this base runtime.

Finish the shared fields that prevent concrete disagreements: branch lifecycle,
accepted artifact head, context identity, children and current verification.
Reuse the existing event/checkpoint sources. Add a small late model suffix only
for facts that affect a decision; do not repeat the task, tool manual or empty
sections on every call. Preserve exact-prefix and compaction behavior.

Coordinate with parallel SituationFrame work rather than implementing a second
frame/reducer. Prove agreement on replay, child join and reconnect using the
same event prefix. A digest-only event without a working consumer is not a
completed feature.

## 2. Make resource policy coherent

There are currently different debt-balancing and requirement-scoring paths.
Use measured service performance and the existing `QuotaLedger` in one
understandable admission policy, retaining configured priority and hard
capability constraints. Keep request rate, in-flight slots, account windows,
cache affinity, cash and wall time separate.

Replace or clearly isolate the 20-million-token fallback normalizer; it is not
a weekly quota. Unknown account allowance and reset times must remain unknown.
Compare correct accepted work per wall time and actual account usage, including
cold input, retries, summaries, child joins and repair. A synthetic provider
ordering test alone is not a performance result.

Resolve the naming and contract of the worker token budget: it currently counts
marginal uncached context growth plus output, not all billable repeated input.
Keep the context-growth guard distinct from a real spending limit. Do not
silently tighten existing task budgets while relabeling the same counter.

Expose only decision-relevant resource facts through the existing state reader.
Do not create another quota ledger or model call to calculate a policy score.

## 3. Retain unfinished work across long sessions

Test specific obligations and verification evidence through summary flush,
rollover, restart, cancellation and child integration. A check must stay tied to
the artifact/configuration it actually tested; a later edit may invalidate it.

Start with the existing SummaryEntry fields and result envelope. Add structured
identity only where string summaries demonstrably lose or confuse a required
item. The complete WorkLedger and ResultCapsule-v2 references are proposals,
not a requirement to install a second knowledge database or migrate every
reader before fixing an observed loss.

Historical tool retrieval is already active and scopes interactive references
by operator turn. Remaining scaling work should measure archive scan cost and
checkpoint retention on long sessions, then reuse the event store's bounded
query facilities where necessary. Output caps alone do not bound all replay
work.

## 4. Evaluate the actual coding prompt

The DSPy modules `should_review` and `should_decompose` are offline experiments;
they do not optimize or load the normal coding prompt. Build a small repeatable
coding comparison using executable repository tasks and the real worker.

Separate training from held-out tasks by source session/repository. Do not use
final outcomes as pre-task inputs. Review/failure labels are not evidence that
an additional agent improves completion. Include malformed actions, actual
provider input/cache/output usage, summaries, repair and wall time.

Compare against the current short static prompt. Adopt useful instructions as
an explicit versioned prompt change, or add a loader only when there is a
measured reason to do so. Do not insert a review/decomposition call into every
task or make DSPy a mandatory hot-path dependency.

## 5. Improve the operator experience from terminal traces

Active inspection, cancellation, usage accumulation and explicit repaint are
covered by PTY regressions. The compact rail still has poor discoverability,
and shared model/operator state semantics remain incomplete.

Use real PTY traces for narrow terminals, long tool output, multiline input,
resize during typing, child activity and reconnect. Prefer clearer text and
stable input over additional panels. Measure replay/repaint work on long
sessions before adding caches or another frontend state machine. Keep non-TTY
output useful and distinguish `NO_COLOR` styling from cursor capability.

## 6. Add explicit root migration only through a complete path

Call-time fallback and a provider lease value already exist. Durable root
migration from an accepted checkpoint, with its artifact and open-work state,
is a separate unfinished transition. Implement it only with a concrete
exhaustion/dead-provider scenario and a working resume path. Record the changed
lease and cold/semantic context honestly; do not call it exact cache reuse.

## Completion evidence for each change

Run the focused regression through its actual producer/consumer, then the
affected suite. Worker, tool or TUI changes also need the appropriate live
coding or PTY path. Retain unsuccessful trials and distinguish model/provider
failures from harness defects. Commit verified work and integrate it into the
requested branch without disturbing unrelated changes.
