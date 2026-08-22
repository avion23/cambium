#!/usr/bin/env python3
"""Focused source-safe corrections after the production-harness generator."""

from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _write(path: Path, text: str) -> None:
    path.write_text(text, encoding="utf-8")


def _function_segment(text: str, name: str, class_name: str | None = None) -> tuple[int, int, str]:
    tree = ast.parse(text)
    scope = tree.body
    if class_name is not None:
        cls = next(
            (
                node
                for node in tree.body
                if isinstance(node, ast.ClassDef) and node.name == class_name
            ),
            None,
        )
        if cls is None:
            raise RuntimeError(f"class {class_name} not found")
        scope = cls.body
    node = next(
        (
            item
            for item in scope
            if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef))
            and item.name == name
        ),
        None,
    )
    if node is None or node.end_lineno is None:
        raise RuntimeError(f"function {name} not found")
    lines = text.splitlines(keepends=True)
    start = node.lineno - 1
    end = node.end_lineno
    return start, end, "".join(lines[start:end])


def _replace_in_function(
    path: Path,
    name: str,
    old: str,
    new: str,
    *,
    class_name: str | None = None,
) -> None:
    text = _read(path)
    start, end, segment = _function_segment(text, name, class_name)
    if new in segment:
        return
    count = segment.count(old)
    if count != 1:
        raise RuntimeError(f"{path.name}:{name}: expected one match, found {count}")
    replacement = segment.replace(old, new, 1)
    lines = text.splitlines(keepends=True)
    lines[start:end] = [replacement]
    _write(path, "".join(lines))


def _move_quota_initialization() -> None:
    path = ROOT / "src" / "cambium" / "diffundo.py"
    text = _read(path)
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
    _write(path, "".join(lines))


def _fix_native_tool_prompt_wiring() -> None:
    path = ROOT / "src" / "cambium" / "worker.py"

    # The generator previously replaced the first generic prompt return in the
    # module, which belongs to _build_forked_prompt and has no local `tools`.
    _replace_in_function(
        path,
        "_build_forked_prompt",
        '    return {"messages": messages, "tools": tools}\n',
        '    return {"messages": messages}\n',
    )
    _replace_in_function(
        path,
        "_build_agent_prompt",
        '    return {"messages": messages}\n',
        '    return {"messages": messages, "tools": tools}\n',
    )

    text = _read(path)
    _start, _end, fork_segment = _function_segment(text, "_fork_prompt")
    if "    tools: list[dict[str, Any]],\n" not in fork_segment:
        old = '''    continuation: list[dict[str, Any]],
) -> dict[str, Any]:
'''
        new = '''    continuation: list[dict[str, Any]],
    tools: list[dict[str, Any]],
) -> dict[str, Any]:
'''
        _replace_in_function(path, "_fork_prompt", old, new)
    _replace_in_function(
        path,
        "_fork_prompt",
        '    return {"messages": messages}\n',
        '    return {"messages": messages, "tools": tools}\n',
    )

    text = _read(path)
    old_call = "prompt = _fork_prompt(base_messages, context_continuation)"
    new_call = "prompt = _fork_prompt(base_messages, context_continuation, tools)"
    if new_call not in text:
        count = text.count(old_call)
        if count != 1:
            raise RuntimeError(f"worker fork prompt call: expected one match, found {count}")
        _write(path, text.replace(old_call, new_call, 1))


def _ensure_oauth_regex_import() -> None:
    path = ROOT / "src" / "cambium" / "oauth.py"
    text = _read(path)
    if "\nimport re\n" in text:
        return
    marker = "import os\n"
    if text.count(marker) != 1:
        raise RuntimeError("oauth import marker mismatch")
    _write(path, text.replace(marker, marker + "import re\n", 1))


def main() -> None:
    _move_quota_initialization()
    _fix_native_tool_prompt_wiring()
    _ensure_oauth_regex_import()


if __name__ == "__main__":
    main()
