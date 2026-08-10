# Cambium — Second External Critique: Assessment + Deltas D8a..D8g

**Version:** 1.0.0
**Date:** 2026-08-09
**Branch:** `wt-fb2` (`/tmp/opencode/cambium-fb2`), based `96da568`
**Status:** Historical assessment of critique claims against architecture v2.0.0, D1–D7,
and merged research. D8a–D8g are adopted amendments; current readers use
`docs/architecture/architecture.md`, `src/cambium/`, and `docs/research/v2-1-status.md`.

**Historical snapshot / current pointer:** the old 30/66-file counts, `main@3621fd9`, and
branch provenance are retained as evidence only. Current notes: provider loop, Diffundo,
EventStore, and root `Result` exist; DLQ, eval cache, ResourceBudget, `worker_pool`, and
`events` are absent; no per-worker sandbox or production shell approval exists, and dynamic
hierarchy is absent.

## 0. Evidence convention and provenance

The critique text was not committed; claim numbers below reproduce the orchestrator disposition.
Repository paths/sections were checked or marked **UNVERIFIED**. Architecture/template were read
from `wt-arch@17ef25f`; D1–D7 from `wt-deltas2@905fc1b`; merged research from `main@3621fd9`;
branch-local `cascade-design.md` `wt-cascade@73093e7`, `sqlite-wal-durability.md`
`wt-sqlite@7f6ac8d`, and `repo-structure-plan.md` `wt-hygiene@660f930`.

## 1. Fourteen critique claims

| # | Claim | Disposition | Retained reason/evidence |
|---|---|---|---|
| 1 | Directory mess/duplicates | **REJECT** (one hygiene residue) | `git ls-files` found no tracked nested `cambium`, only intentional `__init__.py`; the real residue is research-doc volume and template/instance overlap. `repo-structure-plan.md` §2/§3 is the taxonomy. |
| 2 | Independent hill-climbing delusion | **STALE** | Architecture §17.2 already pins frozen siblings; dataset v1 is 200 train/50 eval/10 canary (`example-datasets-v1.md` §1, deviation §5). |
| 3 | `should_decompose` regex is toy | **ACKNOWLEDGED / roadmap** | v2 deliberately uses a ~140 LOC rule engine; DSPy/SIMBA is the v2.1 seam (`example-spec.md` §§0.2, 5.1, 10). |
| 4 | Async I/O deadlock | **STALE / fixed** | DS-C1 was fixed by dedicated writer thread (`architecture.md` §6.2); WAL and Custos research empirically validate it (`sqlite-wal-durability.md`, `custos-asyncio-design.md`). |
| 5 | Actor/SoC/Kahn/Custos separation | **CONFIRMED** | Architecture §§0, 2, 8.2 retain OTP supervision, deterministic LLM-free Custos, and Kahn only where valid (DS-N6). |
| 6 | Let-it-crash/event sourcing/saga | **CONFIRMED** | §§5.3, 6.1–6.3, 7.1, 7.3, 7.5, 7.8 provide liveness, WAL replay, fencing, recovery, and atomic publish. |
| 7 | Pure JSON module + CLI | **ADOPT — D8a** | Typed `Module.decide`/dataclasses exist; no module pipe entry exists (`base.py`, template §3, example-spec §12). |
| 8 | RLM information hiding | **ADOPT — D8b** | D2 has upward envelopes and sibling isolation, but no explicit no-scratchpad rule or normative diff field. |
| 9 | Provider prefix caching | **ADOPT — D8c** | D1 removes local cache; static prompt prefix must precede dynamic content. |
| 10 | Hexagonal ports/DI | **ADOPT — D8d** | `Output`/`Metric` Protocols and `CambiumLM` injection are precedents; the template did not name provider/event/dataset ports. |
| 11 | Sandbox/container boundary | **ADOPT — D8e** | D7 removed in-harness sandbox; host containers/microVMs are the deployment boundary. |
| 12 | LLM plan → N workers → queue → Unio | **CONFIRMED** | Architecture §§2, 5–7 and D2/D3 match; no delta. |
| 13 | Token bucket/circuit breaker/pause | **ADOPT — D8f residue** | Tiers/breaker existed in `cascade-design.md`; rate limiter and queue pause were missing. |
| 14 | SQLite conversation store; JSONL IPC | **ADOPT — D8g residue** | Event WAL and JSONL IPC already exist; per-node queryable conversation history was missing. |

