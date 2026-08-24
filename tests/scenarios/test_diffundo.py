"""Scenario tests for the Diffundo provider router (src/cambium/diffundo.py).

No mocks, no network: each scenario drives real ``Diffundo.call`` against fake
OpenAI-compatible HTTP servers
(``http.server`` in background threads), following the test-strategy rule of
``asyncio.run()`` inside sync test functions. Scenarios map to the architecture
and cascade-design contracts:

1. cascade fallback across tier providers (500 -> success, provenance).
2. tier filtering + model pinning.
3. circuit breaker: retryable failures -> COOLDOWN skipped; auth error first
   call -> DISABLED (cascade-design §2.4, first-call included).
4. token bucket: rpm=1 -> second call cascades (D8f).
5. all providers exhausted -> pause (bounded) then AllProvidersFailed.
6. prompt guard: volatile timestamp/request_id in the static head rejected,
   static top accepted (D8c).
7. no local cache: the instance has no mutable mapping attribute (D1).
"""

from __future__ import annotations

import asyncio
import json
import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any, cast

import pytest

from cambium.diffundo import (
    AllProvidersFailed,
    CallResult,
    Diffundo,
    HealthState,
    PromptStructureError,
    ProviderConfig,
    ProviderError,
    ProviderOutcome,
    ProviderStatus,
    ProviderTier,
    _RawResponse,
    prompt_prefix_bytes,
    prompt_prefix_estimate_tokens,
    validate_prompt_structure,
)

# --------------------------------------------------------------------------- #
# Fake provider server (http.server in a thread — no network)
# --------------------------------------------------------------------------- #


class FakeServer:
    """OpenAI-compatible /chat/completions server on an ephemeral port.

    ``behaviors`` is a list of ``(status, payload, delay_s)`` consumed in order;
    the last behavior repeats for any further request. Requests are recorded.
    """

    def __init__(
        self,
        behaviors: list[
            tuple[int, dict[str, Any], float] | tuple[int, dict[str, Any], float, dict[str, str]]
        ],
        *,
        echo_authorization_in_body: bool = False,
        host: str = "127.0.0.1",
    ) -> None:
        self.behaviors = list(behaviors)
        self.echo_authorization_in_body = echo_authorization_in_body
        self.calls: list[dict[str, Any]] = []
        self.request_headers: list[dict[str, str | None]] = []
        self._lock = threading.Lock()
        self._httpd = cast(_FakeHTTPServer, HTTPServer((host, 0), _Handler))
        self._httpd.fake = self
        self._thread = threading.Thread(
            target=self._httpd.serve_forever,
            kwargs={"poll_interval": 0.001},
            daemon=True,
        )
        self._thread.start()
        self.base_url = f"http://{host}:{self._httpd.server_port}"

    def record(self, body: dict[str, Any], headers: dict[str, str | None]) -> int:
        with self._lock:
            self.calls.append(body)
            self.request_headers.append(headers)
            return len(self.calls) - 1

    def behavior_at(self, index: int) -> tuple[int, dict[str, Any], float, dict[str, str]]:
        behavior = self.behaviors[index] if index < len(self.behaviors) else self.behaviors[-1]
        if len(behavior) == 3:
            status, payload, delay = behavior
            return status, payload, delay, {}
        status, payload, delay, headers = behavior
        return status, payload, delay, headers

    def close(self) -> None:
        self._httpd.shutdown()
        self._httpd.server_close()
        self._thread.join()


class _FakeHTTPServer(HTTPServer):
    fake: FakeServer


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def do_POST(self) -> None:  # noqa: N802 (http.server API)
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b""
        try:
            body = json.loads(raw.decode("utf-8") or "{}")
        except json.JSONDecodeError:
            body = {}
        server = cast(_FakeHTTPServer, self.server).fake
        index = server.record(
            body,
            {
                "User-Agent": self.headers.get("User-Agent"),
                "Authorization": self.headers.get("Authorization"),
            },
        )
        status, payload, delay, extra_headers = server.behavior_at(index)
        if delay:
            time.sleep(delay)
        if server.echo_authorization_in_body:
            error = payload.get("error")
            if isinstance(error, dict):
                payload = {
                    **payload,
                    "error": {
                        **error,
                        "message": (
                            f"{error.get('message', '')}; {self.headers.get('Authorization')}"
                        ),
                    },
                }
        encoded = json.dumps(payload).encode("utf-8")
        try:
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(encoded)))
            for name, value in extra_headers.items():
                self.send_header(name, value)
            self.end_headers()
            self.wfile.write(encoded)
        except OSError:
            pass  # the client timed out (budget-capped attempt) and closed first

    def do_GET(self) -> None:  # noqa: N802 (http.server API)
        # urllib replays a 301/302/303 POST redirect as a GET; record it like a
        # POST so redirect-leak canaries observe a stray contact at the target.
        self.do_POST()

    def log_message(self, format: str, *args: object) -> None:
        pass


def _ok_payload(
    content: str, *, model: str | None = None, usage: dict[str, Any] | None = None
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "id": "chatcmpl-test",
        "object": "chat.completion",
        "model": model or "m",
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": content},
                "finish_reason": "stop",
            }
        ],
    }
    if usage is not None:
        payload["usage"] = usage
    return payload


def _error_payload(message: str) -> dict[str, Any]:
    return {"error": {"message": message, "type": "test_error", "code": "test"}}


def _tool_call_payload(tool_calls: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "id": "chatcmpl-tool",
        "object": "chat.completion",
        "model": "m-tool",
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": None, "tool_calls": tool_calls},
                "finish_reason": "tool_calls",
            }
        ],
        "usage": {"prompt_tokens": 2, "completion_tokens": 2},
    }


PROMPT = {"messages": [{"role": "user", "content": "hello"}]}
STATIC_HEAD = {
    "messages": [{"role": "system", "content": "You are a coding assistant.\nBe concise.\n"}]
}


def test_summary_call_uses_extended_deadline(monkeypatch: pytest.MonkeyPatch) -> None:
    router = Diffundo((), call_budget_s=1.0, summary_call_budget_s=3.0)
    deadlines: list[float] = []

    async def capture_deadline(
        tier: ProviderTier,
        model: str | None,
        deadline: float,
        **kwargs: Any,
    ) -> list[Any]:
        deadlines.append(deadline)
        return []

    monkeypatch.setattr(router, "_await_candidates", capture_deadline)
    started = time.monotonic()
    with pytest.raises(AllProvidersFailed):
        asyncio.run(router.summary_call(ProviderTier.FAST, PROMPT, model="m"))

    assert len(deadlines) == 1
    assert 2.0 < deadlines[0] - started <= 3.1


def _config(
    name: str,
    server: FakeServer,
    env: str,
    *,
    tier: ProviderTier = ProviderTier.FAST,
    model: str = "",
    **overrides: Any,
) -> ProviderConfig:
    base: dict[str, Any] = dict(timeout_s=5.0, max_retries=0, rpm=60, enabled=True, model=model)
    base.update(overrides)
    return ProviderConfig(name=name, tier=tier, base_url=server.base_url, api_key_env=env, **base)


def _set_keys(monkeypatch: pytest.MonkeyPatch, *names: str) -> None:
    for name in names:
        monkeypatch.setenv(name, f"sk-test-{name}")


def test_prompt_structure_rejects_timestamp_on_line_five() -> None:
    prompt = {
        "messages": [
            {
                "role": "system",
                "content": "stable 1\nstable 2\nstable 3\nstable 4\n2026-08-12T10:30:00Z",
            }
        ]
    }

    with pytest.raises(PromptStructureError, match=r"line 5: volatile timestamp token"):
        validate_prompt_structure(prompt)


def test_prompt_prefix_token_estimate_uses_utf8_bytes() -> None:
    prompt = {"messages": [{"role": "system", "content": "abcé"}]}
    expected_bytes = len("abcé".encode())

    assert prompt_prefix_bytes(prompt) == expected_bytes
    assert prompt_prefix_estimate_tokens(prompt) == expected_bytes // 4
    assert prompt_prefix_estimate_tokens(PROMPT) is None


# --------------------------------------------------------------------------- #
# 1. cascade fallback
# --------------------------------------------------------------------------- #


