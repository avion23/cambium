"""Conformance pins for the normative Cambium architecture contracts.

These checks intentionally use the shipped modules, SQLite schema, git, and a
real worker process.  The supervisor environment checks are the one exception
to runtime exercise: they parse the module with :mod:`ast` so every direct
``create_subprocess_exec`` and ``subprocess.run`` call is checked without
starting a full supervisor.
"""

from __future__ import annotations

import ast
import asyncio
import inspect
import json
import os
import sqlite3
import subprocess
import sys
from collections.abc import MutableMapping
from datetime import UTC, datetime
from pathlib import Path

import pytest

from cambium import supervisor
from cambium.fencing import write_generation
from cambium.ipc import MAX_LINE_BYTES, MessageTooLong, read_message
from cambium.merge import ZERO_SHA, MergeSequencer, NonFastForwardError
from cambium.store import CRITICAL_KINDS, EventStore
from cambium.tasktree import NodeStatus, TaskKind, TaskNode, upward_result

EXPECTED_CRITICAL_KINDS = {
    "result",
    "checkpoint",
    "worker_exit",
    "task_failed",
    "merge_progress",
    "task_assigned",
    "merge_committed",
    "merge_staging_quarantined",
    "merge_staging_cleanup_failed",
    "merge_staging_prune_started",
    "merge_staging_pruned",
}

EXPECTED_EVENT_COLUMNS = {
    "seq": "INTEGER",
    "kind": "TEXT",
    "payload": "TEXT",
    "ts": "TEXT",
    "monotonic_ms": "INTEGER",
    "task_id": "TEXT",
    "worker_id": "TEXT",
    "generation": "INTEGER",
    "request_id": "TEXT",
}


def _git(cwd: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args], cwd=cwd, check=True, capture_output=True, text=True
    )


def _init_repo(repo: Path) -> str:
    _git(repo.parent, "init", "-b", "main", str(repo))
    _git(repo, "config", "user.name", "cambium-conformance")
    _git(repo, "config", "user.email", "conformance@example.invalid")
    _git(repo, "config", "gc.auto", "0")
    (repo / "base.txt").write_text("base\n")
    _git(repo, "add", "base.txt")
    _git(repo, "commit", "-m", "initial")
    return _git(repo, "rev-parse", "HEAD").stdout.strip()


def _worker_commit(repo: Path, base: str, branch: str, worktree: Path, name: str) -> str:
    _git(repo, "worktree", "add", "-b", branch, str(worktree), base)
    (worktree / name).write_text(f"{name}\n")
    _git(worktree, "add", name)
    _git(worktree, "commit", "-m", branch)
    return _git(worktree, "rev-parse", "HEAD").stdout.strip()


def test_ipc_line_cap_is_normative_and_oversize_is_rejected() -> None:
    assert MAX_LINE_BYTES == 1_048_576
    assert issubclass(MessageTooLong, ValueError)

    async def scenario() -> None:
        reader = asyncio.StreamReader()
        reader.feed_data(b"x" * (MAX_LINE_BYTES + 1) + b"\n")
        reader.feed_eof()
        with pytest.raises(MessageTooLong):
            await read_message(reader)

    asyncio.run(scenario())


