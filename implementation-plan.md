# Open work

Current contracts have one owner: [runtime](docs/architecture/architecture.md),
[CAST](docs/architecture/context-engine.md),
[delegation](docs/architecture/context-branches.md),
[provider resources](docs/architecture/provider-routing.md),
[terminal interaction](docs/architecture/terminal-interface.md) and
[prompt experiments](docs/architecture/optimization.md).

## Measure completion rather than adding control layers

The model chooses decomposition in its ordinary action call. The supervisor
supplies child workspaces, provider placement and capacity waiting. Native
Codex tool output is consumed when the provider uses it, but text-only providers
can still emit malformed JSON. Fix demonstrated transport defects; do not guess
ambiguous actions or add a paid classifier before every tool.

The historical benchmark contains eight cases, with source provenance and
explicitly derived continuation variants. Run the prepared GEPA experiment and
extend it with fresh cases before claiming general improvement. Cases used to
repair the runtime are regression evidence, not independent final evaluation.
Keep failures and the cost of retries, summaries and joins in the comparison.

## Context quality

Ordinary completion and exact/fresh delegation save checkpoints without forced
summaries. Semantic delegation and working-set thresholds still fold new raw
evidence. K0 supports explicit replacement, obligation closure and stale-check
invalidation through existing semantic entries. It does not infer truth or
completion from arbitrary prose. Reproduce an actual lost obligation before
adding a richer WorkLedger or ResultCapsule.

Model, CLI and TUI inspection now share the recorded BranchState reader. A
mandatory per-turn SituationFrame and full knowledge projection remain separate
proposals. Preserve the unfinished parallel checkout; do not absorb it merely
to declare the diagram complete.

## Resource measurements

Known quota windows, reset times, Retry-After and configured concurrent capacity
now affect admission. Unknown allowances still use balancing heuristics, not
invented weekly entitlement. Compare repeated accepted tasks/hour and quota per
accepted task across providers; no optimal or generally faster ranking has been
demonstrated. Keep task assignment distinct from the provider that actually
served a call after fallback.

## Terminal usability

POSIX input is event-loop-owned; resize, paste and F6 focus preserve its draft.
The draft uses a horizontal viewport with newline markers, not a full multirow
editor. The conversation remains a combined transcript rather than independent
per-child transcript tabs. Non-POSIX input still uses the line-reader path and
needs separate platform verification.

## Change discipline

Fix the owning path, retain a useful regression, run affected checks, and commit
and integrate the result. Remove repeated fixtures and implementation pinning,
not meaningful effect checks. Keep source and operator documentation accurate;
do not replace useful implementation with gates, receipts or another scheduler.
