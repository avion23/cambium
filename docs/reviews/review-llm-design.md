# Adversarial Review — Cambium SYSTEM_DESIGN.md (LLM/Agent Architecture)

**Reviewer:** LLM Systems & DSPy specialist
**Document:** `/home/ubuntu/cambium/SYSTEM_DESIGN.md` (v0.1.0-draft)
**Scope:** LLM/agent design flaws only. Process-supervision and git mechanics are out of scope here.

---

## CRITICAL FLAWS

### C1. FanOut caching by prompt hash is unsafe for coding agents — and is not addressed

`FanOut._cache_key` (lines 214–215) hashes `(model, temperature, prompt)`:

```python
def _cache_key(self, prompt: str, model: str, temperature: float) -> str:
    return hashlib.sha256(f"{model}:{temperature}:{prompt}".encode()).hexdigest()
```

The cache key contains **no representation of repository / filesystem state**. For a coding harness this is a correctness bug, not an optimization:

- The same prompt ("Refactor `dry_run.rs` to remove global state") must produce *different* outputs depending on the current file contents, the current git HEAD, sibling-worker edits, and any in-flight uncommitted changes. Two workers (or the same worker across a restart) issuing an identical prompt will receive a **stale, possibly destructive** cached completion.
- In a ReAct loop, prompts that embed tool *observations* vary naturally and are safe — but the FanOut cache is also hit by **decomposition, evaluation, and routing prompts** (M6), which are highly repetitive across tasks and are exactly the calls most likely to collide while the repo state underneath them has moved.
- The cache is an unbounded correctness hole precisely because the design advertises "every LLM call in the harness goes through FanOut" (line 206). That includes calls whose correct answer is a function of mutable world state.

**The document never addresses this.** Section M2's "Design Rationale" (lines 292–298) celebrates the cache as a feature and lists it again in §6 ("What We Do Differently": "Cache: FanOut-level prompt caching"). There is no opt-out flag, no "stateful call" annotation, no worktree/commit-sha inclusion in the key, and no TTL short enough to be safe (default `cache_ttl = 3600`, line 200 — one hour of potentially stale codegen).

**Required fix:** either (a) disable caching by default for all calls originating from workers/orchestrator and reserve it for genuinely stateless prompts, or (b) fold a content hash of the relevant file(s) / `git rev-parse HEAD` / worktree id into the cache key. The current design will silently produce wrong edits.

---

### C2. Provider cascade does not actually cascade across models

`_cascade` (lines 260–274) contains:

```python
for provider in self.providers:
    if model and provider.model != model:
        continue  # skip if specific model requested
```

`call()` resolves `model` to `self.providers[0].model` when the caller doesn't specify one (line 248), so `model` is **always non-None**. Combined with the `continue` above, the cascade will **only ever try providers whose `.model` string exactly matches the first provider's model**. If DeepCode v4 Flash is rate-limited, the loop will *not* fall back to Gemini Flash or Claude Haiku — it will exhaust the (empty) candidate list and raise `AllProvidersFailed`.

This directly contradicts the stated design ("DeepCode Flash → Gemini Flash → OpenAI Mini → Claude Haiku. First success wins", line 294) and the headline value proposition in §6 ("FanOut cascade through N providers"). The multi-provider resilience that is the entire reason FanOut exists is **not implemented by this code**.

The same bug exists in `_race` (line 277: `if not model or p.model == model`), so race mode is also single-model.

**Required fix:** cascade should be across *providers* (API keys / base_urls), and explicitly across *models* of comparable tier. Introduce a "tier" or "role" field so a request for "a fast coding model" can match DeepCode Flash, Gemini Flash, and Haiku interchangeably, and document the capability tradeoff that introduces (see C3).

---

### C3. Provider/model transparency is assumed where none exists

Even with C2 fixed, the document assumes `dspy.LM` + LiteLLM gives transparent substitution between DeepCode v4 Flash, Gemini Flash, OpenAI Mini, and Claude Haiku. In practice these models differ in ways that break transparent cascading:

