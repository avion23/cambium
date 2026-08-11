"""Scenario tests for the codex_responses protocol adapter in diffundo.py.

No mocks, no network: each scenario drives real ``Diffundo.call`` against a
fake SSE codex server (``http.server`` in a background thread, the same
FakeServer pattern as test_diffundo.py). The endpoint profile is injected
through the ``codex_profile`` constructor seam and the bearer credential
through ``credential_source`` — providers.json can never set either. Env vars
for the legacy chat path are set explicitly (no monkeypatch fixture).

Covers the documented + probed codex contract: the Responses-API request shape
(no chat extras), SSE delta assembly with usage from the completed payload,
in-stream error classification (retryable outage vs CONFIG quarantine vs
REFUSAL), the HTTP 400 model/parameter split, fail-closed credential handling,
transport guards, and the unchanged chat_completions path.
"""

from __future__ import annotations

import asyncio
import json
import os
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any

import pytest

from cambium.diffundo import (
    AllProvidersFailed,
    AuthMode,
    CallResult,
    CredentialSource,
    Diffundo,
    HealthState,
    Protocol,
    ProviderConfig,
    ProviderOutcome,
    ProviderStatus,
    ProviderTier,
)

# --------------------------------------------------------------------------- #
# Fake codex server (http.server in a thread — no network)
# --------------------------------------------------------------------------- #


class CodexServer:
    """codex ``/responses`` server on an ephemeral loopback port.

    ``behaviors`` is a list of ``(status, body, delay_s[, headers])`` consumed
    in order; the last behavior repeats for any further request. A 200 streams
    ``body`` (the raw SSE text) back; a non-200 returns ``body`` as a JSON
    error payload. Requests (body, headers, path) are recorded.
    """

    def __init__(
        self,
        behaviors: list[
            tuple[int, object, float]
            | tuple[int, object, float, dict[str, str]]
        ],
        *,
        host: str = "127.0.0.1",
    ) -> None:
        self.behaviors = list(behaviors)
        self.calls: list[dict[str, Any]] = []
        self.request_headers: list[dict[str, str | None]] = []
        self.request_paths: list[str] = []
        self._lock = threading.Lock()
        self._httpd = HTTPServer((host, 0), _Handler)
        self._httpd.fake = self
        self._thread = threading.Thread(
            target=self._httpd.serve_forever,
            kwargs={"poll_interval": 0.001},
            daemon=True,
        )
        self._thread.start()
        self.base_url = f"http://{host}:{self._httpd.server_port}"

    def record(self, body: dict[str, Any], headers: dict[str, str | None], path: str) -> int:
        with self._lock:
            self.calls.append(body)
            self.request_headers.append(headers)
            self.request_paths.append(path)
            return len(self.calls) - 1

    def behavior_at(self, index: int) -> tuple[int, object, float, dict[str, str]]:
        behavior = self.behaviors[index] if index < len(self.behaviors) else self.behaviors[-1]
        if len(behavior) == 3:
            status, body, delay = behavior
            return status, body, delay, {}
        status, body, delay, headers = behavior
        return status, body, delay, headers

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
        server: CodexServer = self.server.fake  # type: ignore[attr-defined]
        index = server.record(
            body,
            {
                "Content-Type": self.headers.get("Content-Type"),
                "Authorization": self.headers.get("Authorization"),
                "ChatGPT-Account-Id": self.headers.get("ChatGPT-Account-Id"),
                "User-Agent": self.headers.get("User-Agent"),
            },
            self.path,
        )
        status, payload, delay, extra_headers = server.behavior_at(index)
        if delay:
            time.sleep(delay)
        if status == 200:
            if isinstance(payload, str):
                encoded = payload.encode("utf-8")
            else:
                encoded = json.dumps(payload).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
        else:
            encoded = json.dumps(payload).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(encoded)))
        for name, value in extra_headers.items():
            self.send_header(name, value)
        self.end_headers()
        try:
            self.wfile.write(encoded)
        except OSError:
            pass  # the client timed out and closed first

    def log_message(self, *args: object) -> None:
        pass


