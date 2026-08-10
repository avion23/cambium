# Cambium Security Audit — Merged Implementation vs Threat Model

**Date:** 2026-08-09
**Scope:** read-only audit of `main@3d27ba3` against `threat-model.md` R1–R10/M1–M7 and D7's
containment policy (worktree isolation + permission allowlists (approval gates removed by decision); no sandbox).
**Worktree:** `/tmp/opencode/cambium-audit-security` (`wt-audit-security`).
**Audited:** supervisor/store/merge/ipc/worker/tasktree/doctor/orchestrator/events, module
code, scripts, architecture §§4/7/11/12, threat/research, and scenarios.

**Historical snapshot / current pointer:** later `main@6109a6a` merged IPC/worker `38e1d43` and
reported 108 tests/clean ruff; those are historical corrections, not a current-main assertion.
Use `docs/architecture/architecture.md`, `src/cambium/`, and `docs/research/v2-1-status.md` for
current facts. Provider loop, Diffundo, EventStore, and root `Result` exist; DLQ, eval cache,
ResourceBudget, `worker_pool`, and `events` are absent. No per-worker sandbox or production
shell approval exists; dynamic hierarchy is absent.

**Current corrections retained:** R4/F-01 was partial on the Custos branch but open in slice/gate
paths; F-21 is false after D7 fold `e8f0d0f`; the old UP041 lint observation was fixed.

## 0. Baseline verification

Historical commands from the audit worktree: `uv run --python 3.14.7 --extra test pytest --collect-only -q` → 108 collected; `pytest -q` → 108 passed; ruff source gate → clean. Doctor
reported `~/.omp/agent/models.yml is git-tracked` as a WARN. These results anchor the snapshot.

## 1. Findings

Severity is deployed-harness impact/likelihood. Every finding ID, verdict, R-ID, evidence pointer,
accepted boundary, and recommended action is retained in compact form.

| ID | Finding / evidence | Severity; status | Action |
|---|---|---|---|
| **F-01** | Full `{**os.environ}` inherited by workers; provider keys/secrets readable (`supervisor.py:214–215`; R4; architecture §12.1). | **HIGH; OPEN** in slice/gate, partial in `wt-impl-super`. | Construct env from minimal `PATH`/`PYTHONUNBUFFERED`/`PYTHONPATH`/`CAMBIUM_*` plus named `provider_env_keys`. |
| **F-02** | No `REDACT_KEYS`/`REDACT_VALUES`; worker/gate stderr, command, and store payloads persist verbatim (`supervisor.py:81–87,154–157,238`; `store.py:127`; R7). | **MEDIUM; OPEN.** Filter absent, not bypassed. | Redact at enqueue and again before INSERT; version values. |
| **F-03** | Host-authored `scratch_repo` can name any local repo (`supervisor.py:103,203`; merge `:161–178`; R2/R9). | **MEDIUM; accepted trust boundary.** | Document host-authored trust or enforce configured repo allowlist. |
| **F-04** | `prepare_staging` can reuse/clobber any registered worktree (`merge.py:296–313`; §3.5/3.8, R9). | **MEDIUM; OPEN.** | Track sequencer-owned marker/path; refuse other registered paths. |
| **F-05** | Branch/ref interpolated without `check-ref-format`/`--`; raw fetch refspec can overwrite refs (`merge.py:230,296–298`; supervisor `:163`; fake worker `:75–76`; M3). | **MEDIUM; latent/partial.** | Validate branch/ident; use `--`; fetch `refs/heads/<branch>` only. |
| **F-06** | `_write_json` drain and merge `communicate` have no deadline; stalled reader defeats ready/wall budgets (`supervisor.py:114–121,244,280–283,356`; M6). | **MEDIUM; OPEN.** | `asyncio.wait_for` each write/merge to active deadline; kill process group. |
| **F-07** | Unbounded parsed stdout queue (`supervisor.py:219–232`; M6/DoS). | **MEDIUM; OPEN.** | Bound decoded bytes/messages; overflow is protocol violation and kill. |
| **F-08** | `limit=WORKER_STDIN_LIMIT` actually caps stdout; oversized line raises/swallow delays failure; no worker stdin cap (`supervisor.py:43,213,221–232,322`; M6). | **LOW; OPEN.** | Catch `LimitOverrunError`/`ValueError`, log protocol violation, fail fast; document cap. |
| **F-09** | Gate uses `sh -c` with full host env (`supervisor.py:145–147`; §3.3/M3). | **LOW; accepted host-authored command.** | Prefer list form; scrub gate env; document shell boundary. |
| **F-10** | Quarantine stripped/refused and result checked (`merge.py:177,350–354,464–467`; F5). | **PASS; tested** (`test_publish_rejects_quarantine_env`). | — |
| **F-11** | Slice resolve/`is_relative_to` confinement covers target path and symlink validation (`supervisor.py:97–111`; fake worker `:65–71`; R2). | **PASS for slice; partial future tools.** | Port rule into every file-writing tool. |
| **F-12** | No `eval`/`exec`/`os.system`; list-form subprocess and JSON parsing (`supervisor.py:227`; fake worker `:44`). | **PASS; mitigated.** | — |
| **F-13** | Result persistence keeps status/request ID; diff/commits/failure text excluded, except stderr F-02 (`supervisor.py:308–309`; R1/R7). | **PASS with F-02 boundary.** | Keep payload minimization; redact stderr. |
| **F-14** | `doctor.check_secrets` prints only tracked path, no content; WARN behavior (`doctor.py:199–209,141`; R7/M2). | **PASS; mitigated.** | — |
| **F-15** | Critical store events checkpoint/fsync WAL+DB; non-critical crash window documented; writer death raises (`store.py:226–232,267–272`; `test_store.py:66`; M7). | **PASS spot-check; tested.** | — |
| **F-16** | Store queue is unbounded and critical wait has no timeout (`store.py:106,145–148`; M7). | **INFO/documented, LOW risk; accepted v2.1.** | Bounded backpressure and deadline; do not drop source-of-truth events silently. |
| **F-17** | SQLite/JSONL event log has no integrity checksum/signature (`store.py:152`; supervisor `:81–87`; R5). | **INFO; accepted single-UID trust model.** | Optional checksums/monotonic-gap detection. |
| **F-18** | Repo symlink can write outside worktree after D7; target check covers one path only (`fake_worker.py:69–71`; R3/§3.5). | **LOW; accepted per D7; approval not implemented.** | Resolve every tool path; implement Q7.2 external-path approval. |
| **F-19** | Slice has no restart/heartbeat caps; §7.4/7.6 are design-only (`supervisor.py:21–24`; vertical-slice out-of-scope). | **INFO; accepted slice artifact.** | Implement caps with Custos and preserve IPC contract. |
| **F-20** | Tested `MergeSequencer`/`EventStore` are not runtime callers; slice uses plain merge/JSONL (`supervisor.py:161–178`; import grep; R9/§3.8). | **MEDIUM; OPEN integration gap.** | Route terminal merge/store through hardened components. |
| **F-21** | Septum architecture claim superseded by D7 fold (`e8f0d0f`; R3). | **INFO; FALSE/superseded.** | Keep historical finding only. |
| **F-22** | Doctor executes repo `check_dataset_v1.py` under caller env (`doctor.py:168–179`; script `:49–56,201–215`; T5/R6). | **INFO; accepted repo-owned tool.** | — |

