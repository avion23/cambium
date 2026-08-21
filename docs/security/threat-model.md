# Cambium threat model

**Status:** active design-level security contract. Source and tests establish
implemented controls. This document replaces the historical Septum/sandbox
model in `docs/research/threat-model.md`.

## 1. Security posture

Cambium has **no in-harness OS sandbox or per-worker containment boundary**.
Workers, Python code, shell tools, Git hooks, test commands, and repository code
execute with the operating-system authority of the user running Cambium,
subject only to ordinary process credentials and any external host/container
controls.

A worktree, path allowlist, tool schema, approval policy, read-only copy, or
redactor can prevent specific mistakes; none is equivalent to kernel-enforced
containment. Cambium must not describe these mechanisms as a sandbox.

Consequences:

- A malicious repository, dependency, test, hook, or generated command may read
  or modify anything accessible to the current user and may use available
  network access.
- A model or prompt injection that reaches a shell-capable tool can become code
  execution with that same authority.
- Cambium is appropriate only when the operator accepts this trust level or
  runs the entire harness inside an independently managed VM/container/account
  boundary.

External isolation is deployment policy, not an assumed Cambium component.

## 2. Security objectives

Within the no-sandbox posture, Cambium should:

1. never leak provider credentials through prompts, events, logs, commits, or
   child contexts;
2. make every privileged capability explicit and attributable to a task;
3. prevent stale, duplicate, or mismatched workers from publishing state;
4. preserve durable evidence needed to diagnose tool and model actions;
5. confine repository mutations to the intended worktree/ref publication path
   where the existing boundary promises that behavior;
6. fail closed on protocol, identity, generation, artifact, and credential
   mismatches;
7. bound denial-of-service exposure with wall, token, output, queue, process,
   and fan-out limits;
8. avoid turning cached or summarized untrusted content into implicit trusted
   instructions.

Cambium cannot guarantee confidentiality or integrity against arbitrary code
already executing as the same OS user.

## 3. Assets

- provider API keys, OAuth access/refresh tokens, account identifiers;
- source repositories, uncommitted work, Git refs, signing/config state;
- session event databases, checkpoints, conversation stores, result artifacts;
- user prompts, proprietary code, retrieved documents, and child-agent results;
- routing/accounting state and provider quota;
- host files, SSH keys, cloud credentials, browser/session data, and network
  identity reachable by the current user;
- integrity of the active context epoch and branch publication sequence.

## 4. Trust boundaries

### Operator to Cambium

The operator controls the task and deployment. CLI input is trusted as intent,
but may still contain accidental secrets or ambiguous destructive requests.

### Repository/dependencies to worker

Repository files, `agents.md`-style instructions, build scripts, Git hooks,
tests, generated files, lockfiles, and dependencies are untrusted input. Once
executed, they have user-level authority because no sandbox intervenes.

### Provider/model to tool layer

Model output is untrusted structured data. It becomes an action only after
protocol parsing, schema validation, capability checks, and correlation to the
active request/generation.

### Supervisor to worker IPC

NDJSON/stdin/stdout records cross a process boundary. Message size, shape,
request identity, generation, and lifecycle must be validated. Worker stderr is
not protocol data.

### Child branch to parent

A child result is untrusted until its source epoch, generation, artifact IDs,
checks, and merge contract are validated. A child transcript is not inherited
wholesale.

### Cambium to provider

Prompts, tools, code excerpts, and cacheable prefixes leave the host. Provider
retention, cache isolation, regional processing, and account policy are
external trust assumptions and must be represented in provider configuration.

## 5. Adversaries and failure sources

- malicious or compromised repository author;
- prompt injection embedded in source, issue text, documentation, test output,
  web content, or tool results;
- compromised or malicious package/dependency/build system;
- incorrect or adversarial model output;
- another local process with the same user's authority;
- stale, duplicated, crashed, or partially initialized worker;
- provider/account compromise or cross-tenant retention failure;
- accidental operator instruction, configuration drift, or implementation bug;
- resource exhaustion caused by recursion, fan-out, output floods, or retry
  storms.

## 6. Threats and required controls

### T1. Credential disclosure

**Paths:** environment dumps, shell commands, exception text, HTTP debug logs,
provider payloads, event persistence, summaries, child context inheritance, Git
commits.

**Controls:** minimal per-worker environment; inject only the selected
credential; never send refresh tokens to workers; dynamic exact-value and
structured-field redaction; sanitize exceptions before persistence; reject
secret-looking values in durable task/context artifacts; tests for token
rotation and nested structures. Redaction is defense in depth, not a guarantee
against arbitrary same-user code.

### T2. Confused-deputy tool use

**Paths:** untrusted repository text tells the model to invoke shell/network/Git
operations outside the user's intent.

**Controls:** tool calls are data, not executable text; strict schemas; explicit
per-task capabilities; separate inspection from mutation; immutable task
contract visible to the worker; log the request/tool/result correlation;
reject unknown tools and arguments. Capability checks reduce accidental misuse
but do not contain an allowed shell command.

### T3. Path traversal, symlinks, and repository escape

**Paths:** `../`, absolute paths, symlink swaps, alternate worktrees, Git config
or hook indirection, subprocess working-directory changes.

**Controls:** resolve paths at the point of use; compare against intended root;
use descriptor-relative APIs where possible; reject symlink/path identity
changes across validation/use; publish only expected refs with expected-old
CAS; preserve existing worktree-confinement and repository-integrity checks.
Any executed repository code can still access other user-readable paths.

### T4. Stale/duplicate worker publication

**Paths:** timeout races, retries, delayed child completion, warm-worker rebind,
PID reuse, duplicate IPC result.

