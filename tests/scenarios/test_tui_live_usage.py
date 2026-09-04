"""Current-turn usage must be visible before completion, without double counting."""

from dataclasses import replace

from cambium.observability import snapshot_from_events
from cambium.tui import _Cumulative


def test_live_usage_includes_current_turn_without_mutating_completed_totals() -> None:
    empty = snapshot_from_events([])
    previous = replace(
        empty,
        calls=1,
        input_tokens=10,
        output_tokens=5,
        total_tokens=15,
        estimated_cost_usd=0.01,
    )
    current = replace(
        empty,
        calls=2,
        input_tokens=30,
        output_tokens=10,
        cached_tokens=8,
        total_tokens=40,
        output_tokens_per_s=12.5,
        estimated_cost_usd=0.02,
    )
    cumulative = _Cumulative()
    cumulative.add(previous)
    first = cumulative.line(snapshot=current, active=True)
    assert "calls=3" in first
    assert "tokens=55" in first
    assert "in=40 out=15 cached=8" in first
    assert "out/s=12.5" in first
    assert "cost=$0.030000" in first
    assert cumulative.line(snapshot=current, active=True) == first
    assert cumulative.calls == 1 and cumulative.total_tokens == 15
    cumulative.add(current)
    completed = cumulative.line(snapshot=current)
    assert "calls=3" in completed and "tokens=55" in completed
    assert "cost=$0.030000" in completed
