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


def add_import(source: str, statement: str) -> str:
    if statement in source:
        return source
    tree = ast.parse(source)
    imports = [node for node in tree.body if isinstance(node, (ast.Import, ast.ImportFrom))]
    line = (imports[-1].end_lineno if imports else 1)
    lines = source.splitlines(keepends=True)
    lines.insert(line, statement + "\n")
    return "".join(lines)


def add_config_fields() -> None:
    path = ROOT / "src/cambium/oneshot.py"
    source = path.read_text(encoding="utf-8")
    tree = ast.parse(source)
    cls = next((node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == "OneShotConfig"), None)
    if cls is None:
        raise RuntimeError("OneShotConfig missing")
    fields = [node for node in cls.body if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name)]
    names = {node.target.id for node in fields}
    if {"resume_checkpoint_ref", "resume_epoch"} <= names:
        return
    anchor = fields[-1].end_lineno if fields else cls.lineno
    lines = source.splitlines(keepends=True)
    indent = " " * (cls.col_offset + 4)
    lines.insert(anchor, f"{indent}resume_checkpoint_ref: str | None = None\n{indent}resume_epoch: int | None = None\n")
    path.write_text("".join(lines), encoding="utf-8")


def patch_task_spec() -> None:
    path = ROOT / "src/cambium/oneshot.py"
    source = path.read_text(encoding="utf-8")
    if "config.resume_checkpoint_ref" in source:
        return
    tree = ast.parse(source)
    candidates = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Dict):
            continue
        has_prompt = any(
            isinstance(value, ast.Attribute)
            and isinstance(value.value, ast.Name)
            and value.value.id == "config"
            and value.attr == "prompt"
            for value in node.values
        )
        if has_prompt:
            candidates.append(node)
    if len(candidates) != 1:
        raise RuntimeError(f"expected one task-spec prompt dictionary, found {len(candidates)}")
    node = candidates[0]
    segment = ast.get_source_segment(source, node)
    if segment is None:
        raise RuntimeError("cannot extract one-shot task spec")
    close = segment.rfind("}")
    prefix = segment[:close].rstrip()
    comma = "" if prefix.endswith(("{", ",")) else ","
    addition = (
        f"{comma}\n            **({{'resume': {{\n"
        "                'checkpoint_ref': config.resume_checkpoint_ref,\n"
        "                'epoch': config.resume_epoch,\n"
        "                'child_results': [],\n"
        "                'child_results_truncated': False,\n"
        "            }}} if config.resume_checkpoint_ref is not None and config.resume_epoch is not None else {}),\n"
    )
    replacement = prefix + addition + segment[close:]
    starts = []
    offset = 0
    for line in source.splitlines(keepends=True):
        starts.append(offset)
        offset += len(line)
    begin = starts[node.lineno - 1] + node.col_offset
    end = starts[(node.end_lineno or node.lineno) - 1] + (node.end_col_offset or 0)
    path.write_text(source[:begin] + replacement + source[end:], encoding="utf-8")


add_config_fields()
patch_task_spec()

write("src/cambium/interactive.py", r'''"""One durable, mailbox-owned conversation branch for REPL and TUI."""
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
    on_event: Callable[[dict[str, Any]], None] | None


@dataclass(frozen=True, slots=True)
class Reset:
    pass


@dataclass(frozen=True, slots=True)
class InteractiveState:
    session_root: Path
    checkpoint_ref: str | None
    epoch: int | None
    events: int


class InteractiveSession:
    """Serialize prompt admission and publish one checkpoint head at a time."""

    def __init__(self, config: oneshot.OneShotConfig, *, capacity: int = 16) -> None:
        root = _session_root(config)
        self._config = replace(config, session_root=root)
        self._checkpoint_ref: str | None = None
        self._epoch: int | None = None
        self._events: list[dict[str, Any]] = []
        self._mailbox: MailboxActor[Submit | Reset, Any] = MailboxActor(
            self._handle, capacity=capacity, name="interactive-session"
        )

    @property
    def events(self) -> tuple[dict[str, Any], ...]:
        return tuple(self._events)

    @property
    def state(self) -> InteractiveState:
        return InteractiveState(Path(self._config.session_root), self._checkpoint_ref, self._epoch, len(self._events))

    async def submit(self, prompt: str, *, on_event: Callable[[dict[str, Any]], None] | None = None) -> Any:
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
    return repository / ".cambium" / "sessions" / f"interactive-{stamp}-{time.time_ns() & 0xFFFFFF:06x}"
''')