def test_cascade_falls_through_500_to_next_provider(tmp_path, monkeypatch) -> None:
    bad = FakeServer([(500, _error_payload("boom"), 0.0)])
    good = FakeServer([(200, _ok_payload("from good", model="m-good"), 0.0)])
    _set_keys(monkeypatch, "K_BAD", "K_GOOD")
    router = Diffundo(
        (
            _config("p_bad", bad, "K_BAD"),
            _config("p_good", good, "K_GOOD"),
        )
    )
    try:
        result = asyncio.run(router.call(ProviderTier.FAST, PROMPT))
        assert isinstance(result, CallResult)
        assert result.provider == "p_good"
        assert result.model == "m-good"
        assert result.content == "from good"
        assert result.tier is ProviderTier.FAST
        assert len(bad.calls) == 1  # 500 -> fall through
        assert len(good.calls) == 1
        assert result.request_rate_status == "available"
        assert result.retry_after_s is None
        assert result.account_quota_owner is None
    finally:
        bad.close()
        good.close()


def test_pinned_endpoint_death_falls_back_same_tier_and_records_origin(monkeypatch) -> None:
    pinned = FakeServer([(503, _error_payload("Endpoint is unavailable"), 0.0)])
    same_tier = FakeServer([(200, _ok_payload("served by sibling", model="m-sibling"), 0.0)])
    other_tier = FakeServer([(200, _ok_payload("must not be reached", model="m-strong"), 0.0)])
    _set_keys(monkeypatch, "K_PINNED_DEAD", "K_SIBLING", "K_STRONG")
    router = Diffundo(
        (
            _config("p_pinned", pinned, "K_PINNED_DEAD", model="m-pinned"),
            _config("p_sibling", same_tier, "K_SIBLING", model="m-sibling"),
            _config("p_strong", other_tier, "K_STRONG", tier=ProviderTier.STRONG, model="m-strong"),
        ),
        primary_provider="p_pinned",
        pause_timeout_s=0.01,
    )
    try:
        result = asyncio.run(router.call(ProviderTier.FAST, PROMPT, model="m-pinned"))
        assert result.provider == "p_sibling"
        assert result.fell_back_from == "p_pinned"
        assert len(pinned.calls) == 1
        assert len(same_tier.calls) == 1
        assert other_tier.calls == []
    finally:
        pinned.close()
        same_tier.close()
        other_tier.close()


def test_pinned_429_retry_after_does_not_trigger_fallback(monkeypatch) -> None:
    limited = FakeServer([(429, _error_payload("busy"), 0.0, {"Retry-After": "60"})])
    sibling = FakeServer([(200, _ok_payload("must not serve", model="m-sibling"), 0.0)])
    _set_keys(monkeypatch, "K_LIMITED_PIN", "K_429_SIBLING")
    router = Diffundo(
        (
            _config("p_limited", limited, "K_LIMITED_PIN", model="m-pinned"),
            _config("p_sibling", sibling, "K_429_SIBLING", model="m-sibling"),
        ),
        primary_provider="p_limited",
        pause_timeout_s=0.01,
    )
    try:
        with pytest.raises(AllProvidersFailed) as raised:
            asyncio.run(router.call(ProviderTier.FAST, PROMPT, model="m-pinned"))
        error = cast(ProviderError, raised.value.last_error)
        assert error.retry_after_s == 60.0
        assert error.is_real_death is False
        assert sibling.calls == []
    finally:
        limited.close()
        sibling.close()


def test_pinned_endpoint_death_without_alternative_remains_fatal(monkeypatch) -> None:
    dead = FakeServer([(503, _error_payload("server_error"), 0.0)])
    _set_keys(monkeypatch, "K_ONLY_PIN")
    router = Diffundo(
        (_config("p_only", dead, "K_ONLY_PIN", model="m-pinned"),),
        primary_provider="p_only",
        pause_timeout_s=0.01,
    )
    try:
        with pytest.raises(AllProvidersFailed) as raised:
            asyncio.run(router.call(ProviderTier.FAST, PROMPT, model="m-pinned"))
        assert raised.value.providers_tried == ("p_only",)
        error = cast(ProviderError, raised.value.last_error)
        assert error.is_real_death is True
    finally:
        dead.close()


def test_leased_provider_death_releases_lease_for_healthy_sibling(monkeypatch) -> None:
    incumbent = FakeServer(
        [
            (200, _ok_payload("incumbent", model="m-incumbent"), 0.0),
            (503, _error_payload("endpoint unavailable"), 0.0),
        ]
    )
    sibling = FakeServer([(200, _ok_payload("sibling", model="m-sibling"), 0.0)])
    _set_keys(monkeypatch, "K_LEASE_INCUMBENT", "K_LEASE_SIBLING")
    router = Diffundo(
        (
            _config("p_incumbent", incumbent, "K_LEASE_INCUMBENT", model="m-incumbent"),
            _config("p_sibling", sibling, "K_LEASE_SIBLING", model="m-sibling"),
        ),
        pause_timeout_s=0.01,
    )
    try:
        first = asyncio.run(router.call(ProviderTier.FAST, PROMPT, model="m-incumbent"))
        router.bind_provider(first.provider, first.model, root_task_id="lease-task")
        assert router.provider_lease is not None

        fallback = asyncio.run(router.call(ProviderTier.FAST, PROMPT, model="m-incumbent"))

        assert fallback.provider == "p_sibling"
        assert fallback.fell_back_from == "p_incumbent"
        assert router.provider_lease is None
        assert len(incumbent.calls) == 2
        assert len(sibling.calls) == 1

        # The caller can bind the successful replacement after the old lease
        # was released, preserving stickiness for the next turn.
        router.bind_provider(fallback.provider, fallback.model, root_task_id="lease-task")
        assert router.provider_lease is not None
        assert router.provider_lease.provider == "p_sibling"
    finally:
        incumbent.close()
        sibling.close()


def test_healthy_incumbent_keeps_lease_sticky(monkeypatch) -> None:
    incumbent = FakeServer(
        [
            (200, _ok_payload("first", model="m-incumbent"), 0.0),
            (200, _ok_payload("second", model="m-incumbent"), 0.0),
        ]
    )
    sibling = FakeServer([(200, _ok_payload("must not serve", model="m-sibling"), 0.0)])
    _set_keys(monkeypatch, "K_STICKY_INCUMBENT", "K_STICKY_SIBLING")
    router = Diffundo(
        (
            _config("p_incumbent", incumbent, "K_STICKY_INCUMBENT", model="m-incumbent"),
            _config("p_sibling", sibling, "K_STICKY_SIBLING", model="m-sibling"),
        ),
        primary_provider="p_incumbent",
        pause_timeout_s=0.01,
    )
    try:
        first = asyncio.run(router.call(ProviderTier.FAST, PROMPT, model="m-incumbent"))
        router.bind_provider(first.provider, first.model, root_task_id="sticky-task")
        lease = router.provider_lease

        second = asyncio.run(router.call(ProviderTier.FAST, PROMPT, model="m-incumbent"))

        assert second.provider == "p_incumbent"
        assert router.provider_lease is lease
        assert len(incumbent.calls) == 2
        assert sibling.calls == []
    finally:
        incumbent.close()
        sibling.close()


def test_transient_429_keeps_lease_through_cooldown(monkeypatch) -> None:
    incumbent = FakeServer(
        [
            (200, _ok_payload("first", model="m-incumbent"), 0.0),
            (429, _error_payload("busy"), 0.0, {"Retry-After": "60"}),
        ]
    )
    sibling = FakeServer([(200, _ok_payload("must not serve", model="m-sibling"), 0.0)])
    _set_keys(monkeypatch, "K_TRANSIENT_INCUMBENT", "K_TRANSIENT_SIBLING")
    router = Diffundo(
        (
            _config("p_incumbent", incumbent, "K_TRANSIENT_INCUMBENT", model="m-incumbent"),
            _config("p_sibling", sibling, "K_TRANSIENT_SIBLING", model="m-sibling"),
        ),
        primary_provider="p_incumbent",
        pause_timeout_s=0.01,
    )
    try:
        first = asyncio.run(router.call(ProviderTier.FAST, PROMPT, model="m-incumbent"))
        router.bind_provider(first.provider, first.model, root_task_id="transient-task")
        lease = router.provider_lease

        with pytest.raises(AllProvidersFailed) as raised:
            asyncio.run(router.call(ProviderTier.FAST, PROMPT, model="m-incumbent"))

        error = cast(ProviderError, raised.value.last_error)
        assert error.retry_after_s == 60.0
        assert error.is_real_death is False
        assert router.health("p_incumbent") is HealthState.COOLDOWN
        assert router.provider_lease is lease
        assert sibling.calls == []
    finally:
        incumbent.close()
        sibling.close()


