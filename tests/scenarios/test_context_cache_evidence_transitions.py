from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from scripts import context_cache_evidence as evidence


def _event(
    kind: str,
    payload: dict[str, Any],
    *,
    seq: int,
    task_id: str = "task",
    generation: int = 1,
) -> dict[str, Any]:
    return {
        "seq": seq,
        "kind": kind,
        "task_id": task_id,
        "generation": generation,
        "payload": payload,
    }


def _usage(
    *,
    turn: int,
    prefix: int,
    prompt: int,
    cached: int | None,
) -> dict[str, Any]:
    usage: dict[str, Any] = {
        "prompt_tokens": prompt,
        "completion_tokens": 10,
        "total_tokens": prompt + 10,
    }
    if cached is not None:
        usage["cached_tokens"] = cached
    return {
        "turn": turn,
        "prompt_prefix_bytes": prefix,
        "latency_s": 1.25,
        "usage": usage,
    }


def _boundary(
    *,
    turn: int,
    epoch: int,
    prefix: int,
    prefix_hash: str | None,
    folded_from_epoch: int | None = None,
) -> dict[str, Any]:
    cache_key: dict[str, Any] = {"prefix_bytes": prefix}
    if prefix_hash is not None:
        cache_key["prefix_sha256"] = prefix_hash
    payload: dict[str, Any] = {
        "turn": turn,
        "epoch": epoch,
        "cache_key": cache_key,
    }
    if folded_from_epoch is not None:
        payload["folded_from_epoch"] = folded_from_epoch
    return payload


def test_epoch_transitions_pair_nearest_usage_and_boundary_cache_metadata() -> None:
    events = [
        _event(
            "usage_event",
            _usage(turn=1, prefix=100, prompt=100, cached=0),
            seq=1,
        ),
        _event(
            "context_checkpoint",
            _boundary(
                turn=1,
                epoch=1,
                prefix=100,
                prefix_hash="a" * 64,
            ),
            seq=2,
        ),
        _event(
            "usage_event",
            _usage(turn=2, prefix=100, prompt=120, cached=60),
            seq=3,
        ),
        # A different task must not become the nearest usage evidence.
        _event(
            "usage_event",
            _usage(turn=1, prefix=999, prompt=999, cached=999),
            seq=4,
            task_id="child",
        ),
        _event(
            "context_epoch_advanced",
            _boundary(
                turn=3,
                epoch=2,
                prefix=140,
                prefix_hash="b" * 64,
                folded_from_epoch=1,
            ),
            seq=5,
        ),
        _event(
            "usage_event",
            _usage(turn=3, prefix=140, prompt=160, cached=80),
            seq=6,
        ),
    ]

    transitions = evidence._epoch_transitions(events, "/session")

    assert [item["kind"] for item in transitions] == [
        "context_checkpoint",
        "context_epoch_advanced",
    ]
    checkpoint, advanced = transitions
    assert checkpoint["from_epoch"] == 0
    assert checkpoint["to_epoch"] == checkpoint["epoch"] == 1
    assert checkpoint["folded_from_epoch"] is None
    assert checkpoint["turn"] == 1
    assert checkpoint["prefix_bytes"] == 100
    assert checkpoint["prefix_sha256"] == "a" * 64
    assert checkpoint["prefix_bytes_before"] == 100
    assert checkpoint["prefix_bytes_after"] == 100
    assert checkpoint["usage_before"]["cached_tokens"] == 0
    assert checkpoint["usage_before"]["uncached_tokens"] == 100
    assert checkpoint["usage_after"]["cached_tokens"] == 60
    assert checkpoint["usage_after"]["uncached_tokens"] == 60

    assert advanced["from_epoch"] == 1
    assert advanced["to_epoch"] == advanced["epoch"] == 2
    assert advanced["folded_from_epoch"] == 1
    assert advanced["turn"] == 3
    assert advanced["prefix_bytes"] == 140
    assert advanced["prefix_sha256"] == "b" * 64
    assert advanced["usage_before"]["seq"] == 3
    assert advanced["usage_after"]["seq"] == 6
    assert advanced["usage_after"]["cached_tokens"] == 80
    assert advanced["usage_after"]["uncached_tokens"] == 80
    assert checkpoint["boundary_metadata_missing"] == []
    assert advanced["boundary_metadata_missing"] == []
    assert evidence._epoch_transitions(list(reversed(events)), "/session") == transitions


def test_aggregate_reports_sessions_with_missing_boundary_metadata(
    tmp_path: Path, monkeypatch
) -> None:
    session = tmp_path / "session"
    session.mkdir()
    events = [
        _event(
            "context_checkpoint",
            _boundary(
                turn=1,
                epoch=1,
                prefix=123,
                prefix_hash=None,
            ),
            seq=1,
        )
    ]
    monkeypatch.setattr(evidence, "_session_events", lambda _: events)

    providers, sessions, warnings = evidence._aggregate([session])
    report = evidence._report(providers, sessions, warnings, 0.8)

    assert len(report["epoch_transitions"]) == 1
    transition = report["epoch_transitions"][0]
    assert transition["prefix_bytes"] == 123
    assert transition["prefix_sha256"] is None
    assert transition["boundary_metadata_missing"] == ["cache_key.prefix_sha256"]
    assert report["sessions_lacking_boundary_metadata"] == [str(session)]
    assert sessions[0]["boundary_metadata_missing"] == ["seq=1: cache_key.prefix_sha256"]
    assert warnings == [f"{session}: boundary metadata missing: seq=1: cache_key.prefix_sha256"]
    assert report["measurement"]["epoch_transition"] == {
        "boundary_kinds": ["context_checkpoint", "context_epoch_advanced"],
        "prefix_bytes_source": "payload.cache_key.prefix_bytes",
        "prefix_sha256_source": "payload.cache_key.prefix_sha256",
        "usage_source": "usage_event.payload.usage",
    }

    # The machine output is repeatable and all object keys have a stable order.
    encoded = json.dumps(report, sort_keys=True)
    assert encoded == json.dumps(json.loads(encoded), sort_keys=True)
