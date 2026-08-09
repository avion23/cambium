# Cambium Security Audit — Merged Implementation vs Threat Model

**Date:** 2026-08-09
**Scope:** Read-only code audit of the merged implementation (`main` @ `3d27ba3`) against `docs/research/threat-model.md` (R1–R10, M1–M7) and the containment policy (no sandboxing; worktree isolation + permission allowlists + approval gates — `implementation-plan.md` decision 10, `docs/research/design-deltas.md` D7).
**Worktree:** `/tmp/opencode/cambium-audit-security` (branch `wt-audit-security`).
**Audited files:** `src/cambium/{supervisor,store,merge,ipc,worker,tasktree,doctor,orchestrator,events}.py`, `src/cambium/modules/base.py`, `src/cambium/modules/example/*`, `scripts/{fake_worker,check_dataset_v1,generate_should_decompose_v1}.py`, `docs/architecture/architecture.md` (§4, §7, §11, §12), `docs/research/threat-model.md`, `docs/research/design-deltas.md` (D7), `docs/research/vertical-slice-report.md`, `tests/scenarios/*`.

**Current-main status (2026-08-09):** this is a point-in-time audit at
`3d27ba3`. Main is now `6109a6a`, with `ipc.py` and `worker.py` merged by
`38e1d43`, 108 tests collected and passed, and a clean source ruff gate. The
original findings below remain evidence; current corrections are recorded here.

**Current corrections:** R4/F-01 is **PARTIAL**: the in-flight `wt-impl-super`
Custos path strips the worker environment, while slice and gate paths remain
open. F-21 is **FALSE**: D7 is folded into the authoritative architecture by
`e8f0d0f`. The earlier lint observation is superseded by the fixed `UP041`
issue; `ruff check src` now passes.

## 0. Baseline verification

Tests run from the audit worktree (`PYTHONPATH=src python3 -m pytest`):

| Command | Result | Note |
|---|---|---|
| Current-main suite | `uv run --python 3.14.7 --extra test pytest --collect-only -q` → 108 collected; `pytest -q` → 108 passed | CPython 3.14.7 current-main verification. |
| Current-main recheck | `uv run --python 3.14.7 --extra test pytest -q` → 108 passed; `uv run --python 3.14.7 --with ruff ruff check src` → clean | The old 3.12/E501 snapshot is superseded. The interim `UP041` source issue was fixed before `main@6109a6a`. |

The doctor run also surfaced a real environment finding: **`~/.omp/agent/models.yml is git-tracked` (WARN)** — the secrets-hygiene check correctly fires on this host.

## 1. Audit target inventory — discrepancies from the task brief

| Expected | Actual in `main` | Consequence |
|---|---|---|
| `src/cambium/worker.py` (worktree/target validation) | Present in main since `38e1d43` | Worker-side checks are now re-auditable in `worker.py`; the original slice audit remains below as historical evidence. |
| `src/cambium/ipc.py` | Present in main since `38e1d43` | Nuntius framing and limits are now re-auditable in `ipc.py`; inline slice IPC remains a separate integration path. |
| Diffundo module / prompt structure guard | **absent**; only module is `example/should_decompose` (rule-engine, no prompts) | See UNVERIFIED §7. |
| Merge/redaction in the running path | `MergeSequencer` and `EventStore` are **unit-tested but not wired to the runtime**; the slice supervisor uses its own JSONL `EventLog` and `git merge --ff-only` | Integration gap, F-22. |

## 2. Findings

Severity = impact/likelihood for the deployed harness, not the slice alone. `R-ID` maps to the threat model; `—` = new finding not previously enumerated.

