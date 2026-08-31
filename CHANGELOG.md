# Changelog

All notable changes to Cambium will be documented in this file.

The format is based on [Keep a Changelog], and this project adheres to
[Semantic Versioning]. The release version and channel are intentionally left
unselected.

## [Unreleased]

### Added

- **Persistent TUI cockpit.** Reworked the terminal UI into a persistent,
  full-session operator cockpit with streamed Markdown, active-turn steering
  and queued input, `/fork`, `/branches`, `/compact`, and `/model` commands,
  and native primary-buffer scrollback.
- **CAST economics and rollover.** Added provider cache-capability, pricing,
  and quota-aware CAST economics, together with immutable semantic K0 rollover
  for bounded summary trunks.
- **Bounded CAST context policy.** Added `src/cambium/context_policy.py` and
  its `CastPolicy` bounds for CAST segments, trunk tokens, and rollover
  savings, with transactional fork joins that preserve the correct context
  epoch.
- **Join-safe merge envelopes.** Added parent/child join-invariant enforcement
  (`post_join_parent_HEAD == accepted_integration_HEAD`) and structured
  `merge_conflict`/`join_invariant_failed` outcomes instead of silent parent
  overwrite.
- **Opt-in conflict resolver.** Added a bounded resolver-child path whose
  publication is gated by a fresh parent-join check.
- **Navigation and execution tools.** Added structured `search_symbols`,
  `find_references`, `read_symbol`, and `query_lsp` tools; `run_python` uses a
  separate `python` permission key rather than inheriting `shell`.
- **Versioned prompt constants.** Added `src/cambium/prompts.py` with
  `PROMPTS_VERSION`, `CODING_AGENT`, and `SEMANTIC_SUMMARIZER` constants.
- **GEPA optimization stage.** Added a budgeted, held-out train/validation
  GEPA stage with a forward adapter and reportable optimization counters.
- **Trajectory extraction pipeline.** Added read-only, review-gated pi-session
  and OpenCode trajectory extraction with provenance/redaction metadata. The
  reviewed `train_queue_v2.jsonl` contains 34 records (24 train, 10 val).
- **Saved-program evaluation.** Added `cambium optimize eval` to score fresh
  or saved DSPy program state across train, eval, and canary splits, with
  explicit dataset loading and JSON reports.
- **Should-review optimization.** Added the second trainable DSPy decision
  module for `should_review` and generalized optimizer label and metric
  resolution beyond the example module.
- **Codex activation guide.** Added operator documentation for Codex OAuth
  login/import, credential eligibility, verification, acceptance, and rollback.
- **Provider lease semantics guide.** Added architecture documentation for
  sticky provider/model leases, terminal-death release, and transient quota
  and timeout behavior.

### Changed

- **Fresh interactive defaults.** REPL prompts now use fresh one-shot leaves by
  default, while TUI starts a fresh interactive root unless
  `-c [SESSION]`/`--continue [SESSION]` explicitly reconnects to the newest or
  a named root.
- **Two-pane TUI cockpit.** TTY rendering now places the conversation above a
  live provider/model, turn, token, cost, agent, tool-error, and checkpoint
  status pane, with Markdown rendering and `WAITING`/`STREAMING`/`IDLE`
  activity states.
- **Completion repaint.** The TUI forces and flushes the final frame when a
  turn completes, even while input is pending.
- **Interactive event timeline.** `supervisor.read_events` and `monitor` now
  aggregate per-turn event stores into one session-level timeline.
- **Import-budget guard.** Fresh-process import checks now use repeated probes
  to reject contention noise while retaining the provider-SDK lazy-import
  guard.
- **403 classification.** Differentiated HTTP 403 responses into WAF/network,
  credential, quota/billing, model-entitlement, and policy/content-refusal
  outcomes; an unlabelled 403 remains fail-closed as authentication.
- **Provider-aware wall budgets.** Interactive wall budgets now prefer
  explicit configuration, then provider throughput hints or measured branch
  output, with a safety factor. A fresh interactive session with no evidence
  receives the 1,800-second fallback budget.
- **Provider-config quarantine boot.** Invalid individual provider entries are
  recorded in an atomic `<config>.quarantine` sidecar while valid entries keep
  loading; an all-quarantined configuration fails closed with the sidecar path.
- **Terminal-death fallback.** Provider routing now falls back only after
  terminal provider-death evidence and carries `fell_back_from` through result
  metadata and rendering.
- **Test execution.** Enabled pytest-xdist by default with `-n auto` and a
  timing-safe load group, and removed avoidable scenario waits, reducing the
  targeted scenario run from roughly 108 seconds to roughly 40 seconds.
- **GEPA CLI exposure.** The unified `cambium optimize` command now accepts
  the `gepa` optimizer choice.
- **Interactive model controls.** `/model` now lists eligible,
  credential-ready provider/model targets and persists provider/model
  selection; a submitted `q` exits without creating a turn.
- **Public-release readiness.** Reworked the README around installation,
  quickstart, documentation, and license status, and made `doctor` derive its
  Python floor from project or installed package metadata.
- **Scenario timing margins.** Widened diffundo and provider-storm timing
  margins so xdist contention does not obscure retry, cooldown, and wall-budget
  assertions.
- **Profiling baseline refresh.** Refreshed the profiling results and added
  historical comparisons and hot-path probes while documenting host-load
  confounding.

### Fixed

- **Width-safe cockpit wrapping.** TUI conversation and status content now
  wraps within the terminal pane width, including long words and wide text.
- **Collapsed tool errors.** Routine failed tool events now render as one
  per-turn counter instead of repeating full error details.
