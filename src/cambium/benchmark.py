"""Finite repository-task rollouts through the normal Cambium supervisor.

Fixtures and verification commands are operator-owned. Each rollout uses a
private repository; only its accepted Git head is checked. No DSPy at runtime.
"""
from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

from .oneshot import OneShotConfig, _resolve_provider, build_plan
from .prompts import validate_policy
from .supervisor import run_plan


class ExperimentBudgetExceeded(RuntimeError):
    pass


@dataclass
class ExperimentBudget:
    max_calls: int
    max_tokens: int
    max_usd: float
    calls: int = 0
    tokens: int = 0
    cost_usd: float = 0.0
    rows: list[dict[str, Any]] = field(default_factory=list)

    def __deepcopy__(self, memo: dict) -> ExperimentBudget:
        # Candidate copies share the experiment's resource owner.
        return self

    def check(self) -> None:
        if (
            self.calls >= self.max_calls or self.tokens >= self.max_tokens
            or self.cost_usd >= self.max_usd
        ):
            raise ExperimentBudgetExceeded("experiment call, token, or cash budget exhausted")

    def record(self, usage: dict, cost: float = 0.0) -> None:
        self.calls += 1
        self.tokens += int(usage.get("total_tokens", 0) or (
            usage.get("prompt_tokens", usage.get("input_tokens", 0))
            + usage.get("completion_tokens", usage.get("output_tokens", 0))
        ))
        self.cost_usd += max(0.0, float(cost))


def load_cases(path: Path) -> list[dict[str, Any]]:
    cases = [json.loads(line) for line in path.read_text().splitlines() if line.strip()]
    if (
        not cases or not all(isinstance(c.get("id"), str) and c["id"] for c in cases)
        or len({case["id"] for case in cases}) != len(cases)
    ):
        raise ValueError("benchmark cases need unique ids")
    for case in cases:
        if case.get("split") not in {"train", "val", "test"}:
            raise ValueError("benchmark split must be train, val, or test")
        if not isinstance(case.get("task"), str) or not case["task"].strip():
            raise ValueError("benchmark task must be non-empty")
        check = case.get("check")
        if not isinstance(check, list) or not check or not all(isinstance(x, str) for x in check):
            raise ValueError("benchmark check must be a command array")
        if not isinstance(case.get("files", {}), dict):
            raise ValueError("benchmark files must be a path-to-content mapping")
        if "repo" in case:
            case["repo"] = str((path.parent / case["repo"]).resolve())
    return cases


def _git(repo: Path, *args: str) -> str:
    return subprocess.check_output(
        ["git", "-C", str(repo), *args], text=True, stderr=subprocess.PIPE,
    ).strip()


def _repository(case: dict, root: Path) -> Path:
    repo = root / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.name", "Cambium benchmark")
    _git(repo, "config", "user.email", "benchmark@invalid")
    if "repo" in case:
        _git(repo, "fetch", "--quiet", case["repo"], case.get("base_ref", "HEAD"))
        _git(repo, "merge", "--ff-only", "FETCH_HEAD")
    for name, text in case.get("files", {}).items():
        target = (repo / name).resolve()
        if not target.is_relative_to(repo) or ".git" in target.relative_to(repo).parts:
            raise ValueError("benchmark fixture path must stay inside its repository")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")
    _git(repo, "add", ".")
    if "repo" not in case or _git(repo, "diff", "--cached", "--name-only"):
        _git(repo, "commit", "-qm", "benchmark input")
    return repo