| # | Finding | Sev | file:line | R-ID | Mitigation status | Recommended fix |
|---|---|---|---|---|---|---|
| **F-01** | **Full host environment inherited by every worker.** `env={**os.environ, ...}` spawns workers with every host env var — all provider API keys and unrelated secrets readable via `os.environ` in any worker. Contradicts arch §12.1 per-worker key allowlist; D7 re-rates R4 as "must fix at spawn". No scrubbing exists. | **HIGH** | `src/cambium/supervisor.py:214–215` | **R4** | **Open.** Threat model §3.7/R4 residual confirmed in code. | Spawn-time env allowlist: build `env` from `provider_env_keys` (names only) + minimal `PATH`/`PYTHONPATH`/`PYTHONUNBUFFERED`; drop `{**os.environ, ...}`. |
| **F-02** | **No redaction filter implemented anywhere.** Arch §12.3 `REDACT_KEYS`/`REDACT_VALUES` (enqueue + belt-and-braces at INSERT) does not exist in `src/`. Worker stderr lines are appended to `events.jsonl` verbatim (`line[:512]`), gate stderr verbatim, gate `command` verbatim. Combined with F-01 this is the §3.7 exfiltration chain into the durable event log. | **MEDIUM** | `supervisor.py:81–87`, `:238`, `:154–157`; `store.py:127` (payload JSON dumped as-is) | **R7** (§3.7) | **Open** — worse than R7's "regex bypass" posture; the filter is absent, not bypassed. | Implement §12.3 filter at `EventLog.emit` and `EventStore.append` (enqueue), belt-and-braces before INSERT. Version `REDACT_VALUES`. |
| **F-03** | **`scratch_repo` is not confined to the session dir.** `_validate_paths` resolves and confines only `worktree_path` and `target_file`; `scratch_repo` is accepted at any path and is the merge target (`git merge --ff-only` runs there). A crafted `task.json` can name any local repo as the merge target. | **MEDIUM** | `supervisor.py:103`, `:203`; merge at `:161–178` | R2/R9 | Accepted-as-trust: `task_spec` is host-authored (session dir is host-owned). | Document the trust boundary explicitly; optionally confine `scratch_repo` to a configured repo allowlist. |
| **F-04** | **`prepare_staging` will reuse and clobber ANY registered worktree path.** `_is_registered_worktree` returns true for the repo's own worktrees, then `git checkout --force -B <staging_branch> <worker_tip>` + rebase-`--abort`/`merge --abort`/`cherry-pick --abort` run inside it. The only guard rejects `worktree_path == repo.resolve()` (`:293–294`). A caller passing a *live worker worktree* path destroys that worker's in-progress state; the "sequencer-owned worktree" docstring claim is not enforced. | **MEDIUM** | `merge.py:303–313`, `:296–298`; guard `:293–294` | §3.8 / R9 (merge tampering); §3.5 | **Open.** | Track worktrees this sequencer created (persistent ownership marker, e.g. name prefix `cambium-merge/<id>` + recorded path) and refuse reuse of any other registered path. |
| **F-05** | **Branch/ref names interpolated into git args without validation or `--` separator.** List-form args prevent *shell* injection (§3.2 satisfied), but: (a) `git fetch origin <branch>` treats `<branch>` as a refspec — a value like `main:refs/heads/main` overwrites an arbitrary local ref, bypassing the publish fast-forward/expected-old invariants that guard only the final `update-ref`; (b) `ident = task_id or branch` feeds `refs/cambium/staging/<ident>` and `cambium-merge/<ident>`; (c) supervisor's `git merge --ff-only <branch>` parses a leading-`-` value as an option (single argv element → no arg smuggling, worst case is a git error). Today `branch` is host-authored (`task_spec`), so this is latent; it becomes live if a worker ever supplies a branch name. | **MEDIUM** (latent) | `merge.py:230`, `:296–298`; `supervisor.py:163`; `fake_worker.py:75–76` | §3.2 / M3 | Partially mitigated: list-form everywhere (verified by grep — no `shell=True` in `src/` or `scripts/`). | Run `git check-ref-format` on `branch`/`ident` before interpolation; use `--` before positional refs; pass fetch sources as `refs/heads/<branch>` (never raw refspec). |
| **F-06** | **No timeout on stdin writes to the worker and no timeout on the merge step.** `_write_json` awaits `proc.stdin.drain()` with no deadline; the `init` write (`:244`) happens *before* the deadline loop and the `run_task` write (`:280–283`) *inside* it — a worker that stops reading stdin (or a >64 KiB `spec`) blocks `drain` forever, so the ready timeout and wall budget are never enforced and no `killpg` fires. `_merge_branch`'s `proc.communicate()` (`:166`) has no timeout either; the vertical-slice report's claim that merge is "bounded by the wall budget" is not true in code. | **MEDIUM** | `supervisor.py:114–121`, `:244`, `:280–283`, `:356` | M6 / §7.2 | **Open** (liveness violation of the wall-budget invariant). | Wrap each `_write_json` in `asyncio.wait_for` against the active deadline; wrap `_merge_branch` in `wait_for` with the wall-deadline remainder (gate already does this, `:346–347`). |
| **F-07** | **Unbounded message queue from worker stdout.** `messages = asyncio.Queue()` (no maxsize); `_read_stdout` enqueues every parsed JSON message. A worker flooding lines grows memory until the wall budget. | **MEDIUM** | `supervisor.py:219`, `:221–232` | M6 / DoS | **Open.** | Bound `maxsize`; on overflow treat as protocol violation (kill + fail). |
| **F-08** | **IPC read limit mislabeled and silently kills the reader.** `limit=WORKER_STDIN_LIMIT` (`1_048_576`) is passed to `create_subprocess_exec` but applies to the *stdout* StreamReader (worker→supervisor), not stdin. A single worker line > limit raises `ValueError` in `readline`; the exception is swallowed by `gather(..., return_exceptions=True)` (`:322`), the EOF sentinel is never sent, and the session fails only at the wall deadline with a misleading timeout. No worker-side stdin cap exists at all. | **LOW** | `supervisor.py:43`, `:213`, `:221–232`, `:322` | M6 | **Open.** | Catch `LimitOverrunError`/`ValueError` in `_read_stdout`, log as protocol violation, and fail fast; document that `limit` is a read cap. |
| **F-09** | **Gate command executed via `sh -c` with the full host env.** `_run_gate` uses `create_subprocess_exec("sh", "-c", command, ...)` — the only shell-interpreted execution point. `command` is host-authored (`task_spec["gate"]`), so not an injection vector today, but env expansion inside it can reach secrets, and it runs with `cwd=worktree` (worker-controlled directory). | **LOW** | `supervisor.py:145–147` | §3.3 / M3 | Accepted (host-authored command); mirrors D7 item 6's permission-gated `run_shell`. | Keep gate list-form where possible; scrub env for the gate; document as the intended shell-capable boundary. |
| **F-10** | **`GIT_QUARANTINE_PATH` handled correctly.** Stripped from every git subprocess env (`_git_env`), publish refused loudly when set in the parent, git's quarantine refusal re-checked on `update-ref`. Tested (`test_merge.py::test_publish_rejects_quarantine_env`). | PASS | `merge.py:177`, `:350–354`, `:464–467` | §3.8 / F5 | **Mitigated + tested.** | — |
| **F-11** | **`write_file`/`edit_file` path confinement** (R2) exists only in the slice: resolve-based `is_relative_to` checks for `worktree_path` and `target_file` (covers `..`, absolute paths, and symlink-following escapes at validation time), enforced twice (supervisor + worker). The real worker tool set (§11) that would need the same rule does not exist yet. | PASS (slice) | `supervisor.py:97–111`; `fake_worker.py:65–71` | R2 | Partially implemented (slice only). | Port the same resolve+`is_relative_to` rule into every file-writing tool when `worker.py` lands. |
| **F-12** | **No `eval`/`exec`/`os.system` anywhere in `src/` or `scripts/`** (grep-verified). Worker stdout and stdin are parsed with `json.loads` (supervisor `:227`, fake_worker `:44`); malformed JSON is caught and logged (`parse_error`). | PASS | `supervisor.py:227`; `fake_worker.py:44` | §4 | **Mitigated.** | — |
| **F-13** | **No secret/prompt content in logs at the payload boundary.** The supervisor persists only `status`/`request_id` from `result_envelope` (`:308–309`) — the envelope's `diff`, `commits`, `failure_reason` never reach `events.jsonl`. | PASS | `supervisor.py:308–309` | §3.7 | **Mitigated** (except stderr — see F-02). | — |
| **F-14** | **`doctor.check_secrets` leaks no content.** It reads only git-tracked status of `~/.omp/agent/models.yml` and prints the path; never file contents. Correctly `WARN`, not `FAIL`. Fires on this host (models.yml is git-tracked). | PASS | `doctor.py:199–209`, `:141` (event DB opened `?mode=ro`) | §3.7 / M2 | **Mitigated.** | — |
| **F-15** | **Store crash-durability for critical kinds is sound.** Critical events block until `wal_checkpoint(TRUNCATE)` + `fsync` of both WAL and DB fds (`_fsync_now`); non-critical appends ride the fsync cadence with a documented crash window; writer death is fatal and surfaced as `StoreError`. Crash-durability is exercised by a subprocess test (`test_store.py:66`). Phantom-read + unbounded-queue deviations are documented in the module docstring (`store.py:13–22`). | PASS (spot-check) | `store.py:267–272`, `:226–232` | M7 | **Mitigated + tested.** | — |
| **F-16** | **Unbounded append queue in the store** — `queue.Queue()` with `put_nowait`; producers can enqueue without bound if the writer stalls. Documented as a deliberate v2.1 decision (`store.py:20–22`). Critical appends block on `pending.event.wait()` with **no timeout** (a hung fsync stalls producers indefinitely — errors are covered by writer-death, hangs are not). | INFO (documented) / LOW | `store.py:106`, `:145–148` | M7 | Accepted (documented v2.1). | v2.1 bounded-with-backpressure; add `wait_for` on the critical-event event. |
| **F-17** | **Event log has no integrity protection** — plain SQLite WAL / JSON-Lines, no signing or checksums. A same-UID process can edit `events.db` or `events.jsonl` undetected. | INFO | `store.py:152`, `supervisor.py:81–87` | R5 | Accepted (documented limitation; single-host, single-user trust model). | Optionally add per-row checksums or `events_after` monotonic-gap detection. |
| **F-18** | **Symlink-in-worktree escape is not stopped by worktree isolation.** With the sandbox removed (D7), a repo-committed symlink pointing outside the worktree can be written through by worker tools; the slice's `target_file` check catches it only for that one path at validation time. Weaker than the already-accepted `run_shell` host exposure (R3). | LOW (accepted per D7) | `fake_worker.py:69–71` | §3.5 / R3 | Accepted + planned: D7 Q7.2's "first-time external-path writes" approval gate is the designated mitigation, not yet specified/implemented. | Resolve every tool path; land the Q7.2 approval gate for out-of-worktree writes. |
| **F-19** | **Restart/heartbeat caps not implemented.** The slice has no restart policy at all (a timed-out task fails, never retried) and no heartbeat loop — §7.4 burst/absolute caps and §7.6 heartbeats are design-only. Consequently there is currently no restart-loop or heartbeat-forever DoS, but also no cap to lean on when Custos lands. | INFO | `supervisor.py:21–24` (scope guard); `vertical-slice-report.md` (out-of-scope list) | §7.4 / M6 | Accepted for the slice (documented). | Implement §7.4 caps with Custos; keep them in the worker IPC contract. |
| **F-20** | **Merge sequencer + event store not wired to the runtime.** `MergeSequencer` (atomic `update-ref` publish, empty-old backdoor guard, FF enforcement — all tested) and `EventStore` (SQLite WAL) are imported nowhere outside their own files and tests. The slice runtime publishes via plain `git merge --ff-only` in the scratch repo (working-tree merge, no expected-old check, no `create_main` first-publish guard) and logs via its own JSONL `EventLog`. | **MEDIUM** (integration gap) | grep: no importers of `cambium.merge`/`cambium.store` outside tests; `supervisor.py:161–178` | §3.8 / R9 | **Open.** §3.8's "strongly mitigated" is true of `merge.py`, not of the code that actually runs. | Route the supervisor's terminal merge through `MergeSequencer.publish_merge` and the event log through `EventStore`. |
| **F-21** | **Historical architecture-doc claim is superseded.** D7 removed Septum from the v2 scope and the fold is in the authoritative architecture. | FALSE / superseded | `e8f0d0f`, current `docs/architecture/architecture.md` | R3 | No current doc gap. | Keep the original finding as historical evidence. |
| **F-22** | **Doctor dataset check is a hidden subprocess executing a repo script** (`check_dataset_v1.py`) with the caller's env — it runs secrets scanning on dataset content (good), but it is a code-execution dependency of `doctor` on a repo-controlled script. | INFO | `doctor.py:168–179`, `:34`; `scripts/check_dataset_v1.py:49–56,201–215` | T5/R6 | Accepted (repo-owned tool). | — |