def test_terminal_endpoint_death_is_skipped_until_a_new_router(monkeypatch) -> None:
    dead = FakeServer(
        [
            (503, _error_payload("endpoint is unavailable"), 0.0),
            (200, _ok_payload("fresh router"), 0.0),
        ]
    )
    healthy = FakeServer([(200, _ok_payload("healthy sibling"), 0.0)])
    _set_keys(monkeypatch, "K_TERMINAL_DEAD", "K_TERMINAL_HEALTHY")
    providers = (
        _config("p_terminal_dead", dead, "K_TERMINAL_DEAD", model="m"),
        _config("p_terminal_healthy", healthy, "K_TERMINAL_HEALTHY", model="m"),
    )
    router = Diffundo(
        providers,
        primary_provider="p_terminal_dead",
        pause_timeout_s=0.01,
    )
    try:
        first = asyncio.run(router.call(ProviderTier.FAST, PROMPT))
        assert first.provider == "p_terminal_healthy"
        assert router.health("p_terminal_dead") is HealthState.COOLDOWN

        # Once ordinary cooldown has elapsed, terminal-death memory still
        # excludes the dead lane while a healthy sibling can serve.
        router._runtime("p_terminal_dead").cooldown_until = time.monotonic() - 1.0
        assert [
            provider.name for provider in router._candidates(ProviderTier.FAST, None)
        ] == ["p_terminal_healthy"]
        second = asyncio.run(router.call(ProviderTier.FAST, PROMPT))
        assert second.provider == "p_terminal_healthy"
        assert len(dead.calls) == 1
        assert len(healthy.calls) == 2

        # The memory is router-local: a new Diffundo instance considers the
        # endpoint again, and the scripted recovery response can win.
        fresh = Diffundo(
            providers,
            primary_provider="p_terminal_dead",
            pause_timeout_s=0.01,
        )
        assert [
            provider.name for provider in fresh._candidates(ProviderTier.FAST, None)
        ][0] == "p_terminal_dead"
        recovered = asyncio.run(fresh.call(ProviderTier.FAST, PROMPT))
        assert recovered.provider == "p_terminal_dead"
        assert recovered.content == "fresh router"
        assert len(dead.calls) == 2
    finally:
        dead.close()
        healthy.close()


# --------------------------------------------------------------------------- #
# 2. tier filtering + model pin
# --------------------------------------------------------------------------- #


def test_tier_filtering_and_model_pin(tmp_path, monkeypatch) -> None:
    fast = FakeServer([(200, _ok_payload("fast", model="m1"), 0.0)])
    fast2 = FakeServer([(200, _ok_payload("fast m2", model="m2"), 0.0)])
    strong = FakeServer([(200, _ok_payload("strong", model="m-s"), 0.0)])
    balanced = FakeServer([(200, _ok_payload("balanced", model="m-b"), 0.0)])
    _set_keys(monkeypatch, "K_FAST", "K_FAST2", "K_STRONG", "K_BAL")
    router = Diffundo(
        (
            _config("p_fast", fast, "K_FAST", model="m1"),
            _config("p_fast2", fast2, "K_FAST2", model="m2"),
            _config("p_strong", strong, "K_STRONG", tier=ProviderTier.STRONG, model="m-s"),
            _config("p_bal", balanced, "K_BAL", tier=ProviderTier.BALANCED, model="m-b"),
        ),
        pause_timeout_s=0.01,  # the pin-out exhaustion pause is not this test's signal
    )
    try:
        result = asyncio.run(router.call(ProviderTier.BALANCED, PROMPT))
        assert result.provider == "p_bal"
        assert len(balanced.calls) == 1
        assert len(fast.calls) == 0 and len(strong.calls) == 0

        result = asyncio.run(router.call(ProviderTier.STRONG, PROMPT))
        assert result.provider == "p_strong"
        assert len(strong.calls) == 1 and len(fast.calls) == 0

        # model pin: fast tier + exact model -> only the matching provider runs
        result = asyncio.run(router.call(ProviderTier.FAST, PROMPT, model="m2"))
        assert result.provider == "p_fast2"
        assert len(fast2.calls) == 1
        assert len(fast.calls) == 0

        # pinning a model no tier provider declares -> AllProvidersFailed
        with pytest.raises(AllProvidersFailed):
            asyncio.run(router.call(ProviderTier.FAST, PROMPT, model="nope"))
        assert len(fast.calls) == 0 and len(fast2.calls) == 1
    finally:
        for server in (fast, fast2, strong, balanced):
            server.close()


def test_model_pin_falls_through_to_sibling_when_matching_provider_fails(
    tmp_path, monkeypatch
) -> None:
    """An explicitly substitution-enabled sibling may serve after the exact
    model lane fails; substitution is never an implicit fallback."""
    bad = FakeServer([(500, _error_payload("boom"), 0.0)])
    sibling = FakeServer([(200, _ok_payload("sibling served", model="m-other"), 0.0)])
    _set_keys(monkeypatch, "K_M2", "K_OTHER")
    router = Diffundo(
        (
            _config("p_m2", bad, "K_M2", model="m2"),
            _config(
                "p_other",
                sibling,
                "K_OTHER",
                model="m-other",
                allow_model_substitution=True,
            ),
        ),
        pause_timeout_s=0.01,
    )
    try:
        result = asyncio.run(
            router.call(
                ProviderTier.FAST,
                PROMPT,
                model="m2",
                allow_model_substitution=True,
            )
        )
        assert isinstance(result, CallResult)
        assert result.provider == "p_other"
        assert result.model == "m-other"
        assert result.content == "sibling served"
        assert len(bad.calls) == 1  # strict match failed -> fell through
        assert len(sibling.calls) == 1
    finally:
        bad.close()
        sibling.close()


def test_model_pin_does_not_authorize_provider_global_substitution(tmp_path, monkeypatch) -> None:
    """A provider opt-in cannot override a task's exact-model pin."""
    bad = FakeServer([(500, _error_payload("boom"), 0.0)])
    sibling = FakeServer([(200, _ok_payload("must not serve", model="m-other"), 0.0)])
    _set_keys(monkeypatch, "K_PINNED", "K_SUBSTITUTE")
    router = Diffundo(
        (
            _config("p_pinned", bad, "K_PINNED", model="m2"),
            _config(
                "p_substitute",
                sibling,
                "K_SUBSTITUTE",
                model="m-other",
                allow_model_substitution=True,
            ),
        ),
        pause_timeout_s=0.01,
    )
    try:
        with pytest.raises(AllProvidersFailed):
            asyncio.run(router.call(ProviderTier.FAST, PROMPT, model="m2"))
        assert len(bad.calls) == 1
        assert sibling.calls == []
    finally:
        bad.close()
        sibling.close()


def test_model_pin_unavailable_at_selection_falls_through_to_sibling(tmp_path, monkeypatch) -> None:
    """An explicitly substitution-enabled sibling remains eligible while the
    exact model lane is in cooldown."""
    bad = FakeServer([(500, _error_payload("boom"), 0.0)])
    sibling = FakeServer([(200, _ok_payload("sibling served", model="m-other"), 0.0)])
    _set_keys(monkeypatch, "K_M2", "K_OTHER")
    router = Diffundo(
        (
            _config("p_m2", bad, "K_M2", model="m2", cooldown_s=60),
            _config(
                "p_other",
                sibling,
                "K_OTHER",
                model="m-other",
                allow_model_substitution=True,
            ),
        ),
        pause_timeout_s=0.01,
    )
    try:
        first = asyncio.run(
            router.call(
                ProviderTier.FAST,
                PROMPT,
                model="m2",
                allow_model_substitution=True,
            )
        )
        assert first.provider == "p_other"
        assert len(bad.calls) == 1
        assert len(sibling.calls) == 1
        # p_m2 is in COOLDOWN; the next selection relaxes straight to the sibling.
        second = asyncio.run(
            router.call(
                ProviderTier.FAST,
                PROMPT,
                model="m2",
                allow_model_substitution=True,
            )
        )
        assert second.provider == "p_other"
        assert len(bad.calls) == 1  # cooldown skipped the strict match
        assert len(sibling.calls) == 2
    finally:
        bad.close()
        sibling.close()


def test_clear_provider_lease_also_clears_sticky_primary(monkeypatch) -> None:
    first = FakeServer([(200, _ok_payload("first"), 0.0)])
    second = FakeServer([(200, _ok_payload("second"), 0.0)])
    _set_keys(monkeypatch, "K_LEASE_FIRST", "K_LEASE_SECOND")
    router = Diffundo(
        (
            _config("p_first", first, "K_LEASE_FIRST", model="m"),
            _config("p_second", second, "K_LEASE_SECOND", model="m"),
        ),
        primary_provider="p_second",
    )
    try:
        bound = asyncio.run(router.call(ProviderTier.FAST, PROMPT))
        assert bound.provider == "p_second"
        assert router._primary_provider == "p_second"
        router.bind_provider("p_second", "m")
        router.clear_provider_lease()
        assert router.provider_lease is None
        assert router._primary_provider is None

        rebound = asyncio.run(router.call(ProviderTier.FAST, PROMPT))
        assert rebound.provider == "p_first"
    finally:
        first.close()
        second.close()