- **Tool-calling format.** Gemini, Claude, and OpenAI use incompatible function-calling schemas. DSPy's `ReAct` will fall back to text-based tool parsing on models without native function calling, producing materially different (and worse) trajectories. A worker prompt optimized against one tool-calling convention will misbehave on another.
- **Context window.** Gemini Flash (~1M), Claude Haiku (~200K), and (hypothetical) DeepCode Flash differ by an order of magnitude. A cascade that succeeds on Gemini at 600K tokens will silently truncate or reject on Haiku.
- **Instruction following / capability.** A prompt hill-climbed via SIMBA on one model encodes that model's quirks. Cascading to a weaker model mid-task yields lower-quality completions that then contaminate the trajectory dataset feeding the optimizer (see M3).
- **Determinism.** `temperature=0.0` is passed everywhere, but providers do not honor temperature 0 identically; race mode (line 276) between models of different speeds biases every result toward the *fastest*, typically the *weakest*, provider.

There is **no model-capability metadata** in the `Provider` dataclass (lines 184–194) — only `name`, `model`, `priority`. The design cannot reason about "can this provider actually do this call." Cascade across heterogeneous models is a research problem, not a ~500-line module.

---

### C4. "Independently hill-climbable" is false — the nodes are coupled

The TL;DR (line 11) and §M9 (line 1036) claim every node is "a DSPy module that can be independently hill-climbed." This is not true for the two most important nodes:

- **Worker metric depends on decomposer quality.** `worker_metric` (line 1065) scores a worker trajectory on success + tool-call efficiency. But whether a worker *can* succeed is largely determined by whether the decomposer handed it a coherent, self-contained subtask. A bad decomposition makes even a good worker look bad, and SIMBA will "fix" the worker prompt for failures that were actually the decomposer's fault.
- **Decomposer metric depends on worker execution.** `decomposer_metric` (line 1075) is `completed / total` over subtasks. Whether a subtask "completes" depends on worker competence. A bad worker makes a good decomposition look bad.
- **Reviewer metric depends on labeled ground truth** that nobody produces (see C5).

This is a coupled optimization, not a set of independent ones. SIMBA/GEPA optimize one module at a time with the others held fixed, so the optimum is a **moving target**: improving the worker changes what counts as a good decomposition, which changes the trajectories the worker is trained on next iteration. The data-flywheel diagram (lines 1123–1148) literally depicts this feedback loop ("Better results → Better training data") while the prose denies it by calling the nodes independent.

Non-stationarity of this kind is a known failure mode for bootstrap-style prompt optimization. The document does not acknowledge it, does not propose holding one node fixed while optimizing the other, and does not propose any decoupling metric. At minimum the claim should be retracted; realistically the design needs joint optimization or a held-out evaluation harness that scores each node on *fixed* reference tasks rather than on live co-adapted output.

---

### C5. The automatic metric for coding tasks does not exist in this design

SIMBA/GEPA require a metric function. The three metrics defined are inadequate:

- **`worker_metric`** (lines 1065–1073) rewards *fewer tool calls*. This is actively harmful: an agent that skips reading the file and writes a plausible-looking patch will score *higher* on "efficiency" than one that explores carefully. The metric optimizes for the opposite of code quality.
- **`decomposer_metric`** (lines 1075–1081) measures completion ratio only — `status == "done"`. Workers self-report "done"; nothing verifies the work is correct, complete, or non-regressive.
- **`reviewer_metric`** (lines 1083–1092) computes F1 over predicted vs. real bugs — but **where does `ground_truth["bugs"]` come from?** There is no bug-labeling pipeline in the design. This metric is unusable without human annotation, which collapses the "automatic optimization" flywheel.