## 3. Re-rating of threat-model residuals against the actual code

| R-ID | Threat-model rating | Re-rating | Basis |
|---|---|---|---|
| R1 (prompt injection via repo files) | High, needs v2 | **Unchanged, still open — UNVERIFIED in code.** No real LLM path exists yet, so no prompt-injection surface is *implemented*; equally, nothing in code addresses the boundary (no instruction-hygiene layer anywhere). | `worker.py` is present, but no Diffundo/real LLM trust-boundary code exists. |
| R2 (write_file/edit_file confinement) | High, needs v2 | **Partially closed.** Resolve+`is_relative_to` confinement exists in the slice and worker runtime; the complete future tool set remains unspecified. | `supervisor.py:97–111`, `worker.py`, `fake_worker.py:65–71`. |
| R3 (no sandbox / run_shell host exposure) | accepted — out of scope (D7) | **Confirmed.** No sandbox wrapper exists; spawn is direct `create_subprocess_exec` (`supervisor.py:208`). The accepted residual stands. | F-18. |
| R4 (full host env to workers) | High, needs v2 → "must fix" per D7 | **PARTIAL.** The `wt-impl-super` Custos path strips the worker environment; the slice and gate paths still inherit/open the host environment. | F-01; implementation wave. |
| R5 (event-log tamperable at rest) | Medium, accepted | **Confirmed, accepted.** No integrity protection in `EventStore` or slice `EventLog`. | F-17. |
| R6 (supply chain) | Medium, accepted | **Reduced in the current implementation.** `pyproject.toml` has zero runtime dependencies (`dependencies = []`); only dev deps pytest/ruff. No DSPy/LiteLLM in the tree to poison. Risk returns when deps land. | `pyproject.toml`. |
| R7 (redaction-filter bypass) | Low–Medium, accepted | **Worsened: the filter is absent, not bypassed.** No `REDACT_KEYS`/`REDACT_VALUES` anywhere in `src/`. | F-02. |
| R8 (FanOut cache) | Low, accepted | N/A — no cache module exists. | — |
| R9 (concurrent main mutation) | Low, accepted | **Partially addressed.** `merge.py` enforces the atomic expected-old + FF invariants (tested); the *runtime* merge path does not use them (F-20). | F-20. |
| R10 (/proc exposure) | Low, accepted | N/A — no sandbox namespace to expose. | — |

