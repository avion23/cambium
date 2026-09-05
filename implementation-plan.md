# Remaining implementation work

This is an open-work list, not a second architecture specification. Current
behavior is mapped in [the docs index](docs/README.md). The larger design ideas
remain in [the operating model](docs/architecture/agent-operating-model.md) and
[agent-state reference](docs/reference/agent-state.md).

## Direction

Keep the harness small: one worker loop, one supervisor, one publication owner,
existing events/checkpoints/Git as evidence, and offline optimization. Improve
correct useful work per second and per provider quota window. Add no planner,
reviewer, approval step, memory database, or second scheduler without a concrete
trace showing why it is needed.

## 1. Finish shared current-state integration

`branch_state.py` and CLI `inspect-state` already exist. The model, interactive
manifest, and `observability.py` are not yet all consumers of one shared current
state. Complete that integration rather than introducing a second canonical
store.

A useful first slice is a bounded model-facing SituationFrame and `inspect_state`
read operation, derived from the same durable evidence as operator inspection.
Keep rapidly changing state in the request suffix, not the stable prompt head.
Include only decision-relevant task, artifact, context, child, verification, and
resource facts; missing facts must stay unknown. Do not turn the proposed long
section list into mandatory boilerplate on every call.

Verify agreement at a source watermark, reconnect/restart behavior, and prompt
cost. A frame that consumes more context than it saves should be reduced or
omitted. A library or target schema alone is not completion of this slice.

## 2. Preserve useful obligations and evidence through context changes

The existing SummaryEntry/K0 machinery carries semantic strings. Add stable
references for the few facts, decisions, open checks, and failed approaches that
must survive a fold, child join, or restart. Reuse existing event/tool/Git
identities and `branch_history`; do not build another memory service.

Tie verification to the artifact that was checked. An overlapping accepted edit
can stale that evidence. Demonstrate one lost-obligation or stale-verification
case end to end before expanding the proposed WorkLedger schema.

## 3. Evolve child results without a parallel join protocol

The current bounded result plus validated Git join is the starting point.
Introduce a richer versioned ResultCapsule only where it avoids a demonstrated
history replay or lost child obligation. Evidence references and a concise
parent action are more useful than copying the full child transcript.

Keep semantic acceptance, artifact integration, and combined-tree verification
separate. Use the existing ordered merge/resolver owner. Test a child code join,
a read-only child, a conflict, and a failed required child under the same path.

## 4. Improve quota-aware routing with measured evidence

Current output rates and quota observations are usable; decayed routing debt is
still a heuristic. Evaluate remaining tokens/requests and time-to-reset for real
provider windows, especially weekly plans, within `routing.py`. Do not mistake
the fallback token allowance for measured weekly capacity.

Compare accepted tasks/hour and tokens per accepted task across providers,
including summary/retry/child overhead. Use output-only throughput and preserve
unknown tariffs/quotas as unknown. Keep resource steering explicit at compatible
context boundaries; provider migration is not free KV-cache transfer.

## 5. Run finite offline prompt experiments

The two DSPy decision programs, adapter, tests, and evaluation/save-load path
work. They do not optimize or deploy the coding worker's static system prompt.
Build finite repository-task comparisons before adding any automatic promotion.
Use executable artifact/check results, not self-reported success or hidden
reasoning, as labels.

Bound calls and tokens as well as cash for zero-tariff subscriptions. Compare a
short hand-written baseline with candidates on held-out cases. Keep a rule when
an additional classifier call costs more than its measured benefit. Do not add
an online “optimization gate.”

## How to land each change

Take one observed failure or wasteful trace, fix the smallest complete path,
add a regression, and run the affected tests. Model-loop changes also need a
real provider-backed task; canned responses verify plumbing, not coding ability.
Keep terminal interaction checks at a PTY/process boundary.

Update only the document that owns the changed contract, commit the code and
checks together, and integrate the tested result. Large schema inventories,
unconsumed abstractions, and long lists of mandatory review gates are not
substitutes for a working slice.
