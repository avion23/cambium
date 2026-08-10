# Competitive Analysis: Prime Agent (`prime-agent`)

**Researched:** 2026-08-09. **Local version:** 0.7.1. **Target:** `/home/ubuntu/.local/bin/prime-agent` and `~/.prime`. **Scope:** local install plus public sources; local claims cite commands/output, web claims cite URLs, and unsupported items are **UNVERIFIED**. Snapshot only; not runtime authority.

## 1. What it is / stack

Prime Agent is PrimeIntellect-ai’s MIT-licensed “self-improving RLM agent for coding workflows and long-running autonomous tasks.” GitHub snapshot: 10.8k stars, 1.1k forks, 4,480 commits, 176 issues, 271 PRs. It is a hard fork of pi-mono and retains `@earendil-works/pi-*` package identifiers. https://github.com/PrimeIntellect-ai/prime-agent ; https://github.com/badlogic/pi-mono

The launcher is a 54-line ESM script importing an esbuild Node/TypeScript bundle and enforcing Node >=22.8.0. It was installed from `/tmp/prime-agent-0.7.1.tgz`; npm log command was `install --global /tmp/prime-agent-0.7.1.tgz`. Core dependencies are private R2 tarballs (for example `https://pub-728493de92a943e2a9b2d17b4719f318.r2.dev/releases/v0.7.1/prime-agent-core-0.7.1.tgz`). `npm view prime-agent` returns E404; the README points to `https://app.primeintellect.ai/prime-agent/install.sh`.

Two abstractions dominate:

1. **RLM:** context is variables; one persistent IPython kernel is the built-in tool; recursive children use `rlm(...)`.
2. **Continual Harness:** prompts, memories, skills, and subagent specs are durable and refined by `/refine` with evidence, snapshots, and rollback.

Installed architecture docs describe TUI/headless clients → `AgentConnection` → Unix-socket daemon supervisor → session workers (`AgentSessionRuntime`, scheduler, root IPython kernel, child runtimes) → providers and JSONL storage. Workers and kernels are lifecycle isolation, **not security sandboxes**. Sessions are append-only JSONL trees with branching/fork/clone/compaction. `~/.prime/agent` includes settings/models/auth, harness state, bundled `kernel-venv`, sessions, leases, and logs.

Local models define `opencode-go` (DeepSeek V4), NVIDIA MiniMax M3, and TokenRouter auto routes; auth structure includes Google, OpenRouter, Z.AI, Kimi, and OpenAI-Codex. Settings default to `opencode-go/deepseek-v4-flash`, high thinking, two retries/3-second delay, compaction reserve 24,576/keep 24,000.

## 2. What it does well

1. Persistent IPython state makes imports, variables, and handles survive turns.

### 2.2 Native subagents

2. `rlm(...)` supplies named parallel/background children with attributed usage and peer messaging.

### 2.3 Daemon-backed continuity

3. Daemon attach supports terminal detach; goals, heartbeats, schedules, and bounded autonomous gates persist.

### 2.4 Continual self-improvement

4. `/refine` records lessons as prompts/memories/skills/subagent specs with rollback; local `harness_state.json` contains a refine workflow and reviewer spec.
### 2.5 Append-only JSONL branching and compaction

5. Append-only JSONL branching plus compaction summaries carrying `goal/constraints/progress/blocked/decisions` kept a local 1,988-message orchestration session coherent.
6. `prime-agent model list` exposed 399 lines (~398 models) across eight provider groups; local sessions drove parallel worktree reviews.

## 3. What it does poorly / limitations

### 3.1 Memory scaling failure

1. **Memory scaling failure (local incident).** The daemon holds parent and RLM runtimes in one Node process. `agent.jsonl` has six V8 heap-OOM traces. Session `019fe693-f125` reproduced a 32 GB-cap run reaching 20,145 MiB RSS and earlyoom SIGTERM; `NODE_OPTIONS=12288` self-aborted near 12,291 MB heap/RSS ~12.5 GB. The session attributes pressure to the 1M-token DeepSeek context; exact idle-eviction tuning beyond this evidence is **UNVERIFIED**.

