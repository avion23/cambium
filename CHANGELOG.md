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

### Changed

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

### Fixed

- **SQLite resilience.** Hardened SQLite WAL persistence with a single writer,
  bounded busy/checkpoint retries, and explicit disk-full error propagation
  for event append/checkpoint and quota transactions; failed writes are not
  acknowledged as durable.
- **Orphan cleanup.** Reclaimed worktrees orphaned by hard crashes and covered
  the cleanup guarantee with soak scenarios.
- **Ruff corruption guard.** Added a syntax-hygiene guard for corrupted
  multiple-exception handlers and pinned development Ruff below 0.15 after the
  formatter regression was reproduced.

### Removed

- Removed the repository's failing CI workflows; Cambium remains deliberately
  CI-less and is verified through its local test and validation workflows.

<!--
Evidence trail for the unreleased entries (all hashes are commits reachable
from origin/main):

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
-->

[Keep a Changelog]: https://keepachangelog.com/en/1.1.0/
[Semantic Versioning]: https://semver.org/spec/v2.0.0.html
