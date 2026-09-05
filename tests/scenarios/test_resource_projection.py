"""Resource facts must survive replay without making rendering perform I/O."""

from __future__ import annotations

from pathlib import Path

import pytest

from cambium.observability import snapshot_from_events
from cambium.provider_scheduler import QuotaLedger, read_quota_snapshots
from cambium.render import render_tokens_per_s
from cambium.routing import ProviderDebt
from cambium.tui_screen import _display_width, _rail_rows, render_quota_rows


def _event(seq: int, provider: str, **payload: object) -> dict:
    return {
        "seq": seq,
        "kind": "usage_event",
        "task_id": provider,
        "payload": {"provider": provider, "latency_s": 2.0, **payload},
    }


@pytest.mark.parametrize(
    ("usage", "expected"),
    [
        ({"total_tokens": 100_000}, None),
        ({"input_tokens": 99_980, "output_tokens": 20, "total_tokens": 100_000}, 10.0),
        ({"prompt_tokens": 99_980, "completion_tokens": 20}, 10.0),
    ],
)
def test_generation_rate_never_counts_prompt_tokens(usage: dict, expected: float | None) -> None:
    event = _event(1, "one", usage=usage)
    debt = ProviderDebt()
    debt.record(event["payload"], now=1)
    assert debt.tokens_per_s_count == (0 if expected is None else 1)
    assert debt.tokens_per_s == (expected or 0)
    assert render_tokens_per_s([event]) == ("" if expected is None else f"tokens/s={expected:.1f}")
    assert snapshot_from_events([event]).agents[0].output_tokens_per_s == expected


def _window(provider: str, used: int) -> dict:
    return {
        "provider": provider,
        "name": "week",
        "reset_at": 1_900_000_000.0,
        "allowance_tokens": 1000,
        "used_tokens": used,
        "allowance_requests": 0,
        "used_requests": 0,
        "reserve_fraction": 0.0,
    }


def test_quota_replay_keeps_latest_window_for_each_provider() -> None:
    events = [
        _event(1, "one", quota_windows=[_window("one", 100)]),
        _event(2, "two", quota_windows=[_window("two", 200)]),
        _event(3, "one", quota_windows=[_window("one", 300)]),
    ]
    snapshot = snapshot_from_events(events)
    rows = "\n".join(render_quota_rows(snapshot, width=80))
    assert "one/week: 700/1000 tokens" in rows
    assert "two/week: 800/1000 tokens" in rows
    assert rows == "\n".join(render_quota_rows(snapshot_from_events(events), width=80))


def test_quota_render_never_opens_a_writable_ledger(tmp_path: Path, monkeypatch) -> None:
    database = tmp_path / "quota.db"
    QuotaLedger(database)
    monkeypatch.setenv("CAMBIUM_QUOTA_DB", str(database))

    def unexpected_open(*args, **kwargs):
        raise AssertionError("rendering opened a writable quota ledger")

    monkeypatch.setattr(QuotaLedger, "__init__", unexpected_open)
    assert render_quota_rows(snapshot_from_events([])) == []


def test_quota_inspection_does_not_create_or_reconfigure_storage(tmp_path: Path) -> None:
    database = tmp_path / "absent" / "quota.db"
    assert read_quota_snapshots(database) == ()
    assert not database.parent.exists()
    ledger = QuotaLedger(database)
    ledger.observe(
        "one", "week", reset_at=1_900_000_000, allowance_tokens=1000, remaining_tokens=700
    )
    database.parent.chmod(0o750)
    assert read_quota_snapshots(database, "one")[0].remaining_tokens == 700
    assert read_quota_snapshots(database, "two") == ()
    assert database.parent.stat().st_mode & 0o777 == 0o750


def test_resource_rail_keeps_throughput_and_quota_visible_in_a_short_terminal() -> None:
    snapshot = snapshot_from_events([
        _event(1, "one", usage={"input_tokens": 100, "output_tokens": 20},
               quota_windows=[_window("one", 300)]),
    ])
    rows = _rail_rows(snapshot, 32, 18, cumulative_line=(
        "usage: calls=4 in=100 out=40 cached=70 out/s=10.0 cost=0"
    ))
    text = "\n".join(line for _, line in rows)
    assert "RESOURCES" in text
    assert "out 40 · 10.0 tok/s" in text
    assert "in 100 · cached 70" in text
    assert "4 calls · est $0" in text
    assert "QUOTA" in text and "700/1000 tokens" in text
    assert len(rows) <= 18
    assert all(_display_width(line) <= 32 for _, line in rows)


def test_completed_rail_does_not_reserve_empty_live_detail_rows() -> None:
    snapshot = snapshot_from_events([
        _event(1, "one", usage={"output_tokens": 20}),
        {"seq": 2, "kind": "result", "task_id": "one", "payload": {"status": "succeeded"}},
    ])
    rows = _rail_rows(snapshot, 32, 24)
    assert all(line.strip() for _, line in rows)
    assert any(line.strip() == "succeeded" for _, line in rows)


def test_renderer_does_not_read_another_threads_native_editor(monkeypatch) -> None:
    from io import StringIO
    from types import SimpleNamespace

    from cambium import tui_screen

    def forbidden_read():
        raise AssertionError("renderer accessed a live native editor")

    monkeypatch.setattr(tui_screen, "_readline", SimpleNamespace(get_line_buffer=forbidden_read))
    cockpit = tui_screen.Cockpit(StringIO(), enabled=True)
    cockpit._native_input = True
    cockpit._input_active = True
    cockpit._input_owner = -1
    assert cockpit._input_line_text() is None
