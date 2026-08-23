from __future__ import annotations

import re
import runpy
from pathlib import Path


def replace_once(path: Path, old: str, new: str, label: str) -> None:
    text = path.read_text(encoding="utf-8")
    count = text.count(old)
    if count != 1:
        raise RuntimeError(f"{label}: expected one match, found {count}")
    path.write_text(text.replace(old, new, 1), encoding="utf-8")


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
text = text.replace(old, new, 1)
for source, escaped in (
    (r'"parent\n"', r'"parent\\n"'),
    (r'"child\n"', r'"child\\n"'),
):
    if text.count(source) != 1:
        raise RuntimeError(f"expected one generated test literal {source!r}")
    text = text.replace(source, escaped, 1)
path.write_text(text, encoding="utf-8")
runpy.run_path(str(path), run_name="__main__")

root = Path(__file__).resolve().parents[1]
worker = root / "src/cambium/worker.py"
supervisor = root / "src/cambium/supervisor.py"

replace_once(
    worker,
    '''        "child_results",
        "child_results_truncated",
''',
    '''        "child_results",
        "child_results_truncated",
        "workspace_changed",
''',
    "resume key contract",
)
replace_once(
    worker,
    '''    truncated = value.get("child_results_truncated")
    if type(truncated) is not bool:
        raise ContextForkError("resume 'child_results_truncated' must be a boolean")
    return {
        "checkpoint_ref": checkpoint_ref,
        "epoch": epoch,
        "child_results": validated_results,
        "child_results_truncated": truncated,
    }
''',
    '''    truncated = value.get("child_results_truncated")
    if type(truncated) is not bool:
        raise ContextForkError("resume 'child_results_truncated' must be a boolean")
    workspace_changed = value.get("workspace_changed")
    if type(workspace_changed) is not bool:
        raise ContextForkError("resume 'workspace_changed' must be a boolean")
    return {
        "checkpoint_ref": checkpoint_ref,
        "epoch": epoch,
        "child_results": validated_results,
        "child_results_truncated": truncated,
        "workspace_changed": workspace_changed,
    }
''',
    "resume workspace validation",
)
replace_once(
    worker,
    '''        child_code_changed = any(
            child_result.get("status") == "succeeded"
            and bool(child_result.get("commits") or child_result.get("files_changed"))
            for child_result in resume["child_results"]
        )
        code_changed = resume_checkpoint.code_changed or child_code_changed
        verified_after_change = (
            resume_checkpoint.verified_after_change and not child_code_changed
        )
        verification_failed = (
            False if child_code_changed else resume_checkpoint.verification_failed
        )
''',
    '''        workspace_changed = resume["workspace_changed"]
        code_changed = resume_checkpoint.code_changed or workspace_changed
        verified_after_change = (
            resume_checkpoint.verified_after_change and not workspace_changed
        )
        verification_failed = (
            False if workspace_changed else resume_checkpoint.verification_failed
        )
''',
    "supervisor-owned workspace state",
)
replace_once(
    supervisor,
    '''        child_results: list[dict[str, Any]] = []
        truncated = False
        for child_id in child_ids:
            envelope = self._child_result_by_task.get(child_id)
''',
    '''        child_results: list[dict[str, Any]] = []
        truncated = False
        workspace_changed = False
        for child_id in child_ids:
            child_spec = self._session_spec(child_id)
            result = self._results.get(child_id)
            if (
                child_spec is not None
                and child_spec.get("_private_parent_integration") is True
                and result is not None
                and result.status == "succeeded"
                and result.merge_sha is not None
            ):
                workspace_changed = True
            envelope = self._child_result_by_task.get(child_id)
''',
    "workspace change derivation",
)
replace_once(
    supervisor,
    '''            "child_results": child_results,
            "child_results_truncated": truncated,
        }
''',
    '''            "child_results": child_results,
            "child_results_truncated": truncated,
            "workspace_changed": workspace_changed,
        }
''',
    "workspace change payload",
)
replace_once(
    supervisor,
    '''                            child_count=len(child_ids),
                        )
''',
    '''                            child_count=len(child_ids),
                            workspace_changed=resume_payload["workspace_changed"],
                        )
''',
    "workspace change event",
)

resume_line = re.compile(
    r'(?m)^(?P<indent>\s*)"child_results_truncated": (?P<value>[^,\n]+),$'
)
for test_path in sorted((root / "tests").rglob("*.py")):
    test_text = test_path.read_text(encoding="utf-8")
    if "child_results_truncated" not in test_text:
        continue
    updated = resume_line.sub(
        lambda match: (
            match.group(0)
            + "\n"
            + match.group("indent")
            + '"workspace_changed": False,'
        ),
        test_text,
    )
    if updated == test_text:
        raise RuntimeError(f"could not update resume fixtures in {test_path}")
    test_path.write_text(updated, encoding="utf-8")