# --------------------------------------------------------------------------- #
# 3. circuit breaker
# --------------------------------------------------------------------------- #


def test_duplicate_half_open_probe_rejection_is_benign(monkeypatch) -> None:
    provider = ProviderConfig(
        name="p_probe",
        tier=ProviderTier.FAST,
        base_url="http://127.0.0.1:1",
        api_key_env="K_PROBE",
    )
    router = Diffundo((provider,))
    runtime = router._runtime(provider.name)
    runtime.health = HealthState.HALF_OPEN
    runtime.probe_in_flight = True
    runtime.outcomes.append(True)

    with pytest.raises(ProviderError) as raised:
        asyncio.run(router._quota_wrapped_attempt(provider, PROMPT))

    error = raised.value
    assert error.probe_already_in_flight is True
    assert runtime.health is HealthState.HALF_OPEN
    assert list(runtime.outcomes) == [True]
    # The real probe may recover independently; this rejection must not
    # append a false outcome that would re-cool the recovered provider.
    runtime.probe_in_flight = False
    runtime.health = HealthState.HEALTHY
    assert list(runtime.outcomes) == [True]


def test_breaker_three_failures_put_provider_in_cooldown_and_skip(tmp_path, monkeypatch) -> None:
    flaky = FakeServer([(500, _error_payload("intermittent"), 0.0)])
    good = FakeServer([(200, _ok_payload("good"), 0.0)])
    _set_keys(monkeypatch, "K_FLAKY", "K_GOOD")
    router = Diffundo(
        (
            _config("p_flaky", flaky, "K_FLAKY", max_retries=2, cooldown_s=60.0),
            _config("p_good", good, "K_GOOD"),
        )
    )
    try:
        result = asyncio.run(router.call(ProviderTier.FAST, PROMPT))
        assert result.provider == "p_good"
        # 1 attempt + 2 retries all failed on the wire
        assert len(flaky.calls) == 3
        assert router.health("p_flaky") is HealthState.COOLDOWN

        # second call: the COOLDOWN provider is skipped, no new requests hit it
        result2 = asyncio.run(router.call(ProviderTier.FAST, PROMPT))
        assert result2.provider == "p_good"
        assert len(flaky.calls) == 3  # skipped entirely
        assert len(good.calls) == 2
        assert router.status("p_flaky") is ProviderStatus.COOLDOWN
    finally:
        flaky.close()
        good.close()


def test_breaker_auth_error_first_call_disables(tmp_path, monkeypatch) -> None:
    auth = FakeServer([(401, _error_payload("unauthorized"), 0.0)])
    good = FakeServer([(200, _ok_payload("ok"), 0.0)])
    _set_keys(monkeypatch, "K_AUTH", "K_GOOD")
    router = Diffundo((_config("p_auth", auth, "K_AUTH"), _config("p_good", good, "K_GOOD")))
    try:
        result = asyncio.run(router.call(ProviderTier.FAST, PROMPT))
        assert result.provider == "p_good"
        assert len(auth.calls) == 1  # auth is non-retryable: no retries
        assert router.health("p_auth") is HealthState.DISABLED
        assert router.status("p_auth") is ProviderStatus.DISABLED

        # DISABLED is terminal for the session: never touched again
        result2 = asyncio.run(router.call(ProviderTier.FAST, PROMPT))
        assert result2.provider == "p_good"
        assert len(auth.calls) == 1
    finally:
        auth.close()
        good.close()


def test_retry_after_beyond_deadline_skips_retry_without_jitter(monkeypatch) -> None:
    server = FakeServer([(429, _error_payload("busy"), 0.0, {"Retry-After": "60"})])
    _set_keys(monkeypatch, "K_RETRY_LONG")
    # Budget must sit comfortably ABOVE one attempt yet BELOW the 60s
    # Retry-After: under xdist load a tight wall budget starves the first
    # attempt itself, which is starvation noise rather than the behavior
    # under test (no retry when the reset lands beyond the budget).
    router = Diffundo(
        (_config("p_retry_long", server, "K_RETRY_LONG", max_retries=1),),
        call_budget_s=30.0,
    )

    async def fail_sleep(delay: float) -> None:
        raise AssertionError("a delay beyond the call budget must not sleep")

    monkeypatch.setattr(asyncio, "sleep", fail_sleep)
    monkeypatch.setattr(Diffundo, "_retry_delay", lambda self, attempt_no: 0.0)
    try:
        with pytest.raises(AllProvidersFailed) as exc:
            asyncio.run(router.call(ProviderTier.FAST, PROMPT))
        assert len(server.calls) == 1
        failure = cast(AllProvidersFailed, exc.value)
        assert failure.last_error is not None
        last_error = cast(ProviderError, failure.last_error)
        assert last_error.retry_after_s == 60.0
    finally:
        server.close()


def test_retry_after_is_provider_local(monkeypatch) -> None:
    limited = FakeServer([(429, _error_payload("busy"), 0.0, {"Retry-After": "60"})])
    healthy = FakeServer([(200, _ok_payload("healthy"), 0.0)])
    _set_keys(monkeypatch, "K_RETRY_LIMITED", "K_RETRY_HEALTHY")
    router = Diffundo(
        (
            _config("p_retry_limited", limited, "K_RETRY_LIMITED"),
            _config("p_retry_healthy", healthy, "K_RETRY_HEALTHY"),
        )
    )
    sleeps: list[float] = []

    async def record_sleep(delay: float) -> None:
        sleeps.append(delay)

    monkeypatch.setattr(asyncio, "sleep", record_sleep)
    try:
        result = asyncio.run(router.call(ProviderTier.FAST, PROMPT))
        assert result.provider == "p_retry_healthy"
        assert sleeps == []
        assert len(limited.calls) == 1 and len(healthy.calls) == 1
    finally:
        limited.close()
        healthy.close()


def test_http_error_redacts_authorization_key_from_provider_error(tmp_path, monkeypatch) -> None:
    key = "sk-echoed-in-4xx-body"
    server = FakeServer(
        [
            (
                401,
                _error_payload("invalid credential; see https://body-user:body-pass@example.test"),
                0.0,
            )
        ],
        echo_authorization_in_body=True,
    )
    monkeypatch.setenv("K_ECHO", key)
    router = Diffundo(
        (_config("p_echo", server, "K_ECHO"),),
        pause_timeout_s=0.01,
    )
    try:
        with pytest.raises(AllProvidersFailed) as exc:
            asyncio.run(router.call(ProviderTier.FAST, PROMPT))
        failure = cast(AllProvidersFailed, exc.value)
        error = failure.last_error
        assert error is not None
        error = cast(ProviderError, error)
        assert error.outcome is ProviderOutcome.AUTH_ERROR
        assert "HTTP 401" in error.message
        assert "test_error" in error.message
        assert "invalid credential" in error.message
        assert "https://[REDACTED]@example.test" in error.message
        assert "body-pass" not in error.message
        assert key not in error.message
        assert key not in str(error)
        assert error.cause is not None
        assert key not in str(error.cause)
        assert error.__cause__ is not None
        assert key not in str(error.__cause__)
        assert key not in str(exc.value)
    finally:
        server.close()

    # the 429 Retry-After error keeps the key private too, and the honored
    # delay rides the terminal failure
    retry_key = "sk-retry-after-private"
    retry_server = FakeServer(
        [(429, _error_payload("invalid credential"), 0.0, {"Retry-After": "1"})],
        echo_authorization_in_body=True,
    )
    monkeypatch.setenv("K_RETRY_PRIVATE", retry_key)
    retry_router = Diffundo((_config("p_retry_private", retry_server, "K_RETRY_PRIVATE"),))
    try:
        with pytest.raises(AllProvidersFailed) as exc:
            asyncio.run(retry_router.call(ProviderTier.FAST, PROMPT))
        failure = cast(AllProvidersFailed, exc.value)
        assert failure.last_error is not None
        error = cast(ProviderError, failure.last_error)
        assert error.retry_after_s == 1.0
        assert retry_key not in str(exc.value)
        assert retry_key not in error.message
    finally:
        retry_server.close()


