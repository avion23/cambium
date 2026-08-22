# Engineering constitution

Cambium is a correctness-first concurrent systems harness.

## Rules

1. **Single ownership.** Each mutable state domain has one owner: worker,
   supervisor, event-store writer, merge publisher, or session reducer.
2. **Messages over shared mutation.** Processes and actors communicate through
   bounded, validated messages. Cross-thread shared mutable state is avoided.
3. **Functional core, imperative shell.** Parsing, validation, routing,
   reducers, and metrics are pure where practical; process, Git, network, and
   persistence effects stay at explicit boundaries.
4. **Event sourcing for observation.** User interfaces and recovery projections
   derive from durable ordered facts, never from ad-hoc inspection of live
   objects.
5. **Fail closed at trust boundaries.** Unknown wire fields, malformed
   credentials, corrupt checkpoints, stale generations, and unsafe paths are
   rejected before side effects.
6. **Append-only context.** Summary segments cover disjoint raw ranges once.
   Existing summaries and published epochs are immutable.
7. **Mechanical sympathy follows measurement.** Optimize the measured
   bottleneck. Provider RTT and model latency dominate microsecond Python
   bookkeeping; worker cold-start evidence justifies pooling.
8. **Data-oriented hot paths.** Keep event and routing records flat,
   canonical, bounded, and cheap to validate.
9. **Backpressure is explicit.** Critical events are never silently dropped.
   Noncritical coalescing/dropping is typed and observable.
10. **Publication is transactional.** Verify and publish under fencing and
    expected-old references. A successful model response is not publication.
11. **Tests defend invariants, not implementation trivia.** Retain boundary,
    replay, cancellation, accounting, fencing, auth, and corruption tests.
    Consolidate duplicated formatting and fixture tests.
12. **Unknown is not zero.** Missing price, token, cache, latency, and context
    measurements remain unknown or approximate and are labeled accordingly.

## Formal mappings

- Actor model / CSP: supervisor, workers, store writer, and monitor reducer.
- State machines: worker lifecycle, task lifecycle, context epochs, OAuth
  refresh, and merge publication.
- Event sourcing and CQRS: durable events plus read-only projections.
- Linearizability: fenced generation changes and expected-old Git ref updates.
- Write-ahead logging: SQLite WAL event persistence.
- Content addressing: immutable context checkpoints.
- Map-reduce/fold: usage and observability aggregation.