def test_real_worker_result_correlates_and_exit_has_no_request_id(tmp_path: Path) -> None:
    session_dir = tmp_path / "session"
    scratch = session_dir / "scratch"
    scratch.mkdir(parents=True)
    _init_repo(scratch)
    worktree = session_dir / "worker-wt"
    _git(scratch, "worktree", "add", "-b", "wt-conformance-worker", str(worktree), "main")
    write_generation(worktree, 1)
    task_id = "conformance-worker"
    init_request_id = "conformance-init"
    run_request_id = "conformance-run"

    async def scenario() -> None:
        proc = await asyncio.create_subprocess_exec(
            sys.executable,
            "-u",
            "-m",
            "cambium.worker",
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env={**os.environ, "PYTHONUNBUFFERED": "1"},
            start_new_session=True,
        )
        try:
            assert proc.stdin is not None
            assert proc.stdout is not None

            proc.stdin.write((
                f'{{"type":"init","request_id":"{init_request_id}",'
                f'"task_id":"{task_id}","generation":1,"proto":1}}\n'
            ).encode())
            await proc.stdin.drain()
            ready = await asyncio.wait_for(read_message(proc.stdout), 15.0)
            assert ready is not None
            assert ready["type"] == "ready"
            assert ready["request_id"] == init_request_id

            run_message = {
                "type": "run_task",
                "request_id": run_request_id,
                "task_id": task_id,
                "scratch_repo": str(scratch),
                "worktree_path": str(worktree),
                "branch": "wt-conformance-worker",
                "target_file": "base.txt",
                "marker": "conformance marker",
                "write_marker": True,
            }
            proc.stdin.write((json.dumps(run_message) + "\n").encode())
            await proc.stdin.drain()

            result: dict | None = None
            exit_message: dict | None = None
            while result is None or exit_message is None:
                message = await asyncio.wait_for(read_message(proc.stdout), 15.0)
                assert message is not None
                if message.get("type") == "result_envelope":
                    result = message
                elif message.get("type") == "exit_message":
                    exit_message = message

            assert result["request_id"] == run_request_id
            assert "request_id" not in exit_message
            assert await proc.wait() == 0
        finally:
            if proc.returncode is None:
                proc.kill()
                await proc.wait()

    asyncio.run(scenario())


def test_upward_result_has_exact_architecture_envelope_keys() -> None:
    node = TaskNode(
        task_id="child",
        kind=TaskKind.TEST,
        parent_task_id="parent",
        spec={
            "unified_diff": "diff",
            "diff_truncated": False,
            "summary": "summary",
            "metric_score": 1.0,
            "metric_breakdown": {"tests": 1.0},
            "commits": ["abc"],
            "files_changed": ["file.py"],
        },
        depth=1,
        width_idx=0,
        status=NodeStatus.DONE,
    )
    assert set(upward_result(node)) == {
        "parent_task_id",
        "unified_diff",
        "diff_truncated",
        "summary",
        "metric_score",
        "metric_breakdown",
        "commits",
        "files_changed",
        "status",
    }


def test_critical_event_kinds_are_exactly_architecture_critical_set() -> None:
    store_kinds = set(CRITICAL_KINDS)

    assert EXPECTED_CRITICAL_KINDS <= store_kinds
    assert store_kinds <= EXPECTED_CRITICAL_KINDS


def test_event_store_ddl_matches_architecture_and_keeps_iso_ts_text(tmp_path: Path) -> None:
    path = tmp_path / "events.db"
    store = EventStore(path, fsync_interval_s=0.01)
    try:
        iso_ts = datetime.now(UTC).isoformat()
        store.append({"kind": "result", "payload": {}, "ts": iso_ts})
    finally:
        store.close()

    with sqlite3.connect(path) as connection:
        rows = connection.execute("PRAGMA table_info(events)").fetchall()
        columns = {row[1]: row[2].upper() for row in rows}
        assert columns == EXPECTED_EVENT_COLUMNS
        assert connection.execute("SELECT ts FROM events").fetchone()[0] == iso_ts


def test_merge_rejects_invalid_old_values_non_fast_forward_and_quarantine(tmp_path):
    repo = tmp_path / "repo"
    base = _init_repo(repo)
    tip_a = _worker_commit(repo, base, "wt-conformance-a", tmp_path / "wt-a", "a.txt")
    tip_b = _worker_commit(repo, base, "wt-conformance-b", tmp_path / "wt-b", "b.txt")
    sequencer = MergeSequencer(task_id="conformance-merge")

    for invalid_old in (None, "", ZERO_SHA):
        with pytest.raises(NonFastForwardError):
            sequencer.publish_merge(repo, tip_a, invalid_old)
        assert _git(repo, "rev-parse", "refs/heads/main").stdout.strip() == base

    sequencer.publish_merge(repo, tip_a, base)
    with pytest.raises(NonFastForwardError):
        sequencer.publish_merge(repo, tip_b, tip_a)
    assert _git(repo, "rev-parse", "refs/heads/main").stdout.strip() == tip_a

    quarantine_key = "GIT_QUARANTINE_PATH"
    previous_quarantine = os.environ.get(quarantine_key)
    os.environ[quarantine_key] = str(tmp_path / "quarantine")
    try:
        assert quarantine_key not in MergeSequencer._git_env()
        assert quarantine_key not in MergeSequencer._rebase_env()
    finally:
        if previous_quarantine is None:
            os.environ.pop(quarantine_key, None)
        else:
            os.environ[quarantine_key] = previous_quarantine


