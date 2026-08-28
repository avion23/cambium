"""Regression coverage for forced-finalization and progress accounting."""

from __future__ import annotations

import asyncio
from pathlib import Path

from test_worker_agent_loop import (
    _agent_config,
    _drive_loop,
    _make_worktree,
    _ScriptedRouter,
    _UsageScriptedRouter,
)

from cambium import worker


def test_forced_finish_without_code_change_is_failed(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    worktree = _make_worktree(repo)
    config = _agent_config(worktree, max_tokens=10)
    usage = {"prompt_tokens": 10, "completion_tokens": 0, "total_tokens": 10}
    router = _UsageScriptedRouter(
        [
            '{"type":"tool_call","name":"read_batch","arguments":{"paths":["alpha.txt"]}}',
            '{"type":"finish","summary":"the investigation remains incomplete"}',
        ],
        [usage, usage],
    )

    outcome = asyncio.run(_drive_loop(config, worktree, router))

    assert outcome["status"] == "failed"
    assert outcome["failure_reason"] == (
        "forced finalization: investigation incomplete, no changes made"
    )


def test_forced_finish_with_verified_code_change_succeeds(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    worktree = _make_worktree(repo)
    config = _agent_config(worktree, max_tokens=20)
    usage = {"prompt_tokens": 10, "completion_tokens": 0, "total_tokens": 10}
    router = _UsageScriptedRouter(
        [
            '{"type":"tool_call","name":"edit_file","arguments":{"path":"alpha.txt",'
            '"old_string":"alpha-content\\n","new_string":"changed\\n"}}',
            '{"type":"tool_call","name":"run_shell","arguments":{"cmd":["true"]}}',
            '{"type":"finish","summary":"changed and verified"}',
        ],
        [usage, usage, usage],
    )

    outcome = asyncio.run(_drive_loop(config, worktree, router))

    assert outcome["status"] == "succeeded"
    assert outcome["summary"] == "changed and verified"


def test_non_forced_finish_without_code_change_is_unchanged(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    worktree = _make_worktree(repo)
    config = _agent_config(worktree, max_tokens=100)
    router = _ScriptedRouter(
        [
            '{"type":"tool_call","name":"read_batch","arguments":{"paths":["alpha.txt"]}}',
            '{"type":"finish","summary":"completed investigation"}',
        ]
    )

    outcome = asyncio.run(_drive_loop(config, worktree, router))

    assert outcome["status"] == "succeeded"
    assert outcome["summary"] == "completed investigation"


def test_progress_detector_rejects_identical_read_results_from_new_paths() -> None:
    detector = worker._ProgressDetector(max_no_progress_actions=2, progress_window=3)

    def read(path: str) -> dict[str, object]:
        return {
            "type": "tool_call",
            "name": "read_batch",
            "arguments": {"paths": [path]},
        }

    assert not detector.observe(
        action=read("one.txt"), result_content="--- one.txt ---\nsame content"
    )
    assert not detector.observe(
        action=read("two.txt"), result_content="--- two.txt ---\nsame content"
    )
    assert detector.observe(
        action=read("three.txt"), result_content="--- three.txt ---\nsame content"
    )


def test_prompt_context_usage_counts_the_serialized_raw_tail() -> None:
    messages = [
        {"role": "system", "content": "stable header"},
        {"role": "user", "content": "task"},
        {"role": "assistant", "content": "raw observation é"},
    ]

    fields = worker._prompt_context_usage_fields(
        {"messages": messages}, call_kind="agent"
    )

    assert fields["active_context_bytes"] == len(worker._canonical_json_bytes(messages))
    assert fields["summary_trunk_bytes"] == len(worker._canonical_json_bytes(messages[:2]))
    assert fields["raw_tail_bytes"] == len(worker._canonical_json_bytes(messages[2:]))
    assert fields["raw_tail_bytes"] > 0
