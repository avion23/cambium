"""GEPA over real Cambium rollouts, with automatic plain-text deployment."""
from __future__ import annotations

import json
import math
import sys
from dataclasses import replace
from pathlib import Path
from statistics import mean
from typing import Any

from .benchmark import ExperimentBudget, ExperimentBudgetExceeded, load_cases, run_case
from .prompts import load_policy, prompt_path, save_policy


def make_program(component: str, policy: dict[str, str], runner: Any) -> Any:
    """Expose a real rollout as one traceable DSPy predictor, not another agent."""
    import dspy

    class Rollout(dspy.Predict):
        def __init__(self) -> None:
            super().__init__(dspy.Signature(
                "case: dict -> report: str, score: float", instructions=policy[component],
            ))

        def forward(self, **kwargs: Any) -> Any:
            case = kwargs["case"]
            selected = {**policy, component: self.signature.instructions}
            row = runner(case, selected)
            report = json.dumps(row, ensure_ascii=False)
            return self._forward_postprocess(
                [{"report": report, "score": row["score"]}], self.signature, case=case,
            )

    class Program(dspy.Module):
        def __init__(self) -> None:
            super().__init__()
            self.policy = Rollout()

        def forward(self, case: dict) -> Any:
            return self.policy(case=case)

    return Program()


def metric(
    gold: Any, pred: Any, trace: Any = None, pred_name: Any = None, pred_trace: Any = None,
) -> Any:
    import dspy

    return dspy.Prediction(score=float(pred.score), feedback=pred.report)


def _reflection_lm(args: Any, budget: ExperimentBudget) -> Any:
    from .diffundo import CredentialSource, Diffundo, ProviderTier
    from .lm import CambiumLM
    from .oneshot import OneShotConfig, _resolve_provider
    from .optimize import _CostLedger, _TrackingDiffundo
    from .provider_config import AuthMode, load_providers, select_provider

    resolved, environment = _resolve_provider(
        OneShotConfig(provider=args.reflection_provider or args.provider), Path.cwd(),
    )
    providers = [
        replace(p, api_key=environment.get(p.api_key_env, p.api_key))
        for p in load_providers(resolved.provider_config_path)
        if p.name in resolved.authorized_providers
    ]
    selected = select_provider(
        providers, name=resolved.assigned_provider,
        tier=ProviderTier(args.tier) if resolved.assigned_provider is None else None,
    )
    options: dict[str, Any] = {"primary_provider": selected.name}
    if selected.auth is AuthMode.CODEX_CHATGPT:
        from .oauth import TokenManager

        token, account = TokenManager(selected.name).ensure_fresh()
        options["credential_source"] = CredentialSource(access_token=token, account_id=account)

    class Ledger(_CostLedger):
        def check_available(self) -> None:
            budget.check()
            super().check_available()

        def record(self, value: Any, *, provider: Any = None) -> None:
            super().record(value, provider=provider)
            budget.record(dict(value.usage or {}), value.estimated_cost_usd or 0.0)

    router = _TrackingDiffundo(Diffundo(providers, **options), Ledger(args.budget_usd))
    return CambiumLM(router, selected.tier, budget_usd=args.budget_usd, max_tokens=2048)


def run(args: Any) -> int:
    """Run a benchmark or hill climb; publish winners for new sessions by default."""
    dataset = args.dataset or Path(__file__).with_name("benchmarks") / "prompts.jsonl"
    cases = load_cases(dataset)
    for name in ("max_evals", "max_calls", "max_tokens", "max_turns", "max_workers"):
        if getattr(args, name) < 1:
            raise ValueError(f"--{name.replace('_', '-')} must be positive")
    if any(not math.isfinite(v) or v <= 0 for v in (args.max_wall_s, args.budget_usd)):
        raise ValueError("wall and cash budgets must be finite and positive")
    if args.optimizer not in {"zero", "gepa"}:
        raise ValueError("prompt optimization supports zero or gepa")
    policy = load_policy()
    output = (args.output or Path(".cambium/prompt-experiments")).resolve()
    selected_cases = [c for c in cases if not args.case or c["id"] in args.case]
    if not selected_cases:
        raise ValueError("no benchmark cases selected")
    if args.dry_run:
        print(json.dumps({
            "optimizer": args.optimizer, "component": args.component,
            "cases": [{"id": c["id"], "split": c["split"]} for c in selected_cases],
            "deploy": not args.no_deploy and args.optimizer == "gepa",
            "prompt_file": str(prompt_path()), "output": str(output),
            "max_evals": args.max_evals, "max_calls": args.max_calls,
            "max_tokens": args.max_tokens,
        }, indent=2))
        return 0
    budget = ExperimentBudget(args.max_calls, args.max_tokens, args.budget_usd)

    def rollout(case: dict, candidate: dict) -> dict:
        row = run_case(
            case, candidate, output=output, budget=budget, provider=args.provider,
            max_turns=args.max_turns, max_wall_s=args.max_wall_s, max_workers=args.max_workers,
        )
        print(f"{case['id']}: {'pass' if row['passed'] else 'FAIL'} "
              f"{row['elapsed_s']}s {row['calls']} calls {row['tokens']} tokens", file=sys.stderr)
        return row

    report: dict[str, Any] = {
        "optimizer": args.optimizer, "component": args.component, "deployed": False,
    }
    code = 0
    try:
        if args.optimizer == "zero":
            results = [rollout(case, policy) for case in selected_cases]
            code = 0 if all(row["passed"] for row in results) else 1
        else:
            import dspy

            splits = {
                s: [c for c in selected_cases if c["split"] == s] for s in ("train", "val", "test")
            }
            if not all(splits.values()):
                raise ValueError("GEPA needs disjoint train, val and test cases")
            baseline = [rollout(c, policy) for c in splits["val"]]
            student = make_program(args.component, policy, rollout)
            optimizer = dspy.GEPA(
                metric=metric, reflection_lm=_reflection_lm(args, budget),
                max_metric_calls=args.max_evals, reflection_minibatch_size=1,
                candidate_selection_strategy="current_best", use_merge=False,
                num_threads=1, seed=args.seed, track_stats=True, skip_perfect_score=False,
            )
            compiled = optimizer.compile(
                student,
                trainset=[dspy.Example(case=c).with_inputs("case") for c in splits["train"]],
                valset=[dspy.Example(case=c).with_inputs("case") for c in splits["val"]],
            )
            winner = {**policy, args.component: compiled.policy.signature.instructions}
            comparison = [rollout(c, winner) for c in splits["val"]]
            held_out = [rollout(c, winner) for c in splits["test"]]
            improved = (
                winner != policy
                and sum(r["passed"] for r in comparison) >= sum(r["passed"] for r in baseline)
                and mean(r["score"] for r in comparison) > mean(r["score"] for r in baseline)
                and all(r["passed"] for r in held_out)
            )
            save_policy(winner, output / "candidate.json")
            report.update(
                baseline=baseline, validation=comparison, test=held_out, improved=improved,
            )
            if improved and not args.no_deploy:
                report.update(deployed=True, prompt_file=str(save_policy(winner)))
    except ExperimentBudgetExceeded as exc:
        report["stopped"] = str(exc)
        code = 1
    finally:
        output.mkdir(parents=True, exist_ok=True)
        report.update(
            calls=budget.calls, tokens=budget.tokens, cost_usd=budget.cost_usd, runs=budget.rows,
        )
        (output / "report.json").write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps({
        key: value for key, value in {**report, "report": str(output / "report.json")}.items()
        if key not in {"runs", "baseline", "validation", "test"}
    }, indent=2))
    return code
