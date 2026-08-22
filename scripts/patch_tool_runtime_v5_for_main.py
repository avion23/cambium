#!/usr/bin/env python3
"""Adapt the retained tool-runtime materializer to the current worker shape."""

from __future__ import annotations

from pathlib import Path


path = Path(__file__).with_name("apply_tool_runtime_v5.py")
text = path.read_text(encoding="utf-8")
needle = '''        if not changed:
            raise RuntimeError("worker TOOL_SCHEMAS assignment not found")
'''
replacement = '''        if not changed:
            exposed = (
                "    schemas: list[dict[str, Any]] = []\\n"
                "    for schema in TOOL_SCHEMAS:\\n"
            )
            registered = (
                "    tool_registry = registry_from_schemas(TOOL_SCHEMAS)\\n"
                "    schemas: list[dict[str, Any]] = []\\n"
                "    for schema in tool_registry.schemas:\\n"
            )
            if exposed not in text:
                raise RuntimeError("worker tool exposure loop not found")
            text = text.replace(exposed, registered, 1)
            changed = True
'''
if needle not in text:
    raise RuntimeError("tool-runtime compatibility insertion point not found")
text = text.replace(needle, replacement, 1)

fixture = '"class Alpha:\\n    pass\\n\\ndef beta():\\n    return Alpha()\\n",'
escaped_fixture = '"class Alpha:\\\\n    pass\\\\n\\\\ndef beta():\\\\n    return Alpha()\\\\n",'
if fixture not in text:
    raise RuntimeError("generated navigation fixture not found")
text = text.replace(fixture, escaped_fixture, 1)

single_line_fixture = 'target.write_text("x = 1\\n", encoding="utf-8")'
escaped_single_line_fixture = 'target.write_text("x = 1\\\\n", encoding="utf-8")'
if single_line_fixture not in text:
    raise RuntimeError("generated LSP fixture not found")
text = text.replace(single_line_fixture, escaped_single_line_fixture, 1)

path.write_text(text, encoding="utf-8")
