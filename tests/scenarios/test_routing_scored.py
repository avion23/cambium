"""Capability/quality-constrained model selection scenarios.

H2 builds on the merged solution-C ledger and H1 lanes: when a task declares
``requirements``, :func:`score_providers` filters candidates strictly by
capability (``quality == "high"`` keeps only ``ProviderTier.STRONG``
providers) and ranks eligible providers with the shared pure selection
objective. A weak-tier
provider is never substituted for one that fails the task's constraints, and
unknown requirement keys fail closed. Without ``requirements`` the supervisor
keeps the exact ``select_lane`` behavior from H1.

These scenarios run in the fast tier: pure selector scoring, plan-requirement
validation, and the batch pre-assignment pass without worker subprocesses. One
slow end-to-end scenario checks that the ``task_assigned`` event carries the
requirements.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any, cast

import pytest

from cambium.diffundo import ProviderConfig, ProviderTier
from cambium.routing import (
    DEFAULT_TOKEN_WINDOW_ALLOWANCE,
    LaneState,
    ProviderDebt,
    score_providers,
    validate_requirements,
)
from cambium.supervisor import _preassign_lanes, _validate_plan_task


def _pc(name: str, model: str, **overrides: Any) -> ProviderConfig:
    base: dict[str, Any] = dict(
        tier=ProviderTier.FAST,
        base_url="http://127.0.0.1:1",
        api_key_env=f"CAMBIUM_PROVIDER_{name.upper()}_API_KEY",
        model=model,
    )
    base.update(overrides)
    return ProviderConfig(name=name, **base)


def _config_file(
    path: Path, providers: list[tuple[str, str, str, int]]
) -> Path:
    """Write a minimal valid provider config; (name, model, tier, rpm) entries."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "providers": [
                    {
                        "name": name,
                        "tier": tier,
                        "base_url": "http://127.0.0.1:1",
                        "api_key_env": f"CAMBIUM_PROVIDER_{name.upper()}_API_KEY",
                        "rpm": rpm,
                        "enabled": True,
                        "model": model,
                    }
                    for name, model, tier, rpm in providers
                ]
            }
        ),
        encoding="utf-8",
    )
    return path


def _spec(
    task_id: str,
    config_path: Path,
    *,
    requirements: dict[str, Any] | None = None,
    tier: str = "fast",
) -> dict[str, Any]:
    spec = {
        "task_id": task_id,
        "fanout_config": {"tier": tier},
        "model_candidates": ["m1", "m2"],
        "provider_config_path": str(config_path),
    }
    if requirements is not None:
        spec["requirements"] = requirements
    return spec


def _plan_task_spec(session_dir: Path, task_id: str) -> dict[str, Any]:
    return {
        "task_id": task_id,
        "task": "run one task",
        "repo": str(session_dir / "repo"),
        "worktree_path": str(session_dir / "wt"),
        "branch": "wt-b",
    }


# --------------------------------------------------------------------------- #
# 1. strict capability filter: never substitute a failing model
# --------------------------------------------------------------------------- #


def test_score_providers_strict_filter_never_substitutes_weak_tier() -> None:
    providers = [
        _pc("weak", "m1", tier=ProviderTier.FAST),
        _pc("strong", "m1", tier=ProviderTier.STRONG),
    ]
    # weak is 0% utilized; strong is 90% utilized — scoring alone would pick
    # weak, but the STRICT capability filter must never allow it
    debt = {
        "weak": ProviderDebt(),
        "strong": ProviderDebt(tokens=int(0.9 * DEFAULT_TOKEN_WINDOW_ALLOWANCE)),
    }
    scored = score_providers(providers, ["m1"], debt, requirements={"quality": "high"})
    assert [(name, model) for name, model, _ in scored] == [("strong", "m1")]
    # no provider satisfies the requirement -> fail closed, never downgrade
    with pytest.raises(ValueError):
        score_providers(
            providers[:1], ["m1"], debt, requirements={"quality": "high"}
        )
    # "normal" (and absent) applies no tier restriction
    normal = score_providers(
        providers, ["m1"], debt, requirements={"quality": "normal"}
    )
    assert {name for name, _model, _score in normal} == {"weak", "strong"}
    assert score_providers(providers, ["m1"], debt)[0][0] == "weak"


