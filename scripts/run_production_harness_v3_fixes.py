#!/usr/bin/env python3
"""Run the production-harness corrections with source-derived patches."""

from __future__ import annotations

import ast
import importlib.util
from pathlib import Path
from types import ModuleType

ROOT = Path(__file__).resolve().parents[1]


def _patch_candidate_policy() -> None:
    path = ROOT / "src" / "cambium" / "diffundo.py"
    text = path.read_text(encoding="utf-8")
    tree = ast.parse(text)
    cls = next(
        node
        for node in tree.body
        if isinstance(node, ast.ClassDef) and node.name == "Diffundo"
    )
    function = next(
        node
        for node in cls.body
        if isinstance(node, ast.FunctionDef) and node.name == "_candidates"
    )
    if function.end_lineno is None:
        raise RuntimeError("Diffundo._candidates has no end line")

    lines = text.splitlines(keepends=True)
    start = function.lineno - 1
    end = function.end_lineno
    segment = lines[start:end]
    if any("substitutes = [" in line for line in segment):
        return

    exact_index = next(
        (
            index
            for index, line in enumerate(segment)
            if "exact = [provider for provider in candidates" in line
        ),
        None,
    )
    assignment_index = next(
        (
            index
            for index, line in enumerate(segment)
            if exact_index is not None
            and index > exact_index
            and line.strip() == "candidates = exact"
        ),
        None,
    )
    if exact_index is None or assignment_index is None:
        raise RuntimeError("generated model-candidate block not found")

    indent = segment[exact_index][
        : len(segment[exact_index]) - len(segment[exact_index].lstrip())
    ]
    replacement = [
        f"{indent}exact = [provider for provider in candidates if provider.model == requested_model]\n",
        f"{indent}substitutes = [\n",
        f"{indent}    provider\n",
        f"{indent}    for provider in candidates\n",
        f"{indent}    if provider.model != requested_model\n",
        f"{indent}    and provider.allow_model_substitution\n",
        f"{indent}]\n",
        f"{indent}candidates = [*exact, *substitutes]\n",
    ]
    segment[exact_index : assignment_index + 1] = replacement
    lines[start:end] = segment
    path.write_text("".join(lines), encoding="utf-8")


def _load_fixer() -> ModuleType:
    path = ROOT / "scripts" / "fix_production_harness_v3.py"
    spec = importlib.util.spec_from_file_location("production_harness_v3_fixer", path)
    if spec is None or spec.loader is None:
        raise RuntimeError("unable to load production-harness fixer")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _normalize_scheduler_source() -> None:
    path = ROOT / "src" / "cambium" / "provider_scheduler.py"
    text = path.read_text(encoding="utf-8")
    replacements = {
        '                    "used_tokens=excluded.used_tokens, allowance_requests=excluded.allowance_requests, "\n': (
            '                    "used_tokens=excluded.used_tokens, "\n'
            '                    "allowance_requests=excluded.allowance_requests, "\n'
        ),
        '                    "used_requests=excluded.used_requests, reserve_fraction=excluded.reserve_fraction, "\n': (
            '                    "used_requests=excluded.used_requests, "\n'
            '                    "reserve_fraction=excluded.reserve_fraction, "\n'
        ),
        '        used_requests = 0 if remaining_requests is None else max(0, allowance_requests - remaining_requests)\n': (
            '        used_requests = (\n'
            '            0\n'
            '            if remaining_requests is None\n'
            '            else max(0, allowance_requests - remaining_requests)\n'
            '        )\n'
        ),
        '                    windows = () if self._ledger is None else await asyncio.to_thread(self._ledger.snapshots)\n': (
            '                    windows = (\n'
            '                        ()\n'
            '                        if self._ledger is None\n'
            '                        else await asyncio.to_thread(self._ledger.snapshots)\n'
            '                    )\n'
        ),
    }
    for old, new in replacements.items():
        text = text.replace(old, new)
    path.write_text(text, encoding="utf-8")


def _normalize_tool_dispatch() -> None:
    path = ROOT / "src" / "cambium" / "tools.py"
    text = path.read_text(encoding="utf-8")
    tree = ast.parse(text)
    function = next(
        (
            node
            for node in tree.body
            if isinstance(node, ast.AsyncFunctionDef) and node.name == "run_tool"
        ),
        None,
    )
    if function is None or function.end_lineno is None:
        raise RuntimeError("generated async run_tool wrapper not found")
    lines = text.splitlines(keepends=True)
    start = function.lineno - 1
    end = function.end_lineno
    segment = "".join(lines[start:end])
    marker = "return _run_tool_without_python("
    count = segment.count(marker)
    if count != 2:
        raise RuntimeError(
            f"generated run_tool fallback count mismatch: expected 2, found {count}"
        )
    segment = segment.replace(marker, "return await _run_tool_without_python(")
    lines[start:end] = [segment]
    path.write_text("".join(lines), encoding="utf-8")


def _normalize_render_tests() -> None:
    path = ROOT / "tests" / "scenarios" / "test_render_stream.py"
    text = path.read_text(encoding="utf-8")
    text = text.replace("\nimport signal\n", "\n")
    import_marker = "import shutil\n"
    if import_marker not in text:
        raise RuntimeError("test_render_stream import marker missing")
    text = text.replace(import_marker, import_marker + "import signal\n", 1)
    text = text.replace(
        '        on_event({"kind": "tool_event", "payload": {"tool": "run_shell", "cmd": "df -h", "ok": True, "duration_ms": 5}})\n',
        '''        on_event(
            {
                "kind": "tool_event",
                "payload": {
                    "tool": "run_shell",
                    "cmd": "df -h",
                    "ok": True,
                    "duration_ms": 5,
                },
            }
        )
''',
    )
    path.write_text(text, encoding="utf-8")


def _normalize_repl_tests() -> None:
    path = ROOT / "tests" / "scenarios" / "test_repl_usage.py"
    text = path.read_text(encoding="utf-8")
    text = text.replace(
        '    monkeypatch.setattr(repl.render, "render_event_line", lambda _record, stream=None: "usage event")\n',
        '''    monkeypatch.setattr(
        repl.render,
        "render_event_line",
        lambda _record, stream=None: "usage event",
    )
''',
    )
    path.write_text(text, encoding="utf-8")


def main() -> None:
    _patch_candidate_policy()
    fixer = _load_fixer()
    fixer._fix_model_candidate_policy = lambda: None
    fixer._fix_generated_line_lengths = lambda: None
    fixer.main()
    _normalize_scheduler_source()
    _normalize_tool_dispatch()
    _normalize_render_tests()
    _normalize_repl_tests()


if __name__ == "__main__":
    main()
