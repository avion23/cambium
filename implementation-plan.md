# Open work

Implemented behavior belongs in the [runtime map](docs/architecture/architecture.md),
[CAST model](docs/architecture/context-engine.md),
[delegation contract](docs/architecture/context-branches.md) and
[prompt experiments](docs/architecture/optimization.md). Do not duplicate those
contracts here or add a prerequisite framework before useful work can run.

## Improve measured completion

The normal model chooses delegation. The worker completes policy defaults and
the supervisor supplies child workspaces and provider placement. Real runs can
still fail through malformed model actions, provider unavailability, expensive
coordination or exhausted budgets. Use the existing benchmark reports and
transcripts to distinguish these causes. Do not count requested or assigned
providers as proof of actual multi-provider execution.

Build a broader frozen task corpus and run the prepared GEPA experiment. The
coding and summary policies now deploy automatically for new sessions, but no
population-wide quality or efficiency gain has been established. Starter cases
are insufficient for claims about long-session memory or general coding.

## Context quality

CAST appends semantic deltas and retains raw history. K0's text-based compiler
cannot infer arbitrary semantic contradictions or automatically close every
old open item. Improve the smallest observed lost-obligation or stale-fact
case before introducing a typed WorkLedger or richer ResultCapsule.

`BranchState` and CLI inspection exist. Shared model/operator state and the
complete SituationFrame remain separate integration work, including the
parallel checkout. Do not absorb its uncommitted changes. Reuse the existing
sources and reducer rather than introducing another memory store.

## Provider resources

Admission, call-time fallback and actual serving usage are distinct. Measure
accepted work and quota consumption over real account windows. Current decayed
routing debt is a balancing heuristic, not a weekly-entitlement model. Keep
request rate, concurrent capacity, context/cache affinity and cash separate.
Do not add speculative preflight calls or permanent quarantine rules to hide
ordinary provider failures.

## Terminal usability

Compact lane/provider state, CAST context and resource rows are implemented.
Input editing still uses the native line editor; full geometry repaint can be
deferred while editing to avoid cross-thread native buffer access. Replacing
that ownership boundary requires real PTY resize/paste/cancel/reconnect checks,
not more flags or another frontend state store. Do not describe the current
interface as universally best-in-class on the basis of passing render tests.

## Completion discipline

Fix a reproduced cause, retain one useful regression, run affected checks and
commit/integrate the result. Remove repeated fixtures and source-pinning tests,
not checks for meaningful failures. A shell command returning zero does not
certify a whole task. A budget ending without a finish verdict is incomplete.
Report live-provider failures alongside passing runs rather than rerunning
until only favorable evidence remains.