def test_score_providers_strict_filter_applies_in_batch_preassignment(
    tmp_path,
) -> None:
    config_path = _config_file(
        tmp_path / "providers.json",
        [
            ("weak", "m1", "fast", 60),
            ("strong", "m1", "strong", 60),
        ],
    )
    # quality=high requires the STRONG tier; the pinned fanout tier must
    # match, or the assignment would be filtered out by the worker's tier
    # routing (the (provider, model, tier) assignment is atomic).
    specs = [
        _spec(f"t-{i}", config_path, requirements={"quality": "high"}, tier="strong")
        for i in range(2)
    ]
    lanes: dict[str, LaneState] = {}
    debt = {"weak": ProviderDebt(), "strong": ProviderDebt()}

    _preassign_lanes(specs, debt, lanes)

    # both tasks bind to the strong provider even though weak is idle
    assert [spec["assigned_provider"] for spec in specs] == ["strong", "strong"]
    assert lanes["strong"].in_flight == 2
    # weak was never even considered (tier filter), so no lane exists for it
    assert "weak" not in lanes or lanes["weak"].in_flight == 0


# --------------------------------------------------------------------------- #
# 2. scored ordering: shared quality objective
# --------------------------------------------------------------------------- #


def test_score_providers_ranks_shared_quality_objective() -> None:
    providers = [_pc("a", "m1"), _pc("b", "m2")]
    # equal utilization (0 tokens): A has 9/10 cache hits and 2s avg latency,
    # B has 1/10 cache hits and 20s avg latency -> A must win on
    # W_CACHE + W_LATENCY
    debt = {
        "a": ProviderDebt(
            requests=10, cache_hit_count=9, latency_total_s=20.0, latency_count=10
        ),
        "b": ProviderDebt(
            requests=10, cache_hit_count=1, latency_total_s=200.0, latency_count=10
        ),
    }
    scored = score_providers(providers, ["m1", "m2"], debt)
    assert scored[0][0] == "a"
    score_a, score_b = scored[0][2], scored[1][2]
    assert score_a < score_b
    # Token utilization is an admission-balancing concern and does not change
    # the quality ranking used when requirements select this path.
    debt["b"].tokens = int(0.9 * DEFAULT_TOKEN_WINDOW_ALLOWANCE)
    scored = score_providers(providers, ["m1", "m2"], debt)
    assert scored[0][0] == "a"
    assert scored[1][2] == score_b
    assert scored[0][2] == score_a


# --------------------------------------------------------------------------- #
# 3. requirements validation: fail closed at plan validation
# --------------------------------------------------------------------------- #


def test_requirements_validated_at_plan_validation(tmp_path) -> None:
    base = _plan_task_spec(tmp_path, "t-req")
    for bad in (
        {"quality": "ultra"},           # unknown quality value
        {"quality": 1},                 # non-string quality
        {"unknown_key": True},          # unknown key
        {"min_context_window": 0},      # not positive
        {"min_context_window": -8},     # negative
        {"min_context_window": True},   # bool is not an int
        {"min_context_window": "8k"},   # non-int
    ):
        with pytest.raises(ValueError):
            _validate_plan_task(tmp_path, dict(base, requirements=bad))
    # a non-dict requirements is rejected too
    with pytest.raises(ValueError):
        _validate_plan_task(tmp_path, dict(base, requirements="high"))
    # valid forms pass and are retained on the spec
    ok = _validate_plan_task(
        tmp_path, dict(base, requirements={"quality": "high"})
    )
    assert ok["requirements"] == {"quality": "high"}
    ok = _validate_plan_task(
        tmp_path,
        dict(base, requirements={"quality": "normal", "min_context_window": 128_000}),
    )
    assert ok["requirements"] == {
        "quality": "normal",
        "min_context_window": 128_000,
    }
    # absent/empty requirements stay absent (no scoring path)
    assert "requirements" not in _validate_plan_task(tmp_path, dict(base))


