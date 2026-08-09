# Sandboxing options research (superseded)

**Status:** SUPERSEDED — 2026-08-09, user directive: sandboxing is removed from the harness scope and the sandbox tool is removed from the project. The full research (503 lines) is preserved in git history at commit `242a509`.

One factual finding remains relevant (container-deployment note in `docs/research/feedback-2-deltas.md` D8e): **unprivileged user namespaces are blocked by AppArmor on this host** (`kernel.apparmor_restrict_unprivileged_userns=1`).
