"""Vertical-slice end-to-end scenario: real supervisor + real worker process.

Drives the real spawn path (asyncio subprocess + pipes + git) through
the public ``cambium.supervisor.run_session`` adapter (a one-task
``run_plan``), with the fake worker script as the worker process. No
mocks, no network.

S01-aligned happy path: ready -> run_task -> result_envelope
(status=succeeded) -> exit_message, the canonical sequencer publishes
the edit onto ``refs/heads/main``, supervisor exits 0. There is no
pre-merge gate; the worker verdict alone decides merge eligibility.
"""

from __future__ import annotations

import asyncio
import json
import subprocess
from pathlib import Path

import cambium.supervisor as supervisor_module
from cambium.results import ROOT_RESULT_KEYS
from cambium.supervisor import read_events, run_session

WORKER = str(Path(__file__).resolve().parents[2] / "scripts" / "fake_worker.py")
MARKER = "// cambium-slice"


def _make_scratch(repo: Path) -> str:
    subprocess.run(["git", "init", "-b", "main", str(repo)], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.name", "slice-test"], check=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.email", "slice@test"], check=True)
    subprocess.run(["git", "-C", str(repo), "config", "gc.auto", "0"], check=True)
    (repo / "hello.txt").write_text("hello from the vertical slice\n")
    subprocess.run(["git", "-C", str(repo), "add", "hello.txt"], check=True)
    subprocess.run(
        ["git", "-C", str(repo), "commit", "-m", "initial"],
        check=True,
        capture_output=True,
    )
    return subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "HEAD"], check=True, capture_output=True, text=True
    ).stdout.strip()


def _spec(session_dir: Path, *, write_marker: bool) -> dict:
    return {
        "task_id": "slice-001",
        "worker": WORKER,
        "scratch_repo": str(session_dir / "scratch"),
        "worktree_path": str(session_dir / "wt"),
        "branch": "wt-slice-001",
        "target_file": "hello.txt",
        "marker": MARKER,
        "write_marker": write_marker,
        "spec": "append the cambium-slice marker line to the target file",
        "provider_env_keys": ["FAKE_MODE"],
    }


def _protocol_sequence(events: list[dict]) -> list[str]:
    kinds = {"init", "ready", "run_task", "result", "exit"}
    return [e["kind"] for e in events if e["kind"] in kinds]


def _show_main(repo: Path, path: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), "show", f"refs/heads/main:{path}"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout


def _assert_no_events_jsonl(session_dir: Path) -> None:
    assert not (session_dir / ".cambium" / "events.jsonl").exists()


def test_vertical_slice_happy_path(tmp_path) -> None:
    session_dir = tmp_path / "session"
    scratch = session_dir / "scratch"
    _make_scratch(scratch)
    spec = _spec(session_dir, write_marker=True)

    result = asyncio.run(run_session(session_dir, spec))

    assert result.status == "succeeded"
    assert result.exit_code == 0
    assert result.gate_exit_code == 0
    assert result.merge_sha is not None
    assert _show_main(scratch, "hello.txt") == (
        "hello from the vertical slice\n"
        "// cambium-slice\n"
    )
    _assert_no_events_jsonl(session_dir)
    assert _protocol_sequence(read_events(session_dir)) == [
        "init", "ready", "run_task", "result", "exit",
    ]


def test_worker_nonzero_exit_fails(tmp_path, monkeypatch) -> None:
    # Reviewer case worker_exit5.py: envelope says succeeded, exit_message present,
    # but the worker process exits 5. Must FAIL with the canonical exit code 1.
    monkeypatch.setenv("FAKE_MODE", "exit5")
    session_dir = tmp_path / "session"
    scratch = session_dir / "scratch"
    base = _make_scratch(scratch)
    spec = _spec(session_dir, write_marker=True)

    result = asyncio.run(run_session(session_dir, spec))

    assert result.status == "failed"
    assert result.exit_code == 1  # canonical supervisor verdict
    assert result.worker_exit_code is None  # not retained by the canonical runtime
    assert result.merge_sha is None
    tip = subprocess.run(
        ["git", "-C", str(scratch), "rev-parse", "main"], check=True, capture_output=True, text=True
    ).stdout.strip()
    assert tip == base  # no merge


def test_missing_exit_message_fails(tmp_path, monkeypatch) -> None:
    # Reviewer case worker_noexit.py: envelope succeeded, exit_message omitted.
    monkeypatch.setenv("FAKE_MODE", "noexit")
    monkeypatch.setattr(supervisor_module, "EOF_GRACE_S", 0.05)
    session_dir = tmp_path / "session"
    scratch = session_dir / "scratch"
    base = _make_scratch(scratch)
    spec = _spec(session_dir, write_marker=True)

    result = asyncio.run(run_session(session_dir, spec))

    assert result.status == "failed"
    assert result.exit_code == 1
    assert result.merge_sha is None
    tip = subprocess.run(
        ["git", "-C", str(scratch), "rev-parse", "main"], check=True, capture_output=True, text=True
    ).stdout.strip()
    assert tip == base