def test_selector_rejects_unknown_requirement_keys() -> None:
    providers = [_pc("a", "m1"), _pc("b", "m2")]
    with pytest.raises(ValueError):
        score_providers(
            providers, ["m1", "m2"], {}, requirements={"tier": "strong"}
        )
    with pytest.raises(ValueError):
        validate_requirements({"quality": "high", "context": 8000})


# --------------------------------------------------------------------------- #
# 4. regression: requirements absent -> select_lane behavior identical
# --------------------------------------------------------------------------- #


def test_supervisor_resolution_without_requirements_uses_select_lane(
    tmp_path,
) -> None:
    # a requirement-free task resolves exactly like H1 select_lane: idle lane
    # wins config-order ties, and a capped lane is skipped
    config_path = _config_file(
        tmp_path / "providers.json", [("a", "m1", "fast", 60), ("b", "m2", "fast", 60)]
    )
    specs = [_spec("t-0", config_path), _spec("t-1", config_path)]
    lanes: dict[str, LaneState] = {}
    _preassign_lanes(specs, {"a": ProviderDebt(), "b": ProviderDebt()}, lanes)
    assert [spec["assigned_provider"] for spec in specs] == ["a", "b"]
    # requirements={"quality": "normal"} is "present": the scoring path runs
    # (no tier restriction), and with no usage evidence both providers score
    # identically so the config-order tiebreak binds both tasks to a — this
    # discriminates the scoring path from select_lane, which would spread the
    # wave a, b, a, b across lanes
    specs = [
        _spec("t-0", config_path, requirements={"quality": "normal"}),
        _spec("t-1", config_path, requirements={"quality": "normal"}),
    ]
    _preassign_lanes(specs, {"a": ProviderDebt(), "b": ProviderDebt()}, lanes)
    assert [spec["assigned_provider"] for spec in specs] == ["a", "a"]


# --------------------------------------------------------------------------- #
# 5. end-to-end: task_assigned carries the requirements (auditable assignment)
# --------------------------------------------------------------------------- #


from http.server import BaseHTTPRequestHandler, HTTPServer  # noqa: E402

_SUMMARY_CONTROL_OPEN = "<cambium-summary-control>\n"
_SUMMARY_CONTROL_CLOSE = "\n</cambium-summary-control>"


def _summary_completion(
    body: dict[str, Any], *, default_model: str
) -> dict[str, Any] | None:
    """Return a strict synthetic summary response without consuming actions."""
    messages = body.get("messages")
    if not isinstance(messages, list) or not messages:
        return None
    last = messages[-1]
    content = last.get("content") if isinstance(last, dict) else None
    if not isinstance(content, str) or not content.startswith(_SUMMARY_CONTROL_OPEN):
        return None
    try:
        control = json.loads(
            content.removeprefix(_SUMMARY_CONTROL_OPEN).removesuffix(
                _SUMMARY_CONTROL_CLOSE
            )
        )
    except (json.JSONDecodeError, TypeError, ValueError):
        return None
    required = {
        "sequence",
        "source_sha256",
        "source_message_count",
        "through_turn",
    }
    if not required <= control.keys():
        return None
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
    model = body.get("model")
    if not isinstance(model, str) or not model:
        model = default_model
    return {
        "id": "chatcmpl-summary-fixture",
        "object": "chat.completion",
        "model": model,
        "choices": [
            {
                "index": 0,
                "message": {
                    "role": "assistant",
                    "content": json.dumps(
                        summary, sort_keys=True, separators=(",", ":")
                    ),
                },
                "finish_reason": "stop",
            }
        ],
        # Keep pre-existing action-usage assertions stable. Dedicated summary
        # tests cover accounting with non-zero usage.
        "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
    }