"Did tests pass" is the obvious proxy, and it is necessary but not sufficient: most repos have weak coverage, tests can be made to pass trivially (no-op patches, `# noqa`, deleting failing tests — which the worker's `run_shell` tool fully permits), and passing tests says nothing about correctness on uncovered inputs, performance, readability, or whether the change actually satisfies the spec. The Merge Sequencer's own default `test_cmd` is `cargo test --lib 2>&1 | tail -5` (line 939) — piped through `tail`, so the exit code is `tail`'s, not `cargo`'s. **Tests are structurally incapable of failing this gate** (see M1).

Without a real metric, the "moat" (line 1036) is imaginary. The optimization harness will hill-climb toward whatever proxy is coded, however bad, and ship those prompts to production.

---

### C6. There is no "do not decompose" path — the orchestrator always splits

`TaskDecomposer.forward` (lines 816–818) unconditionally calls `self.decompose(...)`:

```python
def forward(self, spec: str, repo_context: str = "") -> list[SubTask]:
    pred = self.decompose(spec=spec, repo_context=repo_context)
    return pred.subtasks
```

There is no atomicity classifier, no single-subtask shortcut, no cost model for "is decomposition worth it." Every task — including trivially atomic ones ("rename this function", "fix this typo", "add this import") — is forced through an LLM decomposition step and then through parallel dispatch + sequential merge.

This is a real and expensive failure mode:

- **Over-decomposition** of coherent tasks produces workers that each see only a fragment of the design intent, yielding inconsistent APIs, duplicate logic, and integration conflicts at merge time.
- **Merge amplifies the cost.** Each unnecessary subtask pays for a worktree, a process spawn, a ReAct loop, a rebase, and a test run (M7). Decomposing a 5-minute task into 4 subtasks can turn it into a 20-minute task with a nonzero chance of merge conflict.
- **The reviewer loop can't recover.** `Orchestrator.execute` retries on `reject` (lines 881–884) but the retry body is literally `...` (unimplemented), so a bad decomposition just fails opaquely.

A competent orchestrator needs a "this is atomic, dispatch as-is" branch, ideally gated by a cheap classifier or a length/complexity heuristic. The design has neither.

---

## MODERATE ISSUES

### M1. The default test command is broken and makes the test gate a no-op

Line 939:

```python
self.test_cmd = test_cmd or "cargo test --lib 2>&1 | tail -5"
```

Because this is run via `shell=True` (line 963), the exit code returned is that of `tail`, which is virtually always 0. `test_output.returncode != 0` (line 968) will therefore almost never trigger, so the "tests failed → revert" safety net (lines 968–976) is dead code in the default configuration. This also poisons C5: the metric pipeline depends on test results that cannot fail.

**Fix:** drop the pipe (capture full output, then truncate in Python), or use `set -o pipefail`.

---

### M2. The worker tool set is inadequate for real coding work

The five tools (lines 661–696) are: `read_file`, `write_file`, `run_shell`, `git_op`, `grep_code`.

- **`write_file` overwrites whole files.** There is no `edit_file` / patch / find-replace tool. For any non-trivial file this means the agent must read, reconstruct, and rewrite the entire file — token-expensive, error-prone, and the dominant failure mode of early coding agents. Claude Code, Aider, and Codex all use structured edits (search-and-replace blocks, diff/patch, or line-range edits) for exactly this reason. Their absence here is a significant capability gap.
- **`run_shell` subsumes everything.** With `shell=True` and no allowlist (line 673), `run_shell` can do anything `git_op` and `grep_code` can and more, so the "five tools" framing is illusory — the agent effectively has one omnibus tool plus file I/O. This also means the worker can `rm -rf` the worktree, exfiltrate via `curl` (network is permitted by default in the init example, line 137), or rewrite `.git`. The M8 sandbox is marked P2 (line 119), so it ships after the worker.
- **`grep_code` is shell-injected and broken.** Line 689 interpolates `pattern` directly into a shell string (`f"grep -rn '{pattern}' {path}"`). A pattern containing `'` breaks the command; a malicious or careless pattern is arbitrary shell execution. Also the function has no `return` (see N1).
- **No structured search.** No symbol/AST search, no semantic search, no LSP integration. Real refactoring requires navigating call graphs, not grepping text.
- **BM25 tool search is claimed but absent.** §6 (line 1197) lists "BM25 tool search (Codex)" under "What We Copy," but the implementation has five flat tools and no retrieval layer. The tool-explosion problem the BM25 search is meant to solve is hand-waved away.

The ReAct signature is also mis-specified: `"task, context -> action"` (line 651) does not match DSPy's `ReAct`, which manages its own `thought`/`action`/`observation` cycle and expects a domain signature like `"question -> answer"`. As written, `ReAct(signature, tools=..., max_iters=...)` will not produce a usable agent loop.

---

### M3. The optimization flywheel is a coupled feedback loop with no stability guarantees

Beyond the independence claim (C4), the flywheel as drawn has no safeguards against the usual pathologies:

- **Reward hacking.** With `worker_metric` rewarding few tool calls and `decomposer_metric` rewarding "done" status, the stable attractor of this system is "decompose into trivial tasks, complete them with minimal exploration, declare done." The metrics *select for* shallow work.
- **Distribution shift.** Optimized prompts are trained on past trajectories and deployed on future tasks. There is no held-out evaluation set, no train/test split, no drift detection.
- **Model lock-in.** Prompts hill-climbed on DeepCode v4 Flash (the free optimization model, line 1137) are deployed on whatever production workers use. Cross-model prompt transfer is known to be unreliable (C3), and the design explicitly trains and deploys on different models.

A flywheel needs brakes: a held-out metric, a human-in-the-loop gate before deploying optimized prompts, and a rollback when production metrics degrade. None are present.

---

### M4. `ReAct` checkpointing callback does not exist in the DSPy API

Lines 741–746 define `on_step_end_callback` and reference `trajectory_state`, but the callback is **never attached** to the ReAct module. DSPy's `ReAct` does not expose a per-step callback hook of this shape. The entire crash-recovery-via-checkpoint mechanism (advertised as a Temporal-style feature in §2.1, line 38) rests on a hook that isn't wired up. Either a custom subclass of `ReAct` is required (not shown), or checkpointing silently never happens — meaning a worker crash restarts from turn 0, not from the last checkpoint, contradicting the design's central recovery claim.

---

### M5. FanOut's cache is per-instance and therefore nearly useless for its stated purpose

`self.cache` (line 212) is a plain dict on the `FanOut` instance. Workers are separate processes (§3.1), each with its own `FanOut`. There is no shared cache store. So:

- Within a single worker, a ReAct loop rarely emits byte-identical prompts (observations differ each turn), so cache hit rate is near zero.
- Across workers, the cache is not shared, so the "identical prompts hit cache instead of API" benefit (line 296) does not materialize for the multi-worker case that is the whole point of the harness.

The cache adds correctness risk (C1) for almost no performance benefit. It should either be removed, made explicitly opt-in, or backed by a shared store (e.g., the event log) — and only then for calls that are genuinely stateless.

---

### M6. Race mode can silently discard a superior result and has no exception hygiene

`_race` (lines 276–289):

- `asyncio.wait(..., FIRST_COMPLETED)` returns the first *finished* task, which may have finished by raising. `winner.result()` (line 287) will then re-raise, and the function fails — even though other providers might have succeeded milliseconds later. There is no `return_exceptions=True` and no scan of `done` for a non-exceptional result.
- Pending tasks are cancelled (line 285), but a cancelled in-flight HTTP request to a metered provider may still count against quota.
- The losing (cancelled) providers' partial work is discarded; if the winner is the weakest model (fastest = often smallest), production quality is biased downward.

---

## MINOR NOTES

### N1. Concrete code defects (syntax / runtime)

These are in shipped pseudocode that the document presents as the implementation spec:

| Line | Defect |
|---|---|
| 667 | `Path(path).write_content(content)` — `Path` has no `write_content`; should be `write_text`. |
| 692 | `grep_code` body ends with `result.stdout + result.stderr` — **no `return`**. Function always returns `None`. |
| 730 | `os.getpid()` — `os` is never imported in the worker script. |
| 761 | `{"type": " M5: error", ...}` — malformed type string, contains " M5: " prefix garbage. |
| 843 | `def __task_id_counter` — incomplete class attribute; invalid syntax. |
| 884 | Retry body is literal `...` — the entire reject/retry path is unimplemented. |
| 980 | `cwd=self.root` — attribute is `self.repo_root`; `self.root` does not exist. |
| 1077 | `r.get("status") "done"` — missing `==` operator; syntax error. |
| 1091 | `... if true_bugs polymorphism` — `polymorphism` is a stray token; syntax error. |
| 1116 | `f".c flywheel/data/optimized/{node_name}.json"` — space in path; should be `.cambium`. |
| 1010 | `def __ sandbox_command` — space after `__`, invalid identifier. |
| 1024 | `sys.executable` referenced in M8 but `sys` not imported there. |
| 118 | Duplicate `M7: Merge Sequencer` row (line 117 is correct; 118 has "test test gate" typo). |
| 1133, 1140 | Broken box-drawing characters (`┌` / `┏` instead of `┐`) in the flywheel diagram. |
| 738 | Stray comment fragment `#  standalone agent`. |

### N2. `SubTask.depends_on` default and DAG handling

`depends_on: list[str] = None` (line 806) is `Optional` but never annotated as such, and the orchestrator's dependency-resolution loop (lines 857–872) has no cycle detection. A decomposition that produces cyclic `depends_on` will loop forever in the `while ready:` block. There is also no handling for a subtask whose dependency *failed* — it will sit in `pending` permanently.

### N3. `_try_provider` constructs a fresh `dspy.LM` per call

Line 233 instantiates `dspy.LM(...)` on every invocation. `dspy.LM` construction is not free (it resolves provider backends via LiteLLM). Workers are separate processes so the global-state concern (`dspy.configure`) is contained, but the per-call construction is wasteful and should be cached per provider.

### N4. Worker reads init via blocking `stdin.readline()` inside `async main()`

Line 721 uses `sys.stdin.readline()` (blocking) within `async def main()`. It works because it's the first thing the worker does, but it betrays that the worker is fundamentally synchronous despite the `asyncio.run` wrapper. The `emit`/`heartbeat`/`checkpoint` helpers are synchronous too. This isn't broken, but the async framing implies concurrency the worker doesn't actually use.

### N5. `collect_commits` assumes ≥5 prior commits

Line 767 runs `git log --oneline HEAD~5..HEAD`. On a fresh worktree with fewer than 5 commits in the range this will error, and the `except` in `main` (line 760) will report a generic failure rather than the actual result. Use `git log --oneline -5 --skip=...` or bound-check.

### N6. "Zero external runtime dependencies" claim is inaccurate

Line 17 claims "Zero external runtime dependencies beyond Python stdlib + DSPy + git." The design also depends on LiteLLM (line 112, line 298), the sandbox tooling (M8), and transitively on whatever DSPy pulls in. The claim should be qualified.

---

## VERDICT

**Not build-ready as an LLM/agent design.** The process-supervision layer (M1/M3/M4) is thoughtfully derived from Erlang/OTP and is the strongest part of the document. The LLM/agent layer — which is the part that actually has to write correct code — has several flaws that are individually fatal to the stated value proposition:

1. **The cache (C1) will silently serve wrong codegen** because it ignores repository state, and nothing in the design flags this.
2. **The cascade (C2) does not fall back across models** due to an `if provider.model != model: continue` guard that makes the headline multi-provider feature a no-op.
3. **The optimization "moat" (C4/C5) rests on (a) a false independence claim and (b) metrics that are either gameable (reward shallow work), unusable (require nonexistent ground-truth labels), or structurally broken (the test gate can't fail — M1).** Without a real automatic metric, SIMBA/GEPA will optimize for the wrong thing and ship it.
4. **The tool surface (M2) lacks any structured editing primitive**, making the agent strictly weaker than every production coding agent it is compared against in §6, and the BM25 tool search it claims to inherit from Codex is absent.
5. **The orchestrator has no atomicity escape hatch (C6)** and an unimplemented retry loop.

**Recommendation:** before implementation, the authors should (a) remove or re-key the FanOut cache against world state, (b) fix the cascade to actually traverse models and document the capability tradeoffs, (c) design a real coding metric (likely: tests-as-floor + LLM-judge / human-graded held-out set + behavioral checks against reward hacking), (d) add a structured-edit tool and a tool-retrieval layer, (e) add an atomic-task shortcut in the orchestrator, and (f) treat the per-node hill-climbing claim as a hypothesis to validate rather than a foundation — the nodes are coupled and the flywheel needs brakes.

The Erlang-inspired scaffolding is good. The part that does the coding is not yet designed.
