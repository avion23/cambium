# Harness audit — 2026-09-04, continued 2026-09-05

The first record describes the September 4 revision. The continuation below
supersedes its runtime-status claims; earlier measurements remain historical.

## Scope and evidence

Started in an isolated worktree from fetched `origin/dev` (`fad5687`). Integrated
committed parallel navigation/prompt work (`cb42036`, `ecf8b1d`) and the later
architecture cleanup (`0509552`). Other checkouts' uncommitted work was not used
as an implementation or overwritten.

This is a test record, not a claim of a measured global performance improvement.
Real-provider results below used the configured `zai / glm-5.3` lane. Local PTY,
process, and scripted-provider cases are identified separately.

## Reproductions and changes

| Observed problem | Change and regression evidence |
| --- | --- |
| A total-token count could report 50,000 generated tokens/s for a call with only 20 output tokens in two seconds | Output-only rates across routing, rendering, and observability; missing output counts stay unknown. `test_resource_projection.py` |
| Quota rendering opened writable account storage and replay could show unrelated current state | Reduce per-provider/window event snapshots; explicit read-only account inspection. `test_resource_projection.py` |
| Decaying sample counts and totals separately changed measured provider speed | Decay evidence weight while preserving its mean. `test_routing_throughput.py` |
| Repeated task/generation/model-turn counters could resolve history from the wrong interactive turn | Scoped `@turn-NNNN` references; reject ambiguous unscoped references. `test_branch_history.py` |
| Navigation/history libraries were not active tools | Wire `repo_query` and `branch_history` through schema, dispatch, batching, prompt and real frontend paths. `test_navigation_tools.py`, `test_live_frontends.py` |
| The full rail lacked resource information and retained empty live-detail rows after completion | Compact resource/quota rows; collapse terminal-state detail. `test_resource_projection.py`, TUI row tests |
| Resize during typing caused a native libedit segfault | Deliver resize signals to the native input owner. PTY resize/typing stress |
| Rendering read libedit's mutable buffer from another thread and could abort in string conversion | No cross-thread native buffer access; live cells update without destructive input repaint. PTY stress and owner-access regression |
| Invalid model responses after the last valid tool call were missing from durable history | Checkpoint invalid responses and repair feedback on the existing path. `test_worker_agent_loop.py` |
| Generic action-repair feedback repeated a long list of action shapes | Short feedback names the parse error and JSON escaping; parser and retry bound unchanged |
| DSPy classifier construction mutated class inheritance at runtime | Ordinary optional DSPy base, lazy stable class exports, independent predictors and save/load tests |
| A process-cleanup fixture observed a newly created but empty PID file | Publish the fixture's PID file atomically; retain actual process cleanup assertions |

The native-input fix has an explicit tradeoff: a complete geometry repaint can
wait until the current edit finishes. Conversation/result/status cells continue
to update, and active cancellation/inspection work. Do not describe this as a
fully rewritten terminal editor or immediate full repaint in every condition.

## Executed checks

Using the checkout's source and the installed Python 3.14 environment:

```sh
PYTHONPATH=src python -m pytest -o addopts='' -n 2 -m 'not acceptance' -q
PYTHONPATH=src python -m pytest -o addopts='' -m acceptance -q -rs tests/acceptance/test_live_coding_gate.py tests/acceptance/test_live_frontends.py tests/acceptance/test_live_tui_coding.py
ruff check src tests
git diff --check
```

The final non-acceptance run passed **1,870 tests**, with **one skipped**. This
includes slow process and PTY tests, not only string rendering. The final live
run passed **all five** selected real-provider tests: coding publication,
impossible-task non-publication, CLI navigation/code change, TUI coding/history
retrieval, and a two-turn coding/read-only continuation without an empty commit.
Lint and whitespace checks passed.

Earlier runs were not uniformly successful. They exposed both native terminal
crashes, an import-boundary regression during the DSPy refactor, a stale prompt
assertion, a fixture publication race, and one real CLI task that exhausted its
invalid-action allowance. Those observations drove the changes above. The
subsequent passing live run does not establish a population-wide prompt success
rate or prove that a small feedback change alone caused its success.

