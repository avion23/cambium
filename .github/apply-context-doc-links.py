# ruff: noqa: E501  # long lines are byte-exact patch anchors, must not wrap
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def replace_once(path: str, old: str, new: str) -> None:
    target = ROOT / path
    text = target.read_text(encoding="utf-8")
    count = text.count(old)
    if count != 1:
        raise RuntimeError(f"{path}: expected one match, found {count}")
    target.write_text(text.replace(old, new, 1), encoding="utf-8")


replace_once(
    "docs/architecture/context-engine.md",
    """Source and tests remain authoritative for current behavior.

## 1. Decision
""",
    """Source and tests remain authoritative for current behavior.

The recursive execution model, explicit `trunk`/`semantic`/`fresh` child
choices, provider placement, and on-demand raw branch recall are defined in
[`context-branches.md`](context-branches.md). CAST owns the active semantic
projection; it does not own the task tree, Git graph, or provider-cache
lineage.

## 1. Decision
""",
)

replace_once(
    "docs/architecture/subagents.md",
    """**Status:** implemented runtime contract. Source and tests remain authoritative.

Cambium does not use a provider-native “spawn agent” feature.
""",
    """**Status:** implemented runtime contract. Source and tests remain authoritative.

The broader recursive context model and the distinction between task tree,
conversation branches, Git graph, and cache lineage are defined in
[`context-branches.md`](context-branches.md). This document focuses on task
admission and fork-join execution.

Cambium does not use a provider-native “spawn agent” feature.
""",
)

replace_once(
    "docs/architecture/provider-routing.md",
    """## Root and child affinity

A Cambium child is a supervised task, not a provider-native subagent. See
[`subagents.md`](subagents.md).
""",
    """## Root and child affinity

A Cambium child is a supervised task, not a provider-native subagent. See
[`subagents.md`](subagents.md) and the explicit context/placement policy in
[`context-branches.md`](context-branches.md).
""",
)
