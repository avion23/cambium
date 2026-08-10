# Sandboxing options research (superseded)

**Status:** SUPERSEDED — 2026-08-09. Historical Septum/bubblewrap research was
removed from the harness scope; the full record remains in git history at
`242a509`.

**Runtime boundary:** The current runtime has no per-worker OS containment.
Host deployment may use a whole-session systemd cgroup, but that is a deployment
boundary, not repository evidence of task isolation. Host note
(`docs/research/feedback-2-deltas.md` D8e): AppArmor blocks unprivileged user
namespaces here (`kernel.apparmor_restrict_unprivileged_userns=1`).
