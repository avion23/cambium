"""Durable per-call provider usage events (implementation plan step 3).

One scenario drives a scripted provider session (tool_call read_file ->
tool_call edit_file -> finish -> failing call) through the real supervisor +
worker subprocess + fake OpenAI server, then replays the session's EventStore:

- every router call persists one redacted ``usage_event`` with provider,
  model, turn, token fields, estimated cost, latency, request-rate status,
  prompt-prefix bytes, and the provider-reported cache-hit flag;
- a 429 with ``Retry-After`` and a reported account-quota owner surfaces both
  on the same-provider retry's event;
- a failed call's event carries the failure reason and omits the fields the
  provider never reported;
- credentials never reach the durable log.
"""

from __future__ import annotations

import asyncio
import copy
import json
import os
import subprocess
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any

import pytest

from cambium.supervisor import read_events, run_plan

ROOT = Path(__file__).resolve().parents[2]
WORKER = "cambium.worker"
PROVIDER_KEY = "CAMBIUM_PROVIDER_USAGE_PROVIDER_API_KEY"
PROVIDER_SECRET = "usage-event-provider-secret"
TASK_TEXT = "Append a single marker line starting with '// usage-' to target.txt."

REQUEST_LOCK = threading.Lock()
REQUESTS: list[dict[str, Any]] = []
REQUEST_AUTHORIZATION: list[str] = []
RESPONSES: list[dict[str, Any]] = []

DEFAULT_USAGE = {"prompt_tokens": 17, "completion_tokens": 9, "total_tokens": 26}
CACHED_USAGE = {
    "prompt_tokens": 17,
    "completion_tokens": 9,
    "total_tokens": 26,
    "cached_tokens": 3,
}


def _reset_server() -> None:
    with REQUEST_LOCK:
        REQUESTS.clear()
        REQUEST_AUTHORIZATION.clear()
        RESPONSES.clear()


def _enqueue(
    content: str,
    *,
    model: str | None = None,
    usage: dict[str, Any] | None = None,
) -> None:
    completion = {
        "id": "chatcmpl-usage-event",
        "object": "chat.completion",
        "model": model or "usage-model",
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": content},
                "finish_reason": "stop",
            }
        ],
        "usage": usage or copy.deepcopy(DEFAULT_USAGE),
    }
    with REQUEST_LOCK:
        RESPONSES.append({"status": 200, "payload": completion, "headers": {}})


def _enqueue_error(
    payload: dict[str, Any],
    *,
    status: int = 500,
    headers: dict[str, str] | None = None,
) -> None:
    with REQUEST_LOCK:
        RESPONSES.append(
            {"status": status, "payload": payload, "headers": headers or {}}
        )


def _rate_limit_error() -> dict[str, Any]:
    return {
        "error": {
            "message": "rate limit exceeded",
            "type": "rate_limit_error",
            "code": "rate_limit_exceeded",
            "rate_limit": {"scope": "account", "quota_owner": "org-acme"},
        }
    }


def _server_error() -> dict[str, Any]:
    return {"error": {"message": "server exploded", "type": "server_error", "code": 500}}


class _FakeOpenAIHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.0"

    def do_POST(self) -> None:  # noqa: N802 (http.server API)
        if self.path != "/chat/completions":
            self.send_error(404)
            return
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length)
        try:
            body = json.loads(raw.decode("utf-8"))
        except json.JSONDecodeError:
            body = {}
        with REQUEST_LOCK:
            REQUESTS.append(body)
            REQUEST_AUTHORIZATION.append(self.headers.get("Authorization", ""))
            response = RESPONSES.pop(0) if RESPONSES else {
                "status": 500,
                "payload": _server_error(),
                "headers": {},
            }
        encoded = json.dumps(response["payload"]).encode("utf-8")
        self.send_response(response["status"])
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(encoded)))
        self.send_header("Connection", "close")
        for name, value in response["headers"].items():
            self.send_header(name, value)
        self.end_headers()
        self.wfile.write(encoded)

    def log_message(self, *args: object) -> None:
        pass


