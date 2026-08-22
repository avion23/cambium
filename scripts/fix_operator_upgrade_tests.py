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
        state = "running" if agent.state == "active" else agent.state
        lines.append(
            f"{agent.task_id:<24} {state:<9} role={agent.role} "
''',
        "preserve session-status running label",
    )


if __name__ == "__main__":
    main()
