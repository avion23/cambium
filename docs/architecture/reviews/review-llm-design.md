# Adversarial Review — Cambium SYSTEM_DESIGN.md (LLM/Agent Architecture)

> **Historical snapshot — pre-implementation review.** These findings apply to
> `/home/ubuntu/cambium/SYSTEM_DESIGN.md` v0.1.0-draft, not current runtime
> behavior. For current behavior, see
> [`docs/architecture/architecture.md`](../architecture.md) and
> [`docs/research/v2-1-status.md`](../../research/v2-1-status.md).

**Reviewer:** LLM Systems & DSPy specialist
**Date:** 2026-08-10
**Scope:** LLM/agent design flaws; process supervision and Git mechanics were
out of scope.

## CRITICAL FLAWS

### C1. FanOut prompt-hash caching is unsafe for coding agents

The draft key hashed only `(model, temperature, prompt)` and cached for 3,600
seconds. The same request can require different output after file edits, a new
Git HEAD, sibling-worker changes, or uncommitted state. Decomposition, routing,
and evaluation prompts are especially repetitive. No state hash, opt-out, or
stateful-call annotation existed. Disable caching for stateful worker calls or
include commit/worktree/content identity.

The collision is not hypothetical: a prompt such as “Refactor `dry_run.rs` to
remove global state” has a different correct patch after a sibling worker edits
the file. The draft's one-hour TTL can serve the old completion to a restart or
to a different worktree. ReAct observations often make prompts unique, but the
decomposer, router, and evaluator repeatedly issue short prompts that collide;
the cache therefore sits on a correctness boundary, not only a performance
boundary.

### C2. Provider cascade does not cross models

`call()` resolves an omitted model to provider 0; `_cascade` then skips providers
whose model differs. DeepCode Flash rate limiting therefore cannot fall through
to Gemini Flash, OpenAI Mini, or Claude Haiku, contrary to the stated cascade.
Race mode has the same filter. Distinguish explicit model constraints from a
provider tier/capability preference.

The code's `resolved_model = model or self.providers[0].model` means the filter
is active even when the caller asks for no particular model. A rate-limited first
provider therefore produces `AllProvidersFailed` instead of trying the intended
DeepCode → Gemini → OpenAI → Claude order. Fixing only the loop still leaves
heterogeneous tool schemas and context limits, so routing needs an explicit
capability contract.

### C3. Provider/model transparency is assumed

Gemini, Claude, OpenAI, and the named DeepCode model differ in tool-calling
formats, context limits (the draft compared roughly 1M vs 200K tokens),
instruction following, and temperature determinism. A prompt optimized for one
model can fail or truncate on another; race mode favors the fastest, often
weakest, model. `Provider` had only name/model/priority, no capability metadata.

The draft's `temperature=0.0` does not make different APIs deterministic in the
same way. Race mode can select the fastest provider before a stronger provider
responds, and a mid-task fallback can change tool-call syntax or truncate the
context. A safe cascade must either constrain a request to compatible models or
validate/translate the response before it re-enters ReAct.

### C4. “Independently hill-climbable” is false

The draft's §M9 claim says every node is independently hill-climbable. Worker
quality depends on decomposition; decomposer completion depends on worker
quality; reviewer F1 needs labels. Optimizing one node while others move makes a
non-stationary target. Hold reference tasks fixed, jointly optimize, or label the
claim as a hypothesis rather than a foundation.

For a fixed task, a bad decomposer can make a good worker appear to fail; a weak
worker can make a coherent decomposition appear wrong. Live co-adaptation then
feeds the optimizer examples whose labels move as prompts change. The review's
minimum alternative was a frozen reference suite per node, with joint tuning
only after independent metrics were shown to be stable.

### C5. No valid automatic coding metric exists

`worker_metric` rewards fewer tool calls, encouraging skipped inspection;
`decomposer_metric` trusts self-reported `done`; `reviewer_metric` requires
`ground_truth["bugs"]` that the design never creates. Tests are necessary but
gameable and the default `cargo test --lib 2>&1 | tail -5` returns `tail`'s status,
so the gate cannot reliably fail. Use tests as a floor plus behavior checks,
quality/human review, and held-out tasks.

The proposed “fewest tool calls” reward selects an agent that skips reading and
declares success. “Done” is an assertion, not a test oracle, and no pipeline
creates the bug labels required for reviewer F1. A test floor should be paired
with behavioral checks, diff sanity, and a held-out task set; otherwise SIMBA or
GEPA will optimize the proxy, not code quality.

### C6. There is no “do not decompose” path

Every request enters an LLM decomposition step, including a typo or one-symbol
rename. Over-decomposition adds worktrees, processes, merges, and conflicts;
the reject retry body is literal `...`. Add a cheap atomicity classifier or a
single-subtask path.

The cost is multiplicative: each unnecessary subtask pays for a process, a
worktree, a ReAct trajectory, a rebase, and a test run. Splitting a five-minute
atomic edit into four fragments also creates integration conflicts that a retry
cannot repair because the historical retry body is literally `...`.

## MODERATE ISSUES

### M1. Default test command is a no-op gate