class _FakeServer:
    """OpenAI-compatible /chat/completions server on an ephemeral port."""

    def __init__(self, behaviors: list[tuple[int, dict[str, Any], float]]) -> None:
        import threading

        self.behaviors = list(behaviors)
        self.calls: list[dict[str, Any]] = []
        self.summary_calls: list[dict[str, Any]] = []
        self._lock = threading.Lock()
        self._httpd = HTTPServer(("127.0.0.1", 0), _Handler)
        cast(Any, self._httpd).fake = self
        self._thread = threading.Thread(
            target=self._httpd.serve_forever,
            kwargs={"poll_interval": 0.001},
            daemon=True,
        )
        self._thread.start()
        self.base_url = f"http://127.0.0.1:{self._httpd.server_port}"

    def record(self, body: dict[str, Any], headers: dict[str, str | None]) -> int:
        with self._lock:
            self.calls.append(body)
            return len(self.calls) - 1

    def behavior_at(self, index: int) -> tuple[int, dict[str, Any], float]:
        return self.behaviors[index] if index < len(self.behaviors) else self.behaviors[-1]

    def close(self) -> None:
        self._httpd.shutdown()
        self._httpd.server_close()
        self._thread.join()


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"  # noqa: N818

    def do_POST(self) -> None:  # noqa: N802 (http.server API)
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b""
        import json as _json

        try:
            body = _json.loads(raw.decode("utf-8") or "{}")
        except _json.JSONDecodeError:
            body = {}
        server: _FakeServer = cast(Any, self.server).fake
        summary_response = _summary_completion(body, default_model="m1")
        if summary_response is None:
            index = server.record(body, {})
            status, payload, delay = server.behavior_at(index)
        else:
            with server._lock:
                server.summary_calls.append(body)
            status, payload, delay = 200, summary_response, 0.0
        if delay:
            import time

            time.sleep(delay)
        encoded = _json.dumps(payload).encode("utf-8")
        try:
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(encoded)))
            self.end_headers()
            self.wfile.write(encoded)
        except OSError:
            pass

    def log_message(self, format: str, *args: object) -> None:
        pass


def _finish_payload(model: str) -> dict[str, Any]:
    content = json.dumps({"type": "finish", "summary": "done on " + model})
    return {
        "id": "chatcmpl-scored",
        "object": "chat.completion",
        "model": model,
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": content},
                "finish_reason": "stop",
            }
        ],
        "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
    }


@pytest.mark.slow
def test_task_assigned_event_carries_requirements(tmp_path, monkeypatch) -> None:
    """A requirement-constrained task's ``task_assigned`` event records the
    requirements, so the assignment is auditable against the task's
    constraints."""
    import subprocess

    from cambium.supervisor import read_events, run_plan

    server = _FakeServer([(200, _finish_payload("m1"), 0.0)])
    monkeypatch.setenv("CAMBIUM_PROVIDER_STRONG_API_KEY", "scored-secret-strong")
    monkeypatch.setenv("CAMBIUM_PROVIDER_WEAK_API_KEY", "scored-secret-weak")
    monkeypatch.setenv("NO_PROXY", "127.0.0.1,localhost")
    monkeypatch.setenv("no_proxy", "127.0.0.1,localhost")
    config_path = _config_file(
        tmp_path / "providers.json",
        [
            ("strong", "m1", "strong", 60),
            ("weak", "m2", "fast", 60),
        ],
    )
    # the strong provider's base_url must point at the fake server; rewrite
    # the config file with the live base_url
    import json as _json

    raw = _json.loads(config_path.read_text(encoding="utf-8"))
    for provider in raw["providers"]:
        if provider["name"] == "strong":
            provider["base_url"] = server.base_url
            provider["timeout_s"] = 5.0
            provider["max_retries"] = 0
    config_path.write_text(_json.dumps(raw), encoding="utf-8")

    session_dir = tmp_path / "session"
    repo = session_dir / "repo"
    repo.mkdir(parents=True)
    subprocess.run(["git", "init", "-b", "main", str(repo)], check=True, capture_output=True)
    subprocess.run(
        ["git", "-C", str(repo), "config", "user.name", "routing-scored-test"], check=True
    )
    subprocess.run(
        ["git", "-C", str(repo), "config", "user.email", "routing@test"], check=True
    )
    subprocess.run(["git", "-C", str(repo), "config", "gc.auto", "0"], check=True)
    (repo / "target.txt").write_text("fixture\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(repo), "add", "-A"], check=True, capture_output=True)
    subprocess.run(
        ["git", "-C", str(repo), "commit", "-m", "initial"], check=True, capture_output=True
    )
    base = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "refs/heads/main"],
        check=True, capture_output=True, text=True,
    ).stdout.strip()

    try:
        spec = {
            "task_id": "t-scored",
            "task": "append a line to target.txt and finish",
            "repo": str(repo),
            "worktree_path": str(session_dir / "wt"),
            "branch": "wt-scored",
            "base_commit": base,
            "fanout_config": {"tier": "strong", "call_budget_s": 5.0},
            "model_candidates": ["m1", "m2"],
            "requirements": {"quality": "high"},
            "provider_config_path": str(config_path),
            "provider_env_keys": [
                "CAMBIUM_PROVIDER_STRONG_API_KEY",
                "CAMBIUM_PROVIDER_WEAK_API_KEY",
                "NO_PROXY",
                "no_proxy",
            ],
            "ready_timeout_s": 5.0,
            "max_wall_s": 20.0,
            "max_tokens": 200_000,
            "max_restarts": 0,
            "heartbeat_interval_s": 0.05,
        }
        result = asyncio.run(
            run_plan(
                session_dir, {"tasks": [spec]},
                routing_state_path=str(tmp_path / "routing-state.json"),
            )
        )
        events = read_events(session_dir)
        assert result.exit_code == 0, result
        assigned = [
            event["payload"]
            for event in events
            if event["kind"] == "task_assigned"
        ]
        assert len(assigned) == 1
        payload = assigned[0]
        # the STRICT capability filter bound the task to the strong provider
        # even though the weak provider is idle
        assert payload["assigned_provider"] == "strong"
        assert payload["model"] == "m1"
        assert payload["requirements"] == {"quality": "high"}
        # the worker really called the strong provider with the assigned model
        assert len(server.calls) == 1
        assert server.calls[0]["model"] == "m1"
    finally:
        server.close()


