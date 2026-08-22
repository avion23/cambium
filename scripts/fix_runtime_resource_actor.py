#!/usr/bin/env python3
"""Normalize and clean the retained runtime-resource materializer.

This file is source-controlled so the CI materialization path remains reviewable.
The materializer is intentionally applied only to the latest verified main.
Every generated source tree must pass the repository's unchanged static and test gates.
"""

from __future__ import annotations

import argparse
from pathlib import Path


def normalize_generator(path: Path) -> None:
    lines = path.read_text(encoding="utf-8").splitlines()
    result: list[str] = []
    skip_closing_fragment = False
    for line in lines:
        if skip_closing_fragment:
            skip_closing_fragment = False
            if line.strip() == '")':
                continue
        if 'if len(args) < 3: raise RuntimeError(f"unexpected run_tool signature:' in line:
            result.append(
                'if len(args) < 3: raise RuntimeError('
                'f"unexpected run_tool signature: {args}")'
            )
            if not line.rstrip().endswith('")'):
                skip_closing_fragment = True
            continue
        result.append(line)
    path.write_text("\n".join(result) + "\n", encoding="utf-8")


def pre_format(root: Path) -> None:
    test = root / "tests" / "scenarios" / "test_provider_resources.py"
    text = test.read_text(encoding="utf-8")
    marker = "# ruff: noqa: F403, F405\n"
    if marker not in text:
        text = text.replace(
            "from __future__ import annotations\n",
            "from __future__ import annotations\n\n" + marker,
            1,
        )
        test.write_text(text, encoding="utf-8")

    replacements = {
        root / "src" / "cambium" / "oauth.py": (
            "import threading as _cambium_threading",
            "import threading as _cambium_threading  # noqa: E402",
        ),
        root / "src" / "cambium" / "schemas.py": (
            "from .extensions import tool_extensions as _cambium_tool_extensions",
            "from .extensions import tool_extensions as _cambium_tool_extensions  # noqa: E402",
        ),
    }
    for path, (old, new) in replacements.items():
        source = path.read_text(encoding="utf-8")
        if old in source and new not in source:
            path.write_text(source.replace(old, new, 1), encoding="utf-8")


def _rewrite_lines(path: Path) -> None:
    source_lines = path.read_text(encoding="utf-8").splitlines()
    output: list[str] = []
    for line in source_lines:
        indent = line[: len(line) - len(line.lstrip())]
        if (
            "w.remaining_tokens if w.remaining_tokens is not None" in line
            and "w.limit_tokens if w.limit_tokens is not None" in line
        ):
            output.extend(
                (
                    indent + "(",
                    indent + '    f"{w.name}:"',
                    indent
                    + '    f"{w.remaining_tokens if w.remaining_tokens is not None else \'?\'}"',
                    indent + '    "/"',
                    indent
                    + '    f"{w.limit_tokens if w.limit_tokens is not None else \'?\'}"',
                    indent + ")",
                )
            )
            continue
        if (
            "profile.provider" in line
            and "profile.billing.value" in line
            and "quota={windows}" in line
        ):
            label = "quality" if " quality=" in line else "q"
            output.extend(
                (
                    indent + "(",
                    indent
                    + f'    f"{{profile.provider}} {{profile.billing.value}} {label}='
                    + '{profile.quality:.2f} "',
                    indent
                    + '    f"tps={profile.tokens_per_second:.1f} quota={windows}"',
                    indent + ")",
                )
            )
            continue
        if "Run a short Python snippet in an isolated interpreter process" in line:
            output.extend(
                (
                    indent + '"description": (',
                    indent
                    + '    "Run a short Python snippet in an isolated interpreter process "',
                    indent
                    + '    "in the worktree. Same host authority as run_shell; not a sandbox."',
                    indent + "),",
                )
            )
            continue
        output.append(line)
    path.write_text("\n".join(output) + "\n", encoding="utf-8")


def post_format(root: Path) -> None:
    for relative in (
        "src/cambium/monitor.py",
        "src/cambium/provider_resources.py",
        "src/cambium/schemas.py",
    ):
        _rewrite_lines(root / relative)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("phase", choices=("normalize", "pre", "post"))
    parser.add_argument("path", type=Path)
    args = parser.parse_args()
    if args.phase == "normalize":
        normalize_generator(args.path)
    elif args.phase == "pre":
        pre_format(args.path.resolve())
    else:
        post_format(args.path.resolve())


if __name__ == "__main__":
    main()
