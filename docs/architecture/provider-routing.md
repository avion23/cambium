# Provider routing

## Problems

1. Providers fail heterogeneously: authentication, quota, configuration, stalls, overload, and content policy require different responses; naive retries treat them alike, burn budget, or lose tasks.

2. Every provider eventually has an outage, so single-provider operation is unacceptable for work that must complete.

3. Switching providers invalidates prompt-cache affinity and may fork context unless task identity and progress move with the switch.

4. Large code contexts can trigger moderation false positives on benign tasks, turning a provider-specific interpretation into an avoidable task failure.

5. Deep-reasoning calls legitimately exceed naive timeouts, so a fixed short deadline mistakes slow useful work for failure.

6. Credential-less lanes waste attempts and pollute health statistics when missing credentials are discovered only during a call.

## Design

- Admission constructs an ordered cascade with a fast tier followed by a core tier. It filters by task capabilities, model constraints, lane capacity, and credential feasibility before dispatch; credential feasibility is decided at admission, never at call time.
- Each lane uses an evidence-based health state machine: open admits work, cooldown withholds transiently, half-open probe permits one test, and disable quarantines proven auth/config failures. Direct success, quota, stall, overload, transport, and endpoint evidence drive distinct transitions; health is never inferred from another lane's result.
- A typed failure taxonomy separates auth, configuration, quota, transient stall/overload, timeout, terminal endpoint death, and content-policy flag. Auth/config failures quarantine; quota and transient failures use bounded jittered backoff and then cascade; content flags cascade without health damage and return a caller-recoverable signal.
- A provider lease pins a semantic task branch to its provider/model for cache affinity. Release it only on terminal death or timeout; failover carries the stable cache identity and latest checkpoint, preserves a pinned model, and permits model substitution only when the caller explicitly allows it.
- Each attempt receives an effort-aware deadline derived from remaining time under a hard task wall; reasoning effort can lengthen an attempt, but backoff and retries can never overrun the wall. Budget charging is uncached-only: count fresh input and newly generated output, not reused cached work.
- Persist checkpoints at safe progress boundaries and resume from the latest checkpoint after a stall or failover, so completed work is not replayed and side effects are not duplicated.

## Invariants

- Provider health changes only on direct evidence.
- Content flags never damage health.
- Pinned-model tasks never silently switch model on transient failure.
- Every failover preserves task progress via checkpoints.
- Admission never spawns a lane that cannot authenticate.

## Violations this design prevents

- **Problems 1–2:** Typed outcomes, the ordered tiered cascade, and evidence-only health transitions replace blind retry and single-provider dependence.
- **Problem 3:** Lease/cache identity and checkpoint preservation uphold the pinned-model and failover-progress invariants, so switching serves the same branch instead of forking it.
- **Problem 4:** Caller-recoverable content flags never damage health, preventing benign code context from quarantining a provider or ending the task.
- **Problem 5:** Effort-aware deadlines inside a hard task wall distinguish legitimate slow reasoning from an overrun; direct-evidence health rules prevent timeout guesses from changing health.
- **Problem 6:** The admission-auth invariant excludes credential-less lanes before dispatch, so missing credentials create no attempts and no misleading health data.
