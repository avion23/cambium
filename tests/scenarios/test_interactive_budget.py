"""Throughput-aware wall-budget scenarios for interactive turns."""

from __future__ import annotations

import asyncio
import subprocess
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from cambium import oneshot
from cambium.oneshot import OneShotConfig, _interactive_wall_budget_s
from cambium.supervisor import PlanResult, TaskResult

INTERACTIVE_FLOOR_S = oneshot.DEFAULT_INTERACTIVE_WALL_BUDGET_S


def _provider(
    name: str = "slow",
    *,
    throughput_hint_tps: float = 20.0,
    interactive_wall_budget_s: float | None = None,
) -> SimpleNamespace:
    return SimpleNamespace(
        name=name,
        model="slow-model",
        throughput_hint_tps=throughput_hint_tps,
        interactive_wall_budget_s=interactive_wall_budget_s,
    )


def _config(**overrides: object) -> OneShotConfig:
    base = OneShotConfig(repo=Path("/tmp"), interactive=True)
    return replace(base, **overrides)  # type: ignore[arg-type]


def test_non_interactive_default_remains_five_minutes() -> None:
    config = oneshot.OneShotConfig(prompt="check")

    assert oneshot._interactive_wall_budget_s(config) == oneshot.DEFAULT_WALL_BUDGET_S


def test_interactive_plan_uses_resilient_defaults(tmp_path: Path) -> None:
    plan = oneshot.build_plan(
        oneshot.OneShotConfig(prompt="check", repo=tmp_path, interactive=True),
        repo=tmp_path,
        session_dir=tmp_path / "session",
    )
    spec = plan["tasks"][0]

    assert spec["max_wall_s"] >= oneshot.DEFAULT_INTERACTIVE_WALL_BUDGET_S
    assert spec["max_restarts"] == 1


def test_interactive_plan_preserves_explicit_zero_restarts(tmp_path: Path) -> None:
    plan = oneshot.build_plan(
        oneshot.OneShotConfig(
            prompt="check",
            repo=tmp_path,
            interactive=True,
            max_restarts=0,
        ),
        repo=tmp_path,
        session_dir=tmp_path / "session",
    )

    assert plan["tasks"][0]["max_restarts"] == 0


def test_interactive_slow_provider_scales_from_static_hint() -> None:
    config = oneshot.OneShotConfig(
        prompt="check",
        provider="slow",
        model="slow-model",
        interactive=True,
    )

    budget = oneshot._interactive_wall_budget_s(config, [_provider(throughput_hint_tps=20.0)])

    assert budget == pytest.approx(INTERACTIVE_FLOOR_S)


def test_observed_rate_replaces_fast_static_hint(tmp_path: Path, monkeypatch) -> None:
    root = tmp_path / "interactive"
    prior = root / "turn-0001"
    current = root / "turn-0002"
    prior.mkdir(parents=True)
    current.mkdir()
    events = [
        {
            "kind": "usage_event",
            "payload": {
                "provider": "slow",
                "model": "slow-model",
                "latency_s": 200.0,
                "usage": {"output_tokens": 1_000},
            },
        }
    ]
    monkeypatch.setattr(oneshot.supervisor, "read_events", lambda _path: events)
    config = oneshot.OneShotConfig(
        prompt="check",
        provider="slow",
        model="slow-model",
        interactive=True,
    )

    budget = oneshot._interactive_wall_budget_s(
        config,
        [_provider(throughput_hint_tps=100.0)],
        session_dir=current,
    )

    # 1,000 / 200 = 5 output tokens/s; 12,000 / 5 * 2 = 4,800 seconds.
    assert budget == pytest.approx(4_800.0)


def test_explicit_interactive_budget_wins_over_scaling() -> None:
    config = oneshot.OneShotConfig(
        prompt="check",
        provider="slow",
        interactive=True,
        interactive_wall_budget_s=777.0,
    )

    assert oneshot._interactive_wall_budget_s(config, [_provider(throughput_hint_tps=1.0)]) == 777.0


