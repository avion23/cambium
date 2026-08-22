#!/usr/bin/env python3
"""Focused compatibility corrections for the generated operator upgrade."""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def replace_once(path: str, old: str, new: str, label: str) -> None:
    target = ROOT / path
    text = target.read_text(encoding="utf-8")
    count = text.count(old)
    if count != 1:
        raise RuntimeError(f"{label}: expected one match, found {count}")
    target.write_text(text.replace(old, new, 1), encoding="utf-8")


def main() -> None:
    replace_once(
        "src/cambium/monitor.py",
        '''    for agent in snapshot.agents:
        parent = agent.parent_task_id or "-"
        lines.append(
            f"{agent.task_id:<24} {agent.state:<9} role={agent.role} "
''',
        '''    for agent in snapshot.agents:
        parent = agent.parent_task_id or "-"
        state = {
            "active": "running",
            "succeeded": "done",
        }.get(agent.state, agent.state)
        lines.append(
            f"{agent.task_id:<24} {state:<9} role={agent.role} "
''',
        "preserve established session-status labels",
    )
    replace_once(
        "src/cambium/ipc.py",
        "Framing rules implemented here (docs/research/ipc-protocol-draft.md §1):\n",
        "Framing rules implemented here (docs/architecture/architecture.md §5.1):\n",
        "IPC active architecture reference",
    )
    replace_once(
        "src/cambium/ipc.py",
        "MAX_LINE_BYTES = 1_048_576  # 1 MiB line cap (ipc-protocol-draft.md §1.4)\n",
        "MAX_LINE_BYTES = 1_048_576  # 1 MiB line cap; enforced before admission.\n",
        "IPC line-cap reference",
    )
    replace_once(
        "src/cambium/worker.py",
        "Speaks the Nuntius JSON-Lines wire protocol over stdio\n"
        "(docs/architecture.md §5, docs/research/ipc-protocol-draft.md). By default\n",
        "Speaks the Nuntius JSON-Lines wire protocol over stdio\n"
        "(`docs/architecture/architecture.md` §5). By default\n",
        "worker IPC active architecture reference",
    )
    replace_once(
        "src/cambium/doctor.py",
        '            "(provider-landscape.md §6)"\n',
        '            "(credential safety invariant)"\n',
        "doctor credential-safety reference",
    )
    replace_once(
        "src/cambium/merge.py",
        "Empirical findings from ``docs/research/worktree-concurrency.md`` are baked\n"
        "into the ordering:\n",
        "Measured Git worktree and reference behavior is baked into the ordering:\n",
        "merge active invariant reference",
    )
    replace_once(
        "docs/research/test-strategy.md",
        "not a test-count or current-status claim. Current authority is\n"
        "[`docs/architecture/architecture.md`](../architecture/architecture.md), source/tests,\n"
        "and [`v2-1-status.md`](v2-1-status.md).\n",
        "not a test-count or current-status claim. Current authority is\n"
        "[`docs/architecture/architecture.md`](../architecture/architecture.md)\n"
        "and source/tests.\n",
        "test-strategy active authority reference",
    )


if __name__ == "__main__":
    main()
