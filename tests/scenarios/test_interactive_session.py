"""Persistent interactive branch and TUI input scenarios."""

from __future__ import annotations

import asyncio
import io
import json
import sqlite3
import time
from pathlib import Path

import pytest

from cambium import tui
from cambium.interactive import InteractiveSession, InteractiveSessionError
from cambium.monitor import monitor_session
from cambium.oneshot import OneShotConfig, default_session_root
from cambium.store import EventStore
from cambium.supervisor import (
    EventCursor,
    PlanResult,
    TaskResult,
    read_events,
    read_events_with_cursor,
)
from cambium.tui_screen import Transcript


class _Tty(io.StringIO):
    def isatty(self) -> bool:
        return True


def _cache_key(provider: str = "provider-a", model: str = "model-a") -> dict[str, object]:
    digest = "a" * 64
    return {
        "provider": provider,
        "model": model,
        "protocol": "chat_completions",
        "reasoning_effort": None,
        "system_sha256": digest,
        "tools_sha256": digest,
        "prefix_sha256": digest,
        "suffix_sha256": digest,
        "full_sha256": digest,
        "prefix_bytes": 1024,
        "message_count": 3,
        "redacted": False,
        "provider_boundary": {},
    }


def _checkpoint_event(
    ref: str, provider: str = "provider-a", model: str = "model-a"
) -> dict[str, object]:
    return {
        "seq": 1,
        "kind": "context_checkpoint",
        "task_id": "interactive-main",
        "payload": {
            "checkpoint_ref": ref,
            "epoch": 3,
            "cache_key": _cache_key(provider, model),
        },
    }


def _two_provider_config(path: Path, *, first_enabled: bool = True) -> Path:
    path.write_text(
        json.dumps(
            {
                "providers": [
                    {
                        "name": "dead-zen",
                        "tier": "balanced",
                        "base_url": "http://127.0.0.1:9999/v1",
                        "api_key_env": "CAMBIUM_PROVIDER_DEAD_ZEN_API_KEY",
                        "model": "zen-model",
                        "enabled": first_enabled,
                    },
                    {
                        "name": "healthy-codex",
                        "tier": "strong",
                        "base_url": "http://127.0.0.1:9998/v1",
                        "api_key_env": "CAMBIUM_PROVIDER_HEALTHY_CODEX_API_KEY",
                        "model": "codex-model",
                    },
                ]
            }
        ),
        encoding="utf-8",
    )
    return path


def _durable_session(repo: Path, name: str) -> Path:
    root = default_session_root(repo) / name
    session = InteractiveSession(OneShotConfig(repo=repo, session_root=root))
    turn = session.prepare_turn("durable prompt")
    event_db = turn.session_dir / ".cambium" / "events.db"
    event_db.parent.mkdir(parents=True)
    event_db.touch()
    session.complete_turn(turn, succeeded=False)
    return root


def test_interactive_session_carries_exact_and_semantic_seed(tmp_path: Path) -> None:
    root = tmp_path / "interactive"
    session = InteractiveSession(OneShotConfig(repo=tmp_path, session_root=root))
    first = session.prepare_turn("inspect")
    checkpoint_ref = "interactive-main/epoch-3-" + "b" * 64 + ".json"
    checkpoint = first.session_dir / ".cambium" / "checkpoints" / checkpoint_ref
    checkpoint.parent.mkdir(parents=True)
    checkpoint.write_text("{}", encoding="utf-8")

    session.observe_event(first, _checkpoint_event(checkpoint_ref))
    session.complete_turn(first, succeeded=True)
    second = session.prepare_turn("continue")

    copied = second.session_dir / ".cambium" / "checkpoints" / checkpoint_ref
    assert copied.read_text(encoding="utf-8") == "{}"
    assert second.summary_trunk_ref == checkpoint_ref
    assert second.context_fork is not None
    assert second.context_fork["provider"] == "provider-a"
    assert second.config.provider == "provider-a"
    assert second.config.model == "model-a"
    assert second.config.task_id == "interactive-main"

    reloaded = InteractiveSession(OneShotConfig(repo=tmp_path, session_root=root))
    assert reloaded.turn == 1
    assert reloaded.provider == "provider-a"
    assert reloaded.seed is not None
    assert reloaded.seed.checkpoint_ref == checkpoint_ref


