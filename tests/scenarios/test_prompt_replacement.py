"""Deployment replaces policy text; active sessions retain their snapshot."""

from pathlib import Path

import pytest

from cambium import prompts, worker
from cambium.benchmark import ExperimentBudget, ExperimentBudgetExceeded, _peak_pending_children


def test_atomic_replacement_changes_new_prompts_not_existing_snapshot(
    tmp_path: Path,
    monkeypatch,
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
    assert '"type":"finish"' in after["messages"][0]["content"]
    assert "under about 600 characters" in after["messages"][0]["content"]
    assert "summary_entry" not in second["coding"]


def test_experiment_budget_counts_tokens_when_cash_is_zero() -> None:
    budget = ExperimentBudget(2, 100, 1.0)
    budget.record({"prompt_tokens": 90, "completion_tokens": 10}, 0.0)
    with pytest.raises(ExperimentBudgetExceeded):
        budget.check()


def test_gepa_search_reserves_measured_budget_for_final_evaluation() -> None:
    from cambium.prompt_optimize import _effective_search_evals

    budget = ExperimentBudget(600, 2_000_000, 50.0, calls=16, tokens=119_911)
    baseline = [
        {"calls": 10, "tokens": 97_740, "cost_usd": 0.0},
        {"calls": 6, "tokens": 22_171, "cost_usd": 0.0},
    ]
    assert _effective_search_evals(24, budget, baseline, evaluation_cases=5) == 10


def test_parallel_benchmark_distinguishes_overlapping_siblings_from_serial_work() -> None:
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


def test_rollout_timeout_retains_report_and_checks_only_accepted_code(tmp_path, monkeypatch):
    import asyncio
    import json

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
                "{python}",
                "-c",
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
    assert json.loads((Path(row["directory"]) / "report.json").read_text()) == row


def test_gepa_reflection_feedback_is_grounded_in_rendered_prompt_and_trajectory() -> None:
    pytest.importorskip("dspy")
    from cambium import prompt_optimize

    policy = {"coding": "baseline", "summary": "keep findings"}
    row = {
        "id": "ground",
        "split": "train",
        "passed": False,
        "score": 0.25,
        "elapsed_s": 299.9,
        "calls": 12,
        "tokens": 48000,
        "cost_usd": 0.5,
        "children": 2,
        "user_summary_chars": 1200,
        "user_summary_lines": 14,
        "malformed_actions": 7,
        "tool_failures": 3,
        "feedback": "exit=1; check=False; scope=True; rollout wall budget exhausted\n" + "x" * 9000,
    }
    seen: dict = {}

    def runner(case, selected):
        seen.update(selected)
        return row

    prediction = prompt_optimize.make_program("coding", policy, runner)(case={"id": "ground"})
    assert prediction.score == 0.25
    result = prompt_optimize.metric(None, prediction)
    assert isinstance(result.score, float)
    assert result.score == 0.25
    feedback = result.feedback
    # (i) the fully rendered production prompt, with its action-protocol envelope
    assert "You are Cambium's coding agent in an assigned Git worktree" in feedback
    assert '{"type":"finish","summary":"...","objective_met":true}' in feedback
    assert "baseline" in feedback  # candidate instructions flow through coding_prompt
    # (ii) trajectory digest keys plus derailment highlights
    assert "<trajectory-digest>" in feedback
    for key in (
        "passed",
        "elapsed_s",
        "calls",
        "tokens",
        "malformed_actions",
        "tool_failures",
        "children",
        "user_summary_chars",
        "user_summary_lines",
        "derailment",
    ):
        assert f'"{key}"' in feedback
    assert "malformed-heavy" in feedback
    assert "check-failed" in feedback
    assert "timeout-shaped" in feedback
    assert "verbose-user-summary" in feedback
    # (iii) feedback stays bounded even for a large row; raw row JSON remains
    assert len(feedback) <= prompt_optimize._FEEDBACK_CHARS
    assert "[feedback truncated:" in feedback
    assert "<raw-row>" in feedback
    # runner still receives the full merged policy for the real rollout
    assert seen == {**policy, "coding": "baseline"}


def test_gepa_reflection_feedback_renders_summary_control_for_summary_component() -> None:
    pytest.importorskip("dspy")
    from cambium import prompt_optimize

    policy = {"coding": "baseline", "summary": "candidate findings contract"}
    row = {
        "id": "ground",
        "split": "train",
        "passed": True,
        "score": 0.9,
        "elapsed_s": 3.1,
        "calls": 2,
        "tokens": 900,
        "cost_usd": 0.0,
        "children": 0,
        "malformed_actions": 0,
        "tool_failures": 0,
        "feedback": "exit=0; check=True; scope=True",
    }

    def runner(case, selected):
        return row

    prediction = prompt_optimize.make_program("summary", policy, runner)(case={"id": "ground"})
    feedback = prompt_optimize.metric(None, prediction).feedback
    # summary candidates render inside the production summary-control shape,
    # not the coding prompt (which omits the summary text entirely)
    assert "<cambium-summary-control>" in feedback
    assert "candidate findings contract" in feedback
    assert "finding_preservation_contract" in feedback
    assert "You are Cambium's coding agent" not in feedback.split("<trajectory-digest>")[0]
    assert len(feedback) <= prompt_optimize._FEEDBACK_CHARS


def test_prompt_metric_scores_malformed_predictions_zero() -> None:
    from types import SimpleNamespace

    from cambium import prompt_optimize

    assert prompt_optimize.metric(None, SimpleNamespace(report="no score")).score == 0.0
    assert prompt_optimize.metric(None, SimpleNamespace(score=None, report="r")).score == 0.0
    assert prompt_optimize.metric(None, SimpleNamespace(score="bad", report="r")).score == 0.0
    good = prompt_optimize.metric(None, SimpleNamespace(score=0.9, report="r"))
    assert good.score == 0.9
    assert good.feedback == "r"


@pytest.mark.parametrize("no_deploy", [False, True])
def test_gepa_winner_is_automatically_deployed_unless_disabled(
    tmp_path: Path,
    monkeypatch,
    no_deploy: bool,
) -> None:
    import json
    from types import SimpleNamespace

    dspy = pytest.importorskip("dspy")
    from cambium import prompt_optimize

    active = tmp_path / "active.json"
    monkeypatch.setenv("CAMBIUM_PROMPTS", str(active))
    prompts.save_policy({"coding": "baseline", "summary": "keep findings"})
    dataset = tmp_path / "cases.jsonl"
    dataset.write_text(
        "\n".join(
            json.dumps(
                {
                    "id": split,
                    "split": split,
                    "task": split,
                    "check": ["unused"],
                }
            )
            for split in ("train", "val", "test")
        )
    )

    def rollout(case, policy, **kwargs):
        passed = policy["coding"] == "improved"
        row = {
            "id": case["id"],
            "score": float(passed),
            "passed": passed,
            "feedback": "pass" if passed else "check failed",
            "elapsed_s": 1,
            "calls": 1,
            "tokens": 1,
        }
        kwargs["budget"].record({"total_tokens": 1})
        kwargs["budget"].rows.append(row)
        return row

    real_gepa = dspy.GEPA

    def propose(candidate, reflective_dataset, components_to_update):
        return {name: "improved" for name in components_to_update}

    def optimizer(**kwargs):
        kwargs.pop("reflection_lm")
        kwargs["instruction_proposer"] = propose
        return real_gepa(**kwargs)

    monkeypatch.setattr(dspy, "GEPA", optimizer)
    monkeypatch.setattr(prompt_optimize, "run_case", rollout)
    monkeypatch.setattr(prompt_optimize, "_reflection_lm", lambda *args: None)
    args = SimpleNamespace(
        dataset=dataset,
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
        case=[],
        dry_run=False,
        no_deploy=no_deploy,
        seed=0,
    )
    assert prompt_optimize.run(args) == 0
    report = json.loads((args.output / "report.json").read_text())
    assert report["deployed"] is not no_deploy
    assert prompts.load_policy()["coding"] == ("baseline" if no_deploy else "improved")
    assert prompts.load_policy(args.output / "candidate.json")["coding"] == "improved"
