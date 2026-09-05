"""Deployment replaces policy text; active sessions retain their snapshot."""
from pathlib import Path

import pytest

from cambium import prompts, worker
from cambium.benchmark import ExperimentBudget, ExperimentBudgetExceeded, load_cases


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


def test_shipped_benchmark_has_disjoint_splits_and_self_change() -> None:
    cases = load_cases(Path(prompts.__file__).with_name("benchmarks") / "prompts.jsonl")
    assert {case["split"] for case in cases} == {"train", "val", "test"}
    assert any("repo" in case for case in cases)
    assert len({case["id"] for case in cases}) == len(cases)


def test_real_gepa_updates_the_policy_used_by_rollouts() -> None:
    dspy = pytest.importorskip("dspy")
    from cambium.prompt_optimize import make_program, metric

    seen = []

    def rollout(case, policy):
        seen.append(policy["coding"])
        return {"score": 1.0 if policy["coding"] == "improved" else 0.0, "feedback": "check failed"}

    student = make_program("coding", {"coding": "baseline", "summary": "keep facts"}, rollout)

    def propose(candidate, reflective_dataset, components_to_update):
        return {name: "improved" for name in components_to_update}

    optimizer = dspy.GEPA(
        metric=metric, instruction_proposer=propose, max_metric_calls=8,
        reflection_minibatch_size=1, candidate_selection_strategy="current_best",
        use_merge=False, num_threads=1, seed=0,
    )
    compiled = optimizer.compile(
        student, trainset=[dspy.Example(case={"id": "train"}).with_inputs("case")],
        valset=[dspy.Example(case={"id": "val"}).with_inputs("case")],
    )
    assert compiled.policy.signature.instructions == "improved"
    assert compiled(case={"id": "heldout"}).score == 1.0
    assert "baseline" in seen and "improved" in seen


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
