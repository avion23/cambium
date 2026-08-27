"""Summary compaction recovery when a provider flags the raw context tail."""

from __future__ import annotations

import asyncio
import json
import threading
from pathlib import Path
from typing import Any

import pytest
from diffundo_helpers import FakeServer, _config, _ok_payload, _set_keys

from cambium import worker
from cambium.diffundo import (
    AllProvidersFailed,
    Diffundo,
    HealthState,
    ProviderError,
    ProviderOutcome,
    ProviderStatus,
    ProviderTier,
)
from cambium.fencing import write_generation
from cambium.redact import Redactor

_SECRET = "FLAGGED_SUMMARY_TAIL_SECRET"
_USAGE = {"prompt_tokens": 7, "completion_tokens": 3, "total_tokens": 10}
_FLAGGED = {
    "error": {
        "type": "invalid_request_error",
        "code": "invalid_prompt",
        "message": "Prompt was flagged by the usage policy",
    }
}
_SUMMARY = json.dumps(
    {
        "type": "summary_entry",
        "objective": "preserved the coding objective",
        "outcome": "completed the work segment",
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


class _Writer:
    def __init__(self) -> None:
        self.lines: list[bytes] = []

    def write(self, data: bytes) -> None:
        self.lines.append(data)

    async def drain(self) -> None:
        pass

    def messages(self) -> list[dict[str, Any]]:
        return [json.loads(line) for line in self.lines if line.strip()]


class _ObservedDiffundo(Diffundo):
    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.summary_failures: list[AllProvidersFailed] = []
        self.summary_failure_health: list[HealthState] = []

    async def summary_call(
        self,
        tier: ProviderTier,
        prompt: dict[str, Any],
        *,
        model: str | None = None,
        budget_usd: float | None = None,
        allow_model_substitution: bool = False,
        requirements: Any = None,
    ) -> Any:
        try:
            return await super().summary_call(
                tier,
                prompt,
                model=model,
                budget_usd=budget_usd,
                allow_model_substitution=allow_model_substitution,
                requirements=requirements,
            )
        except AllProvidersFailed as exc:
            self.summary_failures.append(exc)
            self.summary_failure_health.append(self.health("p_summary"))
            raise


def _summary_server(monkeypatch: pytest.MonkeyPatch, behaviors: list[Any]) -> FakeServer:
    server = FakeServer(behaviors)
    _set_keys(monkeypatch, "K_SUMMARY")
    return server


def _config_for(tmp_path: Path) -> worker.AgentConfig:
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    write_generation(worktree, 1)
    return worker.AgentConfig(
        task_id="summary-content-flagged",
        generation=1,
        task="finish the task",
        worktree=worktree,
        base_commit=None,
        fanout_config={},
        max_turns=1,
        max_tokens=200_000,
        shell_permission=True,
        network_permission=False,
        heartbeat_interval_s=0.05,
        max_wall_s=60.0,
        checkpoint_root=tmp_path / "checkpoints",
        redactor=Redactor(secret_values={_SECRET}),
    )


def _router(server: FakeServer) -> _ObservedDiffundo:
    return _ObservedDiffundo(
        (
            _config(
                "p_summary", server, "K_SUMMARY", model="loopback-model", max_retries=2
            ),
        ),
        pause_timeout_s=0.01,
    )


async def _run(
    config: worker.AgentConfig, router: _ObservedDiffundo, writer: _Writer
) -> dict[str, Any]:
    return await worker._run_agent_loop(
        config=config,
        router=router,
        tier=ProviderTier.FAST,
        model="loopback-model",
        worktree=config.worktree,
        writer=writer,
        stop=threading.Event(),
        progress=worker.AgentProgress(),
    )


def _summary_calls(server: FakeServer) -> list[dict[str, Any]]:
    return [
        call
        for call in server.calls
        if any(
            str(message.get("content", "")).startswith("<cambium-summary-control>\n")
            for message in call.get("messages", [])
            if isinstance(message, dict)
        )
    ]


def _failed_summary_events(writer: _Writer) -> list[dict[str, Any]]:
    return [
        message
        for message in writer.messages()
        if message.get("type") == "usage_event"
        and message.get("call_kind") == "summary"
        and "failure_reason" in message
    ]


def test_content_flagged_summary_retries_once_with_redacted_tail(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    server = _summary_server(
        monkeypatch,
        [
            (
                200,
                _ok_payload(
                    json.dumps({"type": "finish", "summary": _SECRET}),
                    model="loopback-model",
                    usage=_USAGE,
                ),
                0.0,
            ),
            (400, _FLAGGED, 0.0),
            (200, _ok_payload(_SUMMARY, model="loopback-model", usage=_USAGE), 0.0),
        ],
    )
    router = _router(server)
    writer = _Writer()
    try:
        outcome = asyncio.run(_run(_config_for(tmp_path), router, writer))

        assert outcome["status"] == "succeeded"
        assert outcome["failure_reason"] is None
        assert outcome["summary"] == _SECRET
        calls = _summary_calls(server)
        assert len(calls) == 2
        first_text = json.dumps(calls[0]["messages"])
        retry_text = json.dumps(calls[1]["messages"])
        assert _SECRET in first_text
        assert _SECRET not in retry_text
        assert "***" in retry_text
        assert calls[1]["messages"] != calls[0]["messages"]
        assert calls[1]["messages"][:2] == calls[0]["messages"][:2]
        assert calls[1]["messages"][-1] == calls[0]["messages"][-1]
        assert calls[1]["messages"][-2]["content"].startswith('{"summary":"***')
        assert len(calls[1]["messages"][-2]["content"]) < len(
            calls[0]["messages"][-2]["content"]
        )
        assert len(server.calls) == 3
        assert router.summary_failure_health == [HealthState.HEALTHY]
        assert router.health("p_summary") is HealthState.HEALTHY
        assert len(router.summary_failures) == 1
        error = router.summary_failures[0].last_error
        assert isinstance(error, ProviderError)
        assert error.outcome is ProviderOutcome.CONTENT_FLAGGED
        assert _failed_summary_events(writer)[0]["failure_reason"].startswith(
            "content_flagged:"
        )
    finally:
        server.close()


def test_content_flagged_transform_preserves_trunk_and_earlier_tail() -> None:
    control = (
        "<cambium-summary-control>\n{}\n"
        "</cambium-summary-control>"
    )
    prompt = {
        "messages": [
            {"role": "system", "content": "stable system"},
            {"role": "user", "content": "stable task"},
            {"role": "assistant", "content": "earlier raw content"},
            {"role": "user", "content": "safe prefix\nflagged tail"},
            {"role": "user", "content": control},
        ]
    }

    retry = worker._transform_content_flagged_summary_prompt(prompt, None)

    assert retry["messages"][:3] == prompt["messages"][:3]
    assert retry["messages"][-1] == prompt["messages"][-1]
    assert retry["messages"][-2]["content"] == "safe prefix\n*** [redacted]"
    assert "flagged tail" not in retry["messages"][-2]["content"]
    assert prompt["messages"][-2]["content"] == "safe prefix\nflagged tail"


def test_content_flagged_summary_retry_fails_without_health_damage(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    server = _summary_server(
        monkeypatch,
        [
            (
                200,
                _ok_payload(
                    json.dumps({"type": "finish", "summary": "done"}),
                    model="loopback-model",
                    usage=_USAGE,
                ),
                0.0,
            ),
            (400, _FLAGGED, 0.0),
            (400, _FLAGGED, 0.0),
        ],
    )
    router = _router(server)
    writer = _Writer()

    def no_retry(_: int) -> float:
        raise AssertionError("CONTENT_FLAGGED must not use provider retry backoff")

    monkeypatch.setattr(router, "_retry_delay", no_retry)
    try:
        outcome = asyncio.run(_run(_config_for(tmp_path), router, writer))

        assert outcome["status"] == "failed"
        assert outcome["failure_reason"] == (
            "compaction_failed: summary flagged by provider content filter"
        )
        assert len(server.calls) == 3
        calls = _summary_calls(server)
        assert len(calls) == 2
        assert calls[1]["messages"][:2] == calls[0]["messages"][:2]
        assert calls[1]["messages"][-1] == calls[0]["messages"][-1]
        assert calls[1]["messages"][-2] != calls[0]["messages"][-2]
        assert router.summary_failure_health == [HealthState.HEALTHY, HealthState.HEALTHY]
        assert router.health("p_summary") is HealthState.HEALTHY
        assert router.status("p_summary") is ProviderStatus.AVAILABLE
        assert len(router.summary_failures) == 2
        assert all(
            isinstance(failure.last_error, ProviderError)
            and failure.last_error.outcome is ProviderOutcome.CONTENT_FLAGGED
            for failure in router.summary_failures
        )
        events = _failed_summary_events(writer)
        assert len(events) == 2
        assert all(event["failure_reason"].startswith("content_flagged:") for event in events)
    finally:
        server.close()
