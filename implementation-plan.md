# Implementation Plan (TRANSIENT — delete when implementation is done)

Status date: 2026-08-09. Orchestrator-owned tracker. Each subagent reads this to know what
everyone else is doing. Worktrees: /tmp/opencode/cambium-<name> (branches wt-<name>).

## Phase 0 — Foundation (DONE)
- git init (main, a0fc528), docs moved: docs/system-design.md, docs/reviews/ (3 adversarial reviews).
- Python: pin CPython 3.14.7 REGULAR build (free-threaded exists but optional; GIL verified present in default build — the "GIL is gone" assumption is FALSE for default 3.14). See docs/research/python-3.14.md.
- Scaffold: pyproject.toml (requires-python >=3.14), src/cambium (orchestrator skeleton, events, modules/base.py, modules/example/ with dataset + canaries + scenario test). Merged f66bdc6 + review fixes 93db348.

## Phase 1 — Competitive research (DONE — all 9 docs merged into main)
| Tool | Doc | Final commit |
|---|---|---|
| OpenCode | docs/research/opencode.md | 692fff1 (snapshot refresh per review) |
| Codex | docs/research/codex.md | ea81dd1 (PASS) |
| pi | docs/research/pi.md | 68a31c0 (cosmetic fixes) |
| OMP | docs/research/omp.md | 85eed02 (fork provenance + stats) |
| Prime Agent | docs/research/prime-agent.md | 2dd1a89 (counts corrected) |
| py.dev | docs/research/pydev.md | d0bca1e (PASS) |
| Cloud Code | docs/research/cloud-code.md | 7810fb4 (PASS) |
| TUI best practices | docs/research/tui-best-practices.md | d937dca (PASS) |
| Python 3.14 | docs/research/python-3.14.md | 8f5492d (PASS; discipline violation recovered) |

Key research conclusions (see docs for details):
- TUI: headless-first. `cambium serve` = JSON-Lines on stdout (matches Nuntius); `cli` = rich; optional `tui` = Textual. OpenCode TUI (opentui+SolidJS) and codex (ratatui) both full-screen; codex's `exec --json` proves headless-first wins.
- GIL truth: default 3.14.7 has GIL; freethreaded build optional (verified empirically). PEP 649 lazy annotations default; JIT via PYTHON_JIT.
- Competitors: opencode (provider breadth, sequential subagents), codex (sandbox+approval, worktrees), pi (permissionless-by-default — bad), omp (hash-anchored edits), prime-agent (single-process memory blowups — validates process-per-worker), py.dev/JetBrains (Air does parallel worktree agents), cloud code (worktree format bugs).

## Phase 2 — Architecture (DONE — fb17089, wt-arch; review in progress)
GLM-5.2 (GPT-5.6 Sol backend unavailable — sol/reviewer/luna returned empty, kimi misconfigured).
Deliverables: docs/architecture.md (1063 LOC, 21 sections, resolution matrix for all 24 CRITICAL flaws),
agents.md, docs/module-template/{architecture,dataset-format,example-spec}.md.
Key decisions: standard CPython 3.14; SQLite WAL event store single-writer-thread; JSON-Lines stdio IPC
with request_id RPC framing + authoritative exit message (four-layer liveness model); headless-first public
API (Cambium/Session/Result/Instance/Event); TUI as optional view; Diffundo cache keyed on task+context+model
+ TTL (not prompt hash); tier-based cascade; multi-signal coding metric + canaries; ShouldDecompose as
first example module; proto-AGI contract via session-dir + control plane.
Open questions deferred: cascade tier taxonomy, worker pool v2.1, joint optimization, cross-model prompt
transfer, macOS sandbox posture, doom-loop detector.

## Phase 3 — Adversarial review (DONE for research+scaffold; arch review in progress)
Round 1 (reviewer/sol/luna/kimi): all failed (empty results / config error). Round 2 via GLM:
- R1 opencode+codex — codex PASS, opencode CONDITIONAL → fixed 692fff1, merged
- R2 pi+omp — pi PASS (cosmetic fixed), omp CONDITIONAL → fixed 85eed02, merged
- R3 prime+pydev — pydev PASS, prime-agent CONDITIONAL → fixed 2dd1a89, merged
- R4 cloud-code+tui — PASS ×2, merged
- R5 python314+build — doc PASS; scaffold CONDITIONAL → fixed 93db348 (6 tests green), merged
- R-arch — in progress (glm)

## Phase 4 — Merge & cleanup (IN PROGRESS)
Merged: cloud-code, tui, pydev, opencode, prime-agent, codex, python314, build, pi, omp.
Remaining: wt-arch (pending review + merge). Then integration verification in main (uv run pytest),
update this file's final status, final report. Delete this file when implementation is done.

## Decisions (recorded)
1. Python: >=3.14,<3.15 regular build. 2. Interface: headless library + JSON-Lines; TUI optional. 3. Caching upstream in Diffundo, transparent Nuntius. 4. Per-module DSPy + datasets + metric; decoupled eval harness. 5. Subprocess-per-worker (never in-process). 6. No dspy runtime dep in scaffold (heavy; seam documented). 7. Backend availability: OpenAI-family (sol/reviewer/luna) and kimi currently broken → GLM-5.2 for reviews/architecture.
