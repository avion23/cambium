#!/usr/bin/env python3
"""Focused source-compatible corrections for the hybrid tool upgrade."""

from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def add_import(path: Path, statement: str) -> None:
    text = path.read_text(encoding="utf-8")
    if statement in text:
        return
    marker = "from __future__ import annotations\n"
    if marker not in text:
        raise RuntimeError(f"{path}: future import marker missing")
    path.write_text(text.replace(marker, marker + "\n" + statement + "\n", 1), encoding="utf-8")


def fix_permission_field() -> None:
    path = ROOT / "src" / "cambium" / "tools.py"
    text = path.read_text(encoding="utf-8")
    tree = ast.parse(text)
    cls = next(
        node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == "ToolPermissionPolicy"
    )
    fields = [node for node in cls.body if isinstance(node, ast.AnnAssign)]
    python_field = next(
        node
        for node in fields
        if isinstance(node.target, ast.Name) and node.target.id == "allow_python"
    )
    later_required = [
        node
        for node in fields
        if node.lineno > python_field.lineno and node.value is None
    ]
    if later_required:
        lines = text.splitlines(keepends=True)
        removed = lines.pop(python_field.lineno - 1)
        tree = ast.parse("".join(lines))
        cls = next(
            node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == "ToolPermissionPolicy"
        )
        fields = [node for node in cls.body if isinstance(node, ast.AnnAssign)]
        last = fields[-1]
        lines.insert(last.end_lineno or last.lineno, removed)
        path.write_text("".join(lines), encoding="utf-8")
    for statement in ("import json", "import os", "import subprocess", "import sys", "from pathlib import Path"):
        add_import(path, statement)


def fix_safe_homes() -> None:
    lsp = ROOT / "src" / "cambium" / "lsp_query.py"
    text = lsp.read_text(encoding="utf-8")
    text = text.replace(
        "def _safe_environment() -> dict[str, str]:\n    return {\n",
        "def _safe_environment(worktree: Path) -> dict[str, str]:\n"
        "    home = worktree / \".cambium\" / \"lsp-home\"\n"
        "    home.mkdir(parents=True, exist_ok=True, mode=0o700)\n"
        "    environment = {\n",
        1,
    )
    text = text.replace(
        "        if key in _ENV_ALLOWLIST and isinstance(value, str)\n    }\n",
        "        if key in _ENV_ALLOWLIST and key != \"HOME\" and isinstance(value, str)\n"
        "    }\n"
        "    environment[\"HOME\"] = str(home)\n"
        "    return environment\n",
        1,
    )
    text = text.replace("env=_safe_environment(),", "env=_safe_environment(worktree),", 1)
    lsp.write_text(text, encoding="utf-8")

    tools = ROOT / "src" / "cambium" / "tools.py"
    text = tools.read_text(encoding="utf-8")
    old = '''        safe_env = {
            key: value
            for key, value in os.environ.items()
            if key in {"PATH", "HOME", "LANG", "LC_ALL", "TMPDIR"}
        }
'''
    new = '''        scratch_home = worktree / ".cambium" / "python-home"
        scratch_home.mkdir(parents=True, exist_ok=True, mode=0o700)
        safe_env = {
            key: value
            for key, value in os.environ.items()
            if key in {"PATH", "LANG", "LC_ALL", "TMPDIR"}
        }
        safe_env["HOME"] = str(scratch_home)
'''
    if old in text:
        text = text.replace(old, new, 1)
    tools.write_text(text, encoding="utf-8")


def fix_cli() -> None:
    path = ROOT / "src" / "cambium" / "cli.py"
    text = path.read_text(encoding="utf-8")
    text = text.replace("allow_shell=args.allow_shell,,", "allow_shell=args.allow_shell,")
    text = text.replace(
        'allow_shell=getattr(args, "allow_shell", False),,',
        'allow_shell=getattr(args, "allow_shell", False),',
    )
    path.write_text(text, encoding="utf-8")

    smoke = ROOT / "tests" / "scenarios" / "test_cli_operator_smoke.py"
    if smoke.is_file():
        text = smoke.read_text(encoding="utf-8")
        marker = '    "optimize",\n'
        if '    "quota",\n' not in text and marker in text:
            text = text.replace(marker, marker + '    "quota",\n', 1)
        elif '    "quota",\n' not in text:
            marker = '    "tui",\n'
            if marker in text:
                text = text.replace(marker, marker + '    "quota",\n', 1)
        smoke.write_text(text, encoding="utf-8")


def validate_generated_python() -> None:
    for relative in (
        "src/cambium/tools.py",
        "src/cambium/lsp_query.py",
        "src/cambium/worker.py",
        "src/cambium/diffundo.py",
        "src/cambium/cli.py",
        "src/cambium/oneshot.py",
    ):
        path = ROOT / relative
        ast.parse(path.read_text(encoding="utf-8"), filename=str(path))


def main() -> None:
    fix_permission_field()
    fix_safe_homes()
    fix_cli()
    validate_generated_python()


if __name__ == "__main__":
    main()