def test_interactive_reset_starts_fresh_branch(tmp_path: Path) -> None:
    session = InteractiveSession(
        OneShotConfig(repo=tmp_path, session_root=tmp_path / "interactive")
    )
    session.reset()
    assert session.seed is None
    assert "branch=2" in session.describe()


def test_interactive_fork_reuses_current_checkpoint(tmp_path: Path) -> None:
    root = tmp_path / "interactive"
    session = InteractiveSession(OneShotConfig(repo=tmp_path, session_root=root))
    first = session.prepare_turn("inspect")
    checkpoint_ref = "interactive-main/epoch-3-" + "b" * 64 + ".json"
    checkpoint = first.session_dir / ".cambium" / "checkpoints" / checkpoint_ref
    checkpoint.parent.mkdir(parents=True)
    checkpoint.write_text("{}", encoding="utf-8")
    session.observe_event(first, _checkpoint_event(checkpoint_ref))
    session.complete_turn(first, succeeded=True)

    message = session.fork()

    assert "generation=2" in message
    assert session.seed is not None
    assert session.seed.checkpoint_ref == checkpoint_ref
    assert session.active_turn_dirs() == ()


def test_interactive_branches_replay_event_store_heads(tmp_path: Path) -> None:
    root = tmp_path / "interactive"
    session = InteractiveSession(OneShotConfig(repo=tmp_path, session_root=root))
    event_db = root / "turn-0001" / ".cambium" / "events.db"
    event_db.parent.mkdir(parents=True)
    store = EventStore(event_db)
    try:
        store.append(
            {
                "kind": "context_checkpoint",
                "task_id": "interactive-main",
                "generation": 1,
                "payload": {
                    "checkpoint_ref": "interactive-main/epoch-001-ref.json",
                    "epoch": 1,
                },
            }
        )
        store.append(
            {
                "kind": "context_epoch_advanced",
                "task_id": "interactive-main",
                "generation": 1,
                "payload": {
                    "checkpoint_ref": "interactive-main/epoch-002-ref.json",
                    "epoch": 2,
                },
            }
        )
    finally:
        store.close()

    heads = session.branch_heads()

    assert len(heads) == 1
    assert heads[0].epoch == 2
    assert heads[0].checkpoint_ref.endswith("epoch-002-ref.json")


def test_interactive_read_events_merges_turn_stores(tmp_path: Path) -> None:
    root = tmp_path / "interactive"
    (root / ".cambium").mkdir(parents=True)
    (root / ".cambium" / "events.db").touch()
    for number, kinds in ((1, ("session_started", "usage_event")), (2, ("result",))):
        event_db = root / f"turn-{number:04d}" / ".cambium" / "events.db"
        event_db.parent.mkdir(parents=True)
        store = EventStore(event_db)
        try:
            for kind in kinds:
                store.append(
                    {
                        "kind": kind,
                        "task_id": "interactive-main",
                        "generation": 1,
                        "payload": {"status": "succeeded"} if kind == "result" else {},
                    }
                )
        finally:
            store.close()

    events = read_events(root)

    assert [event["kind"] for event in events] == [
        "session_started",
        "usage_event",
        "result",
    ]
    assert [event["seq"] for event in events] == [1, 2, 3]
    assert [event["kind"] for event in read_events(root, after_seq=2)] == ["result"]


def test_interactive_cursor_delivers_late_events_from_each_store(tmp_path: Path) -> None:
    root = tmp_path / "interactive"
    (root / ".cambium").mkdir(parents=True)
    (root / ".cambium" / "events.db").touch()
    stores: dict[int, EventStore] = {}
    for turn, kind in ((1, "t1"), (2, "t2"), (3, "t3")):
        event_db = root / f"turn-{turn:04d}" / ".cambium" / "events.db"
        event_db.parent.mkdir(parents=True)
        stores[turn] = EventStore(event_db, fsync_interval_s=60.0)
        stores[turn].append({"kind": kind, "payload": {}})
        stores[turn].close()

    first, cursor = read_events_with_cursor(root)
    assert isinstance(cursor, EventCursor)
    assert [event["kind"] for event in first] == ["t1", "t2", "t3"]
    assert [event["seq"] for event in first] == [1, 2, 3]
    assert cursor.watermark == 3

    for turn, kind in ((1, "compact"), (2, "late-t2")):
        event_db = root / f"turn-{turn:04d}" / ".cambium" / "events.db"
        store = EventStore(event_db, fsync_interval_s=60.0)
        try:
            store.append({"kind": kind, "payload": {}})
        finally:
            store.close()

    late, cursor = read_events_with_cursor(root, cursor)
    assert [event["kind"] for event in late] == ["compact", "late-t2"]
    assert [event["seq"] for event in late] == [4, 5]
    assert cursor.watermark == 5
    assert read_events(root, after_seq=cursor) == []