- **Pinned-provider sibling fallback.** A terminally dead pinned provider now
  releases its matching lease before the authorized sibling pool is searched,
  while retaining fallback provenance.
- **SQLite resilience.** Hardened SQLite WAL persistence with a single writer,
  bounded busy/checkpoint retries, and explicit disk-full error propagation
  for event append/checkpoint and quota transactions; failed writes are not
  acknowledged as durable.
- **Orphan cleanup.** Reclaimed worktrees orphaned by hard crashes and covered
  the cleanup guarantee with soak scenarios.
- **Ruff corruption guard.** Added a syntax-hygiene guard for corrupted
  multiple-exception handlers and pinned development Ruff below 0.15 after the
  formatter regression was reproduced.
- **Terminal-death lease routing.** Prevented terminally dead incumbent lanes
  from starving healthy siblings, released matching provider leases before
  fallback, and preserved the bound incumbent as fallback provenance.
- **Summary-provider fallback.** Authorized model substitution during semantic
  summary flushes, preserving fallback provenance instead of failing the
  compaction path when the pinned provider dies.
- **Summary degradation.** Kept model-owned summary recovery bounded for
  oversized, surrogate-containing, and deeply nested values by coercing safely,
  truncating text, and trimming lower-priority lists within byte caps.
- **Pi-session extraction redaction.** Strengthened the training-data boundary
  against base64-like, Unicode-confusable, entity-encoded, zero-width, and
  whitespace-split credential forms.
- **Provider quarantine hardening.** Bounded and sanitized quarantine records
  and sidecars, redacted secret-shaped values, refused symlinked paths, and
  handled deep or invalidly encoded entries safely.

### Removed

- Removed the repository's failing CI workflows; the current workflow set under
  `.github/workflows/` was subsequently reintroduced.

<!--
Evidence trail for the unreleased entries (all hashes are commits reachable
from origin/main):

- Fresh interactive defaults and `-c`/`--continue`: 804ef4f, 0e18743, 09a3e84,
  43598b6.
- Two-pane TUI cockpit, Markdown, and activity state: 94ebcad, 7e248c5,
  f262585, fc941db, 07aac51.
- Completion repaint: 965dd26, 0ae4364, 083b310.
- Interactive event aggregation for `read_events` and `monitor`: 7ec6026,
  2939049, 9eb1492.
- Fresh-process import-budget guard: 38b8bb3, e1ad641, e33f1d7.
- Width-safe TUI wrapping: cefb3ac, 74132c8.
- Per-turn tool-error collapse: cefb3ac, 9c29464.
- Pinned-provider sibling fallback: 054408d, e3a969e, 37a1a99, ca8cba8.
- Persistent TUI cockpit: adf937c, 3789458, 3e45659, 16866b4, 545e239.
- CAST economics and K0 rollover: e63d369, 14a2482, d2c3432.
- Bounded CAST policy and transactional joins: 1596385, 6f608f7.
- Join invariant and structured envelopes: 968709d.
- Opt-in resolver child: d5b15a0, d21c9ee, 7c5fe78, 1e49d26.
- Navigation tools and the python permission: 725693e, 25be295.
- Versioned prompt constants: 7862278.
- GEPA stage: ac9a1d3, edc1e7b, fb64502, f7bf23b.
- pi/OpenCode extraction and reviewed queue: 6077091, 0317b55, c4e43fe,
  199bd1d, d9790a4, bcaff76, f78eced.
- Differentiated 403 classifier: 1397f2b.
- Throughput-aware and 1,800-second interactive budgets: 3313bcb, 57c579b,
  b1d9a69.
- Provider-config quarantine boot: b5fecb6, 11d0f37.
- Terminal-death fallback and provenance: 091ed9c, 8afd289.
- pytest-xdist and scenario wait reductions: 8331a56, 52b2f82, 2f890cd,
  287d83d, 951ce83, b184bfb, 7c66d24, 5bbe728.
- SQLite WAL, busy retry, and disk-full handling: 824dfe7, 6036817, 7f3f507,
  87687b9.
- Orphaned-worktree reclamation: 558e15a, 53f474f.
- Ruff corruption guard and version cap: bbf9034, 7d24a15, ffa1f15.
- Removed CI workflows: e6a40b5.
- Saved-program evaluation and explicit datasets: a3e130e, 4fff27e, 5bb4003,
  a89c4f1.
- should_review DSPy module and generic optimizer labels: 8844b2d, 7606da3,
  af0b0e4, e0112a2.
- Codex activation guide: e05b9eb, b35916a.
- Provider lease semantics guide: eeaf295, 634ef3d.
- GEPA CLI exposure: 1f850fb, 57e4cef.
- TUI model listing/switching and q-exit: 41aa98e, ca740f5, d176418.
- README publish readiness and dynamic doctor Python floor: bf3b450, 3f2b8fc.
- Storm/diffundo timing-margin widenings: 523359e, 6d8d30f, 3c99244,
  777cc7f.
- Profiling refresh: 7472df5, f56a210.
- Terminal-death starvation and lease release: 83e0892, a7670f9, 05b28e9,
  e3a969e, 37a1a99, 054408d.
- Summary-provider substitution during compaction: f1f3aef, 32e090c,
  bbabe8d.
- Summary coercion and oversized-entry degradation: 99a751d, 39698f8,
  96b9269, 2b33cf5.
- Pi extraction redaction strengthening: 32f000f, e33b5fd.
- Provider quarantine hardening B: c0c7199, 8b2617d, d049c5d, a268bc3.
-->

[Keep a Changelog]: https://keepachangelog.com/en/1.1.0/
[Semantic Versioning]: https://semver.org/spec/v2.0.0.html
