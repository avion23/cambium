#!/usr/bin/env python3
"""Focused correctness corrections for live dispatch integration."""

from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def allow_unknown_context_capacity() -> None:
    path = ROOT / "src" / "cambium" / "provider_scheduler.py"
    text = path.read_text(encoding="utf-8")
    old = '''    if request.required_context_tokens and (
        policy.context_window <= 0 or policy.context_window < request.required_context_tokens
    ):
        return False
'''
    new = '''    if (
        request.required_context_tokens
        and policy.context_window > 0
        and policy.context_window < request.required_context_tokens
    ):
        return False
'''
    if old in text:
        text = text.replace(old, new, 1)
    path.write_text(text, encoding="utf-8")


def validate() -> None:
    for relative in (
        "src/cambium/dispatch_policy.py",
        "src/cambium/event_tail.py",
        "src/cambium/diffundo.py",
        "src/cambium/monitor.py",
        "src/cambium/provider_scheduler.py",
    ):
        path = ROOT / relative
        ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    monitor = (ROOT / "src" / "cambium" / "monitor.py").read_text(encoding="utf-8")
    if "_event_tail.poll()" not in monitor or "_snapshot_cache.poll()" not in monitor:
        raise RuntimeError("monitor was not converted to incremental event tailing")


def main() -> None:
    allow_unknown_context_capacity()
    validate()


if __name__ == "__main__":
    main()