def test_missing_result_envelope_fails(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("FAKE_MODE", "noresult")
    session_dir = tmp_path / "session"
    scratch = session_dir / "scratch"
    _make_scratch(scratch)
    spec = _spec(session_dir, write_marker=True)

    result = asyncio.run(run_session(session_dir, spec))

    assert result.status == "failed"
    assert result.exit_code == 1
    assert result.merge_sha is None


def test_misrouted_result_envelope_fails(tmp_path, monkeypatch) -> None:
    # Undeliverable result: the envelope does not echo run_task's request_id.
    monkeypatch.setenv("FAKE_MODE", "badrid")
    session_dir = tmp_path / "session"
    scratch = session_dir / "scratch"
    base = _make_scratch(scratch)
    spec = _spec(session_dir, write_marker=True)

    result = asyncio.run(run_session(session_dir, spec))

    assert result.status == "failed"
    assert result.exit_code == 1
    assert result.merge_sha is None
    tip = subprocess.run(
        ["git", "-C", str(scratch), "rev-parse", "main"], check=True, capture_output=True, text=True
    ).stdout.strip()
    assert tip == base
    assert any(e["kind"] == "protocol" for e in read_events(session_dir))


def test_result_envelope_echoes_run_task_request_id(tmp_path) -> None:
    session_dir = tmp_path / "session"
    scratch = session_dir / "scratch"
    _make_scratch(scratch)
    spec = _spec(session_dir, write_marker=True)

    result = asyncio.run(run_session(session_dir, spec))

    assert result.status == "succeeded"
    events = read_events(session_dir)
    by_kind = {e["kind"]: e for e in events}
    init_rid = by_kind["init"]["request_id"]
    run_rid = by_kind["run_task"]["request_id"]
    assert by_kind["ready"]["request_id"] == init_rid  # ready echoes init
    assert by_kind["result"]["request_id"] == run_rid  # envelope echoes run_task
    assert by_kind["exit"]["request_id"] is None  # exit_message carries no request_id


def test_ready_timeout_fails_within_budget(tmp_path, monkeypatch) -> None:
    # A worker that never sends ready must be killed within the (env-configured) budget.
    monkeypatch.setenv("FAKE_MODE", "noready")
    monkeypatch.setenv("CAMBIUM_READY_TIMEOUT_S", "0.5")
    session_dir = tmp_path / "session"
    scratch = session_dir / "scratch"
    _make_scratch(scratch)
    spec = _spec(session_dir, write_marker=True)

    result = asyncio.run(run_session(session_dir, spec))

    assert result.status == "failed"
    assert result.exit_code == 1  # canonical exit code (timeout is a failed verdict)
    assert result.timed_out is True
    assert result.timeout_phase == "ready"
    assert result.merge_sha is None
    events = read_events(session_dir)
    assert any(
        e["kind"] == "timeout" and e["payload"].get("phase") == "ready" for e in events
    )


def test_result_json_has_exact_root_keys_and_success_verdict(tmp_path) -> None:
    session_dir = tmp_path / "session"
    scratch = session_dir / "scratch"
    _make_scratch(scratch)
    spec = _spec(session_dir, write_marker=True)

    result = asyncio.run(run_session(session_dir, spec))

    assert result.status == "succeeded"
    record = json.loads((session_dir / ".cambium" / "result.json").read_text())
    assert set(record) == set(ROOT_RESULT_KEYS)
    assert record["status"] == "done"
    assert record["exit_code"] == 0
    assert record["commits"] and record["files_changed"] == ["hello.txt"]
    assert record["unified_diff"]
    assert record["parent_task_id"] is None
    assert record["session_id"] == str(session_dir.resolve())


def test_spawned_worker_env_has_only_authorized_provider_keys(tmp_path, monkeypatch) -> None:
    """The actual spawned worker process sees the authorized provider key and
    no generic credential-shaped variable (regression: the spawn used to pass
    the env through a stripper that removed the explicitly authorized key)."""
    monkeypatch.setenv("CAMBIUM_PROVIDER_OPENAI_API_KEY", "authorized-secret")
    monkeypatch.setenv("CAMBIUM_PROVIDER_ANTHROPIC_API_KEY", "undeclared-secret")
    monkeypatch.setenv("OPENAI_API_KEY", "generic-secret")
    monkeypatch.setenv("CAMBIUM_PROVIDER_bad_API_KEY", "noncanonical-secret")
    dump_path = tmp_path / "worker-env.json"
    monkeypatch.setenv("ENV_DUMP_PATH", str(dump_path))

    env_worker = str(
        Path(__file__).resolve().parents[2] / "tests" / "fixtures" / "env_worker.py"
    )
    session_dir = tmp_path / "session"
    scratch = session_dir / "scratch"
    _make_scratch(scratch)
    spec = _spec(session_dir, write_marker=True)
    spec["worker"] = env_worker
    spec["provider_env_keys"] = ["CAMBIUM_PROVIDER_OPENAI_API_KEY", "ENV_DUMP_PATH"]

    result = asyncio.run(run_session(session_dir, spec))

    assert result.status == "succeeded"
    assert result.exit_code == 0
    spawned_env = json.loads(dump_path.read_text(encoding="utf-8"))
    assert spawned_env["CAMBIUM_PROVIDER_OPENAI_API_KEY"] == "authorized-secret"
    assert "CAMBIUM_PROVIDER_ANTHROPIC_API_KEY" not in spawned_env
    assert "OPENAI_API_KEY" not in spawned_env
    assert "CAMBIUM_PROVIDER_bad_API_KEY" not in spawned_env
    assert spawned_env["CAMBIUM_TASK_ID"] == "slice-001"
