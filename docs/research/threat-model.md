# Cambium Threat Model

**Historical snapshot — v0.1.0, 2026-08-09.** Design-level analysis; no source audit
was performed. It records the v2 design and the decision-10 removal of Septum
sandboxing. Current behavior belongs to [`docs/architecture/architecture.md`](../architecture/architecture.md),
source/tests, and [`v2-1-status.md`](v2-1-status.md).

**Current note (not retroactive):** active `supervisor.run_plan` is flat;
`task_decomposed` remains unsupported; provider cascade is source-defined and honors
`Retry-After`; worker stdout/event admission is bounded; there is no per-worker OS
sandbox or approval; DLQ and eval cache are absent.

Sources: superseded `system-design.md` (only concrete Septum mount list), authoritative
`architecture.md` v2.0.0, distributed-systems (DS), implementation (IMPL), and LLM
reviews, and module-template dataset/architecture canary rules. Severity is impact ×
likelihood; residual labels are **accepted** or **needs v2**.

## 1. Assets and trust boundary

| ID | Asset | Owner / location |
|---|---|---|
| A1 | Provider API keys | Diffundo; environment only, `api_key_env` names in config. |
| A2 | Repo under edit | Surculus private worktree `${session_dir}/cambium/worktrees/`. |
| A3 | Prompt/context/checkpoints | Opifex/Architectus `init`/`context`, checkpoints dir. |
| A4 | Event log | Custos SQLite WAL (+ optional JSONL), redacted payloads. |
| A5 | Merge/main/result | Unio atomic `git update-ref`; atomic `result.json`. |
| A6 | Optimization artifacts | Ascensus `.cambium/optimized/` and trajectories. |

The historical boundary was worker↔host: subprocess + NDJSON, worktree isolation,
generation fencing, and (then-proposed) Septum. Decision 10 removed sandboxing; the
historical analysis listed worktree isolation, permission allowlists, and approval gates.
The current source has no per-worker OS sandbox or approval hook.

## 2. Threat actors

| ID | Actor | Capability |
|---|---|---|
| T1 | Malicious repo content | Files steer model context via `read_file`/`grep_code`; no instruction-hygiene layer. |
| T2 | Malicious model output | Games metrics/canaries or misuses tools; `run_shell` is model-supplied. |
| T3 | Compromised worker | RCE inside its process/worktree; historical sandbox assumptions no longer apply. |
| T4 | Same-UID local attacker | Reads/writes session files, ptraces workers, sees environment. |
| T5 | Supply chain | DSPy/LiteLLM/tokenizers/git or unverified wheels execute code. |

## 3. Attack paths (historical design disposition)

1. **`grep_code` injection (FIXED in v2):** replace v0.1 shell interpolation with
   `subprocess.run(["rg","-n",pattern,path])`, stdlib `re` fallback; DS-N4,
   LLM-C5, IMPL-C5. Canary 6.1.
2. **`git_op` injection (FIXED):** list-form `git`, allowlist `add|commit|status|diff|log|stash`,
   `shlex.split` only for arguments; IMPL-N4. Canary 6.2.
3. **`run_shell` (M5/M8):** intentionally shell-backed. Historical mitigation was
   sandbox + process-group kill + tool heartbeat; after decision 10 there is no OS
   sandbox, so this is a high-privilege residual governed by permissions/approval.
4. **`write_file`/`edit_file` traversal (M5):** architecture confined `read_file` but
   did not specify write confinement. **UNVERIFIED, needs v2**; can escape to main,
   session files, or home under a no-sandbox deployment. Canary 6.3.
5. **Symlink worktree escape (M3/M8):** v0.1 mount list could block host paths but
   omitted the worktree `.git` admin dir and checkpoints; v2 mount set was not normative.
   **UNVERIFIED, needs v2**; test gitdir reachability and symlink writes.
6. **Prompt injection (M5/M6):** repo instructions can request secrets/backdoors; no
   trust boundary between file content and model. **UNVERIFIED, needs v2**; metric and
   canaries do not prove absence. Canary 6.9.