# --------------------------------------------------------------------------- #
# 6. assignment writes the provider's tier; a pinned tier constrains candidates
# --------------------------------------------------------------------------- #


def test_assignment_writes_tier_and_pinned_tier_constrains(tmp_path, monkeypatch) -> None:
    """The (provider, model, tier) assignment is atomic: the worker routes
    calls by tier, so the resolved fanout tier must match the assigned
    provider's tier, and a caller-pinned tier must act as a hard filter."""
    config_path = _config_file(
        tmp_path / "providers.json",
        [
            ("weak", "m1", "fast", 60),
            ("strong", "m2", "strong", 60),
        ],
    )
    monkeypatch.setenv("CAMBIUM_PROVIDERS", str(config_path.resolve()))
    debt: dict[str, ProviderDebt] = {}
    lanes = {"weak": LaneState(), "strong": LaneState()}

    # no pinned tier: the assignment writes the chosen provider's own tier
    spec: dict[str, Any] = {
        "task_id": "t1",
        "fanout_config": {},
        "model_candidates": ["m1", "m2"],
        "provider_config_path": str(config_path),
    }
    from cambium.supervisor import _resolve_model_candidates

    assert _resolve_model_candidates(spec, debt, lanes)
    assert spec["assigned_provider"] in ("weak", "strong")
    assert spec["fanout_config"]["tier"] == (
        "fast" if spec["assigned_provider"] == "weak" else "strong"
    )
    assert spec["fanout_config"]["model"] in ("m1", "m2")

    # pinned tier "fast": only the fast provider may serve, even though
    # "strong" is idle and would otherwise win on utilization.
    pinned: dict[str, Any] = {
        "task_id": "t2",
        "fanout_config": {"tier": "fast"},
        "model_candidates": ["m1", "m2"],
        "provider_config_path": str(config_path),
    }
    debt = {"strong": ProviderDebt(tokens=0)}
    assert _resolve_model_candidates(pinned, debt, {"weak": LaneState(), "strong": LaneState()})
    assert pinned["assigned_provider"] == "weak"
    assert pinned["fanout_config"]["tier"] == "fast"
