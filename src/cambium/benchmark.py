"""Finite repository-task rollouts through the normal Cambium supervisor.

Fixtures and verification commands are operator-owned. Each rollout uses a
private repository; only its accepted Git head is checked. No DSPy at runtime.
"""

from __future__ import annotations

import asyncio
import json
import math
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
from .worker import MAX_CONSECUTIVE_INVALID_ACTIONS


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
            self.calls >= self.max_calls
            or self.tokens >= self.max_tokens
            or self.cost_usd >= self.max_usd
        ):
            raise ExperimentBudgetExceeded("experiment call, token, or cash budget exhausted")

    def record(self, usage: dict, cost: float = 0.0) -> None:
        self.calls += 1
        self.tokens += int(
            usage.get("total_tokens", 0)
            or (
                usage.get("prompt_tokens", usage.get("input_tokens", 0))
                + usage.get("completion_tokens", usage.get("output_tokens", 0))
            )
        )
        self.cost_usd += max(0.0, float(cost))


def json_finite(value: Any) -> Any:
    """Copy value with non-finite floats mapped to 0.0 so strict JSON cannot fail.

    Provider-reported numbers can be non-finite (an ``inf`` ``estimated_cost_usd``
    genuinely accumulates into ``budget.cost_usd``; nan/-inf are clamped to 0.0
    by ``ExperimentBudget.record``), which ``json.dumps(allow_nan=False)``
    rejects and strict readers refuse.  Dicts and lists (nested) are rebuilt;
    ints, strings including numeric strings, bools, and None pass through.
    Report payloads are JSON-loaded or literal, so keys are always strings.
    """
    if isinstance(value, float):
        return value if math.isfinite(value) else 0.0
    if isinstance(value, dict):
        return {key: json_finite(item) for key, item in value.items()}
    if isinstance(value, list | tuple):
        return [json_finite(item) for item in value]
    return value


def write_json_report(path: Path, payload: Any) -> None:
    """Write an experiment report as strict JSON; non-finite floats become 0.0."""
    text = json.dumps(json_finite(payload), indent=2, allow_nan=False) + "\n"
    path.write_text(text, encoding="utf-8")


def load_cases(path: Path) -> list[dict[str, Any]]:
    cases = [json.loads(line) for line in path.read_text().splitlines() if line.strip()]
    if (
        not cases
        or not all(isinstance(c.get("id"), str) and c["id"] for c in cases)
        or len({case["id"] for case in cases}) != len(cases)
    ):
        raise ValueError("benchmark cases need unique ids")
    families: dict[str, str] = {}
    for case in cases:
        family = case.get("family", case["id"])
        if family in families and families[family] != case.get("split"):
            raise ValueError(f"benchmark family {family} crosses search/evaluation splits")
        families[family] = case.get("split")
        if not all(isinstance(text, str) and text.strip() for text in case.get("followups", [])):
            raise ValueError("benchmark followups must be non-empty task strings")
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


def _root_summaries(result: Any, task_id: str) -> list[str]:
    """Return the one user-facing root summary, if the plan produced one."""
    selected = next(
        (item for item in result.results if item.task_id == task_id),
        result.results[0] if result.results else None,
    )
    return [selected.summary] if selected is not None and selected.summary else []