## 4. Top three risks

1. **R4 — environment inheritance is only partially closed.** The Custos path
   strips the worker environment, but slice and gate paths remain open until
   `wt-impl-super` becomes canonical. **HIGH, in-flight.**
2. **R7/§3.7 — no secret redaction anywhere in the log path** (`supervisor.py:81–87,238`). Worker stderr and gate stderr/commands land verbatim in `events.jsonl`. Combined with risk 1, a key dumped to stderr is persisted durably and printed to stdout by the CLI (`supervisor.py:437`). **MEDIUM.**
3. **Merge/event-store hardening not on the running path** (`F-20`). The only *tested* protections against `main` tampering (atomic expected-old `update-ref`, empty-old backdoor guard, FF enforcement) live in `MergeSequencer`, which the supervisor does not call; the runtime uses a plain `git merge --ff-only`. **MEDIUM.**

## 5. Accepted risks (explicit)

- R3 — no sandbox; containment = worktree isolation + allowlists + approval gates (user directive, D7). A compromised worker has full same-user host access by design.
- R5 — event log readable/tamperable by any same-UID process.
- F-18 — symlink-in-worktree escape (weaker than the accepted R3 exposure); mitigation deferred to the D7 Q7.2 approval-gate design.
- F-03 — `scratch_repo` unconfined (trusted host-authored `task_spec`).
- F-16 — unbounded store queue + no-timeout critical-appends (documented v2.1).
- R6 — reduced today (zero runtime deps); must be revisited on dependency introduction.
- F-19 — no restart caps in the slice (no restart policy exists).

