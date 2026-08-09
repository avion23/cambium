# Implementation Plan (TRANSIENT — delete when implementation is done)

Status date: 2026-08-09. Orchestrator-owned tracker. Each subagent reads this to know what
everyone else is doing. Worktrees: /tmp/opencode/cambium-<name> (branches wt-<name>).

## Phase 0 — Foundation (DONE)
- git init (main, a0fc528), docs moved: docs/system-design.md, docs/reviews/ (3 adversarial reviews).
- Python: pin CPython 3.14.7 REGULAR build (free-threaded exists but optional; GIL verified present in default build — the "GIL is gone" assumption is FALSE for default 3.14). See docs/research/python-3.14.md (wt-python314, 8f5492d).
- Scaffold: pyproject.toml (requires-python >=3.14), src/cambium (orchestrator skeleton, events, modules/base.py, modules/example/ with dataset + canaries + scenario test). See wt-build (90589c6).

## Phase 1 — Competitive research (DONE, all committed)
| Tool | Doc | Worktree / commit | Agent |
|---|---|---|---|
| OpenCode | docs/research/opencode.md | wt-opencode 06023f2 | general |
| Codex | docs/research/codex.md | wt-codex ea81dd1 | general |
| pi | docs/research/pi.md | wt-pi adfb299 | general |
| OMP | docs/research/omp.md | wt-omp b9acd1d | general |
| Prime Agent | docs/research/prime-agent.md | wt-prime-agent 2f6873e | general |
| py.dev | docs/research/pydev.md | wt-pydev d0bca1e | general |
| Cloud Code | docs/research/cloud-code.md | wt-cloud-code 7810fb4 | general |
| TUI best practices | docs/research/tui-best-practices.md | wt-tui d937dca | general |
| Python 3.14 | docs/research/python-3.14.md | wt-python314 8f5492d (recovered — agent violated worktree discipline, committed to main; fixed by orchestrator) | general |

Key research conclusions (see docs for details):
- TUI: headless-first. `cambium serve` = JSON-Lines on stdout (matches Nuntius); `cli` = rich; optional `tui` = Textual. OpenCode TUI (opentui+SolidJS) and codex (ratatui) both full-screen; codex's `exec --json` proves headless-first wins.
- GIL truth: default 3.14.7 has GIL; freethreaded build optional (verified empirically). PEP 649 lazy annotations default; JIT via PYTHON_JIT.
- Competitors: opencode (provider breadth, sequential subagents), codex (sandbox+approval, worktrees), pi (permissionless-by-default — bad), omp (hash-anchored edits), prime-agent (single-process memory blowups — validates process-per-worker), py.dev/JetBrains (Air does parallel worktree agents), cloud code (worktree format bugs).

## Phase 2 — Architecture (IN PROGRESS, wt-arch)
GLM-5.2 (GPT-5.6 Sol backend unavailable — sol/reviewer/luna returned empty, kimi misconfigured).
Deliverables: docs/architecture.md, agents.md, docs/module-template/{architecture,dataset-format,example-spec}.md.
Must resolve all review CRITICAL flaws (matrix in architecture.md).

## Phase 3 — Adversarial review (IN PROGRESS)
Round 1 (reviewer/sol/luna/kimi): all failed (empty results / config error). Round 2 via GLM:
- R1 opencode+codex (glm) — running
- R2 pi+omp (glm) — running
- R3 prime+pydev (luna) — EMPTY, must redo via glm
- R4 cloud-code+tui (glm) — running
- R5 python314+build (glm) — running
Failures → fix via resumed original agent session, re-review.

## Phase 4 — Merge & cleanup (PENDING)
Per-worktree: review PASS → merge --no-ff into main, remove worktree, keep branch. Then integration verification in main (uv run pytest, compileall). Delete implementation-plan.md.

## Decisions (recorded)
1. Python: >=3.14,<3.15 regular build. 2. Interface: headless library + JSON-Lines; TUI optional. 3. Caching upstream in Diffundo, transparent Nuntius. 4. Per-module DSPy + datasets + metric; decoupled eval harness. 5. Subprocess-per-worker (never in-process). 6. No dspy runtime dep in scaffold (heavy; seam documented).
