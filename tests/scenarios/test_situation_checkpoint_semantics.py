"""Pin situation checkpoint semantics: exact epoch bytes vs terminal strip."""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

from cambium import worker


def _config(worktree: Path, **overrides: Any) -> worker.AgentConfig:
    values: dict[str, Any] = {
        "task_id": "situation-checkpoint-semantics",
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


def _framed_request() -> list[dict[str, Any]]:
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
    return [
        {"role": "system", "content": "system prefix é"},
        state_message,
    ]


def test_epoch_checkpoint_persists_exact_provider_request_including_frame(
    tmp_path: Path,
) -> None:
    """Epoch checkpoints persist exact provider-sent bytes, frame included."""
    checkpoint_root = tmp_path / "checkpoints"
    config = _config(tmp_path, checkpoint_root=checkpoint_root)
    original_request = _framed_request()
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

    persisted = json.loads((checkpoint_root / checkpoint.checkpoint_ref).read_text(encoding="utf-8"))
    assert persisted["content"]["provider_messages"] == original_request
    assert persisted["content"]["continuation_suffix"] == suffix
    assert worker._canonical_json_bytes(persisted["content"]["provider_messages"]) == (
        worker._canonical_json_bytes(original_request)
    )
    cache_key = persisted["meta"]["cache_key"]
    assert cache_key["prefix_sha256"] == worker._messages_sha256(original_request)
    assert cache_key["suffix_sha256"] == worker._messages_sha256(suffix)
    assert cache_key["full_sha256"] == worker._messages_sha256([*original_request, *suffix])
    assert cache_key["prefix_bytes"] == worker.prompt_prefix_bytes({"messages": original_request})
    assert "<cambium-situation " in persisted["content"]["provider_messages"][1]["content"]


def test_forced_finalization_terminal_checkpoint_excludes_directive(tmp_path: Path) -> None:
    """Terminal checkpoint strips the harness directive before persistence."""
    checkpoint_root = tmp_path / "checkpoints"
    config = _config(tmp_path, checkpoint_root=checkpoint_root)
    sent_messages = [*_framed_request(), worker._final_synthesis_message()]
    assert any(
        message.get("content") == worker.FINAL_SYNTHESIS_DIRECTIVE for message in sent_messages
    )

    # Same call shape as worker.py forced-finalization terminal path.
    terminal_messages = worker._strip_finalization_directive(copy.deepcopy(sent_messages))

    assert all(
        message.get("content") != worker.FINAL_SYNTHESIS_DIRECTIVE
        for message in terminal_messages
    )
    assert "<cambium-situation " in terminal_messages[1]["content"]

    checkpoint = worker._write_epoch_checkpoint(
        config,
        turn=2,
        epoch=2,
        provider_messages=copy.deepcopy(terminal_messages),
        continuation_suffix=[],
        provider="scenario-provider",
        model="scenario-model",
        tools_sha256="a" * 64,
        provider_compat={"scenario-provider": ("loopback", None)},
    )
    assert checkpoint is not None

    persisted = json.loads((checkpoint_root / checkpoint.checkpoint_ref).read_text(encoding="utf-8"))
    assert all(
        message.get("content") != worker.FINAL_SYNTHESIS_DIRECTIVE
        for message in persisted["content"]["provider_messages"]
    )
    assert worker._canonical_json_bytes(persisted["content"]["provider_messages"]) == (
        worker._canonical_json_bytes(terminal_messages)
    )
    cache_key = persisted["meta"]["cache_key"]
    assert cache_key["prefix_sha256"] == worker._messages_sha256(terminal_messages)
    assert cache_key["full_sha256"] == worker._messages_sha256(terminal_messages)
