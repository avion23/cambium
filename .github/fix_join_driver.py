from __future__ import annotations

import runpy
from pathlib import Path

path = Path(__file__).with_name("fix_join.py")
text = path.read_text(encoding="utf-8")
old = '''replace_once(
    "src/cambium/supervisor.py",
    \'\'\'        task_id = spec["task_id"]
        repo = Path(spec["repo"])
\'\'\',
    \'\'\'        if spec.get("_private_parent_integration") is True:
            return await self._integrate_child_into_suspended_parent(spec, handle)
        task_id = spec["task_id"]
        repo = Path(spec["repo"])
\'\'\',
    label="private merge dispatch",
)
'''
new = '''replace_once(
    "src/cambium/supervisor.py",
    \'\'\'    async def _merge_task(self, spec: dict[str, Any], handle: WorkerHandle) -> str | None:
        """Stage and atomically publish the worker branch onto refs/heads/main.

        On a non-fast-forward refusal a backward-compatible ``merge_failed``
        event is appended.  A conflict uses that same event kind but carries a
        structured ``status=merge_conflict`` envelope; no resolver child is
        spawned here.
        """
        task_id = spec["task_id"]
        repo = Path(spec["repo"])
\'\'\',
    \'\'\'    async def _merge_task(self, spec: dict[str, Any], handle: WorkerHandle) -> str | None:
        """Stage and atomically publish the worker branch onto refs/heads/main.

        On a non-fast-forward refusal a backward-compatible ``merge_failed``
        event is appended.  A conflict uses that same event kind but carries a
        structured ``status=merge_conflict`` envelope; no resolver child is
        spawned here.
        """
        if spec.get("_private_parent_integration") is True:
            return await self._integrate_child_into_suspended_parent(spec, handle)
        task_id = spec["task_id"]
        repo = Path(spec["repo"])
\'\'\',
    label="private merge dispatch",
)
'''
if text.count(old) != 1:
    raise RuntimeError("transactional dispatch patch block changed")
path.write_text(text.replace(old, new, 1), encoding="utf-8")
runpy.run_path(str(path), run_name="__main__")