### 3.2 Socket and lock supervision

2. **Socket/lock supervision failures:** local logs contain 26 “Could not adopt worker” (19 hello timeouts, 5 dead workers), 36 “Timed out” lines, and 57 “Unknown active session” attach failures.

### 3.3 Mid-work child failure

3. Children can die mid-work (“completed without sending a reply”); recovery warns that in-flight model/tool/child work was not replayed.
4. No security sandbox: model-generated Python runs with user permissions. Credentials are plaintext in `models.json`/`auth.json`.
5. Daemon/log sprawl: 59 worker logs (62 files total) and frequent shutdown/restart lines.
6. Fragmented distribution: private R2 tarballs/GitHub/install script, not the npm registry.

## 4. Relevant lessons for Cambium

### 4.1 Process-per-worker isolation

1. Use process-per-worker isolation; the OOM incident validates independent heaps and restart boundaries over one process containing a swarm.

### 4.2 Memory and tool watchdogs

2. Watch memory/heap and per-tool duration, not only process liveness; cap context and evict idle workers.

### 4.3 Pipe IPC and named children

3. Prefer stdin/stdout IPC and PID/pipe identity to Unix sockets plus lock files; retain named, inspectable child tasks and per-child last-activity counts.

### 4.6 JSONL session tree and compaction carry-forward

4. Keep JSONL session trees and compaction summaries carrying `goal/constraints/progress/blocked/decisions`, but make checkpoints/replay durable. Store secrets as environment references, not plaintext JSON.

### 4.7 Self-hosted QA

5. Self-hosted QA with canaries and objective counts is a useful operating pattern.

## 5. Local install evidence

| fact | command/output |
|---|---|
| symlink/version | `file /home/ubuntu/.local/bin/prime-agent`; `prime-agent --version` → `0.7.1` |
| Node floor | launcher source: `MIN_NODE_VERSION_PARTS = [22, 8, 0]` |
| npm registry | `npm view prime-agent version` → 404 |
| install | npm log: global install of `/tmp/prime-agent-0.7.1.tgz` |
| daemon | `prime-agent status`: `/tmp/prime-agent-1001/daemon.sock`, pid 1038512, version 0.7.1, 8 sessions |
| children | `prime-agent list`: named build/review/docs agents on DeepSeek V4 Flash |
| corpus | 19 session files, 7,397 lines, 2,945 messages, 1,714 child-usage events, 4 compactions |
| logs | 886 warn + 6 error; six heap-OOM traces; 26 adopt failures; 36 timeouts; 57 unknown-session failures |
| worktrees | `~/.prime/worktrees/core-audit-7f40/.git` points to target repo worktree at detached `8a6472b5e` |
| kernel | `~/.prime/agent/kernel-venv/bin/` contains bundled IPython venv |

## 6. Sources and stats

- https://github.com/PrimeIntellect-ai/prime-agent (repo metrics/README)
- Installed README, CHANGELOG 0.7.1 (2026-08-07), `docs/architecture/architecture.md`
- `~/.prime/agent/*`, `~/.prime/worktrees/*`, and npm log above; all inspected 2026-08-09
- **UNVERIFIED:** https://arxiv.org/abs/2605.09998 ; https://www.primeintellect.ai/blog/rlm ; exact GitHub metrics beyond the fetched snapshot
- Install/distribution references inspected: https://app.primeintellect.ai/prime-agent/install.sh ; https://pub-728493de92a943e2a9b2d17b4719f318.r2.dev/releases/v0.7.1/prime-agent-core-0.7.1.tgz ; https://registry.npmjs.org/prime-agent (404 for the name)
- Distribution/source URLs retained from the inspected snapshot: https://app.primeintellect.ai/prime-agent/install.sh ; https://pub-728493de92a943e2a9b2d17b4719f318.r2.dev/releases/v0.7.1/prime-agent-core-0.7.1.tgz ; https://registry.npmjs.org/prime-agent ; https://www.primeintellect.ai/blog/rlm