def test_interactive_read_events_skips_symlinked_turn_store(tmp_path: Path) -> None:
    root = tmp_path / "interactive"
    (root / ".cambium").mkdir(parents=True)
    (root / ".cambium" / "events.db").touch()

    external = tmp_path / "external.db"
    store = EventStore(external)
    try:
        store.append({"kind": "outside", "payload": {}})
    finally:
        store.close()
    linked = root / "turn-0001" / ".cambium" / "events.db"
    linked.parent.mkdir(parents=True)
    linked.symlink_to(external)

    available = root / "turn-0002" / ".cambium" / "events.db"
    available.parent.mkdir(parents=True)
    store = EventStore(available)
    try:
        store.append({"kind": "available", "payload": {}})
    finally:
        store.close()

    assert [event["kind"] for event in read_events(root)] == ["available"]


def test_interactive_read_events_skips_locked_turn_until_next_poll(tmp_path: Path) -> None:
    root = tmp_path / "interactive"
    locked_db = root / "turn-0001" / ".cambium" / "events.db"
    available_db = root / "turn-0002" / ".cambium" / "events.db"
    for event_db, kind in ((locked_db, "locked-turn"), (available_db, "available-turn")):
        event_db.parent.mkdir(parents=True)
        store = EventStore(event_db)
        try:
            store.append({"kind": kind, "task_id": "interactive-main", "payload": {}})
        finally:
            store.close()

    # BEGIN EXCLUSIVE does not block readers in WAL mode, so use the legacy
    # rollback journal for this lock scenario.
    with sqlite3.connect(locked_db) as connection:
        connection.execute("PRAGMA journal_mode=DELETE")

    blocker = sqlite3.connect(locked_db, isolation_level=None)
    try:
        blocker.execute("BEGIN EXCLUSIVE")
        started = time.monotonic()
        assert [event["kind"] for event in read_events(root)] == ["available-turn"]
        assert time.monotonic() - started < 1.0
    finally:
        blocker.rollback()
        blocker.close()

    assert [event["kind"] for event in read_events(root)] == [
        "locked-turn",
        "available-turn",
    ]


def test_monitor_smoke_reads_interactive_turn_stores(tmp_path: Path) -> None:
    root = tmp_path / "interactive"
    event_db = root / "turn-0001" / ".cambium" / "events.db"
    event_db.parent.mkdir(parents=True)
    store = EventStore(event_db)
    try:
        store.append(
            {
                "kind": "usage_event",
                "task_id": "interactive-main",
                "generation": 1,
                "payload": {
                    "provider": "provider-a",
                    "model": "model-a",
                    "usage": {"input_tokens": 3, "output_tokens": 2, "total_tokens": 5},
                },
            }
        )
    finally:
        store.close()

    output = io.StringIO()
    assert monitor_session(root, once=True, output_stream=output) == 0
    rendered = output.getvalue()
    assert "usage_event" in rendered
    assert "total=5" in rendered


