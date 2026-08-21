"""M6 staging: provider completion -> worker decision -> publish."""

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

pytest.importorskip("cambium.diffundo")

from cambium.auth import derived_env_name  # noqa: E402
from cambium.diffundo import Diffundo, ProviderError, ProviderOutcome, ProviderTier  # noqa: E402
from cambium.provider_config import load_providers  # noqa: E402
from cambium.supervisor import read_events, run_plan  # noqa: E402

ROOT = Path(__file__).resolve().parents[2]
DECISION = "append marker line to file target.txt: // m6-llm-marker"
STATIC_PREFIX = (
    "You are Cambium's deterministic coding worker.\n"
    "Return exactly one append-marker decision.\n"
    "Change only the requested file."
)
DYNAMIC_TAIL = (
    "task_id=m6-staging\n"
    "request_id=m6-request-001\n"
    "requested_decision=append a marker to target.txt"
)
FAKE_API_KEY = "test-only-not-a-network-key"
_PROXY_ENV_VARS = (
    "ALL_PROXY",
    "all_proxy",
    "FTP_PROXY",
    "ftp_proxy",
    "HTTP_PROXY",
    "http_proxy",
    "HTTPS_PROXY",
    "https_proxy",
)
_LOOPBACK_NO_PROXY = "127.0.0.1,localhost"

# The handler returns this module-level payload. A scenario can replace it to
# exercise another scripted completion without changing the HTTP adapter.
SCRIPTED_COMPLETION: dict[str, Any] = {
    "id": "chatcmpl-m6-test",
    "object": "chat.completion",
    "model": "m6-fake-model",
    "choices": [
        {
            "index": 0,
            "message": {"role": "assistant", "content": DECISION},
            "finish_reason": "stop",
        }
    ],
    "usage": {"prompt_tokens": 18, "completion_tokens": 11, "total_tokens": 29},
}
SCRIPTED_STATUS_CODES: list[int] = []
FAKE_REQUESTS: dict[str, Any] = {"count": 0, "bodies": [], "statuses": []}
_FAKE_LOCK = threading.Lock()


class _FakeOpenAIHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.0"

    def do_POST(self) -> None:  # noqa: N802 (http.server API)
        if self.path != "/chat/completions":
            self.send_error(404)
            return
        if self.headers.get("Authorization") != f"Bearer {FAKE_API_KEY}":
            self.send_error(401, "invalid Authorization header")
            return

        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b""
        try:
            body = json.loads(raw.decode("utf-8") or "{}")
        except json.JSONDecodeError:
            body = {}
        if not isinstance(body, dict):
            body = {}

        with _FAKE_LOCK:
            request_index = FAKE_REQUESTS["count"]
            FAKE_REQUESTS["count"] += 1
            FAKE_REQUESTS["bodies"].append(body)
            status = (
                SCRIPTED_STATUS_CODES[request_index]
                if request_index < len(SCRIPTED_STATUS_CODES)
                else 200
            )
            FAKE_REQUESTS["statuses"].append(status)
            response = (
                copy.deepcopy(SCRIPTED_COMPLETION)
                if status == 200
                else {"error": {"message": f"forced HTTP {status}"}}
            )

        encoded = json.dumps(response).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(encoded)))
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(encoded)

    def log_message(self, format: str, *args: object) -> None:
        pass


class _FakeOpenAIServer:
    """A loopback-only OpenAI-compatible server with request capture."""

    def __init__(self) -> None:
        self._httpd = HTTPServer(("127.0.0.1", 0), _FakeOpenAIHandler)
        self._thread = threading.Thread(
            target=self._httpd.serve_forever,
            kwargs={"poll_interval": 0.005},
            daemon=True,
        )
        self._thread.start()

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self._httpd.server_port}"

    def close(self) -> None:
        self._httpd.shutdown()
        self._httpd.server_close()
        self._thread.join()


def _reset_fake_server() -> None:
    with _FAKE_LOCK:
        FAKE_REQUESTS["count"] = 0
        FAKE_REQUESTS["bodies"] = []
        FAKE_REQUESTS["statuses"] = []
        SCRIPTED_STATUS_CODES.clear()


def _write_provider_config(
    path: Path,
    base_url: str,
    *,
    provider_names: tuple[str, ...] = ("m6-fake-fast",),
) -> None:
    path.write_text(
        json.dumps(
            {
                "providers": [
                    {
                        "name": name,
                        "tier": "fast",
                        "base_url": base_url,
                        "api_key_env": derived_env_name(name),
                        "timeout_s": 2.0,
                        "max_retries": 0,
                        "rpm": 120,
                        "enabled": True,
                        "model": "m6-fake-model",
                        "priority": priority,
                        "cooldown_s": 1.0,
                        "price": 0.0,
                    }
                    for priority, name in enumerate(provider_names)
                ]
            }
        ),
        encoding="utf-8",
    )