def test_cloudflare_1010_forbidden_is_error_not_auth_error(tmp_path, monkeypatch) -> None:
    blocked = FakeServer(
        [
            (
                403,
                _error_payload(
                    "Cloudflare Error 1010: access denied based on the browser's signature"
                ),
                0.0,
            )
        ]
    )
    _set_keys(monkeypatch, "K_BLOCKED")
    router = Diffundo((_config("p_blocked", blocked, "K_BLOCKED"),))
    try:
        with pytest.raises(AllProvidersFailed) as exc:
            asyncio.run(router.call(ProviderTier.FAST, PROMPT))
        failure = cast(AllProvidersFailed, exc.value)
        assert failure.last_error is not None
        error = cast(ProviderError, failure.last_error)
        assert error.outcome is ProviderOutcome.ERROR
        assert router.health("p_blocked") is HealthState.COOLDOWN
        assert router.status("p_blocked") is ProviderStatus.COOLDOWN
        assert "sk-test-K_BLOCKED" not in str(exc.value)
    finally:
        blocked.close()


def test_403_invalid_credential_is_quarantined_until_key_changes(monkeypatch) -> None:
    server = FakeServer(
        [
            (403, _error_payload("invalid_api_key: credential revoked"), 0.0),
            (200, _ok_payload("credential recovered"), 0.0),
        ]
    )
    _set_keys(monkeypatch, "K_ROTATE")
    router = Diffundo(
        (_config("p_rotate", server, "K_ROTATE", max_retries=2),),
        pause_timeout_s=0.01,
    )
    try:
        with pytest.raises(AllProvidersFailed) as first:
            asyncio.run(router.call(ProviderTier.FAST, PROMPT))
        error = cast(ProviderError, first.value.last_error)
        assert error.outcome is ProviderOutcome.AUTH_ERROR
        assert len(server.calls) == 1
        assert router.health("p_rotate") is HealthState.DISABLED

        # The same credential remains quarantined, without another request.
        with pytest.raises(AllProvidersFailed):
            asyncio.run(router.call(ProviderTier.FAST, PROMPT))
        assert len(server.calls) == 1

        # A rotated credential is a new auth identity and may probe again.
        monkeypatch.setenv("K_ROTATE", "sk-test-K_ROTATE-rotated")
        result = asyncio.run(router.call(ProviderTier.FAST, PROMPT))
        assert result.content == "credential recovered"
        assert len(server.calls) == 2
        assert router.health("p_rotate") is HealthState.HEALTHY
    finally:
        server.close()


def test_403_missing_model_entitlement_is_config_error(tmp_path, monkeypatch) -> None:
    server = FakeServer(
        [
            (
                403,
                {
                    "error": {
                        "code": "model_not_found",
                        "message": "The configured model is not available to this account",
                    }
                },
                0.0,
            )
        ]
    )
    _set_keys(monkeypatch, "K_MODEL_ENTITLEMENT")
    router = Diffundo(
        (_config("p_model_entitlement", server, "K_MODEL_ENTITLEMENT", max_retries=2),),
        pause_timeout_s=0.01,
    )
    try:
        with pytest.raises(AllProvidersFailed) as raised:
            asyncio.run(router.call(ProviderTier.FAST, PROMPT))
        error = cast(ProviderError, raised.value.last_error)
        assert error.outcome is ProviderOutcome.CONFIG_ERROR
        assert len(server.calls) == 1
        assert router.health("p_model_entitlement") is HealthState.DISABLED
    finally:
        server.close()


def test_403_quota_or_billing_exhaustion_cools_until_reset(monkeypatch) -> None:
    server = FakeServer(
        [
            (
                403,
                {
                    "error": {
                        "code": "insufficient_quota",
                        "message": "billing hard limit reached",
                    }
                },
                0.0,
                {"Retry-After": "7"},
            )
        ]
    )
    _set_keys(monkeypatch, "K_BILLING")
    router = Diffundo(
        (_config("p_billing", server, "K_BILLING", max_retries=0, cooldown_s=1.0),),
        pause_timeout_s=0.01,
    )
    started = time.monotonic()
    try:
        with pytest.raises(AllProvidersFailed) as raised:
            asyncio.run(router.call(ProviderTier.FAST, PROMPT))
        error = cast(ProviderError, raised.value.last_error)
        assert error.outcome is ProviderOutcome.QUOTA
        assert error.retry_after_s == 7.0
        assert error.request_rate_status == "cooldown"
        assert router.health("p_billing") is HealthState.COOLDOWN
        assert router._runtime("p_billing").cooldown_until >= started + 6.9
    finally:
        server.close()


def test_403_policy_refusal_falls_through_without_health_damage(monkeypatch) -> None:
    refusing = FakeServer(
        [
            (
                403,
                _error_payload("content_policy_violation: blocked by safety policy"),
                0.0,
            )
        ]
    )
    good = FakeServer([(200, _ok_payload("safe fallback"), 0.0)])
    _set_keys(monkeypatch, "K_POLICY", "K_POLICY_GOOD")
    router = Diffundo(
        (
            _config("p_policy", refusing, "K_POLICY"),
            _config("p_policy_good", good, "K_POLICY_GOOD"),
        )
    )
    try:
        result = asyncio.run(router.call(ProviderTier.FAST, PROMPT))
        assert result.provider == "p_policy_good"
        assert len(refusing.calls) == 1 and len(good.calls) == 1
        assert router.health("p_policy") is HealthState.UNKNOWN
    finally:
        refusing.close()
        good.close()


def test_403_waf_block_retries_with_bounded_backoff(monkeypatch) -> None:
    blocked = FakeServer(
        [(403, _error_payload("Web Application Firewall blocked automated traffic"), 0.0)]
    )
    _set_keys(monkeypatch, "K_WAF")
    router = Diffundo(
        (_config("p_waf", blocked, "K_WAF", max_retries=2),),
        retry_base_delay_s=0.2,
    )
    sleeps: list[float] = []

    async def record_sleep(delay: float) -> None:
        sleeps.append(delay)

    monkeypatch.setattr(asyncio, "sleep", record_sleep)
    monkeypatch.setattr(Diffundo, "_retry_delay", lambda self, attempt_no: 0.1)
    try:
        with pytest.raises(AllProvidersFailed) as raised:
            asyncio.run(router.call(ProviderTier.FAST, PROMPT))
        error = cast(ProviderError, raised.value.last_error)
        assert error.outcome is ProviderOutcome.ERROR
        assert len(blocked.calls) == 3
        assert sleeps == [0.1, 0.1]
        assert router.health("p_waf") is HealthState.COOLDOWN
    finally:
        blocked.close()


def test_tool_call_response_with_null_content_succeeds(tmp_path, monkeypatch) -> None:
    # A normal OpenAI tool-call completion carries content:null plus tool_calls;
    # it is a success, not a "content missing" malformed response. A tool call
    # with non-null text content keeps both the text and the call.
    tool_payload = {
        "id": "chatcmpl-tool",
        "object": "chat.completion",
        "model": "m-tool",
        "choices": [
            {
                "index": 0,
                "message": {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [
                        {
                            "id": "call_1",
                            "type": "function",
                            "function": {"name": "search", "arguments": '{"query": "x"}'},
                        }
                    ],
                },
                "finish_reason": "tool_calls",
            }
        ],
        "usage": {"prompt_tokens": 2, "completion_tokens": 2},
    }
    text_payload = {
        "id": "chatcmpl-tool2",
        "object": "chat.completion",
        "model": "m-tool2",
        "choices": [
            {
                "index": 0,
                "message": {
                    "role": "assistant",
                    "content": "explaining the call",
                    "tool_calls": [
                        {
                            "id": "call_2",
                            "type": "function",
                            "function": {"name": "f", "arguments": "{}"},
                        }
                    ],
                },
                "finish_reason": "tool_calls",
            }
        ],
        "usage": {"prompt_tokens": 2, "completion_tokens": 2},
    }
    server = FakeServer([(200, tool_payload, 0.0), (200, text_payload, 0.0)])
    _set_keys(monkeypatch, "K_TOOL")
    router = Diffundo((_config("p_tool", server, "K_TOOL"),))
    try:
        result = asyncio.run(router.call(ProviderTier.FAST, PROMPT))
        assert result.content == ""
        assert result.tool_calls is not None
        assert result.tool_calls[0]["function"]["name"] == "search"
        assert router.health("p_tool") is HealthState.HEALTHY

        result2 = asyncio.run(router.call(ProviderTier.FAST, PROMPT))
        assert result2.content == "explaining the call"
        assert result2.tool_calls is not None
        assert result2.tool_calls[0]["id"] == "call_2"
    finally:
        server.close()