def test_tui_model_preference_is_validated_and_applies_to_next_turn(tmp_path: Path) -> None:
    provider_config = tmp_path / "providers.json"
    provider_config.write_text(
        json.dumps(
            {
                "providers": [
                    {
                        "name": "provider-a",
                        "tier": "balanced",
                        "base_url": "http://127.0.0.1:9999/v1",
                        "api_key_env": "CAMBIUM_PROVIDER_PROVIDER_A_API_KEY",
                        "model": "model-b",
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    session = InteractiveSession(
        OneShotConfig(
            repo=tmp_path,
            session_root=tmp_path / "interactive",
            provider="provider-a",
            model="model-a",
            provider_config_path=provider_config,
        )
    )

    result = session.set_model_preference("model-b")
    turn = session.prepare_turn("continue")

    assert "preference set" in result
    assert turn.config.provider == "provider-a"
    assert turn.config.model == "model-b"


def test_resume_reselects_healthy_provider_and_reconciles_model(
    monkeypatch, tmp_path: Path
) -> None:
    provider_config = _two_provider_config(tmp_path / "providers.json")
    monkeypatch.setenv("CAMBIUM_PROVIDER_DEAD_ZEN_API_KEY", "offline")
    monkeypatch.setenv("CAMBIUM_PROVIDER_HEALTHY_CODEX_API_KEY", "offline")
    config = OneShotConfig(
        repo=tmp_path,
        session_root=tmp_path / "interactive",
        provider_config_path=provider_config,
    )
    session = InteractiveSession(config)
    first = session.prepare_turn("first")
    checkpoint_ref = "interactive-main/epoch-1-" + "d" * 64 + ".json"
    checkpoint = first.session_dir / ".cambium" / "checkpoints" / checkpoint_ref
    checkpoint.parent.mkdir(parents=True)
    checkpoint.write_text("{}", encoding="utf-8")
    session.observe_event(first, _checkpoint_event(checkpoint_ref, "dead-zen", "zen-model"))
    session.complete_turn(first, succeeded=True)

    provider_config = _two_provider_config(provider_config, first_enabled=False)
    monkeypatch.delenv("CAMBIUM_PROVIDER_DEAD_ZEN_API_KEY")

    resumed = InteractiveSession(config)
    assert resumed.provider == "healthy-codex"
    assert resumed.model == "codex-model"
    manifest = json.loads(
        (config.session_root / ".cambium" / "interactive.json").read_text(encoding="utf-8")
    )
    assert manifest["provider_preference"] == "healthy-codex"
    assert manifest["model_preference"] == "codex-model"

    seen: list[tuple[str | None, str | None]] = []

    async def fake_run(self, turn, *, on_event=None):
        seen.append((turn.config.provider, turn.config.model))
        return PlanResult(
            results=(
                TaskResult(
                    task_id="interactive-main",
                    status="succeeded",
                    exit_code=0,
                    provider="healthy-codex",
                ),
            )
        )

    monkeypatch.setattr(InteractiveSession, "run_turn", fake_run)
    output = _Tty()
    error = io.StringIO()
    code = asyncio.run(
        tui.run_tui(
            config,
            input_stream=_Tty("resume\n/exit\n"),
            output_stream=output,
            error_stream=error,
        )
    )

    assert code == 0
    assert error.getvalue() == ""
    assert seen == [("healthy-codex", "codex-model")]
    persisted = json.loads(
        (config.session_root / ".cambium" / "interactive.json").read_text(encoding="utf-8")
    )
    assert persisted["provider_preference"] == "healthy-codex"
    assert persisted["model_preference"] == "codex-model"


def test_serving_reconciliation_preserves_per_provider_model_choices(
    monkeypatch, tmp_path: Path
) -> None:
    provider_config = _two_provider_config(tmp_path / "providers.json")
    monkeypatch.setenv("CAMBIUM_PROVIDER_DEAD_ZEN_API_KEY", "offline")
    monkeypatch.setenv("CAMBIUM_PROVIDER_HEALTHY_CODEX_API_KEY", "offline")
    session = InteractiveSession(
        OneShotConfig(
            repo=tmp_path,
            session_root=tmp_path / "interactive",
            provider="dead-zen",
            model="zen-model",
            provider_config_path=provider_config,
        )
    )

    assert "preference" in session.set_model_preference("dead-zen:zen-model")
    assert "preference set" in session.set_model_preference("healthy-codex:codex-model")
    turn = session.prepare_turn("fallback")
    session.observe_result(
        turn,
        PlanResult(
            results=(
                TaskResult(
                    task_id="interactive-main",
                    status="succeeded",
                    exit_code=0,
                    provider="dead-zen",
                ),
            )
        ),
    )

    manifest = json.loads(
        (tmp_path / "interactive" / ".cambium" / "interactive.json").read_text(
            encoding="utf-8"
        )
    )
    assert manifest["model_preferences"] == {
        "dead-zen": "zen-model",
        "healthy-codex": "codex-model",
    }
    assert session.set_model_preference("healthy-codex")
    assert session.provider == "healthy-codex"
    assert session.model == "codex-model"


def test_tui_multiline_input() -> None:
    source = io.StringIO("<<<\nline one\nline two\n>>>\n")
    out = io.StringIO()
    assert tui._read_prompt(source, out) == "line one\nline two"
    assert out.getvalue() == "cambium> ... ... ... "


def test_tty_tui_reuses_checkpoint_on_second_prompt(monkeypatch, tmp_path: Path) -> None:
    seen_context_forks: list[dict[str, object] | None] = []

    async def fake_run(self, turn, *, on_event=None):
        seen_context_forks.append(turn.context_fork)
        checkpoint_ref = f"interactive-main/epoch-{turn.number}-" + f"{turn.number:064x}" + ".json"
        checkpoint = turn.session_dir / ".cambium" / "checkpoints" / checkpoint_ref
        checkpoint.parent.mkdir(parents=True, exist_ok=True)
        checkpoint.write_text("{}", encoding="utf-8")
        if on_event is not None:
            on_event(_checkpoint_event(checkpoint_ref))
            on_event(
                {
                    "seq": 2,
                    "kind": "usage_event",
                    "task_id": "interactive-main",
                    "payload": {
                        "provider": "provider-a",
                        "model": "model-a",
                        "turn": turn.number,
                        "latency_s": 1.0,
                        "usage": {
                            "input_tokens": 100,
                            "output_tokens": 25,
                            "total_tokens": 125,
                        },
                    },
                }
            )
        return PlanResult(
            results=(TaskResult(task_id=f"task-{turn.number}", status="succeeded", exit_code=0),)
        )

    monkeypatch.setattr(InteractiveSession, "run_turn", fake_run)
    source = _Tty("one\ntwo\n/usage\n/exit\n")
    output = _Tty()
    error = io.StringIO()

    code = asyncio.run(
        tui.run_tui(
            OneShotConfig(repo=tmp_path, session_root=tmp_path / "interactive"),
            input_stream=source,
            output_stream=output,
            error_stream=error,
        )
    )

    assert code == 0
    assert error.getvalue() == ""
    assert seen_context_forks[0] is None
    assert seen_context_forks[1] is not None
    assert "tokens=250" in output.getvalue()
    rendered = output.getvalue()
    assert "provider-a/model-a" in rendered


def test_reset_excludes_prior_turns_from_restored_branch(tmp_path: Path) -> None:
    session = InteractiveSession(
        OneShotConfig(repo=tmp_path, session_root=tmp_path / "interactive")
    )
    first = session.prepare_turn("one")
    session.complete_turn(first, succeeded=False)
    assert session.active_turn_dirs() == (first.session_dir,)

    session.reset()

    assert session.active_turn_dirs() == ()
    second = session.prepare_turn("two")
    assert second.number == 2


def test_restore_history_folds_completed_current_branch(monkeypatch, tmp_path: Path) -> None:
    session = InteractiveSession(
        OneShotConfig(repo=tmp_path, session_root=tmp_path / "interactive")
    )
    first = session.prepare_turn("one")
    event_db = first.session_dir / ".cambium" / "events.db"
    event_db.parent.mkdir(parents=True, exist_ok=True)
    event_db.touch()
    session.complete_turn(first, succeeded=False)

    monkeypatch.setattr(
        tui,
        "read_events_file",
        lambda _path: [
            {
                "seq": 1,
                "kind": "usage_event",
                "task_id": "interactive-main",
                "payload": {
                    "provider": "provider-a",
                    "model": "model-a",
                    "latency_s": 1.0,
                    "usage": {
                        "input_tokens": 10,
                        "output_tokens": 5,
                        "total_tokens": 15,
                    },
                },
            }
        ],
    )

    cumulative, _snapshot = tui._restore_history(session)

    assert cumulative.calls == 1
    assert cumulative.total_tokens == 15


@pytest.mark.parametrize("columns", [80, 60])
def test_tui_reconnects_to_explicit_durable_interactive_session(
    tmp_path: Path, monkeypatch, columns: int
) -> None:
    monkeypatch.setenv("COLUMNS", str(columns))
    monkeypatch.setenv("LINES", "24")
    root = default_session_root(tmp_path) / "prior"
    session = InteractiveSession(OneShotConfig(repo=tmp_path, session_root=root))
    first = session.prepare_turn("durable prompt")
    checkpoint_ref = "interactive-main/epoch-7-" + "c" * 64 + ".json"
    checkpoint = first.session_dir / ".cambium" / "checkpoints" / checkpoint_ref
    checkpoint.parent.mkdir(parents=True)
    checkpoint.write_text("{}", encoding="utf-8")
    checkpoint_event = _checkpoint_event(checkpoint_ref)
    checkpoint_event.pop("seq")
    checkpoint_payload = checkpoint_event["payload"]
    assert isinstance(checkpoint_payload, dict)
    checkpoint_payload["epoch"] = 7

    event_db = first.session_dir / ".cambium" / "events.db"
    store = EventStore(event_db)
    try:
        store.append(
            {
                "kind": "task_assigned",
                "task_id": "interactive-main",
                "payload": {"task": "durable prompt"},
            }
        )
        store.append(checkpoint_event)
        store.append(
            {
                "kind": "usage_event",
                "task_id": "interactive-main",
                "payload": {
                    "usage": {
                        "input_tokens": 100,
                        "output_tokens": 25,
                        "total_tokens": 125,
                    }
                },
            }
        )
    finally:
        store.close()
    (first.session_dir / ".cambium" / "result.json").write_text(
        json.dumps({"summary": "durable answer"}), encoding="utf-8"
    )
    session.observe_event(first, checkpoint_event)
    session.complete_turn(first, succeeded=True)
    session.acquire()
    session.release()

    reconnected = InteractiveSession(OneShotConfig(repo=tmp_path, session_root=root))
    transcript = Transcript()
    cumulative, snapshot = tui._restore_history(reconnected, transcript=transcript)

    assert reconnected.root == root.resolve()
    assert reconnected.reconnected is True
    assert reconnected.turn == 1
    assert reconnected.last_epoch == 7
    assert reconnected.last_checkpoint == checkpoint_ref
    assert cumulative.calls == 1
    assert cumulative.total_tokens == 125
    assert snapshot.context.checkpoint_ref == checkpoint_ref
    assert [entry.text for entry in transcript.entries if entry.role == "user"] == [
        "durable prompt"
    ]
    assert [entry.text for entry in transcript.entries if entry.role == "assistant"] == [
        "durable answer"
    ]

    source = _Tty("/exit\n")
    output = _Tty()
    error = io.StringIO()
    code = asyncio.run(
        tui.run_tui(
            OneShotConfig(repo=tmp_path, session_root=root),
            input_stream=source,
            output_stream=output,
            error_stream=error,
        )
    )
    rendered = output.getvalue()
    assert code == 0
    assert error.getvalue() == ""
    assert "Detected prior interactive session" in rendered
    assert "last_epoch=7" in rendered
    assert "last_checkpoint=interactive-main/epoch-7-" in rendered
    assert "durable prompt" in rendered
    assert "durable answer" in rendered
    assert "125 tok" in rendered


def test_lock_acquisition_refreshes_state_before_contender_can_publish(
    tmp_path: Path,
) -> None:
    root = default_session_root(tmp_path) / "shared"
    config = OneShotConfig(repo=tmp_path, session_root=root)
    owner = InteractiveSession(config)
    first = owner.prepare_turn("first")
    owner.complete_turn(first, succeeded=False)
    owner.acquire()
    try:
        contender = InteractiveSession(config)
        assert contender.turn == 1

        second = owner.prepare_turn("second")
        owner.complete_turn(second, succeeded=False)
    finally:
        owner.release()

    contender.acquire()
    try:
        assert contender.turn == 2
        third = contender.prepare_turn("third")
        contender.observe_event(
            third,
            {"kind": "usage_event", "payload": {"provider": "provider-a", "model": "model-a"}},
        )
        manifest = json.loads(
            (root / ".cambium" / "interactive.json").read_text(encoding="utf-8")
        )
        assert manifest["turn"] == 2
    finally:
        contender.release()


def test_hostile_manifest_turn_is_rejected_without_sequential_probe(tmp_path: Path) -> None:
    root = default_session_root(tmp_path) / "hostile"
    state = root / ".cambium"
    state.mkdir(parents=True)
    (root / "turn-0001" / ".cambium").mkdir(parents=True)
    (root / "turn-0001" / ".cambium" / "events.db").touch()
    (state / "interactive.json").write_text(
        json.dumps(
            {
                "schema": 1,
                "repo": str(tmp_path.resolve()),
                "turn": 200_000,
                "branch_generation": 1,
                "branch_start_turn": 0,
                "seed": None,
                "provider_preference": None,
                "model_preference": None,
                "model_preferences": {},
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(InteractiveSessionError, match="implausibly ahead"):
        InteractiveSession(OneShotConfig(repo=tmp_path, session_root=root))
    assert InteractiveSession.latest_for_repo(tmp_path) is None


def test_stale_interactive_lock_is_detected_and_reclaimed(tmp_path: Path) -> None:
    session = InteractiveSession(
        OneShotConfig(repo=tmp_path, session_root=tmp_path / "interactive")
    )
    session.lock_path.parent.mkdir(parents=True, exist_ok=True)
    session.lock_path.write_text(
        json.dumps({"pid": 2**31 - 1, "released": False}), encoding="utf-8"
    )

    assert session.lock_status == "stale"
    session.acquire()
    try:
        assert session.recovered_stale_lock is True
        assert session.lock_status == "active"
    finally:
        session.release()
    assert session.lock_status == "available"


def test_empty_repo_start_does_not_claim_a_prior_interactive_session(tmp_path: Path) -> None:
    session = InteractiveSession(OneShotConfig(repo=tmp_path))

    assert session.reconnected is False
    assert session.turn == 0
    assert session.root.parent == default_session_root(tmp_path)


def test_default_interactive_launch_always_allocates_a_fresh_root(tmp_path: Path) -> None:
    prior = _durable_session(tmp_path, "prior")

    fresh = InteractiveSession(OneShotConfig(repo=tmp_path))

    assert fresh.root.parent == default_session_root(tmp_path)
    assert fresh.root != prior.resolve()
    assert fresh.reconnected is False
    assert fresh.turn == 0


def test_continue_resolves_latest_and_specific_interactive_sessions(tmp_path: Path) -> None:
    prior = _durable_session(tmp_path, "prior")

    assert InteractiveSession.resolve_continue_session(tmp_path, None) == prior.resolve()
    assert InteractiveSession.resolve_continue_session(tmp_path, "") == prior.resolve()
    assert InteractiveSession.resolve_continue_session(tmp_path, "prior") == prior.resolve()
    assert InteractiveSession.resolve_continue_session(tmp_path, prior) == prior.resolve()


def test_continue_rejects_paths_outside_repo_sessions_and_symlink_components(
    monkeypatch, tmp_path: Path
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    outside = tmp_path.parent / f"cambium-outside-{tmp_path.name}"
    outside.mkdir()
    _write_manifest = {
        "schema": 1,
        "repo": str(repo.resolve()),
        "turn": 1,
        "branch_generation": 1,
        "branch_start_turn": 0,
        "seed": None,
        "provider_preference": None,
        "model_preference": None,
        "model_preferences": {},
    }
    state = outside / ".cambium"
    state.mkdir()
    (state / "interactive.json").write_text(json.dumps(_write_manifest), encoding="utf-8")
    (outside / "turn-0001" / ".cambium").mkdir(parents=True)
    (outside / "turn-0001" / ".cambium" / "events.db").touch()

    cwd = tmp_path / "cwd"
    cwd.mkdir()
    monkeypatch.chdir(cwd)
    with pytest.raises(InteractiveSessionError, match="must stay under"):
        InteractiveSession.resolve_continue_session(repo, f"../../{outside.name}")

    sessions = default_session_root(repo)
    sessions.mkdir(parents=True)
    (sessions / "escape").symlink_to(outside, target_is_directory=True)
    with pytest.raises(InteractiveSessionError, match="symlinked components"):
        InteractiveSession.resolve_continue_session(repo, sessions / "escape")


def test_continue_missing_interactive_session_fails_clearly(tmp_path: Path) -> None:
    with pytest.raises(
        InteractiveSessionError,
        match="no previous interactive session is available to continue",
    ):
        InteractiveSession.resolve_continue_session(tmp_path, None)

    with pytest.raises(
        InteractiveSessionError,
        match="no resumable interactive session found",
    ):
        InteractiveSession.resolve_continue_session(tmp_path, "missing")