Objectively verified: version 0.7.1; Node floor 22.8.0; installed from local tarball; 19 sessions/7,397 lines/2,945 messages/1,714 child events/4 compactions; 6 OOM traces; 26 adopt, 36 timeout, 57 unknown-session failures; 399 model-list lines.

The local daemon status command reported socket `/tmp/prime-agent-1001/daemon.sock`, pid 1038512, version 0.7.1, eight sessions, and one-hour uptime. `prime-agent list` exposed named children such as `exec-test-inventory`, `review-dryrun-config-parity`, `build-dryrun-exec-authority`, and `docs-dryrun-inventory`, all on `opencode-go/deepseek-v4-flash`. The worktree path `/home/ubuntu/polymarket-arbitrage/.git/worktrees/core-audit-7f40` was detached at `8a6472b5e`.

The memory incident has a useful causal distinction: the in-session root-cause note attributed growth to each runtime retaining a 1M-token model context, rather than fixed daemon overhead. The bug configuration reached 20,145 MiB RSS and triggered earlyoom at 15% free memory; the 12,288-MB V8 cap self-aborted near 12,291 MB and left about 12.5 GB RSS without starving the host. The exact idle-eviction value proposed in that session remains **UNVERIFIED** because it was not confirmed in shipped documentation.

Socket failures also have distinct causes: 19 hello timeouts, five workers already dead, and two other adoption failures; the 36 timeout lines include 20 worker-connect timeouts plus attach/eviction/peer-sync/heartbeat cases; the 57 unknown-session lines are attach failures. This supports avoiding the supervision topology, but not a claim that every daemon run fails.

`harness/harness_state.json` contained a `refine_workflow` prompt and a `mac_test_reviewer` spec created 2026-08-06. Session summaries recorded children that completed without replies and a worker-recovery event warning that in-flight tool/model/child work was not replayed. These are the concrete reasons to combine named children, per-worker checkpoints, and process-level isolation in Cambium.

The public project’s RLM paper/blog and continual-harness claims were not independently validated. Keep the local OOM, timeout, and session counts separate from those upstream claims when this snapshot is reused.

The session worker architecture has a useful separation that should not be lost in the criticism: daemon routing/attachments, session workers, and IPython kernels are separate processes for lifecycle recovery, even though the parent and children share a Node heap and the worker/kernel processes are not security sandboxes. The local failures show that process boundaries alone do not provide a complete protocol; hello adoption, leases, heartbeat, and replay need deterministic state transitions.

The bundled kernel venv under `~/.prime/agent/kernel-venv/` contains IPython while the system interpreter did not expose `ipykernel`. This explains why Prime’s “one built-in tool” is a packaged runtime dependency. Cambium’s Python worker boundary should make interpreter/kernel requirements explicit rather than assuming the host environment matches the agent’s.

The local command surface includes `agents`, `list`, `attach`, `stop`, `send`, `schedule`, `status`, `doctor`, `shutdown`, `package`, `update`, `model`, `session`, and `config`, plus `--autonomous` gates/limits and `--resume`/`--fork`. These are useful continuity controls, but the attach/lease errors show why a command can exist without a reliable state transition. Cambium should make each control idempotent and record its result in the event log.

The install provenance is unusually specific: the global npm command consumed a local tarball, while private R2 tarballs supplied core packages and the public registry name returned 404. A later snapshot should record whether the project’s install script or GitHub release was used; otherwise “version 0.7.1” does not identify a reproducible artifact.
Prime’s `child_usage_attributed` events show that parent accounting can remain useful even when child execution is parallel. Cambium should record model/token usage by task ID, but never treat usage attribution as proof that the child’s side effects or final reply were durably recovered.
Future snapshots should retain both the tarball install path and the daemon architecture document; version alone does not identify the private dependency build.
