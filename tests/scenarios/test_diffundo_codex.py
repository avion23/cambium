"""Scenario tests for the codex_responses protocol adapter in diffundo.py.

No mocks, no network: each scenario drives real ``Diffundo.call`` against a
fake SSE codex server (``http.server`` in a background thread, the same
FakeServer pattern as test_diffundo.py). The endpoint profile is injected
through the ``codex_profile`` constructor seam and the bearer credential
through ``credential_source`` — providers.json can never set either. Env vars
for the legacy chat path are set explicitly (no monkeypatch fixture).

Covers the documented + probed codex contract: deterministic Responses-API
request serialization (D8c), fail-closed credential handling, transport
guards, the generic-400 refusal fall-through, and the provider response-size
cap. Request-shape and SSE event-shape wire assertions were culled (the
external API shape may change); the router's own decision/state behavior
remains covered.
"""

from __future__ import annotations

import asyncio
import http.client
import io
import json
import threading
import time
import urllib.error
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any, cast

import pytest

from cambium import diffundo as diffundo_module
from cambium.diffundo import (
    AllProvidersFailed,
    AuthMode,
    CredentialSource,
    Diffundo,
    HealthState,
    Protocol,
    ProviderConfig,
    ProviderError,
    ProviderOutcome,
    ProviderStatus,
    ProviderTier,
    _codex_input_item,
    _codex_request_body,
    _RawResponse,
    _read_provider_sse,
)
from cambium.worker import _PROVIDER_TOOLS_CONFIG, _exposed_tool_schemas

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
        behaviors: list[tuple[int, object, float] | tuple[int, object, float, dict[str, str]]],
        *,
        host: str = "127.0.0.1",
    ) -> None:
        self.behaviors = list(behaviors)
        self.calls: list[dict[str, Any]] = []
        self.request_headers: list[dict[str, str | None]] = []
        self.request_paths: list[str] = []
        self._lock = threading.Lock()
        self._httpd = HTTPServer((host, 0), _Handler)
        cast(Any, self._httpd).fake = self
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
        server = cast(CodexServer, cast(Any, self.server).fake)
        index = server.record(
            body,
            {
                "Content-Type": self.headers.get("Content-Type"),
                "Authorization": self.headers.get("Authorization"),
                "ChatGPT-Account-Id": self.headers.get("ChatGPT-Account-Id"),
                "User-Agent": self.headers.get("User-Agent"),
                "originator": self.headers.get("originator"),
                "session-id": self.headers.get("session-id"),
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

    def log_message(self, format: str, *args: object) -> None:
        pass


# --------------------------------------------------------------------------- #
# SSE + payload builders
# --------------------------------------------------------------------------- #

CODEX_PATH = "/backend-api/codex/responses"


def _stream(*events: dict[str, Any]) -> str:
    return "".join(f"data: {json.dumps(event)}\n\n" for event in events)


class _MemorySocket:
    def __init__(self, data: bytes) -> None:
        self._file = io.BytesIO(data)

    def makefile(self, *_args: Any, **_kwargs: Any) -> io.BytesIO:
        return self._file

    def close(self) -> None:
        self._file.close()


def _chunked_response(
    body: bytes,
    *,
    complete: bool,
    status: int = 200,
    reason: str = "OK",
) -> http.client.HTTPResponse:
    raw = (
        f"HTTP/1.1 {status} {reason}\r\n".encode("ascii")
        + b"Content-Type: text/event-stream\r\n"
        b"Transfer-Encoding: chunked\r\n\r\n"
        + format(len(body), "x").encode("ascii")
        + b"\r\n"
        + body
        + (b"\r\n0\r\n\r\n" if complete else b"")
    )
    response = http.client.HTTPResponse(_MemorySocket(raw))
    response.begin()
    return response


def _delta(text: str) -> dict[str, Any]:
    return {
        "type": "response.output_text.delta",
        "item_id": "it_1",
        "output_index": 0,
        "content_index": 0,
        "delta": text,
    }


def _completed(model: str = "gpt-5.6-luna", text: str = "Hello, world") -> dict[str, Any]:
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
                    "content": [{"type": "output_text", "text": text}],
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


def _completed_call(
    name: str, arguments: dict[str, Any], model: str = "gpt-5.6-luna"
) -> dict[str, Any]:
    return {
        "type": "response.completed",
        "response": {
            "id": "resp_tool",
            "object": "response",
            "model": model,
            "output": [
                {
                    "id": "fc_1",
                    "type": "function_call",
                    "call_id": "call_1",
                    "name": name,
                    "arguments": json.dumps(arguments),
                }
            ],
            "usage": {
                "input_tokens": 12,
                "input_tokens_details": {"cached_tokens": 0},
                "output_tokens": 5,
                "output_tokens_details": {"reasoning_tokens": 0},
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
        _delta(text[len(text) // 2 :]),
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
        _completed(model, text),
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


def _provider_error(failure: AllProvidersFailed) -> ProviderError:
    return cast(ProviderError, failure.last_error)


PROMPT = {"messages": [{"role": "user", "content": "hello"}]}

TOOL_PROMPT = {
    "messages": [
        {"role": "system", "content": "You are a coding assistant."},
        {"role": "user", "content": "read README"},
    ],
    "tools": [
        {
            "name": "read_file",
            "description": "Read a file from the repository",
            "parameters": {
                "type": "object",
                "properties": {"path": {"type": "string"}},
                "required": ["path"],
            },
        }
    ],
    "tool_choice": "auto",
    # chat-only extras must never leak into the codex body
    "max_tokens": 100,
    "max_completion_tokens": 100,
}


# --------------------------------------------------------------------------- #
# 1. request body conversion (exact)
# --------------------------------------------------------------------------- #


def test_codex_stream_larger_than_provider_cap_is_rejected() -> None:
    from cambium.diffundo import MAX_PROVIDER_RESPONSE_BYTES

    server = CodexServer([(200, "x" * (MAX_PROVIDER_RESPONSE_BYTES + 1), 0.0)])
    router = _router(server)
    try:
        with pytest.raises(AllProvidersFailed) as raised:
            asyncio.run(router.call(ProviderTier.FAST, PROMPT))
        assert raised.value.last_error is not None
        assert "response exceeds" in _provider_error(raised.value).message
    finally:
        server.close()


def test_codex_body_serialization_is_byte_identical_across_calls() -> None:
    """D8c: the same prompt serializes to the same request bytes every call —
    fixed field order, no per-call timestamps or ids — so the body head cannot
    churn a provider's exact-prefix cache key."""
    config = _codex_config(None, supports_native_tools=True)
    first = _codex_request_body(config, TOOL_PROMPT)
    second = _codex_request_body(config, TOOL_PROMPT)
    assert json.dumps(first) == json.dumps(second)


def test_codex_native_mode_converts_tools_and_requires_native_controls() -> None:
    config = _codex_config(None, supports_native_tools=True)
    tools = _exposed_tool_schemas(_PROVIDER_TOOLS_CONFIG)

    body = _codex_request_body(
        config,
        {"messages": [{"role": "user", "content": "inspect the repo"}], "tools": tools},
    )

    operational = body["tools"][: len(tools)]
    assert operational == [
        {
            "type": "function",
            "name": tool["name"],
            "description": tool["description"],
            "parameters": tool["parameters"],
        }
        for tool in tools
    ]
    controls = body["tools"][len(tools) :]
    assert [control["name"] for control in controls] == ["plan", "finish"]
    assert all(control["strict"] is True for control in controls)
    assert all(
        set(control["parameters"]["required"]) == set(control["parameters"]["properties"])
        and control["parameters"]["additionalProperties"] is False
        for control in controls
    )
    assert body["tool_choice"] == "required"


def test_codex_native_mode_without_agent_tools_does_not_force_controls() -> None:
    config = _codex_config(None, supports_native_tools=True)

    body = _codex_request_body(config, PROMPT)

    assert "tools" not in body
    assert "tool_choice" not in body


def test_codex_generic_native_tools_do_not_gain_cambium_controls() -> None:
    config = _codex_config(None, supports_native_tools=True)

    body = _codex_request_body(config, TOOL_PROMPT)

    assert body["tool_choice"] == "auto"
    assert [tool["name"] for tool in body["tools"]] == ["read_file"]


def test_codex_required_native_action_never_downgrades_to_text() -> None:
    server = CodexServer(
        [
            (
                200,
                _ok_stream(text='{"type":"finish","summary":"text","objective_met":true}'),
                0.0,
            )
        ]
    )
    config = _codex_config(server, supports_native_tools=True)
    router = Diffundo(
        (config,),
        credential_source=CREDENTIAL,
        codex_profile={"api_origin": server.base_url, "api_path": CODEX_PATH},
        pause_timeout_s=0.01,
    )
    prompt = {
        "messages": [{"role": "user", "content": "finish"}],
        "tools": _exposed_tool_schemas(_PROVIDER_TOOLS_CONFIG),
    }
    try:
        with pytest.raises(AllProvidersFailed) as raised:
            asyncio.run(router.call(ProviderTier.FAST, prompt))
        error = _provider_error(raised.value)
        assert error.outcome is ProviderOutcome.CONFIG_ERROR
        assert "required native action" in error.message
        assert router.health(config.name) is HealthState.DISABLED
    finally:
        server.close()


def test_codex_required_native_mismatch_falls_through_with_serving_provenance() -> None:
    server = CodexServer(
        [
            (200, _ok_stream(text="text instead of required call"), 0.0),
            (
                200,
                _stream(
                    _completed_call(
                        "finish", {"summary": "fallback served", "objective_met": True}
                    )
                ),
                0.0,
            ),
        ]
    )
    bad = _codex_config(server, name="p_bad", supports_native_tools=True)
    good = _codex_config(server, name="p_good", supports_native_tools=True)
    router = Diffundo(
        (bad, good),
        credential_source=CREDENTIAL,
        codex_profile={"api_origin": server.base_url, "api_path": CODEX_PATH},
        pause_timeout_s=0.01,
    )
    prompt = {
        "messages": [{"role": "user", "content": "finish"}],
        "tools": _exposed_tool_schemas(_PROVIDER_TOOLS_CONFIG),
    }
    try:
        result = asyncio.run(router.call(ProviderTier.FAST, prompt))
        assert result.provider == "p_good"
        assert result.model == good.model
        assert result.tool_calls is not None
        assert result.tool_calls[0]["function"]["name"] == "finish"
        assert router.health("p_bad") is HealthState.DISABLED
        assert router.health("p_good") is HealthState.HEALTHY
    finally:
        server.close()


def test_codex_non_native_mode_keeps_messages_and_omits_tool_wire_fields() -> None:
    config = _codex_config(None)

    body = _codex_request_body(config, TOOL_PROMPT)

    assert body["input"] == [
        {
            "role": "developer",
            "content": [{"type": "input_text", "text": "You are a coding assistant."}],
        },
        {"role": "user", "content": [{"type": "input_text", "text": "read README"}]},
    ]
    assert "tools" not in body
    assert "tool_choice" not in body


def test_codex_body_leading_developer_item_is_byte_stable_as_transcript_grows() -> None:
    """The leading system message converts to a byte-identical developer item
    on every turn of a tool loop; only the trailing input items grow."""
    config = _codex_config(None)
    system = "You are Cambium's autonomous coding agent.\nReturn exactly one JSON object."
    prompt_turn1 = {
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": "Begin."},
        ]
    }
    prompt_turn4 = {
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": "Begin."},
            {"role": "assistant", "content": '{"type": "plan", "steps": ["read", "edit"]}'},
            {"role": "user", "content": "tool read_batch ok=true"},
            {"role": "assistant", "content": '{"type": "tool_call", "name": "write_file"}'},
            {"role": "user", "content": "tool write_file ok=true"},
            {
                "role": "assistant",
                "content": '{"type": "finish", "summary": "done", "objective_met": true}',
            },
            {"role": "user", "content": "Continue."},
        ]
    }
    body1 = _codex_request_body(config, prompt_turn1)
    body4 = _codex_request_body(config, prompt_turn4)
    assert body1["input"][0] == body4["input"][0]
    assert body1["input"][0] == {
        "role": "developer",
        "content": [{"type": "input_text", "text": system}],
    }
    assert _codex_input_item(prompt_turn1["messages"][0]) == body1["input"][0]
    # the head serialization is byte-identical across the growing transcript
    assert json.dumps(body1["input"][0]) == json.dumps(body4["input"][0])


# --------------------------------------------------------------------------- #
# 3. in-stream error events and stream termination
# --------------------------------------------------------------------------- #


def test_codex_stream_without_completed_event_is_malformed() -> None:
    server = CodexServer([(200, _stream(_delta("partial")), 0.0)])
    router = _router(server)
    try:
        with pytest.raises(AllProvidersFailed) as exc:
            asyncio.run(router.call(ProviderTier.FAST, PROMPT))
        assert exc.value.last_error is not None
        assert _provider_error(exc.value).outcome is ProviderOutcome.ERROR
        assert "response.completed" in _provider_error(exc.value).message
        assert router.health("p_codex") is HealthState.COOLDOWN
    finally:
        server.close()


def test_codex_incomplete_stream_cascades_after_typed_read_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stream = _stream(_completed(text="partial but complete-looking"))
    primary = _codex_config(None, name="p_codex_incomplete", max_retries=0, priority=0)
    sibling = ProviderConfig(
        name="p_chat_sibling",
        tier=ProviderTier.FAST,
        base_url="http://127.0.0.1",
        api_key_env="",
        api_key="sk-chat-sibling",
        model="m-chat-sibling",
        priority=1,
    )
    router = Diffundo(
        (primary, sibling),
        credential_source=CREDENTIAL,
        pause_timeout_s=0.01,
    )
    typed_errors: list[ProviderError] = []
    statuses: list[dict[str, Any]] = []

    async def scripted_post(
        _self: Diffundo,
        provider: ProviderConfig,
        _prompt: dict[str, Any],
        *,
        timeout_s: float,
        deadline: float | None,
        on_delta: Any = None,
    ) -> _RawResponse:
        del timeout_s, deadline, on_delta
        if provider.protocol is Protocol.CODEX_RESPONSES:
            with _chunked_response(stream.encode("utf-8"), complete=False) as response:
                try:
                    _read_provider_sse(response, provider.name)
                except ProviderError as exc:
                    typed_errors.append(exc)
                    raise
            raise AssertionError("incomplete stream should raise before parsing")
        return _RawResponse(
            {
                "model": provider.model,
                "choices": [
                    {
                        "message": {"role": "assistant", "content": "sibling response"},
                        "finish_reason": "stop",
                    }
                ],
            },
            0.0,
        )

    monkeypatch.setattr(Diffundo, "_post_with_deadline", scripted_post)
    result = asyncio.run(
        router.call(
            ProviderTier.FAST,
            PROMPT,
            on_status=lambda event: statuses.append(dict(event)),
        )
    )

    assert result.provider == sibling.name
    assert result.content == "sibling response"
    assert len(typed_errors) == 1
    assert typed_errors[0].outcome is ProviderOutcome.ERROR
    assert isinstance(typed_errors[0].cause, http.client.IncompleteRead)
    assert typed_errors[0].message == "incomplete provider response"
    assert [event["kind"] for event in statuses] == [
        "provider_attempt",
        "provider_failed",
        "provider_attempt",
        "provider_succeeded",
    ]
    assert statuses[1]["outcome"] == ProviderOutcome.ERROR.value
    assert router.health(primary.name) is HealthState.COOLDOWN


def test_truncated_codex_http_error_preserves_auth_classification(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    body = b'{"error":{"message":"secret codex unauthorized details"}}'

    class ErrorOpener:
        calls = 0

        def open(self, *_args: Any, **_kwargs: Any) -> Any:
            self.calls += 1
            response = _chunked_response(
                body,
                complete=False,
                status=401,
                reason="Unauthorized",
            )
            raise urllib.error.HTTPError(
                "http://127.0.0.1/backend-api/codex/responses",
                401,
                "Unauthorized",
                {},
                response,
            )

    opener = ErrorOpener()
    monkeypatch.setattr(
        diffundo_module.urllib.request,
        "build_opener",
        lambda *_handlers: opener,
    )
    provider = _codex_config(None, name="p_truncated_codex_http_error", max_retries=2)
    router = Diffundo(
        (provider,),
        credential_source=CREDENTIAL,
        codex_profile={"api_origin": "http://127.0.0.1", "api_path": CODEX_PATH},
        pause_timeout_s=0.01,
    )

    async def direct_post(
        _self: Diffundo,
        current_provider: ProviderConfig,
        prompt: dict[str, Any],
        *,
        timeout_s: float,
        deadline: float | None,
        on_delta: Any = None,
    ) -> _RawResponse:
        del deadline, on_delta
        return _self._post_sync(current_provider, prompt, timeout_s)

    monkeypatch.setattr(Diffundo, "_post_with_deadline", direct_post)
    with pytest.raises(AllProvidersFailed) as raised:
        asyncio.run(router.call(ProviderTier.FAST, PROMPT))

    error = _provider_error(raised.value)
    assert error.outcome is ProviderOutcome.AUTH_ERROR
    assert error.http_status == 401
    assert opener.calls == 1
    assert router.health(provider.name) is HealthState.DISABLED
    assert "secret codex unauthorized details" not in error.message


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
        assert _provider_error(exc.value).outcome is ProviderOutcome.ERROR
        assert "overloaded" in _provider_error(exc.value).message
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
        assert _provider_error(exc.value).outcome is ProviderOutcome.CONFIG_ERROR
        assert "model_not_found" in _provider_error(exc.value).message
        # non-retryable config error: no retries, provider disabled
        assert len(server.calls) == 1
        assert router.health("p_codex") is HealthState.DISABLED
        assert router.status("p_codex") is ProviderStatus.DISABLED
    finally:
        server.close()


def test_codex_completed_stream_with_refusal_like_text_passes_through() -> None:
    content = (
        "A policy guide explains when to refuse; a provider cannot assist with prohibited work."
    )
    server = CodexServer([(200, _ok_stream(text=content), 0.0)])
    router = _router(server)
    try:
        result = asyncio.run(router.call(ProviderTier.FAST, PROMPT))
        assert result.content == content
        assert router.health("p_codex") is HealthState.HEALTHY
        assert len(server.calls) == 1
    finally:
        server.close()


def test_codex_stream_policy_refusal_falls_through() -> None:
    stream = _stream(
        {
            "type": "error",
            "error": {
                "type": "content_policy_error",
                "code": "content_policy",
                "message": "The request was blocked by the content policy",
            },
        }
    )
    server = CodexServer([(200, stream, 0.0)])
    router = _router(server)
    try:
        with pytest.raises(AllProvidersFailed) as exc:
            asyncio.run(router.call(ProviderTier.FAST, PROMPT))
        assert exc.value.last_error is not None
        assert _provider_error(exc.value).outcome is ProviderOutcome.REFUSAL
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
        assert _provider_error(exc.value).outcome is ProviderOutcome.CONFIG_ERROR
        assert "HTTP 400" in _provider_error(exc.value).message
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
        assert _provider_error(exc.value).outcome is ProviderOutcome.CONFIG_ERROR
        assert router.health("p_codex") is HealthState.DISABLED
    finally:
        server.close()


def test_codex_http_400_shape_rejection_stays_content_refusal() -> None:
    # The documented live shape rejections ("Stream must be set to true",
    # "Store must be set to false", "Input must be a list") name no model or
    # parameter; they keep the generic 400 -> REFUSAL fall-through.
    server = CodexServer([(400, {"error": {"message": "Input must be a list"}}, 0.0)])
    router = _router(server)
    try:
        with pytest.raises(AllProvidersFailed) as exc:
            asyncio.run(router.call(ProviderTier.FAST, PROMPT))
        assert exc.value.last_error is not None
        assert _provider_error(exc.value).outcome is ProviderOutcome.REFUSAL
        assert "HTTP 400" in _provider_error(exc.value).message
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
        assert _provider_error(exc.value).outcome is ProviderOutcome.AUTH_ERROR
        assert "credential source" in _provider_error(exc.value).message
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
        assert _provider_error(exc.value).outcome is ProviderOutcome.AUTH_ERROR
        assert "empty access token" in _provider_error(exc.value).message
        assert server.calls == []
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
    assert _provider_error(exc.value).outcome is ProviderOutcome.AUTH_ERROR
    assert "http transport is allowed only for loopback hosts" in _provider_error(exc.value).message
    assert router.health("p_codex") is HealthState.DISABLED


def test_codex_redirect_fails_closed_and_never_contacts_target() -> None:
    target = CodexServer([(200, _ok_stream(), 0.0)])
    redirector = CodexServer([(302, {}, 0.0, {"Location": f"{target.base_url}{CODEX_PATH}"})])
    router = _router(redirector)
    try:
        with pytest.raises(AllProvidersFailed) as exc:
            asyncio.run(router.call(ProviderTier.FAST, PROMPT))
        assert exc.value.last_error is not None
        assert _provider_error(exc.value).outcome is ProviderOutcome.AUTH_ERROR
        assert "redirect" in _provider_error(exc.value).message
        assert router.health("p_codex") is HealthState.DISABLED
        assert len(redirector.calls) == 1
        assert target.calls == []  # the bearer never followed the redirect
    finally:
        redirector.close()
        target.close()