def test_explicit_max_wall_s_wins_over_everything() -> None:
    config = _config(max_wall_s=42.0)
    providers = [_provider("zen", throughput_hint_tps=1.0)]
    assert _interactive_wall_budget_s(config, providers) == 42.0


def test_fast_provider_falls_back_to_default_floor() -> None:
    # A fast provider's nominal generation fits inside the interactive floor;
    # the budget must never drop below it.
    config = _config(provider="fast", max_tokens=500)
    providers = [_provider("fast", throughput_hint_tps=400.0)]
    assert _interactive_wall_budget_s(config, providers) == INTERACTIVE_FLOOR_S


def test_cascade_uses_slowest_candidate_hint() -> None:
    config = _config(max_tokens=2000)
    providers = [
        _provider("fast", throughput_hint_tps=400.0),
        _provider("slow", throughput_hint_tps=10.0),
    ]
    budget = _interactive_wall_budget_s(config, providers)
    # Slowest hint yields 400s, below the interactive floor.
    assert budget == INTERACTIVE_FLOOR_S


def test_selected_provider_hint_beats_unrelated_candidates() -> None:
    config = _config(provider="chosen", max_tokens=2000)
    providers = [
        _provider("other", throughput_hint_tps=5.0),
        _provider("chosen", throughput_hint_tps=50.0),
    ]
    budget = _interactive_wall_budget_s(config, providers)
    # The selected provider is fast, so the interactive floor applies despite
    # the slow sibling candidate.
    assert budget == INTERACTIVE_FLOOR_S


def test_interactive_run_plan_receives_computed_wall_budget(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-b", "main", str(repo)], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.name", "budget-test"], check=True)
    subprocess.run(
        ["git", "-C", str(repo), "config", "user.email", "budget@test.invalid"], check=True
    )
    (repo / "README").write_text("budget test\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(repo), "add", "README"], check=True, capture_output=True)
    subprocess.run(
        ["git", "-C", str(repo), "commit", "-m", "initial"], check=True, capture_output=True
    )
    provider_path = tmp_path / "providers.json"
    provider_path.write_text(
        '{"providers":[{"name":"slow","tier":"fast",'
        '"base_url":"https://api.example.test/v1",'
        '"api_key_env":"CAMBIUM_PROVIDER_SLOW_API_KEY",'
        '"model":"slow-model","throughput_hint_tps":10}]}',
        encoding="utf-8",
    )
    monkeypatch.setenv("CAMBIUM_PROVIDER_SLOW_API_KEY", "test-secret-not-emitted")
    captured: dict[str, object] = {}

    async def fake_run_plan(session_dir, plan, **_kwargs):
        captured["session_dir"] = Path(session_dir)
        captured["plan"] = plan
        return PlanResult(results=(TaskResult(task_id="oneshot", status="succeeded", exit_code=0),))

    monkeypatch.setattr(oneshot.supervisor, "run_plan", fake_run_plan)
    config = oneshot.OneShotConfig(
        prompt="check",
        repo=repo,
        session_root=tmp_path / "session",
        provider="slow",
        provider_config_path=provider_path,
        interactive=True,
    )

    asyncio.run(oneshot.run_oneshot(config))

    plan = captured["plan"]
    assert isinstance(plan, dict)
    assert plan["tasks"][0]["max_wall_s"] == pytest.approx(2_400.0)


def test_provider_config_budget_override_is_loaded(tmp_path: Path) -> None:
    import json

    path = tmp_path / "providers.json"
    path.write_text(
        json.dumps(
            {
                "providers": [
                    {
                        "name": "slow",
                        "tier": "strong",
                        "base_url": "https://api.example.test/v1",
                        "api_key_env": "CAMBIUM_PROVIDER_SLOW_API_KEY",
                        "model": "slow-model",
                        "interactive_wall_budget_s": 1_111,
                        "throughput_hint_tps": 1,
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    provider = oneshot.load_providers(path)[0]

    assert provider.interactive_wall_budget_s == 1_111.0
    assert provider.throughput_hint_tps == 1.0
    assert (
        oneshot._interactive_wall_budget_s(
            OneShotConfig(prompt="check", provider="slow", interactive=True),
            [provider],
        )
        == 1_111.0
    )
