"""Fast worker-boundary regressions for the SituationFrame wiring."""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

from cambium import worker


def _config(worktree: Path, **overrides: Any) -> worker.AgentConfig:
    values: dict[str, Any] = {
        "task_id": "situation-events",
        "generation": 1,
        "task": "inspect the worktree",
        "worktree": worktree,
        "base_commit": None,
        "fanout_config": {},
        "max_turns": 3,
        "max_tokens": 200_000,
        "shell_permission": False,
        "network_permission": False,
        "heartbeat_interval_s": 0.05,
        "max_wall_s": 60.0,
        "checkpoint_root": None,
        "context_reuse": False,
    }
    values.update(overrides)
    return worker.AgentConfig(**values)


def test_epoch_checkpoint_preserves_exact_provider_request(tmp_path: Path) -> None:
    worktree = tmp_path / "worktree"
    checkpoint_root = tmp_path / "checkpoints"
    config = _config(worktree, checkpoint_root=checkpoint_root)
    state_message = worker._context_state_message(
        code_changed=False,
        verified_after_change=False,
        verification_failed=False,
        no_progress_actions=0,
        budget_new_tokens=0,
        previous_prompt_tokens=0,
        turn=1,
        situation_frame=(
            '<cambium-situation version="1" source_watermark="9" '
            'frame_sha256="frame">\nMISSION\n</cambium-situation>'
        ),
    )
    original_request = [
        {"role": "system", "content": "system prefix é"},
        state_message,
    ]
    suffix = [{"role": "user", "content": "tool output"}]

    checkpoint = worker._write_epoch_checkpoint(
        config,
        turn=1,
        epoch=1,
        provider_messages=copy.deepcopy(original_request),
        continuation_suffix=copy.deepcopy(suffix),
        provider="scenario-provider",
        model="scenario-model",
        tools_sha256="a" * 64,
        provider_compat={"scenario-provider": ("loopback", None)},
    )
    assert checkpoint is not None

    path = checkpoint_root / checkpoint.checkpoint_ref
    persisted = json.loads(path.read_text(encoding="utf-8"))
    assert persisted["content"]["provider_messages"] == original_request
    assert persisted["content"]["continuation_suffix"] == suffix
    cache_key = persisted["meta"]["cache_key"]
    assert cache_key["prefix_sha256"] == worker._messages_sha256(original_request)
    assert cache_key["suffix_sha256"] == worker._messages_sha256(suffix)
    assert cache_key["full_sha256"] == worker._messages_sha256([*original_request, *suffix])
    assert cache_key["prefix_bytes"] == worker.prompt_prefix_bytes({"messages": original_request})
