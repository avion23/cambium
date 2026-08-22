#!/usr/bin/env python3
from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def write(path: str, text: str) -> None:
    target = ROOT / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text.rstrip() + "\n", encoding="utf-8")


def line_offsets(source: str) -> list[int]:
    offsets: list[int] = []
    cursor = 0
    for line in source.splitlines(keepends=True):
        offsets.append(cursor)
        cursor += len(line)
    return offsets


def insert_after(source: str, node: ast.AST, text: str) -> str:
    offsets = line_offsets(source)
    end_line = node.end_lineno or node.lineno
    end = offsets[end_line - 1] + len(source.splitlines(keepends=True)[end_line - 1])
    return source[:end] + text + source[end:]


def add_config_fields() -> None:
    path = ROOT / "src/cambium/oneshot.py"
    source = path.read_text(encoding="utf-8")
    tree = ast.parse(source)
    cls = next((node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == "OneShotConfig"), None)
    if cls is None:
        raise RuntimeError("OneShotConfig missing")
    fields = [node for node in cls.body if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name)]
    names = {node.target.id for node in fields}
    if "resume_checkpoint_ref" in names and "resume_epoch" in names:
        return
    anchor = fields[-1] if fields else cls.body[-1]
    indent = " " * (cls.col_offset + 4)
    source = insert_after(
        source,
        anchor,
        f"{indent}resume_checkpoint_ref: str | None = None\n"
        f"{indent}resume_epoch: int | None = None\n",
    )
    ast.parse(source)
    path.write_text(source, encoding="utf-8")


def _contains_prompt(node: ast.AST) -> bool:
    return any(
        isinstance(item, ast.Attribute)
        and isinstance(item.value, ast.Name)
        and item.value.id == "config"
        and item.attr == "prompt"
        for item in ast.walk(node)
    )


def add_resume_to_task_spec() -> None:
    path = ROOT / "src/cambium/oneshot.py"
    source = path.read_text(encoding="utf-8")
    if "resume_checkpoint_ref" in source and "child_results_truncated" in source:
        return
    tree = ast.parse(source)
    assignments: list[tuple[ast.Assign | ast.AnnAssign, str]] = []
    for node in ast.walk(tree):
        value = node.value if isinstance(node, (ast.Assign, ast.AnnAssign)) else None
        if not isinstance(value, ast.Dict) or not _contains_prompt(value):
            continue
        target: ast.expr | None = None
        if isinstance(node, ast.Assign) and len(node.targets) == 1:
            target = node.targets[0]
        elif isinstance(node, ast.AnnAssign):
            target = node.target
        if isinstance(target, ast.Name):
            assignments.append((node, target.id))
    if len(assignments) != 1:
        raise RuntimeError(f"expected one assigned one-shot task spec, found {len(assignments)}")
    node, name = assignments[0]
    indent = " " * node.col_offset
    addition = (
        f"{indent}if config.resume_checkpoint_ref is not None and config.resume_epoch is not None:\n"
        f"{indent}    {name}[\"resume\"] = {{\n"
        f"{indent}        \"checkpoint_ref\": config.resume_checkpoint_ref,\n"
        f"{indent}        \"epoch\": config.resume_epoch,\n"
        f"{indent}        \"child_results\": [],\n"
        f"{indent}        \"child_results_truncated\": False,\n"
        f"{indent}    }}\n"
    )
    source = insert_after(source, node, addition)
    ast.parse(source)
    path.write_text(source, encoding="utf-8")


