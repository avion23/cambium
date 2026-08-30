"""Wave-3 regression coverage for deferred compaction and restart resume."""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import textwrap
import threading
from pathlib import Path
from typing import Any

import pytest

from cambium import worker
from cambium.diffundo import ProviderTier
from cambium.fencing import write_generation
from cambium.supervisor import read_events, run_plan


class _Writer:
    def __init__(self) -> None:
        self.lines: list[bytes] = []

    def write(self, data: bytes) -> None:
        self.lines.append(data)

    async def drain(self) -> None:
        pass

    def messages(self) -> list[dict[str, Any]]:
        return [json.loads(line) for line in self.lines if line.strip()]


class _CallResult:
    def __init__(self, content: str) -> None:
        self.content = content
        self.model = "loopback-model"
        self.usage = {"prompt_tokens": 7, "completion_tokens": 3, "total_tokens": 10}
        self.provider = "loopback-provider"
        self.latency_s = 0.01
        self.estimated_cost_usd = 0.0
        self.retry_after_s: float | None = None
        self.request_rate_status: str | None = None
        self.account_quota_owner: str | None = None
        self.prompt_prefix_bytes: int | None = None
        self.provider_cache_hit: bool | None = None


def _summary_response(control: dict[str, Any]) -> str:
    return json.dumps(
        {
            "type": "summary_entry",
            "sequence": control["sequence"],
            "source_sha256": control["source_sha256"],
            "source_message_count": control["source_message_count"],
            "through_turn": control["through_turn"],
            "objective": "preserve the coding objective",
            "outcome": "captured the completed work segment",
            "decisions_added": [],
            "decisions_superseded": [],
            "facts_added": [],
            "facts_invalidated": [],
            "files_and_symbols_changed": [],
            "verification_results": [],
            "relevant_failed_approaches": [],
            "open_items": [],
        },
        sort_keys=True,
        separators=(",", ":"),
    )


class _SummaryRouter:
    def __init__(self, malformed_summaries: int = 0) -> None:
        self.malformed_summaries = malformed_summaries
        self.summary_calls = 0
        self.prompts: list[dict[str, Any]] = []

    def declared_model(self, _name: str) -> str:
        return ""

    async def call(
        self,
        _tier: ProviderTier,
        prompt: dict[str, Any],
        *,
        model: str | None = None,
        budget_usd: float | None = None,
        allow_model_substitution: bool = False,
    ) -> _CallResult:
        del model, budget_usd, allow_model_substitution
        self.prompts.append(prompt)
        control_content = None
        messages = prompt.get("messages")
        if isinstance(messages, list):
            for message in reversed(messages):
                if (
                    isinstance(message, dict)
                    and isinstance(message.get("content"), str)
                    and message["content"].startswith("<cambium-summary-control>\n")
                ):
                    control_content = message["content"]
                    break
        if control_content is not None:
            self.summary_calls += 1
            if self.malformed_summaries:
                self.malformed_summaries -= 1
                return _CallResult("{}{}")
            control = json.loads(
                control_content.removeprefix("<cambium-summary-control>\n").removesuffix(
                    "\n</cambium-summary-control>"
                )
            )
            return _CallResult(_summary_response(control))
        return _CallResult(self.responses.pop(0))

    responses: list[str] = []