def test_tool_call_response_rejects_malformed_empty_tool_call(tmp_path, monkeypatch) -> None:
    # A bare {} tool call has no function name; it must be rejected as a
    # malformed response, not forwarded as a valid-looking (name="") call.
    # A tool call whose function.name is "" is rejected the same way.
    malformed = FakeServer([(200, _tool_call_payload([{}]), 0.0)])
    _set_keys(monkeypatch, "K_MAL")
    router = Diffundo((_config("p_mal", malformed, "K_MAL"),))
    try:
        with pytest.raises(AllProvidersFailed) as exc:
            asyncio.run(router.call(ProviderTier.FAST, PROMPT))
        failure = cast(AllProvidersFailed, exc.value)
        assert failure.last_error is not None
        error = cast(ProviderError, failure.last_error)
        assert error.outcome is ProviderOutcome.ERROR
        assert "tool call without a function name" in error.message
        assert router.health("p_mal") is HealthState.COOLDOWN
    finally:
        malformed.close()

    empty = FakeServer(
        [(200, _tool_call_payload([{"function": {"name": "", "arguments": "{}"}}]), 0.0)]
    )
    _set_keys(monkeypatch, "K_EMPTY")
    empty_router = Diffundo((_config("p_empty", empty, "K_EMPTY"),))
    try:
        with pytest.raises(AllProvidersFailed) as exc:
            asyncio.run(empty_router.call(ProviderTier.FAST, PROMPT))
        failure = cast(AllProvidersFailed, exc.value)
        assert failure.last_error is not None
        error = cast(ProviderError, failure.last_error)
        assert error.outcome is ProviderOutcome.ERROR
        assert "tool call without a function name" in error.message
        assert empty_router.health("p_empty") is HealthState.COOLDOWN
    finally:
        empty.close()


def test_tool_call_response_rejects_malformed_arguments_json(tmp_path, monkeypatch) -> None:
    # A tool call whose arguments are not a JSON object is a malformed response
    # and must be rejected, never forwarded as a silent args={} call.
    malformed = FakeServer(
        [
            (
                200,
                _tool_call_payload(
                    [
                        {
                            "id": "call_1",
                            "type": "function",
                            "function": {"name": "read_file", "arguments": '{"path":'},
                        }
                    ]
                ),
                0.0,
            )
        ]
    )
    _set_keys(monkeypatch, "K_MALARGS")
    router = Diffundo((_config("p_malargs", malformed, "K_MALARGS"),))
    try:
        with pytest.raises(AllProvidersFailed) as exc:
            asyncio.run(router.call(ProviderTier.FAST, PROMPT))
        failure = cast(AllProvidersFailed, exc.value)
        assert failure.last_error is not None
        error = cast(ProviderError, failure.last_error)
        assert error.outcome is ProviderOutcome.ERROR
        assert "arguments" in error.message
        assert router.health("p_malargs") is HealthState.COOLDOWN
    finally:
        malformed.close()


def test_tool_call_response_with_valid_read_file_args_passes(tmp_path, monkeypatch) -> None:
    # A real read_file tool call with concrete arguments must still pass
    # unchanged through the malformed-call guard.
    server = FakeServer(
        [
            (
                200,
                _tool_call_payload(
                    [
                        {
                            "id": "call_read",
                            "type": "function",
                            "function": {
                                "name": "read_file",
                                "arguments": '{"path": "README.md", "offset": 5}',
                            },
                        }
                    ]
                ),
                0.0,
            )
        ]
    )
    _set_keys(monkeypatch, "K_READ")
    router = Diffundo((_config("p_read", server, "K_READ"),))
    try:
        result = asyncio.run(router.call(ProviderTier.FAST, PROMPT))
        assert result.content == ""
        assert result.tool_calls is not None
        assert result.tool_calls[0]["function"]["name"] == "read_file"
        assert result.tool_calls[0]["function"]["arguments"] == '{"path": "README.md", "offset": 5}'
        assert router.health("p_read") is HealthState.HEALTHY
    finally:
        server.close()


def test_200_refusal_content_cascades_to_next_provider(tmp_path, monkeypatch) -> None:
    # Issue 4: a 200 completion whose TEXT is a refusal must fall through like
    # any other refusal — it must not win the cascade as a "success".
    refusing = FakeServer([(200, _ok_payload("I can't assist with that."), 0.0)])
    ok = FakeServer([(200, _ok_payload("real answer"), 0.0)])
    _set_keys(monkeypatch, "K_REFUSE", "K_OK")
    router = Diffundo(
        (
            _config("p_refuse", refusing, "K_REFUSE"),
            _config("p_ok", ok, "K_OK"),
        )
    )
    try:
        result = asyncio.run(router.call(ProviderTier.FAST, PROMPT))
        assert result.provider == "p_ok"
        assert result.content == "real answer"
        # refusal is a request-level fall-through: never marks a provider down
        assert router.health("p_refuse") is HealthState.UNKNOWN
        assert len(refusing.calls) == 1 and len(ok.calls) == 1
    finally:
        refusing.close()
        ok.close()


def test_all_providers_refuse_raises_refusal_outcome(tmp_path, monkeypatch) -> None:
    # All-refused is distinct from all-down: the last error carries the REFUSAL
    # outcome and no provider is marked unhealthy (cascade-design §1.2).
    a = FakeServer([(200, _ok_payload("Sorry, I can't complete this request."), 0.0)])
    b = FakeServer([(200, _ok_payload("I cannot assist with that."), 0.0)])
    _set_keys(monkeypatch, "K_A", "K_B")
    router = Diffundo((_config("p_a", a, "K_A"), _config("p_b", b, "K_B")))
    try:
        with pytest.raises(AllProvidersFailed) as exc:
            asyncio.run(router.call(ProviderTier.FAST, PROMPT))
        failure = cast(AllProvidersFailed, exc.value)
        assert failure.last_error is not None
        error = cast(ProviderError, failure.last_error)
        assert error.outcome is ProviderOutcome.REFUSAL
        assert router.health("p_a") is HealthState.UNKNOWN
        assert router.health("p_b") is HealthState.UNKNOWN
    finally:
        a.close()
        b.close()


# --------------------------------------------------------------------------- #
# 4. token bucket
# --------------------------------------------------------------------------- #


def test_token_bucket_rpm_one_second_call_cascades(tmp_path, monkeypatch) -> None:
    first = FakeServer([(200, _ok_payload("first"), 0.0)])
    second = FakeServer([(200, _ok_payload("second"), 0.0)])
    _set_keys(monkeypatch, "K_1", "K_2")
    router = Diffundo(
        (_config("p_first", first, "K_1", rpm=1), _config("p_second", second, "K_2", rpm=1))
    )
    try:
        result = asyncio.run(router.call(ProviderTier.FAST, PROMPT))
        assert result.provider == "p_first"
        assert len(first.calls) == 1 and len(second.calls) == 0
        assert router.status("p_first") is ProviderStatus.RATE_LIMITED
        # the winning call consumed the last token: the event records it
        assert result.request_rate_status == "rate_limited"

        # first provider's bucket is empty -> skipped, cascade reaches p_second
        result2 = asyncio.run(router.call(ProviderTier.FAST, PROMPT))
        assert result2.provider == "p_second"
        assert len(first.calls) == 1  # not re-dispatched
        assert len(second.calls) == 1
    finally:
        first.close()
        second.close()


# --------------------------------------------------------------------------- #
# 5. all providers exhausted
# --------------------------------------------------------------------------- #


@pytest.mark.slow  # cooldown recovery wait; asserts elapsed >= 0.15
def test_exhaustion_pause_wakes_when_provider_recovers(tmp_path, monkeypatch) -> None:
    # D8f recovery monitor: after the provider's cooldown elapses mid-pause, the
    # monitor wakes dispatch, the call probes, and the provider heals.
    server = FakeServer([(500, _error_payload("boom"), 0.0), (200, _ok_payload("rec"), 0.0)])
    _set_keys(monkeypatch, "K_REC")
    router = Diffundo(
        (_config("p", server, "K_REC", cooldown_s=0.2),),
        pause_timeout_s=2.0,
    )
    try:
        with pytest.raises(AllProvidersFailed):
            asyncio.run(router.call(ProviderTier.FAST, PROMPT))
        assert router.health("p") is HealthState.COOLDOWN

        start = time.monotonic()
        result = asyncio.run(router.call(ProviderTier.FAST, PROMPT))
        # the pause actually waited for the cooldown to lapse, then probed
        assert time.monotonic() - start >= 0.15
        assert result.provider == "p"
        assert result.content == "rec"
        assert router.health("p") is HealthState.HEALTHY  # probe success healed it
        assert len(server.calls) == 2
    finally:
        server.close()