# --------------------------------------------------------------------------- #
# SSE + payload builders
# --------------------------------------------------------------------------- #

CODEX_PATH = "/backend-api/codex/responses"


def _stream(*events: dict[str, Any]) -> str:
    return "".join(f"data: {json.dumps(event)}\n\n" for event in events)


def _delta(text: str) -> dict[str, Any]:
    return {
        "type": "response.output_text.delta",
        "item_id": "it_1",
        "output_index": 0,
        "content_index": 0,
        "delta": text,
    }


def _completed(model: str = "gpt-5.6-luna") -> dict[str, Any]:
    return {
        "type": "response.completed",
        "response": {
            "id": "resp_1",
            "object": "response",
            "model": model,
            "output": [
                {
                    "id": "it_1",
                    "type": "message",
                    "role": "assistant",
                    "content": [{"type": "output_text", "text": "Hello, world"}],
                }
            ],
            "usage": {
                "input_tokens": 12,
                "input_tokens_details": {"cached_tokens": 7},
                "output_tokens": 5,
                "output_tokens_details": {"reasoning_tokens": 2},
                "total_tokens": 17,
            },
        },
    }


def _ok_stream(model: str = "gpt-5.6-luna", text: str = "Hello, world") -> str:
    return _stream(
        {"type": "response.created", "response": {"id": "resp_1"}},
        {"type": "response.in_progress", "response": {"id": "resp_1"}},
        {"type": "response.output_item.added", "item_id": "it_1", "output_index": 0},
        {
            "type": "response.content_part.added",
            "item_id": "it_1",
            "output_index": 0,
            "content_index": 0,
        },
        _delta(text[: len(text) // 2]),
        _delta(text[len(text) // 2:]),
        {
            "type": "response.output_text.done",
            "item_id": "it_1",
            "output_index": 0,
            "content_index": 0,
            "text": text,
        },
        {
            "type": "response.content_part.done",
            "item_id": "it_1",
            "output_index": 0,
            "content_index": 0,
        },
        {
            "type": "response.output_item.done",
            "output": [
                {
                    "id": "it_1",
                    "type": "message",
                    "role": "assistant",
                    "content": [{"type": "output_text", "text": text}],
                }
            ],
        },
        _completed(model),
    )


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #

CREDENTIAL = CredentialSource(access_token="tok-codex-test", account_id="acct-1")


def _codex_config(
    server: CodexServer | None,
    *,
    name: str = "p_codex",
    model: str = "gpt-5.6-luna",
    reasoning_effort: str | None = "max",
    max_retries: int = 0,
    **overrides: Any,
) -> ProviderConfig:
    return ProviderConfig(
        name=name,
        tier=ProviderTier.FAST,
        base_url="",
        api_key_env="",
        timeout_s=5.0,
        max_retries=max_retries,
        rpm=60,
        enabled=True,
        model=model,
        auth=AuthMode.CODEX_CHATGPT,
        protocol=Protocol.CODEX_RESPONSES,
        reasoning_effort=reasoning_effort,
        **overrides,
    )


def _router(
    server: CodexServer,
    *,
    credential: CredentialSource | None = CREDENTIAL,
    max_retries: int = 0,
    **diffundo_overrides: Any,
) -> Diffundo:
    return Diffundo(
        (_codex_config(server, max_retries=max_retries),),
        credential_source=credential,
        codex_profile={"api_origin": server.base_url, "api_path": CODEX_PATH},
        pause_timeout_s=0.01,
        **diffundo_overrides,
    )


class _Env:
    """Explicit env-var injection with restore (no monkeypatch fixture)."""

    def __init__(self, **values: str) -> None:
        self._values = values
        self._saved: dict[str, str | None] = {}

    def __enter__(self) -> _Env:
        for name, value in self._values.items():
            self._saved[name] = os.environ.get(name)
            os.environ[name] = value
        return self

    def __exit__(self, *exc: object) -> None:
        for name in self._values:
            saved = self._saved[name]
            if saved is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = saved


PROMPT = {"messages": [{"role": "user", "content": "hello"}]}

TOOL_PROMPT = {
    "messages": [
        {"role": "system", "content": "You are a coding assistant."},
        {"role": "user", "content": "read README"},
    ],
    "tools": [
        {
            "type": "function",
            "function": {
                "name": "read_file",
                "description": "Read a file from the repository",
                "parameters": {
                    "type": "object",
                    "properties": {"path": {"type": "string"}},
                    "required": ["path"],
                },
            },
        }
    ],
    "tool_choice": {"type": "function", "function": {"name": "read_file"}},
    # chat-only extras must never leak into the codex body
    "max_tokens": 100,
    "max_completion_tokens": 100,
}


# --------------------------------------------------------------------------- #
# 1. request body conversion (exact)
# --------------------------------------------------------------------------- #


def test_codex_request_body_conversion_is_exact() -> None:
    server = CodexServer([(200, _ok_stream(), 0.0)])
    router = _router(server)
    try:
        result = asyncio.run(router.call(ProviderTier.FAST, TOOL_PROMPT))
        assert isinstance(result, CallResult)
        assert server.request_paths == [CODEX_PATH]
        assert server.request_headers[0]["Authorization"] == "Bearer tok-codex-test"
        assert server.request_headers[0]["ChatGPT-Account-Id"] == "acct-1"
        assert server.request_headers[0]["Content-Type"] == "application/json"
        assert server.request_headers[0]["User-Agent"] == "cambium/0.1.0"
        assert server.calls[0] == {
            "model": "gpt-5.6-luna",
            "input": [
                {
                    "role": "developer",
                    "content": [{"type": "input_text", "text": "You are a coding assistant."}],
                },
                {"role": "user", "content": [{"type": "input_text", "text": "read README"}]},
            ],
            "store": False,
            "stream": True,
            "tools": [
                {
                    "type": "function",
                    "name": "read_file",
                    "description": "Read a file from the repository",
                    "parameters": {
                        "type": "object",
                        "properties": {"path": {"type": "string"}},
                        "required": ["path"],
                    },
                }
            ],
            "tool_choice": {"type": "function", "function": {"name": "read_file"}},
            "reasoning": {"effort": "max"},
        }
        assert "max_tokens" not in server.calls[0]
        assert "max_completion_tokens" not in server.calls[0]
    finally:
        server.close()


def test_codex_reasoning_effort_absent_omits_reasoning_field() -> None:
    server = CodexServer([(200, _ok_stream(), 0.0)])
    router = Diffundo(
        (_codex_config(server, reasoning_effort=None),),
        credential_source=CREDENTIAL,
        codex_profile={"api_origin": server.base_url, "api_path": CODEX_PATH},
        pause_timeout_s=0.01,
    )
    try:
        asyncio.run(router.call(ProviderTier.FAST, PROMPT))
        assert server.calls[0] == {
            "model": "gpt-5.6-luna",
            "input": [{"role": "user", "content": [{"type": "input_text", "text": "hello"}]}],
            "store": False,
            "stream": True,
        }
    finally:
        server.close()


# --------------------------------------------------------------------------- #
# 2. SSE parse
# --------------------------------------------------------------------------- #


def test_codex_sse_delta_assembly_and_usage_from_completed_payload() -> None:
    server = CodexServer([(200, _ok_stream(), 0.0)])
    router = _router(server)
    try:
        result = asyncio.run(router.call(ProviderTier.FAST, PROMPT))
        assert result.content == "Hello, world"
        assert result.model == "gpt-5.6-luna"
        assert result.provider == "p_codex"
        assert result.tier is ProviderTier.FAST
        assert router.health("p_codex") is HealthState.HEALTHY
        # Responses-API usage normalized to the chat shape the router consumes,
        # with the codex details preserved.
        assert result.usage is not None
        assert result.usage["prompt_tokens"] == 12
        assert result.usage["completion_tokens"] == 5
        assert result.usage["total_tokens"] == 17
        assert result.usage["prompt_tokens_details"] == {"cached_tokens": 7}
        assert result.usage["input_tokens_details"] == {"cached_tokens": 7}
        assert result.usage["output_tokens_details"] == {"reasoning_tokens": 2}
        assert result.provider_cache_hit is True
        assert result.estimated_cost_usd == 0.0
    finally:
        server.close()


def test_codex_stream_without_completed_event_is_malformed() -> None:
    server = CodexServer([(200, _stream(_delta("partial")), 0.0)])
    router = _router(server)
    try:
        with pytest.raises(AllProvidersFailed) as exc:
            asyncio.run(router.call(ProviderTier.FAST, PROMPT))
        assert exc.value.last_error is not None
        assert exc.value.last_error.outcome is ProviderOutcome.ERROR
        assert "response.completed" in exc.value.last_error.message
        assert router.health("p_codex") is HealthState.COOLDOWN
    finally:
        server.close()


# --------------------------------------------------------------------------- #
# 3. in-stream error events
# --------------------------------------------------------------------------- #


def test_codex_stream_service_unavailable_is_retryable_error() -> None:
    stream = _stream(
        {
            "type": "error",
            "error": {
                "type": "service_unavailable_error",
                "code": "server_is_overloaded",
                "message": "The server is currently overloaded",
            },
        },
        {
            "type": "response.failed",
            "response": {
                "id": "resp_1",
                "error": {
                    "type": "server_error",
                    "code": "server_is_overloaded",
                    "message": "The server is currently overloaded",
                },
            },
        },
    )
    server = CodexServer([(200, stream, 0.0)])
    router = _router(server, max_retries=1)
    try:
        with pytest.raises(AllProvidersFailed) as exc:
            asyncio.run(router.call(ProviderTier.FAST, PROMPT))
        assert exc.value.last_error is not None
        assert exc.value.last_error.outcome is ProviderOutcome.ERROR
        assert "overloaded" in exc.value.last_error.message
        # retryable: the attempt retried once, then the existing cooldown
        # machinery cooled the provider down
        assert len(server.calls) == 2
        assert router.health("p_codex") is HealthState.COOLDOWN
    finally:
        server.close()


def test_codex_stream_model_not_found_quarantines_provider() -> None:
    stream = _stream(
        {
            "type": "response.failed",
            "response": {
                "id": "resp_1",
                "error": {
                    "type": "model_not_found",
                    "code": "model_not_found",
                    "message": "The model gpt-5.6-luna was not found",
                },
            },
        }
    )
    server = CodexServer([(200, stream, 0.0)])
    router = _router(server, max_retries=2)
    try:
        with pytest.raises(AllProvidersFailed) as exc:
            asyncio.run(router.call(ProviderTier.FAST, PROMPT))
        assert exc.value.last_error is not None
        assert exc.value.last_error.outcome is ProviderOutcome.CONFIG_ERROR
        assert "model_not_found" in exc.value.last_error.message
        # non-retryable config error: no retries, provider disabled
        assert len(server.calls) == 1
        assert router.health("p_codex") is HealthState.DISABLED
        assert router.status("p_codex") is ProviderStatus.DISABLED
    finally:
        server.close()


def test_codex_completed_stream_with_refusal_text_falls_through() -> None:
    server = CodexServer([(200, _ok_stream(text="I can't assist with that."), 0.0)])
    router = _router(server)
    try:
        with pytest.raises(AllProvidersFailed) as exc:
            asyncio.run(router.call(ProviderTier.FAST, PROMPT))
        assert exc.value.last_error is not None
        assert exc.value.last_error.outcome is ProviderOutcome.REFUSAL
        assert "refusal" in exc.value.last_error.message
        # a refusal never drives a health transition
        assert router.health("p_codex") is HealthState.UNKNOWN
        assert len(server.calls) == 1
    finally:
        server.close()


# --------------------------------------------------------------------------- #
# 4. HTTP 400 split (codex: CONFIG, not content REFUSAL)
# --------------------------------------------------------------------------- #


def test_codex_http_400_model_not_found_body_quarantines_not_refusal() -> None:
    server = CodexServer(
        [
            (
                400,
                {
                    "error": {
                        "type": "invalid_request_error",
                        "code": "model_not_found",
                        "message": "The model gpt-5.6-luna was not found",
                    }
                },
                0.0,
            )
        ]
    )
    router = _router(server, max_retries=2)
    try:
        with pytest.raises(AllProvidersFailed) as exc:
            asyncio.run(router.call(ProviderTier.FAST, PROMPT))
        assert exc.value.last_error is not None
        assert exc.value.last_error.outcome is ProviderOutcome.CONFIG_ERROR
        assert "HTTP 400" in exc.value.last_error.message
        # non-retryable: quarantined on the first call
        assert len(server.calls) == 1
        assert router.health("p_codex") is HealthState.DISABLED
    finally:
        server.close()


def test_codex_http_400_parameter_body_quarantines_not_refusal() -> None:
    server = CodexServer(
        [
            (
                400,
                {
                    "error": {
                        "type": "invalid_request_error",
                        "code": "unsupported_parameter",
                        "message": "Unsupported parameter: max_output_tokens",
                    }
                },
                0.0,
            )
        ]
    )
    router = _router(server)
    try:
        with pytest.raises(AllProvidersFailed) as exc:
            asyncio.run(router.call(ProviderTier.FAST, PROMPT))
        assert exc.value.last_error is not None
        assert exc.value.last_error.outcome is ProviderOutcome.CONFIG_ERROR
        assert router.health("p_codex") is HealthState.DISABLED
    finally:
        server.close()


def test_codex_http_400_shape_rejection_stays_content_refusal() -> None:
    # The documented live shape rejections ("Stream must be set to true",
    # "Store must be set to false", "Input must be a list") name no model or
    # parameter; they keep the generic 400 -> REFUSAL fall-through.
    server = CodexServer(
        [(400, {"error": {"message": "Input must be a list"}}, 0.0)]
    )
    router = _router(server)
    try:
        with pytest.raises(AllProvidersFailed) as exc:
            asyncio.run(router.call(ProviderTier.FAST, PROMPT))
        assert exc.value.last_error is not None
        assert exc.value.last_error.outcome is ProviderOutcome.REFUSAL
        assert "HTTP 400" in exc.value.last_error.message
        assert router.health("p_codex") is HealthState.UNKNOWN
    finally:
        server.close()


# --------------------------------------------------------------------------- #
# 5. fail-closed credential handling + transport guards
# --------------------------------------------------------------------------- #


def test_codex_without_credential_source_fails_closed() -> None:
    server = CodexServer([(200, _ok_stream(), 0.0)])
    router = _router(server, credential=None)
    try:
        with pytest.raises(AllProvidersFailed) as exc:
            asyncio.run(router.call(ProviderTier.FAST, PROMPT))
        assert exc.value.last_error is not None
        assert exc.value.last_error.outcome is ProviderOutcome.AUTH_ERROR
        assert "credential source" in exc.value.last_error.message
        assert router.health("p_codex") is HealthState.DISABLED
        assert server.calls == []  # nothing ever sent without a token
    finally:
        server.close()


def test_codex_empty_access_token_fails_closed() -> None:
    server = CodexServer([(200, _ok_stream(), 0.0)])
    router = _router(server, credential=CredentialSource(access_token=""))
    try:
        with pytest.raises(AllProvidersFailed) as exc:
            asyncio.run(router.call(ProviderTier.FAST, PROMPT))
        assert exc.value.last_error is not None
        assert exc.value.last_error.outcome is ProviderOutcome.AUTH_ERROR
        assert "empty access token" in exc.value.last_error.message
        assert server.calls == []
    finally:
        server.close()


def test_codex_account_id_omitted_when_credential_has_none() -> None:
    server = CodexServer([(200, _ok_stream(), 0.0)])
    router = _router(server, credential=CredentialSource(access_token="tok-no-acct"))
    try:
        asyncio.run(router.call(ProviderTier.FAST, PROMPT))
        assert server.request_headers[0]["Authorization"] == "Bearer tok-no-acct"
        assert server.request_headers[0]["ChatGPT-Account-Id"] is None
    finally:
        server.close()


def test_codex_non_loopback_http_origin_is_rejected_before_request() -> None:
    router = Diffundo(
        (_codex_config(None),),
        credential_source=CREDENTIAL,
        codex_profile={"api_origin": "http://api.example.test", "api_path": CODEX_PATH},
        pause_timeout_s=0.01,
    )
    with pytest.raises(AllProvidersFailed) as exc:
        asyncio.run(router.call(ProviderTier.FAST, PROMPT))
    assert exc.value.last_error is not None
    assert exc.value.last_error.outcome is ProviderOutcome.AUTH_ERROR
    assert "http transport is allowed only for loopback hosts" in exc.value.last_error.message
    assert router.health("p_codex") is HealthState.DISABLED


def test_codex_redirect_fails_closed_and_never_contacts_target() -> None:
    target = CodexServer([(200, _ok_stream(), 0.0)])
    redirector = CodexServer(
        [(302, {}, 0.0, {"Location": f"{target.base_url}{CODEX_PATH}"})]
    )
    router = _router(redirector)
    try:
        with pytest.raises(AllProvidersFailed) as exc:
            asyncio.run(router.call(ProviderTier.FAST, PROMPT))
        assert exc.value.last_error is not None
        assert exc.value.last_error.outcome is ProviderOutcome.AUTH_ERROR
        assert "redirect" in exc.value.last_error.message
        assert router.health("p_codex") is HealthState.DISABLED
        assert len(redirector.calls) == 1
        assert target.calls == []  # the bearer never followed the redirect
    finally:
        redirector.close()
        target.close()


# --------------------------------------------------------------------------- #
# 6. chat_completions path byte-identical
# --------------------------------------------------------------------------- #


def test_chat_completions_path_stays_byte_identical() -> None:
    server = CodexServer(
        [
            (
                200,
                json.dumps(
                    {
                        "id": "chatcmpl-test",
                        "object": "chat.completion",
                        "model": "m-chat",
                        "choices": [
                            {
                                "index": 0,
                                "message": {"role": "assistant", "content": "ok"},
                                "finish_reason": "stop",
                            }
                        ],
                    }
                ),
                0.0,
            )
        ]
    )
    chat_prompt = {
        "messages": [{"role": "user", "content": "hello"}],
        "max_tokens": 50,
        "tools": TOOL_PROMPT["tools"],
        "tool_choice": TOOL_PROMPT["tool_choice"],
    }
    with _Env(K_CHAT="sk-chat-test"):
        router = Diffundo(
            (
                ProviderConfig(
                    name="p_chat",
                    tier=ProviderTier.FAST,
                    base_url=server.base_url,
                    api_key_env="K_CHAT",
                    timeout_s=5.0,
                    max_retries=0,
                    rpm=60,
                    enabled=True,
                    model="m-chat",
                    # the field is codex-only: a chat provider must ignore it
                    reasoning_effort="max",
                ),
            ),
            pause_timeout_s=0.01,
        )
        try:
            result = asyncio.run(router.call(ProviderTier.FAST, chat_prompt))
            assert result.content == "ok"
            assert server.request_paths == ["/chat/completions"]
            assert server.calls[0] == {**chat_prompt, "model": "m-chat"}
        finally:
            server.close()