## D8a — Pure JSON module CLI

**Source:** external critique claim 7. **Status:** **adopt**. **Amends:** module-template
architecture §§3, 9; architecture §4; example-spec §12.

Every module ships `python -m cambium.modules.<name>`: one JSON object on stdin, one JSON object
on stdout, exit 0 on success, structured error/non-zero on failure, stderr for diagnostics.
Typed dataclasses are strict schemas; the wrapper is thin and leaves `Module.decide`/DSPy seam
unchanged. It is distinct from the v2.1 eval entry point. The rule makes modules pipeable and
independently testable; no CLI existed in the snapshot.

**Open questions:** Q8a.1 single object versus JSONL batch; Q8a.2 async `decide` confirmation;
Q8a.3 error envelope/schema version; Q8a.4 `__main__.py` versus per-module `cli.py`.

## D8b — Task Tree information hiding

**Source:** claim 8. **Status:** **adopt**. **Amends:** architecture §§3.4, 5.2, 6.3 and D2
invariant I2.7.

Child→parent carries exactly `unified_diff` (64 KiB cap, `diff_truncated`), summary (≤2,000
chars), metric breakdown, commits, files changed, and terminal status. It never carries
scratchpad, chain-of-thought, reasoning, or trajectory. Those remain in the node store for the
node, Ascensus, or explicitly authorized host. Nuntius/Custos rejects unknown envelope fields;
the rule is structural. The parent context remains D2 I2.4 (own bounded log + parent summary +
subtree envelopes).

**Open questions:** Q8b.1 truncation flag versus content-addressed diff; Q8b.2 worker-authored
three-sentence summary versus deterministic fallback; Q8b.3 absolute envelope-only visibility
for parent LLMs versus audit-only tool events.

## D8c — Provider prefix caching: static top, dynamic bottom

**Source:** claim 9. **Status:** **adopt**. **Amends:** architecture §9.3, D1, module-template §5.

System/AGENTS/tool/module instructions and stable few-shot context are the byte-stable prefix;
task spec, repo context, observations, and tool results are the dynamic suffix. Timestamps,
request IDs, monotonic values, and nonces never enter the prefix. This enables provider exact-
prefix caching but is not a correctness cache. A pure prompt lint checks the ordering.

**Open questions:** Q8c.1 helper versus convention; Q8c.2 DeepSeek ~64-token alignment (UNVERIFIED);
Q8c.3 ReAct observations remain dynamic at the bottom.

## D8d — Hexagonal modules: ports/adapters + DI

**Source:** claim 10. **Status:** **adopt**. **Amends:** module-template §§3, 5.4; architecture
§§2, 4.

Name typed ports: `LLMProvider.call`, `EventSink`, and `DatasetStore`; adapters (for example
`DiffundoAdapter`) implement them. Construct modules at one composition root from `Config`; do
not import/construct concrete providers inside a module. The v2 rule engine is pure, so the
scaffold needs no fake LLM; a future DSPy `decide` receives an injected port. This preserves
the deterministic layer's no-LLM invariant.

**Open questions:** Q8d.1 `container.py` versus orchestrator composition root; Q8d.2 add a
test-injected `Clock`; Q8d.3 whether `CambiumLM` already satisfies `LLMProvider`.

## D8e — Deployment isolation outside the harness

**Source:** claim 11. **Status:** **adopt** (D7 residue). **Amends:** architecture §§2, 4, 7.2;
D7.

