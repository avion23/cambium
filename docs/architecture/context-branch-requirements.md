# Branch invariants

This page is intentionally short. The live contract is split between:

- [context branches](context-branches.md) for delegation and context/placement policy;
- [subagents](subagents.md) for child lifetime, joins, publication and failure;
- [CAST](context-engine.md) for checkpoints and semantic context;
- [provider routing](provider-routing.md) for capacity, quota and placement resources;
- [context-branch reference](../reference/context-branches.md) for exact public values.

Keep only cross-cutting invariants here:

1. Task ancestry, conversation context, accepted Git state and provider-cache
   lineage are separate identities.
2. Model output proposes work; tools, provider responses and Git establish
   effects and evidence.
3. Published checkpoints and semantic entries are immutable. History remains
   available after projection changes.
4. A child result is not an accepted artifact. The supervisor owns admission,
   integration, parent resume and publication.
5. Explicit `trunk+spread` is invalid. An incompatible exact fork fails rather
   than silently becoming semantic.
6. Unknown cache, quota or throughput evidence stays unknown. Assignment does
   not prove which provider ultimately served a call.
7. Budget exhaustion without a terminal verdict is incomplete, not success.
8. Model `inspect_state`, CLI `inspect-state` and TUI `/inspect` share the
   recorded `BranchState`/`SituationFrame` view. WorkLedger and ResultCapsule-v2
   remain proposals.

Do not add a second policy layer here. When one of these invariants changes,
update its owning document and source/tests together.
