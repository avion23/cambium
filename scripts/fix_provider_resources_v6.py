#!/usr/bin/env python3
"""Focused corrections and invariants for provider resources v6."""

from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def insert_quality_validation() -> None:
    path = ROOT / "src" / "cambium" / "provider_scheduler.py"
    text = path.read_text(encoding="utf-8")
    if "provider quality_score must be in [0, 1]" in text:
        return
    marker = '''        for value in (
            self.price_per_1m_in,
'''
    block = '''        if not 0 <= self.quality_score <= 1:
            raise ValueError("provider quality_score must be in [0, 1]")
        if not self.task_classes:
            raise ValueError("provider task_classes must be non-empty")
'''
    if marker not in text:
        raise RuntimeError("ProviderPolicy validation marker missing")
    path.write_text(text.replace(marker, block + marker, 1), encoding="utf-8")


def fix_cli_smoke_order() -> None:
    path = ROOT / "tests" / "scenarios" / "test_cli_operator_smoke.py"
    if not path.is_file():
        return
    text = path.read_text(encoding="utf-8")
    if '    "quota",\n' not in text:
        marker = '    "optimize",\n'
        if marker in text:
            text = text.replace(marker, marker + '    "quota",\n', 1)
        else:
            marker = '    "tui",\n'
            if marker in text:
                text = text.replace(marker, marker + '    "quota",\n', 1)
    path.write_text(text, encoding="utf-8")


def validate() -> None:
    for relative in (
        "src/cambium/provider_resources.py",
        "src/cambium/provider_scheduler.py",
        "src/cambium/provider_config.py",
        "src/cambium/diffundo.py",
        "src/cambium/worker.py",
        "src/cambium/supervisor.py",
        "src/cambium/quota_cli.py",
        "src/cambium/cli.py",
        "src/cambium/render.py",
    ):
        path = ROOT / relative
        ast.parse(path.read_text(encoding="utf-8"), filename=str(path))


def main() -> None:
    insert_quality_validation()
    fix_cli_smoke_order()
    validate()


if __name__ == "__main__":
    main()