No in-harness sandbox is restored. A local or containerized worker speaks the same JSON-Lines
stdio contract; Docker/Firecracker are host-owned, optional, and transport-agnostic. Cambium
does not build/manage containers. `sandbox-options.md` records the AppArmor block that caused
the D7 decision.

**Open questions:** Q8e.1 reference image; Q8e.2 composing container env with D7's scrubbed env;
Q8e.3 Docker versus Firecracker (UNVERIFIED; no container run).

## D8f — Token bucket and pause on provider exhaustion

**Source:** claim 13. **Status:** **adopt**. **Amends:** architecture §§7.4, 9.1–9.2.

`Diffundo.call` checks a per-provider (optionally per-tier) token bucket before each cascade
attempt; empty buckets mark `RATE_LIMITED` and are skipped with cooldown/breaker filtering.
When all providers fail, dispatch pauses and in-flight workers await a recovery monitor rather
than crash-looping. Existing tier ordering and circuit breaker remain from `cascade-design.md`.

**Open questions:** Q8f.1 per-provider `rpm` defaults (UNVERIFIED); Q8f.2 Custos versus
orchestrator recovery monitor; Q8f.3 gate behavior during a provider pause.

## D8g — Per-node conversation history in SQLite WAL

**Source:** claim 14. **Status:** **adopt**. **Amends:** architecture §6.1 and D2 item 2.

The event log remains SQLite WAL and IPC/optional mirrors remain JSONL. New per-node
conversation history is queryable WAL state for bounded context composition (`last N turns`,
cost, turns since checkpoint), with bounded retention/compaction. Proposed layout is
`sessions/conversations.db` with `node_sessions` tables; using tables in `events.db` is the
alternative. D8g changes D2's append-only files, not architecture §16.2's pre-existing layout.

**Open questions:** Q8g.1 separate DB versus `events.db`; Q8g.2 `ConversationStore` API
(`last_turns`, `context_for`); Q8g.3 transcript versus cross-cutting event duplication.

## 2. Unverified flags and disposition

Branch-state divergence, D8a CLI implementation, D8f rate defaults, D8e container execution,
D8g DB layout, and the “regex = toy” characterization were not re-run in this snapshot. Claim-1
file counts were `git ls-tree` calculations (30 baseline; 66 after this document) and must be
recomputed on current main. The design deltas are historical evidence; no repo-layout tree is
repeated here.

| Version | Date | Change |
|---|---|---|
| 1.0.0 | 2026-08-09 | Claims 1–14; D8a–D8g; original layout proposal. |

## 3. Rationale retained from the critique assessment

### Claims 1–6: what was already in the design

The “directory mess” claim was checked against tracked files, not a live virtual environment:
the only `cambium` directory was `src/cambium/`, duplicate basenames were intentional Python
package markers, and no `.venv`, `.pytest_cache`, or `__pycache__` file was tracked. The real
residue was documentation volume and the deliberate template/instance pair. `repo-structure-
plan.md` classified research evidence, reviews, canonical architecture, templates, per-module
docs, and transient plans; no file move was required. This rejects the critique without deleting
historical evidence.

The independent-optimization claim was already handled by architecture §17.2: each module uses
frozen sibling references, not live co-adapted modules. The dataset v1 deviation is 10 canaries
against the template's 15 target, and remains recorded rather than silently re-anchored. The
regex criticism is accepted as a deliberate v2 rule-engine trade: DSPy/SIMBA remains a v2.1 seam,
not a claim that the rule engine is production-quality LLM reasoning.

DS-C1 (blocking `open`/`write` on the event loop) was fixed in the architecture before this
assessment. SQLite WAL experiments cover read-while-write, crash loss, fsync target, and a ≤1 s
non-critical window; Custos design states that the event loop never calls `open`, `write`,
`fsync`, sqlite, or blocking git. Actor/OTP, separation of concerns, and Kahn language are kept
only where structurally true (`DS-N4` tool boundary and `DS-N6` Kahn pass-through distinction).
The “let it crash”/event-sourcing/saga claim is likewise already represented by §5.3/§7.1
liveness, §6 WAL replay, §7.3 generation fencing, §7.5 worktree recovery, and §7.8 atomic
publish/reconcile. These rows create no new delta.

