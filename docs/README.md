# Cambium docs index

Index for the `docs/` tree. Source and tests establish current behavior; active
contracts state intended invariants and explicitly identify target-only parts.

## `docs/architecture/`

- [`architecture.md`](architecture/architecture.md) — current-versus-target
  system contract.
- [`context-engine.md`](architecture/context-engine.md) — cache-first immutable
  context epochs, branch/merge, compaction, and accounting contract.
- [`provider-routing.md`](architecture/provider-routing.md) — provider/model
  feasibility, routing objective, cache affinity, and capacity contract.
- [`terminal-interface.md`](architecture/terminal-interface.md) — durable
  OpenCode/pi-style session UI and token/cost presentation contract.
- [`system-design.md`](architecture/system-design.md) — historical v0.1.0
  pre-implementation design and review record; not runtime authority.
- [`user-cli.md`](architecture/user-cli.md) — current CLI behavior; source and
  scenario tests remain authoritative.
- [`module-template/`](architecture/module-template/) — normative module
  template for modules under `src/cambium/modules/`.
- [`reviews/`](architecture/reviews/) — historical pre-implementation reviews.

## `docs/security/`

- [`threat-model.md`](security/threat-model.md) — active trust model for the
  deliberate no-sandbox architecture, including cache/context poisoning and
  residual same-user-code risk.

## `docs/research/`

Experiments, measured evidence, corrected hypotheses, competitive snapshots,
and design history. See [`research/README.md`](research/README.md) for authority
order and the complete file index.