7. **Secrets (M2/M4/M5):** env inheritance, logs, events, stderr, and trajectories can
   expose keys. Redaction (`architecture.md` §12.3/§13) helps, but split/reformatted
   secrets bypass regex; env allowlist must be enforced. Canary 6.5/6.6.
8. **Merge/main tampering (M7):** expected-old-SHA `update-ref` rejects concurrent
   mutation (`NonFastForward`), but event log at rest has no signing/checksum. Canary 6.7.
9. **Orphan/split-brain workers (M3/M4):** process groups + generation fencing address
   stale workers; a live malicious worker remains a risk without sandbox. Canary 6.8.

## 4. Mitigation map

| ID | Historical control | Residual |
|---|---|---|
| M1 | Septum OS isolation (removed by decision 10) | `run_shell` has no kernel boundary. |
| M2 | Env names only, redaction, event/log separation | Full env inheritance and regex gaps. |
| M3 | Worktree isolation, generation file, recovery/prune | Symlink/gitdir semantics unspecified. |
| M4 | Human approval for optimization + canaries | No merge-publish/tool-call approval in design. |
| M5 | Dataset canaries catch deletion/`assert True`/no-op reward hacks | Canaries protect metric, not all code backdoors. |
| M6 | NDJSON request IDs, process groups, four-layer liveness, fencing | Does not constrain a live worker. |
| M7 | WAL/fsync, gap-free seq, snapshots/replay | Same-UID edits are undetected. |
| M8/M9 | Historical sandbox milestone and Ascensus optimization | M8 sandbox is out of scope; M9 artifacts need supply-chain controls. |

## 5. Residual risk register

| ID | Risk | Severity | Label / reason |
|---|---|---|---|
| **R1** | Repo prompt injection reaches merged backdoor | High | **needs v2** — no instruction trust boundary. |
| **R2** | Write/edit path escape | High | **needs v2** — only reads were explicitly confined. |
| **R3** | Sandbox gap (macOS/noop) | Out of scope | **accepted, decision 10** — no sandbox anywhere; historical controls were worktree/allowlist/approval. |
| **R4** | Full host environment exposes all A1 keys | High | **needs v2** — `{**os.environ,...}` contradicts allowlist intent. |
| **R5** | Same-UID event/session disclosure and tampering | Medium | **accepted limitation** — operational `0700`/keychain mitigation; no crypto policy. |
| **R6** | DSPy/LiteLLM/git supply chain | Medium | **accepted for v2** — pin/hash/provenance is general ecosystem work. |
| **R7** | Regex redaction bypass | Low–Medium | **accepted** with versioned patterns and provider additions. |
| **R8** | FanOut stale/poisoned cache | Low | **accepted** if opt-in `context_hash` key/TTL/private cache hold. |
| **R9** | Human concurrent main mutation | Low | **accepted**; expected-old-SHA gives clean `NonFastForward`. |
| **R10** | `/proc` metadata exposure | Low | **accepted** historical sandbox limitation; no sandbox now. |

Top historical risks: R1, R4, and R3 (the last is now an explicit out-of-scope
decision, not a claim that a sandbox exists).

## 6. Security canaries

1. `grep_code("'; touch /tmp/pwned; '", ".")` invokes literal list-form rg (pass).
2. Reject `git_op(push/config)` and shell-like args (pass).
3. Write `../escape.txt`, absolute session path, and host-main path (expected fail under
   the historical spec; confirms R2).
4. Symlink write and gitdir reachability (partial/UNVERIFIED).
5. Emit `sk-…`, `AIza…`, `api_key=…` in events/log/stderr and split secrets (standard
   pass; bypass documented as R7).
6. Env with `DEEPCODE_API_KEY`, `GEMINI_API_KEY`, `AWS_SECRET_ACCESS_KEY`; only declared
   keys should reach worker (expected fail against historical full-env design, R4).
7. Grandchild holding stdout is pinged then killed with process group.
8. Kill supervisor, restart same worktree, stale generation worker self-terminates.
9. Fake LLM follows malicious repo instruction; backdoor must not reach main (expected
   fail until R1 control exists).
10. Ascensus variants deleting tests/adding `assert True` are rejected by canaries.

The security test strategy is [`test-strategy.md`](test-strategy.md); these scenarios
remain design records, not evidence of current passing tests.