def _make_repo(repo: Path) -> str:
    repo.mkdir(parents=True)
    subprocess.run(["git", "init", "-b", "main", str(repo)], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.name", "m6-test"], check=True)
    subprocess.run(
        ["git", "-C", str(repo), "config", "user.email", "m6@test"], check=True
    )
    subprocess.run(["git", "-C", str(repo), "config", "gc.auto", "0"], check=True)
    (repo / "target.txt").write_text("fixture\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(repo), "add", "-A"], check=True, capture_output=True)
    subprocess.run(
        ["git", "-C", str(repo), "commit", "-m", "initial"],
        check=True,
        capture_output=True,
    )
    return subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "refs/heads/main"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-C", str(repo), *args], capture_output=True, text=True, check=True
    )


def _decision_fields(content: str) -> tuple[str, str]:
    prefix = "append marker line to file "
    if not content.startswith(prefix):
        raise AssertionError(f"unexpected scripted decision: {content!r}")
    target_file, separator, marker = content[len(prefix) :].partition(": ")
    if not separator or not target_file or not marker:
        raise AssertionError(f"malformed scripted decision: {content!r}")
    return target_file, marker


def _remove_worker_worktree(repo: Path, worktree: Path, branch: str) -> None:
    subprocess.run(
        ["git", "-C", str(repo), "worktree", "remove", "--force", str(worktree)],
        check=False,
        capture_output=True,
    )
    subprocess.run(
        ["git", "-C", str(repo), "branch", "-D", branch],
        check=False,
        capture_output=True,
    )


def _set_absolute_pythonpath(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep module workers importable after the supervisor changes cwd."""
    pythonpath = os.pathsep.join(
        filter(None, [str(ROOT / "src"), os.environ.get("PYTHONPATH")])
    )
    monkeypatch.setenv("PYTHONPATH", pythonpath)


def _isolate_proxy_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep urllib calls to the fake server on loopback, never through a proxy."""
    for variable in _PROXY_ENV_VARS:
        monkeypatch.delenv(variable, raising=False)
    monkeypatch.setenv("NO_PROXY", _LOOPBACK_NO_PROXY)
    monkeypatch.setenv("no_proxy", _LOOPBACK_NO_PROXY)


@pytest.mark.slow  # real git + worker subprocess + sequencer publish
def test_m6_provider_decision_and_atomic_publish(tmp_path: Path, monkeypatch) -> None:
    """Run two uncached provider calls, then publish the second decision once."""
    _reset_fake_server()
    server = _FakeOpenAIServer()
    session_dir = tmp_path / "session"
    repo = session_dir / "repo"
    worktree = session_dir / "worker-wt"
    branch = "wt-m6-staging"

    try:
        _isolate_proxy_environment(monkeypatch)
        monkeypatch.setenv("CAMBIUM_PROVIDER_M6_FAKE_FAST_API_KEY", FAKE_API_KEY)
        _set_absolute_pythonpath(monkeypatch)
        config_path = tmp_path / "providers.json"
        _write_provider_config(config_path, server.base_url)
        providers = load_providers(config_path)
        assert [provider.name for provider in providers] == ["m6-fake-fast"]
        assert providers[0].tier is ProviderTier.FAST

        prompt = {
            "messages": [
                {"role": "system", "content": STATIC_PREFIX},
                {"role": "user", "content": DYNAMIC_TAIL},
            ]
        }
        router = Diffundo(providers, call_budget_s=5.0, pause_timeout_s=0.1)

        first = asyncio.run(router.call(ProviderTier.FAST, prompt))
        second = asyncio.run(router.call(ProviderTier.FAST, prompt))
        assert first.provider == second.provider == "m6-fake-fast"
        assert first.model == second.model == "m6-fake-model"
        assert first.content == second.content == DECISION
        assert first.usage == second.usage == {
            "prompt_tokens": 18,
            "completion_tokens": 11,
            "total_tokens": 29,
        }
        assert first.latency_s >= 0.0

        assert FAKE_REQUESTS["count"] == 2  # identical calls are not cached
        bodies = FAKE_REQUESTS["bodies"]
        assert len(bodies) == 2
        assert bodies[0] == bodies[1]
        body = bodies[0]
        assert body["model"] == "m6-fake-model"
        assert body["messages"] == prompt["messages"]
        assert all(
            isinstance(message, dict)
            and isinstance(message.get("role"), str)
            and isinstance(message.get("content"), str)
            for message in body["messages"]
        )
        assert body["messages"][0]["content"] == STATIC_PREFIX
        assert body["messages"][-1]["content"] == DYNAMIC_TAIL
        assert "request_id" not in body["messages"][0]["content"].lower()
        assert "request_id" in body["messages"][-1]["content"]

        target_file, marker = _decision_fields(second.content)
        base = _make_repo(repo)
        plan = {
            "tasks": [
                {
                    "task_id": "m6-staging",
                    "task": second.content,
                    "repo": str(repo),
                    "worktree_path": str(worktree),
                    "branch": branch,
                    "worker": "cambium.worker",
                    "target_file": target_file,
                    "marker": marker,
                    "write_marker": True,
                    "base_commit": base,
                    "ready_timeout_s": 5.0,
                    "max_wall_s": 20.0,
                }
            ]
        }

        result = asyncio.run(run_plan(session_dir, plan))
        assert result.exit_code == 0
        assert len(result.results) == 1
        task_result = result.results[0]
        assert task_result.task_id == "m6-staging"
        assert task_result.status == "succeeded"
        assert task_result.merge_sha

        main_sha = _git(repo, "rev-parse", "refs/heads/main").stdout.strip()
        assert main_sha == task_result.merge_sha
        assert _git(repo, "merge-base", "--is-ancestor", base, main_sha).returncode == 0
        assert _git(repo, "rev-list", "--count", f"{base}..{main_sha}").stdout.strip() == "1"
        changed_files = _git(repo, "diff", "--name-only", f"{base}..{main_sha}").stdout.splitlines()
        assert changed_files == [target_file]
        diff = _git(repo, "diff", f"{base}..{main_sha}").stdout
        assert f"+{marker}" in diff
        base_target = _git(repo, "show", f"{base}:{target_file}").stdout
        published_target = _git(repo, "show", f"{main_sha}:{target_file}").stdout
        assert published_target == base_target.rstrip("\n") + f"\n{marker}\n"
        assert published_target.splitlines().count(marker) == 1

        events = read_events(session_dir)
        task_events = [event for event in events if event["task_id"] == "m6-staging"]
        positions = {
            kind: next(index for index, event in enumerate(task_events) if event["kind"] == kind)
            for kind in ("result", "merge_started", "merge_committed")
        }
        assert positions["result"] < positions["merge_started"]
        assert positions["merge_started"] < positions["merge_committed"]
        result_events = [event for event in task_events if event["kind"] == "result"]
        assert len(result_events) == 1
        assert result_events[0]["payload"]["status"] == "succeeded"
        merge_events = [event for event in task_events if event["kind"] == "merge_committed"]
        assert len(merge_events) == 1
        assert merge_events[0]["payload"]["new"] == main_sha
        assert events[-1]["kind"] == "session_ended"
    finally:
        _remove_worker_worktree(repo, worktree, branch)
        server.close()


def test_m6_forced_429_falls_back_to_next_provider(tmp_path: Path, monkeypatch) -> None:
    """A forced quota response falls through to the next local fake provider."""
    _isolate_proxy_environment(monkeypatch)
    _reset_fake_server()
    server = _FakeOpenAIServer()
    try:
        with _FAKE_LOCK:
            SCRIPTED_STATUS_CODES[:] = [429, 200]
        for provider_name in ("m6-fake-429", "m6-fake-fallback"):
            monkeypatch.setenv(derived_env_name(provider_name), FAKE_API_KEY)
        config_path = tmp_path / "providers.json"
        _write_provider_config(
            config_path,
            server.base_url,
            provider_names=("m6-fake-429", "m6-fake-fallback"),
        )
        providers = load_providers(config_path)
        router = Diffundo(providers, call_budget_s=5.0, pause_timeout_s=0.1)

        failed_outcomes: list[ProviderOutcome] = []
        original_attempt = router._attempt

        async def record_attempt(*args: Any, **kwargs: Any) -> Any:
            try:
                return await original_attempt(*args, **kwargs)
            except ProviderError as exc:
                failed_outcomes.append(exc.outcome)
                raise

        monkeypatch.setattr(router, "_attempt", record_attempt)

        prompt = {"messages": [{"role": "user", "content": "hello"}]}
        result = asyncio.run(router.call(ProviderTier.FAST, prompt))

        assert result.provider == "m6-fake-fallback"
        assert result.content == DECISION
        assert failed_outcomes == [ProviderOutcome.QUOTA]
        assert FAKE_REQUESTS["statuses"] == [429, 200]
        assert FAKE_REQUESTS["count"] == 2
    finally:
        server.close()
