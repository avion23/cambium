"""GEPA over real Cambium rollouts, with automatic plain-text deployment."""

from __future__ import annotations

import json
import math
import sys
from dataclasses import replace
from pathlib import Path
from statistics import mean
from typing import Any, cast

from .benchmark import (
    ExperimentBudget,
    ExperimentBudgetExceeded,
    json_finite,
    load_cases,
    run_case,
    write_json_report,
)
from .prompts import coding_prompt, load_policy, prompt_path, save_policy

_FEEDBACK_CHARS = 6000
_PROMPT_CHARS = 3500
_DIGEST_KEYS = (
    "passed",
    "score",
    "elapsed_s",
    "calls",
    "tokens",
    "malformed_actions",
    "tool_failures",
    "children",
    "user_summary_chars",
    "user_summary_lines",
)


def _bounded(text: str, cap: int, label: str) -> str:
    if len(text) <= cap:
        return text
    suffix = f"\n[{label} truncated: +~{len(text) - cap} chars]"
    return text[: cap - len(suffix)] + suffix


def _derailment(row: dict[str, Any]) -> list[str]:
    """Name where the rollout derailed so reflection proposals are not blind."""
    feedback = str(row.get("feedback", ""))
    flags = []
    if row.get("malformed_actions"):
        flags.append(f"malformed-heavy: {row['malformed_actions']} invalid_action logs")
    if not row.get("passed") and "check=False" in feedback:
        flags.append("check-failed: acceptance command failed")
    if "wall budget exhausted" in feedback:
        flags.append(f"timeout-shaped: elapsed_s={row.get('elapsed_s')}")
    if row.get("tool_failures"):
        flags.append(f"tool-failures: {row['tool_failures']} failed tool events")
    if (row.get("user_summary_chars") or 0) > 800 or (row.get("user_summary_lines") or 0) > 6:
        flags.append(
            "verbose-user-summary: "
            f"{row.get('user_summary_chars', 0)} chars / {row.get('user_summary_lines', 0)} lines"
        )
    return flags or ["none observed"]


def _render_component_prompt(component: str, selected: dict[str, str]) -> str:
    """Render the production prompt shape the candidate text actually flows into."""
    if component != "summary":
        return coding_prompt(selected)
    from .summary_trunk import SUMMARY_CONTROL_CLOSE, SUMMARY_CONTROL_OPEN

    control = {
        "type": "summarize_tail",
        "finding_preservation_contract": selected["summary"],
    }
    payload = json.dumps(json_finite(control), ensure_ascii=False, allow_nan=False)
    return SUMMARY_CONTROL_OPEN + payload + SUMMARY_CONTROL_CLOSE


def grounded_feedback(component: str, selected: dict[str, str], row: dict[str, Any]) -> str:
    """Ground reflection in what the model saw: rendered prompt, digest, raw row."""
    return _bounded(
        "\n\n".join(
            (
                "<rendered-production-prompt>\n"
                + _bounded(
                    _render_component_prompt(component, selected),
                    _PROMPT_CHARS,
                    "rendered prompt",
                ),
                "<trajectory-digest>\n"
                + json.dumps(
                    json_finite(
                        {key: row.get(key) for key in _DIGEST_KEYS}
                        | {"derailment": _derailment(row)}
                    ),
                    ensure_ascii=False,
                    allow_nan=False,
                ),
                "<raw-row>\n" + json.dumps(json_finite(row), ensure_ascii=False, allow_nan=False),
            )
        ),
        _FEEDBACK_CHARS,
        "feedback",
    )


def make_program(component: str, policy: dict[str, str], runner: Any) -> Any:
    """Expose a real rollout as one traceable DSPy predictor, not another agent."""
    import dspy

    class Rollout(dspy.Predict):
        def __init__(self) -> None:
            # dspy accepts a Signature instance and a positional string; its stubs reject both.
            super().__init__(
                dspy.Signature(  # type: ignore[reportArgumentType]
                    "case: dict -> report: str, score: float",  # type: ignore[reportCallIssue]
                    instructions=policy[component],
                )
            )

        def forward(self, **kwargs: Any) -> Any:
            case = kwargs["case"]
            # dspy binds .signature at runtime; the inferred stubs hide it from the checker.
            selected = {**policy, component: cast(Any, self.signature).instructions}
            row = runner(case, selected)
            report = grounded_feedback(component, selected, row)
            return self._forward_postprocess(
                [{"report": report, "score": row["score"]}],
                self.signature,
                case=case,
            )

    class Program(dspy.Module):
        def __init__(self) -> None:
            super().__init__()
            self.policy = Rollout()

        def forward(self, case: dict) -> Any:
            return self.policy(case=case)

    return Program()


def metric(
    gold: Any,
    pred: Any,
    trace: Any = None,
    pred_name: Any = None,
    pred_trace: Any = None,
) -> Any:
    import dspy

    try:
        score = float(pred.score)
    except (AttributeError, TypeError, ValueError):
        return dspy.Prediction(score=0.0, feedback=getattr(pred, "report", "") or "")
    return dspy.Prediction(score=score, feedback=pred.report)