## 6. UNVERIFIED flags (honest)

- **Historical N-A correction:** `worker.py` and `ipc.py` are now in main via `38e1d43`; their framing, worker deadlines, path checks, and diff limits are re-auditable. The remaining gap is canonical Custos wiring, not file absence.
- **Prompt structure guard in Diffundo: does not exist.** No Diffundo module, no cache, no prompt guard, no instruction-hygiene layer (R1).
- **Redaction: not implemented** (F-02) — the §12.3 design cannot be verified because there is no implementation.
- **Generation fencing** (§7.3): the slice hardcodes `CAMBIUM_GENERATION: "1"` (`supervisor.py:215`) and `fake_worker.py` echoes it without any `.cambium/generation` file check. Split-brain defense is design-only.
- **Approval gates** (D7 containment layer): none exist; Q7.2 (gate shape) is an open question.

## 7. Summary

The codebase is injection-clean where it has code: **no `shell=True`, no `eval`/`exec`, all subprocess calls list-form, JSON parsing only**, and the slice's path confinement is correctly resolve-based. `MergeSequencer` and `EventStore` implement their security-relevant contracts well and are tested. The structural gaps are: (1) R4 full-environment inheritance — confirmed in the actual spawn code; (2) the complete absence of the §12.3 redaction filter, with worker stderr persisted verbatim; (3) the runtime does not use the hardened merge/store components (integration gap). Threat-model residuals R1, R4, R7 remain open; R6 is currently reduced by a zero-runtime-dependency tree; R2 is partially closed for the slice; everything else is accepted as documented.

**Finding counts by severity:** HIGH 1 (F-01) · MEDIUM 7 (F-02..F-07, F-20) · LOW 4 (F-08, F-09, F-16, F-18) · PASS 6 (F-10..F-15) · INFO 4 (F-17, F-19, F-21, F-22) — 22 findings total.