The module tests passed 52 decomposition and 58 review cases. An actual DSPy
classifier call through Cambium's adapter returned `do_not_decompose` for a
single-comment correction, using one request and 422 tokens. This verifies the
adapter/inference path, not an optimization gain.

## What is deliberately not claimed

The coding worker does not load DSPy optimizer artifacts. No held-out coding
prompt improvement or automatic deployment was demonstrated. Finite token/call
budgets matter even where estimated incremental cash cost is zero.

The complete model-facing SituationFrame, unified operator/model reducer,
evidence-linked WorkLedger, richer ResultCapsule, and weekly-capacity-aware
useful-work ranking remain open integration work. Existing BranchState/CLI
inspection, summaries, checkpoints, joins, provider accounting, and navigation
are their starting points, not evidence that every proposed layer is complete.

At that revision, `implementation-plan.md` listed remaining work; that duplicate
plan has since been removed. Current [runtime architecture](../architecture/architecture.md),
[provider accounting](../architecture/provider-routing.md),
[terminal contract](../architecture/terminal-interface.md), and
[offline optimization](../architecture/optimization.md) own the implementation
claims. This record does not introduce a new runtime gate or policy layer.

## September 5 continuation: prompt deployment, delegation and simplification

Continued the isolated `gepa-delegation-20260905` worktree rather than editing
other checkouts. The self-fix for `render_tokens_per_s(None)` came from a real
Cambium task and was already committed. The parallel SituationFrame checkout
was not incorporated.

### What changed

Coding and summary policy are now separate plain-text components. The offline
GEPA runner executes normal Cambium rollouts, checks accepted artifacts and
atomically replaces an improved policy for new sessions. Interactive sessions
pin their policy; `/new` picks up replacement text. This is not a DSPy import or
classification call on every worker turn. The actual hill climb remains an
operator-run experiment; no general quality gain is claimed.

The normal model decides whether to delegate. The worker fills context/placement
from the current delegate batch; the supervisor supplies child workspace and
execution settings. A single child defaults to trunk/inherit; independent
siblings default to semantic/spread. Placement is a preference subject to actual
provider availability and call-time fallback.

Transcript-driven corrections:

| Observation | Change |
| --- | --- |
| Valid name/arguments requests were rejected just for missing the redundant type tag | Accept the unambiguous tool shape; validate the actual tool and arguments |
| Repeated malformed batch brackets received the same generic escaping advice | One repair path with a single-tool example; no separate finish-only repair policy |
| Arbitrary successful shell commands counted as verification, but valid completion could be blocked | Remove the ritual shell-success finish gate; retain observations and external artifact checks |
| Budget pressure discarded useful tool calls and could fabricate success without a finish verdict | Keep tools available within the bounded run; no terminal verdict means incomplete |
| One fallback notice was appended again on every subsequent model call | Append it only on provider change |
| A provider quota failure was visible in usage but hidden behind a generic final exception | Include the provider and typed failure category in the final reason |
| A Python-version linter warning was counted as an extra source error beside valid findings | Do not turn warning stderr into another error when Ruff returned ordinary diagnostics |
| Suspended parents looked terminal and the rail reserved empty detail rows | Keep suspension nonterminal; compact provider/status rows for each lane and three CAST context rows |
| PTY helper could pass a negative timeout after two clock reads | Compute the remaining wait once; keep the real resize/typing stress |

Removed the newly added JSON-mode feature flag and its wiring. Earlier probes
showed provider-side text corruption under that mode; ordinary action JSON does
not need it. Removed source-catalogue pinning tests, repeated finish-gate variants
and duplicated live-TUI setup. The consolidated real frontend test checks code,
read-only continuation, history retrieval, resizing and absence of empty commits.
The resize stress uses 120 edits/resizes in one process instead of three repeated
startups. Checkpoint identity, child lifetime, actual Git effects and malformed
argument checks remain covered.

### Executed evidence and limitations

The non-acceptance suite, including slow process and PTY tests, passed 1,876 tests
with one skipped in `.cambium/continue-suite-03`. A later repair-feedback change
passed all 67 affected loop/budget tests; one intervening full run exposed only
an obsolete assertion pinning the old feedback wording, which was corrected.
Lint and whitespace checks passed. After merging the latest `origin/main`, the
final integration run in `.cambium/continue-integration` passed **1,876 tests,
one skipped**, in 103.55 seconds. This run includes the consolidated PTY stress
and the final shared repair path; Ruff and the complete change's whitespace
check passed as well.