class _FakeOpenAIServer:
    def __init__(self) -> None:
        self._httpd = HTTPServer(("127.0.0.1", 0), _FakeOpenAIHandler)
        self._thread = threading.Thread(target=self._httpd.serve_forever, daemon=True)
        self._thread.start()

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self._httpd.server_port}"

    def close(self) -> None:
        self._httpd.shutdown()
        self._httpd.server_close()
        self._thread.join()


def _make_repo(repo: Path) -> str:
    repo.mkdir(parents=True)
    subprocess.run(["git", "init", "-b", "main", str(repo)], check=True, capture_output=True)
    subprocess.run(
        ["git", "-C", str(repo), "config", "user.name", "usage-event-test"], check=True
    )
    subprocess.run(
        ["git", "-C", str(repo), "config", "user.email", "usage-event@test"], check=True
    )
    subprocess.run(["git", "-C", str(repo), "config", "gc.auto", "0"], check=True)
    (repo / "target.txt").write_text("fixture\n", encoding="utf-8")
    (repo / "notes.txt").write_text("output-sentinel-9x7q\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(repo), "add", "-A"], check=True, capture_output=True)
    subprocess.run(
        ["git", "-C", str(repo), "commit", "-m", "initial"], check=True, capture_output=True
    )
    return subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "refs/heads/main"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def _provider_config(path: Path, base_url: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "providers": [
                    {
                        "name": "usage-provider",
                        "tier": "fast",
                        "base_url": base_url,
                        "api_key_env": PROVIDER_KEY,
                        "timeout_s": 2.0,
                        "max_retries": 1,
                        "rpm": 120,
                        "enabled": True,
                        "model": "usage-model",
                        "priority": 0,
                        "cooldown_s": 1.0,
                        "price": 0.0,
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    return path


def _task(session_dir: Path, repo: Path, base: str, config_path: Path) -> dict[str, Any]:
    task = {
        "task_id": "usage-events",
        "task": TASK_TEXT,
        "repo": str(repo),
        "worktree_path": str(session_dir / "worker-wt"),
        "branch": "usage-events",
        "worker": WORKER,
        "gate": "test \"$(grep -Ec '^// usage-' target.txt)\" -eq 1",
        "base_commit": base,
        "fanout_config": {
            "tier": "fast",
            "model": "usage-model",
            "call_budget_s": 5.0,
            "pause_timeout_s": 0.1,
        },
        "provider_env_keys": [PROVIDER_KEY, "NO_PROXY", "no_proxy"],
        "ready_timeout_s": 5.0,
        "gate_timeout_s": 5.0,
        "max_wall_s": 20.0,
        "heartbeat_interval_s": 0.05,
    }
    assert config_path.is_absolute()
    assert "target_file" not in task
    assert "marker" not in task
    return task


def _set_provider_env(monkeypatch: pytest.MonkeyPatch, config_path: Path) -> None:
    monkeypatch.setenv("CAMBIUM_PROVIDERS", str(config_path.resolve()))
    monkeypatch.setenv(PROVIDER_KEY, PROVIDER_SECRET)
    monkeypatch.setenv("NO_PROXY", "127.0.0.1,localhost")
    monkeypatch.setenv("no_proxy", "127.0.0.1,localhost")
    monkeypatch.setenv(
        "PYTHONPATH",
        os.pathsep.join(filter(None, [str(ROOT / "src"), os.environ.get("PYTHONPATH")])),
    )


def test_durable_usage_events_redacted_and_missing_fields_omitted(
    tmp_path, monkeypatch
) -> None:
    _reset_server()
    server = _FakeOpenAIServer()
    try:
        config_path = _provider_config(tmp_path / "providers.json", server.base_url)
        _set_provider_env(monkeypatch, config_path)
        _enqueue(
            '{"type":"tool_call","name":"read_file","arguments":{"path":"notes.txt"}}'
        )
        # turn 2: 429 with Retry-After + reported quota owner, then a same-provider
        # retry succeeds (max_retries=1)
        _enqueue_error(_rate_limit_error(), status=429, headers={"Retry-After": "1"})
        _enqueue(
            '{"type":"tool_call","name":"edit_file","arguments":'
            '{"path":"target.txt","old_string":"fixture\\n",'
            '"new_string":"fixture\\n// usage-1\\n"}}',
            usage=CACHED_USAGE,
        )
        # turn 3: a failing call (retried once) so the failure-reason event is
        # also durable; there is no finish, so the task fails
        _enqueue_error(_server_error())
        _enqueue_error(_server_error())

        session_dir = tmp_path / "session"
        repo = session_dir / "repo"
        base = _make_repo(repo)
        task = _task(session_dir, repo, base, config_path)
        task["max_restarts"] = 0
        result = asyncio.run(run_plan(session_dir, {"tasks": [task]}))
        events = read_events(session_dir)

        # the failing turn means the task fails, but the events are durable
        assert result.exit_code != 0
        assert result.results[0].status == "failed"
        usage_events = [e for e in events if e["kind"] == "usage_event"]
        assert [e["payload"]["turn"] for e in usage_events] == [1, 2, 3]

        # every call recorded the provider/model/turn/rate-status/prefix evidence
        for event in usage_events:
            payload = event["payload"]
            assert payload["provider"] == "usage-provider"
            assert payload["model"] == "usage-model"
            assert isinstance(payload["request_rate_status"], str)
            assert isinstance(payload["prompt_prefix_bytes"], int)
            assert payload["prompt_prefix_bytes"] > 0
            if "failure_reason" not in payload:
                # completed calls carry cost + latency; failed calls omit them
                assert isinstance(payload["estimated_cost_usd"], float)
                assert payload["estimated_cost_usd"] == 0.0
                assert isinstance(payload["latency_s"], float) and payload["latency_s"] >= 0.0

        # prompt-prefix stability: the leading system message is byte-identical
        # across every turn of the same task
        prefixes = {e["payload"]["prompt_prefix_bytes"] for e in usage_events}
        assert len(prefixes) == 1

        first = usage_events[0]["payload"]
        assert first["usage"] == DEFAULT_USAGE
        assert first["provider_cache_hit"] is False  # usage present, no cache fields
        assert "retry_after_s" not in first
        assert "account_quota_owner" not in first
        assert "failure_reason" not in first

        retried = usage_events[1]["payload"]
        assert retried["retry_after_s"] == 1.0
        assert retried["account_quota_owner"] == "org-acme"
        assert retried["usage"]["cached_tokens"] == 3
        assert retried["provider_cache_hit"] is True  # provider-reported cache hit
        assert "failure_reason" not in retried

        failed = usage_events[2]["payload"]
        assert failed["failure_reason"].startswith("error: HTTP 500")
        assert failed["request_rate_status"] == "cooldown"
        # the failed call never produced usage/cost/latency or cache evidence
        assert "usage" not in failed
        assert "estimated_cost_usd" not in failed
        assert "latency_s" not in failed
        assert "retry_after_s" not in failed
        assert "account_quota_owner" not in failed
        assert "provider_cache_hit" not in failed

        # the 429 was retried on the same provider (no fallback, no weighted routing)
        with REQUEST_LOCK:
            assert len(REQUESTS) == 5
            assert all(value == f"Bearer {PROVIDER_SECRET}" for value in REQUEST_AUTHORIZATION)
            assert all(request["model"] == "usage-model" for request in REQUESTS)

        event_text = json.dumps(events)
        assert PROVIDER_SECRET not in event_text
        assert "You are Cambium's autonomous coding agent." not in event_text
        assert "output-sentinel-9x7q" not in event_text
    finally:
        server.close()
