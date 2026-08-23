"""Throughput-aware interactive wall-budget resolution tests."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

from cambium.oneshot import OneShotConfig, _interactive_wall_budget_s

DEFAULT_BUDGET_S = 300.0


def _config(**overrides: object) -> OneShotConfig:
    base = OneShotConfig(repo=Path("/tmp"), interactive=True)
    return replace(base, **overrides)  # type: ignore[arg-type]


class _Provider:
    def __init__(
        self,
        name: str,
        *,
        throughput_hint_tps: float = 0.0,
        interactive_wall_budget_s: float | None = None,
    ) -> None:
        self.name = name
        self.throughput_hint_tps = throughput_hint_tps
        self.interactive_wall_budget_s = interactive_wall_budget_s


def test_non_interactive_keeps_default_budget() -> None:
    config = replace(_config(), interactive=False)
    assert _interactive_wall_budget_s(config, ()) == DEFAULT_BUDGET_S


def test_explicit_max_wall_s_wins_over_everything() -> None:
    config = _config(max_wall_s=42.0)
    providers = [_Provider("zen", throughput_hint_tps=1.0)]
    assert _interactive_wall_budget_s(config, providers) == 42.0


def test_explicit_interactive_budget_beats_scaling() -> None:
    config = _config(interactive_wall_budget_s=900.0)
    providers = [_Provider("zen", throughput_hint_tps=1.0)]
    assert _interactive_wall_budget_s(config, providers) == 900.0


def test_slow_provider_hint_scales_budget_past_default() -> None:
    # A slow free-tier provider (10 tok/s) needs 400s of nominal generation
    # for the estimated output; the safety factor (>=2) must push the budget
    # past the historical default so the turn is not killed at five minutes.
    config = _config(provider="zen", max_tokens=2000)
    providers = [_Provider("zen", throughput_hint_tps=10.0)]
    budget = _interactive_wall_budget_s(config, providers)
    assert budget > DEFAULT_BUDGET_S
    assert budget >= 2000 / 10 * 2


def test_fast_provider_falls_back_to_default_floor() -> None:
    # A fast provider's nominal generation fits inside the default; the
    # budget must never drop below it.
    config = _config(provider="fast", max_tokens=500)
    providers = [_Provider("fast", throughput_hint_tps=400.0)]
    assert _interactive_wall_budget_s(config, providers) == DEFAULT_BUDGET_S


def test_cascade_uses_slowest_candidate_hint() -> None:
    config = _config(max_tokens=2000)
    providers = [
        _Provider("fast", throughput_hint_tps=400.0),
        _Provider("slow", throughput_hint_tps=10.0),
    ]
    budget = _interactive_wall_budget_s(config, providers)
    # Slowest hint governs: 2000/10*2 = 400s.
    assert budget == 400.0


def test_selected_provider_hint_beats_unrelated_candidates() -> None:
    config = _config(provider="chosen", max_tokens=2000)
    providers = [
        _Provider("other", throughput_hint_tps=5.0),
        _Provider("chosen", throughput_hint_tps=50.0),
    ]
    budget = _interactive_wall_budget_s(config, providers)
    # The selected provider is fast, so the floor applies despite the slow
    # sibling candidate.
    assert budget == DEFAULT_BUDGET_S