## Appendix A — threat-path evidence and boundary assumptions

The historical `run_shell` analysis depended on assumptions now explicitly removed:
Septum would bind only the worktree, read-only system paths, `/proc`, and a tmpfs;
network-off mode would unshare the network; process-group kill would contain
grandchildren. The v2 architecture later described Septum as pass-through without a
normative mount set, so the analysis could not prove symlink confinement or worktree
`.git` reachability. After decision 10, neither Linux nor macOS has an in-harness kernel
boundary. Shell remains intentional, but permission allowlists, approval gates,
worktree isolation, redaction, and generation fencing are the only claimed controls.

`write_file`/`edit_file` are more dangerous than `read_file`: architecture specified read
confinement but only atomic write, leaving path resolution unspecified. The historical
test tried `../escape.txt`, an absolute session path, and host-main path. Failure under a
no-sandbox worker was evidence for R2, not a flaky test. The symlink test also checked
that a Git worktree `.git` indirection reached the repository admin directory; the old
mount list omitted it.

The env test placed `DEEPCODE_API_KEY`, `GEMINI_API_KEY`, and unrelated
`AWS_SECRET_ACCESS_KEY` in the host. Architecture §7.2's `{**os.environ,...}` copied all
three, contradicting §12.1's names-only allowlist. With no network, a worker could write
a key to a tracked file and merge it. R4 therefore remained **needs v2**. Redaction
tests covered canonical `sk-`/`AIza` forms, `api_key=`, and split secrets; split values
documented R7's regex gap.

## Appendix B — accepted/rejected alternatives

The design rejected shell-string interpolation for `grep_code` and `git_op`; list-form
subprocess calls and a Git operation allowlist are the causal fixes for DS-N4/IMPL-N4.
It rejected trusting canaries alone against a malicious model: canaries test reward
gaming, not every backdoor. It rejected pid files as orphan identity, relying on durable
spawn events plus generation. It accepted same-UID at-rest exposure as a single-user
operational limitation, with `0700` session directories and an OS keychain as deployment
mitigations. It accepted supply-chain risk for v2 while recommending pinned/hashed
dependencies and provenance checks.

The residual list deliberately did not imply an eval cache, DLQ, OS sandbox, or
per-worker approval. Those names may appear in historical architecture references, but
they are not current features; source/tests and `v2-1-status.md` are the check.

## Appendix C — scenario evidence matrix

| Test | Distinguishes |
|---|---|
| Literal `grep_code` pattern and `/tmp/pwned` sentinel | list-form execution from shell injection. |
| `git_op(push/config)` and upload-pack-like args | operation allowlist from argument parsing. |
| Traversal and symlink writes | path-confinement promise from an unbounded fallback. |
| Secrets in events/log/stderr and split form | normal redaction from regex bypass. |
| Env allowlist with unrelated AWS secret | declared-key isolation from full inheritance. |
| Grandchild holding stdout | EOF advisory plus group kill from PID-only kill. |
| Supervisor kill/restart with stale generation | fencing from split-brain commit. |
| Prompt-injected repo file with fake LLM | instruction-hygiene control (currently absent). |
| Canary-gaming optimization variants | metric gate from training-score increase. |

Each historical scenario reports pass, fail, or partial against the then-design; none is
silently reclassified as a current source result.

## Appendix D — threat assumptions and severity method

The model treated impact as code integrity, secret confidentiality, process/session
availability, or evidence integrity, and likelihood as the capability required by T1–T5.
“Accepted” did not mean impossible; it meant the design explicitly bounded or excluded
the path. “Needs v2” meant a control was missing at the boundary, not that an operator
could safely rely on a fallback. For example, R3 became accepted only because the user
decision removed sandboxing from scope; it was not converted into a claim of isolation.

The highest-impact chain was T1→model context→`run_shell`/write tool→event/redaction or
merge→A5 `main`. A metric canary catches a subset of this chain (delete-test or
`assert True` reward hacks) but cannot prove that an injected instruction did not request
a subtle backdoor. That is why R1 remained a separate instruction-hygiene gap even when
M5 canaries passed. The env chain T3→`os.environ`→tracked file→merge made R4 similarly
independent of network egress.