### D8a: executable module boundary

The CLI contract is intentionally one object in/one object out, not an undocumented JSONL
stream. `<name>` is the package directory (`example`), while the logical decision is
`should_decompose`; the two names must not be conflated. Unknown/invalid fields are rejected by
the same validation pattern as `ExampleDatasetLoader._validate`, and errors use a JSON `error`
object with nonzero exit. A thin `__main__.py` keeps `decide()` pure and leaves a future DSPy
replacement compatible. The eval entry point remains separate (`python -m ...eval`) so scoring
does not become the production module surface. Q8a.1–Q8a.4 cover batching, async execution,
error versioning, and wrapper placement.

### D8b: strict information hiding

D2's parent context rule was insufficiently explicit: it forbade sibling raw-session reads but
did not say that a parent must not receive child chain-of-thought. D8b makes the upward envelope
an allow-list: `unified_diff`, bounded summary, `metric_breakdown`, commits, files changed, and
terminal status. `diff` is capped at 64 KiB by the merged IPC draft; overflow sets
`diff_truncated`. Unknown top-level fields such as `scratchpad` and `reasoning` are rejected by
schema validation. Ascensus may inspect a node store offline, but Custos never forwards the
transcript. The rule protects context budget and prevents repository text from steering an
ancestor as if it were trusted reasoning (threat-model R1). Q8b.1–Q8b.3 keep truncation,
summary authorship, and audit visibility open.

### D8c: prompt layout is performance-only

With D1's local cache gone, provider exact-prefix caching is the only caching concern. Static
system/AGENTS/tool/module instructions and stable few-shots go first; task spec, repository
context, observations, and tool output go last. A timestamp, request ID, monotonic value, or
nonce at the head invalidates a prefix but cannot change correctness. The proposed lint is a
pure helper over `(static, dynamic)` segments, so it can run without a provider. DeepSeek's
prefix unit alignment is **UNVERIFIED** and requires the telemetry in D1 Q1.2. Q8c.1–Q8c.3
retain helper, alignment, and ReAct-turn questions.

### D8d: ports are a future-provider boundary

The scaffold already has `Output`/`Metric` Protocols and `CambiumLM` constructor injection, but
the module template did not name an `LLMProvider`, `EventSink`, or `DatasetStore` boundary. D8d
records those ports so a future DSPy module cannot import a concrete provider accidentally. The
pure v2 rule engine needs no fake provider and no network; a DSPy implementation will. A
composition root (proposed `container.py` or orchestrator) builds adapters from `Config` and
injects them. The deterministic layer still never imports an LLM type. Q8d.1–Q8d.3 cover root,
port granularity/clock, and `CambiumLM` adapter shape.

### D8e: deployment vehicle is outside Cambium

D7 already removed Septum. D8e names what an operator may use instead: wrap the same
`python -m cambium.opifex` stdio process in Docker or Firecracker and connect its pipes. The
protocol bytes and harness semantics do not change, and Cambium neither builds nor assumes the
vehicle. The AppArmor evidence in `sandbox-options.md` remains the reason the in-harness option
was dropped. Q8e.1–Q8e.3 leave image layout, env composition, and vehicle choice to operations;
no container execution was performed.

### D8f: rate and outage behavior

Cooldown bounds failure repetition but does not bound a healthy provider's throughput. The token
bucket therefore refills at provider/tier `rpm`; an empty bucket yields `RATE_LIMITED` and uses
the same selection filter as cooldown. When every provider is unavailable, dispatch pauses and a
recovery monitor wakes it; workers do not restart-loop while waiting. Existing tier fallback and
sliding-window breaker in `cascade-design.md` are retained, not duplicated. Q8f.1 leaves defaults
UNVERIFIED; Q8f.2 chooses the monitor owner; Q8f.3 specifies gate behavior during pause.