Real-provider results were mixed, and the failures are retained:

* `.cambium/continue-live-01`: both CLI and TUI frontend tasks passed. The TUI
  transcript exposed the missing-type repair calls that drove the parser change.
* `.cambium/continue-live-final`: CLI passed; TUI stopped on three malformed batch
  objects. Both were assigned Codex but served by ZAI after fallback.
* `.cambium/continue-live-repair`: the consolidated TUI task passed after repair
  feedback was simplified, including exact historical retrieval and a read-only
  turn without an empty commit. One passing rerun is not a success-rate estimate.
* `.cambium/continue-parallel-01`: the model admitted two semantic/spread children,
  but both ultimately used ZAI. The task failed after 254.813 seconds, 35 calls
  and 177,747 reported tokens. This is not successful multi-provider evidence.
* `.cambium/continue-parallel-02`: a subsequent attempt failed on a ZAI HTTP 429
  request-rate limit before child execution. No artifact was published.

The larger automatic multi-provider task is still not a demonstrated reliable
completion path under these provider conditions. Earlier runs did execute
children on different providers and a single blocking child on the parent trunk,
but do not turn those observations into a performance or reliability guarantee.

CAST documentation now distinguishes ordinary immutable semantic-delta folds
from deterministic K0 rollover and explicitly states K0's text-identity/open-item
limitations. The TUI retains its native-input geometry-refresh tradeoff. Future
SituationFrame/WorkLedger/resource-ranking proposals remain in the open plan;
this continuation does not claim to have completed them.

### September 5 isolated integration: live lanes and CAST precision

Further edits were isolated in `cast-tui-final-20260905` after concurrent writes
were detected in the earlier worktree. Its changes were captured without changing
that checkout's HEAD, index or files, then integrated with `origin/main` at
`f8de134`. The separate SituationFrame checkout remains untouched.

The rail now retains the selected parent and live children ahead of completed
history, collapses detail before hiding lanes, and reports the omitted count.
The duplicate footer usage row is hidden by default. CAST documentation now
separates checkpoint epoch numbering from prefix replacement and explains fold
break-even costs without claiming an implemented economic optimizer.

The remaining finish-only parse-repair path and duplicate action normalization
were removed. Recorded calls retain their natural name-before-arguments order.
The module-deletion fixture no longer recursively copies top-level runtime
sessions into its scratch repository. Behavioral checks replace assertions
about fixed footer row counts or exact repair prose.

The integrated tree at `8c76708` passed **1,879 non-acceptance tests, one skipped**
in 96.26 seconds, including real-process/PTY cases. Ruff and whitespace checks
passed. Before integration, the real CLI task passed in 27.94 seconds. One TUI
trial failed after three repeated malformed batches; after feedback and call
serialization changes, the two-turn TUI coding/history task passed in 34.44
seconds with **11 calls and 34,887 reported tokens**. Its six tool calls succeeded,
the accepted code passed external assertions, and the read-only turn created no
empty commit. Captured terminal output was inspected, not just the model summary.
An earlier pinned-provider TUI run had instead reached verification and then
failed on ZAI quota; it was not a rendering failure. These individual trials do
not establish a general prompt improvement or reliable multi-provider speedup.

## Selected runtime follow-up — September 5

Started at fetched `origin/main` `20bf89c` in isolated worktree
`cambium-19b38f89`, branch `streamline-runtime-20260905`. Integrated the subsequently
published `d17db3b` SituationFrame work without modifying its parallel checkout.
Kept its bounded local frame and removed the duplicate single-tool execution
branch rather than restoring two implementations. Kept the parallel deletion
of `implementation-plan.md`; contracts and remaining limits live with their owners.

### Changes checked through their consumers

- Codex Responses function-call items and argument completion events now reach
  normal native-tool dispatch. Coding prompts no longer repeat the complete
  summary schema/policy; the summary control carries it when needed.
- Finish and exact/fresh delegation checkpoint without a mandatory summary.
  Semantic batches fold once. One execution path owns tool observations,
  cancellation and checkpointing; identical batch checkpoints are not rewritten
  once per tool.