def _git(repo: Path, *args: str) -> str:
    return subprocess.check_output(
        ["git", "-C", str(repo), *args],
        text=True,
        stderr=subprocess.PIPE,
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


def _peak_pending_children(events: list[dict]) -> int:
    """Count overlapping siblings, including queued work, not a lifetime total."""
    pending: dict[str, set[str]] = {}
    peak = 0
    for event in events:
        data = event.get("payload", {})
        if event.get("kind") == "child_admitted":
            parent = data.get("parent_task_id") or event.get("task_id")
            children = pending.setdefault(parent, set())
            children.add(data["child_task_id"])
            peak = max(peak, len(children))
        elif event.get("kind") == "child_result":
            pending.get(data.get("parent_task_id"), set()).discard(event.get("task_id"))
    return peak


def _breaker_strike(event: dict) -> bool:
    """True when the supervisor logged the agent loop's final invalid-action strike."""
    if event.get("kind") != "log":
        return False
    message = event.get("payload", {}).get("message")
    return (
        isinstance(message, str)
        and message.startswith("invalid_action:")
        and message.endswith(f" (strike {MAX_CONSECUTIVE_INVALID_ACTIONS})")
    )


def _case_provider_pool(resolved: Any, case: dict) -> Any:
    """Restrict one case's resolved provider to its declared credential-ready pool."""
    if not case.get("providers"):
        return resolved
    from .provider_config import load_providers

    names = set(case["providers"])
    available = [
        p
        for p in load_providers(resolved.provider_config_path)
        if p.name in names and p.name in resolved.authorized_providers
    ]
    if {p.name for p in available} != names:
        raise ValueError("benchmark provider pool is not credential-ready")
    if resolved.assigned_provider and resolved.assigned_provider not in names:
        raise ValueError("benchmark primary provider is outside its provider pool")
    return replace(
        resolved,
        authorized_providers=tuple(p.name for p in available),
        model_candidates=tuple(sorted({p.model for p in available})),
    )


def run_case(  # noqa: C901 - one rollout owns setup, execution and artifact checking
    case: dict,
    policy: dict[str, str],
    *,
    output: Path,
    budget: ExperimentBudget,
    provider: str | None = None,
    max_turns: int = 12,
    max_wall_s: float = 300,
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
    turn_heads: list[str] = []
    user_summaries: list[str] = []
    started = time.monotonic()
    before = (budget.calls, budget.tokens, budget.cost_usd)
    config = OneShotConfig(
        prompt=case["task"],
        repo=repo,
        session_root=root / "session",
        provider=provider,
        max_turns=max_turns,
        max_wall_s=max_wall_s,
        max_restarts=0,
        max_tokens=max(1, budget.max_tokens - budget.tokens),
        prompt_policy=policy,
    )

    async def execute() -> tuple[int, str]:
        owner = asyncio.current_task()

        def observe(event: dict) -> None:
            nonlocal abort_reason
            events.append(event)
            if abort_reason is None and _breaker_strike(event):
                # The agent-loop breaker has decided this rollout FAILED: the
                # worker returns failed immediately and no later result can
                # un-fail the plan (any non-succeeded task forces exit 1).
                # Cancel like the budget path below instead of burning provider
                # calls to the wall; the verdict fields, including the breaker
                # reason, are unchanged.
                abort_reason = (
                    f"agent emitted {MAX_CONSECUTIVE_INVALID_ACTIONS} consecutive invalid actions"
                )
                if owner is not None:
                    owner.cancel()
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
        resolved = _case_provider_pool(resolved, case)
        if case.get("followups"):
            from .interactive import InteractiveSession

            session = InteractiveSession(
                replace(
                    resolved,
                    routing_state_path=root / "routing.json",
                )
            )
            session.acquire()
            try:
                for number, prompt in enumerate([case["task"], *case["followups"]], 1):
                    budget.check()
                    turn = session.prepare_turn(prompt)

                    def live(event: dict, current=turn, owner=session) -> None:
                        owner.observe_event(current, event)
                        observe(event)

                    result = await session.run_turn(
                        turn,
                        on_event=live,
                        max_concurrent_tasks=max_workers,
                    )
                    user_summaries.extend(_root_summaries(result, turn.config.task_id))
                    session.complete_turn(turn, succeeded=result.exit_code == 0)
                    turn_heads.append(_git(repo, "rev-parse", "main"))
                    if result.exit_code:
                        break
                    if number in case.get("compact_after_turns", []):
                        session.compact()
                    if case.get("reconnect_between_turns"):
                        session.release()
                        session = InteractiveSession(
                            replace(
                                resolved,
                                routing_state_path=root / "routing.json",
                            )
                        )
                        session.acquire()
            finally:
                session.release()
        else:
            plan = build_plan(resolved, repo, root / "session")
            result = await run_plan(
                root / "session",
                plan,
                provider_environment=environment,
                routing_state_path=root / "routing.json",
                on_event=observe,
                max_concurrent_tasks=max_workers,
                context_reuse=True,
            )
            user_summaries.extend(_root_summaries(result, plan["tasks"][0]["task_id"]))
            turn_heads.append(_git(repo, "rev-parse", "main"))
        return result.exit_code, "; ".join(r.reason for r in result.results if r.reason)

    async def bounded_execute() -> tuple[int, str]:
        # One existing wall budget covers the whole case, including follow-ups,
        # child joins and reconnects; it must not restart on each operator turn.
        async with asyncio.timeout(max_wall_s):
            return await execute()

    error = ""
    abort_reason: str | None = None
    try:
        exit_code, error = asyncio.run(bounded_execute())
    except TimeoutError:
        exit_code, error = 1, f"rollout wall budget exhausted ({max_wall_s:g}s)"
    except asyncio.CancelledError:
        exit_code = 1
        error = abort_reason or "experiment budget exhausted; rollout cancelled"
    elapsed = time.monotonic() - started
    from .branch_history import _session_event_stores
    from .store import read_events_file

    stores = _session_event_stores(root / "session")
    if stores:
        events = [event for store in stores for event in read_events_file(store)]
    accepted = _git(repo, "rev-parse", "main")
    changed = _git(repo, "diff", "--name-only", base, accepted).splitlines()
    # The checker sees accepted code, never an uncommitted worker tree.
    verify = root / "accepted"
    _git(repo, "worktree", "add", "--quiet", "--detach", str(verify), accepted)
    command = [sys.executable if part == "{python}" else part for part in case["check"]]
    check_env = dict(os.environ, PYTHONPATH=str(verify / "src"))
    try:
        check = subprocess.run(
            command,
            cwd=verify,
            env=check_env,
            capture_output=True,
            text=True,
            timeout=60,
        )
        checked = check.returncode == 0
        diagnostic = (check.stdout + check.stderr)[-4000:]
    except subprocess.TimeoutExpired:
        checked, diagnostic = False, "verification timed out"
    allowed = case.get("allowed_files")
    scope_ok = allowed is None or set(changed) <= set(allowed)
    observed_tools = {
        e.get("payload", {}).get("tool")
        for e in events
        if e.get("kind") == "tool_event" and e.get("payload", {}).get("ok")
    }
    missing_tools = set(case.get("required_tools", [])) - observed_tools
    children = [e.get("payload", {}) for e in events if e.get("kind") == "child_admitted"]
    peak_children = _peak_pending_children(events)
    trace_ok = (
        not missing_tools
        and len(children) >= case.get("required_children", 0)
        and peak_children >= case.get("required_parallel_children", 0)
    )
    if case.get("read_only"):
        trace_ok = trace_ok and accepted == base
    if case.get("read_only_followups"):
        trace_ok = trace_ok and bool(turn_heads) and all(h == turn_heads[0] for h in turn_heads)
    rollovers = sum(
        e.get("kind") == "context_epoch_advanced"
        and e.get("payload", {}).get("reason") == "manual K0 rollover"
        for e in events
    )
    trace_ok = trace_ok and rollovers >= case.get("required_rollovers", 0)
    passed = exit_code == 0 and checked and scope_ok and trace_ok
    calls, tokens = budget.calls - before[0], budget.tokens - before[1]
    user_summary_chars = sum(len(text.strip()) for text in user_summaries)
    user_summary_lines = sum(
        len(text.strip().splitlines()) for text in user_summaries if text.strip()
    )
    # Correctness dominates. Resource use and operator-visible verbosity only break ties.
    score = (
        0.0
        if not passed
        else 0.9
        + 0.1
        / (
            1
            + elapsed / 60
            + tokens / 10000
            + calls / 10
            + user_summary_chars / 1000
        )
    )
    usage = [e.get("payload", {}) for e in events if e.get("kind") == "usage_event"]
    failures = [
        e.get("payload", {})
        for e in events
        if e.get("kind") in {"child_rejected", "worker_failed", "merge_failed"}
        or (e.get("kind") == "result" and e.get("payload", {}).get("status") == "failed")
    ]
    task_providers: dict[str, set[str]] = {}
    for event in events:
        if event.get("kind") == "usage_event" and not event.get("payload", {}).get(
            "failure_reason"
        ):
            task_providers.setdefault(event.get("task_id", "unknown"), set()).add(
                event.get("payload", {}).get("provider", "unknown")
            )

    def reported_tokens(key: str, alternate: str = "") -> int:
        return sum(
            (item.get("usage") or {}).get(key, (item.get("usage") or {}).get(alternate, 0)) or 0
            for item in usage
        )

    row = {
        "id": case["id"],
        "split": case["split"],
        "passed": passed,
        "score": score,
        "elapsed_s": round(elapsed, 3),
        "calls": calls,
        "tokens": tokens,
        "cost_usd": budget.cost_usd - before[2],
        "head": accepted,
        "base": base,
        "changed": changed,
        "providers": sorted({name for names in task_providers.values() for name in names}),
        "children": len(children),
        "peak_pending_children": peak_children,
        "directory": str(root),
        "turn_heads": turn_heads,
        "source": case.get("source"),
        "family": case.get("family"),
        "rollovers": rollovers,
        "summary_calls": sum(e.get("call_kind") == "summary" for e in usage),
        "user_summary_chars": user_summary_chars,
        "user_summary_lines": user_summary_lines,
        "failed_provider_calls": sum(bool(e.get("failure_reason")) for e in usage),
        "malformed_actions": sum(
            e.get("kind") == "log"
            and str(e.get("payload", {}).get("message", "")).startswith("invalid_action:")
            for e in events
        ),
        "tool_failures": sum(
            e.get("kind") == "tool_event" and e.get("payload", {}).get("ok") is False
            for e in events
        ),
        "output_tokens": reported_tokens("completion_tokens", "output_tokens"),
        "cached_tokens": reported_tokens("cached_tokens", "cache_read_input_tokens"),
        "task_providers": {k: sorted(v) for k, v in task_providers.items()},
        "child_policies": [
            {
                k: e["payload"].get(k)
                for k in (
                    "child_task_id",
                    "resolved_context_mode",
                    "resolved_placement",
                )
            }
            for e in events
            if e.get("kind") == "context_fork"
        ],
        "feedback": (
            f"exit={exit_code}; check={checked}; scope={scope_ok}; trace={trace_ok}; "
            f"missing_tools={sorted(missing_tools)}; "
            f"parallel_children={peak_children}/{case.get('required_parallel_children', 0)}; "
            f"{error}\n"
            f"{diagnostic}\n{json.dumps(json_finite(failures), allow_nan=False)[-3000:]}"
        ),
    }
    budget.rows.append(row)
    write_json_report(root / "report.json", row)
    return row