### D8g: event log versus conversation projection

The event log answers “what happened system-wide”; a node conversation store answers “what did
this node see and decide?” D8g therefore uses SQLite WAL for bounded queries (`last_turns`, cost,
turns since checkpoint) and keeps JSONL only for IPC/optional mirror. Per-node append-only files
from D2 become a WAL-backed projection with bounded retention/compaction. The proposal is a
single `conversations.db` under `sessions/`, but Q8g.1 leaves separate DB versus events tables
open; D8g's attribution note prevents readers from mistaking the new `sessions/<node_id>` tree
for architecture §16.2's older layout. Q8g.2 defines the future `ConversationStore` port;
Q8g.3 separates protocol transcript from cross-cutting audit facts and calls out double-write
risk.

## 4. Historical source anchors

The original hygiene and review references `IMPL-M2`, `IMPL-M5`, `LLM-C4`, and `LLM-M3` remain
part of the decision corpus. Claim-1 counts (30 tracked files at `96da568`, 66 after the
document) were `git ls-tree` calculations over branch unions; they are not current-main counts.
The full source-map/tree block was removed because architecture and `v2-1-status` now own those
pointers.

## 5. Later hierarchy feedback — skeptical classification

The later “explicit agent tree” feedback is consistent with D2/D8b only at the structural
boundary. It does not change the historical D8 dispositions:

- **Accept as target:** the harness owns an explicit TaskTree/DAG; admission follows static
  validation; each child gets fresh declared context; upward messages are strict diff/summary/
  metrics/status envelopes; raw scratchpads and reasoning stay in the child store.
- **Static-before-dynamic admission:** accept as an M5 invariant. Validate IDs, dependencies,
  cycles, depth, width, and envelope shape before any worker is admitted. A worker may return an
  outcome, but may not add a sibling or alter the topology implicitly.
- **Implicit recursion is “dead”:** not a verified fact. The design rejects implicit topology,
  but no source comparison or runtime measurement proves a universal claim about recursion.
- **90% cache discount, “Prime 2026 proves it,” and “five cheap branches”** are **UNVERIFIED as
  broad claims**: the primary audit supports Prime explicit `AgentSession`/runtime contexts and
  bounded depth, with descendants sharing one root worker, but no process-per-child isolation or
  90% total-request/latency metric. Provider caches are org/workspace scoped; exact prefixes can
  be shared by tasks. D1/D8c keep static-prefix guidance and require measured token/latency/cost.
- **AlphaCodium/LATS require MCTS/tests at every node:** the primary descriptions are a staged
  run/fix flow (AlphaCodium) and candidate-solution MCTS with test/environment feedback (LATS),
  not universal task orchestration. M5 requires per-node deterministic gates; MCTS needs a
  falsifiable comparison against the explicit DAG baseline.

This classification accepts information hiding and explicit hierarchy as a target while keeping
consensus, cache economics, and algorithm universality out of the current-runtime record. Recursion
evidence remains task-dependent; no implicit-recursion dead-end consensus is adopted.

## 6. Implementation boundary notes

The D8 set is deliberately additive except where it closes an ambiguity. D8a adds a transport
wrapper without changing the `Module` ABC. D8b promotes an existing slice `diff` field and
existing architecture summary limit into a schema allow-list. D8c adds a prompt-order lint but
does not add a cache or provider dependency. D8d names ports around existing Protocols and
constructor injection rather than introducing a framework. D8e documents a host deployment
vehicle without adding Docker/Firecracker code. D8f layers a bucket and queue pause on existing
cooldown/breaker filtering. D8g changes only D2's node-session storage engine; IPC JSONL and the
cross-cutting event log remain unchanged.