def run_case(
    case: dict, policy: dict[str, str], *, output: Path, budget: ExperimentBudget,
    provider: str | None = None, max_turns: int = 12, max_wall_s: float = 300,
    max_workers: int = 3,
) -> dict[str, Any]:
    """Run, check and retain one real trajectory; never change the source repo."""
    budget.check()
    policy = validate_policy(policy)
    output.mkdir(parents=True, exist_ok=True)
    root = Path(tempfile.mkdtemp(prefix="task-", dir=output)).resolve()
    repo = _repository(case, root)
    base = _git(repo, "rev-parse", "HEAD")
    events: list[dict] = []
    started = time.monotonic()
    before = (budget.calls, budget.tokens, budget.cost_usd)
    config = OneShotConfig(
        prompt=case["task"], repo=repo, session_root=root / "session", provider=provider,
        max_turns=max_turns, max_wall_s=max_wall_s, max_restarts=0,
        max_tokens=max(1, budget.max_tokens - budget.tokens), prompt_policy=policy,
    )

    async def execute() -> tuple[int, str]:
        owner = asyncio.current_task()

        def observe(event: dict) -> None:
            events.append(event)
            if event.get("kind") != "usage_event":
                return
            data = event.get("payload", {})
            budget.record(data.get("usage") or {}, data.get("estimated_cost_usd", 0.0) or 0.0)
            try:
                budget.check()
            except ExperimentBudgetExceeded:
                if owner is not None:
                    owner.cancel()

        resolved, environment = _resolve_provider(config, repo)
        if case.get("providers"):
            from .provider_config import load_providers

            names = set(case["providers"])
            available = [
                p for p in load_providers(resolved.provider_config_path)
                if p.name in names and p.name in resolved.authorized_providers
            ]
            if {p.name for p in available} != names:
                raise ValueError("benchmark provider pool is not credential-ready")
            if resolved.assigned_provider and resolved.assigned_provider not in names:
                raise ValueError("benchmark primary provider is outside its provider pool")
            resolved = replace(
                resolved, authorized_providers=tuple(p.name for p in available),
                model_candidates=tuple(sorted({p.model for p in available})),
            )
        plan = build_plan(resolved, repo, root / "session")
        result = await run_plan(
            root / "session", plan, provider_environment=environment,
            routing_state_path=root / "routing.json", on_event=observe,
            max_concurrent_tasks=max_workers, context_reuse=True,
        )
        return result.exit_code, "; ".join(r.reason for r in result.results if r.reason)

    error = ""
    try:
        exit_code, error = asyncio.run(execute())
    except asyncio.CancelledError:
        exit_code, error = 1, "experiment budget exhausted; rollout cancelled"
    elapsed = time.monotonic() - started
    accepted = _git(repo, "rev-parse", "main")
    changed = _git(repo, "diff", "--name-only", base, accepted).splitlines()
    # The checker sees accepted code, never an uncommitted worker tree.
    verify = root / "accepted"
    _git(repo, "worktree", "add", "--quiet", "--detach", str(verify), accepted)
    command = [sys.executable if part == "{python}" else part for part in case["check"]]
    check_env = dict(os.environ, PYTHONPATH=str(verify / "src"))
    try:
        check = subprocess.run(
            command, cwd=verify, env=check_env, capture_output=True, text=True, timeout=60,
        )
        checked = check.returncode == 0
        diagnostic = (check.stdout + check.stderr)[-4000:]
    except subprocess.TimeoutExpired:
        checked, diagnostic = False, "verification timed out"
    allowed = case.get("allowed_files")
    scope_ok = allowed is None or set(changed) <= set(allowed)
    passed = exit_code == 0 and checked and scope_ok
    calls, tokens = budget.calls - before[0], budget.tokens - before[1]
    # Correctness dominates. Small bounded efficiency reward breaks ties.
    score = 0.0 if not passed else 0.9 + 0.1 / (1 + elapsed / 60 + tokens / 10000 + calls / 10)
    usage = [e.get("payload", {}) for e in events if e.get("kind") == "usage_event"]
    children = [e.get("payload", {}) for e in events if e.get("kind") == "child_admitted"]
    failures = [
        e.get("payload", {}) for e in events
        if e.get("kind") in {"child_rejected", "worker_failed", "merge_failed"}
        or (e.get("kind") == "result" and e.get("payload", {}).get("status") == "failed")
    ]
    task_providers: dict[str, set[str]] = {}
    for event in events:
        if event.get("kind") == "usage_event":
            task_providers.setdefault(event.get("task_id", "unknown"), set()).add(
                event.get("payload", {}).get("provider", "unknown")
            )
    row = {
        "id": case["id"], "split": case["split"], "passed": passed, "score": score,
        "elapsed_s": round(elapsed, 3), "calls": calls, "tokens": tokens,
        "cost_usd": budget.cost_usd - before[2], "head": accepted, "base": base,
        "changed": changed, "providers": sorted({e.get("provider", "unknown") for e in usage}),
        "children": len(children), "directory": str(root),
        "task_providers": {k: sorted(v) for k, v in task_providers.items()},
        "child_policies": [
            {k: e["payload"].get(k) for k in (
                "child_task_id", "resolved_context_mode", "resolved_placement",
            )}
            for e in events if e.get("kind") == "context_fork"
        ],
        "feedback": (
            f"exit={exit_code}; check={checked}; scope={scope_ok}; {error}\n"
            f"{diagnostic}\n{json.dumps(failures)[-3000:]}"
        ),
    }
    budget.rows.append(row)
    (root / "report.json").write_text(json.dumps(row, indent=2) + "\n")
    return row
