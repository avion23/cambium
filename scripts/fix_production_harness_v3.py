#!/usr/bin/env python3
"""Focused corrections after the production-harness generator."""

from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def main() -> None:
    path = ROOT / "src" / "cambium" / "diffundo.py"
    text = path.read_text(encoding="utf-8")
    block = '''        self._provider_lease: ProviderLease | None = None
        self._quota_ledger = (
            QuotaLedger() if any(provider.quota_windows for provider in self._providers) else None
        )
'''
    count = text.count(block)
    if count != 1:
        raise RuntimeError(f"Diffundo quota-init block: expected one match, found {count}")
    text = text.replace(block, "", 1)
    tree = ast.parse(text)
    cls = next(
        node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == "Diffundo"
    )
    init = next(
        node
        for node in cls.body
        if isinstance(node, ast.FunctionDef) and node.name == "__init__"
    )
    assignment = None
    for node in ast.walk(init):
        if not isinstance(node, (ast.Assign, ast.AnnAssign)):
            continue
        targets = node.targets if isinstance(node, ast.Assign) else [node.target]
        if any(
            isinstance(target, ast.Attribute)
            and isinstance(target.value, ast.Name)
            and target.value.id == "self"
            and target.attr == "_providers"
            for target in targets
        ):
            assignment = node
            break
    if assignment is None or assignment.end_lineno is None:
        raise RuntimeError("Diffundo self._providers assignment not found")
    lines = text.splitlines(keepends=True)
    lines.insert(assignment.end_lineno, block)
    path.write_text("".join(lines), encoding="utf-8")


if __name__ == "__main__":
    main()