def test_diffundo_is_cacheless_and_lints_volatile_prompt_prefix() -> None:
    diffundo = pytest.importorskip("cambium.diffundo")
    router = diffundo.Diffundo(())
    attributes = getattr(router, "__dict__", {})
    assert not any(isinstance(value, MutableMapping) for value in attributes.values())

    prompt = {
        "messages": [
            {
                "role": "user",
                "content": "stable instruction\ncreated 2026-08-09T12:00:00Z\nthird line",
            }
        ]
    }
    with pytest.raises(diffundo.PromptStructureError):
        diffundo.validate_prompt_structure(prompt)


def test_worker_env_drops_api_key_names_and_controls_path(tmp_path: Path) -> None:
    redact = pytest.importorskip("cambium.redact")
    env = redact.build_worker_env({
        "TEST_API_KEY_DEMO": "not-for-workers",
        "PATH": "/host/bin",
        "HOME": "/home/host",
    }, worktree=tmp_path / "worker")
    assert "TEST_API_KEY_DEMO" not in env
    assert env["PATH"] == os.defpath
    assert env["HOME"] == str((tmp_path / "worker").resolve() / ".cambium" / "home")
    assert "/host/bin" not in env.values()
    assert "/home/host" not in env.values()


def test_supervisor_spawn_sites_use_scrubbed_environment() -> None:
    """Use AST instead of a runtime hook to cover every direct spawn site.

    Every ``create_subprocess_exec`` spawn must pass an explicit ``env=`` that
    is either ``_worker_environment(...)`` (the worker spawn, which re-adds
    only authorized canonical provider keys) or ``scrub_environment()`` (git,
    gate, and shell spawns, which must carry no provider credentials).
    """
    source = Path(inspect.getfile(supervisor)).read_text(encoding="utf-8")
    tree = ast.parse(source)

    scrubber_names = {"_worker_environment", "scrub_environment"}

    def calls_scrubber(node: ast.AST) -> bool:
        return any(
            isinstance(child, ast.Call)
            and isinstance(child.func, ast.Name)
            and child.func.id in scrubber_names
            for child in ast.walk(node)
        )

    def assignment_uses_scrubber(body: list[ast.stmt], name: str) -> bool:
        for statement in body:
            if not isinstance(statement, ast.Assign):
                continue
            for target in statement.targets:
                if not isinstance(target, ast.Name) or target.id != name:
                    continue
                if calls_scrubber(statement.value):
                    return True
                if (
                    isinstance(statement.value, ast.Call)
                    and isinstance(statement.value.func, ast.Attribute)
                    and statement.value.func.attr == "_worker_env"
                ):
                    return True
        return False

    parent_map: dict[ast.AST, ast.AST] = {
        child: parent
        for parent in ast.walk(tree)
        for child in ast.iter_child_nodes(parent)
    }

    def enclosing_function(node: ast.AST) -> ast.FunctionDef | ast.AsyncFunctionDef | None:
        current = node
        parent = parent_map.get(current)
        while parent is not None:
            if isinstance(parent, (ast.FunctionDef, ast.AsyncFunctionDef)):
                return parent
            current = parent
            parent = parent_map.get(current)
        return None

    def env_is_scrubbed(env_keyword: ast.keyword, node: ast.AST) -> bool:
        if calls_scrubber(env_keyword.value):
            return True
        if not isinstance(env_keyword.value, ast.Name):
            return False
        function = enclosing_function(node)
        return (
            function is not None
            and assignment_uses_scrubber(function.body, env_keyword.value.id)
        )

    spawn_calls = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "create_subprocess_exec"
    ]
    assert spawn_calls, "supervisor must have at least one subprocess spawn path"

    assert any(
        isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and node.name == "_worker_environment"
        for node in ast.walk(tree)
    )
    for call in spawn_calls:
        env_keyword = next((keyword for keyword in call.keywords if keyword.arg == "env"), None)
        assert env_keyword is not None, f"spawn at line {call.lineno} has no env="
        assert env_is_scrubbed(env_keyword, call), (
            f"spawn at line {call.lineno} does not use a scrubbed environment builder"
        )


