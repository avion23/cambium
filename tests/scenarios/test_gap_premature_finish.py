"""Budget pressure must not discard useful actions or change reported outcomes."""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

import pytest
from test_worker_agent_loop import _agent_config, _drive_loop, _make_worktree, _UsageScriptedRouter

from cambium import worker


@pytest.mark.parametrize("objective_met", [False, True])
def test_budget_advice_allows_last_check_and_preserves_verdict(
    tmp_path: Path, objective_met: bool,
) -> None:
    worktree = _make_worktree(tmp_path / "repo")
    config = _agent_config(worktree, max_tokens=20)
    check = {
        "name": "run_shell",
        "arguments": {"cmd": [sys.executable, "-c",
            "from pathlib import Path; assert Path('alpha.txt').read_text() == 'changed\\n'"],
        },
    }
    router = _UsageScriptedRouter(
        [
            '{"name":"edit_file","arguments":{"path":"alpha.txt",'
            '"old_string":"alpha-content\\n","new_string":"changed\\n"}}',
            json.dumps(check),
            json.dumps({"type": "finish", "summary": "checked", "objective_met": objective_met}),
        ],
        [{"prompt_tokens": n, "completion_tokens": 0, "total_tokens": n} for n in (18, 19, 20)],
    )
    outcome = asyncio.run(_drive_loop(config, worktree, router))
    assert outcome["status"] == ("succeeded" if objective_met else "failed")
    assert "tool run_shell ok=True" in "\n".join(m["content"] for m in outcome["transcript"])
    assert (worktree / "alpha.txt").read_text() == "changed\n"
    assert len(router.prompts) == 3


def test_progress_detector_compares_contents_not_read_paths() -> None:
    detector = worker._ProgressDetector(max_no_progress_actions=2, progress_window=3)
    outcomes = [
        detector.observe(
            action={"type": "tool_call", "name": "read_batch", "arguments": {"paths": [path]}},
            result_content=f"--- {path} ---\nsame content",
        )
        for path in ("one.txt", "two.txt", "three.txt")
    ]
    assert outcomes == [False, False, True]


def test_progress_hash_retains_only_recent_reads() -> None:
    detector = worker._ProgressDetector(max_no_progress_actions=2, progress_window=3)
    for index in range(worker.MAX_PROGRESS_CONTENT_HASHES + 2):
        detector.observe(
            action={"type": "tool_call", "name": "read_batch",
                    "arguments": {"paths": [f"file-{index}.txt"]}},
            result_content=f"body-{index}",
        )
    assert len(detector._recent_content_hashes) == worker.MAX_PROGRESS_CONTENT_HASHES


def test_progress_hash_covers_body_tail_and_length() -> None:
    prefix = "x" * (16 * 1024)
    assert worker._progress_content_hash(prefix + "a") != worker._progress_content_hash(
        prefix + "b"
    )
    prefix = "x" * worker.MAX_PROGRESS_CONTENT_BYTES
    assert worker._progress_content_hash(prefix + "a") != worker._progress_content_hash(
        prefix + "ab"
    )


def test_prompt_context_usage_counts_the_serialized_raw_tail() -> None:
    messages = [
        {"role": "system", "content": "stable header"},
        {"role": "user", "content": "task"},
        {"role": "assistant", "content": "raw observation é"},
    ]
    fields = worker._prompt_context_usage_fields({"messages": messages}, call_kind="agent")
    assert fields["active_context_bytes"] == len(worker._canonical_json_bytes(messages))
    assert fields["summary_trunk_bytes"] == len(worker._canonical_json_bytes(messages[:2]))
    assert fields["raw_tail_bytes"] == len(worker._canonical_json_bytes(messages[2:]))