write("src/cambium/interactive.py", r'''"""Mailbox-owned durable conversation branch shared by REPL and TUI."""
from __future__ import annotations

import time
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Callable

from . import oneshot
from .mailbox import MailboxActor


@dataclass(frozen=True, slots=True)
class Submit:
    prompt: str
    on_event: Callable[[dict[str, Any]], None] | None = None


@dataclass(frozen=True, slots=True)
class Reset:
    pass


@dataclass(frozen=True, slots=True)
class InteractiveState:
    session_root: Path
    checkpoint_ref: str | None
    epoch: int | None
    event_count: int


class InteractiveSession:
    """Single writer for one branch head; provider calls remain asynchronous."""

    def __init__(self, config: oneshot.OneShotConfig, *, capacity: int = 16) -> None:
        root = _session_root(config)
        self._config = replace(config, session_root=root)
        self._checkpoint_ref: str | None = None
        self._epoch: int | None = None
        self._events: list[dict[str, Any]] = []
        self._mailbox: MailboxActor[Submit | Reset, Any] = MailboxActor(
            self._handle,
            capacity=capacity,
            name="interactive-session",
        )

    @property
    def events(self) -> tuple[dict[str, Any], ...]:
        return tuple(self._events)

    @property
    def state(self) -> InteractiveState:
        return InteractiveState(
            Path(self._config.session_root),
            self._checkpoint_ref,
            self._epoch,
            len(self._events),
        )

    async def submit(
        self,
        prompt: str,
        *,
        on_event: Callable[[dict[str, Any]], None] | None = None,
    ) -> Any:
        return await self._mailbox.ask(Submit(prompt, on_event))

    async def reset(self) -> InteractiveState:
        return await self._mailbox.ask(Reset())

    async def close(self) -> None:
        await self._mailbox.close()

    async def _handle(self, command: Submit | Reset) -> Any:
        if isinstance(command, Reset):
            self._checkpoint_ref = None
            self._epoch = None
            return self.state
        config = replace(
            self._config,
            prompt=command.prompt,
            resume_checkpoint_ref=self._checkpoint_ref,
            resume_epoch=self._epoch,
        )

        def sink(record: dict[str, Any]) -> None:
            self._events.append(record)
            payload = record.get("payload")
            if not isinstance(payload, dict):
                payload = record
            kind = record.get("kind") or record.get("type")
            if kind in {"context_checkpoint", "context_epoch_advanced"}:
                reference = payload.get("checkpoint_ref")
                epoch = payload.get("epoch")
                if isinstance(reference, str) and reference:
                    self._checkpoint_ref = reference
                if isinstance(epoch, int) and not isinstance(epoch, bool):
                    self._epoch = epoch
            if command.on_event is not None:
                command.on_event(record)

        return await oneshot.run_oneshot(config, on_event=sink)


def _session_root(config: oneshot.OneShotConfig) -> Path:
    if config.session_root is not None:
        return Path(config.session_root).expanduser().resolve()
    repository = Path(config.repo).expanduser().resolve()
    stamp = time.strftime("%Y%m%d-%H%M%S")
    return repository / ".cambium" / "sessions" / (
        f"interactive-{stamp}-{time.time_ns() & 0xFFFFFF:06x}"
    )
''')


def ensure_import(source: str) -> str:
    statement = "from .interactive import InteractiveSession\n"
    if statement in source:
        return source
    tree = ast.parse(source)
    imports = [node for node in tree.body if isinstance(node, (ast.Import, ast.ImportFrom))]
    if not imports:
        return statement + source
    anchor = imports[-1]
    return insert_after(source, anchor, statement)