def _worktree(repo: Path) -> Path:
    repo.mkdir(parents=True)
    subprocess.run(["git", "init", "-b", "main", str(repo)], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.name", "compaction-test"], check=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.email", "compaction@test"], check=True)
    (repo / "state.txt").write_text("base\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(repo), "add", "state.txt"], check=True, capture_output=True)
    subprocess.run(
        ["git", "-C", str(repo), "commit", "-m", "initial"], check=True, capture_output=True
    )
    worktree = repo.parent / "worktree"
    subprocess.run(
        ["git", "-C", str(repo), "worktree", "add", "-b", "compaction", str(worktree), "main"],
        check=True,
        capture_output=True,
    )
    write_generation(worktree, 1)
    return worktree


def _config(worktree: Path, checkpoint_root: Path, **overrides: Any) -> worker.AgentConfig:
    values: dict[str, Any] = {
        "task_id": "compaction-agent",
        "generation": 1,
        "task": "continue and finish",
        "worktree": worktree,
        "base_commit": None,
        "fanout_config": {},
        "max_turns": 6,
        "max_tokens": 200_000,
        "shell_permission": True,
        "network_permission": False,
        "heartbeat_interval_s": 0.05,
        "max_wall_s": 60.0,
        "checkpoint_root": checkpoint_root,
        "context_reuse": True,
        "rolling_compact": True,
        "rolling_compact_threshold_high": 1,
        "rolling_compact_threshold_low": 1,
    }
    values.update(overrides)
    return worker.AgentConfig(**values)


async def _drive(
    config: worker.AgentConfig,
    worktree: Path,
    router: _SummaryRouter,
    writer: _Writer,
) -> dict[str, Any]:
    return await worker._run_agent_loop(
        config=config,
        router=router,  # type: ignore[arg-type]  # duck-typed Diffundo
        tier=ProviderTier.FAST,
        model="loopback-model",
        worktree=worktree,
        writer=writer,  # type: ignore[arg-type]  # duck-typed StreamWriter
        stop=threading.Event(),
        progress=worker.AgentProgress(),
        provider_compat={"loopback-provider": ("loopback", None)},
        run_request_id="compaction-run",
    )


def test_valid_summary_response_does_not_defer(tmp_path: Path) -> None:
    worktree = _worktree(tmp_path / "repo")
    router = _SummaryRouter()
    router.responses = [
        '{"type":"plan","steps":["continue"]}',
        '{"type":"finish","summary":"done","objective_met":true}',
    ]
    writer = _Writer()

    outcome = asyncio.run(
        _drive(_config(worktree, tmp_path / "checkpoints"), worktree, router, writer)
    )

    assert outcome["status"] == "succeeded"
    assert router.summary_calls == 2
    assert not [
        message for message in writer.messages() if message["type"] == "compaction_deferred"
    ]
    assert any(message["type"] == "context_epoch_advanced" for message in writer.messages())


def test_compaction_deferral_count_survives_generation_boundary(tmp_path: Path) -> None:
    worktree = _worktree(tmp_path / "repo")
    checkpoint_root = tmp_path / "checkpoints"
    first_config = _config(worktree, checkpoint_root, max_turns=10)
    first_router = _SummaryRouter(malformed_summaries=2)
    first_router.responses = [
        '{"type":"tool_call","name":"run_shell","arguments":{"cmd":["true"]}}',
        "not-an-action",
    ]
    first_writer = _Writer()

    first = asyncio.run(_drive(first_config, worktree, first_router, first_writer))

    assert first["status"] == "failed"
    checkpoint_path = checkpoint_root / first_config.task_id / "turn-001.json"
    checkpoint = json.loads(checkpoint_path.read_text(encoding="utf-8"))
    assert checkpoint["compaction_deferred"] is True
    assert checkpoint["consecutive_compaction_deferrals"] == 1

    write_generation(worktree, 2)
    resumed_config = _config(
        worktree,
        checkpoint_root,
        generation=2,
        max_turns=10,
        resume={
            "checkpoint_ref": f"{first_config.task_id}/turn-001.json",
            "epoch": 1,
            "child_results": [],
            "child_results_truncated": False,
            "workspace_changed": False,
        },
    )
    resumed_router = _SummaryRouter(malformed_summaries=4)
    resumed_router.responses = [
        '{"type":"plan","steps":["continue"]}',
        '{"type":"plan","steps":["continue again"]}',
        '{"type":"plan","steps":["continue"]}',
        '{"type":"plan","steps":["continue again"]}',
    ]
    resumed_writer = _Writer()

    resumed = asyncio.run(_drive(resumed_config, worktree, resumed_router, resumed_writer))

    assert resumed["status"] == "failed"
    assert resumed["failure_reason"] == (
        "compaction_failed: summary response must be exactly one JSON object"
    )
    assert len(
        [
            message
            for message in resumed_writer.messages()
            if message["type"] == "compaction_deferred"
        ]
    ) == 1
    assert len(
        [message for message in resumed_writer.messages() if message["type"] == "compaction_failed"]
    ) == 1
    assert resumed_router.summary_calls == 4


def _write_restart_worker(path: Path, wire_log: Path, prompt_log: Path, init_log: Path) -> None:
    source_root = Path(__file__).resolve().parents[2] / "src"
    path.write_text(
        textwrap.dedent(
            """
            import asyncio
            import json
            import sys
            import threading
            import time
            from pathlib import Path

            sys.path.insert(0, __SRC_ROOT__)
            from cambium import worker
            from cambium.diffundo import ProviderTier

            WIRE = Path(__WIRE_LOG__)
            PROMPTS = Path(__PROMPT_LOG__)
            INITS = Path(__INIT_LOG__)

            def append(path, value):
                path.parent.mkdir(parents=True, exist_ok=True)
                with path.open("a", encoding="utf-8") as stream:
                    stream.write(json.dumps(value, sort_keys=True) + "\\n")

            class Writer:
                def write(self, data):
                    with WIRE.open("ab") as stream:
                        stream.write(data)
                    sys.stdout.buffer.write(data)
                    sys.stdout.buffer.flush()

                async def drain(self):
                    pass

            class Result:
                def __init__(self, content):
                    self.content = content
                    self.model = "loopback-model"
                    self.usage = {"prompt_tokens": 7, "completion_tokens": 3, "total_tokens": 10}
                    self.provider = "loopback-provider"
                    self.latency_s = 0.01
                    self.estimated_cost_usd = 0.0
                    self.retry_after_s = None
                    self.request_rate_status = None
                    self.account_quota_owner = None
                    self.prompt_prefix_bytes = None
                    self.provider_cache_hit = None

            def summary(control):
                return json.dumps({
                    "type": "summary_entry",
                    "sequence": control["sequence"],
                    "source_sha256": control["source_sha256"],
                    "source_message_count": control["source_message_count"],
                    "through_turn": control["through_turn"],
                    "objective": "preserve the objective",
                    "outcome": "captured the work",
                    "decisions_added": [], "decisions_superseded": [],
                    "facts_added": [], "facts_invalidated": [],
                    "files_and_symbols_changed": [], "verification_results": [],
                    "relevant_failed_approaches": [], "open_items": [],
                }, sort_keys=True, separators=(",", ":"))

            class Router:
                def __init__(self, generation):
                    self.generation = generation
                    self.agent_calls = 0
                    self.summary_calls = 0

                def declared_model(self, _name):
                    return ""

                async def call(self, _tier, prompt, **_kwargs):
                    control_content = None
                    messages = prompt.get("messages")
                    if isinstance(messages, list):
                        for message in reversed(messages):
                            if (
                                isinstance(message, dict)
                                and isinstance(message.get("content"), str)
                                and message["content"].startswith("<cambium-summary-control>\\n")
                            ):
                                control_content = message["content"]
                                break
                    is_summary = control_content is not None
                    append(PROMPTS, {
                        "generation": self.generation,
                        "summary": is_summary,
                        "last": (control_content or "")[:200],
                    })
                    if is_summary:
                        self.summary_calls += 1
                        if self.generation == 1:
                            return Result("{}{}")
                        control = json.loads(
                            control_content.removeprefix("<cambium-summary-control>\\n").removesuffix(
                                "\\n</cambium-summary-control>"
                            )
                        )
                        return Result(summary(control))
                    self.agent_calls += 1
                    if self.generation == 1:
                        if self.agent_calls == 1:
                            return Result(
                                '{"type":"tool_call","name":"run_shell",'
                                '"arguments":{"cmd":["true"]}}'
                            )
                        await asyncio.sleep(3600)
                    if self.agent_calls == 1:
                        return Result(
                            '{"type":"tool_call","name":"run_shell",'
                            '"arguments":{"cmd":["true"]}}'
                        )
                    return Result(
                        '{"type":"finish","summary":"resumed","objective_met":true}'
                    )

            async def main():
                init = json.loads(sys.stdin.readline())
                generation = init.get("generation", 1)
                append(INITS, init)
                writer = Writer()
                await worker.send(writer, {
                    "type": "ready", "request_id": init["request_id"],
                    "task_id": init["task_id"], "generation": generation, "proto": 1,
                })
                run = json.loads(sys.stdin.readline())
                configured = dict(init)
                configured["rolling_compact_threshold_high"] = 1
                configured["rolling_compact_threshold_low"] = 1
                config = worker.AgentConfig.from_init(configured)
                config = worker._merge_task_config(config, configured, run)
                outcome = await worker._run_agent_loop(
                    config=config,
                    router=Router(generation),
                    tier=ProviderTier.FAST,
                    model="loopback-model",
                    worktree=Path(run["worktree_path"]),
                    writer=writer,
                    stop=threading.Event(),
                    progress=worker.AgentProgress(),
                    provider_compat={"loopback-provider": ("loopback", None)},
                    run_request_id=run["request_id"],
                )
                await worker._emit_result_envelope(writer, {
                    **outcome,
                    "request_id": run["request_id"],
                    "task_id": init["task_id"],
                    "generation": generation,
                    "requires_commit": False,
                    "commits": [], "files_changed": [], "diff": "",
                    "diff_truncated": False,
                })
                await worker.send(writer, {
                    "type": "exit_message", "task_id": init["task_id"],
                    "generation": generation, "reason": "done",
                })

            asyncio.run(main())
            """
        )
        .replace("__SRC_ROOT__", repr(str(source_root)))
        .replace("__WIRE_LOG__", repr(str(wire_log)))
        .replace("__PROMPT_LOG__", repr(str(prompt_log)))
        .replace("__INIT_LOG__", repr(str(init_log))),
        encoding="utf-8",
    )


def test_deferred_compaction_survives_stall_restart_and_later_folds(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    session = tmp_path / "session"
    repo = session / "repo"
    repo.mkdir(parents=True)
    subprocess.run(["git", "init", "-b", "main", str(repo)], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.name", "compaction-test"], check=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.email", "compaction@test"], check=True)
    (repo / "state.txt").write_text("base\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(repo), "add", "state.txt"], check=True, capture_output=True)
    subprocess.run(
        ["git", "-C", str(repo), "commit", "-m", "initial"], check=True, capture_output=True
    )
    base = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()

    wire_log = tmp_path / "wire.jsonl"
    prompt_log = tmp_path / "prompts.jsonl"
    init_log = tmp_path / "inits.jsonl"
    restart_worker = tmp_path / "restart_worker.py"
    _write_restart_worker(restart_worker, wire_log, prompt_log, init_log)
    monkeypatch.setattr("cambium.supervisor.RESTART_BASE_DELAY_S", 0.01)
    monkeypatch.setenv(
        "PYTHONPATH",
        os.pathsep.join(
            filter(
                None,
                [str(Path(__file__).resolve().parents[2] / "src"), os.environ.get("PYTHONPATH")],
            )
        ),
    )

    task = {
        "task_id": "compaction-resume",
        "task": "exercise deferred compaction across restart",
        "repo": str(repo),
        "worktree_path": str(session / "worktree"),
        "branch": "compaction-resume",
        "base_commit": base,
        "worker": str(restart_worker),
        "provider_env_keys": [],
        "heartbeat_interval_s": 0.02,
        "heartbeat_timeout_s": 1.0,
        "ready_timeout_s": 2.0,
        "max_wall_s": 10.0,
        "max_restarts": 1,
    }

    result = asyncio.run(run_plan(session, {"tasks": [task]}, context_reuse=True))

    assert result.exit_code == 0
    assert result.results[0].restarts == 1
    wire = [json.loads(line) for line in wire_log.read_text(encoding="utf-8").splitlines()]
    deferred = [message for message in wire if message.get("type") == "compaction_deferred"]
    assert len(deferred) == 1
    assert len(deferred[0]["reason"].encode()) <= worker.MAX_ENVELOPE_FIELD_CHARS
    checkpoint = json.loads(
        (
            session
            / ".cambium"
            / "checkpoints"
            / "compaction-resume"
            / "turn-001.json"
        ).read_text(encoding="utf-8")
    )
    assert checkpoint["compaction_deferred"] is True

    inits = [json.loads(line) for line in init_log.read_text(encoding="utf-8").splitlines()]
    assert len(inits) == 2
    assert inits[1]["generation"] == 2
    assert inits[1]["resume"]["checkpoint_ref"].endswith("turn-001.json")

    prompts = [json.loads(line) for line in prompt_log.read_text(encoding="utf-8").splitlines()]
    resumed = [prompt for prompt in prompts if prompt["generation"] == 2]
    assert resumed and resumed[0]["summary"] is False
    assert any(prompt["summary"] for prompt in resumed[1:])

    events = read_events(session)
    assert any(
        event["kind"] == "restart_scheduled" and event["payload"]["restart_count"] == 1
        for event in events
    )