@pytest.mark.slow  # 0.2s blocking-pause wait; asserts elapsed >= 0.15
def test_outage_pause_actually_blocks_not_busy_spins(tmp_path, monkeypatch) -> None:
    # D8f: a tier outage must BLOCK on the pause event, not spin the candidate
    # loop. The reviewer measured ~26k pause iterations in 0.6s before the fix;
    # a blocked call must keep the loop iteration count low.
    down = FakeServer([(500, _error_payload("down"), 0.0)])
    ok = FakeServer([(200, _ok_payload("ok"), 0.0)])
    _set_keys(monkeypatch, "K_DOWN", "K_OK")
    router = Diffundo(
        (
            _config("p_down", down, "K_DOWN", rpm=1, cooldown_s=60.0),
            _config("p_ok", ok, "K_OK", rpm=1),
        ),
        pause_timeout_s=0.1,
    )
    try:
        # consume both buckets so the next call finds an empty tier
        result = asyncio.run(router.call(ProviderTier.FAST, PROMPT))
        assert result.provider == "p_ok"

        calls = {"n": 0}
        original = Diffundo._pause_for

        async def counting(self, tier, max_wait):
            calls["n"] += 1
            return await original(self, tier, max_wait)

        monkeypatch.setattr(Diffundo, "_pause_for", counting)
        start = time.monotonic()
        with pytest.raises(AllProvidersFailed):
            asyncio.run(router.call(ProviderTier.FAST, PROMPT))
        elapsed = time.monotonic() - start
        # the 100ms pause actually blocked on the event ...
        assert elapsed >= 0.07
        # ... a bounded pause on exhaustion, not a hang ...
        assert elapsed < 5.0
        # ... instead of spinning the loop thousands of times
        assert calls["n"] < 100
        # nothing was re-dispatched during the pause
        assert len(down.calls) == 1 and len(ok.calls) == 1
    finally:
        down.close()
        ok.close()


# --------------------------------------------------------------------------- #
# 5b. wall-clock budget (cascade-design §2.2)
# --------------------------------------------------------------------------- #


def test_call_budget_outer_deadline_bounds_threaded_post(monkeypatch) -> None:
    """A blocking socket read cannot extend the async call past its budget."""
    provider = ProviderConfig(
        name="p_threaded_slow",
        tier=ProviderTier.FAST,
        base_url="http://127.0.0.1:1",
        api_key_env="K_THREADED_SLOW",
        timeout_s=5.0,
        max_retries=0,
    )
    router = Diffundo((provider,), call_budget_s=0.05, pause_timeout_s=0.01)

    def slow_post_sync(self, provider, prompt, timeout_s):
        time.sleep(0.3)
        return _RawResponse(_ok_payload("late"), 0.3)

    monkeypatch.setattr(Diffundo, "_post_sync", slow_post_sync)

    async def scenario() -> None:
        start = time.monotonic()
        with pytest.raises(AllProvidersFailed) as raised:
            await router.call(ProviderTier.FAST, PROMPT)
        elapsed = time.monotonic() - start
        assert elapsed < 0.15
        failure = raised.value
        assert failure.last_error is not None
        error = cast(ProviderError, failure.last_error)
        assert error.outcome is ProviderOutcome.TIMEOUT
        assert error.budget_exhausted is True
        # Keep the loop alive until the deliberately orphaned executor work
        # finishes, so the regression test also checks its cleanup callback.
        await asyncio.sleep(0.35)

    asyncio.run(scenario())


@pytest.mark.slow  # 0.3s scripted provider delays; timing assertion
def test_call_budget_bounds_slow_attempts(tmp_path, monkeypatch) -> None:
    # call_budget_s is a hard deadline over the WHOLE cascade, not just
    # candidate waiting. Two 0.4s-timeout providers with a retry would naively
    # take ~1.6s; a 0.2s budget caps it to budget + one in-flight attempt.
    slow1 = FakeServer([(200, _ok_payload("slow1"), 0.3)])
    slow2 = FakeServer([(200, _ok_payload("slow2"), 0.3)])
    _set_keys(monkeypatch, "K_S1", "K_S2")
    router = Diffundo(
        (
            _config("p_s1", slow1, "K_S1", timeout_s=0.4, max_retries=1),
            _config("p_s2", slow2, "K_S2", timeout_s=0.4, max_retries=1),
        ),
        call_budget_s=0.2,
    )
    try:
        start = time.monotonic()
        with pytest.raises(AllProvidersFailed):
            asyncio.run(router.call(ProviderTier.FAST, PROMPT))
        elapsed = time.monotonic() - start
        # budget (0.2) + one budget-capped in-flight attempt, far under the
        # naive 2 providers x (1+1 retries) x 0.4s = ~1.6s product
        assert elapsed <= 0.8
        # the first attempt really was capped by the budget (not instant)
        assert elapsed >= 0.15
    finally:
        slow1.close()
        slow2.close()


def test_two_calls_to_static_prompt_both_hit_provider(tmp_path, monkeypatch) -> None:
    # Opposite of a cache: two byte-identical calls are two provider round-trips.
    server = FakeServer([(200, _ok_payload("same"), 0.0)])
    _set_keys(monkeypatch, "K")
    router = Diffundo((_config("p", server, "K"),))
    try:
        r1 = asyncio.run(router.call(ProviderTier.FAST, STATIC_HEAD))
        r2 = asyncio.run(router.call(ProviderTier.FAST, STATIC_HEAD))
        assert r1.content == "same" and r2.content == "same"
        assert len(server.calls) == 2
    finally:
        server.close()


def test_remote_http_provider_without_config_validation_is_rejected_at_call(
    tmp_path, monkeypatch
) -> None:
    # A ProviderConfig constructed directly (bypassing the config loader) must
    # still never send the Authorization header over plaintext http to a remote
    # host: _post_sync re-checks the resolved base_url scheme before sending.
    unvalidated = ProviderConfig(
        name="p_insecure",
        tier=ProviderTier.FAST,
        base_url="http://api.example.test/v1",
        api_key_env="K_INSECURE",
    )
    monkeypatch.setenv("K_INSECURE", "sk-test-K_INSECURE")
    router = Diffundo((unvalidated,), pause_timeout_s=0.01)

    with pytest.raises(AllProvidersFailed) as exc:
        asyncio.run(router.call(ProviderTier.FAST, PROMPT))
    failure = cast(AllProvidersFailed, exc.value)
    assert failure.last_error is not None
    error = cast(ProviderError, failure.last_error)
    assert error.outcome is ProviderOutcome.AUTH_ERROR
    assert "http transport is allowed only for loopback hosts" in error.message
    assert "sk-test-K_INSECURE" not in str(exc.value)
    assert router.health("p_insecure") is HealthState.DISABLED


@pytest.mark.parametrize(
    ("scheme", "base_url"),
    [("ftp", "ftp://provider.example/v1"), ("file", "file:///tmp/provider")],
)
def test_non_http_provider_schemes_are_rejected_before_urllib(
    scheme: str, base_url: str, monkeypatch
) -> None:
    config = ProviderConfig(
        name=f"p_{scheme}",
        tier=ProviderTier.FAST,
        base_url=base_url,
        api_key_env=f"K_{scheme.upper()}",
    )
    monkeypatch.setenv(config.api_key_env, f"sk-{scheme}-secret")
    router = Diffundo((config,), pause_timeout_s=0.01)

    with pytest.raises(AllProvidersFailed) as exc:
        asyncio.run(router.call(ProviderTier.FAST, PROMPT))
    failure = cast(AllProvidersFailed, exc.value)
    assert failure.last_error is not None
    error = cast(ProviderError, failure.last_error)
    assert error.outcome is ProviderOutcome.AUTH_ERROR
    assert "URL scheme must be http or https" in error.message
    assert router.health(config.name) is HealthState.DISABLED


# --------------------------------------------------------------------------- #
# 9. transport hardening: redirects and proxies must never leak the key
# --------------------------------------------------------------------------- #