**Controls:** session/task/request IDs, worker generation, operation idempotency
key, expected parent epoch, expected artifact/ref state, compare-and-swap
publication, one terminal result, and generation reset on warm-worker rebind.
A stale result is evidence, never authority to publish.

### T5. IPC injection or protocol confusion

**Paths:** repository code writes JSON to worker stdout, malformed/oversized
records, response for another request, diagnostic text in protocol stream.

**Controls:** bounded framing, object/schema validation, request correlation,
generation/lifecycle checks, malformed-record budget, stderr for diagnostics,
and fail-closed terminal-result rules. Treat stdout from child processes as
untrusted even when they were launched by Cambium.

### T6. Context/cache poisoning

**Paths:** malicious content enters a stable cache prefix, summary, semantic
memory, or parent checkpoint and is inherited by many children.

**Controls:** separate instructions from evidence; provenance every imported
fact; least-authority child projection; do not promote tool/repository text into
system/developer instructions; immutable epochs with correction by successor;
source-range/artifact binding; validation and held-out canaries for compaction;
explicit invalidation when trust policy changes.

A cache magnifies both useful context and poisoning. Cache hit rate is never a
reason to retain content that violates the current trust projection.

### T7. Summary omission or fabrication

**Paths:** compaction drops an unresolved constraint, invents a passed check, or
changes the meaning of a decision.

**Controls:** deterministic extraction before LLM synthesis; schema-constrained
summary; evidence references; preserve failed checks/open questions/verbatim
recent tail; raw history retained; summary publication uses CAS; quality tests
compare pre/post-compaction continuations. LLM summarization is not assumed
idempotent or authoritative.

### T8. Provider/cache privacy mismatch

**Paths:** sensitive prefix retained longer than intended, wrong cache namespace
or account, failover to a provider with different retention/regional policy,
cache identity collision.

**Controls:** provider capabilities include retention/isolation policy; cache
identity includes provider/model/protocol/namespace; sensitive tasks may disable
provider caching or require an approved provider class; failover re-runs policy
eligibility instead of preserving cache affinity at any cost; normalized usage
never exposes secret cache keys.

### T9. Supply-chain execution

**Paths:** package installation, test runners, compiler plugins, Git hooks,
editable dependencies, model-generated dependency changes.

**Controls:** avoid implicit installs; pin reviewed dependencies where the
project chooses to install them; expose dependency changes in the result;
prefer isolated/read-only evaluation copies for integrity of measured results;
record exact commands and revisions. Without external containment, a malicious
installed dependency has full user authority.

### T10. Denial of service and quota exhaustion

**Paths:** recursive subagent spawning, unbounded tool output, retry storms,
provider cascade, huge event payloads, slow terminal consumers, disk growth.

**Controls:** explicit recursion depth/fan-out, wall/token/output/process/queue
budgets; bounded IPC and renderer queues; no retry without typed policy and
budget debit; rate and concurrency controls with correct units; cancellation
and terminal checkpoints; storage retention/compaction policy that preserves
required evidence.

### T11. Billing/accounting manipulation

**Paths:** omitted usage fields, false cache misses/hits, duplicate usage events,
model substitution, cache-write cost ignored.

**Controls:** persist raw provider usage plus normalized fields; correlate one
usage record per attempted call; keep unknown distinct from zero; record actual
provider/model/protocol; separate input/output/cache-read/cache-write prices;
reconcile against provider invoices on samples.

## 7. Cache-first context specific rules

1. Provider KV cache is performance state and may disappear at any time.
2. Context checkpoints contain no provider credential or opaque secret cache
   handle unless a provider adapter explicitly protects and scopes it.
3. Child inheritance is a policy projection, not automatic transcript copying.
4. A trust-policy or tool-schema change creates a new cache identity/epoch.
5. A summary cannot upgrade untrusted evidence into trusted instructions.
6. Cross-provider/model reuse means semantic checkpoint replay, not assumed KV
   reuse.
7. Cache observation fields are safe metadata only; raw provider identifiers
   that reveal accounts are redacted or fingerprinted.

## 8. Residual risk accepted by the no-sandbox design

Even with every control above, an allowed shell/test/build command can execute
arbitrary code as the Cambium user. That code can bypass Cambium's redactor,
read same-user files, access the network, modify repositories outside the
worktree, or persist itself. Same-user local malware can read Cambium memory and
credentials.

Therefore the security claim is limited to **protocol/state integrity and
accidental capability reduction inside the harness**, not hostile-code
containment. High-risk or untrusted repositories require an external VM,
container, disposable account, or equivalent boundary selected by the
operator.

## 9. Verification matrix

- secrets rotated mid-session do not appear in later events/results;
- OAuth refresh tokens never enter worker environments;
- malicious stdout JSON cannot impersonate another request/generation;
- stale/duplicate child and worker results cannot publish refs or epochs;
- path/symlink race cases fail the existing worktree boundary;
- child context projections omit excluded secrets and unrelated siblings;
- compaction preserves unresolved constraints and evidence references;
- unknown cache fields remain unknown in routing evidence;
- tool/reasoning/schema changes invalidate cache identity;
- output, queue, fan-out, wall, token, and process limits hold under fuzzed
  schedules;
- usage totals reconcile without cache-token double counting;
- documentation and CLI never call worktrees/tool allowlists a sandbox.

## 10. Foundations

This model follows least privilege, complete mediation, fail-safe defaults, and
open-design principles from Saltzer and Schroeder. The tool layer is a
capability system only to the extent that capabilities are explicit and cannot
be forged; a general shell capability is intentionally broad. Generation/CAS
publication addresses classic stale-writer and ABA-style races. Context
provenance and monotonic merge rules follow the event-sourcing/MVCC model in
`docs/architecture/context-engine.md`.
