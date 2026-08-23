#!/usr/bin/env python3
"""Bounded real-provider interactive-session soak.

The soak deliberately uses the OpenCode auth file only as an input source. It
never prints credentials, provider config values, prompts, or worker output.
It clones the requested repository, runs ten sequential interactive turns,
and records only bounded operational evidence (duration, usage counts,
budget, and outcome).

Example::

    PYTHONPATH=src python scripts/soak.py --provider opencode-zen

The command is opt-in and is not part of the default pytest suite.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

# Permit ``python scripts/soak.py`` from a source checkout without installing
# the package first.
_ROOT = Path(__file__).resolve().parents[1]
_SRC = _ROOT / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from cambium.interactive import InteractiveSession, InteractiveTurn  # noqa: E402
from cambium.oneshot import OneShotConfig  # noqa: E402

_DEFAULT_AUTH = Path.home() / ".local" / "share" / "opencode" / "auth.json"
_DEFAULT_PROVIDER_CONFIG = Path.home() / ".config" / "cambium" / "providers.json"
_SAFE_ENV_NAME = "CAMBIUM_PROVIDER_OPENCODE_ZEN_API_KEY"


@dataclass(frozen=True, slots=True)
class Evidence:
    turn: int
    fault: str
    duration_s: float
    budget_s: float | None
    output_tokens: int
    tool_timeout: bool
    cancelled: bool
    outcome: str


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", type=Path, default=_ROOT, help="repository to clone and soak")
    parser.add_argument("--provider", default="opencode-zen")
    parser.add_argument("--model", default=None)
    parser.add_argument("--auth-path", type=Path, default=_DEFAULT_AUTH)
    parser.add_argument("--provider-config", type=Path, default=_DEFAULT_PROVIDER_CONFIG)
    parser.add_argument("--turns", type=int, default=10)
    parser.add_argument(
        "--cancel-turn",
        type=int,
        default=5,
        help="1-based turn to cancel after run_task (default: 5)",
    )
    parser.add_argument(
        "--shell-timeout-turn",
        type=int,
        default=1,
        help="1-based turn that must observe a run_shell timeout (default: 1)",
    )
    parser.add_argument(
        "--throughput-hint",
        type=float,
        default=20.3,
        help="static output-token/s hint for the first turn (default: 20.3)",
    )
    parser.add_argument(
        "--max-seconds",
        type=float,
        default=3_600.0,
        help="overall soak bound (default: 3600 seconds)",
    )
    parser.add_argument(
        "--cancel-delay",
        type=float,
        default=0.5,
        help="seconds after run_task before cancellation (default: 0.5)",
    )
    return parser


def _read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"cannot read soak input {path}: {type(exc).__name__}") from exc


def _secret_from_auth(document: Any, provider: str) -> str:
    """Extract an API key without ever returning it to output code."""
    if not isinstance(document, dict):
        raise RuntimeError("OpenCode auth document is not an object")
    names = (
        provider,
        provider.replace("-", "_"),
        "opencode",
        "opencode-go",
        "zai-coding-plan",
    )
    fields = ("key", "apiKey", "api_key", "access", "token")
    for name in names:
        value = document.get(name)
        if not isinstance(value, dict):
            continue
        for field in fields:
            secret = value.get(field)
            if isinstance(secret, str) and secret:
                return secret
    # Some OpenCode versions wrap provider records one level deeper. Restrict
    # this fallback to credential-shaped fields; never dump arbitrary JSON.
    for value in document.values():
        if not isinstance(value, dict):
            continue
        for field in fields:
            secret = value.get(field)
            if isinstance(secret, str) and secret:
                return secret
    raise RuntimeError(
        f"no usable credential record for {provider!r} in the OpenCode auth file"
    )


def _provider_entry(path: Path, provider: str) -> dict[str, Any]:
    document = _read_json(path)
    entries = document.get("providers") if isinstance(document, dict) else None
    if not isinstance(entries, list):
        raise RuntimeError("Cambium provider config has no providers list")
    for entry in entries:
        if isinstance(entry, dict) and entry.get("name") == provider:
            return dict(entry)
    raise RuntimeError(f"provider {provider!r} is not configured")


def _write_soak_provider_config(
    source: Path,
    destination: Path,
    provider: str,
    throughput_hint: float,
) -> tuple[str, str]:
    entry = _provider_entry(source, provider)
    model = entry.get("model")
    tier = entry.get("tier")
    if not isinstance(model, str) or not model:
        raise RuntimeError("soak provider has no configured model")
    if not isinstance(tier, str) or not tier:
        raise RuntimeError("soak provider has no configured tier")
    entry["api_key_env"] = _SAFE_ENV_NAME
    entry["throughput_hint_tps"] = throughput_hint
    entry.pop("interactive_wall_budget_s", None)
    destination.write_text(
        json.dumps({"providers": [entry]}, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )
    return model, tier


def _clone(repo: Path, destination: Path) -> None:
    try:
        subprocess.run(
            ["git", "clone", "--quiet", "--no-hardlinks", str(repo), str(destination)],
            check=True,
            capture_output=True,
            text=True,
        )
        # A source worktree may have a feature branch checked out while its
        # shared repository's main branch is not advertised as the clone's
        # remote HEAD. The supervisor contract requires refs/heads/main.
        subprocess.run(
            ["git", "-C", str(destination), "branch", "-f", "main", "HEAD"],
            check=True,
            capture_output=True,
        )
        subprocess.run(
            ["git", "-C", str(destination), "config", "user.name", "cambium-soak"],
            check=True,
            capture_output=True,
        )
        subprocess.run(
            [
                "git",
                "-C",
                str(destination),
                "config",
                "user.email",
                "cambium-soak@example.invalid",
            ],
            check=True,
            capture_output=True,
        )
        subprocess.run(
            ["git", "-C", str(destination), "config", "gc.auto", "0"],
            check=True,
            capture_output=True,
        )
    except (OSError, subprocess.CalledProcessError) as exc:
        raise RuntimeError(f"cannot create soak clone: {type(exc).__name__}") from exc


def _output_tokens(events: list[dict[str, Any]]) -> int:
    total = 0
    for event in events:
        if event.get("kind") != "usage_event":
            continue
        payload = event.get("payload")
        usage = payload.get("usage") if isinstance(payload, dict) else None
        if not isinstance(usage, dict):
            continue
        value = usage.get("output_tokens", usage.get("completion_tokens", 0))
        if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
            total += value
    return total


def _planned_budget(session_dir: Path) -> float | None:
    try:
        document = json.loads((session_dir / "plan.json").read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return None
    tasks = document.get("tasks") if isinstance(document, dict) else None
    if not isinstance(tasks, list) or not tasks or not isinstance(tasks[0], dict):
        return None
    value = tasks[0].get("max_wall_s")
    return float(value) if isinstance(value, int | float) and not isinstance(value, bool) else None


def _result_outcome(response: Any, error_type: str | None = None) -> str:
    # Keep provider failure details out of the evidence table; they can contain
    # transport text that should never be mistaken for safe soak output.
    if error_type is not None:
        return f"error:{error_type}"
    return "succeeded" if getattr(response, "exit_code", None) == 0 else "failed"


def _normal_prompt(turn: int) -> str:
    return (
        f"This is bounded soak turn {turn}. Make no file changes and use no tools. "
        f"Return exactly one finish action with summary SOAK_OK_{turn}."
    )


def _shell_timeout_prompt(turn: int) -> str:
    return (
        f"This is bounded soak fault turn {turn}. The fault is intentional. First emit a "
        'plan action, then exactly one tool_call action using run_shell with '
        'arguments {"command":["sleep","2"],"timeout_s":0.1}. '
        "After the expected timeout, emit one finish action with summary "
        "SOAK_TIMEOUT_HANDLED. Do not modify files."
    )


def _cancel_prompt(turn: int) -> str:
    return (
        f"This is bounded soak cancellation turn {turn}. First emit a plan action, then "
        'call run_shell with arguments {"command":["sleep","30"],"timeout_s":60}. '
        "Do not finish before the shell call returns; no files may be changed."
    )


async def _run_one(
    session: InteractiveSession,
    turn: InteractiveTurn,
    *,
    fault: str,
    cancel_delay: float,
) -> Evidence:
    events: list[dict[str, Any]] = []
    started = asyncio.Event()

    def sink(event: dict[str, Any]) -> None:
        events.append(event)
        if event.get("kind") == "run_task":
            started.set()

    began = time.monotonic()
    cancelled = False
    response: Any = None
    error_type: str | None = None
    try:
        task = asyncio.create_task(session.run_turn(turn, on_event=sink))
        if fault == "cancelled":
            try:
                await asyncio.wait_for(started.wait(), timeout=30.0)
            except TimeoutError:
                # A cancellation after supervisor admission is still a valid
                # mid-turn fault if provider resolution/startup is unusually slow.
                pass
            await asyncio.sleep(max(0.0, cancel_delay))
            if not task.done():
                task.cancel()
            try:
                response = await task
            except asyncio.CancelledError:
                cancelled = True
        else:
            response = await task
    except asyncio.CancelledError:
        cancelled = True
    except Exception as exc:  # noqa: BLE001 - evidence records only the type
        error_type = type(exc).__name__

    duration = time.monotonic() - began
    tool_timeout = any(
        event.get("kind") == "tool_event"
        and isinstance(event.get("payload"), dict)
        and event["payload"].get("tool") == "run_shell"
        and event["payload"].get("ok") is False
        for event in events
    )
    if cancelled:
        session.complete_turn(turn, succeeded=False)
        outcome = "cancelled"
    else:
        outcome = _result_outcome(response, error_type)
        session.complete_turn(turn, succeeded=outcome == "succeeded")
    return Evidence(
        turn=turn.number,
        fault=fault,
        duration_s=duration,
        budget_s=_planned_budget(turn.session_dir),
        output_tokens=_output_tokens(events),
        tool_timeout=tool_timeout,
        cancelled=cancelled,
        outcome=outcome,
    )


async def _soak(args: argparse.Namespace) -> list[Evidence]:
    if args.turns != 10:
        raise RuntimeError("the bounded soak contract requires exactly --turns 10")
    if not 1 <= args.cancel_turn <= args.turns:
        raise RuntimeError("--cancel-turn must be within the turn count")
    if not 1 <= args.shell_timeout_turn <= args.turns:
        raise RuntimeError("--shell-timeout-turn must be within the turn count")
    if args.throughput_hint <= 0 or args.max_seconds <= 0:
        raise RuntimeError("throughput hint and max-seconds must be positive")

    auth_path = args.auth_path.expanduser().resolve()
    provider_config_path = args.provider_config.expanduser().resolve()
    secret = _secret_from_auth(_read_json(auth_path), args.provider)
    source_repo = args.repo.expanduser().resolve()
    if not (source_repo / ".git").exists():
        raise RuntimeError("soak repository is not a git repository")

    with tempfile.TemporaryDirectory(prefix="cambium-soak-") as temporary:
        root = Path(temporary)
        clone = root / "repo"
        _clone(source_repo, clone)
        provider_config = root / "providers.json"
        model, _tier = _write_soak_provider_config(
            provider_config_path,
            provider_config,
            args.provider,
            args.throughput_hint,
        )
        session_root = root / "interactive"
        # The child worker inherits only this in-memory environment. The secret
        # is never passed in argv, written to disk, or included in evidence.
        old_provider_path = os.environ.get("CAMBIUM_PROVIDERS")
        old_secret = os.environ.get(_SAFE_ENV_NAME)
        os.environ["CAMBIUM_PROVIDERS"] = str(provider_config)
        os.environ[_SAFE_ENV_NAME] = secret
        try:
            config = OneShotConfig(
                repo=clone,
                session_root=session_root,
                provider=args.provider,
                model=args.model or model,
                provider_config_path=provider_config,
                interactive=True,
                max_wall_s=None,
                max_tokens=40_000,
                max_turns=8,
                max_restarts=0,
                # This soak targets supervisor turn lifecycle and fault
                # recovery. Disable the optional summary-provider call so a
                # transient free-provider failure in compaction cannot mask
                # whether the next interactive turn remains usable.
                context_reuse=False,
            )
            session = InteractiveSession(config)
            evidence: list[Evidence] = []
            for number in range(1, args.turns + 1):
                if number == args.shell_timeout_turn:
                    fault = "shell-timeout"
                    prompt = _shell_timeout_prompt(number)
                elif number == args.cancel_turn:
                    fault = "cancelled"
                    prompt = _cancel_prompt(number)
                else:
                    fault = "none"
                    prompt = _normal_prompt(number)
                turn = session.prepare_turn(prompt)
                evidence.append(
                    await _run_one(
                        session,
                        turn,
                        fault=fault,
                        cancel_delay=args.cancel_delay,
                    )
                )
            return evidence
        finally:
            if old_provider_path is None:
                os.environ.pop("CAMBIUM_PROVIDERS", None)
            else:
                os.environ["CAMBIUM_PROVIDERS"] = old_provider_path
            if old_secret is None:
                os.environ.pop(_SAFE_ENV_NAME, None)
            else:
                os.environ[_SAFE_ENV_NAME] = old_secret


def _print_evidence(rows: list[Evidence]) -> None:
    print("turn fault duration_s budget_s output_tokens tool_timeout cancelled outcome")
    for row in rows:
        budget = "-" if row.budget_s is None else f"{row.budget_s:.1f}"
        print(
            f"{row.turn:>4} {row.fault:<13} {row.duration_s:>10.2f} {budget:>8} "
            f"{row.output_tokens:>12} {str(row.tool_timeout):>11} "
            f"{str(row.cancelled):>9} {row.outcome}"
        )


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        evidence = asyncio.run(asyncio.wait_for(_soak(args), timeout=args.max_seconds))
        _print_evidence(evidence)
        shell_rows = [row for row in evidence if row.fault == "shell-timeout"]
        cancel_rows = [row for row in evidence if row.fault == "cancelled"]
        if len(evidence) != 10:
            raise RuntimeError("soak did not complete ten turns")
        if not shell_rows or not shell_rows[0].tool_timeout:
            raise RuntimeError("shell-timeout fault was not observed in durable tool events")
        if not cancel_rows or not cancel_rows[0].cancelled:
            raise RuntimeError("mid-turn cancellation was not observed")
        cancel_turn = cancel_rows[0].turn
        next_rows = [row for row in evidence if row.turn > cancel_turn]
        if not next_rows or next_rows[0].outcome != "succeeded":
            raise RuntimeError("the turn after cancellation did not succeed")
        failed = [row for row in evidence if row.outcome.startswith("failed")]
        if failed:
            raise RuntimeError("one or more non-cancelled soak turns failed")
    except (RuntimeError, TimeoutError) as exc:
        print(f"soak: FAIL: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    except Exception as exc:  # noqa: BLE001 - never expose provider exception text
        print(f"soak: FAIL: unexpected {type(exc).__name__}", file=sys.stderr)
        return 1
    print("soak: PASS (10 sequential turns; session recovered after both injected faults)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
