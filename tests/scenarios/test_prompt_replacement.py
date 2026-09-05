"""Deployment replaces policy text; active sessions retain their snapshot."""
from pathlib import Path

import pytest

from cambium import prompts, worker
from cambium.benchmark import ExperimentBudget, ExperimentBudgetExceeded, _peak_pending_children


def test_atomic_replacement_changes_new_prompts_not_existing_snapshot(
    tmp_path: Path, monkeypatch,
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
    assert "summary_entry" not in second["coding"]


def test_experiment_budget_counts_tokens_when_cash_is_zero() -> None:
    budget = ExperimentBudget(2, 100, 1.0)
    budget.record({"prompt_tokens": 90, "completion_tokens": 10}, 0.0)
    with pytest.raises(ExperimentBudgetExceeded):
        budget.check()


def test_parallel_benchmark_distinguishes_overlapping_siblings_from_serial_work() -> None:
    admitted = [
        {"kind": "child_admitted", "task_id": "parent",
         "payload": {"parent_task_id": "parent", "child_task_id": child}}
        for child in ("csv", "config")
    ]
    finished = [
        {"kind": "child_result", "task_id": child,
         "payload": {"parent_task_id": "parent", "status": "succeeded"}}
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
            "id": "timeout", "split": "train", "task": "Read the file without edits",
            "files": {"note.txt": "unchanged"}, "read_only": True,
            "check": ["{python}", "-c", "from pathlib import Path; "
                      "assert Path('note.txt').read_text() == 'unchanged'"],
        },
        {"coding": "Read only.", "summary": "Keep facts."}, output=tmp_path,
        budget=ExperimentBudget(10, 1000, 1), max_wall_s=0.02,
    )
    assert not row["passed"]
    assert "rollout wall budget exhausted" in row["feedback"]
    assert "check=True" in row["feedback"]
    assert row["head"] == row["base"]
    assert json.loads((Path(row["directory"]) / "report.json").read_text()) == row


@pytest.mark.parametrize("no_deploy", [False, True])
def test_gepa_winner_is_automatically_deployed_unless_disabled(
    tmp_path: Path, monkeypatch, no_deploy: bool,
) -> None:
    import json
    from types import SimpleNamespace

    dspy = pytest.importorskip("dspy")
    from cambium import prompt_optimize

    active = tmp_path / "active.json"
    monkeypatch.setenv("CAMBIUM_PROMPTS", str(active))
    prompts.save_policy({"coding": "baseline", "summary": "keep findings"})
    dataset = tmp_path / "cases.jsonl"
    dataset.write_text("\n".join(json.dumps({
        "id": split, "split": split, "task": split, "check": ["unused"],
    }) for split in ("train", "val", "test")))

    def rollout(case, policy, **kwargs):
        passed = policy["coding"] == "improved"
        row = {
            "id": case["id"], "score": float(passed), "passed": passed,
            "feedback": "pass" if passed else "check failed",
            "elapsed_s": 1, "calls": 1, "tokens": 1,
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
        dataset=dataset, output=tmp_path / "experiment", component="coding", optimizer="gepa",
        max_evals=8, max_calls=30, max_tokens=100, max_turns=8, max_wall_s=10,
        max_workers=2, budget_usd=1, provider=None, case=[], dry_run=False,
        no_deploy=no_deploy, seed=0,
    )
    assert prompt_optimize.run(args) == 0
    report = json.loads((args.output / "report.json").read_text())
    assert report["deployed"] is not no_deploy
    assert prompts.load_policy()["coding"] == ("baseline" if no_deploy else "improved")
    assert prompts.load_policy(args.output / "candidate.json")["coding"] == "improved"