def patch_frontend(path_text: str, function_name: str) -> None:
    path = ROOT / path_text
    source = ensure_import(path.read_text(encoding="utf-8"))
    if "InteractiveSession(config)" in source:
        path.write_text(source, encoding="utf-8")
        return
    tree = ast.parse(source)
    fn = next(
        (
            node
            for node in tree.body
            if isinstance(node, ast.AsyncFunctionDef) and node.name == function_name
        ),
        None,
    )
    if fn is None:
        raise RuntimeError(f"{path_text}: {function_name} missing")
    calls = [
        node
        for node in ast.walk(fn)
        if isinstance(node, ast.Await)
        and isinstance(node.value, ast.Call)
        and isinstance(node.value.func, ast.Attribute)
        and isinstance(node.value.func.value, ast.Name)
        and node.value.func.value.id == "oneshot"
        and node.value.func.attr == "run_oneshot"
    ]
    if len(calls) != 1:
        raise RuntimeError(f"{path_text}: expected one run_oneshot await, found {len(calls)}")
    call = calls[0].value
    event_arg = next((keyword.value for keyword in call.keywords if keyword.arg == "on_event"), None)
    if event_arg is None:
        raise RuntimeError(f"{path_text}: on_event callback missing")
    event_text = ast.get_source_segment(source, event_arg)
    if event_text is None:
        raise RuntimeError(f"{path_text}: cannot recover event callback")
    offsets = line_offsets(source)
    begin = offsets[calls[0].lineno - 1] + calls[0].col_offset
    end = offsets[(calls[0].end_lineno or calls[0].lineno) - 1] + (calls[0].end_col_offset or 0)
    source = (
        source[:begin]
        + f"await interactive_session.submit(prompt, on_event={event_text})"
        + source[end:]
    )
    tree = ast.parse(source)
    fn = next(node for node in tree.body if isinstance(node, ast.AsyncFunctionDef) and node.name == function_name)
    outer_try = next((node for node in fn.body if isinstance(node, ast.Try) and node.finalbody), None)
    if outer_try is None:
        raise RuntimeError(f"{path_text}: outer try/finally missing")
    first = outer_try.finalbody[0]
    indent = " " * first.col_offset
    source = insert_after(
        source,
        fn.body[fn.body.index(outer_try) - 1] if fn.body.index(outer_try) > 0 else fn.args,
        f"{indent[:-4] if len(indent) >= 4 else ''}interactive_session = InteractiveSession(config)\n",
    )
    # Reparse after insertion and locate the finalizer by syntax, then prepend close.
    tree = ast.parse(source)
    fn = next(node for node in tree.body if isinstance(node, ast.AsyncFunctionDef) and node.name == function_name)
    outer_try = next((node for node in fn.body if isinstance(node, ast.Try) and node.finalbody), None)
    first = outer_try.finalbody[0]
    offsets = line_offsets(source)
    position = offsets[first.lineno - 1]
    source = source[:position] + " " * first.col_offset + "await interactive_session.close()\n" + source[position:]
    ast.parse(source)
    path.write_text(source, encoding="utf-8")


add_config_fields()
add_resume_to_task_spec()
patch_frontend("src/cambium/repl.py", "run_repl")
patch_frontend("src/cambium/tui.py", "run_tui")

write("tests/scenarios/test_interactive_session.py", r'''from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

from cambium import interactive
from cambium.oneshot import OneShotConfig


def test_interactive_session_advances_one_checkpoint_head(monkeypatch, tmp_path: Path):
    seen = []

    async def run(config, *, on_event=None):
        seen.append(config)
        assert on_event is not None
        on_event(
            {
                "kind": "context_checkpoint",
                "payload": {
                    "checkpoint_ref": f"checkpoint-{len(seen)}",
                    "epoch": len(seen),
                },
            }
        )
        return SimpleNamespace(exit_code=0)

    monkeypatch.setattr(interactive.oneshot, "run_oneshot", run)

    async def scenario():
        session = interactive.InteractiveSession(OneShotConfig(repo=tmp_path))
        await session.submit("one")
        await session.submit("two")
        await session.close()

    asyncio.run(scenario())
    assert seen[0].resume_checkpoint_ref is None
    assert seen[1].resume_checkpoint_ref == "checkpoint-1"
    assert seen[1].resume_epoch == 1
    assert seen[0].session_root == seen[1].session_root


def test_interactive_mailbox_serializes_branch_publication(monkeypatch, tmp_path: Path):
    active = 0
    peak = 0

    async def run(config, *, on_event=None):
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        await asyncio.sleep(0)
        active -= 1
        return SimpleNamespace(exit_code=0)

    monkeypatch.setattr(interactive.oneshot, "run_oneshot", run)

    async def scenario():
        session = interactive.InteractiveSession(OneShotConfig(repo=tmp_path))
        await asyncio.gather(*(session.submit(str(index)) for index in range(8)))
        await session.close()

    asyncio.run(scenario())
    assert peak == 1
''')

for path in (ROOT / "src").rglob("*.py"):
    ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
print("interactive v2 applied")