def _reflection_lm(args: Any, budget: ExperimentBudget) -> Any:
    from .diffundo import CredentialSource, Diffundo, ProviderTier
    from .lm import CambiumLM
    from .oneshot import OneShotConfig, _resolve_provider
    from .optimize import _CostLedger, _TrackingDiffundo
    from .provider_config import AuthMode, load_providers, select_provider

    resolved, environment = _resolve_provider(
        OneShotConfig(provider=args.reflection_provider or args.provider),
        Path.cwd(),
    )
    providers = [
        replace(p, api_key=environment.get(p.api_key_env, p.api_key))
        for p in load_providers(resolved.provider_config_path)
        if p.name in resolved.authorized_providers
    ]
    selected = select_provider(
        providers,
        name=resolved.assigned_provider,
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


def _effective_search_evals(
    requested: int,
    budget: ExperimentBudget,
    baseline: list[dict[str, Any]],
    evaluation_cases: int,
) -> int:
    """Reserve measured capacity for validation and held-out evaluation."""
    if not baseline or evaluation_cases < 1:
        return requested
    max_tokens = max(1, max(int(row.get("tokens", 0) or 0) for row in baseline))
    max_calls = max(1, max(int(row.get("calls", 0) or 0) for row in baseline))
    cushion = 1.25
    reserve_tokens = math.ceil(max_tokens * evaluation_cases * cushion)
    reserve_calls = math.ceil(max_calls * evaluation_cases * cushion)
    token_headroom = budget.max_tokens - budget.tokens - reserve_tokens
    call_headroom = budget.max_calls - budget.calls - reserve_calls
    if token_headroom <= 0 or call_headroom <= 0:
        raise ExperimentBudgetExceeded("budget cannot cover GEPA search plus final evaluation")
    token_evals = token_headroom // math.ceil(max_tokens * cushion)
    call_evals = call_headroom // math.ceil(max_calls * cushion)
    limits = [requested, token_evals, call_evals]
    max_cost = max(float(row.get("cost_usd", 0.0) or 0.0) for row in baseline)
    if max_cost > 0:
        reserve_cost = max_cost * evaluation_cases * cushion
        cost_headroom = budget.max_usd - budget.cost_usd - reserve_cost
        if cost_headroom <= 0:
            raise ExperimentBudgetExceeded("cash budget cannot cover final GEPA evaluation")
        limits.append(int(cost_headroom // (max_cost * cushion)))
    effective = min(limits)
    if effective < 1:
        raise ExperimentBudgetExceeded("budget leaves no GEPA search call after evaluation reserve")
    return int(effective)


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
        print(
            json.dumps(
                json_finite(
                    {
                        "optimizer": args.optimizer,
                        "component": args.component,
                        "cases": [{"id": c["id"], "split": c["split"]} for c in selected_cases],
                        "deploy": not args.no_deploy and args.optimizer == "gepa",
                        "prompt_file": str(prompt_path()),
                        "output": str(output),
                        "max_evals": args.max_evals,
                        "max_calls": args.max_calls,
                        "max_tokens": args.max_tokens,
                    }
                ),
                indent=2,
                allow_nan=False,
            )
        )
        return 0
    budget = ExperimentBudget(args.max_calls, args.max_tokens, args.budget_usd)

    def rollout(case: dict, candidate: dict) -> dict:
        row = run_case(
            case,
            candidate,
            output=output,
            budget=budget,
            provider=args.provider,
            max_turns=args.max_turns,
            max_wall_s=args.max_wall_s,
            max_workers=args.max_workers,
        )
        print(
            f"{case['id']}: {'pass' if row['passed'] else 'FAIL'} "
            f"{row['elapsed_s']}s {row['calls']} calls {row['tokens']} tokens",
            file=sys.stderr,
        )
        return row

    report: dict[str, Any] = {
        "optimizer": args.optimizer,
        "component": args.component,
        "deployed": False,
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
            effective_max_evals = _effective_search_evals(
                args.max_evals,
                budget,
                baseline,
                len(splits["val"]) + len(splits["test"]),
            )
            report["effective_max_evals"] = effective_max_evals
            student = make_program(args.component, policy, rollout)
            optimizer = dspy.GEPA(
                metric=metric,
                reflection_lm=_reflection_lm(args, budget),
                max_metric_calls=effective_max_evals,
                reflection_minibatch_size=1,
                candidate_selection_strategy="current_best",
                use_merge=False,
                num_threads=1,
                seed=args.seed,
                track_stats=True,
                skip_perfect_score=False,
            )
            compiled = optimizer.compile(
                student,
                trainset=[dspy.Example(case=c).with_inputs("case") for c in splits["train"]],
                valset=[dspy.Example(case=c).with_inputs("case") for c in splits["val"]],
            )
            # dspy exposes compiled.policy.signature only at runtime.
            winner = {**policy, args.component: cast(Any, compiled.policy).signature.instructions}
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
                baseline=baseline,
                validation=comparison,
                test=held_out,
                improved=improved,
            )
            if improved and not args.no_deploy:
                report.update(deployed=True, prompt_file=str(save_policy(winner)))
    except ExperimentBudgetExceeded as exc:
        report["stopped"] = str(exc)
        code = 1
    finally:
        output.mkdir(parents=True, exist_ok=True)
        report.update(
            calls=budget.calls,
            tokens=budget.tokens,
            cost_usd=budget.cost_usd,
            runs=budget.rows,
        )
        write_json_report(output / "report.json", report)
    print(
        json.dumps(
            json_finite(
                {
                    key: value
                    for key, value in {**report, "report": str(output / "report.json")}.items()
                    if key not in {"runs", "baseline", "validation", "test"}
                }
            ),
            indent=2,
            allow_nan=False,
        )
    )
    return code
