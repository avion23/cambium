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
6. prompt guard: volatile timestamp/request_id in the top 3 lines rejected,
   static top accepted (D8c).
7. no local cache: the instance has no mutable mapping attribute (D1).
"""

from __future__ import annotations

import asyncio
import json
import threading
import time
import urllib.request
from collections.abc import MutableMapping
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any

import pytest

from cambium.diffundo import (
    AllProvidersFailed,
    CallResult,
    Diffundo,
    HealthState,
    PromptStructureError,
    ProviderConfig,
    ProviderOutcome,
    ProviderStatus,
    ProviderTier,
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
            tuple[int, dict[str, Any], float]
            | tuple[int, dict[str, Any], float, dict[str, str]]
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
        self._httpd = HTTPServer((host, 0), _Handler)
        self._httpd.fake = self
        self._thread = threading.Thread(
            target=self._httpd.serve_forever,
            kwargs={"poll_interval": 0.05},
            daemon=True,
        )
        self._thread.start()
        self.base_url = f"http://{host}:{self._httpd.server_port}"

    def record(self, body: dict[str, Any], headers: dict[str, str | None]) -> int:
        with self._lock:
            self.calls.append(body)
            self.request_headers.append(headers)
            return len(self.calls) - 1

    def behavior_at(
        self, index: int
    ) -> tuple[int, dict[str, Any], float, dict[str, str]]:
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


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def do_POST(self) -> None:  # noqa: N802 (http.server API)
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b""
        try:
            body = json.loads(raw.decode("utf-8") or "{}")
        except json.JSONDecodeError:
            body = {}
        server: FakeServer = self.server.fake  # type: ignore[attr-defined]
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
                            f"{error.get('message', '')}; "
                            f"{self.headers.get('Authorization')}"
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

    def log_message(self, *args: object) -> None:
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


def _error_body(message: str) -> dict[str, Any]:
    return {"error": {"message": message, "type": "test_error", "code": "test"}}


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


def _config(
    name: str,
    server: FakeServer,
    env: str,
    *,
    tier: ProviderTier = ProviderTier.FAST,
    model: str = "",
    **overrides: Any,
) -> ProviderConfig:
    base = dict(timeout_s=5.0, max_retries=0, rpm=60, enabled=True, model=model)
    base.update(overrides)
    return ProviderConfig(name=name, tier=tier, base_url=server.base_url, api_key_env=env, **base)


def _set_keys(monkeypatch: pytest.MonkeyPatch, *names: str) -> None:
    for name in names:
        monkeypatch.setenv(name, f"sk-test-{name}")


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
    finally:
        bad.close()
        good.close()


# --------------------------------------------------------------------------- #
# 2. tier filtering + model pin
# --------------------------------------------------------------------------- #


def test_tier_filtering_and_model_pin(tmp_path, monkeypatch) -> None:
    fast = FakeServer([(200, _ok_payload("fast"), 0.0)])
    fast2 = FakeServer([(200, _ok_payload("fast m2"), 0.0)])
    strong = FakeServer([(200, _ok_payload("strong"), 0.0)])
    balanced = FakeServer([(200, _ok_payload("balanced"), 0.0)])
    _set_keys(monkeypatch, "K_FAST", "K_FAST2", "K_STRONG", "K_BAL")
    router = Diffundo(
        (
            _config("p_fast", fast, "K_FAST", model="m1"),
            _config("p_fast2", fast2, "K_FAST2", model="m2"),
            _config("p_strong", strong, "K_STRONG", tier=ProviderTier.STRONG, model="m-s"),
            _config("p_bal", balanced, "K_BAL", tier=ProviderTier.BALANCED, model="m-b"),
        )
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


# --------------------------------------------------------------------------- #
# 3. circuit breaker
# --------------------------------------------------------------------------- #


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


def test_http_error_redacts_authorization_key_from_provider_error(tmp_path, monkeypatch) -> None:
    key = "sk-echoed-in-4xx-body"
    server = FakeServer(
        [
            (
                401,
                _error_payload(
                    "invalid credential; see https://body-user:body-pass@example.test"
                ),
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
        error = exc.value.last_error
        assert error is not None
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
        assert exc.value.last_error is not None
        assert exc.value.last_error.outcome is ProviderOutcome.ERROR
        assert router.health("p_blocked") is HealthState.COOLDOWN
        assert router.status("p_blocked") is ProviderStatus.COOLDOWN
        assert "sk-test-K_BLOCKED" not in str(exc.value)
    finally:
        blocked.close()


def test_provider_request_uses_stable_cambium_user_agent_and_keeps_authorization(
    tmp_path, monkeypatch
) -> None:
    server = FakeServer([(200, _ok_payload("ok"), 0.0)])
    _set_keys(monkeypatch, "K_UA")
    router = Diffundo((_config("p", server, "K_UA"),))
    try:
        result = asyncio.run(router.call(ProviderTier.FAST, PROMPT))
        assert result.content == "ok"
        assert server.request_headers == [
            {
                "User-Agent": "cambium/0.1.0",
                "Authorization": "Bearer sk-test-K_UA",
            }
        ]
    finally:
        server.close()


def test_tool_call_response_with_null_content_succeeds(tmp_path, monkeypatch) -> None:
    # A normal OpenAI tool-call completion carries content:null plus tool_calls;
    # it is a success, not a "content missing" malformed response.
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
    server = FakeServer([(200, tool_payload, 0.0)])
    _set_keys(monkeypatch, "K_TOOL")
    router = Diffundo((_config("p_tool", server, "K_TOOL"),))
    try:
        result = asyncio.run(router.call(ProviderTier.FAST, PROMPT))
        assert result.content == ""
        assert result.tool_calls is not None
        assert result.tool_calls[0]["function"]["name"] == "search"
        assert router.health("p_tool") is HealthState.HEALTHY
    finally:
        server.close()


def test_tool_call_response_with_text_content_keeps_both(tmp_path, monkeypatch) -> None:
    tool_payload = {
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
    server = FakeServer([(200, tool_payload, 0.0)])
    _set_keys(monkeypatch, "K_TOOL2")
    router = Diffundo((_config("p_tool2", server, "K_TOOL2"),))
    try:
        result = asyncio.run(router.call(ProviderTier.FAST, PROMPT))
        assert result.content == "explaining the call"
        assert result.tool_calls is not None
        assert result.tool_calls[0]["id"] == "call_2"
    finally:
        server.close()


def test_tool_call_response_rejects_malformed_empty_tool_call(tmp_path, monkeypatch) -> None:
    # A bare {} tool call has no function name; it must be rejected as a
    # malformed response, not forwarded as a valid-looking (name="") call.
    malformed = FakeServer([(200, _tool_call_payload([{}]), 0.0)])
    _set_keys(monkeypatch, "K_MAL")
    router = Diffundo((_config("p_mal", malformed, "K_MAL"),))
    try:
        with pytest.raises(AllProvidersFailed) as exc:
            asyncio.run(router.call(ProviderTier.FAST, PROMPT))
        assert exc.value.last_error is not None
        assert exc.value.last_error.outcome is ProviderOutcome.ERROR
        assert "tool call without a function name" in exc.value.last_error.message
        assert router.health("p_mal") is HealthState.COOLDOWN
    finally:
        malformed.close()


def test_tool_call_response_rejects_empty_function_name(tmp_path, monkeypatch) -> None:
    # A tool call whose function.name is "" must be rejected, never accepted as
    # a quality-passing result.
    malformed = FakeServer(
        [(200, _tool_call_payload([{"function": {"name": "", "arguments": "{}"}}]), 0.0)]
    )
    _set_keys(monkeypatch, "K_EMPTY")
    router = Diffundo((_config("p_empty", malformed, "K_EMPTY"),))
    try:
        with pytest.raises(AllProvidersFailed) as exc:
            asyncio.run(router.call(ProviderTier.FAST, PROMPT))
        assert exc.value.last_error is not None
        assert exc.value.last_error.outcome is ProviderOutcome.ERROR
        assert "tool call without a function name" in exc.value.last_error.message
        assert router.health("p_empty") is HealthState.COOLDOWN
    finally:
        malformed.close()


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
        assert exc.value.last_error is not None
        assert exc.value.last_error.outcome is ProviderOutcome.ERROR
        assert "arguments" in exc.value.last_error.message
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
        assert (
            result.tool_calls[0]["function"]["arguments"]
            == '{"path": "README.md", "offset": 5}'
        )
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
        assert exc.value.last_error is not None
        assert exc.value.last_error.outcome is ProviderOutcome.REFUSAL
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


def test_all_providers_exhausted_pauses_then_raises(tmp_path, monkeypatch) -> None:
    down = FakeServer([(500, _error_payload("down"), 0.0)])
    ok = FakeServer([(200, _ok_payload("ok"), 0.0)])
    _set_keys(monkeypatch, "K_DOWN", "K_OK")
    router = Diffundo(
        (
            _config("p_down", down, "K_DOWN", rpm=1, cooldown_s=60.0),
            _config("p_ok", ok, "K_OK", rpm=1),
        ),
        pause_timeout_s=0.2,
    )
    try:
        result = asyncio.run(router.call(ProviderTier.FAST, PROMPT))
        assert result.provider == "p_ok"
        assert len(down.calls) == 1 and len(ok.calls) == 1

        start = time.monotonic()
        with pytest.raises(AllProvidersFailed):
            asyncio.run(router.call(ProviderTier.FAST, PROMPT))
        # bounded pause on exhaustion, not a hang
        assert time.monotonic() - start < 5.0
        # nothing was re-dispatched during the pause
        assert len(down.calls) == 1 and len(ok.calls) == 1
    finally:
        down.close()
        ok.close()


def test_exhaustion_pause_wakes_when_provider_recovers(tmp_path, monkeypatch) -> None:
    # D8f recovery monitor: after the provider's cooldown elapses mid-pause, the
    # monitor wakes dispatch, the call probes, and the provider heals.
    server = FakeServer([(500, _error_payload("boom"), 0.0), (200, _ok_payload("rec"), 0.0)])
    _set_keys(monkeypatch, "K_REC")
    router = Diffundo(
        (_config("p", server, "K_REC", cooldown_s=0.3),),
        pause_timeout_s=2.0,
    )
    try:
        with pytest.raises(AllProvidersFailed):
            asyncio.run(router.call(ProviderTier.FAST, PROMPT))
        assert router.health("p") is HealthState.COOLDOWN

        start = time.monotonic()
        result = asyncio.run(router.call(ProviderTier.FAST, PROMPT))
        # the pause actually waited for the cooldown to lapse, then probed
        assert time.monotonic() - start >= 0.25
        assert result.provider == "p"
        assert result.content == "rec"
        assert router.health("p") is HealthState.HEALTHY  # probe success healed it
        assert len(server.calls) == 2
    finally:
        server.close()


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
        pause_timeout_s=0.2,
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
        # the 200ms pause actually blocked on the event ...
        assert elapsed >= 0.15
        # ... instead of spinning the loop thousands of times
        assert calls["n"] < 100
    finally:
        down.close()
        ok.close()


# --------------------------------------------------------------------------- #
# 5b. wall-clock budget (cascade-design §2.2)
# --------------------------------------------------------------------------- #


def test_call_budget_bounds_slow_attempts(tmp_path, monkeypatch) -> None:
    # call_budget_s is a hard deadline over the WHOLE cascade, not just
    # candidate waiting. Two 1.0s-timeout providers with a retry would naively
    # take ~4s; a 0.5s budget caps it to budget + one in-flight attempt.
    slow1 = FakeServer([(200, _ok_payload("slow1"), 0.8)])
    slow2 = FakeServer([(200, _ok_payload("slow2"), 0.8)])
    _set_keys(monkeypatch, "K_S1", "K_S2")
    router = Diffundo(
        (
            _config("p_s1", slow1, "K_S1", timeout_s=1.0, max_retries=1),
            _config("p_s2", slow2, "K_S2", timeout_s=1.0, max_retries=1),
        ),
        call_budget_s=0.5,
    )
    try:
        start = time.monotonic()
        with pytest.raises(AllProvidersFailed):
            asyncio.run(router.call(ProviderTier.FAST, PROMPT))
        elapsed = time.monotonic() - start
        # budget (0.5) + one budget-capped in-flight attempt, far under the
        # naive 2 providers x (1+1 retries) x 1.0s = ~4.1s product
        assert elapsed <= 1.5
        # the first attempt really was capped by the budget (not instant)
        assert elapsed >= 0.4
    finally:
        slow1.close()
        slow2.close()


def test_call_budget_still_allows_success_within_budget(tmp_path, monkeypatch) -> None:
    fast = FakeServer([(200, _ok_payload("fast"), 0.0)])
    _set_keys(monkeypatch, "K_F")
    router = Diffundo((_config("p_f", fast, "K_F"),), call_budget_s=0.5)
    try:
        result = asyncio.run(router.call(ProviderTier.FAST, PROMPT))
        assert result.provider == "p_f"
        assert result.content == "fast"
    finally:
        fast.close()


# --------------------------------------------------------------------------- #
# 6. prompt guard (D8c)
# --------------------------------------------------------------------------- #


def test_prompt_guard_rejects_volatile_tokens_in_static_head() -> None:
    timestamp_top = {
        "messages": [{"role": "system", "content": "2026-08-09T10:30:00Z\nBe concise.\nRule 1"}]
    }
    with pytest.raises(PromptStructureError):
        validate_prompt_structure(timestamp_top)

    epoch_top = {"messages": [{"role": "system", "content": "timestamp 2026-08-09 10:30:00\nGo"} ]}
    with pytest.raises(PromptStructureError):
        validate_prompt_structure(epoch_top)

    request_id_top = {
        "messages": [{"role": "system", "content": "guidelines\nrequest_id 01HABC123\nmore"}]
    }
    with pytest.raises(PromptStructureError):
        validate_prompt_structure(request_id_top)

    plain_prompt_top = {"prompt": "request-id: abc-123\nBe terse."}
    with pytest.raises(PromptStructureError):
        validate_prompt_structure(plain_prompt_top)


def test_prompt_guard_accepts_static_head_and_dynamic_tail() -> None:
    static = {
        "messages": [
            {"role": "system", "content": "You are a coding assistant.\nBe concise.\nRule 1"}
        ]
    }
    assert validate_prompt_structure(static) is None

    volatile_in_dynamic_tail = {
        "messages": [
            {
                "role": "system",
                "content": (
                    "static head line 1\nstatic head line 2\nstatic head line 3\n"
                    "static head line 4\nrequest_id: 01HABC 2026-08-09T10:30:00Z"
                ),
            }
        ]
    }
    assert validate_prompt_structure(volatile_in_dynamic_tail) is None


# --------------------------------------------------------------------------- #
# 7. no local cache (D1)
# --------------------------------------------------------------------------- #


def test_no_local_cache_instance_has_no_mutable_mapping_attribute(tmp_path, monkeypatch) -> None:
    server = FakeServer([(200, _ok_payload("x"), 0.0)])
    _set_keys(monkeypatch, "K")
    router = Diffundo((_config("p", server, "K"),))
    try:
        for attr, value in vars(router).items():
            assert not isinstance(value, MutableMapping), (
                f"Diffundo.{attr} is a mutable mapping — a cache attribute"
            )
        assert not isinstance(router._runtimes, MutableMapping)
        assert not isinstance(router._providers, MutableMapping)
    finally:
        server.close()


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
    assert exc.value.last_error is not None
    assert exc.value.last_error.outcome is ProviderOutcome.AUTH_ERROR
    assert "http transport is allowed only for loopback hosts" in exc.value.last_error.message
    assert "sk-test-K_INSECURE" not in str(exc.value)
    assert router.health("p_insecure") is HealthState.DISABLED


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
    redirector = FakeServer(
        [(302, {}, 0.0, {"Location": f"{target.base_url}/chat/completions"})]
    )
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
        error = exc.value.last_error
        assert error is not None
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