def patch_frontend(path_text: str, function_name: str) -> None:
    path = ROOT / path_text
    source = add_import(path.read_text(encoding="utf-8"), "from .interactive import InteractiveSession")
    tree = ast.parse(source)
    fn = next((node for node in tree.body if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == function_name), None)
    if fn is None or not isinstance(fn, ast.AsyncFunctionDef):
        raise RuntimeError(f"{path_text}: async {function_name} missing")
    if "interactive_session = InteractiveSession(config)" not in source:
        first_try = next((node for node in fn.body if isinstance(node, ast.Try)), None)
        if first_try is None:
            raise RuntimeError(f"{path_text}: outer try missing")
        lines = source.splitlines(keepends=True)
        lines.insert(first_try.lineno - 1, " " * first_try.col_offset + "interactive_session = InteractiveSession(config)\n")
        source = "".join(lines)
    patterns = [
        r"await oneshot\.run_oneshot\(prompt_config, on_event=([A-Za-z_][A-Za-z0-9_]*)\)",
        r"await oneshot\.run_oneshot\([^,]+, on_event=([A-Za-z_][A-Za-z0-9_]*)\)",
    ]
    if "interactive_session.submit" not in source:
        for pattern in patterns:
            source, count = re.subn(pattern, r"await interactive_session.submit(prompt, on_event=\1)", source, count=1)
            if count:
                break
        else:
            raise RuntimeError(f"{path_text}: run_oneshot call missing")
    if "await interactive_session.close()" not in source:
        tree = ast.parse(source)
        fn = next(node for node in tree.body if isinstance(node, ast.AsyncFunctionDef) and node.name == function_name)
        outer = next((node for node in fn.body if isinstance(node, ast.Try) and node.finalbody), None)
        if outer is None:
            raise RuntimeError(f"{path_text}: finalizer missing")
        lines = source.splitlines(keepends=True)
        first = outer.finalbody[0]
        lines.insert(first.lineno - 1, " " * first.col_offset + "await interactive_session.close()\n")
        source = "".join(lines)
    ast.parse(source)
    path.write_text(source, encoding="utf-8")


patch_frontend("src/cambium/repl.py", "run_repl")
patch_frontend("src/cambium/tui.py", "run_tui")

write("tests/scenarios/test_interactive_session.py", r'''from __future__ import annotations
import asyncio
from pathlib import Path
from types import SimpleNamespace
from cambium import interactive
from cambium.oneshot import OneShotConfig


def test_interactive_session_advances_one_checkpoint_head(monkeypatch, tmp_path: Path):
    seen=[]
    async def run(config, *, on_event=None):
        seen.append(config); assert on_event is not None
        on_event({"kind":"context_checkpoint","payload":{"checkpoint_ref":f"c{len(seen)}","epoch":len(seen)}})
        return SimpleNamespace(exit_code=0)
    monkeypatch.setattr(interactive.oneshot,"run_oneshot",run)
    async def scenario():
        session=interactive.InteractiveSession(OneShotConfig(repo=tmp_path))
        await session.submit("one"); await session.submit("two"); await session.close()
    asyncio.run(scenario())
    assert seen[0].resume_checkpoint_ref is None
    assert seen[1].resume_checkpoint_ref=="c1" and seen[1].resume_epoch==1
    assert seen[0].session_root==seen[1].session_root


def test_interactive_mailbox_prevents_parallel_head_publication(monkeypatch, tmp_path: Path):
    active=0; peak=0
    async def run(config, *, on_event=None):
        nonlocal active,peak
        active+=1; peak=max(peak,active); await asyncio.sleep(0); active-=1
        return SimpleNamespace(exit_code=0)
    monkeypatch.setattr(interactive.oneshot,"run_oneshot",run)
    async def scenario():
        session=interactive.InteractiveSession(OneShotConfig(repo=tmp_path))
        await asyncio.gather(*(session.submit(str(i)) for i in range(8))); await session.close()
    asyncio.run(scenario()); assert peak==1
''')

for path in (ROOT / "src").rglob("*.py"):
    ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
print("interactive session applied")
