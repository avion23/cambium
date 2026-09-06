"""Prompt deployment and real-rollout optimizer behavior."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from cambium import prompts, worker
from cambium.benchmark import ExperimentBudget, ExperimentBudgetExceeded, _peak_pending_children


def _dataset(tmp_path: Path) -> Path:
    path = tmp_path / "cases.jsonl"
    path.write_text(
        "\n".join(
            json.dumps({"id": split, "split": split, "task": split, "check": ["unused"]})
            for split in ("train", "val", "test")
        )
        + "\n"
    )
    return path


def _args(tmp_path: Path, **changes):
    values = dict(
        dataset=_dataset(tmp_path),
        output=tmp_path / "experiment",
        component="coding",
        optimizer="gepa",
        max_evals=8,
        max_calls=30,
        max_tokens=100,
        max_turns=8,
        max_wall_s=10,
        max_workers=2,
        budget_usd=1,
        provider=None,
        reflection_provider=None,
        tier="fast",
        case=[],
        dry_run=False,
        no_deploy=False,
        seed=0,
    )
    values.update(changes)
    return SimpleNamespace(**values)


def test_replacement_changes_new_prompts_not_existing_session_snapshot(
    tmp_path, monkeypatch
) -> None:
    path = tmp_path / "prompts.json"
    monkeypatch.setenv("CAMBIUM_PROMPTS", str(path))
    first = {"coding": "Inspect then make the smallest change.", "summary": "Keep open work."}
    prompts.save_policy(first)
    pinned = prompts.load_policy()
    before = worker._build_agent_prompt("task", [], [], prompt_policy=pinned)

    second = {**first, "coding": "Locate, edit, verify."}
    prompts.save_policy(second)
    after = worker._build_agent_prompt("task", [], [], prompt_policy=prompts.load_policy())

    assert first["coding"] in before["messages"][0]["content"]
    assert second["coding"] in after["messages"][0]["content"]
    assert before == worker._build_agent_prompt("task", [], [], prompt_policy=pinned)


def test_experiment_budget_counts_tokens_and_rejects_non_finite_usage() -> None:
    budget = ExperimentBudget(2, 100, 1.0)
    budget.record({"prompt_tokens": 90, "completion_tokens": 10}, 0.0)
    with pytest.raises(ExperimentBudgetExceeded):
        budget.check()
    with pytest.raises(ValueError, match="finite"):
        ExperimentBudget(2, 100, 1.0).record({"total_tokens": float("inf")})
    with pytest.raises(ValueError, match="finite"):
        ExperimentBudget(2, 100, 1.0).record({}, float("nan"))


def test_gepa_search_reserves_measured_budget_for_final_evaluation() -> None:
    from cambium.prompt_optimize import _effective_search_evals

    budget = ExperimentBudget(600, 2_000_000, 50.0, calls=16, tokens=119_911)
    baseline = [
        {"calls": 10, "tokens": 97_740, "cost_usd": 0.0},
        {"calls": 6, "tokens": 22_171, "cost_usd": 0.0},
    ]
    assert _effective_search_evals(24, budget, baseline, evaluation_cases=5) == 10


def test_parallel_benchmark_counts_overlapping_siblings_not_serial_or_nested_work() -> None:
    admitted = [
        {
            "kind": "child_admitted",
            "task_id": "parent",
            "payload": {"parent_task_id": "parent", "child_task_id": child},
        }
        for child in ("csv", "config")
    ]
    finished = [
        {
            "kind": "child_result",
            "task_id": child,
            "payload": {"parent_task_id": "parent", "status": "succeeded"},
        }
        for child in ("csv", "config")
    ]
    assert _peak_pending_children([admitted[0], finished[0], admitted[1], finished[1]]) == 1
    assert _peak_pending_children([*admitted, *finished]) == 2
    nested = {**admitted[1], "payload": {"parent_task_id": "csv", "child_task_id": "config"}}
    assert _peak_pending_children([admitted[0], nested]) == 1


def test_rollout_timeout_reports_failure_against_accepted_code(tmp_path, monkeypatch) -> None:
    import asyncio

    from cambium import benchmark

    async def blocked_provider(*args, **kwargs):
        await asyncio.Event().wait()

    monkeypatch.setattr(benchmark, "run_plan", blocked_provider)
    monkeypatch.setattr(benchmark, "_resolve_provider", lambda config, repo: (config, {}))
    row = benchmark.run_case(
        {
            "id": "timeout",
            "split": "train",
            "task": "Read the file without edits",
            "files": {"note.txt": "unchanged"},
            "read_only": True,
            "check": [
                "{python}", "-c",
                "from pathlib import Path; assert Path('note.txt').read_text() == 'unchanged'",
            ],
        },
        {"coding": "Read only.", "summary": "Keep facts."},
        output=tmp_path,
        budget=ExperimentBudget(10, 1000, 1),
        max_wall_s=0.02,
    )
    assert not row["passed"]
    assert "rollout wall budget exhausted" in row["feedback"]
    assert "check=True" in row["feedback"]
    assert row["head"] == row["base"]


def test_tripped_breaker_aborts_dead_rollout_promptly_with_identical_verdict(tmp_path, monkeypatch):
    import asyncio

    from cambium import benchmark, worker

    strike = worker.MAX_CONSECUTIVE_INVALID_ACTIONS
    usage = {"kind": "usage_event", "payload": {"usage": {"total_tokens": 10}}}

    async def tripped_breaker(session_dir, plan, on_event=None, **kwargs):
        if on_event is not None:
            on_event(usage)
            on_event(
                {
                    "kind": "log",
                    "payload": {"message": f"invalid_action: bad JSON (strike {strike})"},
                }
            )
        await asyncio.sleep(0.05)
        if on_event is not None:
            on_event(usage)
        await asyncio.Event().wait()

    monkeypatch.setattr(benchmark, "run_plan", tripped_breaker)
    monkeypatch.setattr(benchmark, "_resolve_provider", lambda config, repo: (config, {}))
    row = benchmark.run_case(
        {
            "id": "breaker",
            "split": "train",
            "task": "Read the file without edits",
            "files": {"note.txt": "unchanged"},
            "read_only": True,
            "check": [
                "{python}",
                "-c",
                "from pathlib import Path; assert Path('note.txt').read_text() == 'unchanged'",
            ],
        },
        {"coding": "Read only.", "summary": "Keep facts."},
        output=tmp_path,
        budget=ExperimentBudget(10, 1000, 1),
        max_wall_s=60,
    )
    assert not row["passed"]
    assert row["score"] == 0.0
    assert "agent emitted 3 consecutive invalid actions" in row["feedback"]
    assert "rollout wall budget exhausted" not in row["feedback"]
    assert row["calls"] == 1
    assert row["tokens"] == 10


@pytest.mark.parametrize(
    ("component", "policy", "marker", "absent"),
    [
        (
            "coding",
            {"coding": "baseline", "summary": "keep findings"},
            "You are Cambium's coding agent",
            None,
        ),
        (
            "summary",
            {"coding": "baseline", "summary": "candidate findings contract"},
            "<cambium-summary-control>",
            "You are Cambium's coding agent",
        ),
    ],
)
def test_gepa_feedback_contains_real_prompt_and_bounded_trajectory(
    component: str, policy: dict[str, str], marker: str, absent: str | None
) -> None:
    from cambium import prompt_optimize

    row = {
        "passed": False,
        "score": 0.25,
        "elapsed_s": 299.9,
        "calls": 12,
        "tokens": 48_000,
        "summary_calls": 1,
        "children": 2,
        "user_summary_chars": 1200,
        "user_summary_lines": 14,
        "malformed_actions": 7,
        "tool_failures": 3,
        "turn_heads": ["a"],
        "feedback": "exit=1; check=False; rollout wall budget exhausted\n" + "x" * 9000,
    }
    feedback = prompt_optimize.grounded_feedback(component, policy, row)

    prompt_part, digest = feedback.split("<trajectory-digest>", 1)
    assert marker in prompt_part
    if absent is not None:
        assert absent not in prompt_part
    for key in ("summary_calls", "malformed_actions", "tool_failures", "user_summary_chars"):
        assert f'"{key}"' in digest
    assert "verbose-user-summary" in digest and "timeout-shaped" in digest
    assert len(feedback) <= prompt_optimize._FEEDBACK_CHARS


@pytest.mark.parametrize(
    ("score", "expected"),
    [
        (None, 0.0), ("bad", 0.0), (float("nan"), 0.0), (float("inf"), 0.0),
        (True, 1.0), (False, 0.0), (0.9, 0.9),
    ],
)
def test_prompt_metric_normalizes_prediction_scores(score, expected) -> None:
    from cambium import prompt_optimize

    prediction = SimpleNamespace(report="r")
    if score is not None:
        prediction.score = score
    result = prompt_optimize.metric(None, prediction)
    assert result.score == expected
    assert result.feedback == "r"


def test_summary_gepa_stops_when_validation_never_uses_summary_policy(
    tmp_path, monkeypatch
) -> None:
    from cambium import prompt_optimize

    monkeypatch.setenv("CAMBIUM_PROMPTS", str(tmp_path / "active.json"))
    prompts.save_policy({"coding": "baseline", "summary": "keep findings"})

    def rollout(case, policy, **kwargs):
        row = {
            "id": case["id"], "score": 1.0, "passed": True, "feedback": "pass",
            "elapsed_s": 1, "calls": 1, "tokens": 1, "summary_calls": 0,
        }
        kwargs["budget"].record({"total_tokens": 1})
        return row

    monkeypatch.setattr(prompt_optimize, "run_case", rollout)
    with pytest.raises(ValueError, match="actually performs a summary call"):
        prompt_optimize.run(_args(tmp_path, component="summary"))


@pytest.mark.parametrize("no_deploy", [False, True])
def test_gepa_deploys_only_when_enabled(tmp_path, monkeypatch, no_deploy: bool) -> None:
    dspy = pytest.importorskip("dspy")
    from cambium import prompt_optimize

    active = tmp_path / "active.json"
    monkeypatch.setenv("CAMBIUM_PROMPTS", str(active))
    prompts.save_policy({"coding": "baseline", "summary": "keep findings"})

    def rollout(case, policy, **kwargs):
        passed = policy["coding"] == "improved"
        row = {
            "id": case["id"], "score": float(passed), "passed": passed,
            "feedback": "pass" if passed else "check failed", "elapsed_s": 1,
            "calls": 1, "tokens": 1, "summary_calls": 0,
        }
        kwargs["budget"].record({"total_tokens": 1})
        kwargs["budget"].rows.append(row)
        return row

    real_gepa = dspy.GEPA

    def optimizer(**kwargs):
        kwargs.pop("reflection_lm")
        kwargs["instruction_proposer"] = (
            lambda candidate, reflective_dataset, components_to_update:
            {name: "improved" for name in components_to_update}
        )
        return real_gepa(**kwargs)

    monkeypatch.setattr(dspy, "GEPA", optimizer)
    monkeypatch.setattr(prompt_optimize, "run_case", rollout)
    monkeypatch.setattr(prompt_optimize, "_reflection_lm", lambda *args: None)
    args = _args(tmp_path, no_deploy=no_deploy)

    assert prompt_optimize.run(args) == 0
    report = json.loads((args.output / "report.json").read_text())
    assert report["deployed"] is not no_deploy
    assert prompts.load_policy()["coding"] == ("baseline" if no_deploy else "improved")