- CAST uses existing D/F IDs plus O/V IDs for obligation closure and stale-check
  invalidation. Replacements follow invalidations in a delta; previous entries
  remain immutable. Manual K0 retains the raw tail and resets the prompt baseline.
- Admission consumes observed quota/reset times and persisted Retry-After,
  respects configured concurrency, and waits for capacity without occupying a
  worker-process slot. Successful fallback moves the existing reservation once.
- POSIX terminal input is owned by the event loop. PTY tests exercise immediate
  resize, pasted newlines, F6 focus with an unfinished draft, cancellation and
  terminal restoration. The input viewport is not a full multirow editor.
- Model `inspect_state`, CLI `inspect-state` and TUI `/inspect` use one durable
  BranchState reader. The worker-local SituationFrame has a different source
  watermark; it is not falsely labelled as an event-store snapshot.
- Eight historic/derived benchmark cases record provenance and keep task families
  within one split. Interactive cases use the real session path, accepted Git
  checks, history retrieval, reconnect and K0. Reports count malformed actions,
  summary requests, failed provider attempts and actual serving providers.

### Failures retained

The first navigation/history benchmark failed before execution because a
re-resolved automatic configuration treated credential environment-variable
names as provider names. The resolver now maps them correctly and preserves the
original provider/model pool across reconnects, including when other providers
become available later. An unknown requested credential still fails explicitly.

The first corrected-history trial exhausted its 75-second turn budget after
five malformed model responses. A longer run completed the artifact checks but
failed the required `inspect_state` trace: a running task's absent result was
treated as a mapping. The reader now represents that pending result without an
exception. The following trial passed, with one semantic summary and one K0.

The merge check exposed obsolete terminal-summary accounting and a checkpoint
fixture duplicating the producer's schema. The fixture now uses the real
checkpoint writer; the assertions still check accepted events and exact persisted
content. No malformed provider response or incomplete task is converted to success.

### Executed checks

On the final merged runtime:

```sh
PYTHONPATH=src python -m pytest -o addopts='' -n 2 -m 'not acceptance' -q
PYTHONPATH=src python -m pytest -o addopts='' -m acceptance tests/acceptance/test_live_frontends.py -q
PYTHONPATH=src python -m cambium optimize prompts --optimizer zero --case corrected-history --max-turns 16 --max-wall-s 90 --max-calls 45 --max-tokens 250000 --budget-usd 2 --output .cambium/integrated-corrected-history
```

The non-acceptance suite passed **1,906 tests with one skipped** (118.66 seconds).
Both real CLI/TUI frontend tests passed (38.07 seconds). The final three-turn
correction/child/K0/reconnect benchmark passed in **93.718 seconds**, using
**22 calls and 102,243 reported tokens**, including one summary, one K0 rollover,
four malformed actions and no tool failures. Only the requested file changed;
the final read-only turn left its accepted head unchanged.

Earlier in this work, navigation/history passed with nine calls and 25,069
reported tokens; the frozen Cambium self-fix passed with four calls and 20,680
tokens; a blocking exact child passed with six calls and 13,699 tokens. The latter
two made **zero summary calls**. These are individual development runs, not a
controlled speed comparison against the prior release.

Reports remain in the worktree's `.cambium` benchmark directories; early failed
trials are also under `/tmp/cambium-selected-*`. The final continuation run was
served by ZAI, not a demonstrated simultaneous multi-provider speedup. Quota and
lane behavior have deterministic runtime regressions; population-wide resource
efficiency and prompt-quality gains still require the operator's GEPA experiment
and fresh evaluation cases. No hill-climbing result was promoted in this work.

## Reconnect finalization — September 5

Reconnected after the connector restart at `origin/main` `be6190e`. No local or
remote `dev` ref existed. Preserved the pending `finalize-cast-runtime-20260905`
checkout, then isolated a snapshot in `cambium-ece9baf5` when concurrent edits
appeared. Runtime changes were committed as `f08ab8e` and `b06ac1a`; unrelated
working trees were not reset or cleaned.

