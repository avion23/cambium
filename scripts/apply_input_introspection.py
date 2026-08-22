#!/usr/bin/env python3
from __future__ import annotations

import ast
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def write(path: str, text: str) -> None:
    target = ROOT / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text.rstrip() + "\n", encoding="utf-8")


def append_once(path: str, marker: str, text: str) -> None:
    target = ROOT / path
    source = target.read_text(encoding="utf-8")
    if marker not in source:
        target.write_text(source.rstrip() + "\n\n" + text.rstrip() + "\n", encoding="utf-8")


def add_import(path: str, statement: str) -> None:
    target = ROOT / path
    source = target.read_text(encoding="utf-8")
    if statement in source:
        return
    tree = ast.parse(source)
    imports = [node for node in tree.body if isinstance(node, (ast.Import, ast.ImportFrom))]
    if not imports:
        target.write_text(statement + "\n" + source, encoding="utf-8")
        return
    anchor = imports[-1]
    lines = source.splitlines(keepends=True)
    lines.insert(anchor.end_lineno or anchor.lineno, statement + "\n")
    target.write_text("".join(lines), encoding="utf-8")


def rename_top_level(path: str, old: str, new: str) -> None:
    target = ROOT / path
    source = target.read_text(encoding="utf-8")
    tree = ast.parse(source)
    nodes = [
        node
        for node in tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == old
    ]
    if len(nodes) != 1:
        raise RuntimeError(f"{path}: expected one {old}, found {len(nodes)}")
    node = nodes[0]
    lines = source.splitlines(keepends=True)
    lines[node.lineno - 1] = re.sub(rf"\b{re.escape(old)}\b", new, lines[node.lineno - 1], count=1)
    target.write_text("".join(lines), encoding="utf-8")


write("src/cambium/input_protocol.py", r'''"""Small paste-safe multiline protocol shared by the line REPL and TUI."""
from __future__ import annotations

from collections.abc import Iterable, Iterator

MAX_PROMPT_CHARS = 1_048_576
START = "<<<"
END = ">>>"


def iter_prompts(lines: Iterable[str]) -> Iterator[str]:
    """Yield ordinary lines or one verbatim block delimited by `<<<` / `>>>`."""
    block: list[str] | None = None
    size = 0
    for raw in lines:
        line = raw.rstrip("\r\n")
        if block is None:
            if line == START:
                block = []
                size = 0
                continue
            yield raw
            continue
        if line == END:
            yield "\n".join(block)
            block = None
            size = 0
            continue
        size += len(raw)
        if size > MAX_PROMPT_CHARS:
            raise ValueError("multiline prompt exceeds 1 MiB")
        block.append(line)
    if block is not None:
        raise ValueError("unterminated multiline prompt; close it with >>>")
''')

for frontend in ("src/cambium/repl.py", "src/cambium/tui.py"):
    add_import(frontend, "from .input_protocol import iter_prompts")
    path = ROOT / frontend
    source = path.read_text(encoding="utf-8")
    if "for line in iter_prompts(input_stream):" not in source:
        count = source.count("for line in input_stream:")
        if count != 1:
            raise RuntimeError(f"{frontend}: expected one input loop, found {count}")
        source = source.replace(
            "for line in input_stream:",
            "for line in iter_prompts(input_stream):",
            1,
        )
        path.write_text(source, encoding="utf-8")