With `shell=True`, `cargo test --lib 2>&1 | tail -5` reports `tail`'s success.
Capture and truncate in Python or use `pipefail`; otherwise the merge and metric
pipeline cannot observe test failure.

This defect also poisons C5: a metric that consumes the gate sees success even
when `cargo` failed. Capturing full output and truncating only for display keeps
the real return code while retaining concise logs.

### M2. Worker tools are inadequate and overpowered

The five tools were `read_file`, full-overwrite `write_file`, unrestricted
`run_shell`, `git_op`, and shell-injected `grep_code`. There is no structured
edit/patch or symbol search; `run_shell` subsumes the other tools and permits
destructive commands or network exfiltration when M8 is P2. The promised Codex
BM25 retrieval is absent. The ReAct signature `task, context -> action` also does
not match the domain-signature shape expected by DSPy ReAct.

Full-file overwrite plus unrestricted shell gives the model no precise edit
primitive and no least-privilege boundary. A structured `edit_file(path,
old_string,new_string)` or patch grammar was the concrete historical fix; BM25
retrieval was listed under “What We Copy” but had no module or test.

### M3. The flywheel has no stability controls

Metrics select shallow work, live trajectories create distribution shift, and
prompts optimized on DeepCode Flash may be deployed on another model. Add held-
out evaluation, human approval, deployment rollback, and drift checks.

The flywheel has no train/test split or rollback point. Prompt changes trained on
DeepCode v4 Flash can be deployed on Gemini or Claude, while live trajectories
mix provider quality with prompt quality. The reviewer treated optimization as
an experiment requiring a held-out gate, not an automatic production upgrade.

### M4. ReAct checkpoint callback is not a DSPy API

The draft defined `on_step_end_callback` and `trajectory_state` but never attached
them to ReAct. Checkpoints therefore may never be written or read, so a crash
restarts from turn 0 despite the Temporal-style claim. Show a custom wrapper or
remove the guarantee.

The callback is only a local function in the sample; no ReAct subclass, hook
registration, state serialization, or resume read path is shown. A worker crash
therefore restarts from turn zero and can repeat a side effect the design called
idempotent.

### M5. FanOut cache is per-instance and nearly useless

Workers are processes, each with its own dict; within a ReAct loop observations
usually differ each turn. Cross-worker cache hits do not occur, while C1's stale
state risk remains. Remove it, make it opt-in for stateless prompts, or provide a
safe shared store.

Separate worker processes cannot see one another's dict. Within one worker,
tool observations normally change every turn, so hit rate is low even before
state invalidation is considered. The review's alternatives were remove it,
make it opt-in for stateless calls, or build a shared store with explicit world
state in its key.

### M6. Race mode discards results and mishandles exceptions

`FIRST_COMPLETED` can select a task that raised while another provider would
shortly succeed; `winner.result()` then fails. Pending requests are cancelled
after potentially consuming quota, and fastest/weakest model bias is unchecked.
Scan completed tasks for a successful result and define cancellation/quota policy.

`asyncio.wait(..., FIRST_COMPLETED)` reports completion, not success. If the
first task raises, `winner.result()` raises while another provider may be about
to return a valid answer. Cancellation can still consume provider quota, and
choosing latency over quality needs to be an explicit policy.

## MINOR NOTES

- **N1:** Concrete defects include `write_content`, missing `grep_code` return,
  missing `os`, malformed error type, broken `__task_id_counter`, literal retry
  ellipsis, `self.root`, metric syntax tokens, `.c flywheel` path, invalid
  sandbox method, missing `sys`, duplicate M7 row, broken box drawing, and a
  stray comment fragment.
- **N2:** `SubTask.depends_on` defaults to `None` despite `list[str]`; DAG cycles
  and failed dependencies are not rejected.
- **N3:** `_try_provider` constructs a fresh `dspy.LM` per call.
- **N4:** `sys.stdin.readline()` blocks inside `async main()`; the worker is
  effectively synchronous despite its async wrapper.
- **N5:** `collect_commits` uses `HEAD~5..HEAD` and fails on short histories.
- **N6:** “Zero external runtime dependencies” omits LiteLLM, sandbox tooling,
  and DSPy's transitive dependencies.

The review separated model-layer findings from process-supervision findings on
purpose. C1 and C2 are routing/caching correctness failures; C3 concerns model
capability substitution; C4–C6 concern optimization, metrics, and task shape.
The concrete alternatives were conservative: disable stateful caching, route
only across compatible capability tiers, freeze evaluation tasks, make tests a
floor rather than an oracle, add a structured edit tool, and allow atomic
dispatch. These recommendations preserve the draft's provider-cascade and
DSPy goals while making their assumptions testable.

## VERDICT

**Not build-ready as an LLM/agent design.** The process-supervision scaffolding
was the strongest part; the coding layer had fatal stale-cache and cascade bugs,
coupled optimization with unusable metrics, no structured editing, and no atomic
task path. The recorded recommendation was to re-key/disable cache, implement
capability-aware cascade, build a real metric, add structured edits and retrieval,
add atomic dispatch, and treat hill-climbing independence as a hypothesis. The
Erlang scaffolding was good; the part that writes code still required design work.