The historical correction transcript showed `tool inspect_state ok=True` with
its body missing. Turn-checkpoint cleanup removed every embedded SituationFrame,
including an explicit tool observation. Cleanup now recognizes only the generated
loop-state message. A real checkpoint regression retains the explicit read while
removing the transient frame. It also exposed `uncached_token_pressure` being
redacted as a credential; that known metric now uses the existing metadata rule.

Model and operator inspection now share the bounded SituationFrame renderer
rather than separate JSON/text projections. It includes results and provider
identity, keeps active children visible ahead of completed ones, and names real
history operations for omitted detail instead of nonexistent inspection arguments.

Requirement-constrained routing now consumes the same quota/cooldown evidence as
ordinary assignment. An additional regression showed that even expired or
unrelated windows switched the quality ranking to resource order. Only current
observations for eligible providers now enable that preference. Existing quality
ordering remains the tie-breaker; no synthetic weekly allowance was introduced.

One benchmark wall deadline now covers follow-ups, children and reconnects.
Timed-out rollouts retain reports and check accepted code, never salvage or an
uncommitted worker tree. Paste handling inserts chunks rather than repeatedly
copying the entire draft per character. PTY coverage retains Unicode, split
framing and large multiline paste; the obsolete libedit-only fixture and a
duplicated GEPA exercise were removed.

### Final checks

On code commit `b06ac1a`, the complete non-acceptance suite passed **1,910 tests,
one skipped**, in **104.32 seconds**. It includes process, checkpoint/replay,
provider-routing, real PTY input/resize/focus/paste and cancellation scenarios.
Ruff and the whitespace check passed. Both real-provider CLI/TUI frontend tests
passed in **48.36 seconds** after the routing correction.

Two fresh historical rollouts ran through the public optimizer baseline command:

| Case | Result | Seconds | Calls | Reported tokens | Summary calls |
| --- | --- | --- | --- | --- | --- |
| Frozen Cambium self-fix | Pass; only `src/cambium/render.py` changed | 35.762 | 10 | 106,920 | 0 |
| Blocking trunk child | Pass; no code change; parent and child on ZAI | 25.551 | 6 | 22,919 | 0 |

Reports are under `.cambium/final-small-tasks/` in the isolated worktree. These
are individual runs, not a speed or prompt-quality comparison. The self-fix had
three malformed actions and one failed exact-text edit; the blocking-child run
had one malformed action. The plain-text action path still needs evaluation.

The preserved pre-finalization three-turn correction run passed artifact and
trace checks in 139.396 seconds, with 19 calls, 91,868 tokens, one summary and
one K0. Its empty inspection observation prompted the persistence regression
above: a passing artifact check did not prove intact history. A fresh correction
benchmark launch was blocked by the connector/tool layer, so it is not reported
as another live pass. No simultaneous multi-provider speedup is claimed.

The GEPA dry run resolved all eight historical/derived cases with automatic
candidate deployment enabled. No optimizer search ran and no prompt artifact
was promoted. These cases are a development regression corpus; reserve new
cases before claiming generalization from an eventual hill climb.

### Parallel publication integrated at the final boundary

`origin/main` advanced to `ed095ca` during publication. Its overlapping fixes and
additional redaction regression were merged normally. The regression checks that
registered credentials still redact even under the benign pressure-metric key.
Repeated metadata and documentation were consolidated rather than duplicated.
The merged code at `ba95ad2` passed 1,910 tests with one skipped in 107.47 seconds;
Ruff and whitespace checks passed. A loopback OAuth fixture printed a broken-pipe
diagnostic during that successful suite; no test failed.

The parallel audit recorded 1,910 non-acceptance passes and one skip in 104.72
seconds, plus two live frontend passes in 51.84 seconds. Its earlier ZAI
self-fix and blocking-child reports, under `.cambium/finalize-self-exact` in
`cambium-8e3fdca5`, recorded respectively 21.713 seconds/5 calls/47,182 tokens and
20.194 seconds/7 calls/26,226 tokens, both with zero summaries. Its correction
report is the same 139.396-second run discussed above, not an additional trial.
Six malformed actions occurred across those three recorded runs.

The earlier `.cambium/finalize-corrected` attempt ended after the connector wait
with two completed turns but no final result. That interrupted attempt is not a
pass; it motivated the whole-rollout deadline. Neither set of live runs proves
simultaneous multi-provider speedup or long-run quota efficiency.