interactive = ROOT / "src/cambium/interactive.py"
source = interactive.read_text(encoding="utf-8")
if "CAMBIUM_LOCAL_INTROSPECTION" not in source:
    rename_top_level(
        "src/cambium/interactive.py",
        "_interactive_local_command",
        "_interactive_local_command_base",
    )
    append_once("src/cambium/interactive.py", "CAMBIUM_LOCAL_INTROSPECTION", r'''# CAMBIUM_LOCAL_INTROSPECTION
async def _interactive_local_command(
    self: InteractiveSession,
    prompt: str,
) -> Any | None:
    from .supervisor import PlanResult, TaskResult

    command = prompt.strip()
    if command == "/branch":
        state = self.state
        summary = (
            f"session={state.session_root} checkpoint={state.checkpoint_ref or '?'} "
            f"epoch={state.epoch or 0} events={state.event_count}"
        )
    elif command == "/agents":
        states: dict[str, str] = {}
        terminal = {"result", "worker_failed", "child_rejected", "cancelled"}
        for event in self._events:
            task_id = event.get("task_id")
            if not isinstance(task_id, str) or not task_id:
                continue
            kind = str(event.get("kind") or event.get("type") or "event")
            payload = event.get("payload")
            if kind == "result" and isinstance(payload, dict):
                states[task_id] = str(payload.get("status") or "done")
            elif kind in terminal:
                states[task_id] = kind
            elif kind in {"task_assigned", "spawned", "ready", "run_task", "heartbeat", "tool_event", "usage_event"}:
                states[task_id] = "active"
        summary = "no agents" if not states else "\n".join(
            f"{task_id} {state}" for task_id, state in sorted(states.items())
        )
    elif command == "/context":
        shape: dict[str, Any] = {}
        for event in reversed(self._events):
            payload = event.get("payload")
            if not isinstance(payload, dict):
                continue
            candidate = payload.get("context_shape")
            if isinstance(candidate, dict):
                shape = candidate
                break
            keys = {
                "active_context_bytes",
                "active_context_messages",
                "summary_trunk_bytes",
                "summary_segments",
                "raw_tail_bytes",
                "prompt_tokens",
                "input_tokens",
                "cached_tokens",
            }
            selected = {key: payload[key] for key in keys if key in payload}
            if selected:
                shape = selected
                break
        summary = "no context metrics yet" if not shape else " ".join(
            f"{key}={shape[key]}" for key in sorted(shape)
        )
    else:
        return await _interactive_local_command_base(self, prompt)
    return PlanResult(
        (
            TaskResult(
                task_id="interactive-command",
                status="succeeded",
                exit_code=0,
                summary=summary,
            ),
        )
    )
''')

# The existing help response is extended without changing model behavior.
source = interactive.read_text(encoding="utf-8")
source = source.replace(
    'text = "/new /usage /quota /model /help /exit"',
    'text = "/new /branch /agents /context /usage /quota /model /help /exit"',
)
interactive.write_text(source, encoding="utf-8")

write("tests/scenarios/test_input_protocol.py", r'''from __future__ import annotations

import pytest

from cambium.input_protocol import iter_prompts


def test_regular_lines_remain_independent_prompts():
    assert list(iter_prompts(["one\n", "two\n"])) == ["one\n", "two\n"]


def test_multiline_block_is_one_verbatim_prompt():
    assert list(iter_prompts(["<<<\n", "one\n", "two\n", ">>>\n"])) == ["one\ntwo"]


def test_unterminated_block_fails_closed():
    with pytest.raises(ValueError, match="unterminated"):
        list(iter_prompts(["<<<\n", "one\n"]))
''')

write("tests/scenarios/test_interactive_introspection.py", r'''from __future__ import annotations

import asyncio
from pathlib import Path

from cambium import interactive
from cambium.oneshot import OneShotConfig


def test_local_branch_and_agent_commands_do_not_call_model(monkeypatch, tmp_path: Path):
    async def forbidden(*args, **kwargs):
        raise AssertionError("model called")

    monkeypatch.setattr(interactive.oneshot, "run_oneshot", forbidden)

    async def scenario():
        session = interactive.InteractiveSession(OneShotConfig(repo=tmp_path))
        branch = await session.submit("/branch")
        agents = await session.submit("/agents")
        context = await session.submit("/context")
        await session.close()
        return branch, agents, context

    branch, agents, context = asyncio.run(scenario())
    assert "checkpoint=?" in branch.results[0].summary
    assert agents.results[0].summary == "no agents"
    assert context.results[0].summary == "no context metrics yet"
''')

operator = ROOT / "docs/operator-runtime.md"
if operator.exists():
    text = operator.read_text(encoding="utf-8")
    if "Multiline prompts" not in text:
        text += '''

## Multiline prompts

Enter `<<<` on its own line, paste or type the complete prompt, then enter `>>>`
on its own line. The block is admitted as one immutable user message. The local
commands `/branch`, `/agents`, and `/context` inspect the active session without
calling an LLM.
'''
        operator.write_text(text, encoding="utf-8")

for candidate in (ROOT / "src").rglob("*.py"):
    ast.parse(candidate.read_text(encoding="utf-8"), filename=str(candidate))
print("multiline input and introspection applied")
