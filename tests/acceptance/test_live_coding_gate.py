"""The live coding gate: does Cambium actually code?

This is the acceptance test every execution-loop change must pass. It runs
one real provider-backed coding task through ``supervisor.run_plan`` — a real
model, real tool calls, a real fenced commit on a disposable scratch repo —
and asserts the whole chain: provider call, tool execution, single-commit
publication touching only the target file, durable usage events, and
credential redaction. A green marker fixture or loopback stub is never
evidence that the harness codes; this test is.

The test skips until a configured provider credential is resolvable through
the provider's environment variable or the local OpenCode auth store (the
same sources the transport matrix uses). Run explicitly with:

    python -m pytest -m acceptance tests/acceptance/test_live_coding_gate.py -rs
"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import pytest

from cambium import supervisor
from cambium.provider_config import AuthMode, load_providers
from cambium.store import read_events_file

# Provider preference: cheapest proven coding plan first.
_PROVIDER_PREFERENCE = ("zai", "opencode-go", "opencode-zen", "openrouter")
_AUTH_STORE_PATHS = (Path.home() / ".local" / "share" / "opencode" / "auth.json",)
# OpenCode's credential store names differ from Cambium's provider profiles.
_AUTH_ALIASES: dict[str, tuple[str, ...]] = {
    "zai": ("zai-coding-plan", "zai"),
    "opencode-go": ("opencode", "opencode-zen"),
    "opencode-zen": ("opencode", "opencode-go"),
}


def _api_key_from_auth_sources(provider_name: str) -> str | None:
    """Find one API key for ``provider_name`` in the supported auth stores."""
    names = _AUTH_ALIASES.get(provider_name, (provider_name,))
    for path in _AUTH_STORE_PATHS:
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        entries = raw if isinstance(raw, Mapping) else {}
        for name in names:
            value = entries.get(name)
            if isinstance(value, Mapping):
                value = value.get("key")
            if isinstance(value, str) and value:
                return value
    return None


def _pick_provider() -> tuple[Any, str] | None:
    """Preference-ordered provider/key resolution."""
    found: list[tuple[int, Any, str]] = []
    for provider in load_providers():
        if provider.auth is not AuthMode.API_KEY:
            continue
        key = os.environ.get(provider.api_key_env, "") or (
            _api_key_from_auth_sources(provider.name) or ""
        )
        if not key:
            continue
        order = (
            _PROVIDER_PREFERENCE.index(provider.name)
            if provider.name in _PROVIDER_PREFERENCE
            else len(_PROVIDER_PREFERENCE)
        )
        found.append((order, provider, key))
    if not found:
        return None
    found.sort(key=lambda item: item[0])
    return found[0][1], found[0][2]


def _scratch_repo(tmp_path: Path) -> tuple[Path, str]:
    repo = tmp_path / "repo"
    repo.mkdir(parents=True)
    env = {
        **os.environ,
        "GIT_AUTHOR_NAME": "gate",
        "GIT_COMMITTER_NAME": "gate",
        "GIT_AUTHOR_EMAIL": "gate@invalid",
        "GIT_COMMITTER_EMAIL": "gate@invalid",
    }

    def git(*args: str) -> None:
        subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True, env=env)

    git("init", "-q", "-b", "main")
    (repo / "calc.py").write_text("def add(a, b):\n    return a + b\n", encoding="utf-8")
    git("add", "calc.py")
    git("commit", "-qm", "init")
    base = subprocess.run(
        ["git", "rev-parse", "refs/heads/main"],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    return repo, base


def _session_events(session_dir: Path) -> list[dict[str, Any]]:
    db = session_dir / ".cambium" / "events.db"
    if not db.is_file():
        db = session_dir / "events.db"
    return [dict(event) for event in read_events_file(db)]


def _run_gate(
    tmp_path: Path, provider: Any, key: str, task_text: str
) -> tuple[Any, Path, str, Path]:
    # The real worker confines worktree_path to the scratch repo's parent:
    # repo at <session>/repo, worktree at <session>/wt.
    session_dir = tmp_path / "session"
    session_dir.mkdir()
    repo, base = _scratch_repo(session_dir)

    # Credential handoff (the documented provider_config_path mechanism): the
    # worker resolves provider.api_key only from its loaded config, so the
    # gate materializes a session-scoped config holding the resolved key.
    # 0600, inside the disposable session dir; never inside the repo.
    config_path = session_dir / "providers.json"
    config_path.write_text(
        json.dumps(
            {
                "providers": [
                    {
                        "name": provider.name,
                        "tier": getattr(getattr(provider, "tier", None), "value", "fast"),
                        "base_url": provider.base_url,
                        "model": provider.model,
                        "auth": "api_key",
                        "protocol": "chat_completions",
                        "api_key": key,
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    config_path.chmod(0o600)
    tier = getattr(getattr(provider, "tier", None), "value", "fast")
    task: dict[str, Any] = {
        "task_id": "live-coding-gate",
        "task": task_text,
        "repo": str(repo),
        "worktree_path": str(session_dir / "wt"),
        "branch": "live-gate",
        "base_commit": base,
        # Pin admission to the resolved provider: the operator's config may
        # also carry codex_chatgpt/other entries whose credentials this gate
        # deliberately does not hold. Unset credentials must fail closed.
        "assigned_provider": provider.name,
        "authorized_providers": [provider.name],
        "authorized_providers_explicit": True,
        "provider_config_path": str(config_path),
        "fanout_config": {
            "tier": tier,
            "model": provider.model,
            "call_budget_s": 180.0,
        },
        "provider_env_keys": [provider.api_key_env],
        "max_wall_s": 300.0,
        "max_turns": 10,
    }
    result = asyncio.run(
        supervisor.run_plan(
            session_dir,
            [task],
            routing_state_path=session_dir / "routing-state.json",
            max_concurrent_tasks=1,
        )
    )
    return result, repo, base, session_dir


def _main_head(repo: Path) -> str:
    return subprocess.run(
        ["git", "rev-parse", "refs/heads/main"],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def _main_changed_files(repo: Path, base: str) -> list[str]:
    out = subprocess.run(
        ["git", "diff", "--name-only", f"{base}..refs/heads/main"],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    return [line for line in out.splitlines() if line.strip()]


@pytest.mark.acceptance
def test_live_coding_gate(tmp_path: Path) -> None:
    resolved = _pick_provider()
    if resolved is None:
        pytest.skip("no configured provider credential is resolvable for the live gate")
    provider, key = resolved

    task_text = (
        "In calc.py, add a one-line docstring to the add function, then verify it "
        'by running: python3 -c "import calc; print(calc.add(2, 3))". Do not run git.'
    )
    attempt_evidence: list[str] = []
    last_attempt: tuple[Any, Path, str, Path] | None = None
    outcomes: list[Any] = []

    # Test-only best-of-two tolerance for transient live-provider/model action
    # failures. Product execution remains retry-free.
    for attempt in range(1, 3):
        attempt_dir = tmp_path / f"attempt-{attempt}"
        attempt_dir.mkdir()
        last_attempt = _run_gate(attempt_dir, provider, key, task_text)
        outcomes = list(last_attempt[0].results)
        if outcomes and outcomes[0].status == "succeeded":
            break
        if not outcomes:
            attempt_evidence.append(f"attempt {attempt}: run_plan produced no task results")
            continue
        outcome = outcomes[0]
        attempt_evidence.append(
            f"attempt {attempt}: status={outcome.status!r}, "
            f"exit_code={outcome.exit_code!r}, reason={outcome.reason!r}"
        )

    assert outcomes, (
        "run_plan produced no task results after the bounded external-dependency retry; "
        + "; ".join(attempt_evidence)
    )
    assert outcomes[0].status == "succeeded", (
        "live coding gate task failed after the bounded external-dependency retry:\n"
        + "\n".join(attempt_evidence)
    )
    assert last_attempt is not None
    _, repo, base, session_dir = last_attempt

    # The model actually edited the file and exactly one commit was published.
    changed = _main_changed_files(repo, base)
    assert changed == ["calc.py"], f"publication touched unexpected files: {changed}"
    diff = subprocess.run(
        ["git", "diff", f"{base}..refs/heads/main", "--", "calc.py"],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    added = [line[1:] for line in diff.splitlines() if line.startswith("+")]
    assert any('"""' in line or "'''" in line for line in added), (
        f"published commit did not add a docstring; diff:\n{diff[:1500]}"
    )

    # The session durably recorded provider usage and tool execution.
    events = _session_events(session_dir)
    events_json = json.dumps(events)
    usage_rows = [e for e in events if e.get("kind") == "usage_event"]
    assert usage_rows, "no usage_event rows were recorded"
    assert any(
        isinstance((e.get("payload") or {}).get("usage"), Mapping) and bool(e["payload"]["usage"])
        for e in usage_rows
    ), "usage events carry no token counts"
    assert any(e.get("kind") == "tool_event" for e in events), (
        "no tool events were recorded; the model never called a tool"
    )

    # The credential must never leak into durable artifacts.
    assert key not in events_json, "provider credential leaked into session events"


@pytest.mark.acceptance
def test_live_coding_gate_impossible_task_publishes_nothing(tmp_path: Path) -> None:
    resolved = _pick_provider()
    if resolved is None:
        pytest.skip("no configured provider credential is resolvable for the live gate")
    provider, key = resolved

    result, repo, base, _session_dir = _run_gate(
        tmp_path,
        provider,
        key,
        "In missing_file.py, fix the syntax error. Do not create any files. Do not run git.",
    )
    # Whether the worker reports failed or succeeds conversationally, the
    # fabricated target must never reach publication.
    assert _main_head(repo) == base, "an impossible task advanced main"