The test obligations are equally narrow. D8a must exercise a one-object pipe and error envelope;
D8b must inject a scratchpad field and prove schema rejection plus a 64 KiB truncation flag;
D8c must lint volatile tokens at the static-prefix head; D8d must run a fake `LLMProvider` without
network; D8e can use a stdio fixture rather than a real container; D8f must pause a queue on
synthetic total outage and wake it on recovery; D8g must delete/rebuild a conversation projection
from protocol events. None of these checks existed in the snapshot, so “adopt” is a decision
status, not an implementation status.

## 7. Scope of adopted status

The D8 table is a decision register, not a release checklist. “ADOPT” means the architecture or
template should carry the rule; “CONFIRMED” means an existing contract was found; “STALE” means
the critique targeted an earlier version; “UNVERIFIED” means source or metric evidence was not
available. A branch module, a copied directory tree, or a source symbol with no caller cannot
upgrade an adopted delta to delivered runtime.

The later hierarchy feedback sharpens this distinction. The target is a static DAG validated
before admission, with fresh child context and strict parent envelopes. Dynamic admission may
select ready nodes from that DAG, but it may not create topology. This makes information hiding
testable without claiming that a current worker tree, provider cache hit, or MCTS scheduler exists.

## 8. Required falsification evidence

Before a future document changes these dispositions, it must provide: a primary source for any
external consensus claim; a fixed task set and provider/model; measured prompt tokens, latency,
cost, and success; a schema/test proving scratchpad exclusion and DAG-before-admission; and a
rollback or failure case. In particular, “90% cache discount,” “Prime 2026 proves it,” “cheap
five branches,” and “MCTS at every AlphaCodium/LATS node” remain outside the normative corpus
until those conditions are met.

The later primary-source correction also changes how D8e's Prime precedent is described. Prime's
explicit child `AgentSession`/runtime objects support independent context and bounded-depth
hierarchy, while descendants share one root-session worker; that is not process-per-child
isolation. This distinction matters for D8e deployment and M7 pool: a host container boundary is
optional, while a shared worker runtime has different contamination/reset requirements. Any
future benchmark must report worker lifetime, context isolation, prefix hit rate, total cost,
latency, and task success separately.

The current-status pointer is intentionally repeated only at file header and here: implementation
claims must be checked in source. In particular, a matching `Diffundo` name does not prove the
provider loop, a `store.py` file does not prove runtime EventStore wiring, and a planned DLQ does
not make malformed messages durable today.

Prime's shared root-session worker is relevant to D8e: a host container is an optional deployment
boundary, while context isolation must still be enforced inside the worker runtime. Exact-prefix
matching is compatible with shared org/workspace caches; it prevents wrong-prefix reuse but does
not promise per-conversation privacy or a fixed discount. Measure context bytes and provider
billing separately from hierarchy correctness.

Static plan validation is also a safety and cost gate: reject cycles, unknown dependencies,
depth/width overflow, and envelope violations before admission. Steering can revise a node's
task content but cannot add a sibling or second root. A future scenario should inject a topology
mutation after admission and prove it becomes bounded typed evidence with no extra worker or
provider call.

This order keeps the structural and performance questions independent: M5 can prove graph and
context boundaries with a fake provider; M6 can measure exact-prefix hits and cached-token billing
on a fixed task; M7 can test shared-worker reset. No Prime, AlphaCodium, LATS, cache, or branch-
count slogan is needed to pass the structural test.

This also keeps source terms precise: “AgentSession” denotes a runtime/context abstraction, not
a process; “cached-token read price” denotes an input billing rate, not total latency; “MCTS”
denotes LATS's candidate search, not a universal TaskTree scheduler; and “run/fix” denotes
AlphaCodium's staged flow. The historical D8 IDs remain unchanged.

No adopted delta therefore implies current provider admission, pool isolation, or cache telemetry;
those remain milestone tests.

The structural gate is the first falsifiable result.

The record distinguishes accepted structure from unverified economics and keeps current-runtime
claims out of this historical assessment.
