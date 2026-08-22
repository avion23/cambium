#!/usr/bin/env python3
"""Run the production-harness corrections with a source-derived candidate patch."""

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

    indent = segment[exact_index][: len(segment[exact_index]) - len(segment[exact_index].lstrip())]
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


def main() -> None:
    _patch_candidate_policy()
    fixer = _load_fixer()
    fixer._fix_model_candidate_policy = lambda: None
    fixer.main()


if __name__ == "__main__":
    main()
