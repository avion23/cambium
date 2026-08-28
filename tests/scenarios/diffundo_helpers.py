"""Shared loopback provider fixtures for the Diffundo scenario tests."""

from __future__ import annotations

import json
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any, cast

from cambium.diffundo import ProviderConfig, ProviderTier


class FakeServer:
    """OpenAI-compatible ``/chat/completions`` server on an ephemeral port."""

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


PROMPT = {"messages": [{"role": "user", "content": "hello"}]}


def _config(
    name: str,
    server: FakeServer,
    env: str,
    *,
    tier: ProviderTier = ProviderTier.FAST,
    model: str = "",
    **overrides: Any,
) -> ProviderConfig:
    base: dict[str, Any] = dict(
        timeout_s=5.0,
        max_retries=0,
        rpm=60,
        enabled=True,
        model=model,
        api_key=f"sk-test-{env}",
    )
    base.update(overrides)
    return ProviderConfig(
        name=name,
        tier=tier,
        base_url=server.base_url,
        api_key_env=env,
        **base,
    )
