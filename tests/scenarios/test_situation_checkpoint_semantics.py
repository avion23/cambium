"""Pin situation checkpoint semantics: exact epoch bytes vs terminal strip."""

from __future__ import annotations

import asyncio
import copy
import json
import subprocess
import threading
from pathlib import Path
from typing import Any

from cambium import worker
from cambium.diffundo import ProviderTier
from cambium.fencing import write_generation


def _git_repo(tmp_path: Path) -> Path:
    """One real git repo so the loop's situation snapshot sees a valid HEAD."""
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-b", "main", str(repo)], check=True, capture_output=True)
    subprocess.run(
        ["git", "-C", str(repo), "config", "user.name", "situation-test"],
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "-C", str(repo), "config", "user.email", "situation@test"],
        check=True,
        capture_output=True,
    )
    (repo / "alpha.txt").write_text("alpha-content\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(repo), "add", "-A"], check=True, capture_output=True)
    subprocess.run(
        ["git", "-C", str(repo), "commit", "-m", "initial"], check=True, capture_output=True
    )
    return repo


class _LoopResult:
    """Duck-typed provider result for _run_agent_loop."""

    def __init__(self, content: str) -> None:
        self.content = content
        self.model = "scenario-model"
        self.provider = "scenario-provider"
        self.usage = {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15}
        self.latency_s = 0.01
        self.estimated_cost_usd = 0.0
        self.retry_after_s = None
        self.request_rate_status = None
        self.account_quota_owner = None
        self.prompt_prefix_bytes = None
        self.provider_cache_hit = None
        self.fell_back_from = None


class _LoopRouter:
    """Scripted router recording every sent prompt, like test_worker_agent_loop.

    Answers a terminal summary-flush call (prompt carrying a
    ``<cambium-summary-control>`` message) with a valid summary entry keyed by
    the control block, so the real loop can run to completion.
    """

    def __init__(self, responses: list[str]) -> None:
        self.responses = list(responses)
        self.prompts: list[dict[str, Any]] = []

    def declared_model(self, name: str) -> str:
        return ""

    @staticmethod
    def _summary_entry(control_content: str) -> str:
        control = json.loads(
            control_content.removeprefix("<cambium-summary-control>\n").removesuffix(
                "\n</cambium-summary-control>"
            )
        )
        summary = {
            "type": "summary_entry",
            "sequence": control["sequence"],
            "source_sha256": control["source_sha256"],
            "source_message_count": control["source_message_count"],
            "through_turn": control["through_turn"],
            "objective": "preserve the current coding objective",
            "outcome": "captured the completed work segment",
            "decisions_added": [],
            "decisions_superseded": [],
            "facts_added": [],
            "facts_invalidated": [],
            "files_and_symbols_changed": [],
            "verification_results": [],
            "relevant_failed_approaches": [],
            "open_items": [],
        }
        return json.dumps(summary, sort_keys=True, separators=(",", ":"))

    async def call(
        self,
        tier: ProviderTier,
        prompt: dict[str, Any],
        *,
        model: str | None = None,
        budget_usd: float | None = None,
        allow_model_substitution: bool = False,
    ) -> _LoopResult:
        self.prompts.append(prompt)
        control_content = None
        for message in reversed(prompt.get("messages", [])):
            content = message.get("content") if isinstance(message, dict) else None
            if isinstance(content, str) and content.startswith("<cambium-summary-control>\n"):
                control_content = content
                break
        if control_content is not None:
            return _LoopResult(self._summary_entry(control_content))
        return _LoopResult(self.responses.pop(0))


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

    persisted = json.loads(
        (checkpoint_root / checkpoint.checkpoint_ref).read_text(encoding="utf-8")
    )
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
    """Driving _run_agent_loop: the forced-final call carries the harness
    directive and the frame, but the durable terminal epoch checkpoint
    persists the exact sent bytes minus the directive."""
    repo = _git_repo(tmp_path)
    write_generation(repo, 1)
    checkpoint_root = tmp_path / "checkpoints"
    config = _config(repo, max_turns=2, context_reuse=True, checkpoint_root=checkpoint_root)
    router = _LoopRouter(
        [
            '{"type":"tool_call","name":"read_batch","arguments":{"paths":["alpha.txt"]}}',
            '{"type":"finish","summary":"read alpha","objective_met":true}',
        ]
    )

    outcome = asyncio.run(
        worker._run_agent_loop(
            config=config,
            router=router,  # type: ignore[arg-type]  # duck-typed router
            tier=ProviderTier.FAST,
            model="scenario-model",
            worktree=repo,
            writer=None,
            stop=threading.Event(),
            progress=worker.AgentProgress(),
        )
    )

    assert outcome["status"] == "succeeded"
    # Checkpointing the finished task does not make a summary request.
    assert len(router.prompts) == 2
    # The turn-limit arm injected the directive into the forced-final sent
    # prompt, right before the loop-state message carrying the bounded frame.
    sent_messages = router.prompts[1]["messages"]
    assert sent_messages[-2]["content"] == worker.FINAL_SYNTHESIS_DIRECTIVE
    assert "<cambium-situation " in sent_messages[-1]["content"]

    # Durable truth: every checkpoint on disk excludes the harness directive
    # from both the persisted request and its continuation suffix, and every
    # cache digest is exact over the persisted bytes.
    checkpoints = sorted(
        checkpoint_root.rglob("*.json"),
        key=lambda path: json.loads(path.read_text(encoding="utf-8"))["meta"]["epoch"],
    )
    assert checkpoints
    for path in checkpoints:
        persisted = json.loads(path.read_text(encoding="utf-8"))
        persisted_messages = persisted["content"]["provider_messages"]
        suffix = persisted["content"]["continuation_suffix"]
        for message in [*persisted_messages, *suffix]:
            assert message.get("content") != worker.FINAL_SYNTHESIS_DIRECTIVE
        cache_key = persisted["meta"]["cache_key"]
        assert cache_key["prefix_sha256"] == worker._messages_sha256(persisted_messages)
        assert cache_key["suffix_sha256"] == worker._messages_sha256(suffix)
        assert cache_key["full_sha256"] == worker._messages_sha256([*persisted_messages, *suffix])

    # The terminal checkpoint excludes both transient artifacts: the
    # directive message and the embedded frame in the loop-state message.
    terminal = json.loads(checkpoints[-1].read_text(encoding="utf-8"))
    for message in [
        *terminal["content"]["provider_messages"],
        *terminal["content"]["continuation_suffix"],
    ]:
        assert "<cambium-situation " not in str(message.get("content", ""))

    # The returned transcript excludes the directive and the embedded frame.
    for message in outcome["transcript"]:
        assert message.get("content") != worker.FINAL_SYNTHESIS_DIRECTIVE
        assert "<cambium-situation " not in str(message.get("content", ""))