def test_supervisor_subprocess_run_git_calls_use_scrubbed_environment() -> None:
    """Every ``subprocess.run`` in the supervisor must pass an explicit ``env=``
    built from a scrubber, so no git subprocess or hook inherits provider
    credentials.

    Regression guard for the CLI convenience paths (``_ensure_repo_initialized``
    and ``_sh``) that ran git with the inherited ``os.environ``.  Scrubber
    references are resolved through module builders (``scrub_environment``,
    ``_worker_environment``), scrubber-derived class methods (``_env``,
    ``_worker_env``), and locals assigned from any of those.
    """
    source = Path(inspect.getfile(supervisor)).read_text(encoding="utf-8")
    tree = ast.parse(source)

    scrubber_names = {"_worker_environment", "scrub_environment"}

    def calls_scrubber(node: ast.AST) -> bool:
        return any(
            isinstance(child, ast.Call)
            and isinstance(child.func, ast.Name)
            and child.func.id in scrubber_names
            for child in ast.walk(node)
        )

    scrubber_method_names = {
        item.name
        for class_node in (node for node in ast.walk(tree) if isinstance(node, ast.ClassDef))
        for item in class_node.body
        if isinstance(item, ast.FunctionDef) and calls_scrubber(item)
    }

    def references_scrubber(value: ast.AST) -> bool:
        if calls_scrubber(value):
            return True
        return any(
            isinstance(child, ast.Call)
            and isinstance(child.func, ast.Attribute)
            and child.func.attr in scrubber_method_names
            for child in ast.walk(value)
        )

    parent_map: dict[ast.AST, ast.AST] = {
        child: parent
        for parent in ast.walk(tree)
        for child in ast.iter_child_nodes(parent)
    }

    def enclosing_function(node: ast.AST) -> ast.FunctionDef | ast.AsyncFunctionDef | None:
        current = node
        parent = parent_map.get(current)
        while parent is not None:
            if isinstance(parent, (ast.FunctionDef, ast.AsyncFunctionDef)):
                return parent
            current = parent
            parent = parent_map.get(current)
        return None

    def assignment_uses_scrubber(body: list[ast.stmt], name: str) -> bool:
        for statement in body:
            if not isinstance(statement, ast.Assign):
                continue
            for target in statement.targets:
                if not isinstance(target, ast.Name) or target.id != name:
                    continue
                if references_scrubber(statement.value):
                    return True
        return False

    def env_is_scrubbed(value: ast.AST, node: ast.AST) -> bool:
        if references_scrubber(value):
            return True
        if isinstance(value, ast.Name):
            function = enclosing_function(node)
            return (
                function is not None
                and assignment_uses_scrubber(function.body, value.id)
            )
        return False

    run_calls = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "run"
        and isinstance(node.func.value, ast.Name)
        and node.func.value.id == "subprocess"
    ]
    assert run_calls, "supervisor must have at least one subprocess.run git call"

    for call in run_calls:
        env_keyword = next((keyword for keyword in call.keywords if keyword.arg == "env"), None)
        assert env_keyword is not None, (
            f"subprocess.run at line {call.lineno} has no env="
        )
        assert env_is_scrubbed(env_keyword.value, call), (
            f"subprocess.run at line {call.lineno} does not use a scrubbed environment"
        )
