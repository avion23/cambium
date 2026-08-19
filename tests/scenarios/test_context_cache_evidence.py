from __future__ import annotations

from pathlib import Path
from typing import Any

from scripts import context_cache_evidence as evidence


def _event(
    task_id: str,
    payload: dict[str, Any],
    *,
    seq: int,
) -> dict[str, Any]:
    return {
        "seq": seq,
        "kind": "usage_event",
        "task_id": task_id,
        "payload": payload,
    }


def _usage(
    provider: str,
    *,
    hit: bool | None = None,
    prefix: int | None = 100,
    epoch: int | None = None,
    fork_of: str | None = None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "provider": provider,
        "model": "test-model",
        "turn": 1,
    }
    if hit is not None:
        payload["provider_cache_hit"] = hit
    if prefix is not None:
        payload["prompt_prefix_bytes"] = prefix
    if epoch is not None:
        payload["epoch"] = epoch
    if fork_of is not None:
        payload["fork_of"] = fork_of
    return payload


def test_aggregate_classifies_parent_fork_and_resume_calls(
    tmp_path: Path, monkeypatch
) -> None:
    session = tmp_path / "session"
    session.mkdir()
    events = [
        _event("parent", _usage("chat", hit=False), seq=1),
        _event("parent", _usage("chat", hit=True), seq=2),
        _event("child", _usage("chat", hit=True, epoch=1, fork_of="p/epoch"), seq=3),
        _event("child", _usage("chat", hit=False, epoch=1, fork_of="p/epoch"), seq=4),
        _event("parent", _usage("chat", hit=True, epoch=1), seq=5),
        _event("parent", _usage("chat", hit=False, epoch=1), seq=6),
        _event("other", _usage("other", hit=True, prefix=200), seq=7),
    ]
    monkeypatch.setattr(evidence, "_session_usage_events", lambda _: events)

    providers, sessions, warnings = evidence._aggregate([session])
    assert not warnings
    assert sessions[0]["usable_events"] == 7

    chat = providers["chat"]
    assert chat["baseline"].calls == 2
    assert chat["fork_first"].calls == 1
    assert chat["fork_later"].calls == 1
    assert chat["resume_first"].calls == 1
    assert chat["resume_later"].calls == 1
    assert evidence._cache_rate(chat["baseline"]) == 0.5
    assert evidence._cache_rate(chat["fork_first"]) == 1.0
    assert evidence._cache_rate(chat["resume_first"]) == 1.0

    report = evidence._report(providers, sessions, warnings, 0.8)
    comparison = report["providers"]["chat"]["comparison"]
    assert comparison["fork_first"]["relative_to_baseline"] == 2.0
    assert comparison["fork_first"]["meets_threshold"] is True
    assert comparison["resume_first"]["meets_threshold"] is True
    assert report["measurement"]["cache_policy_changed"] is False


def test_missing_cache_fields_are_not_counted_as_misses(monkeypatch, tmp_path: Path) -> None:
    session = tmp_path / "session"
    session.mkdir()
    events = [
        _event("parent", _usage("chat", hit=None), seq=1),
        _event("child", _usage("chat", hit=None, epoch=2, fork_of="ref"), seq=2),
    ]
    monkeypatch.setattr(evidence, "_session_usage_events", lambda _: events)

    providers, _, _ = evidence._aggregate([session])
    buckets = providers["chat"]
    assert buckets["baseline"].calls == 1
    assert buckets["baseline"].cache_known == 0
    assert evidence._cache_rate(buckets["baseline"]) is None
    report = evidence._report(providers, [], [], 0.8)
    comparison = report["providers"]["chat"]["comparison"]
    assert comparison["baseline_cache_hit_rate"] is None
    assert comparison["fork_first"]["meets_threshold"] is None


def test_missing_db_is_reported_without_reading_or_creating_it(
    tmp_path: Path, monkeypatch
) -> None:
    missing = tmp_path / "missing"
    missing.mkdir()
    calls: list[Path] = []

    def unexpected_read(path: Path) -> list[dict[str, Any]]:
        calls.append(path)
        return []

    monkeypatch.setattr(evidence, "read_events", unexpected_read)
    providers, sessions, warnings = evidence._aggregate([missing])

    assert providers == {}
    assert calls == []
    assert sessions == [{
        "dir": str(missing), "usage_events": 0, "usable_events": 0,
        "skipped": True, "missing_db": True,
    }]
    assert warnings == [f"{missing}: no event DB; skipped"]
