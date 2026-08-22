#!/usr/bin/env python3
"""Adapt the operator-upgrade generator to the latest upstream UI tree."""

from __future__ import annotations

import argparse
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BOOTSTRAP = ROOT / "scripts" / "apply_operator_upgrade.py"


def replace_once(text: str, old: str, new: str, label: str) -> str:
    count = text.count(old)
    if count != 1:
        raise RuntimeError(f"{label}: expected one match, found {count}")
    return text.replace(old, new, 1)


def prepare() -> None:
    text = BOOTSTRAP.read_text(encoding="utf-8")
    old = '''replace_once(
    "src/cambium/oauth.py",
    "        self._client_id = client_id\\n"
    "        self._issuer = validate_issuer(issuer)\\n",
    "        self._client_id = resolve_codex_client_id(client_id)\\n"
    "        self._issuer = validate_issuer(issuer)\\n",
    "device flow pinned client id",
)
'''
    new = '''replace_once(
    "src/cambium/oauth.py",
    "        self._provider = _validate_provider_id(provider)\\n"
    "        self._client_id = client_id\\n"
    "        self._issuer = validate_issuer(issuer)\\n",
    "        self._provider = _validate_provider_id(provider)\\n"
    "        self._client_id = resolve_codex_client_id(client_id)\\n"
    "        self._issuer = validate_issuer(issuer)\\n",
    "device flow pinned client id",
)
'''
    text = replace_once(text, old, new, "scope DeviceFlow client id")
    text = replace_once(
        text,
        'remove("tests/scenarios/test_render_stream.py")\n',
        "",
        "preserve upstream stream-render tests",
    )
    BOOTSTRAP.write_text(text, encoding="utf-8")


def finalize() -> None:
    tui_path = ROOT / "src" / "cambium" / "tui.py"
    text = tui_path.read_text(encoding="utf-8")
    text = replace_once(
        text,
        "from typing import Any\n\nfrom .monitor import AnsiDashboard\n",
        "from typing import Any\n\nfrom cambium.render_markdown import render_markdown_if_tty\n\n"
        "from .monitor import AnsiDashboard\n",
        "preserve upstream markdown renderer",
    )
    text = replace_once(
        text,
        '''            out.write(text)
            if not text.endswith("\\n"):
                out.write("\\n")
            try:
''',
        '''            out.write(text)
            if not text.endswith("\\n"):
                out.write("\\n")
            summaries = [
                entry.summary
                for entry in getattr(response, "results", ())
                if getattr(entry, "summary", None)
            ]
            if summaries:
                rendered_summaries = render_markdown_if_tty(
                    "\\n\\n".join(summaries), out
                )
                out.write(rendered_summaries)
                if not rendered_summaries.endswith("\\n"):
                    out.write("\\n")
            try:
''',
        "preserve upstream TUI markdown output",
    )
    tui_path.write_text(text, encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("phase", choices=("prepare", "finalize"))
    args = parser.parse_args()
    if args.phase == "prepare":
        prepare()
    else:
        finalize()


if __name__ == "__main__":
    main()