## 2. Threat-model re-rating

| R-ID | Re-rating and evidence |
|---|---|
| R1 | **Open/UNVERIFIED:** no real LLM/prompt hygiene layer exists. |
| R2 | **Partial:** slice/worker resolve confinement exists; complete future tool set does not. |
| R3 | **Confirmed accepted:** no sandbox; direct `create_subprocess_exec` (F-18). |
| R4 | **Partial:** Custos strips env; slice/gate inherit it (F-01). |
| R5 | **Confirmed accepted:** no event integrity protection (F-17). |
| R6 | **Reduced today:** `pyproject.toml` has zero runtime dependencies; revisit when DSPy/provider deps land. |
| R7 | **Worsened/open:** redaction is absent, not bypassed (F-02). |
| R8 | **N/A:** no cache module at snapshot. |
| R9 | **Partial:** merge primitive enforces expected-old/FF, runtime bypasses it (F-20). |
| R10 | **N/A:** no sandbox namespace. |

## 3. Top risks and accepted boundary

Top risks were R4/F-01 environment inheritance (HIGH), R7/F-02 missing redaction (MEDIUM), and
F-20 runtime bypass of hardened merge/store (MEDIUM). Accepted risks: R3 no sandbox (same-UID
host exposure by design); R5 tamperable log; F-18 symlink escape; F-03 trusted host `scratch_repo`;
F-16 unbounded queue/no-timeout; reduced R6; F-19 no slice restart caps. Production shell
approval and D7 Q7.2 are not implemented; they remain an unresolved security boundary.

## 4. UNVERIFIED flags

IPC/worker files are now re-auditable via `38e1d43`; canonical Custos wiring remains open.
Diffundo prompt guard, redaction, generation-file fencing (`CAMBIUM_GENERATION` is hardcoded
`"1"` in the slice), and approval gates are absent/design-only. These flags distinguish the
historical audit from a current certification.

**Finding counts:** HIGH 1 (F-01) · MEDIUM 7 (F-02–F-07, F-20) · LOW 4 (F-08, F-09, F-16,
F-18) · PASS 6 (F-10–F-15) · INFO 4 (F-17, F-19, F-21, F-22) — 22 findings.

