"""Regression coverage for forced-finalization and progress accounting."""

from __future__ import annotations

import asyncio
import json
from dataclasses import replace
from pathlib import Path

from test_worker_agent_loop import (
    _agent_config,
    _drive_loop,
    _FakeWriter,
    _make_worktree,
    _ScriptedRouter,
    _UsageScriptedRouter,
)

from cambium import worker
from cambium.fencing import write_generation
from cambium.schemas import FINISH_ACTION_SCHEMA, validate_tool_call


def test_forced_finish_without_code_change_is_failed(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    worktree = _make_worktree(repo)
    config = _agent_config(worktree, max_tokens=10)
    usage = {"prompt_tokens": 10, "completion_tokens": 0, "total_tokens": 10}
    router = _UsageScriptedRouter(
        [
            '{"type":"tool_call","name":"read_batch","arguments":{"paths":["alpha.txt"]}}',
            '{"type":"finish","summary":"the investigation remains incomplete",'
            '"objective_met":false}',
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
    usages = [
        {"prompt_tokens": 10, "completion_tokens": 0, "total_tokens": 10},
        {"prompt_tokens": 20, "completion_tokens": 0, "total_tokens": 20},
        {"prompt_tokens": 30, "completion_tokens": 0, "total_tokens": 30},
    ]
    router = _UsageScriptedRouter(
        [
            '{"type":"tool_call","name":"edit_file","arguments":{"path":"alpha.txt",'
            '"old_string":"alpha-content\\n","new_string":"changed\\n"}}',
            '{"type":"tool_call","name":"run_shell","arguments":{"cmd":['
            '"git","diff","--check"]}}',
            '{"type":"finish","summary":"changed and verified","objective_met":true}',
        ],
        usages,
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
            '{"type":"finish","summary":"completed investigation","objective_met":true}',
        ]
    )

    outcome = asyncio.run(_drive_loop(config, worktree, router))

    assert outcome["status"] == "succeeded"
    assert outcome["summary"] == "completed investigation"


def test_non_forced_finish_with_objective_false_is_failed(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    worktree = _make_worktree(repo)
    config = _agent_config(worktree, max_tokens=100)
    router = _ScriptedRouter(
        [
            '{"type":"finish","summary":"task remains incomplete",'
            '"objective_met":false}',
        ]
    )

    outcome = asyncio.run(_drive_loop(config, worktree, router))

    assert outcome["status"] == "failed"
    assert outcome["failure_reason"] == "finish declared objective_met=false"
    assert outcome["summary"] == "task remains incomplete"
    assert outcome["terminal_action"] == {
        "type": "finish",
        "objective_met": False,
        "summary_present": True,
        "summary": "task remains incomplete",
    }


def test_forced_finish_without_code_change_but_objective_met_succeeds_review(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    worktree = _make_worktree(repo)
    config = _agent_config(worktree, max_tokens=10)
    usage = {"prompt_tokens": 10, "completion_tokens": 0, "total_tokens": 10}
    router = _UsageScriptedRouter(
        [
            '{"type":"tool_call","name":"read_batch","arguments":{"paths":["alpha.txt"]}}',
            '{"type":"finish","summary":"review complete; no defect found",'
            '"objective_met":true}',
        ],
        [usage, usage],
    )

    outcome = asyncio.run(_drive_loop(config, worktree, router))

    assert outcome["status"] == "succeeded"
    assert outcome["summary"] == "review complete; no defect found"


def test_forced_finish_with_code_change_and_failed_verification_stays_failed(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    worktree = _make_worktree(repo)
    config = _agent_config(worktree, max_tokens=20)
    usages = [
        {"prompt_tokens": 10, "completion_tokens": 0, "total_tokens": 10},
        {"prompt_tokens": 20, "completion_tokens": 0, "total_tokens": 20},
        {"prompt_tokens": 30, "completion_tokens": 0, "total_tokens": 30},
    ]
    router = _UsageScriptedRouter(
        [
            '{"type":"tool_call","name":"edit_file","arguments":{"path":"alpha.txt",'
            '"old_string":"alpha-content\\n","new_string":"changed\\n"}}',
            '{"type":"tool_call","name":"run_shell","arguments":{"cmd":["false"]}}',
            '{"type":"finish","summary":"verification failed","objective_met":true}',
        ],
        usages,
    )

    outcome = asyncio.run(_drive_loop(config, worktree, router))

    assert outcome["status"] == "failed"
    assert outcome["failure_reason"] == (
        "final synthesis failed: finish rejected: your verification command failed; "
        "run the tests successfully (e.g. run_shell) before finishing"
    )


def test_forced_finish_trivial_edit_and_true_still_succeeds(
    tmp_path: Path,
) -> None:
    """The gate accepts any successful verification; gaming prevention is separate."""
    repo = tmp_path / "repo"
    worktree = _make_worktree(repo)
    config = _agent_config(worktree, max_tokens=20)
    usages = [
        {"prompt_tokens": 10, "completion_tokens": 0, "total_tokens": 10},
        {"prompt_tokens": 20, "completion_tokens": 0, "total_tokens": 20},
        {"prompt_tokens": 30, "completion_tokens": 0, "total_tokens": 30},
    ]
    router = _UsageScriptedRouter(
        [
            '{"type":"tool_call","name":"edit_file","arguments":{"path":"alpha.txt",'
            '"old_string":"alpha-content\\n","new_string":"alpha-content \\n"}}',
            '{"type":"tool_call","name":"run_shell","arguments":{"cmd":["true"]}}',
            '{"type":"finish","summary":"trivial edit verified","objective_met":true}',
        ],
        usages,
    )

    outcome = asyncio.run(_drive_loop(config, worktree, router))

    assert outcome["status"] == "succeeded"
    assert outcome["summary"] == "trivial edit verified"


def test_forced_finish_resume_restores_no_code_change_gate(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    worktree = _make_worktree(repo)
    # Keep the tracked worktree non-empty without an agent edit: the old
    # resume heuristic treated this diff as code_changed=True.
    (worktree / "beta.txt").write_text("pre-existing change\n", encoding="utf-8")
    checkpoint_root = tmp_path / "checkpoints"
    config = _agent_config(worktree, max_tokens=10, checkpoint_root=checkpoint_root)
    usage = {"prompt_tokens": 10, "completion_tokens": 0, "total_tokens": 10}
    writer = _FakeWriter()
    first_router = _UsageScriptedRouter(
        [
            '{"type":"tool_call","name":"read_batch","arguments":{"paths":["alpha.txt"]}}',
            '{"type":"finish","summary":"still incomplete","objective_met":false}',
        ],
        [usage, usage],
    )
    first_outcome = asyncio.run(_drive_loop(config, worktree, first_router, writer))

    assert first_outcome["status"] == "failed"
    checkpoint_path = checkpoint_root / config.task_id / "turn-001.json"
    checkpoint = json.loads(checkpoint_path.read_text(encoding="utf-8"))
    assert checkpoint["code_changed"] is False
    checkpoint.pop("code_changed")
    checkpoint_path.write_text(json.dumps(checkpoint), encoding="utf-8")
    assert worker._load_turn_checkpoint(config, f"{config.task_id}/turn-001.json")[
        "code_changed"
    ] is False

    write_generation(worktree, 2)
    resumed_config = replace(
        config,
        generation=2,
        resume={
            "checkpoint_ref": f"{config.task_id}/turn-001.json",
            "epoch": 1,
            "child_results": [],
            "child_results_truncated": False,
            "workspace_changed": False,
        },
    )
    resumed_router = _UsageScriptedRouter(
        ['{"type":"finish","summary":"still incomplete after resume",'
         '"objective_met":false}'],
        [usage],
    )

    resumed = asyncio.run(_drive_loop(resumed_config, worktree, resumed_router))

    assert resumed["status"] == "failed"
    assert resumed["failure_reason"] == (
        "forced finalization: investigation incomplete, no changes made"
    )


def test_progress_hash_uses_full_capped_body_and_length() -> None:
    prefix = "x" * (16 * 1024)
    assert worker._progress_content_hash(prefix + "a") != worker._progress_content_hash(
        prefix + "b"
    )
    capped_prefix = "x" * worker.MAX_PROGRESS_CONTENT_BYTES
    assert worker._progress_content_hash(capped_prefix + "a") != worker._progress_content_hash(
        capped_prefix + "ab"
    )


def test_progress_hash_retains_only_recent_reads() -> None:
    detector = worker._ProgressDetector(max_no_progress_actions=2, progress_window=3)
    for index in range(worker.MAX_PROGRESS_CONTENT_HASHES + 2):
        detector.observe(
            action={
                "type": "tool_call",
                "name": "read_batch",
                "arguments": {"paths": [f"file-{index}.txt"]},
            },
            result_content=f"body-{index}",
        )

    assert len(detector._recent_content_hashes) == worker.MAX_PROGRESS_CONTENT_HASHES


def test_finish_schema_requires_boolean_objective_signal() -> None:
    assert "objective_met" in FINISH_ACTION_SCHEMA["required"]
    assert validate_tool_call(
        FINISH_ACTION_SCHEMA,
        {"type": "finish", "summary": "done", "objective_met": True},
    ) == []
    assert validate_tool_call(
        FINISH_ACTION_SCHEMA,
        {"type": "finish", "summary": "done"},
    )
    assert validate_tool_call(
        FINISH_ACTION_SCHEMA,
        {"type": "finish", "summary": "done", "objective_met": "yes"},
    )


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