def test_loopback_redirect_to_non_loopback_http_never_contacts_target(
    tmp_path, monkeypatch
) -> None:
    # A provider completion endpoint must never redirect: urllib replays the
    # original request headers — including Authorization — against the redirect
    # target, which bypasses the loopback/https transport guard entirely. The
    # redirect must be rejected before any follow-up request is made.
    # 127.0.0.2 is reachable on the loopback interface but is NOT in the
    # transport allowlist (only localhost/127.0.0.1/::1), so it is a genuine
    # non-loopback http origin for the redirect target.
    target = FakeServer([(200, _ok_payload("must never arrive"), 0.0)], host="127.0.0.2")
    redirector = FakeServer([(302, {}, 0.0, {"Location": f"{target.base_url}/chat/completions"})])
    _set_keys(monkeypatch, "K_REDIRECT")
    router = Diffundo(
        (_config("p_redirect", redirector, "K_REDIRECT"),),
        pause_timeout_s=0.01,
    )
    try:
        with pytest.raises(AllProvidersFailed) as exc:
            asyncio.run(router.call(ProviderTier.FAST, PROMPT))
        assert len(redirector.calls) == 1
        assert target.calls == []  # the redirect target was never contacted
        assert target.request_headers == []  # ... so it never saw the key either
        failure = cast(AllProvidersFailed, exc.value)
        error = failure.last_error
        assert error is not None
        error = cast(ProviderError, error)
        assert error.outcome is ProviderOutcome.AUTH_ERROR
        assert "redirect" in error.message
        assert "sk-test-K_REDIRECT" not in str(exc.value)
        assert router.health("p_redirect") is HealthState.DISABLED
    finally:
        redirector.close()
        target.close()


def test_loopback_http_request_bypasses_proxy_and_proxy_never_sees_key(
    tmp_path, monkeypatch
) -> None:
    # A loopback http provider carries the Authorization Bearer in the clear;
    # honoring HTTP_PROXY would forward the request (and the key) to the proxy.
    # Loopback requests must go straight to the address, never via a proxy.
    server = FakeServer([(200, _ok_payload("direct"), 0.0)])
    proxy = FakeServer([(200, _ok_payload("via proxy"), 0.0)])
    _set_keys(monkeypatch, "K_PROXY")
    for var in (
        "ALL_PROXY",
        "all_proxy",
        "HTTP_PROXY",
        "http_proxy",
        "HTTPS_PROXY",
        "https_proxy",
        "FTP_PROXY",
        "ftp_proxy",
        "NO_PROXY",
        "no_proxy",
    ):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("HTTP_PROXY", proxy.base_url)
    monkeypatch.setenv("http_proxy", proxy.base_url)
    # urllib caches its default opener (with the proxies dict read at build
    # time); reset it so the pre-fix urlopen path re-reads HTTP_PROXY and the
    # canary actually routes through the proxy before the fix.
    monkeypatch.setattr(urllib.request, "_opener", None)
    router = Diffundo((_config("p_proxy", server, "K_PROXY"),))
    try:
        result = asyncio.run(router.call(ProviderTier.FAST, PROMPT))
        assert result.content == "direct"
        assert len(server.calls) == 1  # reached the loopback provider directly
        assert proxy.calls == []  # the proxy never saw the request ...
        assert proxy.request_headers == []  # ... nor the Authorization header
    finally:
        server.close()
        proxy.close()


# --------------------------------------------------------------------------- #
# 10. usage/quota evidence (implementation plan step 3)
# --------------------------------------------------------------------------- #


def test_429_retry_after_and_quota_owner_surface_on_winning_result(monkeypatch) -> None:
    # A 429 with Retry-After + a reported account-quota owner retries on the
    # same provider; the winning result carries both as usage evidence.
    limited = FakeServer(
        [
            (
                429,
                {
                    "error": {
                        "message": "rate limit exceeded",
                        "type": "rate_limit_error",
                        "rate_limit": {"scope": "account", "quota_owner": "org-acme"},
                    }
                },
                0.0,
                {"Retry-After": "5"},
            ),
            (200, _ok_payload("recovered"), 0.0),
        ]
    )
    _set_keys(monkeypatch, "K_QUOTA")
    router = Diffundo((_config("p_quota", limited, "K_QUOTA", max_retries=1),))
    sleeps: list[float] = []

    async def record_sleep(delay: float) -> None:
        sleeps.append(delay)

    monkeypatch.setattr(asyncio, "sleep", record_sleep)
    try:
        result = asyncio.run(router.call(ProviderTier.FAST, PROMPT))
        assert result.content == "recovered"
        assert result.retry_after_s == 5.0
        assert result.account_quota_owner == "org-acme"
        assert sleeps == [5.0]  # the honored Retry-After controls the same-provider retry
        assert result.request_rate_status == "available"
        assert len(limited.calls) == 2
    finally:
        limited.close()


def test_429_quota_owner_reaches_failure_error(monkeypatch) -> None:
    # The provider-reported quota owner rides the terminal failure too, so a
    # failed call's durable usage event can name the exhausted quota.
    server = FakeServer(
        [
            (
                429,
                {
                    "error": {
                        "message": "quota",
                        "type": "rate_limit_error",
                        "quota_owner": "org-xyz",
                    }
                },
                0.0,
                {"Retry-After": "1"},
            )
        ]
    )
    _set_keys(monkeypatch, "K_QUOTA_FAIL")
    router = Diffundo((_config("p_quota_fail", server, "K_QUOTA_FAIL"),))
    try:
        with pytest.raises(AllProvidersFailed) as exc:
            asyncio.run(router.call(ProviderTier.FAST, PROMPT))
        failure = cast(AllProvidersFailed, exc.value)
        error = failure.last_error
        assert error is not None
        error = cast(ProviderError, error)
        assert error.outcome is ProviderOutcome.QUOTA
        assert error.retry_after_s == 1.0
        assert error.account_quota_owner == "org-xyz"
        # post-failure request-rate status: the 429 sent the provider to COOLDOWN
        assert error.request_rate_status == "cooldown"
    finally:
        server.close()


def test_usage_metric_fields_follow_provider_reports(tmp_path, monkeypatch) -> None:
    # prompt-prefix stability + provider-reported cache-hit metrics: recorded
    # per call, never evidence of a local response cache (D1).
    server = FakeServer(
        [
            (
                200,
                _ok_payload(
                    "plain",
                    usage={"prompt_tokens": 3, "completion_tokens": 1, "total_tokens": 4},
                ),
                0.0,
            ),
            (
                200,
                _ok_payload(
                    "cached",
                    usage={
                        "prompt_tokens": 3,
                        "completion_tokens": 1,
                        "total_tokens": 4,
                        "prompt_tokens_details": {"cached_tokens": 2},
                    },
                ),
                0.0,
            ),
            (200, _ok_payload("no usage"), 0.0),
        ]
    )
    _set_keys(monkeypatch, "K_METRIC")
    router = Diffundo((_config("p_metric", server, "K_METRIC"),))
    try:
        r1 = asyncio.run(router.call(ProviderTier.FAST, STATIC_HEAD))
        r2 = asyncio.run(router.call(ProviderTier.FAST, STATIC_HEAD))
        r3 = asyncio.run(router.call(ProviderTier.FAST, PROMPT))
        assert r1.provider_cache_hit is False  # usage present, no cache fields
        assert r2.provider_cache_hit is True  # provider reports cached tokens
        assert r3.provider_cache_hit is None  # no usage -> unknown, never an error
        expected_prefix = len(STATIC_HEAD["messages"][0]["content"].encode("utf-8"))
        # stable prefix across turns of the same fixed prompt fixture
        assert r1.prompt_prefix_bytes == r2.prompt_prefix_bytes == expected_prefix
        assert r1.prompt_prefix_tokens_estimate == expected_prefix // 4
        assert r2.prompt_prefix_tokens_estimate == expected_prefix // 4
        assert r3.prompt_prefix_bytes is None  # no leading system message
        assert r3.prompt_prefix_tokens_estimate is None
        assert r3.estimated_cost_usd == 0.0  # no usage -> zero cost
        assert r3.request_rate_status == "available"
    finally:
        server.close()


def test_chat_response_larger_than_provider_cap_is_rejected(monkeypatch) -> None:
    from cambium.diffundo import MAX_PROVIDER_RESPONSE_BYTES

    oversized = _ok_payload("x" * (MAX_PROVIDER_RESPONSE_BYTES + 1))
    server = FakeServer([(200, oversized, 0.0)])
    _set_keys(monkeypatch, "K_OVERSIZED")
    router = Diffundo((_config("p_oversized", server, "K_OVERSIZED"),))
    try:
        with pytest.raises(AllProvidersFailed) as raised:
            asyncio.run(router.call(ProviderTier.FAST, PROMPT))
        failure = cast(AllProvidersFailed, raised.value)
        assert failure.last_error is not None
        error = cast(ProviderError, failure.last_error)
        assert "response exceeds" in error.message
    finally:
        server.close()