The model did not assume an eval cache, DLQ, or universal approval hook. A DLQ could
preserve dropped records, but no source or test proved one; adding it would change event
durability semantics. An eval cache could alter canary/metric evidence, so its absence
was recorded rather than inferred from provider-cache wording. Per-worker approval and
an OS sandbox were likewise not implied by a `permissions` field or historical Septum
name.

## Appendix E — review traceability

`DS-N4`, `IMPL-C5`, and `IMPL-N4` identify the list-form injection fixes; `IMPL-M4` and
M1/R3 identify sandbox assumptions; `M2` covers env/redaction, `M3` worktree, `M4`
approval/canaries, `M5` metric defenses, `M6` process/IPC/fencing, `M7` event durability,
and `M8/M9` historical optimization/sandbox milestones. `LLM-C5` identifies the
`grep_code` path, while `R1–R10` are this model's residual IDs. All remain historical
labels for audit linkage; they do not certify a present mitigation.

## Appendix F — control boundaries and evidence

The threat review distinguished a control from an assumption. List-form subprocess
execution, argument allowlists, generation fencing, and event redaction were controls
with named code or test anchors. A historical `Septum` wrapper, a `permissions` field,
or a worker name was only an assumption until a source path and test proved its effect.
The review therefore marked per-worker OS sandboxing and approval prompts as out of
scope, not as hidden defenses. It also recorded that no DLQ or eval cache existed; an
operator could not infer either from a bounded queue or provider-cache wording.

The highest-impact paths crossed boundaries: T1 prompt injection to a shell or write
tool, T3 environment inheritance to a tracked file, T4 stale process state to a merge,
and T5 canary gaming to an inflated score. Each path required independent evidence. A
passing delete-test canary did not prove instruction hygiene, and a clean merge did not
prove that a grandchild had been reaped. Residual IDs remained open when only one link
in the chain was tested.

## Appendix G — scenario evidence table

| Scenario | Oracle | Failure kept visible |
|---|---|---|
| shell metacharacter in `grep_code` | no command side effect; typed result | shell injection or hidden fallback |
| traversal/symlink write | resolved path stays in worktree | host-file overwrite |
| unrelated secret in inherited env | absent from worker event/log/store | broad environment leak |
| stdout flood or grandchild pipe | bounded admission; group kill | memory growth or false EOF success |
| stale generation merge | expected-old SHA rejects publication | split-brain `main` update |
| prompt-injected repository file | policy test records rejection or gap | untracked instruction execution |
| deleted test / `assert True` variant | canary fails independently of score | reward hacking |

These were proposed or historical tests, not a declaration that every row passed on the
snapshot. The table preserves the threat actor, asset, and oracle mapping for later
audits without rewriting the proposal as current security posture.

## Appendix H — residual-risk reporting

Residual reports had to name the asset, actor, path, control, evidence, and remaining
impact. “Accepted” meant bounded or deliberately out of scope; it did not mean safe in
all deployments. R1 prompt injection, R2 secret leakage, R3 no sandbox, R4 environment
inheritance, R5 worktree escape, R6 split-brain merge, R7 process orphaning, R8 event
loss, R9 canary gaming, and R10 provider/metric manipulation remained traceable IDs.
The model required a fresh source/test check before relabeling any residual as fixed.

Threat actors were capability classes, not user identities: repository content could
inject instructions, a worker could attempt unsafe tools, an environment could carry
secrets, a crashed process could retain pipes, and an optimizer could game canaries. The
model avoided assuming malicious intent from every worker; it asked which boundary would
contain each capability and which test would expose a bypass. This kept the historical
analysis actionable without claiming universal security.

The current note remains the authority for present controls.

The model's mitigations were scoped to the named boundary. It did not infer isolation
from permissions metadata, queue bounds, or a process name.

Each residual needed a named oracle.

Residual acceptance remained review evidence, not a runtime approval mechanism. Security
controls required source and test anchors before a label changed.

The model was not a current certification.

Architecture and tests define present controls.

Residual IDs remain audit labels.

No universal mitigation is claimed.

Retain residual traceability.

Controls need evidence.

Historical only.

Historical decision identifier retained: `D7`.