## 5. Causal chains and evidence details

### Secret path (F-01/F-02/R4/R7)

The highest-risk chain is concrete: `env={**os.environ}` makes provider keys readable by a
worker; worker stderr are written into JSONL/event payloads without a redaction
filter; a secret can therefore travel from `os.environ` to stderr, durable event log, and CLI
output. D7's intended fix is a constructed environment (`PATH`, `PYTHONUNBUFFERED`, Cambium
identity, and only names from `provider_env_keys`) plus one versioned redactor at enqueue and a
second check before INSERT. The snapshot had no `REDACT_KEYS`/`REDACT_VALUES`, so treating this as
a regex bypass would understate the failure. Current production approval is also absent; an
approval callback is the only planned boundary for first-time external-path writes or network
egress after the sandbox decision.

### Git/worktree path (F-03/F-04/F-05/F-18/R2/R9)

The slice confines `worktree_path` and `target_file` with resolve plus `is_relative_to`, which
catches `..`, absolute path, and symlink-following escape at validation time. That check is not
enough for the merge sequencer: a registered live worker path can be reused and force-checked
out, a raw branch can act as a fetch refspec, and a committed symlink can reach outside the
worktree. These are separate causes. Sequencer ownership needs a persistent marker/path registry;
refs need `git check-ref-format`, `--` separators, and `refs/heads/<branch>` source construction;
all future write tools need the resolve rule. D7 Q7.2 approval is the designated mitigation for
first-time external paths, but no production gate exists.

### Liveness/resource path (F-06/F-07/F-08/F-16/F-19)

The worker can stop reading stdin while `drain()` blocks, or flood parsed stdout into an
unbounded asyncio queue. The supervisor then misses ready/wall deadlines and never reaches
`killpg`; an oversized line can raise inside `readline` and be swallowed by `gather` until the
wall timeout. The store has a similar class: `queue.Queue()` accepts unbounded events and a
critical append waits on an event with no timeout. The required fix is phase-deadline wrappers,
decoded-byte and message caps, fail-fast protocol events, and a bounded/redacted DLQ. F-19 is
different: the slice has no restart/heartbeat policy, so it cannot loop forever today, but it
also has no cap when Custos becomes canonical.

### What passed and why it matters

F-10 quarantine checks strip `GIT_QUARANTINE_PATH`, reject a parent quarantine, and defend the
`update-ref` result string; F-11's slice confinement is enforced in supervisor and fake worker;
F-12 found no `eval`, `exec`, or `os.system`, and all subprocess calls are list-form except the
host-authored gate; F-13 minimizes result payload persistence; F-14's doctor check reports only
the tracked path; F-15's critical store path waits for WAL/DB fsync and has a subprocess crash
test. These passes do not close F-20: `MergeSequencer` and `EventStore` can be secure in isolation
while the runtime uses neither.

The audit therefore does not certify “injection-clean” as a production claim. It certifies only
the observed source paths and records the distinction between tested component behavior and the
unwired canonical runtime.

## 6. Audit method boundary

The inventory first checked whether the files named in the task brief existed, then traced
callers and tests. `ipc.py`/`worker.py` were absent in the first slice audit but present after
`38e1d43`; this is why their framing findings are re-auditable while runtime integration is not.
The audit used grep for `shell=True`, `eval`, `exec`, `os.system`, redaction constants, imports
of `store`/`merge`, and cache decorators; it did not infer safety from module names.

The doctor warning is not a secret leak: it prints only the path of a tracked models file. F-02
is different because worker/gate stderr is persisted as content. F-15 is a spot-check of the
critical append path plus `test_store.py:66`, not full power-loss certification. F-20 remains
until one integration scenario proves the running supervisor uses those components. These method
limits are part of the historical severity and preserve the unresolved boundary rather than
substituting a fallback verdict.

The later explicit-tree feedback does not change security status: fresh child context and strict
upward envelopes are accepted targets that reduce reasoning leakage, while cache-discount,
Prime-2026, cheap-branch, and MCTS claims are unverified. No information-hiding rule can replace
the missing env redactor, generation file, or production approval callback.

Static DAG validation reduces exposure before admission but does not replace those controls: a
valid child can still read same-UID host state unless env, path, and approval boundaries close.

Provider cache pricing or MCTS search cannot mitigate F-01/F-02; those require source-level
allowlist, redaction, and approval enforcement.

The same distinction applies to context privacy: strict upward envelopes reduce parent exposure,
but same-UID host access remains until env/path/approval controls are implemented.

No cache or search result changes that threat boundary.

Source controls do.

Current source remains authoritative.

This file is historical evidence.
